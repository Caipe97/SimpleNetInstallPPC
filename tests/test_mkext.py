import os
import plistlib
import struct
import tempfile
import unittest
from pathlib import Path

from netboot_server.imaging import mkext


def make_kext(folder: Path, name: str, required, binary: bytes = b"\xfe\xed\xfa\xce" + b"x" * 500,
              old_style: bool = False) -> Path:
    kext = folder / f"{name}.kext"
    info = {"CFBundleIdentifier": f"com.example.{name}", "CFBundleExecutable": name}
    if required is not None:
        info["OSBundleRequired"] = required
    if old_style:
        kext.mkdir(parents=True)
        (kext / "Info-macos.xml").write_bytes(plistlib.dumps(info))
        (kext / name).write_bytes(binary)
    else:
        (kext / "Contents" / "MacOS").mkdir(parents=True)
        (kext / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
        (kext / "Contents" / "MacOS" / name).write_bytes(binary)
    return kext


class LzssTests(unittest.TestCase):
    def test_round_trip(self):
        samples = [b"", b"a", b"abcabcabcabcabcabc" * 50, os.urandom(10000),
                   b" " * 5000, bytes(range(256)) * 40 + b"tail"]
        for data in samples:
            packed = mkext.compress_lzss(data)
            self.assertEqual(mkext.decompress_lzss(packed, len(data)), data)

    def test_compresses_repetitive_data(self):
        data = b"IOKitPersonalities" * 1000
        self.assertLess(len(mkext.compress_lzss(data)), len(data) // 5)

    def test_decodes_reference_to_prefilled_spaces(self):
        # Apple's encoder may point into the ring's initial spaces: flag byte
        # 0 (a match), ring index 0, length 3+15 = 18 spaces.
        self.assertEqual(mkext.decompress_lzss(bytes((0x00, 0x00, 0x0F)), 18), b" " * 18)


class MkextTests(unittest.TestCase):
    def test_build_and_parse(self):
        entries = [mkext.MkextEntry(plistlib.dumps({"CFBundleIdentifier": "a"}), b"\xfe\xed" * 300, 1, 2),
                   mkext.MkextEntry(plistlib.dumps({"CFBundleIdentifier": "b"}), None, 3, 0)]
        for compress in (True, False):
            data = mkext.build_mkext(entries, compress=compress)
            self.assertEqual(data[:8], b"MKXTMOSX")
            self.assertEqual(struct.unpack_from(">I", data, 8)[0], len(data))
            self.assertEqual(struct.unpack_from(">I", data, 16)[0], mkext.VERSION)
            parsed = mkext.parse_mkext(data)
            self.assertEqual([e.identifier for e in parsed.entries], ["a", "b"])
            self.assertEqual(parsed.entries[0].module, b"\xfe\xed" * 300)
            self.assertIsNone(parsed.entries[1].module)
            self.assertEqual(parsed.entries[0].module_mtime, 2)

    def test_corruption_detected(self):
        data = bytearray(mkext.build_mkext([mkext.MkextEntry(b"<plist/>", b"x" * 100)]))
        data[-1] ^= 0xFF
        with self.assertRaises(ValueError):
            mkext.parse_mkext(bytes(data))

    def test_thin_fat_binary(self):
        ppc, i386 = b"\xfe\xed\xfa\xce" + b"P" * 60, b"\xce\xfa\xed\xfe" + b"I" * 60
        header = struct.pack(">II", mkext.FAT_MAGIC, 2)
        header += struct.pack(">iiIII", 18, 0, 64, len(ppc), 0)
        header += struct.pack(">iiIII", 7, 3, 128, len(i386), 0)
        fat = header.ljust(64, b"\0") + ppc.ljust(64, b"\0") + i386
        self.assertEqual(mkext.thin(fat, "ppc"), ppc)
        self.assertEqual(mkext.thin(fat, "i386"), i386)
        self.assertEqual(mkext.thin(ppc, "ppc"), ppc)

    def test_collect_filters_and_handles_old_bundles(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext = Path(tmp) / "System/Library/Extensions"
            ext.mkdir(parents=True)
            family = make_kext(ext, "Family", "Root")
            make_kext(family / "Contents" / "PlugIns", "Plugin", "Local-Root")
            make_kext(ext, "Audio", None)
            make_kext(ext, "msdosfs", "Local-Root", old_style=True)
            out = Path(tmp) / "out.mkext"
            report = mkext.build_from_volume(Path(tmp), "ppc", out)
            self.assertEqual(sorted(report.included),
                             ["com.example.Family", "com.example.Plugin", "com.example.msdosfs"])
            self.assertEqual(report.skipped, ["com.example.Audio"])
            parsed = mkext.parse_mkext(out.read_bytes())
            self.assertEqual(len(parsed.entries), 3)
            everything = mkext.build_from_volume(Path(tmp), "ppc", out, include_all=True)
            self.assertEqual(len(everything.included), 4)


if __name__ == "__main__":
    unittest.main()

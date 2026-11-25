"""Multi-CD merge: growing HFS+ in place, and replacing proxy packages."""

import os
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from netboot_server.imaging.merge import (PROXY_KEY, Disc, apply_merge, copy_tree_portable,
                                          find_proxies, grow_hfsplus, parse_location, plan_merge)
from netboot_server.imaging.partitions import UnsupportedImage

BS = 4096


def fake_volume(path: Path, blocks: int, used=(0, 1), bitmap_bytes: int = BS,
                journaled: bool = False) -> None:
    """A bare HFS+ volume with just a volume header and a bitmap at block 1."""
    data = bytearray(blocks * BS)
    vh = bytearray(512)
    vh[:2] = b"H+"
    struct.pack_into(">HI", vh, 2, 4, (1 << 8) | ((1 << 13) if journaled else 0))
    used = set(used) | {blocks - 1}
    struct.pack_into(">III", vh, 40, BS, blocks, blocks - len(used))
    struct.pack_into(">QII", vh, 0x70, bitmap_bytes, BS, 1)
    struct.pack_into(">II", vh, 0x80, 1, 1)
    for block in used:
        data[BS + block // 8] |= 0x80 >> (block % 8)
    data[1024:1536] = vh
    data[blocks * BS - 1024:blocks * BS - 512] = vh
    path.write_bytes(bytes(data))


def bit(data: bytes, block: int) -> bool:
    return bool(data[BS + block // 8] & (0x80 >> (block % 8)))


class GrowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "vol.raw"

    def tearDown(self):
        self.tmp.cleanup()

    def test_grow_updates_header_bitmap_and_alternate_header(self):
        fake_volume(self.path, 16)
        result = grow_hfsplus(self.path, 32 * BS + 100)   # rounds down to blocks
        data = self.path.read_bytes()
        self.assertEqual(len(data), 32 * BS)
        self.assertEqual((result.old_blocks, result.new_blocks), (16, 32))
        _bs, total, free = struct.unpack_from(">III", data, 1024 + 40)
        self.assertEqual((total, free), (32, 32 - 3))       # blocks 0, 1 and 31 used
        self.assertFalse(bit(data, 15))                      # old alternate header block
        self.assertTrue(bit(data, 31))
        self.assertEqual(data[32 * BS - 1024:32 * BS - 512], data[1024:1536])
        self.assertEqual(data[16 * BS - 1024:16 * BS - 512], bytes(512))

    def test_no_shrinking(self):
        fake_volume(self.path, 16)
        self.assertEqual(grow_hfsplus(self.path, 8 * BS).new_blocks, 16)
        self.assertEqual(self.path.stat().st_size, 16 * BS)

    def test_refuses_journaled_and_too_small_bitmap(self):
        fake_volume(self.path, 16, journaled=True)
        with self.assertRaises(UnsupportedImage):
            grow_hfsplus(self.path, 32 * BS)
        fake_volume(self.path, 16, bitmap_bytes=2)          # covers 16 blocks only
        with self.assertRaises(UnsupportedImage):
            grow_hfsplus(self.path, 32 * BS)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("hdiutil"), "needs hdiutil")
    def test_real_volume_passes_fsck_after_growing(self):
        made = Path(self.tmp.name) / "real.dmg"
        subprocess.run(["hdiutil", "create", "-size", "8m", "-fs", "HFS+", "-layout", "NONE",
                        "-volname", "GrowTest", str(made)], check=True,
                       stdout=subprocess.DEVNULL)
        # hdiutil create makes a read/write UDIF; keep just the volume bytes.
        size = struct.unpack_from(">III", made.read_bytes(), 1024 + 40)
        self.path.write_bytes(made.read_bytes()[:size[0] * size[1]])
        attributes = struct.unpack_from(">I", self.path.read_bytes(), 1028)[0]
        if attributes & (1 << 13):
            self.skipTest("hdiutil made a journaled volume")
        grow_hfsplus(self.path, 24 * 2**20)
        dev = subprocess.run(["hdiutil", "attach", "-nomount", "-readonly", "-noverify",
                              "-imagekey", "diskimage-class=CRawDiskImage", str(self.path)],
                             check=True, capture_output=True, text=True).stdout.split()[0]
        try:
            fsck = subprocess.run(["fsck_hfs", "-n", "-f", dev], capture_output=True, text=True)
        finally:
            subprocess.run(["hdiutil", "detach", dev], stdout=subprocess.DEVNULL)
        self.assertIn("appears to be OK", fsck.stdout + fsck.stderr)


def write_package(path: Path, info: dict, payload: bool = True) -> None:
    (path / "Contents/Resources").mkdir(parents=True)
    with open(path / "Contents/Info.plist", "wb") as fh:
        plistlib.dump(info, fh)
    (path / "Contents/Archive.bom").write_bytes(b"BOM")
    if payload:
        (path / "Contents/Archive.pax.gz").write_bytes(b"PAYLOAD" * 100)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.disc1, self.disc2, self.disc2b = root / "d1", root / "d2", root / "d2b"
        packages = self.disc1 / "System/Installation/Packages"
        write_package(packages / "Fonts.pkg",
                      {PROXY_KEY: "disc://Install Disc 2/Fonts.pkg", "Version": "stub"},
                      payload=False)
        write_package(packages / "BSD.pkg", {"Version": "real"})
        write_package(packages / "Printers.mpkg/Contents/Packages/HP.pkg",
                      {PROXY_KEY: "disc://Install Disc 3/HP.pkg"}, payload=False)
        write_package(self.disc2 / "Fonts.pkg", {"Version": "disc2"})
        write_package(self.disc2b / "Fonts.pkg", {"Version": "disc2b"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_location(self):
        self.assertEqual(parse_location("disc://Mac OS X Install Disc 2/AdditionalFonts.pkg"),
                         ("Mac OS X Install Disc 2", "AdditionalFonts.pkg"))
        self.assertIsNone(parse_location("file://x/y.pkg"))
        self.assertIsNone(parse_location("disc://Disc 2/../etc"))
        self.assertIsNone(parse_location("disc://Disc 2/"))

    def test_finds_top_level_and_nested_proxies(self):
        proxies = {p.path: (p.disc, p.location) for p in find_proxies(self.disc1)}
        self.assertEqual(proxies, {
            "System/Installation/Packages/Fonts.pkg": ("Install Disc 2", "Fonts.pkg"),
            "System/Installation/Packages/Printers.mpkg/Contents/Packages/HP.pkg":
                ("Install Disc 3", "HP.pkg"),
        })

    def test_plan_prefers_the_named_disc_and_reports_missing(self):
        plan = plan_merge(self.disc1, [Disc(self.disc2b, "Other"),
                                       Disc(self.disc2, "Install Disc 2")])
        [(proxy, disc, source)] = plan.replacements
        self.assertEqual(disc.root, self.disc2)
        self.assertEqual([p.location for p in plan.missing], ["HP.pkg"])
        self.assertGreater(plan.payload_bytes, 0)

    def test_plan_falls_back_to_path_when_names_are_unknown(self):
        plan = plan_merge(self.disc1, [Disc(self.disc2b), Disc(self.disc2)])
        self.assertEqual(plan.replacements[0][1].root, self.disc2b)

    def test_a_proxy_on_another_disc_does_not_count(self):
        shutil.rmtree(self.disc2 / "Fonts.pkg")
        write_package(self.disc2 / "Fonts.pkg", {PROXY_KEY: "disc://Install Disc 4/Fonts.pkg"},
                      payload=False)
        plan = plan_merge(self.disc1, [Disc(self.disc2, "Install Disc 2")])
        self.assertEqual(plan.replacements, [])
        self.assertEqual(len(plan.missing), 2)

    def test_apply_replaces_the_proxy_with_the_real_package(self):
        plan = plan_merge(self.disc1, [Disc(self.disc2, "Install Disc 2")])
        done = apply_merge(plan, self.disc1, copier=copy_tree_portable)
        self.assertEqual(done, ["System/Installation/Packages/Fonts.pkg"])
        merged = self.disc1 / "System/Installation/Packages/Fonts.pkg/Contents"
        self.assertEqual((merged / "Archive.pax.gz").read_bytes(), b"PAYLOAD" * 100)
        with open(merged / "Info.plist", "rb") as fh:
            self.assertEqual(plistlib.load(fh), {"Version": "disc2"})
        remaining = [p.location for p in find_proxies(self.disc1)]
        self.assertEqual(remaining, ["HP.pkg"])

    def test_portable_copy_keeps_modes_and_symlinks(self):
        source = self.disc2 / "Fonts.pkg"
        (source / "Contents/Resources/tool").write_bytes(b"#!/bin/sh\n")
        os.chmod(source / "Contents/Resources/tool", 0o755)
        os.symlink("tool", source / "Contents/Resources/link")
        target = Path(self.tmp.name) / "copy.pkg"
        copy_tree_portable(source, target)
        self.assertEqual(os.stat(target / "Contents/Resources/tool").st_mode & 0o777, 0o755)
        self.assertEqual(os.readlink(target / "Contents/Resources/link"), "tool")


if __name__ == "__main__":
    unittest.main()

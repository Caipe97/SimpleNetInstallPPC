"""Reading UDIF .dmg images without hdiutil.

The fixtures were made on macOS 14 from one 1 MiB raw image (an Apple
partition map with an HFS+ volume "SNIPTest" holding a text file and 20 KB
of random bytes):

    hdiutil convert base.dmg -format UDZO|UDBZ|UDCO -o snip-test-...

All three must decode to that raw image, whose SHA-256 is RAW_SHA256.
Other chunk types and the old resource-fork layout are built by hand below.
"""

from __future__ import annotations

import bz2
import hashlib
import lzma
import plistlib
import random
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from simplenetinstallppc.imaging import udif
from simplenetinstallppc.imaging.partitions import UnsupportedImage, find_volumes
from simplenetinstallppc.imaging.quick_image import copy_volume, ensure_raw

from capture import FIXTURES

RAW_SHA256 = "4223fa100fa4914561bdc980a9fc7967f31bcacbf898ea3d59f3141c0ae01272"
FORMATS = {"udzo": udif.ZLIB, "udbz": udif.BZIP2, "udco": udif.ADC}


def fixture(kind: str) -> Path:
    return FIXTURES / f"snip-test-{kind}.dmg"


def mish(first_sector: int, chunks) -> bytes:
    """A block map: chunks are (kind, sector, sectors, offset, length) tuples."""
    total = sum(c[2] for c in chunks)
    header = struct.pack(">4sIQQQII24x", b"mish", 1, first_sector, total, 0, 0, len(chunks) + 1)
    header += struct.pack(">II128xI", 2, 32, len(chunks) + 1)
    body = b"".join(struct.pack(">IIQQQQ", *c[:1], 0, *c[1:]) for c in chunks)
    body += struct.pack(">IIQQQQ", udif.TERMINATOR, 0, total, 0, 0, 0)
    return header + body


def resource_fork(resources) -> bytes:
    """A classic resource fork holding 'blkx' resources (UDIF before 10.2)."""
    data = b"".join(struct.pack(">I", len(r)) + r for r in resources)
    data_start = 256
    type_list = struct.pack(">H", 0) + struct.pack(">4sHH", b"blkx", len(resources) - 1, 10)
    refs, at = b"", 0
    for i, r in enumerate(resources):
        refs += struct.pack(">HHII", 128 + i, 0xFFFF, at, 0)
        at += 4 + len(r)
    map_body = bytes(24) + struct.pack(">HH", 28, 28 + len(type_list) + len(refs))
    resource_map = map_body + type_list + refs
    map_start = data_start + len(data)
    header = struct.pack(">IIII", data_start, map_start, len(data), len(resource_map))
    return header + bytes(data_start - 16) + data + resource_map


def build_udif(path: Path, pieces, *, xml: bool = True) -> bytes:
    """Write a UDIF file from (kind, raw bytes) pieces; returns the raw image."""
    stored, entries, raw, sector = b"", [], b"", 0
    for kind, content in pieces:
        sectors = len(content) // udif.SECTOR
        encoded = {udif.RAW: lambda b: b, udif.ZLIB: zlib.compress, udif.BZIP2: bz2.compress,
                   udif.LZMA: lzma.compress, udif.LZFSE: lambda b: b"bvx2",
                   udif.ZERO_FILL: lambda b: b"", udif.IGNORED: lambda b: b""}[kind](content)
        entries.append((kind, sector, sectors, len(stored), len(encoded)))
        stored += encoded
        raw += content if kind not in (udif.ZERO_FILL, udif.IGNORED) else bytes(len(content))
        sector += sectors
    block_map = mish(0, entries)
    koly = bytearray(udif.KOLY_SIZE)
    koly[0:4] = b"koly"
    struct.pack_into(">II", koly, 4, 4, udif.KOLY_SIZE)
    struct.pack_into(">QQ", koly, 0x18, 0, len(stored))
    if xml:
        listing = plistlib.dumps({"resource-fork": {"blkx": [{"Name": "whole disk",
                                                             "Data": block_map}]}})
        struct.pack_into(">QQ", koly, 0xD8, len(stored), len(listing))
        tail = listing
    else:
        tail = resource_fork([block_map])
        struct.pack_into(">QQ", koly, 0x28, len(stored), len(tail))
    struct.pack_into(">Q", koly, 0x1EC, sector)
    path.write_bytes(stored + tail + bytes(koly))
    return raw


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_format_decodes_to_the_same_raw_image(self):
        for kind, chunk_type in FORMATS.items():
            with self.subTest(kind):
                self.assertTrue(udif.is_udif(fixture(kind)))
                self.assertIn(chunk_type, udif.chunk_kinds(fixture(kind)))
                raw = udif.convert_to_raw(fixture(kind), self.dir / f"{kind}.img")
                self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), RAW_SHA256)

    def test_volumes_are_found_inside_the_dmg(self):
        raw = udif.convert_to_raw(fixture("udzo"), self.dir / "raw.img")
        expected = find_volumes(raw)
        for kind in FORMATS:
            volumes = find_volumes(fixture(kind))
            self.assertEqual(volumes, expected, kind)
        [volume] = expected
        self.assertEqual((volume.kind, volume.name, volume.scheme), ("hfs+", "disk image", "apm"))

    def test_volume_copied_straight_from_the_dmg(self):
        raw = udif.convert_to_raw(fixture("udco"), self.dir / "raw.img")
        [volume] = find_volumes(fixture("udco"))
        copy_volume(fixture("udco"), volume, self.dir / "root.dmg", lambda _m: None)
        data = raw.read_bytes()
        self.assertEqual((self.dir / "root.dmg").read_bytes(),
                         data[volume.offset:volume.offset + volume.length])

    def test_random_reads(self):
        data = udif.convert_to_raw(fixture("udbz"), self.dir / "raw.img").read_bytes()
        rng = random.Random(3)
        with udif.UdifFile(fixture("udbz")) as image:
            self.assertEqual(image.size, len(data))
            for _ in range(200):
                offset = rng.randrange(len(data))
                length = rng.choice([1, 17, 512, 4096, 70000])
                image.seek(offset)
                self.assertEqual(image.read(length), data[offset:offset + length])
            image.seek(len(data) - 10)
            self.assertEqual(len(image.read()), 10)

    def test_ensure_raw_converts_for_merging(self):
        work = self.dir / "work"
        work.mkdir()
        raw = ensure_raw(fixture("udzo"), work, lambda _m: None)
        self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), RAW_SHA256)
        plain = self.dir / "plain.img"
        plain.write_bytes(b"x" * 1024)
        self.assertEqual(ensure_raw(plain, work, lambda _m: None), plain)


class BuiltImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        rng = random.Random(5)
        self.noise = bytes(rng.getrandbits(8) for _ in range(8 * 512))
        self.text = b"SimpleNetInstallPPC " * 512          # 10240 bytes = 20 sectors

    def tearDown(self):
        self.tmp.cleanup()

    def pieces(self):
        return [(udif.RAW, self.noise), (udif.ZERO_FILL, bytes(4096)),
                (udif.ZLIB, self.text), (udif.IGNORED, bytes(1024)),
                (udif.BZIP2, self.noise), (udif.LZMA, self.text)]

    def test_chunk_types_and_both_block_map_layouts(self):
        for xml in (True, False):
            with self.subTest(xml=xml):
                path = self.dir / f"built-{xml}.dmg"
                raw = build_udif(path, self.pieces(), xml=xml)
                out = udif.convert_to_raw(path, self.dir / f"built-{xml}.img")
                self.assertEqual(out.read_bytes(), raw)
                with udif.UdifFile(path) as image:
                    image.seek(8 * 512 + 100)            # inside the zero run
                    self.assertEqual(image.read(50), bytes(50))

    def test_lzfse_explains_what_to_do(self):
        path = self.dir / "new.dmg"
        build_udif(path, [(udif.LZFSE, self.text)])
        with self.assertRaises(udif.UdifError) as caught:
            udif.convert_to_raw(path, self.dir / "new.img")
        self.assertIn("hdiutil convert", str(caught.exception))

    def test_truncated_and_broken_images(self):
        path = self.dir / "cut.dmg"
        build_udif(path, self.pieces())
        data = path.read_bytes()
        cut = self.dir / "cut2.dmg"
        cut.write_bytes(data[:2000] + data[-512:])       # data gone, trailer kept
        with self.assertRaises(udif.UdifError):
            udif.convert_to_raw(cut, self.dir / "cut.img")
        bad = self.dir / "bad.dmg"
        bad.write_bytes(data[:-512] + b"kol!" + data[-508:])
        self.assertFalse(udif.is_udif(bad))
        broken = self.dir / "broken.dmg"
        broken.write_bytes(b"\0" * 1000 + data[-512:])   # trailer points at garbage
        with self.assertRaises(UnsupportedImage):
            find_volumes(broken)


class AdcTests(unittest.TestCase):
    def test_literals_and_back_references(self):
        # "abc" as a literal run, then a 2-byte copy (length 6, distance 3),
        # which overlaps what it writes: abcabcabc.
        data = bytes([0x82]) + b"abc" + bytes([((6 - 3) << 2), 2])
        self.assertEqual(udif.adc_decompress(data, 9), b"abcabcabc")
        # A 3-byte copy: length 4 from distance 1 back ("bc").
        data = bytes([0x82]) + b"abc" + bytes([0x40, 0x00, 0x01])
        self.assertEqual(udif.adc_decompress(data, 7), b"abcbcbc")

    def test_reference_before_the_start(self):
        with self.assertRaises(udif.UdifError):
            udif.adc_decompress(bytes([0x04, 0x05]), 10)


if __name__ == "__main__":
    unittest.main()

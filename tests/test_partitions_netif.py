import json
import struct
import tempfile
import unittest
import uuid
from pathlib import Path

from simplenetinstallppc.imaging.partitions import (HFS_PLUS_GPT_TYPE, UnsupportedImage,
                                               find_volumes, pick_volume)
from simplenetinstallppc.netif import parse_ifconfig, parse_ip_json

MiB = 1024 * 1024


def hfs_plus_header(block_size=4096, total_blocks=256, journaled=False, sig=b"H+"):
    header = bytearray(512)
    header[0:2] = sig
    struct.pack_into(">H", header, 2, 4)
    struct.pack_into(">I", header, 4, (1 << 13) if journaled else 0)
    struct.pack_into(">II", header, 40, block_size, total_blocks)
    return bytes(header)


def write_at(image: bytearray, offset: int, data: bytes) -> None:
    image[offset:offset + len(data)] = data


class PartitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, data: bytes) -> Path:
        path = Path(self.tmp.name) / "image.img"
        path.write_bytes(data)
        return path

    def test_bare_volume(self):
        image = bytearray(2 * MiB)
        write_at(image, 1024, hfs_plus_header(journaled=True))
        [volume] = find_volumes(self.save(image))
        self.assertEqual((volume.offset, volume.kind, volume.scheme), (0, "hfs+", "none"))
        self.assertEqual(volume.length, 4096 * 256)
        self.assertTrue(volume.journaled)

    def test_apple_partition_map(self):
        image = bytearray(4 * MiB)
        write_at(image, 0, b"ER" + struct.pack(">H", 512))

        def entry(start, blocks, name, ptype):
            e = bytearray(512)
            e[0:2] = b"PM"
            struct.pack_into(">III", e, 4, 2, start, blocks)
            e[16:16 + len(name)] = name
            e[48:48 + len(ptype)] = ptype
            return bytes(e)
        write_at(image, 512, entry(1, 63, b"Apple", b"Apple_partition_map"))
        write_at(image, 1024, entry(64, 4096, b"Mac OS X Install Disc 1", b"Apple_HFS"))
        write_at(image, 64 * 512 + 1024, hfs_plus_header(block_size=512, total_blocks=4096))
        [volume] = find_volumes(self.save(image))
        self.assertEqual((volume.scheme, volume.offset, volume.name),
                         ("apm", 64 * 512, "Mac OS X Install Disc 1"))
        self.assertEqual(volume.length, 4096 * 512)

    def test_gpt(self):
        image = bytearray(4 * MiB)
        header = bytearray(92)
        header[0:8] = b"EFI PART"
        struct.pack_into("<Q", header, 72, 2)
        struct.pack_into("<II", header, 80, 128, 128)
        write_at(image, 512, bytes(header))
        entry = bytearray(128)
        entry[0:16] = HFS_PLUS_GPT_TYPE.bytes_le
        entry[16:32] = uuid.uuid4().bytes_le
        struct.pack_into("<QQ", entry, 32, 40, 40 + 2047)
        name = "Macintosh HD".encode("utf-16-le")
        entry[56:56 + len(name)] = name
        write_at(image, 1024, bytes(entry))
        write_at(image, 40 * 512 + 1024, hfs_plus_header())
        [volume] = find_volumes(self.save(image))
        self.assertEqual((volume.scheme, volume.offset, volume.name), ("gpt", 40 * 512, "Macintosh HD"))
        self.assertEqual(volume.length, 2048 * 512)

    def test_hfs_wrapper_is_unwrapped(self):
        image = bytearray(4 * MiB)
        mdb = bytearray(512)
        mdb[0:2] = b"BD"
        struct.pack_into(">H", mdb, 0x12, 1000)        # allocation blocks
        struct.pack_into(">I", mdb, 0x14, 2048)        # allocation block size
        struct.pack_into(">H", mdb, 0x1C, 16)          # first allocation block (sectors)
        mdb[0x24] = 7
        mdb[0x25:0x2C] = b"Mac OS9"
        mdb[0x7C:0x7E] = b"H+"
        struct.pack_into(">HH", mdb, 0x7E, 4, 900)     # embedded extent
        write_at(image, 1024, bytes(mdb))
        inner = 16 * 512 + 4 * 2048
        write_at(image, inner + 1024, hfs_plus_header(block_size=2048, total_blocks=900))
        [volume] = find_volumes(self.save(image))
        self.assertEqual((volume.kind, volume.offset, volume.length), ("hfs+", inner, 900 * 2048))
        self.assertEqual(volume.name, "Mac OS9")

    def test_udif_is_rejected_with_advice(self):
        image = bytearray(4096)
        write_at(image, 4096 - 512, b"koly")
        with self.assertRaises(UnsupportedImage) as ctx:
            find_volumes(self.save(image))
        self.assertIn("dmg2img", str(ctx.exception))

    def test_pick_volume(self):
        with self.assertRaises(UnsupportedImage):
            pick_volume([])


IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether a4:83:e7:00:11:22
\tinet6 fe80::1%en0 prefixlen 64 scopeid 0x6
\tinet 192.168.2.10 netmask 0xffffff00 broadcast 192.168.2.255
en7: flags=8822<BROADCAST,SMART,SIMPLEX,MULTICAST> mtu 1500
\tether 00:0a:95:00:00:01
"""

IP_JSON = json.dumps([
    {"ifname": "lo", "flags": ["LOOPBACK", "UP"], "link_type": "loopback",
     "address": "00:00:00:00:00:00",
     "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
    {"ifname": "eth1", "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"],
     "link_type": "ether", "address": "52:54:00:12:34:56",
     "addr_info": [{"family": "inet", "local": "10.0.0.1", "prefixlen": 24},
                   {"family": "inet6", "local": "fe80::1", "prefixlen": 64}]},
])


class NetifTests(unittest.TestCase):
    def test_ifconfig(self):
        interfaces = {i.name: i for i in parse_ifconfig(IFCONFIG)}
        self.assertEqual(str(interfaces["en0"].primary), "192.168.2.10/24")
        self.assertEqual(interfaces["en0"].mac, "a4:83:e7:00:11:22")
        self.assertTrue(interfaces["en0"].up)
        self.assertFalse(interfaces["en7"].up)
        self.assertIsNone(interfaces["en7"].primary)

    def test_ip_json(self):
        interfaces = {i.name: i for i in parse_ip_json(IP_JSON)}
        self.assertEqual(str(interfaces["eth1"].primary), "10.0.0.1/24")
        self.assertEqual(interfaces["eth1"].mac, "52:54:00:12:34:56")
        self.assertIsNone(interfaces["lo"].mac)


if __name__ == "__main__":
    unittest.main()

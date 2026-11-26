import ipaddress
import struct
import unittest

from simplenetinstallppc.net import bsdp
from simplenetinstallppc.net.dhcp_packet import (BOOTREPLY, MAGIC_COOKIE, MIN_PACKET_SIZE,
                                            DhcpPacket, MessageType, Opt)


class DhcpPacketTests(unittest.TestCase):
    def test_round_trip(self):
        packet = DhcpPacket(op=BOOTREPLY, xid=0xDEADBEEF, flags=0x8000,
                            yiaddr=ipaddress.IPv4Address("10.0.0.9"),
                            siaddr=ipaddress.IPv4Address("10.0.0.1"),
                            chaddr=bytes.fromhex("0011223344556677"), file=b"img1/ppc/booter")
        packet.options[Opt.MESSAGE_TYPE] = bytes((MessageType.ACK,))
        packet.options[Opt.ROOT_PATH] = b"http://10.0.0.1/img1/root.dmg"
        data = packet.encode()
        self.assertGreaterEqual(len(data), MIN_PACKET_SIZE)
        decoded = DhcpPacket.decode(data)
        self.assertEqual(decoded.xid, 0xDEADBEEF)
        self.assertEqual(decoded.mac, "00:11:22:33:44:55")
        self.assertEqual(decoded.file, b"img1/ppc/booter")
        self.assertEqual(decoded.message_type, MessageType.ACK)
        self.assertEqual(decoded.options[Opt.ROOT_PATH], b"http://10.0.0.1/img1/root.dmg")
        self.assertEqual(str(decoded.yiaddr), "10.0.0.9")

    def test_long_option_is_split_and_reassembled(self):
        packet = DhcpPacket()
        packet.options[Opt.VENDOR_SPECIFIC] = bytes(range(256)) * 2   # 512 bytes
        data = packet.encode()
        body = data[236 + 4:]
        self.assertEqual(body[0], Opt.VENDOR_SPECIFIC)
        self.assertEqual(body[1], 255)
        self.assertEqual(DhcpPacket.decode(data).options[Opt.VENDOR_SPECIFIC],
                         bytes(range(256)) * 2)

    def test_overloaded_file_field(self):
        packet = DhcpPacket()
        packet.options[Opt.OVERLOAD] = b"\x01"
        packet.file = bytes((Opt.HOSTNAME, 3)) + b"mac" + bytes((Opt.END,))
        data = packet.encode()
        decoded = DhcpPacket.decode(data)
        self.assertEqual(decoded.options[Opt.HOSTNAME], b"mac")
        self.assertEqual(decoded.file, b"")

    def test_rejects_short_packet(self):
        with self.assertRaises(ValueError):
            DhcpPacket.decode(b"\x01" * 100)

    def test_file_field_limit(self):
        packet = DhcpPacket(file=b"x" * 128)
        with self.assertRaises(ValueError):
            packet.encode()

    def test_cookie_present(self):
        self.assertEqual(DhcpPacket().encode()[236:240], MAGIC_COOKIE)


class BsdpTests(unittest.TestCase):
    def test_image_id(self):
        image_id = bsdp.make_image_id(7, bsdp.ImageKind.MACOSX, install=True)
        self.assertEqual(image_id, 0x81000007)
        self.assertEqual(bsdp.split_image_id(image_id), (7, 1, True))
        self.assertEqual(bsdp.make_image_id(3, bsdp.ImageKind.MACOS9), 0x00000003)
        with self.assertRaises(ValueError):
            bsdp.make_image_id(0)

    def test_client_class(self):
        cc = bsdp.ClientClass.parse(b"AAPLBSDPC/ppc/PowerMac3,1")
        self.assertEqual((cc.arch, cc.model), ("ppc", "PowerMac3,1"))
        cc = bsdp.ClientClass.parse(b"AAPLBSDPC")
        self.assertEqual((cc.arch, cc.model), (None, None))
        self.assertIsNone(bsdp.ClientClass.parse(b"MSFT 5.0"))
        self.assertIsNone(bsdp.ClientClass.parse(None))

    def test_options_round_trip(self):
        data = bsdp.encode_options([(1, b"\x01"), (7, bsdp.u32(0x01000002))])
        self.assertEqual(bsdp.decode_options(data + b"\xff"), {1: b"\x01", 7: b"\x01\x00\x00\x02"})
        with self.assertRaises(ValueError):
            bsdp.decode_options(b"\x09\x05ab")

    def test_image_list_budget(self):
        images = [(bsdp.make_image_id(i), f"Image number {i}") for i in range(1, 30)]
        payload, count = bsdp.encode_image_list(images, 100)
        self.assertLessEqual(len(payload), 100)
        self.assertEqual(count, 100 // (5 + len("Image number 1")))
        decoded = bsdp.decode_image_list(payload)
        self.assertEqual(decoded[0], images[0])
        self.assertEqual(len(decoded), count)

    def test_image_name_is_mac_roman(self):
        payload, _ = bsdp.encode_image_list([(1, "Café")], 255)
        self.assertEqual(payload[5:], "Café".encode("mac_roman"))
        self.assertEqual(struct.unpack_from("!I", payload)[0], 1)


if __name__ == "__main__":
    unittest.main()

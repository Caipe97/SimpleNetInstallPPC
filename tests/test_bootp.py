"""Plain BOOTP, as sent by the Mac OS X 10.0 kernel's nfs_boot (xnu-123.5)."""

import ipaddress
import tempfile
import unittest
from pathlib import Path

from simplenetinstallppc.catalog import Catalog
from simplenetinstallppc.net.dhcp_packet import (BOOTREPLY, BOOTREQUEST, MAGIC_COOKIE, DhcpPacket,
                                            MessageType, Opt)
from simplenetinstallppc.net.dhcp_server import DhcpBsdpServer, DhcpConfig
from simplenetinstallppc.net.leases import LeasePool

from simplenetinstallppc.net import bsdp
from simplenetinstallppc.net.bsdp import BsdpMessageType, BsdpTag

from helpers import CLIENT_MAC, SERVER_IP, RecordingBus, bsdp_inform, make_image

IP = ipaddress.IPv4Address
SUBNET = ipaddress.IPv4Network("10.0.0.0/24")


def kernel_bootp_request() -> bytes:
    """Same layout as make_bootp_request(): cookie then END, no options."""
    packet = DhcpPacket(op=BOOTREQUEST, xid=0x10203040, chaddr=CLIENT_MAC)
    data = bytearray(packet.encode())
    assert data[236:240] == MAGIC_COOKIE
    return bytes(data)


class BootpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.catalog = Catalog(Path(self.tmp.name))
        self.catalog.reload()
        self.events = RecordingBus()

    def tearDown(self):
        self.tmp.cleanup()

    def server(self, mode):
        pool = LeasePool(IP("10.0.0.100"), IP("10.0.0.110"), excluded=[SERVER_IP]) if mode == "full" else None
        config = DhcpConfig(server_ip=SERVER_IP, subnet=SUBNET, mode=mode, clients="all",
                            pool=pool, router=IP("10.0.0.254"))
        return DhcpBsdpServer(config, self.catalog, self.events)

    def test_full_mode_answers_plain_bootp(self):
        server = self.server("full")
        [(reply, destination)] = server.handle(DhcpPacket.decode(kernel_bootp_request()))
        reply = DhcpPacket.decode(reply.encode())
        self.assertEqual(reply.op, BOOTREPLY)
        self.assertEqual(reply.xid, 0x10203040)
        self.assertEqual(reply.yiaddr, IP("10.0.0.100"))
        self.assertEqual(reply.chaddr[:6], CLIENT_MAC)
        self.assertEqual(reply.options[Opt.SUBNET_MASK], IP("255.255.255.0").packed)
        self.assertEqual(reply.options[Opt.ROUTER], IP("10.0.0.254").packed)
        self.assertNotIn(Opt.MESSAGE_TYPE, reply.options)
        self.assertEqual(destination, ("255.255.255.255", 68))

    def test_bootp_keeps_the_address_from_firmware_dhcp(self):
        server = self.server("full")
        discover = DhcpPacket(op=BOOTREQUEST, chaddr=CLIENT_MAC)
        discover.options[Opt.MESSAGE_TYPE] = bytes((MessageType.DISCOVER,))
        [(offer, _)] = server.handle(discover)
        request = DhcpPacket(op=BOOTREQUEST, chaddr=CLIENT_MAC)
        request.options[Opt.MESSAGE_TYPE] = bytes((MessageType.REQUEST,))
        request.options[Opt.REQUESTED_IP] = offer.yiaddr.packed
        server.handle(request)
        [(reply, _)] = server.handle(DhcpPacket.decode(kernel_bootp_request()))
        self.assertEqual(reply.yiaddr, offer.yiaddr)

    def test_bsdp_only_mode_explains_why_bootp_is_ignored(self):
        server = self.server("bsdp-only")
        self.assertEqual(server.handle(DhcpPacket.decode(kernel_bootp_request())), [])
        self.assertIn(("dhcp", "bootp-ignored"), self.events.kinds())


class FirmwareBootpTests(unittest.TestCase):
    """Tray-loading iMac G3: BSDP LIST/SELECT, then boots from a plain BOOTP reply."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        make_image(root, 1, "Tiger", ("ppc",))
        make_image(root, 2, "Cheetah", ("ppc",), protocol="NFS")
        self.catalog = Catalog(root)
        self.catalog.reload()
        pool = LeasePool(IP("10.0.0.100"), IP("10.0.0.110"), excluded=[SERVER_IP])
        config = DhcpConfig(server_ip=SERVER_IP, subnet=SUBNET, mode="full", clients="all",
                            pool=pool,
                            http_base="http://10.0.0.1", nfs_export="/srv/images")
        self.server = DhcpBsdpServer(config, self.catalog, RecordingBus())

    def tearDown(self):
        self.tmp.cleanup()

    def test_bootp_after_select_names_the_selected_booter(self):
        select = bsdp_inform(BsdpMessageType.SELECT, model="iMac,1", extra=[
            (BsdpTag.SELECTED_BOOT_IMAGE, bsdp.u32(self.catalog.by_index(2).image_id))])
        self.server.handle(DhcpPacket.decode(select.encode()))
        [(reply, _)] = self.server.handle(DhcpPacket.decode(kernel_bootp_request()))
        reply = DhcpPacket.decode(reply.encode())
        self.assertEqual(reply.file, b"img2/ppc/booter")
        self.assertEqual(reply.siaddr, SERVER_IP)
        self.assertEqual(reply.options[Opt.ROOT_PATH], b"nfs:10.0.0.1:/srv/images:img2/root.dmg")

    def test_bootp_without_select_uses_default_image(self):
        self.catalog.default_index["ppc"] = 1
        [(reply, _)] = self.server.handle(DhcpPacket.decode(kernel_bootp_request()))
        self.assertEqual(reply.file, b"img1/ppc/booter")


if __name__ == "__main__":
    unittest.main()

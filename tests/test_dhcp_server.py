import ipaddress
import tempfile
import unittest
from pathlib import Path

from netboot_server.catalog import Catalog
from netboot_server.net import bsdp
from netboot_server.net.bsdp import BsdpMessageType, BsdpTag
from netboot_server.net.dhcp_packet import BOOTREQUEST, DhcpPacket, MessageType, Opt
from netboot_server.net.dhcp_server import DhcpBsdpServer, DhcpConfig
from netboot_server.net.leases import LeasePool

from helpers import CLIENT_MAC, SERVER_IP, RecordingBus, bsdp_inform, make_image

IP = ipaddress.IPv4Address
SUBNET = ipaddress.IPv4Network("10.0.0.0/24")


class BsdpServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_image(self.root, 1, "Tiger Install", ("ppc", "i386"), install=True)
        make_image(self.root, 2, "Jaguar NetBoot", ("ppc",), protocol="NFS", IsDefault=True)
        make_image(self.root, 3, "Leopard Intel", ("i386",))
        self.catalog = Catalog(self.root)
        self.catalog.reload()
        self.events = RecordingBus()
        self.config = DhcpConfig(server_ip=SERVER_IP, subnet=SUBNET,
                                 http_base="http://10.0.0.1", nfs_export="/srv/netboot/images")
        self.server = DhcpBsdpServer(self.config, self.catalog, self.events)

    def tearDown(self):
        self.tmp.cleanup()

    def bsdp_reply(self, packet):
        replies = self.server.handle(DhcpPacket.decode(packet.encode()))
        self.assertEqual(len(replies), 1)
        reply, destination = replies[0]
        reply = DhcpPacket.decode(reply.encode())
        return reply, destination, bsdp.decode_options(reply.options[Opt.VENDOR_SPECIFIC])

    def test_list_filters_by_arch_and_reports_default(self):
        reply, destination, options = self.bsdp_reply(bsdp_inform(BsdpMessageType.LIST))
        self.assertEqual(destination, ("10.0.0.50", 68))
        self.assertEqual(reply.message_type, MessageType.ACK)
        self.assertEqual(reply.options[Opt.VENDOR_CLASS], b"AAPLBSDPC")
        self.assertEqual(options[BsdpTag.MESSAGE_TYPE], bytes((BsdpMessageType.LIST,)))
        names = [name for _id, name in bsdp.decode_image_list(options[BsdpTag.BOOT_IMAGE_LIST])]
        self.assertEqual(names, ["Tiger Install", "Jaguar NetBoot"])
        default = int.from_bytes(options[BsdpTag.DEFAULT_BOOT_IMAGE], "big")
        self.assertEqual(default, self.catalog.by_index(2).image_id)
        self.assertEqual(options[BsdpTag.SERVER_IDENTIFIER], SERVER_IP.packed)

    def test_operator_default_overrides_plist(self):
        self.catalog.default_index["ppc"] = 1
        _, _, options = self.bsdp_reply(bsdp_inform(BsdpMessageType.LIST))
        self.assertEqual(int.from_bytes(options[BsdpTag.DEFAULT_BOOT_IMAGE], "big"),
                         self.catalog.by_index(1).image_id)

    def test_select_http_image(self):
        image = self.catalog.by_index(1)
        packet = bsdp_inform(BsdpMessageType.SELECT, arch="i386", model="iMac4,1", extra=[
            (BsdpTag.SERVER_IDENTIFIER, SERVER_IP.packed),
            (BsdpTag.SELECTED_BOOT_IMAGE, bsdp.u32(image.image_id))])
        reply, _, options = self.bsdp_reply(packet)
        self.assertEqual(options[BsdpTag.MESSAGE_TYPE], bytes((BsdpMessageType.SELECT,)))
        self.assertEqual(reply.file, b"img1/i386/booter")
        self.assertEqual(reply.siaddr, SERVER_IP)
        self.assertEqual(reply.options[Opt.ROOT_PATH], b"http://10.0.0.1/img1/root.dmg")
        self.assertEqual(self.server.clients["00:0a:95:aa:bb:cc"].selected_image, 1)

    def test_select_nfs_image_root_path_format(self):
        image = self.catalog.by_index(2)
        packet = bsdp_inform(BsdpMessageType.SELECT, extra=[
            (BsdpTag.SELECTED_BOOT_IMAGE, bsdp.u32(image.image_id))])
        reply, _, _ = self.bsdp_reply(packet)
        self.assertEqual(reply.options[Opt.ROOT_PATH],
                         b"nfs:10.0.0.1:/srv/netboot/images:img2/root.dmg")
        self.assertEqual(reply.file, b"img2/ppc/booter")

    def test_select_wrong_arch_fails(self):
        image = self.catalog.by_index(3)   # Intel only
        packet = bsdp_inform(BsdpMessageType.SELECT, arch="ppc", extra=[
            (BsdpTag.SELECTED_BOOT_IMAGE, bsdp.u32(image.image_id))])
        _, _, options = self.bsdp_reply(packet)
        self.assertEqual(options[BsdpTag.MESSAGE_TYPE], bytes((BsdpMessageType.FAILED,)))

    def test_select_nfs_without_export_fails(self):
        self.config.nfs_export = None
        image = self.catalog.by_index(2)
        packet = bsdp_inform(BsdpMessageType.SELECT, extra=[
            (BsdpTag.SELECTED_BOOT_IMAGE, bsdp.u32(image.image_id))])
        _, _, options = self.bsdp_reply(packet)
        self.assertEqual(options[BsdpTag.MESSAGE_TYPE], bytes((BsdpMessageType.FAILED,)))

    def test_folder_root_needs_no_nfs_export(self):
        # 10.0/10.1 images boot from a folder found through bootparamd.
        self.config.nfs_export = None
        image = self.catalog.by_index(2)
        image.root_directory = "/var/lib/netboot-server/roots/img2"
        packet = bsdp_inform(BsdpMessageType.SELECT, extra=[
            (BsdpTag.SELECTED_BOOT_IMAGE, bsdp.u32(image.image_id))])
        reply, _, options = self.bsdp_reply(packet)
        self.assertEqual(options[BsdpTag.MESSAGE_TYPE], bytes((BsdpMessageType.SELECT,)))
        self.assertEqual(reply.options[Opt.ROOT_PATH],
                         b"nfs:10.0.0.1:/var/lib/netboot-server/roots:img2")

    def test_select_for_other_server_is_ignored(self):
        packet = bsdp_inform(BsdpMessageType.SELECT, extra=[
            (BsdpTag.SERVER_IDENTIFIER, IP("10.0.0.2").packed),
            (BsdpTag.SELECTED_BOOT_IMAGE, bsdp.u32(self.catalog.by_index(1).image_id))])
        self.assertEqual(self.server.handle(packet), [])

    def test_reply_port_and_broadcast(self):
        packet = bsdp_inform(BsdpMessageType.LIST, ciaddr="0.0.0.0",
                             extra=[(BsdpTag.REPLY_PORT, bsdp.u16(1001))])
        _, destination, _ = self.bsdp_reply(packet)
        self.assertEqual(destination, ("255.255.255.255", 1001))

    def test_no_images_for_arch_means_no_reply(self):
        for image in self.catalog.images:
            image.enabled = False
        self.assertEqual(self.server.handle(bsdp_inform(BsdpMessageType.LIST)), [])
        self.assertIn(("bsdp", "list-empty"), self.events.kinds())

    def test_long_image_list_is_truncated_to_one_option(self):
        for i in range(10, 40):
            make_image(self.root, i, f"A rather long image name number {i}")
        self.catalog.reload()
        reply, _, options = self.bsdp_reply(bsdp_inform(BsdpMessageType.LIST))
        self.assertLessEqual(len(reply.options[Opt.VENDOR_SPECIFIC]), 255)
        self.assertIn(("bsdp", "list-truncated"), self.events.kinds())

    def test_bsdp_only_ignores_dhcp_discover(self):
        packet = DhcpPacket(op=BOOTREQUEST, chaddr=CLIENT_MAC)
        packet.options[Opt.MESSAGE_TYPE] = bytes((MessageType.DISCOVER,))
        packet.options[Opt.VENDOR_CLASS] = b"AAPLBSDPC/ppc/PowerMac3,4"
        self.assertEqual(self.server.handle(packet), [])


class FullDhcpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_image(self.root, 1, "Panther", ("ppc",))
        catalog = Catalog(self.root)
        catalog.reload()
        pool = LeasePool(IP("10.0.0.100"), IP("10.0.0.101"), 600, excluded=[SERVER_IP])
        self.config = DhcpConfig(server_ip=SERVER_IP, subnet=SUBNET, mode="full",
                                 clients="all", pool=pool,
                                 router=IP("10.0.0.254"), dns=[IP("10.0.0.53")],
                                 http_base="http://10.0.0.1")
        self.server = DhcpBsdpServer(self.config, catalog, RecordingBus())

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, message_type, opts=None, mac=CLIENT_MAC, vendor=None):
        packet = DhcpPacket(op=BOOTREQUEST, xid=1, chaddr=mac)
        packet.options[Opt.MESSAGE_TYPE] = bytes((message_type,))
        if vendor:
            packet.options[Opt.VENDOR_CLASS] = vendor
        packet.options.update(opts or {})
        return [(DhcpPacket.decode(r.encode()), d) for r, d in self.server.handle(packet)]

    def test_discover_request_ack(self):
        [(offer, destination)] = self.request(MessageType.DISCOVER)
        self.assertEqual(offer.message_type, MessageType.OFFER)
        self.assertEqual(destination, ("255.255.255.255", 68))
        self.assertEqual(offer.yiaddr, IP("10.0.0.100"))
        self.assertEqual(offer.options[Opt.ROUTER], IP("10.0.0.254").packed)
        self.assertEqual(offer.options[Opt.SUBNET_MASK], IP("255.255.255.0").packed)
        [(ack, _)] = self.request(MessageType.REQUEST, {
            Opt.REQUESTED_IP: offer.yiaddr.packed, Opt.SERVER_ID: SERVER_IP.packed})
        self.assertEqual(ack.message_type, MessageType.ACK)
        self.assertEqual(ack.yiaddr, IP("10.0.0.100"))
        self.assertEqual(int.from_bytes(ack.options[Opt.LEASE_TIME], "big"), 600)

    def test_request_for_other_server_is_ignored(self):
        self.request(MessageType.DISCOVER)
        self.assertEqual(self.request(MessageType.REQUEST, {
            Opt.REQUESTED_IP: IP("10.0.0.100").packed, Opt.SERVER_ID: IP("10.0.0.2").packed}), [])

    def test_request_wrong_address_is_naked(self):
        [(nak, _)] = self.request(MessageType.REQUEST, {Opt.REQUESTED_IP: IP("10.0.0.7").packed})
        self.assertEqual(nak.message_type, MessageType.NAK)

    def test_pool_exhaustion(self):
        self.request(MessageType.DISCOVER, mac=bytes.fromhex("000000000001"))
        self.request(MessageType.DISCOVER, mac=bytes.fromhex("000000000002"))
        self.assertEqual(self.request(MessageType.DISCOVER, mac=bytes.fromhex("000000000003")), [])

    def test_netboot_1_0_firmware_gets_boot_file_in_offer(self):
        vendor_options = bsdp.encode_options([(BsdpTag.NETBOOT_1_0_FIRMWARE, b"")])
        [(offer, _)] = self.request(MessageType.DISCOVER, {Opt.VENDOR_SPECIFIC: vendor_options},
                                    vendor=b"AAPLBSDPC/ppc/iMac,1")
        self.assertEqual(offer.file, b"img1/ppc/booter")
        self.assertEqual(offer.siaddr, SERVER_IP)
        self.assertEqual(offer.options[Opt.ROOT_PATH], b"http://10.0.0.1/img1/root.dmg")

    def test_modern_firmware_offer_has_no_boot_file(self):
        [(offer, _)] = self.request(MessageType.DISCOVER, vendor=b"AAPLBSDPC/ppc/PowerMac3,4")
        self.assertEqual(offer.file, b"")


class SharedLanTests(unittest.TestCase):
    """Full mode next to a household DHCP server: answer NetBoot Macs only."""
    APPLE = b"AAPLBSDPC/ppc/PowerMac2,2"
    PHONE = bytes.fromhex("96a0290000f6")
    tearDown = FullDhcpTests.tearDown
    request = FullDhcpTests.request

    def setUp(self):
        FullDhcpTests.setUp(self)
        self.config.clients = "netboot"

    def test_other_devices_get_no_offer(self):
        self.assertEqual(self.request(MessageType.DISCOVER, mac=self.PHONE), [])

    def test_other_devices_rejoining_are_not_naked(self):
        # INIT-REBOOT: a phone asks to keep the address the home router gave it.
        self.assertEqual(self.request(MessageType.REQUEST, {
            Opt.REQUESTED_IP: IP("10.0.0.14").packed}, mac=self.PHONE), [])
        self.assertEqual(self.request(MessageType.INFORM, mac=self.PHONE), [])

    def test_netboot_mac_is_served(self):
        [(offer, _)] = self.request(MessageType.DISCOVER, vendor=self.APPLE)
        self.assertEqual(offer.message_type, MessageType.OFFER)

    def test_known_mac_is_served_without_vendor_class(self):
        # The 10.0 kernel's plain BOOTP and the booted OS's DHCP carry no
        # BSDP vendor class; the firmware's DISCOVER did.
        from test_bootp import kernel_bootp_request
        self.request(MessageType.DISCOVER, vendor=self.APPLE)
        [(reply, _)] = self.server.handle(DhcpPacket.decode(kernel_bootp_request()))
        self.assertEqual(reply.yiaddr, IP("10.0.0.100"))

    def test_unknown_plain_bootp_is_ignored(self):
        from test_bootp import kernel_bootp_request
        self.assertEqual(self.server.handle(DhcpPacket.decode(kernel_bootp_request())), [])

    def test_mac_asking_for_routers_address_is_left_alone(self):
        self.request(MessageType.DISCOVER, vendor=self.APPLE)
        self.assertEqual(self.request(MessageType.REQUEST, {
            Opt.REQUESTED_IP: IP("10.0.0.14").packed}), [])

    def test_mac_asking_for_a_pool_address_it_lacks_is_naked(self):
        self.request(MessageType.DISCOVER, vendor=self.APPLE, mac=bytes.fromhex("000000000001"))
        [(nak, _)] = self.request(MessageType.REQUEST, {
            Opt.REQUESTED_IP: IP("10.0.0.100").packed}, vendor=self.APPLE)
        self.assertEqual(nak.message_type, MessageType.NAK)


class LeasePoolTests(unittest.TestCase):
    def test_reservation_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "leases.json"
            pool = LeasePool(IP("10.0.0.100"), IP("10.0.0.110"),
                             reservations={"AA:00:00:00:00:01": IP("10.0.0.50")}, state_file=state)
            self.assertEqual(pool.offer("aa:00:00:00:00:01"), IP("10.0.0.50"))
            self.assertTrue(pool.confirm("aa:00:00:00:00:01", IP("10.0.0.50")))
            ip = pool.offer("bb:00:00:00:00:02", requested=IP("10.0.0.105"))
            self.assertEqual(ip, IP("10.0.0.105"))
            self.assertTrue(pool.confirm("bb:00:00:00:00:02", ip))
            self.assertFalse(pool.confirm("cc:00:00:00:00:03", ip))   # taken
            reloaded = LeasePool(IP("10.0.0.100"), IP("10.0.0.110"), state_file=state)
            self.assertEqual(reloaded.lookup("bb:00:00:00:00:02").ip, IP("10.0.0.105"))

    def test_offer_expires(self):
        now = [1000.0]
        pool = LeasePool(IP("10.0.0.100"), IP("10.0.0.100"), clock=lambda: now[0])
        self.assertEqual(pool.offer("aa"), IP("10.0.0.100"))
        self.assertIsNone(pool.offer("bb"))
        now[0] += 61
        self.assertEqual(pool.offer("bb"), IP("10.0.0.100"))

    def test_decline_quarantines(self):
        pool = LeasePool(IP("10.0.0.100"), IP("10.0.0.101"))
        pool.decline("aa", IP("10.0.0.100"))
        self.assertEqual(pool.offer("aa"), IP("10.0.0.101"))


if __name__ == "__main__":
    unittest.main()

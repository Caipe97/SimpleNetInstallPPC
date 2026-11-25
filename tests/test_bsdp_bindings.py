"""Startup Disk: a SELECT from the running OS is remembered and named in later LIST replies."""

import ipaddress
import json
import tempfile
import unittest
from pathlib import Path

from netboot_server.catalog import Catalog
from netboot_server.net import bsdp
from netboot_server.net.bsdp import BsdpMessageType, BsdpTag
from netboot_server.net.dhcp_packet import DhcpPacket, Opt
from netboot_server.net.dhcp_server import DhcpBsdpServer, DhcpConfig

from helpers import SERVER_IP, RecordingBus, bsdp_inform, make_image

IP = ipaddress.IPv4Address
MAC = "00:0a:95:aa:bb:cc"


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "images"
        make_image(self.root, 4, "Jaguar Install", protocol="NFS", IsDefault=True)
        make_image(self.root, 5, "Panther")
        make_image(self.root, 6, "Leopard Intel", ("i386",))
        self.catalog = Catalog(self.root)
        self.catalog.reload()
        self.bindings = Path(self.tmp.name) / "bsdp-bindings.json"
        self.server = self.make_server()

    def tearDown(self):
        self.tmp.cleanup()

    def make_server(self):
        config = DhcpConfig(server_ip=SERVER_IP, subnet=ipaddress.IPv4Network("10.0.0.0/24"),
                            http_base="http://10.0.0.1", nfs_export="/srv/images")
        return DhcpBsdpServer(config, self.catalog, RecordingBus(), self.bindings)

    def options(self, server, packet):
        [(reply, _)] = server.handle(DhcpPacket.decode(packet.encode()))
        return bsdp.decode_options(DhcpPacket.decode(reply.encode()).options[Opt.VENDOR_SPECIFIC])

    def select(self, server, index, server_ip=SERVER_IP):
        return server.handle(bsdp_inform(BsdpMessageType.SELECT, extra=[
            (BsdpTag.SERVER_IDENTIFIER, server_ip.packed),
            (BsdpTag.SELECTED_BOOT_IMAGE, bsdp.u32(self.catalog.by_index(index).image_id))]))

    def list_options(self, server=None):
        return self.options(server or self.server, bsdp_inform(BsdpMessageType.LIST))

    def test_list_has_no_selected_image_before_any_select(self):
        options = self.list_options()
        self.assertNotIn(BsdpTag.SELECTED_BOOT_IMAGE, options)
        self.assertEqual(options[BsdpTag.DEFAULT_BOOT_IMAGE],
                         bsdp.u32(self.catalog.by_index(4).image_id))

    def test_startup_disk_flow_names_the_chosen_image(self):
        # Startup Disk (running OS) selects Panther; after the restart the
        # firmware's LIST reply must name Panther as selected, with Jaguar
        # still the default.
        self.select(self.server, 5)
        options = self.list_options()
        self.assertEqual(options[BsdpTag.SELECTED_BOOT_IMAGE],
                         bsdp.u32(self.catalog.by_index(5).image_id))
        self.assertEqual(options[BsdpTag.DEFAULT_BOOT_IMAGE],
                         bsdp.u32(self.catalog.by_index(4).image_id))

    def test_binding_survives_a_restart(self):
        self.select(self.server, 5)
        self.assertEqual(json.loads(self.bindings.read_text()), {MAC: 5})
        options = self.list_options(self.make_server())
        self.assertEqual(options[BsdpTag.SELECTED_BOOT_IMAGE],
                         bsdp.u32(self.catalog.by_index(5).image_id))

    def test_binding_to_a_removed_or_unsuitable_image_is_not_sent(self):
        self.bindings.write_text(json.dumps({MAC: 6}))   # Intel-only image, PPC client
        self.assertNotIn(BsdpTag.SELECTED_BOOT_IMAGE, self.list_options(self.make_server()))
        self.bindings.write_text(json.dumps({MAC: 99}))  # no such image
        self.assertNotIn(BsdpTag.SELECTED_BOOT_IMAGE, self.list_options(self.make_server()))

    def test_select_for_another_server_clears_the_binding(self):
        self.select(self.server, 5)
        self.assertEqual(self.select(self.server, 5, IP("10.0.0.2")), [])
        self.assertNotIn(BsdpTag.SELECTED_BOOT_IMAGE, self.list_options())
        self.assertEqual(json.loads(self.bindings.read_text()), {})

    def test_unreadable_bindings_file_is_ignored(self):
        self.bindings.write_text("not json")
        self.assertNotIn(BsdpTag.SELECTED_BOOT_IMAGE, self.list_options(self.make_server()))

    def test_list_fits_in_one_option_with_the_selected_tag(self):
        for i in range(7, 40):
            make_image(self.root, i, f"Image with a fairly long name {i}")
        self.catalog.reload()
        self.select(self.server, 5)
        [(reply, _)] = self.server.handle(DhcpPacket.decode(
            bsdp_inform(BsdpMessageType.LIST).encode()))
        self.assertLessEqual(len(reply.options[Opt.VENDOR_SPECIFIC]), 255)


if __name__ == "__main__":
    unittest.main()

"""Replay real NetBoot traffic captured from two iMac G3s against the server.

``fixtures/imac-g3-boots.pcap`` was captured on the Fedora host (2026-11-25)
while the server ran in full DHCP mode, NetBoot Macs only, on a household LAN:

    serve -i enp0s31f6 --dhcp-mode full --pool-start 192.168.50.50
          --pool-end 192.168.50.59 --router 192.168.50.253 --dns 192.168.50.253
          --nfs-export /var/lib/netboot-server/images --default ppc=4

It holds only the two iMacs' packets and the server's replies (the routers'
offers and other devices were filtered out):

- tray-loading iMac G3 (iMac,1, 00:05:02:00:00:a1), running the Panther
  installer, picks "Tiger Install" in Startup Disk (BSDP LIST + SELECT from
  its own address), restarts, and boots it through the firmware's plain BOOTP.
  Tiger then broadcasts NetInfo binder CALLITs, which nobody answers.
- slot-loading iMac G3 (PowerMac2,2, 00:30:65:00:00:b2) boots "Jaguar
  Install" with N: firmware DHCP, BSDP LIST + SELECT, TFTP, then the 10.2
  kernel asks the portmapper for MOUNT v1 and NFS v2 over UDP.
"""

from __future__ import annotations

import ipaddress
import tempfile
import time
import unittest
from pathlib import Path

from netboot_server.catalog import Catalog
from netboot_server.events import EventBus
from netboot_server.net import bsdp, nfs2, rpc
from netboot_server.net.bsdp import BsdpTag, ClientClass
from netboot_server.net.dhcp_packet import BOOTREQUEST, DhcpPacket, MessageType, Opt
from netboot_server.net.dhcp_server import DhcpBsdpServer, DhcpConfig
from netboot_server.net.leases import LeasePool
from netboot_server.net.portmap import Portmapper
from netboot_server.store import Store

from helpers import make_image
from capture import FIXTURES, read_udp

IP = ipaddress.IPv4Address
SERVER = "192.168.50.104"
TRAY = "00:05:02:00:00:a1"      # iMac,1
SLOT = "00:30:65:00:00:b2"      # PowerMac2,2
MACS = {TRAY, SLOT}
NFS_PORT = 58565                # the built-in NFS/MOUNT server's port in that run
NETINFO_BINDER = 200100001

PACKETS = read_udp(FIXTURES / "imac-g3-boots.pcap")


def dhcp_packets():
    return [p for p in PACKETS if {p.sport, p.dport} & {67, 68}]


def from_mac(p) -> bool:
    return p.src_mac in MACS


class CapturedPacketTests(unittest.TestCase):
    def test_fixture_holds_only_the_imacs_and_our_server(self):
        self.assertEqual(len(PACKETS), 70)
        for p in PACKETS:
            self.assertTrue(p.src_mac in MACS or p.src == SERVER, p)

    def test_every_dhcp_packet_decodes_and_re_encodes(self):
        for p in dhcp_packets():
            packet = DhcpPacket.decode(p.payload)
            again = DhcpPacket.decode(packet.encode())
            self.assertEqual(again.__dict__, packet.__dict__, p)

    def test_client_classes(self):
        seen = {}
        for p in dhcp_packets():
            if not from_mac(p):
                continue
            packet = DhcpPacket.decode(p.payload)
            client = ClientClass.parse(packet.options.get(Opt.VENDOR_CLASS))
            if client:
                seen[packet.mac] = (client.arch, client.model)
        self.assertEqual(seen, {TRAY: ("ppc", "iMac,1"), SLOT: ("ppc", "PowerMac2,2")})

    def test_tray_imac_firmware_sends_plain_bootp(self):
        firmware = [DhcpPacket.decode(p.payload) for p in dhcp_packets()
                    if p.src_mac == TRAY and p.sport == 1234]
        self.assertEqual(len(firmware), 2)
        for packet in firmware:
            self.assertIsNone(packet.message_type)
            self.assertNotIn(Opt.VENDOR_CLASS, packet.options)
            self.assertEqual(str(packet.ciaddr), "0.0.0.0")

    def test_startup_disk_select_comes_from_the_running_os(self):
        [select] = [DhcpPacket.decode(p.payload) for p in dhcp_packets()
                    if p.src_mac == TRAY and p.sport == 1020][:1]
        self.assertEqual(select.message_type, MessageType.INFORM)
        self.assertEqual(str(select.ciaddr), "192.168.50.51")
        options = bsdp.decode_options(select.options[Opt.VENDOR_SPECIFIC])
        self.assertEqual(options[BsdpTag.MESSAGE_TYPE][0], bsdp.BsdpMessageType.SELECT)
        index, _kind, install = bsdp.split_image_id(
            int.from_bytes(options[BsdpTag.SELECTED_BOOT_IMAGE], "big"))
        self.assertEqual((index, install), (1, True))


class ReplayTests(unittest.TestCase):
    """Feed each captured Mac request to a server set up as in the capture run, and
    check it answers exactly as the real server did."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        images = base / "images"
        common = dict(install=True, BootFile="booter", IsDefault=False,
                      SupportsDiskless=False)
        make_image(images, 1, "Tiger Install", dir_name="tiger-inst", osVersion="10.4.6",
                   **common)
        make_image(images, 2, "Cheetah Install", protocol="NFS", dir_name="cheetah-inst",
                   osVersion="10.0.3", RootDirectory=str(base / "roots" / "cheetah-inst"),
                   **common)
        make_image(images, 4, "Jaguar Install", protocol="NFS", dir_name="jaguar-inst",
                   osVersion="10.2", **common)
        make_image(images, 5, "Panther", dir_name="panther-inst", osVersion="10.3", **common)
        (base / "roots" / "cheetah-inst").mkdir(parents=True)
        catalog = Catalog(images)
        catalog.reload()
        catalog.default_index.update({"ppc": 4})

        # State the server had when the capture started.
        self.store = Store()
        expires = time.time() + 3000
        self.store.save_lease(SLOT, "192.168.50.50", expires)
        self.store.save_lease(TRAY, "192.168.50.51", expires)
        self.store.set_selected_image(TRAY, 5)
        self.store.set_selected_image(SLOT, 4)

        pool = LeasePool(IP("192.168.50.50"), IP("192.168.50.59"), 3600,
                         excluded=[IP(SERVER), IP("192.168.50.253")], store=self.store)
        config = DhcpConfig(server_ip=IP(SERVER), subnet=ipaddress.IPv4Network("192.168.50.0/24"),
                            mode="full", clients="netboot", pool=pool,
                            router=IP("192.168.50.253"), dns=[IP("192.168.50.253")],
                            nfs_export="/var/lib/netboot-server/images",
                            http_base=f"http://{SERVER}")
        self.server = DhcpBsdpServer(config, catalog, EventBus(), self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def exchanges(self):
        """Each Mac request with the server replies captured right after it."""
        out, current = [], None
        for p in dhcp_packets():
            if from_mac(p):
                current = (p, [])
                out.append(current)
            elif p.src == SERVER and current is not None:
                current[1].append(p)
        return out

    def test_replies_match_the_capture(self):
        mismatches = []
        for request, captured in self.exchanges():
            packet = DhcpPacket.decode(request.payload)
            replies = self.server.handle(packet)
            got = [(DhcpPacket.decode(r.encode()), d) for r, d in replies]
            want = [(DhcpPacket.decode(c.payload), (c.dst, c.dport)) for c in captured]
            if len(got) != len(want):
                mismatches.append((request.time, "count", len(got), len(want)))
                continue
            for (g, gd), (w, wd) in zip(got, want):
                if gd != wd:
                    mismatches.append((request.time, "destination", gd, wd))
                for key in w.__dict__:
                    if g.__dict__[key] != w.__dict__[key]:
                        mismatches.append((request.time, key, g.__dict__[key], w.__dict__[key]))
        self.assertEqual(mismatches, [])
        # The Startup Disk choice is saved, as it was in the real run.
        self.assertEqual({c.mac: c.selected_image for c in self.store.clients()},
                         {TRAY: 1, SLOT: 4})


class CapturedRpcTests(unittest.TestCase):
    def setUp(self):
        self.pm = Portmapper(EventBus())
        self.pm.register_local(nfs2.MOUNT_PROG, 1, NFS_PORT)
        self.pm.register_local(nfs2.NFS_PROG, 2, NFS_PORT)
        self.rpc = [p for p in PACKETS if 111 in (p.sport, p.dport)]

    def test_jaguar_kernel_asks_for_mount_v1_and_nfs_v2_over_udp(self):
        calls = [rpc.parse_call(p.payload) for p in self.rpc
                 if p.src_mac == SLOT and p.dport == 111]
        asked = []
        for call in calls:
            self.assertEqual((call.prog, call.proc), (rpc.PMAP_PROG, rpc.PMAPPROC_GETPORT))
            prog, vers, prot, _ = call.args.u32(), call.args.u32(), call.args.u32(), call.args.u32()
            asked.append((prog, vers, prot))
        self.assertEqual(asked, [(nfs2.MOUNT_PROG, 1, 17), (nfs2.NFS_PROG, 2, 17)])

    def test_getport_replies_match_the_capture(self):
        pairs = [(a, b) for a, b in zip(self.rpc, self.rpc[1:])
                 if a.src_mac == SLOT and b.src == SERVER]
        self.assertEqual(len(pairs), 2)
        for call, reply in pairs:
            self.assertEqual(self.pm.handle(call.payload, call.src), reply.payload)

    def test_netinfo_binder_broadcasts_get_no_answer(self):
        broadcasts = [p for p in self.rpc if p.src_mac == TRAY]
        self.assertEqual(len(broadcasts), 8)
        for p in broadcasts:
            self.assertEqual(p.dst, "192.168.50.255")
            call = rpc.parse_call(p.payload)
            self.assertEqual(call.proc, rpc.PMAPPROC_CALLIT)
            self.assertEqual(call.args.u32(), NETINFO_BINDER)
            self.assertIsNone(self.pm.handle(p.payload, p.src))


if __name__ == "__main__":
    unittest.main()

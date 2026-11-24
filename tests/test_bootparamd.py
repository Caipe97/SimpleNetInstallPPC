"""bootparamd, checked against the request/reply handling of xnu-123.5 nfs_boot.c."""

import ipaddress
import struct
import tempfile
import unittest
from pathlib import Path

from netboot_server.catalog import Catalog
from netboot_server.net import rpc
from netboot_server.net.bootparamd import (BOOTPARAM_PROG, BOOTPARAM_VERS, PROC_GETFILE,
                                           PROC_NULL, PROC_WHOAMI, BootparamServer,
                                           client_hostname)

from helpers import RecordingBus, make_image

IP = ipaddress.IPv4Address
SERVER = IP("192.168.40.101")
CLIENT = IP("192.168.40.102")
MAC = "00:30:65:00:00:b2"


def kernel_inaddr(ip: IP) -> bytes:
    """bp_inaddr as the kernel builds it: atype 1, then one IP byte per 32-bit word."""
    return struct.pack(">I4i", 1, *ip.packed)


def kernel_string(text: str) -> bytes:
    raw = text.encode()
    return struct.pack(">I", len(raw)) + raw + b"\0" * (-len(raw) % 4)


def call(proc: int, args: bytes, xid: int = 0x1234, prog: int = BOOTPARAM_PROG,
         vers: int = BOOTPARAM_VERS) -> bytes:
    # AUTH_NULL credentials and verifier, as krpc_call sends.
    return struct.pack(">10I", xid, 0, 2, prog, vers, proc, 0, 0, 0, 0) + args


class KernelReply:
    """Reads replies the way krpc_call + bp_whoami/bp_getfile do."""

    def __init__(self, data: bytes, xid: int = 0x1234):
        xid_, direction, astatus, _verf_flavor, verf_len, rstatus = struct.unpack_from(">6I", data)
        assert (xid_, direction, astatus, verf_len, rstatus) == (xid, 1, 0, 0, 0), \
            "kernel would ignore this reply and retry forever"
        self.offset = 24
        self.data = data

    def u32(self):
        value = struct.unpack_from(">I", self.data, self.offset)[0]
        self.offset += 4
        return value

    def string(self, limit):
        length = self.u32()
        assert length < limit, "kernel: bad reply"
        value = self.data[self.offset:self.offset + length].decode()
        self.offset += (length + 3) & ~3
        return value

    def inaddr(self):
        assert self.u32() == 1, "kernel: atype != 1 is a bad reply"
        return IP(bytes(self.u32() for _ in range(4)))


class BootparamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.mounted = root / "mnt" / "cheetah-inst"
        self.mounted.mkdir(parents=True)
        (self.mounted / "mach_kernel").write_bytes(b"k")
        make_image(root / "images", 2, "Cheetah", ("ppc",), protocol="NFS",
                   RootDirectory=str(self.mounted))
        make_image(root / "images", 1, "Tiger", ("ppc",))
        self.catalog = Catalog(root / "images")
        self.catalog.reload()
        self.events = RecordingBus()
        clients = {CLIENT: (MAC, self.catalog.by_index(2))}
        self.server = BootparamServer(SERVER, clients.get, self.events,
                                      router=IP("192.168.40.1"))

    def tearDown(self):
        self.tmp.cleanup()

    def whoami(self, ip=CLIENT):
        return self.server.handle(call(PROC_WHOAMI, kernel_inaddr(ip)), "127.0.0.1")

    def test_whoami_reply_parses_like_the_kernel(self):
        reply = KernelReply(self.whoami())
        self.assertEqual(reply.string(256), client_hostname(MAC))
        self.assertEqual(reply.string(256), "")
        self.assertEqual(reply.inaddr(), IP("192.168.40.1"))

    def test_getfile_root_and_private(self):
        self.whoami()
        name = kernel_string(client_hostname(MAC))
        root = KernelReply(self.server.handle(call(PROC_GETFILE, name + kernel_string("root")), "x"))
        self.assertEqual(root.string(90), "netboot")
        self.assertEqual(root.inaddr(), SERVER)
        root_path = root.string(1024)
        self.assertEqual(root_path, str(self.mounted))
        private = KernelReply(self.server.handle(call(PROC_GETFILE, name + kernel_string("private")), "x"))
        private.string(90)
        self.assertEqual(private.inaddr(), SERVER)
        # Equal to <root>/private on the same server: the kernel skips the mount.
        self.assertEqual(private.string(1024), root_path + "/private")

    def test_unknown_client_gets_no_reply(self):
        self.assertIsNone(self.whoami(IP("192.168.40.200")))
        self.assertIn(("bootparam", "whoami-unknown"), self.events.kinds())
        self.assertIsNone(self.server.handle(
            call(PROC_GETFILE, kernel_string("stranger") + kernel_string("root")), "x"))

    def test_null_and_errors(self):
        self.assertEqual(self.server.handle(call(PROC_NULL, b""), "x"), rpc.accepted_reply(0x1234))
        bad_vers = self.server.handle(call(PROC_WHOAMI, b"", vers=3), "x")
        self.assertEqual(struct.unpack_from(">I", bad_vers, 20)[0], rpc.PROG_MISMATCH)
        garbage = self.server.handle(call(PROC_WHOAMI, b"\0\0"), "x")
        self.assertEqual(struct.unpack_from(">I", garbage, 20)[0], rpc.GARBAGE_ARGS)
        self.assertIsNone(self.server.handle(b"not rpc", "x"))

    def test_catalog_reports_unmounted_root_directory(self):
        self.assertEqual(self.catalog.by_index(2).problems(), [])
        (self.mounted / "mach_kernel").unlink()
        self.assertTrue(any("RootDirectory" in p for p in self.catalog.by_index(2).problems()))


class XdrTests(unittest.TestCase):
    def test_strings_are_padded(self):
        self.assertEqual(rpc.pack_string("abcde"), b"\0\0\0\x05abcde\0\0\0")
        u = rpc.Unpacker(rpc.pack_string("abcde") + rpc.pack_u32(7))
        self.assertEqual(u.string(), "abcde")
        self.assertEqual(u.u32(), 7)

    def test_parse_reply(self):
        reply = rpc.accepted_reply(99, rpc.pack_u32(5))
        self.assertEqual(rpc.parse_reply(reply, 99).u32(), 5)
        self.assertIsNone(rpc.parse_reply(reply, 98))


if __name__ == "__main__":
    unittest.main()


class LookupTests(unittest.TestCase):
    """The tray-loading iMac case: BOOTP only (no vendor class), or a server restart mid-boot."""

    def setUp(self):
        from netboot_server.net.dhcp_server import DhcpBsdpServer, DhcpConfig
        from netboot_server.net.leases import LeasePool
        from netboot_server.server import make_bootparam_lookup
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        mounted = root / "mnt"
        mounted.mkdir()
        (mounted / "mach_kernel").write_bytes(b"k")
        make_image(root / "images", 2, "Cheetah", ("ppc",), protocol="NFS",
                   RootDirectory=str(mounted))
        catalog = Catalog(root / "images")
        catalog.reload()
        catalog.default_index["ppc"] = 2
        self.pool = LeasePool(IP("192.168.40.100"), IP("192.168.40.150"), excluded=[SERVER])
        config = DhcpConfig(server_ip=SERVER, subnet=ipaddress.IPv4Network("192.168.40.0/24"),
                            mode="full", pool=self.pool)
        self.dhcp = DhcpBsdpServer(config, catalog, RecordingBus())
        self.lookup = make_bootparam_lookup(self.dhcp, catalog)

    def tearDown(self):
        self.tmp.cleanup()

    def test_plain_bootp_client_is_tracked(self):
        from test_bootp import kernel_bootp_request
        from netboot_server.net.dhcp_packet import DhcpPacket
        [(reply, _)] = self.dhcp.handle(DhcpPacket.decode(kernel_bootp_request()))
        mac, image = self.lookup(reply.yiaddr)
        self.assertEqual(mac, "00:0a:95:aa:bb:cc")
        self.assertEqual(image.index, 2)

    def test_lease_table_covers_a_server_restart(self):
        self.pool.offer("00:05:02:00:00:a1", IP("192.168.40.100"))
        self.pool.confirm("00:05:02:00:00:a1", IP("192.168.40.100"))
        self.assertEqual(self.dhcp.clients, {})          # fresh server, nothing seen
        mac, image = self.lookup(IP("192.168.40.100"))
        self.assertEqual((mac, image.index), ("00:05:02:00:00:a1", 2))
        self.assertIsNone(self.lookup(IP("192.168.40.140")))

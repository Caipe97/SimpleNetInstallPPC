"""Folder-style roots for 10.0/10.1: exports management, NFS v2 server, portmapper."""

import ipaddress
import os
import socket
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from netboot_server.catalog import Catalog
from netboot_server.events import EventBus
from netboot_server.net import nfs2, rpc
from netboot_server.net.bootparamd import BOOTPARAM_PROG, PROC_WHOAMI
from netboot_server.net.portmap import Portmapper, answer_broadcast, parse_ipv4_udp
from netboot_server.roots import (BEGIN, END, RootManager, default_root_directory,
                                  is_folder_style, render_exports)

from helpers import make_image

NET = ipaddress.IPv4Network("192.168.40.0/24")
CLIENT = "192.168.40.102"


def call(prog, vers, proc, args=b"", xid=7):
    return rpc.build_call(xid, prog, vers, proc, args)


def results(reply, xid=7):
    u = rpc.parse_reply(reply, xid)
    assert u is not None, "not a successful RPC reply"
    return u


class VersionTests(unittest.TestCase):
    def test_folder_style_versions(self):
        for version in ("10.0", "10.0.3", "10.0.4", "10.1", "10.1.5"):
            self.assertTrue(is_folder_style(version), version)
        for version in ("10.2", "10.2.8", "10.4.6", "10.10", "unknown", "9.2.2"):
            self.assertFalse(is_folder_style(version), version)

    def test_default_root_has_no_spaces(self):
        path = default_root_directory(Path("/Library/Application Support/NetBootServer/images"),
                                      "puma-inst")
        self.assertNotIn(" ", str(path))
        self.assertTrue(str(path).endswith("puma-inst"))


class ExportsBlockTests(unittest.TestCase):
    MANUAL = '"/Library/Application Support/NetBootServer/images" -ro -network 192.168.40.0 -mask 255.255.255.0\n'

    def test_adds_block_and_keeps_manual_lines(self):
        text, skipped = render_exports(self.MANUAL, ["/private/var/netboot/jaguar"], NET)
        self.assertTrue(text.startswith(self.MANUAL))
        self.assertIn(BEGIN, text)
        self.assertIn("/private/var/netboot/jaguar -ro -maproot=root -network 192.168.40.0 "
                      "-mask 255.255.255.0", text)
        self.assertEqual(skipped, [])

    def test_replaces_block_and_skips_hand_exported_paths(self):
        manual = self.MANUAL + "/private/var/netboot/cheetah-inst -ro -maproot=root -network 192.168.40.0 -mask 255.255.255.0\n"
        first, _ = render_exports(manual, ["/private/var/netboot/a", "/private/var/netboot/b"], NET)
        second, skipped = render_exports(first, ["/private/var/netboot/cheetah-inst",
                                                 "/private/var/netboot/b"], NET)
        self.assertEqual(skipped, ["/private/var/netboot/cheetah-inst"])
        self.assertNotIn("/private/var/netboot/a ", second)
        self.assertEqual(second.count(BEGIN), 1)
        self.assertEqual(second.count("/private/var/netboot/cheetah-inst"), 1)

    def test_removes_block_when_nothing_to_export(self):
        with_block, _ = render_exports(self.MANUAL, ["/x"], NET)
        text, _ = render_exports(with_block, [], NET)
        self.assertEqual(text, self.MANUAL)
        self.assertNotIn(END, text)

    def test_quotes_paths_with_spaces(self):
        text, _ = render_exports("", ["/srv/my roots/x"], NET)
        self.assertIn('"/srv/my roots/x" -ro', text)


class FakeRunner:
    def __init__(self, fail=()):
        self.commands = []
        self.fail = set(fail)

    def __call__(self, cmd):
        self.commands.append(list(cmd))
        code = 1 if " ".join(cmd[:2]) in self.fail else 0
        out = "nfsd is running" if cmd[:2] == ["nfsd", "status"] else ""
        return subprocess.CompletedProcess(cmd, code, out, "bad export" if code else "")


class RootManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.exports = self.root / "exports"
        self.exports.write_text(ExportsBlockTests.MANUAL)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rejected_exports_are_rolled_back(self):
        runner = FakeRunner(fail={"nfsd checkexports"})
        manager = RootManager("system", NET, EventBus(), self.exports, runner)
        error = manager.export_system(["/private/var/netboot/new"])
        self.assertIn("restored", error)
        self.assertEqual(self.exports.read_text(), ExportsBlockTests.MANUAL)
        self.assertNotIn(["nfsd", "update"], runner.commands)

    def test_accepted_exports_are_applied(self):
        runner = FakeRunner()
        manager = RootManager("system", NET, EventBus(), self.exports, runner)
        self.assertIsNone(manager.export_system(["/private/var/netboot/new"]))
        self.assertIn(BEGIN, self.exports.read_text())
        self.assertIn(["nfsd", "update"], runner.commands)
        runner.commands.clear()
        self.assertIsNone(manager.export_system(["/private/var/netboot/new"]))   # no change
        self.assertEqual(runner.commands, [])

    def test_sync_mounts_and_skips_disabled_images(self):
        images = self.root / "images"
        make_image(images, 2, "Cheetah", protocol="NFS", RootDirectory=str(self.root / "r2"))
        make_image(images, 3, "Puma", protocol="NFS", RootDirectory=str(self.root / "r3"),
                   IsEnabled=False)
        catalog = Catalog(images)
        catalog.reload()
        runner = FakeRunner()

        class Manager(RootManager):
            mounted = set()

            @classmethod
            def is_mounted(cls, path):
                return str(path) in cls.mounted

        def mounting_runner(cmd):
            result = runner(cmd)
            if cmd[0] in ("hdiutil", "mount"):
                Manager.mounted.add(cmd[cmd.index("-mountpoint") + 1] if "-mountpoint" in cmd else cmd[-1])
            return result

        manager = Manager("builtin", NET, EventBus(), self.exports, mounting_runner)
        statuses = manager.sync(catalog)
        self.assertEqual([(s.image.index, s.mounted) for s in statuses], [(2, True)])
        self.assertEqual(manager.builtin_exports(statuses), [str(self.root / "r2")])
        mount_cmds = [c for c in runner.commands if c[0] in ("hdiutil", "mount")]
        self.assertEqual(len(mount_cmds), 1)
        self.assertIn(str((images / "img2" / "root.dmg").resolve()), mount_cmds[0])


class Nfs2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.export = Path(self.tmp.name) / "root"
        (self.export / "System" / "Library").mkdir(parents=True)
        self.kernel = os.urandom(20000)
        (self.export / "mach_kernel").write_bytes(self.kernel)
        for i in range(60):
            (self.export / "System" / "Library" / f"Framework{i:02d}").write_bytes(b"x")
        (self.export / "link").symlink_to("/etc/passwd")
        self.server = nfs2.Nfs2Server(EventBus(), allowed=NET)
        self.server.set_exports([str(self.export)])

    def tearDown(self):
        self.tmp.cleanup()

    def nfs(self, proc, args=b"", source=CLIENT):
        return self.server.handle(call(nfs2.NFS_PROG, nfs2.NFS_VERS, proc, args), source)

    def mount(self, path=None, source=CLIENT):
        reply = self.server.handle(call(nfs2.MOUNT_PROG, nfs2.MOUNT_VERS, nfs2.M_MNT,
                                        rpc.pack_string(path or str(self.export))), source)
        u = results(reply)
        status = u.u32()
        return status, u.rest()[:32] if status == 0 else None

    def lookup(self, fh, name):
        u = results(self.nfs(nfs2.P_LOOKUP, fh + rpc.pack_string(name)))
        status = u.u32()
        if status:
            return status, None, None
        handle = u.rest()[:32]
        u.offset += 32
        return status, handle, u.u32()   # u32 = file type

    def test_mount_lookup_read_like_the_10_0_kernel(self):
        status, root = self.mount()
        self.assertEqual(status, 0)
        status, fh, ftype = self.lookup(root, "mach_kernel")
        self.assertEqual((status, ftype), (0, nfs2.NFREG))
        data = b""
        while len(data) < len(self.kernel):
            u = results(self.nfs(nfs2.P_READ, fh + rpc.pack_u32(len(data), 8192, 0)))
            self.assertEqual(u.u32(), 0)
            u.offset += 68                        # fattr
            data += u.opaque()
        self.assertEqual(data, self.kernel)

    def test_mount_refused_for_unexported_path_or_foreign_client(self):
        self.assertEqual(self.mount("/etc")[0], nfs2.NFSERR_ACCES)
        self.assertEqual(self.mount(source="10.9.9.9")[0], nfs2.NFSERR_ACCES)

    def test_readdir_pages_through_everything(self):
        _, root = self.mount()
        _, system, _ = self.lookup(root, "System")
        _, library, ftype = self.lookup(system, "Library")
        self.assertEqual(ftype, nfs2.NFDIR)
        names, cookie = [], 0
        for _ in range(100):
            u = results(self.nfs(nfs2.P_READDIR, library + struct.pack(">II", cookie, 512)))
            self.assertEqual(u.u32(), 0)
            while u.u32():
                u.u32()
                names.append(u.string())
                cookie = u.u32()
            if u.u32():
                break
        self.assertEqual(sorted(n for n in names if n not in (".", "..")),
                         sorted(os.listdir(self.export / "System" / "Library")))

    def test_dotdot_stays_inside_export_and_symlinks_are_not_followed(self):
        _, root = self.mount()
        status, parent, _ = self.lookup(root, "..")
        self.assertEqual(status, 0)
        self.assertEqual(self.lookup(parent, "mach_kernel")[0], 0)   # still the export root
        status, link, ftype = self.lookup(root, "link")
        self.assertEqual(ftype, nfs2.NFLNK)
        u = results(self.nfs(nfs2.P_READLINK, link))
        self.assertEqual((u.u32(), u.string()), (0, "/etc/passwd"))
        self.assertEqual(results(self.nfs(nfs2.P_READ, link + rpc.pack_u32(0, 100, 0))).u32(),
                         nfs2.NFSERR_INVAL)
        self.assertEqual(self.lookup(root, "../etc")[0], nfs2.NFSERR_NOENT)

    def test_writes_are_refused_and_stale_handles_reported(self):
        _, root = self.mount()
        self.assertEqual(results(self.nfs(nfs2.P_CREATE, root)).u32(), nfs2.NFSERR_ROFS)
        self.assertEqual(results(self.nfs(nfs2.P_GETATTR, b"\1" * 32)).u32(), nfs2.NFSERR_STALE)
        self.assertEqual(results(self.nfs(nfs2.P_GETATTR, root, source="10.9.9.9")).u32(),
                         nfs2.NFSERR_ACCES)

    def test_statfs_and_getattr(self):
        _, root = self.mount()
        u = results(self.nfs(nfs2.P_GETATTR, root))
        self.assertEqual((u.u32(), u.u32()), (0, nfs2.NFDIR))
        self.assertEqual(results(self.nfs(nfs2.P_STATFS, root)).u32(), 0)


class PortmapperTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def bootparam(proc, args):
            self.calls.append(proc)
            return rpc.pack_string("nb-123456")
        self.pm = Portmapper(EventBus())
        self.pm.register_local(BOOTPARAM_PROG, 1, 4000, bootparam)
        self.pm.register_local(nfs2.NFS_PROG, 2, 5000)

    def callit(self):
        args = rpc.pack_u32(BOOTPARAM_PROG, 1, PROC_WHOAMI) + rpc.pack_opaque(b"\0" * 20)
        return call(rpc.PMAP_PROG, 2, rpc.PMAPPROC_CALLIT, args)

    def test_getport_and_callit(self):
        u = results(self.pm.handle(call(rpc.PMAP_PROG, 2, rpc.PMAPPROC_GETPORT,
                                        rpc.pack_u32(nfs2.NFS_PROG, 2, 17, 0)), CLIENT))
        self.assertEqual(u.u32(), 5000)
        u = results(self.pm.handle(self.callit(), CLIENT))
        self.assertEqual(u.u32(), 4000)              # kernel: bootparam port first
        self.assertEqual(rpc.Unpacker(u.opaque()).string(), "nb-123456")
        self.assertEqual(self.calls, [PROC_WHOAMI])

    def test_set_only_from_loopback(self):
        args = rpc.pack_u32(123, 1, 17, 999)
        self.assertEqual(results(self.pm.handle(call(rpc.PMAP_PROG, 2, rpc.PMAPPROC_SET, args),
                                                CLIENT)).u32(), 0)
        self.assertEqual(results(self.pm.handle(call(rpc.PMAP_PROG, 2, rpc.PMAPPROC_SET, args),
                                                "127.0.0.1")).u32(), 1)

    def test_broadcast_listener_answers_raw_callit(self):
        payload = self.callit()
        udp = struct.pack(">HHHH", 1023, 111, 8 + len(payload), 0) + payload
        ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0,
                         socket.inet_aton(CLIENT), socket.inet_aton("255.255.255.255")) + udp
        self.assertEqual(parse_ipv4_udp(ip)[:4], (CLIENT, 1023, "255.255.255.255", 111))
        reply, destination = answer_broadcast(ip, {"255.255.255.255"}, self.pm.programs)
        self.assertEqual(destination, (CLIENT, 1023))
        self.assertEqual(results(reply).u32(), 4000)
        unicast = ip[:16] + socket.inet_aton("192.168.40.101") + ip[20:]
        self.assertIsNone(answer_broadcast(unicast, {"255.255.255.255"}, self.pm.programs))


if __name__ == "__main__":
    unittest.main()

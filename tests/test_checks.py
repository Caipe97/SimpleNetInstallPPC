"""`check` against fake hosts, and the defaults that let `serve` run as a service."""

import os
import subprocess
import unittest
from pathlib import Path

from simplenetinstallppc import checks, cli
from simplenetinstallppc.catalog import resolve_root_directory
from simplenetinstallppc.checks import MISSING, OK, WARN, Host, run_checks
from simplenetinstallppc.server import ServeOptions, with_default_export

LINUX_TOOLS = {"ip", "mount", "umount", "modprobe", "systemd-inhibit", "systemctl"}


def fake_host(platform="linux", tools=LINUX_TOOLS, commands=None, files=None, euid=0):
    commands = commands or {}
    files = files or {"/proc/filesystems": "nodev\tproc\n\thfsplus\n"}

    def run(cmd):
        out = commands.get(" ".join(cmd), (1, "", ""))
        return subprocess.CompletedProcess(cmd, *out)

    def read(path):
        if str(path) not in files:
            raise FileNotFoundError(path)
        return files[str(path)]

    return Host(platform=platform, which=lambda name: f"/usr/bin/{name}" if name in tools else None,
                run=run, exists=lambda path: str(path) in files, read=read, euid=euid)


def by_name(host):
    return {c.name: c for c in run_checks(host)}


class CheckTests(unittest.TestCase):
    def test_ready_linux_host(self):
        files = {"/proc/filesystems": "\thfsplus\n", str(checks.SYSTEMD_UNIT): ""}
        host = fake_host(files=files, commands={"systemctl is-active simplenetinstallppc":
                                                (0, "active\n", "")})
        found = by_name(host)
        self.assertEqual({c.state for c in found.values()}, {OK}, checks.report(list(found.values())))
        self.assertEqual(found["Firewall"].detail, "no firewalld")

    def test_missing_pieces(self):
        host = fake_host(tools={"ip", "mount"}, files={"/proc/filesystems": "\text4\n"}, euid=1000)
        found = by_name(host)
        self.assertEqual(found["Commands"].state, MISSING)
        self.assertIn("modprobe", found["Commands"].detail)
        self.assertEqual(found["HFS+ support"].state, MISSING)
        self.assertTrue(any("dnf install" in f for f in found["HFS+ support"].fix))
        self.assertEqual(found["Running as root"].state, WARN)
        self.assertEqual(found["Service"].state, OK)              # optional

    def test_hfsplus_module_not_loaded_yet(self):
        host = fake_host(files={"/proc/filesystems": "\text4\n"},
                         commands={"modinfo -n hfsplus": (0, "/lib/modules/x/hfsplus.ko.xz\n", "")})
        self.assertEqual(by_name(host)["HFS+ support"].state, OK)

    def test_firewalld(self):
        tools = LINUX_TOOLS | {"firewall-cmd"}
        open_all = {"firewall-cmd --list-ports": (0, "67/udp 69/udp 1025-65535/udp\n", ""),
                    "firewall-cmd --list-services": (0, "ssh http rpc-bind\n", "")}
        self.assertEqual(by_name(fake_host(tools=tools, commands=open_all))["Firewall"].state, OK)

        closed = {"firewall-cmd --list-ports": (0, "69/udp\n", ""),
                  "firewall-cmd --list-services": (0, "ssh\n", "")}
        firewall = by_name(fake_host(tools=tools, commands=closed))["Firewall"]
        self.assertEqual(firewall.state, MISSING)
        for text in ("67/udp", "80/tcp", "111/udp", "UDP above 1024"):
            self.assertIn(text, firewall.detail)
        self.assertNotIn("--add-port=69/udp", " ".join(firewall.fix))
        self.assertEqual(firewall.fix[-1], "sudo firewall-cmd --reload")

        stopped = {"firewall-cmd --list-ports": (252, "FirewallD is not running\n", "")}
        self.assertEqual(by_name(fake_host(tools=tools, commands=stopped))["Firewall"].detail,
                         "no firewalld")
        denied = {"firewall-cmd --list-ports": (253, "", "Authorization failed.\n")}
        self.assertEqual(by_name(fake_host(tools=tools, commands=denied))["Firewall"].state, WARN)

    def test_mac(self):
        tools = {"hdiutil", "ditto", "diskutil", "nfsd", "caffeinate"}
        found = by_name(fake_host(platform="darwin", tools=tools,
                                  files={str(checks.LAUNCHD_PLIST): ""}))
        self.assertNotIn("HFS+ support", found)
        self.assertNotIn("Firewall", found)
        self.assertEqual((found["Commands"].state, found["Service"].state), (OK, OK))

    def test_service_hint(self):
        self.assertIn("systemctl reload", checks.service_hint(
            fake_host(files={str(checks.SYSTEMD_UNIT): ""})))
        self.assertEqual(checks.service_hint(fake_host()), "")


class ServiceDefaultsTests(unittest.TestCase):
    def test_builtin_nfs_exports_the_images_folder(self):
        options = ServeOptions(interface=None, images_root=Path("/srv/snippc/images"), nfs_mode="builtin")
        self.assertEqual(with_default_export(options).nfs_export,
                         str(Path("/srv/snippc/images").resolve()))
        given = ServeOptions(interface=None, images_root=Path("/x"), nfs_mode="builtin", nfs_export="/exports")
        self.assertEqual(with_default_export(given).nfs_export, "/exports")
        system = ServeOptions(interface=None, images_root=Path("/x"), nfs_mode="system")
        self.assertIsNone(with_default_export(system).nfs_export)


if __name__ == "__main__":
    unittest.main()


class DataFolderTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.saved = {k: os.environ.pop(k, None) for k in ("NETBOOT_DATA", cli.FILE_ENV)}

    def tearDown(self):
        for key, value in self.saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_order_of_choice(self):
        stick = self.dir / "stick"
        stick.mkdir()
        os.environ[cli.FILE_ENV] = str(stick / "simplenetinstallppc.pyz")
        # From the .pyz: next to it, whether the folder exists yet or not.
        beside = ((stick / cli.PORTABLE_DIR).resolve(), "next to simplenetinstallppc.pyz")
        self.assertEqual(cli.find_data_root(), beside)
        (stick / cli.PORTABLE_DIR).mkdir()
        self.assertEqual(cli.find_data_root(), beside)
        os.environ["NETBOOT_DATA"] = "/srv/netboot"
        self.assertEqual(cli.find_data_root()[0], Path("/srv/netboot"))
        self.assertEqual(cli.find_data_root(Path("/x"))[0], Path("/x"))

    def test_installed_command_uses_the_system_folder(self):
        for folder in ("/usr/local/bin", "/usr/bin", "/opt/homebrew/bin"):
            os.environ[cli.FILE_ENV] = f"{folder}/simplenetinstallppc"
            self.assertEqual(cli.find_data_root(), (cli.system_data_root(), "the system folder"))

    def test_from_the_source_folder(self):
        self.assertEqual(cli.find_data_root()[0], cli.system_data_root())

    def test_folder_roots_move_with_the_data_folder(self):
        images = self.dir / "stick" / "data" / "images"
        here = str(images.resolve().parent)
        self.assertEqual(resolve_root_directory("roots/puma", images, "puma"), f"{here}/roots/puma")
        # Built on another host, then the folder was copied here.
        self.assertEqual(resolve_root_directory("/var/lib/netboot-server/roots/puma", images, "puma"),
                         f"{here}/roots/puma")
        for kept in ("/private/var/netboot/puma", "/srv/roots/other"):
            self.assertEqual(resolve_root_directory(kept, images, "puma"), kept)


class FilesystemTests(unittest.TestCase):
    MOUNTS = ("/dev/nvme0n1p6 / btrfs rw 0 0\n"
              "/dev/sdb1 /run/media/pat/MY\\040STICK vfat rw 0 0\n"
              "/dev/sdc1 /mnt/big exfat rw 0 0\n")

    def test_linux(self):
        host = fake_host(files={"/proc/self/mounts": self.MOUNTS})
        self.assertEqual(checks.filesystem_of(Path("/var/lib/netboot-server"), host), "btrfs")
        self.assertEqual(checks.filesystem_of(Path("/run/media/pat/MY STICK/d"), host), "vfat")
        self.assertEqual(checks.filesystem_of(Path("/mnt/bigger"), host), "btrfs")

    def test_mac(self):
        mount = ("/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
                 "/dev/disk4s1 on /Volumes/NO NAME (msdos, local, nodev, nosuid, noowners)\n")
        host = fake_host(platform="darwin", commands={"mount": (0, mount, "")})
        self.assertEqual(checks.filesystem_of(Path("/Volumes/NO NAME/x"), host), "msdos")
        self.assertEqual(checks.filesystem_of(Path("/Library"), host), "apfs")

    def test_fat_is_warned_about(self):
        stick = Path("/run/media/pat/MY STICK/simplenetinstallppc-data")
        host = fake_host(files={"/proc/self/mounts": self.MOUNTS, str(stick): ""})
        found = checks.check_data_folder(host, stick, "next to simplenetinstallppc.pyz")
        self.assertEqual(found.state, WARN)
        self.assertIn("4 GiB", found.detail)
        exfat = checks.check_data_folder(
            fake_host(files={"/proc/self/mounts": self.MOUNTS, "/mnt/big/d": ""}),
            Path("/mnt/big/d"), "--data")
        self.assertEqual((exfat.state, exfat.detail), (OK, "/mnt/big/d (--data), exfat"))

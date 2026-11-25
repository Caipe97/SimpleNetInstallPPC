import shutil
import subprocess
import sys
import unittest

from netboot_server.server import keep_awake_command


class KeepAwakeCommandTests(unittest.TestCase):
    def test_macos_uses_caffeinate_watching_us(self):
        self.assertEqual(keep_awake_command(42, "darwin"),
                         ["caffeinate", "-i", "-s", "-w", "42"])

    def test_linux_blocks_sleep_and_idle_until_we_exit(self):
        command = keep_awake_command(42, "linux", which=lambda name: f"/usr/bin/{name}")
        self.assertEqual(command[:5], ["systemd-inhibit", "--what=sleep:idle",
                                       "--who=netboot-server", "--why=Serving NetBoot clients",
                                       "--mode=block"])
        self.assertEqual(command[5:], ["tail", "--pid=42", "-f", "/dev/null"])

    def test_linux_without_systemd(self):
        self.assertIsNone(keep_awake_command(42, "linux", which=lambda name: None))

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("caffeinate"),
                         "needs caffeinate")
    def test_helper_exits_with_the_watched_process(self):
        watched = subprocess.Popen(["sleep", "0.5"])
        helper = subprocess.Popen(keep_awake_command(watched.pid))
        watched.wait()
        self.assertEqual(helper.wait(timeout=10), 0)


if __name__ == "__main__":
    unittest.main()

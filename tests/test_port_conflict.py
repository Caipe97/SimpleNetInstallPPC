import unittest

from simplenetinstallppc.server import parse_server_processes, port_in_use_message

PS = """\
  1 /sbin/launchd
 9567 sudo PYTHONPATH=src python3 -m simplenetinstallppc serve -i en7 --dhcp-mode full
 9568 /Users/x/.pyenv/versions/3.11.14/bin/python3 -m simplenetinstallppc serve -i en7 --dhcp-mode full
 9600 /usr/local/bin/simplenetinstallppc -v serve -i eth0
 9700 python3 -m simplenetinstallppc make-test-image disc.iso --dir serve
 9800 vim src/simplenetinstallppc/server.py
 9900 python3 -m simplenetinstallppc serve -i en7
 9950 python3 /media/usb/simplenetinstallppc.pyz serve
"""


class ParseServerProcessesTests(unittest.TestCase):
    def test_finds_servers_and_skips_sudo_other_commands_and_self(self):
        found = parse_server_processes(PS, exclude={9900})
        self.assertEqual([pid for pid, _ in found], [9568, 9600, 9950])

    def test_finds_a_server_started_under_the_old_name(self):
        ps = " 4242 python3 -m netboot_server serve -i en7\n 4243 /usr/bin/netboot-server serve\n"
        self.assertEqual([pid for pid, _ in parse_server_processes(ps, set())], [4242, 4243])

    def test_empty_output(self):
        self.assertEqual(parse_server_processes("", set()), [])


class MessageTests(unittest.TestCase):
    def test_names_the_running_server(self):
        msg = port_in_use_message("DHCP/BSDP", 67, [(9568, "python3 -m simplenetinstallppc serve")])
        self.assertIn("another simplenetinstallppc (pid 9568)", msg)
        self.assertIn("sudo kill -HUP 9568", msg)
        self.assertNotIn("Internet Sharing", msg)

    def test_falls_back_to_port_hints(self):
        msg = port_in_use_message("DHCP/BSDP", 67, [])
        self.assertIn("Internet Sharing", msg)


if __name__ == "__main__":
    unittest.main()

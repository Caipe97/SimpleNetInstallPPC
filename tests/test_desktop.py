"""When and how serve opens the web UI in the user's browser."""

import unittest

from simplenetinstallppc.desktop import Proc, Situation, User, browser_command, ui_url

URL = "http://127.0.0.1:8080/"
ME = User("pat", 1000, "/home/pat")
DESKTOP = {"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0", "XDG_RUNTIME_DIR": "/run/user/1000",
           "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus", "HOME": "/home/pat",
           "SECRET_TOKEN": "not passed on"}
# Konsole -> zsh -> sudo -> (serve): sudo runs as root with a cleaned environment.
SUDO_FROM_DESKTOP = [Proc(300, 0, "sudo", {"PATH": "/usr/bin"}),
                     Proc(200, 1000, "zsh", DESKTOP), Proc(100, 1000, "konsole", DESKTOP)]


def linux(**changes):
    values = dict(platform="linux", euid=0, user=ME, ancestors=SUDO_FROM_DESKTOP)
    values.update(changes)
    return Situation(**values)


class LinuxTests(unittest.TestCase):
    def test_desktop_terminal_under_sudo(self):
        command, why = browser_command(URL, linux())
        self.assertEqual(why, "opened")
        self.assertEqual(command[:4], ["runuser", "-u", "pat", "--"])
        self.assertEqual(command[4:6], ["env", "-i"])
        self.assertEqual(command[-2:], ["xdg-open", URL])
        env = dict(item.split("=", 1) for item in command[6:-2])
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-1")       # read, not guessed
        self.assertEqual(env["HOME"], "/home/pat")               # the user's, not root's
        self.assertNotIn("SECRET_TOKEN", env)

    def test_without_sudo(self):
        command, _ = browser_command(URL, linux(euid=1000, ancestors=SUDO_FROM_DESKTOP[1:]))
        self.assertEqual(command[:2], ["env", "-i"])

    def test_not_opened(self):
        ssh = [Proc(300, 0, "sudo"), Proc(200, 1000, "bash", {"SSH_CONNECTION": "x"}),
               Proc(150, 1000, "sshd-session"), Proc(100, 0, "sshd")]
        cases = {
            "started over SSH": linux(ancestors=ssh),
            "not started from a terminal": linux(interactive=False),       # a service
            "running as root without sudo": linux(user=None),
            "not started from a desktop session": linux(ancestors=[
                Proc(300, 0, "sudo"), Proc(200, 1000, "bash", {"TERM": "linux"})]),  # a text console
        }
        for why, situation in cases.items():
            self.assertEqual(browser_command(URL, situation), (None, why))

    def test_another_users_desktop_is_not_used(self):
        theirs = [Proc(300, 0, "sudo"), Proc(200, 1001, "zsh", DESKTOP)]
        self.assertEqual(browser_command(URL, linux(ancestors=theirs))[0], None)


class MacTests(unittest.TestCase):
    def mac(self, **changes):
        values = dict(platform="darwin", euid=0, user=User("pat", 501, "/Users/pat"),
                      ancestors=[Proc(300, 0, "sudo"), Proc(200, 501, "zsh"),
                                 Proc(100, 501, "Terminal")], console_uid=501)
        values.update(changes)
        return Situation(**values)

    def test_terminal_under_sudo(self):
        self.assertEqual(browser_command(URL, self.mac())[0],
                         ["launchctl", "asuser", "501", "sudo", "-u", "pat", "open", URL])
        self.assertEqual(browser_command(URL, self.mac(euid=501))[0], ["open", URL])

    def test_someone_else_at_the_screen(self):
        self.assertEqual(browser_command(URL, self.mac(console_uid=502))[0], None)
        self.assertEqual(browser_command(URL, self.mac(ancestors=[Proc(9, 0, "sshd")]))[1],
                         "started over SSH")


class UrlTests(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(ui_url("127.0.0.1", 8080), URL)
        self.assertEqual(ui_url("0.0.0.0", 9000), "http://127.0.0.1:9000/")
        self.assertEqual(ui_url("192.168.50.104", 8080), "http://192.168.50.104:8080/")
        self.assertEqual(ui_url("::1", 8080), "http://[::1]:8080/")


if __name__ == "__main__":
    unittest.main()

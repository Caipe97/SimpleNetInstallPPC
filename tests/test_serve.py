"""The whole server on loopback (high ports), driven through the web UI's API.

Binding sockets to an interface needs root on Linux (SO_BINDTODEVICE), so
there this runs only as root. On macOS it runs as any user.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import ipaddress
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from simplenetinstallppc.events import EventBus
from simplenetinstallppc.netif import list_interfaces
from simplenetinstallppc.server import ServeOptions, serve
from simplenetinstallppc.store import Store

from helpers import make_image

LOOPBACK = next((i.name for i in list_interfaces()
                 if any(str(a.ip) == "127.0.0.1" for a in i.addresses)), None)
CAN_BIND = sys.platform == "darwin" or (hasattr(os, "geteuid") and os.geteuid() == 0)


def free_port(kind=socket.SOCK_DGRAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(LOOPBACK and CAN_BIND, "needs a loopback interface it can bind to")
class ServeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        make_image(self.data / "images", 1, "Tiger Install", install=True)
        make_image(self.data / "images", 4, "Jaguar Install", protocol="NFS", install=True)
        (self.data / "ui-password").write_text("pw\n")
        self.ui_port = free_port(socket.SOCK_STREAM)
        self.events = EventBus()

    def start(self, **network):
        options = ServeOptions(
            interface=None, images_root=self.data / "images", state_dir=self.data,
            dhcp_port=free_port(), client_port=free_port(), tftp_port=free_port(),
            http_port=free_port(socket.SOCK_STREAM), bootparamd=False, nfs_mode="system",
            manage_roots=False, keep_awake=False, dhcp_probe=False, ui_port=self.ui_port,
            given=set(network), **network)
        self.loop = asyncio.new_event_loop()
        self.stop_event = None
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)

            async def main():
                self.stop_event = asyncio.Event()
                ready.set()
                await serve(options, self.events, self.stop_event)
            self.loop.run_until_complete(main())
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        ready.wait(5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if self.call("GET", "/api/status")[0] == 200:
                    return
            except OSError:
                time.sleep(0.05)
        self.fail("server didn't start")

    def tearDown(self):
        if getattr(self, "stop_event", None) is not None:
            self.loop.call_soon_threadsafe(self.stop_event.set)
            self.thread.join(10)
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.ui_port, timeout=30)
        headers = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
        if method != "GET":
            headers.update({"X-Netboot-UI": "1", "Content-Type": "application/json"})
        conn.request(method, path, body=json.dumps(body or {}) if method != "GET" else None,
                     headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data

    def lights(self):
        return {l["name"]: l["state"] for l in self.call("GET", "/api/status")[1]["lights"]}

    def test_starts_without_an_interface_and_is_set_up_from_the_ui(self):
        self.start()
        self.assertEqual(self.lights()["DHCP / BSDP"], "error")
        network = self.call("GET", "/api/network")[1]
        self.assertFalse(network["running"])
        self.assertIn("no network interface chosen", network["error"])
        self.assertIn(LOOPBACK, [i["name"] for i in network["interfaces"]])

        settings = {"interface": LOOPBACK, "dhcp_mode": "full", "pool_start": "127.0.0.50",
                    "pool_end": "127.0.0.59"}
        result = self.call("POST", "/api/network", {"settings": settings})[1]
        self.assertTrue(result["applied"], result)
        self.assertTrue(result["running"])
        lights = self.lights()
        self.assertEqual((lights["DHCP / BSDP"], lights["TFTP"], lights["HTTP"]), ("ok", "ok", "ok"))
        self.assertEqual(lights["NFS"], "warn")            # Jaguar is NFS, no --nfs-export
        self.assertEqual(lights["bootparamd"], "off")

        # Settings that fail the checks leave the running services alone.
        bad = dict(settings, pool_end="127.0.0.40")
        result = self.call("POST", "/api/network", {"settings": bad})[1]
        self.assertFalse(result["applied"])
        self.assertTrue(result["running"])
        self.assertEqual(result["settings"]["pool_end"], "127.0.0.59")

        # Back to BSDP-only, then saved for the next start.
        result = self.call("POST", "/api/network", {"settings": {"interface": LOOPBACK}})[1]
        self.assertTrue(result["applied"])
        status = self.call("GET", "/api/status")[1]
        self.assertIn(["DHCP", "BSDP only (another DHCP server hands out addresses)"],
                      status["summary"])
        store = Store.open(self.data)
        self.assertEqual(store.setting("network")["dhcp_mode"], "bsdp-only")
        store.close()

    def test_exits_when_neither_services_nor_ui_start(self):
        from simplenetinstallppc.server import StartupError
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
            busy.bind(("127.0.0.1", self.ui_port))
            busy.listen()
            options = ServeOptions(interface=None, images_root=self.data / "images",
                                   state_dir=self.data, keep_awake=False, dhcp_probe=False,
                                   ui_port=self.ui_port, given=set())
            with self.assertRaises(StartupError) as caught:
                asyncio.run(serve(options, self.events))
        self.assertIn("no network interface chosen", str(caught.exception))

    def test_saved_settings_are_used_and_the_command_line_wins(self):
        store = Store.open(self.data)
        store.set_setting("network", {"interface": LOOPBACK, "dhcp_mode": "full",
                                      "pool_start": "127.0.0.50", "pool_end": "127.0.0.59"})
        store.close()
        self.start(dhcp_mode="bsdp-only")
        network = self.call("GET", "/api/network")[1]
        self.assertTrue(network["running"])
        self.assertEqual((network["settings"]["interface"], network["settings"]["dhcp_mode"],
                          network["settings"]["pool_start"]), (LOOPBACK, "bsdp-only", "127.0.0.50"))
        self.assertEqual(network["command_line"], ["dhcp_mode"])


if __name__ == "__main__":
    unittest.main()

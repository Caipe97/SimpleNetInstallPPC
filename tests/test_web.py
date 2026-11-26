"""The web UI's API, over real HTTP on 127.0.0.1."""

from __future__ import annotations

import base64
import http.client
import json
import os
import plistlib
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from simplenetinstallppc.catalog import Catalog
from simplenetinstallppc.events import EventBus
from simplenetinstallppc.net.dhcp_probe import DhcpAnswer
from simplenetinstallppc.net.dhcp_server import ClientRecord
from simplenetinstallppc.store import Store
from simplenetinstallppc.web.app import (PASSWORD_FILE, WebContext, load_or_create_password,
                                    start_web_server)
from simplenetinstallppc.web.jobs import BuildJobs

from helpers import make_image

import ipaddress

PASSWORD = "s3cret"
MAC = "00:30:65:00:00:b2"


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


class WebTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        images = self.root / "images"
        make_image(images, 1, "Tiger Install", install=True, osVersion="10.4.6", IsDefault=True)
        make_image(images, 4, "Jaguar Install", protocol="NFS", install=True, osVersion="10.2")
        make_image(images, 7, "Intel only", archs=("i386",))
        self.catalog = Catalog(images)
        self.catalog.reload()
        self.events = EventBus()
        self.store = Store()
        self.live = {}
        self.reloads = 0

        def reload():
            self.reloads += 1
            self.catalog.reload()

        def probe():
            return [DhcpAnswer(server=ipaddress.IPv4Address("192.168.50.1"), source="192.168.50.1",
                               offered=ipaddress.IPv4Address("192.168.50.100"))]

        (self.root / "sources").mkdir()
        self.commands = []
        self.jobs = BuildJobs(self.root, self.events, on_success=reload,
                              command_for=self.fake_command)
        self.ctx = WebContext(events=self.events, catalog=self.catalog, data_root=self.root,
                              store=self.store, status=lambda: {"summary": [["Interface", "en0"]]},
                              reload=reload, probe=probe, live_clients=lambda: self.live,
                              jobs=self.jobs, cli_defaults={"ppc": 4})
        self.server = start_web_server("127.0.0.1", 0, self.ctx, PASSWORD)
        self.port = self.server.server_address[1]

    def fake_command(self, request, sources):
        self.commands.append((request, sources))
        script = "print('Copying disc 1'); print('Created images/x')"
        if request.name == "fail":
            script = "import sys; print('error: no HFS+ volume'); sys.exit(1)"
        return [sys.executable, "-c", script]

    def tearDown(self):
        self.server.stop()
        self.store.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, auth=(("admin", PASSWORD)), csrf=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if auth:
            token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        data = None
        if method != "GET":
            data = json.dumps(body or {}).encode()
            headers["Content-Type"] = "application/json"
            if csrf:
                headers["X-Netboot-UI"] = "1"
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw
        return response.status, payload

    def get(self, path):
        status, data = self.request("GET", path)
        self.assertEqual(status, 200, data)
        return data

    def post(self, path, body=None):
        status, data = self.request("POST", path, body)
        self.assertEqual(status, 200, data)
        return data

    def plist(self, index):
        image = self.catalog.by_index(index)
        with open(image.directory / "NBImageInfo.plist", "rb") as fh:
            return plistlib.load(fh)


class AuthTests(WebTestCase):
    def test_login_required(self):
        for auth in (None, ("admin", "wrong"), ("root", PASSWORD)):
            status, _ = self.request("GET", "/api/status", auth=auth)
            self.assertEqual(status, 401, auth)
        status, _ = self.request("GET", "/", auth=None)
        self.assertEqual(status, 303)          # to the login page
        self.assertEqual(self.request("GET", "/login.html", auth=None)[0], 200)
        self.assertEqual(self.request("GET", "/style.css", auth=None)[0], 200)

    def login(self, password):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/login", body=f"password={password}",
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        response = conn.getresponse()
        response.read()
        conn.close()
        return response

    def cookie_request(self, method, path, cookie, csrf=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Cookie": cookie}
        if csrf and method != "GET":
            headers["X-Netboot-UI"] = "1"
        conn.request(method, path, body=b"{}" if method != "GET" else None, headers=headers)
        response = conn.getresponse()
        response.read()
        conn.close()
        return response

    def test_login_form_sets_a_session_cookie(self):
        response = self.login(PASSWORD)
        self.assertEqual((response.status, response.getheader("Location")), (303, "/"))
        set_cookie = response.getheader("Set-Cookie")
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        cookie = set_cookie.split(";")[0]
        self.assertEqual(self.cookie_request("GET", "/api/status", cookie).status, 200)
        self.assertEqual(self.cookie_request("POST", "/api/reload", cookie, csrf=False).status, 403)
        logout = self.cookie_request("POST", "/logout", cookie)
        self.assertEqual(logout.status, 303)
        self.assertEqual(self.cookie_request("GET", "/api/status", cookie).status, 401)
        self.assertEqual(self.cookie_request("GET", "/api/status",
                                             f"{self.server.cookie}=made-up").status, 401)

    def test_two_servers_on_one_host_use_different_cookies(self):
        other = start_web_server("127.0.0.1", 0, self.ctx, "another password")
        try:
            self.assertNotEqual(other.cookie, self.server.cookie)
            self.assertTrue(other.cookie.startswith("snippc_session_"))
        finally:
            other.stop()

    def test_login_survives_a_restart_but_not_a_new_password(self):
        cookie = self.login(PASSWORD).getheader("Set-Cookie").split(";")[0]
        token = cookie.split("=", 1)[1]
        rows = self.store._db.execute("SELECT token_hash FROM sessions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn(token, rows[0][0])              # only a hash is stored
        self.server.stop()
        self.server = start_web_server("127.0.0.1", self.port, self.ctx, PASSWORD)
        self.assertEqual(self.cookie_request("GET", "/api/status", cookie).status, 200)
        self.server.stop()
        self.server = start_web_server("127.0.0.1", self.port, self.ctx, "new-password")
        self.assertEqual(self.cookie_request("GET", "/api/status", cookie).status, 401)

    def test_wrong_password(self):
        response = self.login("guess")
        self.assertEqual(response.getheader("Location"), "/login.html?failed")
        self.assertIsNone(response.getheader("Set-Cookie"))

    def test_changes_need_the_ui_header(self):
        status, data = self.request("POST", "/api/images/1", {"enabled": False}, csrf=False)
        self.assertEqual(status, 403)
        self.assertTrue(self.plist(1)["IsEnabled"])

    def test_no_cors_preflight(self):
        status, _ = self.request("OPTIONS", "/api/images/1")
        self.assertEqual(status, 405)

    def test_password_file_is_created_private_and_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            first, created = load_or_create_password(Path(folder))
            self.assertTrue(created)
            mode = stat.S_IMODE(os.stat(Path(folder) / PASSWORD_FILE).st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(load_or_create_password(Path(folder)), (first, False))


class PageTests(WebTestCase):
    def test_static_files(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"SimpleNetInstallPPC", body)
        self.assertEqual(self.request("GET", "/app.js")[0], 200)
        self.assertEqual(self.request("GET", "/../app.py")[0], 404)
        self.assertEqual(self.request("GET", "/api/nothing")[0], 404)

    def test_status(self):
        data = self.get("/api/status")
        self.assertEqual(data["summary"], [["Interface", "en0"]])
        self.events.emit("dhcp", "probe", "no DHCP server answered", level=30)
        self.assertEqual([w["message"] for w in self.get("/api/status")["warnings"]],
                         ["no DHCP server answered"])


class SetupTests(WebTestCase):
    def steps(self):
        data = self.get("/api/setup")
        return {step["id"]: step["done"] for step in data["steps"]}, data["done"]

    def test_steps_follow_the_server(self):
        steps, done = self.steps()
        self.assertEqual(steps, {"network": False, "image": True, "default": True, "boot": False})
        self.assertEqual(done, 2)
        self.ctx.status = lambda: {"summary": [], "running": True}
        self.ctx.network = lambda: {"settings": {"interface": "en7", "dhcp_mode": "full"}}
        self.store.update_client(MAC, last_seen=time.time())
        data = self.get("/api/setup")
        self.assertEqual(data["done"], 4)
        self.assertEqual(data["steps"][0]["state"], "Running on en7, full DHCP.")

    def test_no_images(self):
        for image in self.catalog.images:
            self.post(f"/api/images/{image.index}", {"enabled": False})
        steps, _ = self.steps()
        self.assertEqual((steps["image"], steps["default"]), (False, False))


class ImageTests(WebTestCase):
    def test_list(self):
        data = self.get("/api/images")
        self.assertEqual([i["index"] for i in data["images"]], [1, 4, 7])
        self.assertEqual(data["defaults"], {"ppc": 1, "i386": 7})
        self.assertEqual(data["cli_defaults"], {"ppc": 4})
        tiger = data["images"][0]
        self.assertEqual((tiger["os_version"], tiger["size"], tiger["problems"]), ("10.4.6", 4096, []))

    def test_disable_and_rename(self):
        data = self.post("/api/images/4", {"enabled": False, "name": "Jaguar (10.2)"})
        jaguar = data["images"][1]
        self.assertEqual((jaguar["enabled"], jaguar["name"]), (False, "Jaguar (10.2)"))
        self.assertEqual((self.plist(4)["IsEnabled"], self.plist(4)["Name"]), (False, "Jaguar (10.2)"))
        self.assertEqual(self.plist(4)["RootPath"], "root.dmg")      # other keys kept

    def test_bad_changes(self):
        for body in ({"enabled": "no"}, {"name": ""}, {"name": "x\n"}, {}, {"Index": 3}):
            status, _ = self.request("POST", "/api/images/4", body)
            self.assertEqual(status, 400, body)
        self.assertEqual(self.request("POST", "/api/images/99", {"enabled": True})[0], 404)

    def test_set_default(self):
        data = self.post("/api/defaults", {"arch": "ppc", "index": 4})
        self.assertEqual(data["defaults"]["ppc"], 4)
        self.assertEqual((self.plist(1)["IsDefault"], self.plist(4)["IsDefault"]), (False, True))
        self.assertEqual(self.catalog.default_for("ppc", None).index, 4)
        self.assertEqual(self.request("POST", "/api/defaults", {"arch": "i386", "index": 4})[0], 400)

    def test_reload(self):
        make_image(self.catalog.images_root, 9, "Panther")
        data = self.post("/api/reload")
        self.assertIn(9, [i["index"] for i in data["images"]])


class ClientTests(WebTestCase):
    def test_list_merges_store_live_records_and_activity(self):
        self.store.update_client(MAC, arch="ppc", model="PowerMac2,2", ip="192.168.50.50",
                                 last_seen=100.0, selected_image=4)
        self.store.update_client("00:05:02:00:00:a1", selected_image=1)
        self.live[MAC] = ClientRecord(MAC, "ppc", "PowerMac2,2", "192.168.50.50", 200.0, "INFORM", 4)
        self.events.emit("tftp", "done", "sent booter", client="192.168.50.50")
        clients = self.get("/api/clients")["clients"]
        self.assertEqual([c["mac"] for c in clients], [MAC, "00:05:02:00:00:a1"])
        self.assertEqual(clients[0]["last_seen"], 200.0)
        self.assertEqual(clients[0]["activity"]["message"], "sent booter")
        self.assertNotIn("activity", clients[1])

    def test_set_and_clear_saved_image(self):
        self.live[MAC] = ClientRecord(MAC, "ppc", selected_image=4)
        self.post(f"/api/clients/{MAC.upper()}", {"image": 1})
        self.assertEqual((self.store.selected_image(MAC), self.live[MAC].selected_image), (1, 1))
        self.post(f"/api/clients/{MAC}", {"image": None})
        self.assertIsNone(self.store.selected_image(MAC))
        self.assertEqual(self.request("POST", f"/api/clients/{MAC}", {"image": 99})[0], 404)
        self.assertEqual(self.request("POST", "/api/clients/00:11", {"image": 1})[0], 400)


class BuildTests(WebTestCase):
    def test_sources_listing(self):
        (self.root / "sources" / "Jaguar1.iso").write_bytes(b"x" * 10)
        (self.root / "sources" / "notes.txt").write_text("no")
        (self.root / "sources" / ".hidden.iso").write_text("no")
        self.assertEqual(self.get("/api/sources")["sources"], [{"name": "Jaguar1.iso", "size": 10}])

    def test_build_runs_and_reloads(self):
        (self.root / "sources" / "Panther.cdr").write_bytes(b"x")
        job = self.post("/api/builds", {"sources": ["Panther.cdr"], "name": "Panther",
                                        "index": 5, "protocol": "http"})
        self.assertTrue(wait_for(lambda: self.get(f"/api/builds/{job['id']}")["state"] != "running"))
        done = self.get(f"/api/builds/{job['id']}")
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["log"], ["Copying disc 1", "Created images/x"])
        self.assertEqual(self.reloads, 1)
        request, sources = self.commands[0]
        self.assertEqual((request.protocol, request.index), ("HTTP", 5))
        self.assertEqual(sources, [self.root / "sources" / "Panther.cdr"])
        self.assertEqual(len(self.get("/api/builds")["builds"]), 1)

    def test_failed_build(self):
        (self.root / "sources" / "bad.iso").write_bytes(b"x")
        job = self.post("/api/builds", {"sources": ["bad.iso"], "name": "fail"})
        self.assertTrue(wait_for(lambda: self.jobs.get(job["id"]).state != "running"))
        self.assertEqual(self.jobs.get(job["id"]).state, "failed")
        self.assertIn("build", [e.subsystem for e in self.events.recent() if e.level == 40])
        self.assertEqual(self.reloads, 0)

    def test_discs_from_any_folder(self):
        outside = self.root / "elsewhere" / "Jaguar Disc 1.iso"
        outside.parent.mkdir()
        outside.write_bytes(b"x")
        (self.root / "sources" / "ok.iso").write_bytes(b"x")
        (self.root / "notes.txt").write_text("no")
        for sources in ([], ["../ok.iso"], ["sub/ok.iso"], ["missing.iso"],
                        [str(self.root / "notes.txt")], [str(outside), str(outside)]):
            status, data = self.request("POST", "/api/builds", {"sources": sources})
            self.assertEqual(status, 400, sources)
        status, data = self.request("POST", "/api/builds", {"sources": ["ok.iso"], "dir": "../x"})
        self.assertEqual(status, 400)
        self.assertEqual(self.commands, [])
        job = self.post("/api/builds", {"sources": [str(outside), "ok.iso"]})
        self.assertTrue(wait_for(lambda: self.jobs.get(job["id"]).state != "running"))
        self.assertEqual(self.commands[0][1], [outside, self.root / "sources" / "ok.iso"])

    def test_real_command_line(self):
        jobs = BuildJobs(self.root, self.events)
        from simplenetinstallppc.web.jobs import BuildRequest
        command = jobs._command(BuildRequest(["a.iso"], name="Jaguar", index=4, dir_name="jag",
                                             protocol="NFS"), [Path("/s/a.iso"), Path("/s/b.iso")])
        self.assertEqual(command[1:], ["-m", "simplenetinstallppc", "--data", str(self.root),
                                       "make-test-image", "/s/a.iso", "/s/b.iso", "--protocol",
                                       "NFS", "--name", "Jaguar", "--index", "4", "--dir", "jag"])


class FileTests(WebTestCase):
    def upload(self, name, offset, total, data):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        token = base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
        conn.request("POST", f"/api/uploads?name={name}&offset={offset}&total={total}", body=data,
                     headers={"Authorization": f"Basic {token}", "X-Netboot-UI": "1",
                              "Content-Type": "application/octet-stream"})
        response = conn.getresponse()
        payload = json.loads(response.read())
        conn.close()
        return response.status, payload

    def test_browse(self):
        folder = self.root / "discs"
        (folder / "Jaguar").mkdir(parents=True)
        (folder / "Panther.cdr").write_bytes(b"x" * 3)
        (folder / "readme.txt").write_text("no")
        (folder / ".hidden.iso").write_text("no")
        data = self.get(f"/api/browse?path={folder}")
        self.assertEqual((data["path"], data["parent"], data["dirs"]), (str(folder), str(self.root),
                                                                         ["Jaguar"]))
        self.assertEqual(data["images"], [{"name": "Panther.cdr", "path": str(folder / "Panther.cdr"),
                                           "size": 3}])
        self.assertEqual(self.get("/api/browse")["path"], str(self.root / "sources"))
        self.assertEqual(self.request("GET", "/api/browse?path=relative")[0], 400)
        self.assertEqual(self.request("GET", f"/api/browse?path={folder}/nope")[0], 404)

    def test_chunked_upload_with_resume(self):
        body = bytes(range(256)) * 40          # 10240 bytes
        self.assertEqual(self.get("/api/uploads?name=Tiger.iso")["received"], 0)
        self.assertEqual(self.upload("Tiger.iso", 0, len(body), body[:4000])[0], 200)
        # A retry of the same chunk is refused with the offset to resume from.
        status, data = self.upload("Tiger.iso", 0, len(body), body[:4000])
        self.assertEqual(status, 409)
        self.assertIn("expected offset 4000", data["error"])
        self.assertEqual(self.get("/api/uploads?name=Tiger.iso")["received"], 4000)
        status, data = self.upload("Tiger.iso", 4000, len(body), body[4000:])
        self.assertEqual((status, data["done"]), (200, True))
        final = self.root / "sources" / "Tiger.iso"
        self.assertEqual(final.read_bytes(), body)
        self.assertFalse((self.root / "sources" / ".uploads").exists())
        self.assertEqual(self.get("/api/uploads?name=Tiger.iso")["done"], True)
        self.assertEqual(self.upload("Tiger.iso", 0, 3, b"abc")[0], 409)     # already there

    def test_bad_uploads(self):
        for name in ("../x.iso", "notes.txt", ".hidden.iso", "a/b.iso"):
            self.assertEqual(self.upload(name, 0, 3, b"abc")[0], 400, name)
        self.assertEqual(self.upload("x.iso", 2, 3, b"abc")[0], 400)         # past the end
        status, _ = self.upload("huge.iso", 0, 2**60, b"abc")
        self.assertEqual(status, 507)                                       # no room

    def test_cancel(self):
        self.upload("Panther.iso", 0, 10, b"12345")
        self.post("/api/uploads/cancel?name=Panther.iso")
        self.assertEqual(self.get("/api/uploads?name=Panther.iso")["received"], 0)


class NetworkApiTests(WebTestCase):
    def setUp(self):
        super().setUp()
        self.applied = []
        self.refuse = None

        def network():
            return {"settings": {"interface": "en7"}, "running": True, "error": None,
                    "command_line": [], "interfaces": [{"name": "en7"}]}

        def apply_network(settings, force):
            from simplenetinstallppc.netsettings import SettingsError
            if self.refuse and not force:
                raise SettingsError(self.refuse, probe_refusal=True)
            self.applied.append((settings, force))
        self.ctx.network = network
        self.ctx.apply_network = apply_network

    def test_apply(self):
        data = self.post("/api/network", {"settings": {
            "interface": "en7", "dhcp_mode": "full", "pool_start": "192.168.50.50",
            "pool_end": "192.168.50.59", "dns": "192.168.50.1, 8.8.8.8", "lease_time": 600}})
        self.assertTrue(data["applied"])
        settings, force = self.applied[0]
        self.assertEqual((settings.dhcp_mode, str(settings.pool_end), [str(d) for d in settings.dns],
                          settings.lease_time, force),
                         ("full", "192.168.50.59", ["192.168.50.1", "8.8.8.8"], 600, False))

    def test_bad_settings_and_probe_refusal(self):
        data = self.post("/api/network", {"settings": {"interface": "en7", "pool_start": "300.1.1.1"}})
        self.assertEqual((data["applied"], data["probe_refusal"]), (False, False))
        self.assertIn("not an IPv4 address", data["apply_error"])
        self.refuse = "another DHCP server is on this network"
        data = self.post("/api/network", {"settings": {"interface": "en7"}})
        self.assertEqual((data["applied"], data["probe_refusal"]), (False, True))
        self.assertTrue(self.post("/api/network", {"settings": {"interface": "en7"},
                                                   "force": True})["applied"])


class EventTests(WebTestCase):
    def test_events_since(self):
        first = self.events.emit("dhcp", "a", "one")
        self.events.emit("dhcp", "b", "two")
        data = self.get(f"/api/events?since={first.seq}")
        self.assertEqual([e["message"] for e in data["events"]], ["two"])

    def test_stream_sends_backlog_then_live_events(self):
        self.events.emit("server", "started", "backlog")
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        token = base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
        sock.sendall(f"GET /api/events/stream?since=0 HTTP/1.1\r\nHost: x\r\n"
                     f"Authorization: Basic {token}\r\n\r\n".encode())
        received = b""

        def read_until(text):
            nonlocal received
            while text not in received:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                received += chunk
            return text in received

        self.assertTrue(read_until(b'"backlog"'))
        self.assertIn(b"text/event-stream", received)
        threading.Timer(0.1, lambda: self.events.emit("tftp", "done", "live one")).start()
        self.assertTrue(read_until(b'"live one"'))
        sock.close()

    def test_probe(self):
        data = self.post("/api/probe")
        self.assertEqual(data["answers"][0]["server"], "192.168.50.1")
        self.assertEqual(data["answers"][0]["text"], "192.168.50.1 (offered 192.168.50.100)")
        self.assertIn("probe from the web UI: 192.168.50.1", self.events.recent()[-1].message)


if __name__ == "__main__":
    unittest.main()

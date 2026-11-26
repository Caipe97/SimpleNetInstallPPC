"""The web UI and its JSON API (PLAN §5), on the standard library's HTTP server.

The page in ``static/`` is plain HTML and JavaScript that talks only to the
API below, so anything the UI does can be scripted with curl:

    GET  /api/status                   server, network and service settings
    GET  /api/setup                    the Get started steps and which are done
    GET  /api/images                   images and the default per architecture
    POST /api/images/<index>           {"name", "description", "enabled"}
    POST /api/defaults                 {"arch": "ppc", "index": 4}
    POST /api/reload                   re-read the images folder
    GET  /api/clients                  Macs seen, their saved image and last activity
    POST /api/clients/<mac>            {"image": 4} or {"image": null}
    GET  /api/sources                  disc images in <data root>/sources
    GET  /api/uploads?name=a.iso       how much of an upload has arrived
    POST /api/uploads?name=&offset=&total=   one chunk (raw bytes) of an upload
    POST /api/uploads/cancel?name=     drop a partial upload
    GET  /api/builds[/<id>]            build jobs (with the log for one job)
    POST /api/builds                   {"sources": [...], "name", "index", "dir", "protocol"}
    POST /api/probe                    look for other DHCP servers now ({"interface"} optional)
    GET  /api/network                  interface and DHCP settings, and the interfaces
    POST /api/network                  apply new settings ({"settings": {...}, "force": bool})
    GET  /api/events?since=N           buffered events after sequence number N
    GET  /api/events/stream?since=N    the same, then live, as Server-Sent Events

Who may use it:

* **On localhost** (the default), there's no password: only this machine can
  connect, and other computers come in through an SSH tunnel, which already
  logged them in. Requests must name the server as ``127.0.0.1``,
  ``localhost`` or ``[::1]`` in their Host header, so a web page whose own
  name was pointed at 127.0.0.1 (DNS rebinding) is refused.
* **On any other address**, or with ``--ui-password``, every request needs the
  password in ``<data root>/ui-password``: the page logs in with a form
  (``POST /login``) that sets a session cookie, and scripts can send HTTP
  Basic auth instead (user ``admin``).

Either way, requests that change something need an ``X-Netboot-UI: 1``
header. A page on another site can't add that header without a CORS
preflight, which this server never approves, and the cookie is
SameSite=Strict, so another site can't make the browser change anything
(CSRF).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import pkgutil
import queue
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from .. import __version__
from ..catalog import Catalog, update_info
from ..events import Event, EventBus
from ..store import Store
from .files import FileError, Uploads
from .jobs import BuildError, BuildJobs, BuildRequest, list_sources

def static_file(name: str) -> Optional[bytes]:
    """A file of the page, read through the import system so it also works
    from the single-file simplenetinstallppc.pyz."""
    try:
        return pkgutil.get_data("simplenetinstallppc.web", f"static/{name}")
    except OSError:
        return None
PASSWORD_FILE = "ui-password"
USER = "admin"
CSRF_HEADER = "X-Netboot-UI"
MAX_BODY = 64 * 1024
ARCHES = ("ppc", "i386")
MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
COOKIE_PREFIX = "snippc_session_"
SESSION_SECONDS = 7 * 24 * 3600
PUBLIC = {"/login.html", "/login.js", "/style.css"}      # served before logging in


def load_or_create_password(data_root: Path) -> Tuple[str, bool]:
    """The UI password, created (random, root-only file) on first use."""
    path = data_root / PASSWORD_FILE
    if path.is_file():
        password = path.read_text().strip()
        if password:
            return password, False
    password = secrets.token_urlsafe(12)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(password + "\n")
    return password, True


@dataclass
class WebContext:
    """What the UI can see and change in the running server."""
    events: EventBus
    catalog: Catalog
    data_root: Path
    store: Optional[Store] = None
    status: Callable[[], Dict[str, Any]] = dict
    reload: Callable[[], None] = lambda: None              # blocking catalog reload
    probe: Optional[Callable[..., List[Any]]] = None       # (interface=None) -> answers
    live_clients: Callable[[], Dict[str, Any]] = dict      # mac -> ClientRecord
    jobs: Optional[BuildJobs] = None
    cli_defaults: Dict[str, int] = field(default_factory=dict)
    # Problems that depend on the server's settings (e.g. an NFS image without --nfs-export).
    image_problems: Callable[[Any], List[str]] = lambda image: []
    network: Optional[Callable[[], Dict[str, Any]]] = None          # settings + interfaces
    apply_network: Optional[Callable[[Any, bool], None]] = None    # (NetworkSettings, force)


def event_json(event: Event) -> Dict[str, Any]:
    return {"seq": event.seq, "time": event.timestamp, "level": event.level,
            "subsystem": event.subsystem, "kind": event.kind, "message": event.message,
            "client": event.client}


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Api:
    def __init__(self, ctx: WebContext) -> None:
        self.ctx = ctx
        self.uploads = Uploads(ctx.data_root / "sources")

    # -- get started ---------------------------------------------------------------
    def setup(self) -> Dict[str, Any]:
        """The first-run steps, each checked against the running server."""
        status = self.ctx.status()
        running = bool(status.get("running"))
        network = self.ctx.network() if self.ctx.network is not None else {}
        settings = network.get("settings") or {}
        enabled = [i for i in self.ctx.catalog.images if i.enabled]
        default = self.ctx.catalog.default_for("ppc", None)
        seen = []
        if self.ctx.store is not None:
            seen = [c for c in self.ctx.store.clients() if c.last_seen]
        seen_live = [r for r in self.ctx.live_clients().values() if r.last_seen]
        booted = seen or seen_live
        if running:
            mode = "full DHCP" if settings.get("dhcp_mode") == "full" else "BSDP only"
            net_state = f"Running on {settings.get('interface') or 'the chosen interface'}, {mode}."
        elif not settings.get("interface"):
            net_state = "No interface chosen yet."
        else:
            net_state = f"Not running: {network.get('error') or 'not started'}"
        steps = [
            {"id": "network", "title": "Connect to the Macs' network", "done": running,
             "state": net_state, "page": "network", "action": "Network settings"},
            {"id": "image", "title": "Add an install image", "done": bool(enabled),
             "state": (f"{len(enabled)} image(s) ready: " + ", ".join(i.name for i in enabled))
             if enabled else "No images yet.", "page": "build", "action": "Build an image"},
            {"id": "default", "title": "Choose the default image", "done": default is not None,
             "state": f"Macs boot {default.name!r} unless they pick another."
             if default else "Needs an image first.", "page": "images", "action": "Images"},
            {"id": "boot", "title": "Boot a Mac from the network", "done": bool(booted),
             "state": (f"{len(booted)} Mac(s) seen." if booted
                       else "Hold N while the Mac starts up."),
             "page": "clients", "action": "Clients"},
        ]
        return {"steps": steps, "done": sum(1 for step in steps if step["done"]),
                "total": len(steps)}

    # -- images --------------------------------------------------------------------
    def images(self) -> Dict[str, Any]:
        catalog = self.ctx.catalog
        defaults = {}
        for arch in ARCHES:
            image = catalog.default_for(arch, None)
            if image is not None:
                defaults[arch] = image.index
        rows = []
        for image in sorted(catalog.images, key=lambda i: i.index):
            root = image.directory / image.root_image
            try:
                size = root.stat().st_size
            except OSError:
                size = None
            rows.append({
                "index": image.index, "name": image.name, "description": image.description,
                "dir": image.rel_dir, "protocol": image.protocol,
                "architectures": list(image.architectures), "install": image.is_install,
                "enabled": image.enabled, "os_version": image.os_version, "size": size,
                "folder_root": bool(image.root_directory),
                "problems": image.problems() + self.ctx.image_problems(image),
            })
        return {"images": rows, "defaults": defaults, "cli_defaults": self.ctx.cli_defaults,
                "images_root": str(catalog.images_root)}

    def _image(self, index: int):
        image = self.ctx.catalog.by_index(index)
        if image is None:
            raise ApiError(404, f"no image with index {index}")
        return image

    def update_image(self, index: int, body: Dict[str, Any]) -> Dict[str, Any]:
        image = self._image(index)
        changes: Dict[str, object] = {}
        if "name" in body:
            name = body["name"]
            if not isinstance(name, str) or not name.strip() or len(name) > 64 \
                    or not name.isprintable():
                raise ApiError(400, "name: 1-64 printable characters")
            changes["Name"] = name.strip()
        if "description" in body:
            if not isinstance(body["description"], str) or len(body["description"]) > 500:
                raise ApiError(400, "description: up to 500 characters")
            changes["Description"] = body["description"]
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                raise ApiError(400, "enabled must be true or false")
            changes["IsEnabled"] = body["enabled"]
        if not changes:
            raise ApiError(400, "nothing to change")
        update_info(image.directory, changes)
        self.ctx.events.emit("web", "image", f"image {index}: changed "
                             + ", ".join(f"{k}={v!r}" for k, v in changes.items()))
        self.ctx.reload()
        return self.images()

    def set_default(self, body: Dict[str, Any]) -> Dict[str, Any]:
        arch, index = body.get("arch"), body.get("index")
        if arch not in ARCHES or not isinstance(index, int) or isinstance(index, bool):
            raise ApiError(400, 'expects {"arch": "ppc" or "i386", "index": N}')
        image = self._image(index)
        if arch not in image.architectures:
            raise ApiError(400, f"image {index} has no {arch} booter")
        # Saved as IsDefault in the plists, so it survives a restart. A --default
        # on the serve command line still wins at the next start.
        for other in self.ctx.catalog.images:
            if arch in other.architectures and other.is_default != (other.index == index):
                update_info(other.directory, {"IsDefault": other.index == index})
        self.ctx.catalog.default_index[arch] = index
        self.ctx.events.emit("web", "default", f"default for {arch}: {image.name!r}")
        self.ctx.reload()
        return self.images()

    # -- clients -------------------------------------------------------------------
    def clients(self) -> Dict[str, Any]:
        rows: Dict[str, Dict[str, Any]] = {}
        if self.ctx.store is not None:
            for row in self.ctx.store.clients():
                rows[row.mac] = {"mac": row.mac, "arch": row.arch, "model": row.model,
                                 "ip": row.ip, "first_seen": row.first_seen,
                                 "last_seen": row.last_seen, "last_message": row.last_message,
                                 "selected_image": row.selected_image}
        for mac, record in self.ctx.live_clients().items():
            entry = rows.setdefault(mac, {"mac": mac, "first_seen": None})
            entry.update(arch=record.arch, model=record.model, ip=record.ip,
                         last_seen=record.last_seen or entry.get("last_seen"),
                         last_message=record.last_message,
                         selected_image=record.selected_image
                         if self.ctx.store is None else entry.get("selected_image"))
        # Last activity: events name a Mac by MAC (DHCP/BSDP) or IP (TFTP/HTTP/NFS).
        by_ip = {r["ip"]: mac for mac, r in rows.items() if r.get("ip")}
        for event in self.ctx.events.recent(5000):
            mac = event.client if event.client in rows else by_ip.get(event.client or "")
            if mac:
                rows[mac]["activity"] = {"time": event.timestamp, "message": event.message,
                                         "subsystem": event.subsystem}
        ordered = sorted(rows.values(), key=lambda r: -(r.get("last_seen") or 0))
        return {"clients": ordered}

    def set_client_image(self, mac: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if not MAC.match(mac):
            raise ApiError(400, f"not an Ethernet address: {mac!r}")
        if self.ctx.store is None:
            raise ApiError(409, "no data folder, so saved images can't be changed")
        index = body.get("image")
        if index is not None:
            if not isinstance(index, int) or isinstance(index, bool):
                raise ApiError(400, '"image" must be an index or null')
            image = self._image(index)
        self.ctx.store.set_selected_image(mac, index)
        live = self.ctx.live_clients().get(mac)
        if live is not None:
            live.selected_image = index
        self.ctx.events.emit("web", "set-image", "saved image cleared: boots the default"
                             if index is None else f"saved image set to {image.name!r}",
                             client=mac)
        return self.clients()

    # -- builds --------------------------------------------------------------------
    def _jobs(self) -> BuildJobs:
        if self.ctx.jobs is None:
            raise ApiError(409, "builds need a data folder")
        return self.ctx.jobs

    def sources(self) -> Dict[str, Any]:
        folder = self.ctx.data_root / "sources"
        return {"folder": str(folder), "exists": folder.is_dir(),
                "sources": list_sources(folder)}

    def start_build(self, body: Dict[str, Any]) -> Dict[str, Any]:
        sources = body.get("sources")
        if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
            raise ApiError(400, '"sources" must be a list of file names')
        index = body.get("index")
        if index in ("", None):
            index = None
        elif not isinstance(index, int) or isinstance(index, bool):
            raise ApiError(400, '"index" must be a number')
        request = BuildRequest(sources=sources, name=(body.get("name") or None),
                               index=index, dir_name=(body.get("dir") or None),
                               protocol=str(body.get("protocol") or "HTTP").upper(),
                               force=bool(body.get("force", False)))
        try:
            job = self._jobs().start(request)
        except BuildError as exc:
            raise ApiError(400, str(exc)) from exc
        return job.summary()

    def build(self, job_id: int) -> Dict[str, Any]:
        job = self._jobs().get(job_id)
        if job is None:
            raise ApiError(404, f"no build {job_id}")
        return job.summary(with_log=True)

    # -- network -------------------------------------------------------------------
    def network(self) -> Dict[str, Any]:
        if self.ctx.network is None:
            raise ApiError(409, "network settings can't be changed here")
        return self.ctx.network()

    def apply_network(self, body: Dict[str, Any]) -> Dict[str, Any]:
        from ..netsettings import NetworkSettings, SettingsError
        if self.ctx.apply_network is None:
            raise ApiError(409, "network settings can't be changed here")
        try:
            settings = NetworkSettings.from_json(body.get("settings") or {})
            self.ctx.apply_network(settings, bool(body.get("force", False)))
        except SettingsError as exc:
            result = self.network()
            result.update(apply_error=str(exc), probe_refusal=exc.probe_refusal, applied=False)
            return result
        result = self.network()
        result["applied"] = True
        return result

    def probe(self, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from ..netsettings import SettingsError
        if self.ctx.probe is None:
            raise ApiError(409, "the probe isn't available here")
        interface = (body or {}).get("interface") or None
        try:
            answers = self.ctx.probe(interface) if interface else self.ctx.probe()
        except (SettingsError, LookupError) as exc:
            raise ApiError(400, str(exc)) from exc
        self.ctx.events.emit("dhcp", "probe", "probe from the web UI: " + (
            "; ".join(a.describe() for a in answers) if answers
            else "no other DHCP server answered"))
        return {"answers": [{"server": str(a.server), "offered": str(a.offered or ""),
                             "router": str(a.router or ""), "dns": [str(d) for d in a.dns],
                             "text": a.describe()} for a in answers]}


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], ctx: WebContext,
                 password: Optional[str]) -> None:
        self.ctx = ctx
        self.api = Api(ctx)
        self.password = password          # None: no login (localhost only)
        self.stopping = threading.Event()
        # Changes with every start. The event stream sends it first: event
        # numbers restart at 1 with a new server, so a page that sees a new
        # instance reloads instead of waiting for numbers it already passed.
        self.instance = secrets.token_hex(8)
        # Logins live in the store when there is one, so they survive a restart;
        # otherwise in memory. Either way only a hash of each token is kept.
        self._sessions: Dict[str, float] = {}      # token hash -> expiry
        self._sessions_lock = threading.Lock()
        super().__init__(address, WebHandler)

    @property
    def cookie(self) -> str:
        """Cookie name for this server. Browsers share cookies between ports, so two
        servers reached through tunnels on one host would log each other out with a
        fixed name; deriving it from the password keeps them apart."""
        return COOKIE_PREFIX + hmac.new(self.password.encode(), b"cookie name",
                                        hashlib.sha256).hexdigest()[:10]

    @property
    def login_required(self) -> bool:
        return self.password is not None

    def password_ok(self, password: str) -> bool:
        if self.password is None:
            return False
        return hmac.compare_digest(password.encode(), self.password.encode())

    def _hash(self, token: str) -> str:
        # Keyed with the password: changing ui-password logs everyone out.
        return hmac.new(self.password.encode(), token.encode(), hashlib.sha256).hexdigest()

    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        expires = time.time() + SESSION_SECONDS
        store = self.ctx.store
        if store is not None:
            store.add_session(self._hash(token), expires)
        else:
            with self._sessions_lock:
                now = time.time()
                self._sessions = {t: e for t, e in self._sessions.items() if e > now}
                self._sessions[self._hash(token)] = expires
        return token

    def session_ok(self, token: str) -> bool:
        store = self.ctx.store
        if store is not None:
            expiry = store.session_expires(self._hash(token))
        else:
            with self._sessions_lock:
                expiry = self._sessions.get(self._hash(token))
        return expiry is not None and expiry > time.time()

    def end_session(self, token: str) -> None:
        store = self.ctx.store
        if store is not None:
            store.delete_session(self._hash(token))
        else:
            with self._sessions_lock:
                self._sessions.pop(self._hash(token), None)

    def stop(self) -> None:
        self.stopping.set()
        self.shutdown()
        self.server_close()


Route = Tuple[str, "re.Pattern[str]", Callable[..., Any]]


class WebHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SimpleNetInstallPPC"
    server: WebServer

    # -- plumbing ------------------------------------------------------------------
    def _cookie(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == self.server.cookie:
                return value
        return ""

    def _authorized(self) -> bool:
        if not self.server.login_required:
            return True
        token = self._cookie()
        if token and self.server.session_ok(token):
            return True
        header = self.headers.get("Authorization", "")
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            user, _, password = base64.b64decode(encoded).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(user.encode(), USER.encode()) and self.server.password_ok(password)

    def _redirect(self, location: str, cookie: Optional[str] = None) -> None:
        extra = {"Location": location}
        if cookie is not None:
            extra["Set-Cookie"] = cookie
        self._send(303, b"", "text/plain", extra)

    def _login(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(min(length, MAX_BODY)).decode("utf-8", "replace"))
        if self.server.password_ok(form.get("password", [""])[0]):
            token = self.server.new_session()
            self.server.ctx.events.emit("web", "login", f"logged in from {self.client_address[0]}")
            self._redirect("/", f"{self.server.cookie}={token}; Path=/; HttpOnly; SameSite=Strict; "
                                f"Max-Age={SESSION_SECONDS}")
            return
        self.server.ctx.events.emit("web", "login-failed",
                                    f"wrong password from {self.client_address[0]}", level=30)
        time.sleep(1)    # slow down guessing
        self._redirect("/login.html?failed")

    def _logout(self) -> None:
        self.server.end_session(self._cookie())
        self._redirect("/login.html", f"{self.server.cookie}=; Path=/; HttpOnly; SameSite=Strict; "
                                      "Max-Age=0")

    def _send(self, status: int, body: bytes, content_type: str,
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "request too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError as exc:
            raise ApiError(400, f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "expects a JSON object")
        return data

    def _query(self, name: str, default: str = "") -> str:
        return parse_qs(urlsplit(self.path).query).get(name, [default])[0]

    def _int_query(self, name: str) -> int:
        try:
            return int(self._query(name))
        except ValueError as exc:
            raise ApiError(400, f"{name} must be a number") from exc

    def _upload_chunk(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            result = self.server.api.uploads.write(
                self._query("name"), self._int_query("offset"), self._int_query("total"),
                length, self.rfile)
        except FileError:
            self.close_connection = True      # the unread body can't be reused
            raise
        if result["done"]:
            self.server.ctx.events.emit("web", "upload", f"uploaded {result['path']}")
        return result

    def _routes(self) -> List[Route]:
        api = self.server.api
        return [
            ("GET", re.compile(r"/api/uploads"),
             lambda: api.uploads.status(self._query("name"))),
            ("POST", re.compile(r"/api/uploads"), self._upload_chunk),
            ("POST", re.compile(r"/api/uploads/cancel"),
             lambda: (api.uploads.cancel(self._query("name")), {"ok": True})[1]),
            ("GET", re.compile(r"/api/status"), lambda: self._status()),
            ("GET", re.compile(r"/api/setup"), api.setup),
            ("GET", re.compile(r"/api/images"), api.images),
            ("POST", re.compile(r"/api/images/(\d+)"),
             lambda i: api.update_image(int(i), self._body())),
            ("POST", re.compile(r"/api/defaults"), lambda: api.set_default(self._body())),
            ("POST", re.compile(r"/api/reload"), self._reload),
            ("GET", re.compile(r"/api/clients"), api.clients),
            ("POST", re.compile(r"/api/clients/([0-9a-fA-F:]+)"),
             lambda mac: api.set_client_image(mac.lower(), self._body())),
            ("GET", re.compile(r"/api/sources"), api.sources),
            ("GET", re.compile(r"/api/builds"), lambda: {"builds": api._jobs().list()}),
            ("GET", re.compile(r"/api/builds/(\d+)"), lambda i: api.build(int(i))),
            ("POST", re.compile(r"/api/builds"), lambda: api.start_build(self._body())),
            ("POST", re.compile(r"/api/probe"), lambda: api.probe(self._body())),
            ("GET", re.compile(r"/api/network"), api.network),
            ("POST", re.compile(r"/api/network"), lambda: api.apply_network(self._body())),
            ("GET", re.compile(r"/api/events"), self._events),
        ]

    def _status(self) -> Dict[str, Any]:
        status = dict(self.server.ctx.status())
        status["version"] = __version__
        status["warnings"] = [event_json(e) for e in self.server.ctx.events.recent(5000)
                              if e.level >= 30][-20:]
        return status

    def _reload(self) -> Dict[str, Any]:
        self.server.ctx.reload()
        return self.server.api.images()

    def _events(self) -> Dict[str, Any]:
        since = self._since()
        return {"events": [event_json(e) for e in self.server.ctx.events.since(since)]}

    def _since(self) -> int:
        values = parse_qs(urlsplit(self.path).query).get("since", ["0"])
        try:
            return max(int(values[0]), 0)
        except ValueError:
            return 0

    def _dispatch(self, method: str) -> None:
        path = urlsplit(self.path).path
        if not self.server.login_required:
            if not host_is_local(self.headers.get("Host")):
                self._error(403, "this server only answers as 127.0.0.1 or localhost")
                return
            if path in ("/login", "/login.html", "/logout"):
                self._redirect("/")              # nothing to log in to
                return
        if method == "GET" and path == "/api/session":
            self._json(200, {"login": self.server.login_required})
            return
        if method == "POST" and path == "/login":
            self._login()
            return
        if method == "GET" and path in PUBLIC:
            self._static(path)
            return
        if not self._authorized():
            if path.startswith("/api/"):
                self._error(401, "login required")
            elif method == "GET":
                self._redirect("/login.html")
            else:
                self._error(401, "login required")
            return
        if method == "POST" and path == "/logout":
            self._logout()
            return
        if method == "GET" and path == "/api/events/stream":
            self._stream()
            return
        if not path.startswith("/api/"):
            if method == "GET":
                self._static(path)
            else:
                self._error(405, "method not allowed")
            return
        if method != "GET" and self.headers.get(CSRF_HEADER) != "1":
            self._error(403, f"missing {CSRF_HEADER} header")
            return
        for verb, pattern, handler in self._routes():
            match = pattern.fullmatch(path)
            if match and verb == method:
                try:
                    self._json(200, handler(*match.groups()))
                except (ApiError, FileError) as exc:
                    self._error(exc.status, str(exc))
                except (OSError, ValueError) as exc:
                    self._error(500, str(exc))
                return
        self._error(404, f"no such endpoint: {method} {path}")

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        if "/" in name or name.startswith("."):
            self._error(404, "not found")
            return
        data = static_file(name)
        if data is None:
            self._error(404, "not found")
            return
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"
        self._send(200, data, content_type)

    def _stream(self) -> None:
        events = self.server.ctx.events
        pending: "queue.Queue[Event]" = queue.Queue(maxsize=10000)

        def push(event: Event) -> None:
            try:
                pending.put_nowait(event)
            except queue.Full:
                pass
        unsubscribe = events.subscribe(push)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(f"event: hello\ndata: {self.server.instance}\n\n".encode())
            self.wfile.flush()
            last = self._since()
            for event in events.since(last):
                self._sse(event)
                last = event.seq
            idle = 0.0
            while not self.server.stopping.is_set():
                try:
                    event = pending.get(timeout=1.0)
                except queue.Empty:
                    idle += 1.0
                    if idle >= 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        idle = 0.0
                    continue
                if event.seq > last:
                    self._sse(event)
                    last = event.seq
                    idle = 0.0
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            unsubscribe()

    def _sse(self, event: Event) -> None:
        self.wfile.write(f"id: {event.seq}\ndata: {json.dumps(event_json(event))}\n\n".encode())
        self.wfile.flush()

    # -- verbs ---------------------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:   # never approve a CORS preflight
        self._error(405, "method not allowed")

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass   # the services log what matters; UI polling would flood the log


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def host_is_local(header: Optional[str]) -> bool:
    """Is a Host header this machine by a loopback name? (Missing: a script.)"""
    if header is None:
        return True
    host = header.strip().lower()
    if host.startswith("["):                       # [::1]:8080
        host = host[1:host.find("]")] if "]" in host else host
    elif host.count(":") == 1:                     # 127.0.0.1:8080
        host = host.split(":")[0]
    return host.rstrip(".") in LOCAL_HOSTS


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def start_web_server(bind: str, port: int, ctx: WebContext,
                     password: Optional[str]) -> WebServer:
    """Start the UI. password None: no login, which is only allowed on loopback."""
    if password is None and not is_loopback(bind):
        raise ValueError(f"the web UI needs a password on {bind}")
    server = WebServer((bind, port), ctx, password)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                     name="web-ui", daemon=True).start()
    return server

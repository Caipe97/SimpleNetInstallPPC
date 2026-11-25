"""HTTP server for root disk images (10.3+ clients can mount them over HTTP).

The client's DiskImages driver reads the image in many byte-range requests,
so this server supports single ``Range`` requests, keep-alive and HEAD, and
uses ``sendfile``. It never lists directories.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional, Tuple, Union
from urllib.parse import unquote, urlsplit

from ..events import EventBus
from ..netif import bind_to_interface

REPORT_EVERY = 64 * 1024 * 1024   # log a progress line per client every 64 MiB

RangeResult = Union[None, str, Tuple[int, int]]


def parse_range(header: str, size: int) -> RangeResult:
    """Parse a Range header.

    Returns (start, end) inclusive, ``"full"`` to ignore the header and send
    the whole file, or None if the range can't be satisfied (416).
    """
    header = header.strip()
    if not header.startswith("bytes=") or "," in header:
        return "full"   # multi-range / unknown unit: RFC 7233 lets us send 200
    spec = header[len("bytes="):].strip()
    start_text, sep, end_text = spec.partition("-")
    if not sep:
        return "full"
    try:
        if start_text == "":
            suffix = int(end_text)
            if suffix <= 0:
                return None
            return (max(size - suffix, 0), size - 1) if size else None
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    except ValueError:
        return "full"
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)


class ImageHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], root: Path, events: EventBus,
                 interface: Optional[str] = None) -> None:
        self.root = root.resolve()
        self.events = events
        self._served: Dict[str, int] = {}
        self._lock = threading.Lock()
        super().__init__(address, ImageRequestHandler, bind_and_activate=False)
        try:
            if interface:
                bind_to_interface(self.socket, interface)
            self.server_bind()
            self.server_activate()
        except Exception:
            self.server_close()
            raise

    def account(self, client: str, path: str, count: int) -> None:
        with self._lock:
            before = self._served.get(client, 0)
            after = before + count
            self._served[client] = after
        if after // REPORT_EVERY > before // REPORT_EVERY:
            self.events.emit("http", "progress",
                             f"{after // (1024 * 1024)} MiB of {path} served",
                             client=client, path=path, bytes=after)


class ImageRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "netboot-server"
    # TCP_NODELAY. Each response is two writes (headers, then sendfile), and
    # with Nagle on the last short segment of every reply waits for an ACK that
    # old Mac OS X clients delay. That stalls each of the thousands of small
    # range reads a NetBoot client makes.
    disable_nagle_algorithm = True
    server: ImageHTTPServer

    def do_HEAD(self) -> None:
        self._serve(send_body=False)

    def do_GET(self) -> None:
        self._serve(send_body=True)

    def _resolve(self) -> Optional[Path]:
        rel = unquote(urlsplit(self.path).path).lstrip("/")
        if not rel:
            return None
        candidate = (self.server.root / rel).resolve()
        try:
            candidate.relative_to(self.server.root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def _serve(self, send_body: bool) -> None:
        client = self.client_address[0]
        path = self._resolve()
        if path is None:
            self.server.events.emit("http", "not-found", f"{self.command} {self.path}: not found",
                                    client=client, level=30)
            self.send_error(404)
            return
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range")
        if range_header:
            parsed = parse_range(range_header, size)
            if parsed is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed != "full":
                start, end = parsed  # type: ignore[misc]
                status = 206
        length = max(end - start + 1, 0)
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Last-Modified", self.date_time_string(int(path.stat().st_mtime)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not send_body or length == 0:
            return
        with open(path, "rb") as fh:
            self.connection.sendfile(fh, start, length)
        self.server.account(client, self.path, length)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        self.server.events.emit("http", "request", format % args,
                                client=self.client_address[0], level=10)


def start_http_server(bind_ip: str, port: int, root: Path, events: EventBus,
                      interface: Optional[str] = None) -> ImageHTTPServer:
    server = ImageHTTPServer((bind_ip, port), root, events, interface)
    thread = threading.Thread(target=server.serve_forever, name="http-images", daemon=True)
    thread.start()
    return server


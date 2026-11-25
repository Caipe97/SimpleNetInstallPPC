import asyncio
import http.client
import os
import socket
import struct
import tempfile
import unittest
from pathlib import Path

from netboot_server.events import EventBus
from netboot_server.net.http_server import ImageRequestHandler, parse_range, start_http_server
from netboot_server.net.tftp import (OP_ACK, OP_DATA, OP_ERROR, OP_OACK, OP_RRQ, TftpConfig,
                                     TftpServer, resolve_path)


def rrq(name: str, **options) -> bytes:
    body = name.encode() + b"\0octet\0"
    for key, value in options.items():
        body += key.encode() + b"\0" + str(value).encode() + b"\0"
    return struct.pack("!H", OP_RRQ) + body


class _Client(asyncio.DatagramProtocol):
    def __init__(self):
        self.queue = asyncio.Queue()

    def datagram_received(self, data, addr):
        self.queue.put_nowait((data, addr))


class TftpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "img1" / "ppc").mkdir(parents=True)
        self.payload = os.urandom(512 * 3)          # exact multiple: needs an empty last block
        (self.root / "img1" / "ppc" / "booter").write_bytes(self.payload)
        (self.root / "odd.bin").write_bytes(os.urandom(5000))
        outside = Path(self.tmp.name + "-outside")
        outside.write_bytes(b"secret")
        self.addCleanup(outside.unlink)
        (self.root / "escape").symlink_to(outside)

    def tearDown(self):
        self.tmp.cleanup()

    def run_client(self, coro_factory, **config):
        async def main():
            loop = asyncio.get_running_loop()
            server_config = TftpConfig(root=self.root, bind_ip="127.0.0.1", port=0,
                                       timeout=0.3, retries=2, **config)
            transport, _ = await loop.create_datagram_endpoint(
                lambda: TftpServer(server_config, EventBus()), local_addr=("127.0.0.1", 0))
            port = transport.get_extra_info("sockname")[1]
            client_transport, client = await loop.create_datagram_endpoint(
                _Client, local_addr=("127.0.0.1", 0))
            try:
                return await asyncio.wait_for(coro_factory(client_transport, client, port), 10)
            finally:
                client_transport.close()
                transport.close()
        return asyncio.run(main())

    @staticmethod
    async def download(transport, client, port, name, ack_duplicates=False, **options):
        transport.sendto(rrq(name, **options), ("127.0.0.1", port))
        received = bytearray()
        blksize = int(options.get("blksize", 512))
        oack = None
        while True:
            data, addr = await client.queue.get()
            opcode, number = struct.unpack_from("!HH", data)
            if opcode == OP_ERROR:
                return None, number, oack
            if opcode == OP_OACK:
                parts = data[2:].split(b"\0")
                oack = {parts[i].decode(): parts[i + 1].decode() for i in range(0, len(parts) - 1, 2)}
                blksize = int(oack.get("blksize", 512))
                transport.sendto(struct.pack("!HH", OP_ACK, 0), addr)
                continue
            assert opcode == OP_DATA
            received += data[4:]
            transport.sendto(struct.pack("!HH", OP_ACK, number), addr)
            if ack_duplicates:
                transport.sendto(struct.pack("!HH", OP_ACK, number), addr)
            if len(data) - 4 < blksize:
                return bytes(received), None, oack

    def test_plain_transfer_exact_multiple(self):
        data, error, _ = self.run_client(
            lambda t, c, p: self.download(t, c, p, "img1/ppc/booter"))
        self.assertEqual(data, self.payload)

    def test_options_and_backslashes(self):
        data, _, oack = self.run_client(
            lambda t, c, p: self.download(t, c, p, "\\img1\\ppc\\booter", blksize=1024, tsize=0))
        self.assertEqual(data, self.payload)
        self.assertEqual(oack, {"blksize": "1024", "tsize": str(len(self.payload))})

    def test_blksize_is_capped(self):
        data, _, oack = self.run_client(
            lambda t, c, p: self.download(t, c, p, "odd.bin", blksize=9000), max_blksize=1468)
        self.assertEqual(oack["blksize"], "1468")
        self.assertEqual(data, (self.root / "odd.bin").read_bytes())

    def test_duplicate_acks_do_not_corrupt(self):
        data, _, _ = self.run_client(
            lambda t, c, p: self.download(t, c, p, "odd.bin", ack_duplicates=True))
        self.assertEqual(data, (self.root / "odd.bin").read_bytes())

    def test_missing_and_escaping_paths(self):
        for name in ("nope", "../" + Path(self.tmp.name).name + "-outside", "escape"):
            data, error, _ = self.run_client(lambda t, c, p, n=name: self.download(t, c, p, n))
            self.assertIsNone(data, name)
            self.assertEqual(error, 1, name)

    def test_resolve_path(self):
        root = self.root.resolve()
        self.assertEqual(resolve_path(root, "/img1/ppc/booter"), root / "img1/ppc/booter")
        self.assertIsNone(resolve_path(root, "../etc/passwd"))
        self.assertIsNone(resolve_path(root, ""))


class HttpTests(unittest.TestCase):
    def test_parse_range(self):
        self.assertEqual(parse_range("bytes=0-99", 1000), (0, 99))
        self.assertEqual(parse_range("bytes=900-", 1000), (900, 999))
        self.assertEqual(parse_range("bytes=-100", 1000), (900, 999))
        self.assertEqual(parse_range("bytes=990-2000", 1000), (990, 999))
        self.assertIsNone(parse_range("bytes=1000-", 1000))
        self.assertEqual(parse_range("bytes=0-1,5-6", 1000), "full")
        self.assertEqual(parse_range("items=0-1", 1000), "full")

    def test_server_ranges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "img1").mkdir()
            payload = os.urandom(100_000)
            (root / "img1" / "root.dmg").write_bytes(payload)
            server = start_http_server("127.0.0.1", 0, root, EventBus())
            port = server.server_address[1]
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/img1/root.dmg", headers={"Range": "bytes=10-19"})
                response = conn.getresponse()
                self.assertEqual(response.status, 206)
                self.assertEqual(response.getheader("Content-Range"), "bytes 10-19/100000")
                self.assertEqual(response.read(), payload[10:20])
                # Same keep-alive connection, whole file.
                conn.request("GET", "/img1/root.dmg")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), payload)
                conn.request("HEAD", "/img1/root.dmg")
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.getheader("Content-Length"), "100000")
                conn.request("GET", "/img1/root.dmg", headers={"Range": "bytes=200000-"})
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 416)
                conn.close()
                for bad in ("/img1/", "/../etc/passwd", "/"):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request("GET", bad)
                    response = conn.getresponse()
                    response.read()
                    self.assertEqual(response.status, 404, bad)
                    conn.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_responses_are_not_delayed_by_nagle(self):
        seen = []

        class Recording(ImageRequestHandler):
            def setup(self):
                super().setup()
                seen.append(self.connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "root.dmg").write_bytes(b"x" * 10)
            server = start_http_server("127.0.0.1", 0, root, EventBus())
            server.RequestHandlerClass = Recording
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                conn.request("GET", "/root.dmg")
                conn.getresponse().read()
                conn.close()
            finally:
                server.shutdown()
                server.server_close()
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0])


if __name__ == "__main__":
    unittest.main()

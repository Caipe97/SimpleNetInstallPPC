"""Read-only TFTP server (RFC 1350, options RFC 2347/2348/2349).

Serves booters, kernels and mkexts from the images root. Open Firmware and
EFI TFTP clients are fragile, so we:

* accept backslashes and leading slashes in file names,
* support the blksize / tsize / timeout options and answer plain 512-byte
  requests,
* ignore duplicate ACKs (Sorcerer's Apprentice bug) and duplicate RRQs,
* never serve anything outside the root, even through symlinks.
"""

from __future__ import annotations

import asyncio
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from ..events import EventBus
from ..netif import make_udp_socket

OP_RRQ, OP_WRQ, OP_DATA, OP_ACK, OP_ERROR, OP_OACK = range(1, 7)
ERR_UNDEFINED, ERR_NOT_FOUND, ERR_ACCESS, ERR_DISK_FULL, ERR_ILLEGAL_OP, ERR_UNKNOWN_TID = range(6)
ERR_OPTION = 8

Address = Tuple[str, int]


@dataclass
class TftpConfig:
    root: Path
    bind_ip: str = "0.0.0.0"
    port: int = 69
    interface: Optional[str] = None
    max_blksize: int = 1468        # fits a 1500-byte Ethernet MTU without fragmenting
    timeout: float = 2.0
    retries: int = 5


class ClientAborted(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"client sent error {code}: {message}")
        self.code = code


def parse_request(data: bytes) -> Tuple[str, str, Dict[str, str]]:
    parts = data[2:].split(b"\0")
    if len(parts) < 3:
        raise ValueError("malformed request")
    filename = parts[0].decode("latin-1")
    mode = parts[1].decode("latin-1").lower()
    options: Dict[str, str] = {}
    fields = parts[2:]
    for i in range(0, len(fields) - 1, 2):
        if fields[i]:
            options[fields[i].decode("latin-1").lower()] = fields[i + 1].decode("latin-1")
    return filename, mode, options


def resolve_path(root: Path, filename: str) -> Optional[Path]:
    """Map a requested name to a file under ``root`` (already resolved), or None."""
    name = filename.replace("\\", "/").lstrip("/")
    if not name:
        return None
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def error_packet(code: int, message: str) -> bytes:
    return struct.pack("!HH", OP_ERROR, code) + message.encode("ascii", "replace") + b"\0"


class _TransferProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: "asyncio.Queue[Tuple[bytes, Address]]") -> None:
        self.queue = queue

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        self.queue.put_nowait((data, addr))


class TftpServer(asyncio.DatagramProtocol):
    def __init__(self, config: TftpConfig, events: EventBus) -> None:
        self.config = config
        self.root = config.root.resolve()
        self.events = events
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.active: Dict[Address, asyncio.Task] = {}

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        if len(data) < 2:
            return
        opcode = struct.unpack_from("!H", data)[0]
        assert self.transport is not None
        if opcode == OP_WRQ:
            self.transport.sendto(error_packet(ERR_ACCESS, "read-only server"), addr)
            return
        if opcode != OP_RRQ:
            return
        if addr in self.active:
            return  # retransmitted RRQ; the transfer is already running
        try:
            filename, mode, options = parse_request(data)
        except ValueError:
            self.transport.sendto(error_packet(ERR_ILLEGAL_OP, "malformed request"), addr)
            return
        task = asyncio.ensure_future(self._serve(addr, filename, mode, options))
        self.active[addr] = task
        task.add_done_callback(lambda _t, a=addr: self.active.pop(a, None))

    async def _serve(self, addr: Address, filename: str, mode: str,
                     options: Dict[str, str]) -> None:
        loop = asyncio.get_running_loop()
        client = addr[0]
        sock = make_udp_socket(self.config.bind_ip, 0, self.config.interface)
        queue: "asyncio.Queue[Tuple[bytes, Address]]" = asyncio.Queue()
        transport, _ = await loop.create_datagram_endpoint(lambda: _TransferProtocol(queue),
                                                           sock=sock)
        try:
            path = resolve_path(self.root, filename)
            if path is None or not path.is_file():
                transport.sendto(error_packet(ERR_NOT_FOUND, "file not found"), addr)
                self.events.emit("tftp", "not-found", f"RRQ {filename!r}: not found",
                                 client=client, level=30)
                return
            await self._send_file(transport, queue, addr, path, filename, mode, options)
        except ClientAborted as exc:
            # Firmware often sends an error after the OACK when it only
            # wanted the file size (tsize). That's normal, so log at debug.
            level = 10 if exc.code == ERR_OPTION else 30
            self.events.emit("tftp", "aborted", f"{filename!r}: {exc}", client=client, level=level)
        except OSError as exc:
            transport.sendto(error_packet(ERR_UNDEFINED, "read error"), addr)
            self.events.emit("tftp", "error", f"{filename!r}: {exc}", client=client, level=40)
        finally:
            transport.close()

    async def _send_file(self, transport, queue, addr: Address, path: Path, filename: str,
                         mode: str, options: Dict[str, str]) -> None:
        client = addr[0]
        size = path.stat().st_size
        blksize, timeout = 512, self.config.timeout
        accepted: Dict[str, str] = {}
        if "blksize" in options:
            try:
                requested = int(options["blksize"])
            except ValueError:
                requested = 0
            if requested >= 8:
                blksize = min(requested, self.config.max_blksize, 65464)
                accepted["blksize"] = str(blksize)
        if "tsize" in options:
            accepted["tsize"] = str(size)
        if "timeout" in options:
            try:
                value = int(options["timeout"])
            except ValueError:
                value = 0
            if 1 <= value <= 255:
                timeout = float(value)
                accepted["timeout"] = str(value)

        self.events.emit("tftp", "start",
                         f"RRQ {filename!r} ({size} bytes, mode {mode}, blksize {blksize})",
                         client=client, path=filename, size=size, blksize=blksize)
        started = time.monotonic()
        with open(path, "rb") as fh:
            if accepted:
                oack = struct.pack("!H", OP_OACK) + b"".join(
                    k.encode() + b"\0" + v.encode() + b"\0" for k, v in accepted.items())
                if not await self._send_and_wait(transport, queue, addr, oack, 0, timeout):
                    self._timed_out(client, filename, 0)
                    return
            block = 1
            sent = 0
            while True:
                chunk = fh.read(blksize)
                packet = struct.pack("!HH", OP_DATA, block & 0xFFFF) + chunk
                if not await self._send_and_wait(transport, queue, addr, packet,
                                                 block & 0xFFFF, timeout):
                    self._timed_out(client, filename, sent)
                    return
                sent += len(chunk)
                if len(chunk) < blksize:
                    break
                block += 1
        elapsed = max(time.monotonic() - started, 1e-6)
        self.events.emit("tftp", "done",
                         f"sent {filename!r}: {sent} bytes in {elapsed:.1f}s "
                         f"({sent / elapsed / 1024:.0f} KiB/s)",
                         client=client, path=filename, bytes=sent, seconds=elapsed)

    def _timed_out(self, client: str, filename: str, sent: int) -> None:
        self.events.emit("tftp", "timeout",
                         f"{filename!r}: client stopped acknowledging after {sent} bytes",
                         client=client, level=30)

    async def _send_and_wait(self, transport, queue, addr: Address, packet: bytes,
                             expected_block: int, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        for _attempt in range(self.config.retries + 1):
            transport.sendto(packet, addr)
            deadline = loop.time() + timeout
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    data, source = await asyncio.wait_for(queue.get(), remaining)
                except asyncio.TimeoutError:
                    break
                if source != addr:
                    transport.sendto(error_packet(ERR_UNKNOWN_TID, "unknown transfer ID"), source)
                    continue
                if len(data) < 4:
                    continue
                opcode, number = struct.unpack_from("!HH", data)
                if opcode == OP_ERROR:
                    raise ClientAborted(number, data[4:].split(b"\0", 1)[0].decode("latin-1"))
                if opcode == OP_ACK and number == expected_block:
                    return True
                # A duplicate ACK for an earlier block is ignored. Resending on
                # it would double the traffic (Sorcerer's Apprentice bug).
        return False

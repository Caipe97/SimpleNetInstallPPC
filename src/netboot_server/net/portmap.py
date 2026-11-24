"""Getting RPC calls to us when the host's rpcbind can't help.

Mac OS X 10.0/10.1 clients find bootparamd with a *broadcast* portmapper
CALLIT to UDP port 111. Then they look up MOUNT v1 and NFS v2 with GETPORT
calls to the bootparam server's port 111. There are three host situations:

1. rpcbind runs and forwards CALLIT (macOS): we just register with it.
2. No rpcbind runs (a Linux host without kernel NFS):
   :class:`Portmapper` runs a minimal portmapper v2 on UDP 111 itself. It
   answers CALLIT for our in-process programs directly.
3. rpcbind runs but forwarding is disabled (Linux rpcbind 1.2.6+ by
   default): :class:`BroadcastListener` catches the broadcast with an
   AF_PACKET socket and answers it from an ordinary UDP socket. The 10.0
   kernel accepts the first reply with a matching xid from any source port.
   GETPORT still goes to the real rpcbind, where our programs are registered.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import struct
from typing import Callable, Dict, Optional, Tuple

from ..events import EventBus
from . import rpc

# prog -> (vers, port, handler(proc, args) -> results or None)
LocalProgram = Tuple[int, int, Callable[[int, rpc.Unpacker], Optional[bytes]]]


def callit_reply(xid: int, port: int, results: bytes) -> bytes:
    return rpc.accepted_reply(xid, rpc.pack_u32(port) + rpc.pack_opaque(results))


def handle_callit(call: rpc.Call, programs: Dict[int, LocalProgram]) -> Optional[bytes]:
    """Answer a CALLIT for one of our programs; None means stay silent (as rpcbind does)."""
    try:
        prog, vers, proc = call.args.u32(), call.args.u32(), call.args.u32()
        args = call.args.opaque()
    except rpc.XdrError:
        return None
    entry = programs.get(prog)
    if entry is None or entry[0] != vers:
        return None
    _vers, port, handler = entry
    try:
        results = handler(proc, rpc.Unpacker(args))
    except rpc.XdrError:
        return None
    return None if results is None else callit_reply(call.xid, port, results)


class Portmapper(asyncio.DatagramProtocol):
    """Minimal portmapper v2 (NULL, SET, UNSET, GETPORT, DUMP, CALLIT) on UDP."""

    def __init__(self, events: EventBus) -> None:
        self.events = events
        self.mappings: Dict[Tuple[int, int, int], int] = {}   # (prog, vers, prot) -> port
        self.programs: Dict[int, LocalProgram] = {}
        self.transport: Optional[asyncio.DatagramTransport] = None

    def register_local(self, prog: int, vers: int, port: int,
                       handler: Optional[Callable[[int, rpc.Unpacker], Optional[bytes]]] = None) -> None:
        self.mappings[(prog, vers, rpc.IPPROTO_UDP)] = port
        if handler is not None:
            self.programs[prog] = (vers, port, handler)

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        reply = self.handle(data, addr[0])
        if reply is not None and self.transport is not None:
            self.transport.sendto(reply, addr)

    def handle(self, data: bytes, source: str) -> Optional[bytes]:
        call = rpc.parse_call(data)
        if call is None or call.prog != rpc.PMAP_PROG:
            return None if call is None else rpc.accepted_reply(call.xid, status=rpc.PROG_UNAVAIL)
        if call.vers != rpc.PMAP_VERS:
            return rpc.accepted_reply(call.xid, rpc.pack_u32(2, 2), status=rpc.PROG_MISMATCH)
        u = call.args
        try:
            if call.proc == rpc.PMAPPROC_NULL:
                return rpc.accepted_reply(call.xid)
            if call.proc in (rpc.PMAPPROC_SET, rpc.PMAPPROC_UNSET):
                prog, vers, prot, port = u.u32(), u.u32(), u.u32(), u.u32()
                if not source.startswith("127."):
                    return rpc.accepted_reply(call.xid, rpc.pack_u32(0))
                if call.proc == rpc.PMAPPROC_SET:
                    self.mappings[(prog, vers, prot)] = port
                else:
                    for key in [k for k in self.mappings if k[:2] == (prog, vers)]:
                        del self.mappings[key]
                return rpc.accepted_reply(call.xid, rpc.pack_u32(1))
            if call.proc == rpc.PMAPPROC_GETPORT:
                prog, vers, prot, _port = u.u32(), u.u32(), u.u32(), u.u32()
                return rpc.accepted_reply(call.xid, rpc.pack_u32(self.mappings.get((prog, vers, prot), 0)))
            if call.proc == 4:   # DUMP
                out = b""
                for (prog, vers, prot), port in sorted(self.mappings.items()):
                    out += rpc.pack_u32(1, prog, vers, prot, port)
                return rpc.accepted_reply(call.xid, out + rpc.pack_u32(0))
            if call.proc == rpc.PMAPPROC_CALLIT:
                return handle_callit(call, self.programs)
        except rpc.XdrError:
            return rpc.accepted_reply(call.xid, status=rpc.GARBAGE_ARGS)
        return rpc.accepted_reply(call.xid, status=rpc.PROC_UNAVAIL)


# -- Linux: catch broadcast CALLITs that rpcbind won't forward ---------------------------
ETH_P_IP = 0x0800


def parse_ipv4_udp(packet: bytes) -> Optional[Tuple[str, int, str, int, bytes]]:
    """(src, sport, dst, dport, payload) of an IPv4/UDP packet, else None."""
    if len(packet) < 28 or packet[0] >> 4 != 4 or packet[9] != 17:
        return None
    header = (packet[0] & 0x0F) * 4
    if (struct.unpack_from(">H", packet, 6)[0] & 0x3FFF) != 0:
        return None   # fragmented; a CALLIT is tiny and never is
    src = socket.inet_ntoa(packet[12:16])
    dst = socket.inet_ntoa(packet[16:20])
    sport, dport, length = struct.unpack_from(">HHH", packet, header)
    payload = packet[header + 8:header + length]
    return src, sport, dst, dport, payload


class BroadcastListener:
    """Answers broadcast CALLITs for our programs on one interface (Linux, root)."""

    def __init__(self, interface: str, network: ipaddress.IPv4Network, server_ip: str,
                 programs: Dict[int, LocalProgram], events: EventBus) -> None:
        self.interface = interface
        self.broadcasts = {"255.255.255.255", str(network.broadcast_address)}
        self.programs = programs
        self.events = events
        self.raw = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM,   # type: ignore[attr-defined]
                                 socket.htons(ETH_P_IP))
        self.raw.bind((interface, ETH_P_IP))
        self.raw.setblocking(False)
        self.reply = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.reply.bind((server_ip, 0))
        self.reply.setblocking(False)

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        loop.add_reader(self.raw.fileno(), self._readable)

    def close(self, loop: asyncio.AbstractEventLoop) -> None:
        loop.remove_reader(self.raw.fileno())
        self.raw.close()
        self.reply.close()

    def _readable(self) -> None:
        try:
            packet = self.raw.recv(65535)
        except OSError:
            return
        reply = self.answer(packet)
        if reply is not None:
            data, destination = reply
            try:
                self.reply.sendto(data, destination)
            except OSError as exc:
                self.events.emit("bootparam", "reply-failed", f"broadcast reply failed: {exc}",
                                 level=40)

    def answer(self, packet: bytes) -> Optional[Tuple[bytes, Tuple[str, int]]]:
        return answer_broadcast(packet, self.broadcasts, self.programs)


def answer_broadcast(packet: bytes, broadcasts, programs: Dict[int, LocalProgram]
                     ) -> Optional[Tuple[bytes, Tuple[str, int]]]:
    """Reply and destination for a broadcast CALLIT to port 111 in an IPv4 packet."""
    parsed = parse_ipv4_udp(packet)
    if parsed is None:
        return None
    src, sport, dst, dport, payload = parsed
    if dport != rpc.PMAP_PORT or dst not in broadcasts:
        return None
    call = rpc.parse_call(payload)
    if (call is None or call.prog != rpc.PMAP_PROG or call.vers != rpc.PMAP_VERS
            or call.proc != rpc.PMAPPROC_CALLIT):
        return None
    reply = handle_callit(call, programs)
    return None if reply is None else (reply, (src, sport))

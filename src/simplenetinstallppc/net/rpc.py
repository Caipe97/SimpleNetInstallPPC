"""Minimal ONC RPC v2 over UDP (RFC 5531) and portmapper v2 (RFC 1833) client.

Just enough to run bootparamd. Only AUTH_NULL replies are produced, because
the Mac OS X 10.0 kernel (xnu-123.5 krpc_call) reads the accept status at a
fixed offset that assumes an empty verifier.
"""

from __future__ import annotations

import os
import socket
import struct
from dataclasses import dataclass
from typing import Optional, Tuple

CALL, REPLY = 0, 1
MSG_ACCEPTED = 0
SUCCESS, PROG_UNAVAIL, PROG_MISMATCH, PROC_UNAVAIL, GARBAGE_ARGS = 0, 1, 2, 3, 4

PMAP_PROG, PMAP_VERS, PMAP_PORT = 100000, 2, 111
PMAPPROC_NULL, PMAPPROC_SET, PMAPPROC_UNSET, PMAPPROC_GETPORT, PMAPPROC_CALLIT = 0, 1, 2, 3, 5
IPPROTO_UDP = 17


class XdrError(ValueError):
    pass


class Unpacker:
    def __init__(self, data: bytes, offset: int = 0) -> None:
        self.data = data
        self.offset = offset

    def u32(self) -> int:
        if self.offset + 4 > len(self.data):
            raise XdrError("truncated")
        value = struct.unpack_from(">I", self.data, self.offset)[0]
        self.offset += 4
        return value

    def opaque(self, limit: int = 65536) -> bytes:
        length = self.u32()
        if length > limit or self.offset + length > len(self.data):
            raise XdrError("bad opaque length")
        value = self.data[self.offset:self.offset + length]
        self.offset += (length + 3) & ~3
        return value

    def string(self, limit: int = 1024) -> str:
        return self.opaque(limit).decode("latin-1")

    def rest(self) -> bytes:
        return self.data[self.offset:]


def pack_u32(*values: int) -> bytes:
    return struct.pack(f">{len(values)}I", *values)


def pack_opaque(data: bytes) -> bytes:
    return pack_u32(len(data)) + data + b"\0" * (-len(data) % 4)


def pack_string(text: str) -> bytes:
    return pack_opaque(text.encode("latin-1"))


@dataclass
class Call:
    xid: int
    prog: int
    vers: int
    proc: int
    args: Unpacker


def parse_call(data: bytes) -> Optional[Call]:
    """Parse an RPC CALL header; None if the packet is not an RPC v2 call."""
    try:
        u = Unpacker(data)
        xid, direction, rpcvers = u.u32(), u.u32(), u.u32()
        if direction != CALL or rpcvers != 2:
            return None
        prog, vers, proc = u.u32(), u.u32(), u.u32()
        u.u32(); u.opaque(400)   # credentials
        u.u32(); u.opaque(400)   # verifier
        return Call(xid, prog, vers, proc, u)
    except XdrError:
        return None


def accepted_reply(xid: int, results: bytes = b"", status: int = SUCCESS) -> bytes:
    return pack_u32(xid, REPLY, MSG_ACCEPTED, 0, 0, status) + results


def build_call(xid: int, prog: int, vers: int, proc: int, args: bytes = b"") -> bytes:
    return pack_u32(xid, CALL, 2, prog, vers, proc, 0, 0, 0, 0) + args


def parse_reply(data: bytes, xid: int) -> Optional[Unpacker]:
    """Return an unpacker positioned at the results of a successful reply."""
    try:
        u = Unpacker(data)
        if u.u32() != xid or u.u32() != REPLY or u.u32() != MSG_ACCEPTED:
            return None
        u.u32(); u.opaque(400)   # verifier
        if u.u32() != SUCCESS:
            return None
        return u
    except XdrError:
        return None


def _reserved_socket() -> socket.socket:
    """UDP socket on a reserved port (rpcbind only accepts SET/UNSET from those)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for port in range(1023, 600, -1):
        try:
            sock.bind(("127.0.0.1", port))
            return sock
        except PermissionError:
            break
        except OSError:
            continue
    sock.bind(("127.0.0.1", 0))
    return sock


def pmap_call(proc: int, args: bytes, timeout: float = 1.0,
              host: str = "127.0.0.1") -> Optional[Unpacker]:
    sock = _reserved_socket()
    try:
        sock.settimeout(timeout)
        xid = int.from_bytes(os.urandom(4), "big")
        sock.sendto(build_call(xid, PMAP_PROG, PMAP_VERS, proc, args), (host, PMAP_PORT))
        while True:
            data, _ = sock.recvfrom(65536)
            result = parse_reply(data, xid)
            if result is not None:
                return result
    except (socket.timeout, OSError):
        return None
    finally:
        sock.close()


def pmap_set(prog: int, vers: int, port: int) -> bool:
    pmap_unset(prog, vers)
    result = pmap_call(PMAPPROC_SET, pack_u32(prog, vers, IPPROTO_UDP, port))
    try:
        return result is not None and result.u32() == 1
    except XdrError:
        return False


def pmap_unset(prog: int, vers: int) -> None:
    pmap_call(PMAPPROC_UNSET, pack_u32(prog, vers, IPPROTO_UDP, 0))


def pmap_callit(prog: int, vers: int, proc: int, args: bytes = b"",
                timeout: float = 1.0) -> Optional[Tuple[int, bytes]]:
    """Ask the local portmapper to forward a call. Returns (port, result) or None."""
    result = pmap_call(PMAPPROC_CALLIT, pack_u32(prog, vers, proc) + pack_opaque(args), timeout)
    if result is None:
        return None
    try:
        return result.u32(), result.opaque()
    except XdrError:
        return None

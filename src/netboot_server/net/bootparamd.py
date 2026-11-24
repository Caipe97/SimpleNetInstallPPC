"""bootparamd (RPC program 100026) for Mac OS X 10.0 diskless clients.

The 10.0 kernel's ``nfs_boot`` (xnu-123.5), when no ``rp=`` boot argument
is set:

1. ``WHOAMI(client ip)``, sent as a broadcast portmapper CALLIT to port 111.
   It accepts the first reply with the right xid from any host and port, and
   uses the reply's source address as the bootparam server. Our answer
   arrives via the host's rpcbind, which forwards CALLIT to the port we
   registered.
2. ``GETFILE(name, "root")``: server name, server address and the NFS path of
   the root directory, mounted with MOUNT v1 / NFS v2 over UDP.
3. ``GETFILE(name, "private")``: an unanswered or failed call retries
   forever, and a failed mount panics. But if the private path is exactly
   ``<root>/private`` on the same server, the kernel skips that mount, so
   that is the answer we send.

Addresses use the kernel's encoding: atype 1, then each of the 4 IP bytes
in its own 32-bit word. Names are XDR strings.
"""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Callable, Dict, Optional, Tuple

from ..catalog import NetBootImage
from ..events import EventBus
from . import rpc

BOOTPARAM_PROG, BOOTPARAM_VERS = 100026, 1
PROC_NULL, PROC_WHOAMI, PROC_GETFILE = 0, 1, 2
IP_ADDR_TYPE = 1
MAX_HOSTNAME = 255        # kernel: cn_len >= MAXHOSTNAMELEN (256) is "bad reply"
MAX_SERVER_NAME = 89      # kernel: sn_len >= MNAMELEN (90)
MAX_PATH = 1023           # kernel: path_len >= MAXPATHLEN (1024)

IPv4 = ipaddress.IPv4Address
# Given a client's IP: (MAC, image whose RootDirectory it should boot), or None.
ClientLookup = Callable[[IPv4], Optional[Tuple[str, NetBootImage]]]


def pack_inaddr(ip: IPv4) -> bytes:
    return rpc.pack_u32(IP_ADDR_TYPE, *ip.packed)


def unpack_inaddr(u: rpc.Unpacker) -> IPv4:
    if u.u32() != IP_ADDR_TYPE:
        raise rpc.XdrError("unsupported address type")
    return IPv4(bytes(u.u32() & 0xFF for _ in range(4)))


def client_hostname(mac: str) -> str:
    return "nb-" + mac.replace(":", "")[-6:]


class BootparamServer(asyncio.DatagramProtocol):
    def __init__(self, server_ip: IPv4, lookup: ClientLookup, events: EventBus,
                 server_name: str = "netboot", router: Optional[IPv4] = None) -> None:
        self.server_ip = server_ip
        self.lookup = lookup
        self.events = events
        self.server_name = server_name[:MAX_SERVER_NAME]
        self.router = router or IPv4(0)
        self.names: Dict[str, IPv4] = {}   # hostname we gave out -> client IP
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        reply = self.handle(data, addr[0])
        if reply is not None and self.transport is not None:
            self.transport.sendto(reply, addr)

    # -- request handling (no I/O, for tests) ---------------------------------
    def handle(self, data: bytes, source: str) -> Optional[bytes]:
        call = rpc.parse_call(data)
        if call is None:
            return None
        if call.prog != BOOTPARAM_PROG:
            return rpc.accepted_reply(call.xid, status=rpc.PROG_UNAVAIL)
        if call.vers != BOOTPARAM_VERS:
            return rpc.accepted_reply(call.xid, rpc.pack_u32(BOOTPARAM_VERS, BOOTPARAM_VERS),
                                      status=rpc.PROG_MISMATCH)
        try:
            if call.proc == PROC_NULL:
                return rpc.accepted_reply(call.xid)
            if call.proc == PROC_WHOAMI:
                results = self._whoami(unpack_inaddr(call.args))
            elif call.proc == PROC_GETFILE:
                results = self._getfile(call.args.string(MAX_HOSTNAME), call.args.string(64))
            else:
                return rpc.accepted_reply(call.xid, status=rpc.PROC_UNAVAIL)
        except rpc.XdrError:
            return rpc.accepted_reply(call.xid, status=rpc.GARBAGE_ARGS)
        # Like Sun's bootparamd, stay silent for clients we don't serve, so
        # another server can answer.
        return None if results is None else rpc.accepted_reply(call.xid, results)

    def _whoami(self, client_ip: IPv4) -> Optional[bytes]:
        found = self.lookup(client_ip)
        if found is None:
            self.events.emit("bootparam", "whoami-unknown",
                             f"WHOAMI from {client_ip}: neither its selected image nor the default has a "
                             "RootDirectory key (the mounted, NFS-exported root volume)",
                             client=str(client_ip), level=30)
            return None
        mac, image = found
        name = client_hostname(mac)
        self.names[name] = client_ip
        self.events.emit("bootparam", "whoami",
                         f"WHOAMI {client_ip}: hostname {name}, image {image.name!r}",
                         client=mac, hostname=name, image=image.index)
        return rpc.pack_string(name) + rpc.pack_string("") + pack_inaddr(self.router)

    def _getfile(self, name: str, key: str) -> Optional[bytes]:
        client_ip = self.names.get(name)
        found = self.lookup(client_ip) if client_ip is not None else None
        if found is None:
            self.events.emit("bootparam", "getfile-unknown",
                             f"GETFILE {key!r} for unknown client {name!r}", level=30)
            return None
        mac, image = found
        root = (image.root_directory or "").rstrip("/")
        if key == "root":
            path = root
        elif key == "private":
            path = root + "/private"   # makes the kernel skip a separate private mount
        else:
            self.events.emit("bootparam", "getfile-key", f"GETFILE unknown key {key!r}",
                             client=mac, level=30)
            return None
        if len(path.encode("latin-1", "replace")) > MAX_PATH:
            self.events.emit("bootparam", "getfile-long", f"path too long: {path}",
                             client=mac, level=40)
            return None
        self.events.emit("bootparam", "getfile",
                         f"GETFILE {key!r}: {self.server_ip}:{path}", client=mac, key=key,
                         path=path)
        return (rpc.pack_string(self.server_name) + pack_inaddr(self.server_ip)
                + rpc.pack_string(path))


def forwarding_works(timeout: float = 1.0) -> Optional[bool]:
    """Check that the host's rpcbind forwards CALLIT to our registered service.

    Sends a NULL call through 127.0.0.1:111. True means 10.0 clients can
    reach us. None means the portmapper didn't answer at all.
    """
    result = rpc.pmap_callit(BOOTPARAM_PROG, BOOTPARAM_VERS, PROC_NULL, timeout=timeout)
    return result is not None

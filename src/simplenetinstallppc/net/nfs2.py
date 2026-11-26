"""Read-only NFS v2 (RFC 1094) and MOUNT v1 server over UDP.

Why we have our own: Mac OS X 10.0/10.1 mount their root with MOUNT v1 and
NFS v2 over UDP (xnu-123.5 ``md_mount`` / ``krpc_portmap``). On Linux the
kernel NFS server can't export hfsplus mounts, and recent kernels are often
built without NFS v2 at all. This server reads the mounted HFS+ volume
through the host's filesystem, so case-insensitive lookups behave exactly as
on a Mac.

Both programs share one UDP socket and are registered with the portmapper.
Only the folders given to :meth:`Nfs2Server.set_exports` can be mounted.
Lookups never leave an export (``..`` at its root stays there), and symlinks
are returned to the client as links, never followed on the server. Every
write operation answers NFSERR_ROFS.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import stat
from typing import Dict, Iterable, List, Optional, Tuple

from ..events import EventBus
from . import rpc

NFS_PROG, NFS_VERS = 100003, 2
MOUNT_PROG, MOUNT_VERS = 100005, 1
FHSIZE = 32
MAXDATA = 8192
MAXNAMLEN = 255

NFS_OK, NFSERR_PERM, NFSERR_NOENT, NFSERR_IO = 0, 1, 2, 5
NFSERR_ACCES, NFSERR_NOTDIR, NFSERR_ISDIR, NFSERR_INVAL = 13, 20, 21, 22
NFSERR_ROFS, NFSERR_NAMETOOLONG, NFSERR_STALE = 30, 63, 70
NFNON, NFREG, NFDIR, NFBLK, NFCHR, NFLNK = range(6)

# NFS v2 procedures
(P_NULL, P_GETATTR, P_SETATTR, P_ROOT, P_LOOKUP, P_READLINK, P_READ, P_WRITECACHE,
 P_WRITE, P_CREATE, P_REMOVE, P_RENAME, P_LINK, P_SYMLINK, P_MKDIR, P_RMDIR,
 P_READDIR, P_STATFS) = range(18)
WRITE_PROCS = {P_SETATTR, P_WRITE, P_CREATE, P_REMOVE, P_RENAME, P_LINK, P_SYMLINK,
               P_MKDIR, P_RMDIR}
# MOUNT v1 procedures
M_NULL, M_MNT, M_DUMP, M_UMNT, M_UMNTALL, M_EXPORT = range(6)

U32 = 0xFFFFFFFF


def _u32(value: int) -> int:
    return max(0, min(int(value), U32))


def fattr(st: os.stat_result) -> bytes:
    mode = st.st_mode
    if stat.S_ISREG(mode):
        ftype = NFREG
    elif stat.S_ISDIR(mode):
        ftype = NFDIR
    elif stat.S_ISLNK(mode):
        ftype = NFLNK
    elif stat.S_ISBLK(mode):
        ftype = NFBLK
    elif stat.S_ISCHR(mode):
        ftype = NFCHR
    else:
        ftype = NFNON

    def ts(value: float) -> Tuple[int, int]:
        seconds = int(value)
        return _u32(seconds), int((value - seconds) * 1_000_000)

    return rpc.pack_u32(
        ftype, mode & U32, _u32(st.st_nlink), _u32(st.st_uid), _u32(st.st_gid),
        _u32(st.st_size), 512, _u32(getattr(st, "st_rdev", 0)),
        _u32(getattr(st, "st_blocks", (st.st_size + 511) // 512)), 1, st.st_ino & U32,
        *ts(st.st_atime), *ts(st.st_mtime), *ts(st.st_ctime))


class Nfs2Server(asyncio.DatagramProtocol):
    def __init__(self, events: EventBus,
                 allowed: Optional[ipaddress.IPv4Network] = None) -> None:
        self.events = events
        self.allowed = allowed
        self._salt = os.urandom(8)              # handles go stale when we restart
        self._exports: Dict[bytes, bytes] = {}  # export path -> its real path
        self._handles: Dict[bytes, Tuple[bytes, bytes]] = {}  # fh -> (export root, path)
        self._dir_cache: Dict[bytes, List[bytes]] = {}
        self.transport: Optional[asyncio.DatagramTransport] = None

    def set_exports(self, paths: Iterable[str]) -> None:
        self._exports = {os.fsencode(p): os.fsencode(os.path.realpath(p)) for p in paths}
        self._dir_cache.clear()

    @property
    def exports(self) -> List[str]:
        return [os.fsdecode(p) for p in self._exports]

    # -- asyncio --------------------------------------------------------------------
    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        reply = self.handle(data, addr[0])
        if reply is not None and self.transport is not None:
            self.transport.sendto(reply, addr)

    # -- handles ------------------------------------------------------------------------
    def _handle_for(self, root: bytes, path: bytes) -> bytes:
        fh = hashlib.sha256(self._salt + root + b"\0" + path).digest()[:FHSIZE]
        self._handles[fh] = (root, path)
        return fh

    def _resolve(self, u: rpc.Unpacker) -> Tuple[bytes, bytes]:
        fh = u.data[u.offset:u.offset + FHSIZE]
        if len(fh) != FHSIZE:
            raise rpc.XdrError("short file handle")
        u.offset += FHSIZE
        found = self._handles.get(fh)
        if found is None:
            raise LookupError("stale")
        return found

    # -- dispatch -------------------------------------------------------------------------
    def _version_mismatch(self, program: str, asked: int, offered: int, source: str) -> None:
        self.events.emit("nfs", "version-mismatch",
                         f"{program} v{asked} requested, only v{offered} is offered",
                         client=source, level=30)

    def handle(self, data: bytes, source: str) -> Optional[bytes]:
        call = rpc.parse_call(data)
        if call is None:
            return None
        if call.prog == MOUNT_PROG:
            if call.vers != MOUNT_VERS:
                self._version_mismatch("MOUNT", call.vers, MOUNT_VERS, source)
                return rpc.accepted_reply(call.xid, rpc.pack_u32(MOUNT_VERS, MOUNT_VERS),
                                          status=rpc.PROG_MISMATCH)
            handler = self._mount
        elif call.prog == NFS_PROG:
            if call.vers != NFS_VERS:
                self._version_mismatch("NFS", call.vers, NFS_VERS, source)
                return rpc.accepted_reply(call.xid, rpc.pack_u32(NFS_VERS, NFS_VERS),
                                          status=rpc.PROG_MISMATCH)
            handler = self._nfs
        else:
            return rpc.accepted_reply(call.xid, status=rpc.PROG_UNAVAIL)
        try:
            results = handler(call.proc, call.args, source)
        except rpc.XdrError:
            return rpc.accepted_reply(call.xid, status=rpc.GARBAGE_ARGS)
        if results is None:
            return rpc.accepted_reply(call.xid, status=rpc.PROC_UNAVAIL)
        return rpc.accepted_reply(call.xid, results)

    def _permitted(self, source: str) -> bool:
        if self.allowed is None:
            return True
        try:
            return ipaddress.IPv4Address(source) in self.allowed
        except ValueError:
            return False

    # -- MOUNT v1 ----------------------------------------------------------------------------
    def _mount(self, proc: int, u: rpc.Unpacker, source: str) -> Optional[bytes]:
        if proc == M_NULL or proc in (M_UMNT, M_UMNTALL):
            if proc == M_UMNT:
                u.string(1024)
            return b""
        if proc == M_DUMP:
            return rpc.pack_u32(0)
        if proc == M_EXPORT:
            out = b""
            for path in self._exports:
                out += rpc.pack_u32(1) + rpc.pack_opaque(path) + rpc.pack_u32(0)
            return out + rpc.pack_u32(0)
        if proc == M_MNT:
            requested = u.opaque(1024)
            root = self._exports.get(requested.rstrip(b"/") or b"/")
            if root is None or not self._permitted(source):
                reason = "not exported" if root is None else "not allowed from this address"
                self.events.emit("nfs", "mount-denied",
                                 f"MOUNT {os.fsdecode(requested)!r} {reason}", client=source,
                                 level=30)
                return rpc.pack_u32(NFSERR_ACCES)
            self.events.emit("nfs", "mount", f"MOUNT {os.fsdecode(requested)}", client=source)
            return rpc.pack_u32(0) + self._handle_for(root, root)
        return None

    # -- NFS v2 -------------------------------------------------------------------------------
    def _nfs(self, proc: int, u: rpc.Unpacker, source: str) -> Optional[bytes]:
        if proc in (P_NULL, P_ROOT, P_WRITECACHE):
            return b""
        if proc > P_STATFS:
            return None
        if not self._permitted(source):
            return rpc.pack_u32(NFSERR_ACCES)
        if proc in WRITE_PROCS:
            return rpc.pack_u32(NFSERR_ROFS)
        try:
            root, path = self._resolve(u)
        except LookupError:
            return rpc.pack_u32(NFSERR_STALE)
        try:
            if proc == P_GETATTR:
                return rpc.pack_u32(NFS_OK) + fattr(os.lstat(path))
            if proc == P_LOOKUP:
                return self._lookup(root, path, u.opaque(MAXNAMLEN + 1))
            if proc == P_READLINK:
                return rpc.pack_u32(NFS_OK) + rpc.pack_opaque(os.readlink(path))
            if proc == P_READ:
                offset, count, _total = u.u32(), u.u32(), u.u32()
                return self._read(path, offset, min(count, MAXDATA))
            if proc == P_READDIR:
                cookie = u.data[u.offset:u.offset + 4]
                u.offset += 4
                return self._readdir(root, path, int.from_bytes(cookie, "big"), u.u32())
            if proc == P_STATFS:
                fs = os.statvfs(path)
                return rpc.pack_u32(NFS_OK, MAXDATA, _u32(fs.f_frsize or fs.f_bsize),
                                    _u32(fs.f_blocks), _u32(fs.f_bfree), _u32(fs.f_bavail))
        except FileNotFoundError:
            return rpc.pack_u32(NFSERR_NOENT)
        except NotADirectoryError:
            return rpc.pack_u32(NFSERR_NOTDIR)
        except IsADirectoryError:
            return rpc.pack_u32(NFSERR_ISDIR)
        except PermissionError:
            return rpc.pack_u32(NFSERR_ACCES)
        except OSError:
            return rpc.pack_u32(NFSERR_IO)
        return None

    def _lookup(self, root: bytes, directory: bytes, name: bytes) -> bytes:
        if len(name) > MAXNAMLEN:
            return rpc.pack_u32(NFSERR_NAMETOOLONG)
        if not name or b"/" in name or b"\0" in name:
            return rpc.pack_u32(NFSERR_NOENT)
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            return rpc.pack_u32(NFSERR_NOTDIR)
        if name == b".":
            target = directory
        elif name == b"..":
            target = directory if directory == root else os.path.dirname(directory)
        else:
            target = os.path.join(directory, name)
        st = os.lstat(target)   # never follow symlinks on the server
        return rpc.pack_u32(NFS_OK) + self._handle_for(root, target) + fattr(st)

    def _read(self, path: bytes, offset: int, count: int) -> bytes:
        st = os.lstat(path)
        if stat.S_ISDIR(st.st_mode):
            return rpc.pack_u32(NFSERR_ISDIR)
        if not stat.S_ISREG(st.st_mode):
            return rpc.pack_u32(NFSERR_INVAL)
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(count)
        return rpc.pack_u32(NFS_OK) + fattr(st) + rpc.pack_opaque(data)

    def _readdir(self, root: bytes, path: bytes, cookie: int, count: int) -> bytes:
        if cookie == 0 or path not in self._dir_cache:
            self._dir_cache[path] = [b".", b".."] + sorted(os.listdir(path))
        names = self._dir_cache[path]
        out = bytearray(rpc.pack_u32(NFS_OK))
        budget = max(count, 512) - 16
        index = cookie
        while index < len(names):
            name = names[index]
            if name == b".":
                target = path
            elif name == b"..":
                target = path if path == root else os.path.dirname(path)
            else:
                target = os.path.join(path, name)
            try:
                fileid = os.lstat(target).st_ino & U32
            except OSError:
                index += 1
                continue
            entry = rpc.pack_u32(1, fileid) + rpc.pack_opaque(name) + rpc.pack_u32(index + 1)
            if len(out) + len(entry) > budget and index > cookie:
                break   # always return at least one entry, or the client loops
            out += entry
            index += 1
        out += rpc.pack_u32(0, 1 if index >= len(names) else 0)
        return bytes(out)

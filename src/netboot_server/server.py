"""Starts and stops all network services together."""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .catalog import Catalog
from .events import EventBus
from .net import rpc
from .net.bootparamd import (BOOTPARAM_PROG, BOOTPARAM_VERS, BootparamServer,
                             forwarding_works)
from .net.nfs2 import MOUNT_PROG, MOUNT_VERS, NFS_PROG, NFS_VERS, Nfs2Server
from .net.portmap import BroadcastListener, Portmapper
from .roots import RootManager, default_nfs_mode
from .net.dhcp_server import DhcpBsdpServer, DhcpConfig, root_path_for
from .net.http_server import ImageHTTPServer, start_http_server
from .net.leases import LeasePool
from .net.tftp import TftpConfig, TftpServer
from .netif import get_interface, make_udp_socket

IPv4 = ipaddress.IPv4Address

PORT_HINTS = {
    67: "macOS: Internet Sharing (bootpd). Linux: dnsmasq, isc-dhcp-server or kea.",
    69: "macOS: com.apple.tftpd. Linux: tftpd-hpa, dnsmasq or in.tftpd.",
}


@dataclass
class ServeOptions:
    interface: str
    images_root: Path
    server_ip: Optional[IPv4] = None
    dhcp_mode: str = "bsdp-only"
    pool_start: Optional[IPv4] = None
    pool_end: Optional[IPv4] = None
    router: Optional[IPv4] = None
    dns: List[IPv4] = field(default_factory=list)
    lease_time: int = 3600
    http_port: int = 80            # 0 disables HTTP
    nfs_export: Optional[str] = None
    defaults: Dict[str, int] = field(default_factory=dict)   # arch -> image index
    tftp_max_blksize: int = 1468
    dhcp_port: int = 67
    client_port: int = 68
    tftp_port: int = 69
    state_dir: Optional[Path] = None
    bootparamd: bool = True        # for Mac OS X 10.0/10.1 (RootDirectory images)
    bootparam_port: int = 0        # 0 = any free port, registered with the portmapper
    nfs_mode: str = "auto"         # folder roots: "system" (macOS nfsd), "builtin", "auto"
    manage_roots: bool = True      # mount + export RootDirectory images automatically


class StartupError(Exception):
    pass


def _bind_error(exc: OSError, port: int, what: str) -> StartupError:
    if exc.errno == errno.EADDRINUSE:
        hint = PORT_HINTS.get(port, "")
        return StartupError(f"{what}: port {port} is already in use. {hint}".strip())
    if exc.errno in (errno.EACCES, errno.EPERM):
        return StartupError(f"{what}: permission denied for port {port}; run as root (sudo)")
    return StartupError(f"{what}: cannot bind port {port}: {exc}")


async def serve(options: ServeOptions, events: EventBus,
                stop: Optional[asyncio.Event] = None) -> None:
    loop = asyncio.get_running_loop()
    iface = get_interface(options.interface)
    if options.server_ip is not None:
        address = next((a for a in iface.addresses if a.ip == options.server_ip), None)
        if address is None:
            raise StartupError(f"{options.server_ip} is not configured on {iface.name}")
    else:
        address = iface.primary
        if address is None:
            raise StartupError(f"interface {iface.name} has no IPv4 address")
    server_ip = address.ip

    catalog = Catalog(options.images_root)
    catalog.default_index.update(options.defaults)
    catalog.reload()

    nfs_mode = default_nfs_mode() if options.nfs_mode == "auto" else options.nfs_mode
    roots = RootManager(nfs_mode, address.network, events) if options.manage_roots else None
    root_statuses = await loop.run_in_executor(None, roots.sync, catalog) if roots else []

    pool = None
    if options.dhcp_mode == "full":
        if options.pool_start is None or options.pool_end is None:
            raise StartupError("full DHCP mode needs --pool-start and --pool-end")
        for ip in (options.pool_start, options.pool_end):
            if ip not in address.network:
                raise StartupError(f"pool address {ip} is outside {address.network}")
        state_file = options.state_dir / "leases.json" if options.state_dir else None
        excluded = [server_ip] + ([options.router] if options.router else [])
        pool = LeasePool(options.pool_start, options.pool_end, options.lease_time,
                         excluded=excluded, state_file=state_file)

    http_base = None
    if options.http_port:
        http_base = f"http://{server_ip}" + ("" if options.http_port == 80 else f":{options.http_port}")
    config = DhcpConfig(server_ip=server_ip, subnet=address.network, mode=options.dhcp_mode,
                        pool=pool, router=options.router, dns=options.dns,
                        nfs_export=options.nfs_export, http_base=http_base,
                        client_port=options.client_port, server_port=options.dhcp_port)

    _report_catalog(catalog, config, events)

    try:
        dhcp_sock = make_udp_socket("0.0.0.0", options.dhcp_port, iface.name, broadcast=True)
    except OSError as exc:
        raise _bind_error(exc, options.dhcp_port, "DHCP/BSDP") from exc
    dhcp = DhcpBsdpServer(config, catalog, events)
    dhcp_transport, _ = await loop.create_datagram_endpoint(lambda: dhcp, sock=dhcp_sock)

    tftp_config = TftpConfig(root=options.images_root, bind_ip=str(server_ip),
                             port=options.tftp_port, interface=iface.name,
                             max_blksize=options.tftp_max_blksize)
    try:
        tftp_sock = make_udp_socket(str(server_ip), options.tftp_port, iface.name)
    except OSError as exc:
        dhcp_transport.close()
        raise _bind_error(exc, options.tftp_port, "TFTP") from exc
    tftp_transport, _ = await loop.create_datagram_endpoint(
        lambda: TftpServer(tftp_config, events), sock=tftp_sock)

    services = RpcServices(events)
    await services.start(options, iface.name, address, dhcp, catalog, nfs_mode)
    if services.nfs is not None and roots is not None:
        services.nfs.set_exports(roots.builtin_exports(root_statuses))

    http: Optional[ImageHTTPServer] = None
    if options.http_port:
        try:
            http = start_http_server(str(server_ip), options.http_port, options.images_root,
                                     events, iface.name)
        except OSError as exc:
            dhcp_transport.close()
            tftp_transport.close()
            await services.stop()
            raise _bind_error(exc, options.http_port, "HTTP") from exc

    events.emit("server", "started",
                f"serving on {iface.name} ({address}), DHCP mode {options.dhcp_mode}, "
                f"HTTP {'port ' + str(options.http_port) if options.http_port else 'off'}, "
                f"NFS export {options.nfs_export or 'not configured'}")

    stop = stop or asyncio.Event()

    async def reload_catalog() -> None:
        catalog.reload()
        if roots is not None:
            statuses = await loop.run_in_executor(None, roots.sync, catalog)
            if services.nfs is not None:
                services.nfs.set_exports(roots.builtin_exports(statuses))
        _report_catalog(catalog, config, events)

    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
        loop.add_signal_handler(signal.SIGTERM, stop.set)
        loop.add_signal_handler(signal.SIGHUP,
                                lambda: asyncio.ensure_future(reload_catalog()))
    except (NotImplementedError, RuntimeError):
        pass  # not the main thread (tests)

    try:
        await stop.wait()
    finally:
        dhcp_transport.close()
        tftp_transport.close()
        await services.stop()
        if http is not None:
            http.shutdown()
            http.server_close()
        events.emit("server", "stopped", "all services stopped")


def make_bootparam_lookup(dhcp: DhcpBsdpServer, catalog: Catalog):
    """Map a 10.0 client's IP to (MAC, image with a RootDirectory)."""
    def lookup(ip: IPv4):
        record = next((r for r in dhcp.clients.values() if r.ip == str(ip)), None)
        mac = record.mac if record else None
        if mac is None and dhcp.config.pool is not None:
            # Not seen since the server started (e.g. restarted mid-boot):
            # the saved lease table still knows who holds this address.
            mac = dhcp.config.pool.mac_for(ip)
        if mac is None:
            return None
        image = None
        if record is not None and record.selected_image:
            image = catalog.by_index(record.selected_image)
        if image is None or not image.root_directory:
            image = catalog.default_for((record.arch if record else None) or "ppc",
                                        record.model if record else None)
        if image is not None and image.root_directory:
            return mac, image
        return None
    return lookup


class RpcServices:
    """bootparamd and the built-in NFS v2 server, plus how clients reach them.

    With a host rpcbind we register with it. If it doesn't forward CALLIT (Linux),
    we also listen for the WHOAMI broadcast. Without rpcbind, we run our own
    portmapper on UDP 111. See net/portmap.py.
    """

    def __init__(self, events: EventBus) -> None:
        self.events = events
        self.transports: list = []
        self.registered: List[Tuple[int, int]] = []
        self.listener: Optional[BroadcastListener] = None
        self.bootparam: Optional[BootparamServer] = None
        self.bootparam_port = 0
        self.nfs: Optional[Nfs2Server] = None
        self.nfs_port = 0

    def _mappings(self) -> List[Tuple[int, int, int]]:
        mappings = []
        if self.bootparam is not None:
            mappings.append((BOOTPARAM_PROG, BOOTPARAM_VERS, self.bootparam_port))
        if self.nfs is not None:
            mappings += [(NFS_PROG, NFS_VERS, self.nfs_port), (MOUNT_PROG, MOUNT_VERS, self.nfs_port)]
        return mappings

    async def start(self, options: ServeOptions, interface: str,
                    address: ipaddress.IPv4Interface, dhcp: DhcpBsdpServer,
                    catalog: Catalog, nfs_mode: str) -> None:
        loop = asyncio.get_running_loop()
        server_ip = address.ip
        if options.bootparamd:
            try:
                # Not bound to the interface: a host rpcbind forwards the
                # broadcast WHOAMI to us over loopback.
                sock = make_udp_socket("0.0.0.0", options.bootparam_port)
            except OSError as exc:
                self.events.emit("bootparam", "bind-failed", f"bootparamd not started: {exc}",
                                 level=40)
            else:
                self.bootparam = BootparamServer(server_ip, make_bootparam_lookup(dhcp, catalog),
                                                 self.events, router=options.router)
                transport, _ = await loop.create_datagram_endpoint(lambda: self.bootparam, sock=sock)
                self.transports.append(transport)
                self.bootparam_port = sock.getsockname()[1]
        if nfs_mode == "builtin":
            sock = make_udp_socket(str(server_ip), 0, interface)
            self.nfs = Nfs2Server(self.events, allowed=address.network)
            transport, _ = await loop.create_datagram_endpoint(lambda: self.nfs, sock=sock)
            self.transports.append(transport)
            self.nfs_port = sock.getsockname()[1]
            self.events.emit("nfs", "started",
                             f"built-in read-only NFS v2 / MOUNT v1 server on UDP {self.nfs_port}")
        if not self._mappings():
            return

        host_rpcbind = await loop.run_in_executor(
            None, lambda: rpc.pmap_call(rpc.PMAPPROC_NULL, b"") is not None)
        if host_rpcbind:
            await self._use_host_rpcbind(loop, interface, address, nfs_mode)
        else:
            await self._run_own_portmapper(loop, interface, nfs_mode)

    async def _use_host_rpcbind(self, loop, interface: str, address: ipaddress.IPv4Interface,
                                nfs_mode: str) -> None:
        for prog, vers, port in self._mappings():
            if await loop.run_in_executor(None, rpc.pmap_set, prog, vers, port):
                self.registered.append((prog, vers))
            else:
                self.events.emit("rpc", "register-failed",
                                 f"rpcbind refused to register program {prog} v{vers}", level=40)
        if nfs_mode == "builtin" and sys.platform == "darwin":
            self.events.emit("nfs", "takeover",
                             "built-in NFS v2 registered in place of macOS nfsd's NFS v2", level=30)
        if (BOOTPARAM_PROG, BOOTPARAM_VERS) not in self.registered:
            return
        if await loop.run_in_executor(None, forwarding_works):
            self.events.emit("bootparam", "ready",
                             f"bootparamd on UDP {self.bootparam_port}, registered with rpcbind; "
                             "CALLIT forwarding works, so Mac OS X 10.0/10.1 clients need no rp= "
                             "boot-arg")
            return
        if sys.platform.startswith("linux"):
            try:
                self.listener = BroadcastListener(
                    interface, address.network, str(address.ip),
                    {BOOTPARAM_PROG: (BOOTPARAM_VERS, self.bootparam_port, self.bootparam.dispatch)},
                    self.events)
                self.listener.start(loop)
                self.events.emit("bootparam", "ready",
                                 f"bootparamd on UDP {self.bootparam_port}; rpcbind doesn't forward "
                                 f"CALLIT, so answering WHOAMI broadcasts on {interface} directly")
                return
            except OSError as exc:
                self.events.emit("bootparam", "listener-failed",
                                 f"cannot listen for WHOAMI broadcasts: {exc}", level=40)
        self.events.emit("bootparam", "no-forwarding",
                         f"bootparamd on UDP {self.bootparam_port} is registered, but rpcbind doesn't "
                         "forward CALLIT, so 10.0/10.1 clients need an rp= boot-arg", level=30)

    async def _run_own_portmapper(self, loop, interface: str, nfs_mode: str) -> None:
        try:
            sock = make_udp_socket("0.0.0.0", rpc.PMAP_PORT, interface, broadcast=True)
        except OSError as exc:
            self.events.emit("rpc", "portmap-failed",
                             f"no rpcbind is running and port 111 can't be bound ({exc}); "
                             "10.0/10.1 clients won't find bootparamd or NFS", level=40)
            return
        portmapper = Portmapper(self.events)
        for prog, vers, port in self._mappings():
            handler = self.bootparam.dispatch if prog == BOOTPARAM_PROG and self.bootparam else None
            portmapper.register_local(prog, vers, port, handler)
        transport, _ = await loop.create_datagram_endpoint(lambda: portmapper, sock=sock)
        self.transports.append(transport)
        self.events.emit("rpc", "portmap",
                         f"no rpcbind on this host: running our own portmapper on UDP 111 ({interface})")
        if nfs_mode == "system":
            self.events.emit("rpc", "portmap-system-nfs",
                             "the system NFS server needs rpcbind; folder roots won't be reachable",
                             level=30)

    async def stop(self) -> None:
        loop = asyncio.get_running_loop()
        for prog, vers in self.registered:
            await loop.run_in_executor(None, rpc.pmap_unset, prog, vers)
        self.registered.clear()
        if self.listener is not None:
            self.listener.close(loop)
            self.listener = None
        for transport in self.transports:
            transport.close()
        self.transports.clear()


def _report_catalog(catalog: Catalog, config: DhcpConfig, events: EventBus) -> None:
    if not catalog.images:
        events.emit("catalog", "empty", f"no images in {catalog.images_root}", level=30)
    for image in catalog.images:
        problems = image.problems()
        if root_path_for(image, config) is None:
            problems.append(f"uses {image.protocol} but that is not configured")
        state = "enabled" if image.enabled else "disabled"
        line = (f"image {image.index} {image.name!r} [{', '.join(image.architectures)}] "
                f"{image.protocol}, {state}")
        if problems:
            events.emit("catalog", "problem", f"{line}: " + "; ".join(problems), level=30)
        else:
            events.emit("catalog", "image", line)
    for arch in ("ppc", "i386"):
        default = catalog.default_for(arch, None)
        if default:
            events.emit("catalog", "default", f"default for {arch}: {default.name!r}")

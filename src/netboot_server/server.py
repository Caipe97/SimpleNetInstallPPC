"""Starts and stops all network services together."""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .catalog import Catalog
from .events import EventBus
from .net import rpc
from .net.bootparamd import (BOOTPARAM_PROG, BOOTPARAM_VERS, BootparamServer,
                             forwarding_works)
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
    bootparamd: bool = True        # for Mac OS X 10.0 (RootDirectory images)
    bootparam_port: int = 0        # 0 = any free port, registered with rpcbind


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

    bootparam_transport = None
    bootparam_registered = False
    if options.bootparamd:
        bootparam_transport, bootparam_registered = await _start_bootparamd(
            options, server_ip, dhcp, catalog, events)

    http: Optional[ImageHTTPServer] = None
    if options.http_port:
        try:
            http = start_http_server(str(server_ip), options.http_port, options.images_root,
                                     events, iface.name)
        except OSError as exc:
            dhcp_transport.close()
            tftp_transport.close()
            if bootparam_transport is not None:
                bootparam_transport.close()
            raise _bind_error(exc, options.http_port, "HTTP") from exc

    events.emit("server", "started",
                f"serving on {iface.name} ({address}), DHCP mode {options.dhcp_mode}, "
                f"HTTP {'port ' + str(options.http_port) if options.http_port else 'off'}, "
                f"NFS export {options.nfs_export or 'not configured'}")

    stop = stop or asyncio.Event()

    def reload_catalog() -> None:
        catalog.reload()
        _report_catalog(catalog, config, events)

    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
        loop.add_signal_handler(signal.SIGTERM, stop.set)
        loop.add_signal_handler(signal.SIGHUP, reload_catalog)
    except (NotImplementedError, RuntimeError):
        pass  # not the main thread (tests)

    try:
        await stop.wait()
    finally:
        dhcp_transport.close()
        tftp_transport.close()
        if bootparam_transport is not None:
            bootparam_transport.close()
            if bootparam_registered:
                await loop.run_in_executor(None, rpc.pmap_unset, BOOTPARAM_PROG, BOOTPARAM_VERS)
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


async def _start_bootparamd(options: ServeOptions, server_ip: IPv4, dhcp: DhcpBsdpServer,
                            catalog: Catalog, events: EventBus):
    loop = asyncio.get_running_loop()
    try:
        # Not bound to the interface: the host's rpcbind forwards the
        # kernel's broadcast WHOAMI to us over loopback.
        sock = make_udp_socket("0.0.0.0", options.bootparam_port)
    except OSError as exc:
        events.emit("bootparam", "bind-failed", f"bootparamd not started: {exc}", level=40)
        return None, False
    server = BootparamServer(server_ip, make_bootparam_lookup(dhcp, catalog), events,
                             router=options.router)
    transport, _ = await loop.create_datagram_endpoint(lambda: server, sock=sock)
    port = sock.getsockname()[1]
    registered = await loop.run_in_executor(None, rpc.pmap_set, BOOTPARAM_PROG,
                                            BOOTPARAM_VERS, port)
    if not registered:
        events.emit("bootparam", "unregistered",
                    f"bootparamd on UDP {port}, but rpcbind refused or is not running "
                    "(start NFS first). Mac OS X 10.0 clients need an rp= boot-arg until "
                    "this works", level=30)
        return transport, False
    forwards = await loop.run_in_executor(None, forwarding_works)
    if forwards:
        events.emit("bootparam", "ready",
                    f"bootparamd on UDP {port}, registered with rpcbind; CALLIT forwarding works, "
                    "so Mac OS X 10.0 clients need no rp= boot-arg")
    else:
        events.emit("bootparam", "no-forwarding",
                    f"bootparamd on UDP {port} is registered, but rpcbind does not forward "
                    "CALLIT, so 10.0 clients' WHOAMI broadcast won't reach us. Use an rp= "
                    "boot-arg for now", level=30)
    return transport, True


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

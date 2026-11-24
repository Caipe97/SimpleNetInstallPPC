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

    http: Optional[ImageHTTPServer] = None
    if options.http_port:
        try:
            http = start_http_server(str(server_ip), options.http_port, options.images_root,
                                     events, iface.name)
        except OSError as exc:
            dhcp_transport.close()
            tftp_transport.close()
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
        if http is not None:
            http.shutdown()
            http.server_close()
        events.emit("server", "stopped", "all services stopped")


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

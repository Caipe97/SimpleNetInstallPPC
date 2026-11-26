"""Starts and stops all network services together."""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import functools
import ipaddress
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .catalog import Catalog
from .events import EventBus
from .net import dhcp_probe, rpc
from .net.bootparamd import (BOOTPARAM_PROG, BOOTPARAM_VERS, BootparamServer,
                             forwarding_works)
from .net.nfs2 import MOUNT_PROG, MOUNT_VERS, NFS_PROG, NFS_VERS, Nfs2Server
from .net.portmap import BroadcastListener, Portmapper
from .checks import service_hint
from .roots import RootManager, default_nfs_mode
from .store import DB_NAME, Store
from .net.dhcp_server import DhcpBsdpServer, DhcpConfig, root_path_for
from .net.http_server import ImageHTTPServer, start_http_server
from .net.leases import LeasePool
from .net.tftp import TftpConfig, TftpServer
from .netif import get_interface, list_interfaces, make_udp_socket
from .netsettings import (FIELD_NAMES, STORE_KEY, NetworkSettings, SettingsError, check,
                          merge)

IPv4 = ipaddress.IPv4Address

PORT_HINTS = {
    67: "macOS: Internet Sharing (bootpd). Linux: dnsmasq, isc-dhcp-server or kea.",
    69: "macOS: com.apple.tftpd. Linux: tftpd-hpa, dnsmasq or in.tftpd.",
}


@dataclass
class ServeOptions:
    interface: Optional[str]
    images_root: Path
    server_ip: Optional[IPv4] = None
    dhcp_mode: str = "bsdp-only"
    dhcp_clients: str = "netboot"
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
    keep_awake: bool = True        # stop the host from sleeping while serving
    dhcp_probe: bool = True        # look for other DHCP servers before starting
    ignore_other_dhcp: bool = False  # start even if the probe found a conflict
    ui_port: int = 0               # web UI port; 0 = no web UI
    ui_bind: str = "127.0.0.1"     # web UI address (localhost by default, PLAN §5)
    ui_password: bool = False      # ask for the password on localhost too (shared machines)
    # Which network settings (netsettings.FIELD_NAMES) came from the command line;
    # they win over the saved ones. None: all of them (callers other than the CLI).
    given: Optional[Set[str]] = None

    def network_given(self) -> Dict[str, object]:
        names = FIELD_NAMES if self.given is None else self.given
        return {name: getattr(self, name) for name in names
                if not (self.given is None and name == "interface" and self.interface is None)}


class StartupError(Exception):
    pass


def keep_awake_command(pid: int, platform: str = sys.platform,
                       which=shutil.which) -> Optional[List[str]]:
    """A command that keeps the host awake until process ``pid`` exits, if there is one.

    A host that sleeps mid-install strands the Mac: KDE's and GNOME's idle
    suspend respect a logind block inhibitor, macOS idle sleep respects
    caffeinate. Both helpers watch ``pid``, so they go away with us even if
    we're killed.
    """
    if platform == "darwin":
        return ["caffeinate", "-i", "-s", "-w", str(pid)]
    if which("systemd-inhibit") and which("tail"):
        return ["systemd-inhibit", "--what=sleep:idle", "--who=simplenetinstallppc",
                "--why=Serving NetBoot clients", "--mode=block",
                "tail", f"--pid={pid}", "-f", "/dev/null"]
    return None


def start_keep_awake(events: EventBus) -> Optional[subprocess.Popen]:
    command = keep_awake_command(os.getpid())
    if command is None:
        events.emit("server", "sleep", "can't stop this host from sleeping (no "
                    "systemd-inhibit); turn off automatic suspend while serving", level=30)
        return None
    try:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        events.emit("server", "sleep", f"can't stop this host from sleeping: {exc}", level=30)
        return None
    events.emit("server", "awake", f"keeping this host awake while serving ({command[0]})")
    return proc


CLI_COMMANDS = {"interfaces", "images", "inspect", "make-test-image", "build-mkext", "serve"}


def _ancestor_pids() -> Set[int]:
    """This process and its parents (sudo, the shell), so we never report ourselves."""
    pids = {os.getpid()}
    pid = os.getppid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True,
                                 text=True, timeout=2).stdout.strip()
            pid = int(out)
        except (OSError, ValueError, subprocess.SubprocessError):
            break
    return pids


def parse_server_processes(ps_output: str, exclude: Set[int]) -> List[Tuple[int, str]]:
    """(pid, command) of running ``simplenetinstallppc serve`` processes in ``ps`` output.

    Expects lines of ``pid command...``. The ``sudo`` wrapper is skipped, so the
    pid reported is the server itself.
    """
    found = []
    for line in ps_output.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, command = int(parts[0]), parts[1].strip()
        if pid in exclude:
            continue
        words = command.split()
        if not words or os.path.basename(words[0]) == "sudo":
            continue
        for i, word in enumerate(words):
            # The old names too, for a server started before the rename.
            name = os.path.basename(word)
            if name.endswith(".pyz"):          # python3 simplenetinstallppc.pyz serve
                name = name[:-4]
            if name in ("simplenetinstallppc", "netboot_server", "netboot-server"):
                command_word = next((w for w in words[i + 1:] if w in CLI_COMMANDS), None)
                if command_word == "serve":
                    found.append((pid, command))
                break
    return found


def other_servers() -> List[Tuple[int, str]]:
    """Other simplenetinstallppc instances running on this host (best effort)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_server_processes(out, _ancestor_pids())


def port_in_use_message(what: str, port: int, servers: List[Tuple[int, str]]) -> str:
    if servers:
        pid = servers[0][0]
        return (f"{what}: port {port} is already in use by another simplenetinstallppc "
                f"(pid {pid}). To pick up new or changed images, reload it instead: "
                f"sudo kill -HUP {pid}. To restart with other options, stop it first: "
                f"sudo kill {pid}. " + service_hint()).strip()
    return f"{what}: port {port} is already in use. {PORT_HINTS.get(port, '')}".strip()


def _bind_error(exc: OSError, port: int, what: str) -> StartupError:
    if exc.errno == errno.EADDRINUSE:
        return StartupError(port_in_use_message(what, port, other_servers()))
    if exc.errno in (errno.EACCES, errno.EPERM):
        return StartupError(f"{what}: permission denied for port {port}; run as root (sudo)")
    return StartupError(f"{what}: cannot bind port {port}: {exc}")


def open_store(state_dir: Optional[Path], events: EventBus) -> Optional[Store]:
    """The state database in the data folder, importing the JSON files it replaced."""
    if state_dir is None:
        return None
    try:
        store = Store.open(state_dir)
    except (sqlite3.Error, RuntimeError) as exc:
        raise StartupError(f"can't open {state_dir / DB_NAME}: {exc}") from exc
    for level, message in store.import_json(state_dir):
        events.emit("server", "import", message, level=level)
    return store


async def check_other_dhcp(options: ServeOptions, interface: str, mac: str,
                           own: List[str], events: EventBus,
                           override_hint: str = "add --ignore-other-dhcp to start anyway") -> None:
    """Probe for other DHCP servers; raise StartupError on a conflict."""
    loop = asyncio.get_running_loop()
    try:
        answers = await loop.run_in_executor(None, functools.partial(
            dhcp_probe.probe, interface, mac, ignore=own,
            client_port=options.client_port, server_port=options.dhcp_port))
    except dhcp_probe.ProbeUnavailable as exc:
        events.emit("dhcp", "probe-skipped", f"{exc}; not checking for other DHCP servers",
                    level=30)
        return
    pool = None
    if options.dhcp_mode == "full" and options.pool_start and options.pool_end:
        pool = (options.pool_start, options.pool_end)
    lines, refusal = dhcp_probe.assess(answers, options.dhcp_mode, options.dhcp_clients, pool)
    for level, message in lines:
        events.emit("dhcp", "probe", message, level=level)
    if refusal is None:
        return
    if not options.ignore_other_dhcp:
        raise StartupError(f"{refusal} (or {override_hint})")
    events.emit("dhcp", "probe", f"{refusal}; starting anyway", level=30)


class NetworkServices:
    """DHCP/BSDP, TFTP, HTTP, NFS and bootparamd on one interface.

    They start and stop together, so the web UI can move them to another
    interface or change the DHCP settings while the server keeps running.
    """

    def __init__(self, options: ServeOptions, catalog: Catalog, store: Optional[Store],
                 events: EventBus) -> None:
        self.options = options
        self.catalog = catalog
        self.store = store
        self.events = events
        self.nfs_mode = default_nfs_mode() if options.nfs_mode == "auto" else options.nfs_mode
        self.settings: Optional[NetworkSettings] = None   # last started or attempted
        self.running = False
        self.last_error: Optional[str] = None
        self.started_at = 0.0
        self.iface = None
        self.address: Optional[ipaddress.IPv4Interface] = None
        self.config: Optional[DhcpConfig] = None
        self.dhcp: Optional[DhcpBsdpServer] = None
        self.roots: Optional[RootManager] = None
        self.rpc: Optional[RpcServices] = None
        self._transports: list = []
        self._http: Optional[ImageHTTPServer] = None
        self._lock = asyncio.Lock()

    def effective(self, settings: NetworkSettings) -> ServeOptions:
        return dataclasses.replace(self.options,
                                   **{name: getattr(settings, name) for name in FIELD_NAMES})

    def _builtin_exports(self, statuses) -> List[str]:
        # 10.0/10.1 root folders, plus the folder holding 10.2+ root.dmg files
        exports = self.roots.builtin_exports(statuses) if self.roots is not None else []
        if self.options.nfs_export:
            exports.append(self.options.nfs_export)
        return exports

    async def start(self, settings: NetworkSettings, *, probe: bool = True) -> None:
        """Start every service; raises StartupError (nothing left running) on failure."""
        loop = asyncio.get_running_loop()
        self.settings = settings
        try:
            iface, address = check(settings, list_interfaces())
        except SettingsError as exc:
            raise StartupError(str(exc)) from exc
        options = self.effective(settings)
        server_ip = address.ip
        if probe and options.dhcp_probe and iface.mac:
            await check_other_dhcp(options, iface.name, iface.mac,
                                   [str(a.ip) for a in iface.addresses], self.events)

        self.roots = (RootManager(self.nfs_mode, address.network, self.events)
                      if options.manage_roots else None)
        root_statuses = (await loop.run_in_executor(None, self.roots.sync, self.catalog)
                         if self.roots else [])
        pool = None
        if options.dhcp_mode == "full":
            excluded = [server_ip] + ([options.router] if options.router else [])
            pool = LeasePool(options.pool_start, options.pool_end, options.lease_time,
                             excluded=excluded, store=self.store)
        http_base = None
        if options.http_port:
            http_base = f"http://{server_ip}" + ("" if options.http_port == 80
                                                 else f":{options.http_port}")
        config = DhcpConfig(server_ip=server_ip, subnet=address.network, mode=options.dhcp_mode,
                            clients=options.dhcp_clients, pool=pool,
                            router=options.router, dns=options.dns,
                            nfs_export=options.nfs_export, http_base=http_base,
                            client_port=options.client_port, server_port=options.dhcp_port)
        _report_catalog(self.catalog, config, self.events)

        try:
            dhcp_sock = make_udp_socket("0.0.0.0", options.dhcp_port, iface.name, broadcast=True)
        except OSError as exc:
            raise _bind_error(exc, options.dhcp_port, "DHCP/BSDP") from exc
        dhcp = DhcpBsdpServer(config, self.catalog, self.events, self.store)
        transport, _ = await loop.create_datagram_endpoint(lambda: dhcp, sock=dhcp_sock)
        self._transports.append(transport)
        try:
            tftp_config = TftpConfig(root=options.images_root, bind_ip=str(server_ip),
                                     port=options.tftp_port, interface=iface.name,
                                     max_blksize=options.tftp_max_blksize)
            try:
                tftp_sock = make_udp_socket(str(server_ip), options.tftp_port, iface.name)
            except OSError as exc:
                raise _bind_error(exc, options.tftp_port, "TFTP") from exc
            transport, _ = await loop.create_datagram_endpoint(
                lambda: TftpServer(tftp_config, self.events), sock=tftp_sock)
            self._transports.append(transport)

            self.rpc = RpcServices(self.events)
            await self.rpc.start(options, iface.name, address, dhcp, self.catalog, self.nfs_mode)
            if self.rpc.nfs is not None:
                self.rpc.nfs.set_exports(self._builtin_exports(root_statuses))

            if options.http_port:
                try:
                    self._http = start_http_server(str(server_ip), options.http_port,
                                                   options.images_root, self.events, iface.name)
                except OSError as exc:
                    raise _bind_error(exc, options.http_port, "HTTP") from exc
        except BaseException:
            await self._close()
            raise

        self.iface, self.address, self.config, self.dhcp = iface, address, config, dhcp
        self.running, self.last_error, self.started_at = True, None, time.time()
        self.events.emit("server", "started",
                         f"serving on {iface.name} ({address}), DHCP mode {options.dhcp_mode}"
                         + (" (NetBoot Macs only)" if options.dhcp_mode == "full"
                            and options.dhcp_clients == "netboot" else "") + ", "
                         f"HTTP {'port ' + str(options.http_port) if options.http_port else 'off'}"
                         ", NFS disk images (10.2+) "
                         + (f"from {options.nfs_export}" if options.nfs_export
                            else "off (no --nfs-export)"))

    async def _close(self) -> None:
        for transport in self._transports:
            transport.close()
        self._transports.clear()
        if self.rpc is not None:
            await self.rpc.stop()
            self.rpc = None
        if self._http is not None:
            http, self._http = self._http, None
            await asyncio.get_running_loop().run_in_executor(None, http.shutdown)
            http.server_close()

    async def stop(self) -> None:
        was_running = self.running
        await self._close()
        self.running = False
        self.dhcp = self.config = None
        if was_running:
            self.events.emit("server", "network-stopped", "network services stopped")

    async def apply(self, settings: NetworkSettings, ignore_other_dhcp: bool = False) -> None:
        """Move the services to new settings; on failure go back to the old ones."""
        async with self._lock:
            iface, _address = check(settings, list_interfaces())    # SettingsError
            options = self.effective(settings)
            if options.dhcp_probe and iface.mac:
                options.ignore_other_dhcp = ignore_other_dhcp
                try:
                    await check_other_dhcp(options, iface.name, iface.mac,
                                           [str(a.ip) for a in iface.addresses], self.events,
                                           override_hint="tick \"start anyway\"")
                except StartupError as exc:
                    raise SettingsError(str(exc), probe_refusal=True) from exc
            previous = self.settings if self.running else None
            await self.stop()
            try:
                await self.start(settings, probe=False)
            except StartupError as exc:
                self.last_error = str(exc)
                note = "network services are stopped"
                if previous is not None:
                    try:
                        await self.start(previous, probe=False)
                        note = "went back to the previous settings"
                    except StartupError as again:
                        self.last_error = str(again)
                self.events.emit("server", "apply-failed", f"new network settings failed: {exc}; "
                                 f"{note}", level=40)
                raise SettingsError(f"{exc} ({note})") from exc
            if self.store is not None:
                self.store.set_setting(STORE_KEY, settings.to_json())
            self.events.emit("server", "settings", "network settings applied and saved")

    async def reload_catalog(self) -> None:
        self.catalog.reload()
        if self.running and self.roots is not None:
            statuses = await asyncio.get_running_loop().run_in_executor(
                None, self.roots.sync, self.catalog)
            if self.rpc is not None and self.rpc.nfs is not None:
                self.rpc.nfs.set_exports(self._builtin_exports(statuses))
        _report_catalog(self.catalog, self.config, self.events)

    def lights(self) -> List[Dict[str, str]]:
        """One status light per service: ok (green), warn (amber), error (red), off (grey)."""
        def light(name: str, state: str, detail: str) -> Dict[str, str]:
            return {"name": name, "state": state, "detail": detail}
        options = self.options
        if not self.running or self.settings is None:
            return [light(name, "error", "stopped (see Server below)") for name in
                    ("DHCP / BSDP", "TFTP", "HTTP", "NFS", "bootparamd")]
        net = self.effective(self.settings)
        out = []
        if net.dhcp_mode == "full":
            out.append(light("DHCP / BSDP", "ok", f"full DHCP on {self.iface.name}, pool "
                             f"{net.pool_start} - {net.pool_end}"))
        else:
            out.append(light("DHCP / BSDP", "ok", f"BSDP only on {self.iface.name}"))
        out.append(light("TFTP", "ok", f"{self.address.ip} port {options.tftp_port}"))
        if options.http_port:
            out.append(light("HTTP", "ok", f"port {options.http_port} (10.3+ root images)"))
        else:
            out.append(light("HTTP", "off", "turned off (--http-port 0)"))
        rpc = self.rpc
        needs_nfs = [i.name for i in self.catalog.images if i.enabled and i.protocol == "NFS"
                     and not i.root_directory]
        needs_folder = [i.name for i in self.catalog.images if i.enabled and i.root_directory]
        nfs_parts, nfs_state = [], "ok"
        if options.nfs_export:
            nfs_parts.append(f"disk images from {options.nfs_export}")
        elif needs_nfs:
            nfs_state = "warn"
            nfs_parts.append(f"no --nfs-export, needed by {', '.join(needs_nfs)}")
        if rpc is not None and rpc.nfs is not None:
            nfs_parts.append(f"built-in server on UDP {rpc.nfs_port}")
        elif self.nfs_mode == "system":
            nfs_parts.append("system nfsd for folder roots")
        if not nfs_parts:
            out.append(light("NFS", "off", "no NFS images"))
        else:
            out.append(light("NFS", nfs_state, "; ".join(nfs_parts)))
        if rpc is not None and rpc.bootparam is not None:
            state, detail = rpc.portmap
            out.append(light("bootparamd", "warn" if state == "error" else state,
                             f"UDP {rpc.bootparam_port}; {detail}"))
        elif rpc is not None and rpc.bootparam_error:
            out.append(light("bootparamd", "error", rpc.bootparam_error))
        else:
            out.append(light("bootparamd", "warn" if needs_folder else "off",
                             "turned off" + (f", but {', '.join(needs_folder)} needs it"
                                             if needs_folder else "")))
        return out

    def summary(self) -> List[Tuple[str, str]]:
        """The settings the services run with, as (label, value) rows for the UI."""
        options = self.options
        if not self.running or self.settings is None:
            return [("Network services", f"stopped: {self.last_error or 'not started'}")]
        net = self.effective(self.settings)
        iface, address = self.iface, self.address
        rows = [("Interface", f"{iface.name} ({address}, {iface.mac or 'no MAC'})"),
                ("Running since", time.strftime("%Y-%m-%d %H:%M",
                                                time.localtime(self.started_at)))]
        if net.dhcp_mode == "full":
            who = "NetBoot Macs only" if net.dhcp_clients == "netboot" else "every DHCP client"
            rows.append(("DHCP", f"full, answering {who}"))
            rows.append(("Address pool", f"{net.pool_start} - {net.pool_end}, "
                         f"lease {net.lease_time}s"))
            rows.append(("Router / DNS", f"{net.router or '-'} / "
                         + (", ".join(map(str, net.dns)) or "-")))
        else:
            rows.append(("DHCP", "BSDP only (another DHCP server hands out addresses)"))
        rows.append(("TFTP", f"{address.ip} port {options.tftp_port}"))
        rows.append(("HTTP images", f"port {options.http_port}" if options.http_port else "off"))
        rows.append(("NFS disk images (10.2+)", options.nfs_export or "off (no --nfs-export)"))
        rpc = self.rpc
        rows.append(("NFS folder roots (10.0/10.1)", self.nfs_mode
                     + (f", built-in server on UDP {rpc.nfs_port}"
                        if rpc is not None and rpc.nfs is not None else "")))
        rows.append(("bootparamd", f"UDP {rpc.bootparam_port}"
                     if rpc is not None and rpc.bootparam is not None else "off"))
        return rows


def saved_settings(store: Optional[Store], events: EventBus) -> Optional[NetworkSettings]:
    if store is None:
        return None
    data = store.setting(STORE_KEY)
    if not data:
        return None
    try:
        return NetworkSettings.from_json(data)
    except SettingsError as exc:
        events.emit("server", "settings", f"ignoring the saved network settings: {exc}", level=30)
        return None


def with_default_export(options: ServeOptions) -> ServeOptions:
    """Our own NFS server can export the images folder itself, so --nfs-export
    is only needed with the system's nfsd (macOS)."""
    mode = default_nfs_mode() if options.nfs_mode == "auto" else options.nfs_mode
    if options.nfs_export or mode != "builtin":
        return options
    return dataclasses.replace(options, nfs_export=str(options.images_root.resolve()))


async def serve(options: ServeOptions, events: EventBus,
                stop: Optional[asyncio.Event] = None) -> None:
    loop = asyncio.get_running_loop()
    options = with_default_export(options)
    store = open_store(options.state_dir, events)
    catalog = Catalog(options.images_root)
    catalog.default_index.update(options.defaults)
    catalog.reload()
    net = NetworkServices(options, catalog, store, events)
    ui = bool(options.ui_port and options.state_dir is not None)
    web = awake = None
    try:
        settings = merge(saved_settings(store, events), options.network_given())
        try:
            if not settings.interface:
                raise StartupError("no network interface chosen: give -i/--interface"
                                   + (", or choose one on the web UI's Network page" if ui else ""))
            await net.start(settings)
            if store is not None:
                store.set_setting(STORE_KEY, settings.to_json())
        except StartupError as exc:
            if not ui:
                raise
            net.settings, net.last_error = settings, str(exc)
            _report_catalog(catalog, None, events)
            events.emit("server", "network-off", f"network services not started: {exc}. The web "
                        "UI is running: fix the settings on its Network page.", level=40)

        stop = stop or asyncio.Event()
        web = start_web(options, events, catalog, store, net, loop)
        if web is None and not net.running:
            # Neither the NetBoot services nor the UI to fix them: nothing to run.
            raise StartupError(net.last_error or "nothing started")
        awake = start_keep_awake(events) if options.keep_awake else None
        try:
            loop.add_signal_handler(signal.SIGINT, stop.set)
            loop.add_signal_handler(signal.SIGTERM, stop.set)
            loop.add_signal_handler(signal.SIGHUP,
                                    lambda: asyncio.ensure_future(net.reload_catalog()))
        except (NotImplementedError, RuntimeError):
            pass  # not the main thread (tests)
        await stop.wait()
    finally:
        if web is not None:
            await loop.run_in_executor(None, web.stop)
        await net.stop()
        if awake is not None:
            awake.terminate()
            awake.wait()
        if store is not None:
            store.close()
        events.emit("server", "stopped", "all services stopped")


def interface_list() -> List[Dict[str, object]]:
    return [{"name": i.name, "mac": i.mac, "up": i.up,
             "addresses": [str(a) for a in i.addresses]}
            for i in list_interfaces() if i.addresses or i.mac]


def start_web(options: ServeOptions, events: EventBus, catalog: Catalog, store: Optional[Store],
              net: NetworkServices, loop: asyncio.AbstractEventLoop):
    """Start the web UI in its own threads, if it's turned on (None otherwise)."""
    if not options.ui_port:
        return None
    if options.state_dir is None:
        events.emit("web", "off", "web UI not started: it needs the data folder", level=30)
        return None
    from .web.app import PASSWORD_FILE, WebContext, is_loopback, load_or_create_password, \
        start_web_server
    from .web.jobs import BuildJobs

    def run(coroutine, timeout: float):
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result(timeout=timeout)

    def reload() -> None:
        run(net.reload_catalog(), 600)

    def probe(interface: Optional[str] = None):
        name = interface or (net.iface.name if net.iface else None)
        if not name:
            raise SettingsError("choose an interface first")
        iface = get_interface(name)
        if not iface.mac:
            raise SettingsError(f"{name} has no Ethernet address, so it can't probe")
        return dhcp_probe.probe(iface.name, iface.mac, ignore=[str(a.ip) for a in iface.addresses],
                                client_port=options.client_port, server_port=options.dhcp_port)

    def network() -> Dict[str, object]:
        settings = net.settings or NetworkSettings()
        return {"settings": settings.to_json(), "running": net.running, "error": net.last_error,
                "command_line": sorted(options.network_given()), "interfaces": interface_list()}

    def apply_network(settings: NetworkSettings, ignore_other_dhcp: bool) -> None:
        run(net.apply(settings, ignore_other_dhcp), 120)

    def status() -> Dict[str, object]:
        rows = net.summary()
        rows.append(("Web UI", f"http://{options.ui_bind}:{options.ui_port}"))
        rows.append(("Data folder", str(options.state_dir)))
        lights = net.lights()
        enabled = [i for i in catalog.images if i.enabled]
        troubled = [i.name for i in enabled if i.problems() or (
            net.config is not None and root_path_for(i, net.config) is None)]
        if not enabled:
            lights.append({"name": "Images", "state": "error", "detail": "no enabled images"})
        elif troubled:
            lights.append({"name": "Images", "state": "warn",
                           "detail": f"{len(enabled)} enabled; problems: {', '.join(troubled)}"})
        else:
            lights.append({"name": "Images", "state": "ok", "detail": f"{len(enabled)} enabled"})
        return {"summary": rows, "running": net.running, "lights": lights}

    ctx = WebContext(
        events=events, catalog=catalog, data_root=options.state_dir, store=store,
        status=status, reload=reload, probe=probe,
        live_clients=lambda: dict(net.dhcp.clients) if net.dhcp is not None else {},
        jobs=BuildJobs(options.state_dir, events, on_success=reload),
        cli_defaults=dict(options.defaults),
        image_problems=lambda image: [] if net.config is None
        or root_path_for(image, net.config) is not None
        else [f"uses {image.protocol} but that is not configured"],
        network=network, apply_network=apply_network)
    # No login on localhost: only this machine (and SSH tunnels into it) can
    # connect. Anywhere else the password protects a server that runs as root.
    login = options.ui_password or not is_loopback(options.ui_bind)
    try:
        password, created = (load_or_create_password(options.state_dir) if login
                             else (None, False))
        web = start_web_server(options.ui_bind, options.ui_port, ctx, password)
    except OSError as exc:
        events.emit("web", "bind-failed", f"web UI not started on {options.ui_bind}:"
                    f"{options.ui_port}: {exc.strerror or exc}", level=40)
        return None
    url = f"http://{options.ui_bind}:{options.ui_port}/"
    if login:
        password_file = options.state_dir / PASSWORD_FILE
        events.emit("web", "started", f"web UI on {url} (user admin, password in "
                    f"{password_file}" + (", just created" if created else "") + ")")
    else:
        events.emit("web", "started", f"web UI on {url} (only this machine can connect; "
                    f"from another computer: ssh -N -L {options.ui_port}:127.0.0.1:"
                    f"{options.ui_port} <this host>)")
    if not is_loopback(options.ui_bind):
        events.emit("web", "exposed", f"the web UI listens on {options.ui_bind}: anyone who "
                    "can reach it there and has the password controls this server", level=30)
    return web


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
        self.bootparam_error: Optional[str] = None
        # How 10.0/10.1 clients find bootparamd and NFS: (light, detail) for the dashboard.
        self.portmap: Tuple[str, str] = ("off", "not needed")

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
                self.bootparam_error = f"not started: {exc}"
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
        self.portmap = ("ok", "host rpcbind")
        for prog, vers, port in self._mappings():
            if await loop.run_in_executor(None, rpc.pmap_set, prog, vers, port):
                self.registered.append((prog, vers))
            else:
                self.portmap = ("error", f"host rpcbind refused program {prog} v{vers}")
                self.events.emit("rpc", "register-failed",
                                 f"rpcbind refused to register program {prog} v{vers}", level=40)
        if nfs_mode == "builtin" and sys.platform == "darwin":
            self.events.emit("nfs", "takeover",
                             "built-in NFS v2 registered in place of macOS nfsd's NFS v2", level=30)
        if (BOOTPARAM_PROG, BOOTPARAM_VERS) not in self.registered:
            return
        if await loop.run_in_executor(None, forwarding_works):
            self.portmap = ("ok", "host rpcbind (forwards broadcasts)")
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
                self.portmap = ("ok", f"host rpcbind, plus WHOAMI broadcasts on {interface}")
                self.events.emit("bootparam", "ready",
                                 f"bootparamd on UDP {self.bootparam_port}; rpcbind doesn't forward "
                                 f"CALLIT, so answering WHOAMI broadcasts on {interface} directly")
                return
            except OSError as exc:
                self.events.emit("bootparam", "listener-failed",
                                 f"cannot listen for WHOAMI broadcasts: {exc}", level=40)
        self.portmap = ("warn", "rpcbind doesn't forward broadcasts: 10.0/10.1 need an rp= "
                                "boot-arg")
        self.events.emit("bootparam", "no-forwarding",
                         f"bootparamd on UDP {self.bootparam_port} is registered, but rpcbind doesn't "
                         "forward CALLIT, so 10.0/10.1 clients need an rp= boot-arg", level=30)

    async def _run_own_portmapper(self, loop, interface: str, nfs_mode: str) -> None:
        try:
            sock = make_udp_socket("0.0.0.0", rpc.PMAP_PORT, interface, broadcast=True)
        except OSError as exc:
            self.portmap = ("error", f"port 111 unavailable ({exc.strerror or exc})")
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
        self.portmap = ("ok", f"our own portmapper on UDP 111 ({interface})")
        self.events.emit("rpc", "portmap",
                         f"no rpcbind on this host: running our own portmapper on UDP 111 ({interface})")
        if nfs_mode == "system":
            self.portmap = ("warn", "own portmapper, but the system NFS server needs rpcbind")
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


def _report_catalog(catalog: Catalog, config: Optional[DhcpConfig], events: EventBus) -> None:
    if not catalog.images:
        events.emit("catalog", "empty", f"no images in {catalog.images_root}", level=30)
    for image in catalog.images:
        problems = image.problems()
        if config is not None and root_path_for(image, config) is None:
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
        unusable = catalog.unusable_default(arch)
        if unusable:
            instead = f"using {default.name!r} instead" if default else "no other image boots it"
            events.emit("catalog", "default",
                        f"default for {arch} is not usable: {unusable}; {instead}. "
                        "Pick a default on the Images page or change --default.", level=30)
        elif default:
            events.emit("catalog", "default", f"default for {arch}: {default.name!r}")

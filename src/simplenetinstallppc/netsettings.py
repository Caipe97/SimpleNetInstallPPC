"""The network settings the web UI can change: interface and DHCP.

They're saved in the store (``settings`` table, key ``network``). At start-up
the saved settings are used, except for options given on the ``serve``
command line, which win and are saved in turn. So ``serve`` with no network
options runs with whatever was last applied, from the UI or the command line.
"""

from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .netif import Interface

IPv4 = ipaddress.IPv4Address
MODES = ("bsdp-only", "full")
CLIENTS = ("netboot", "all")
STORE_KEY = "network"


class SettingsError(ValueError):
    def __init__(self, message: str, probe_refusal: bool = False) -> None:
        super().__init__(message)
        self.probe_refusal = probe_refusal   # another DHCP server conflicts; can be overridden


@dataclass
class NetworkSettings:
    interface: Optional[str] = None
    server_ip: Optional[IPv4] = None     # which address, if the interface has several
    dhcp_mode: str = "bsdp-only"
    dhcp_clients: str = "netboot"
    pool_start: Optional[IPv4] = None
    pool_end: Optional[IPv4] = None
    router: Optional[IPv4] = None
    dns: List[IPv4] = field(default_factory=list)
    lease_time: int = 3600

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, value in asdict(self).items():
            if isinstance(value, list):
                out[key] = [str(v) for v in value]
            elif isinstance(value, IPv4):
                out[key] = str(value)
            else:
                out[key] = value
        return out

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "NetworkSettings":
        """Parse settings from the UI or the store; raises SettingsError."""
        def ip(key: str) -> Optional[IPv4]:
            value = data.get(key)
            if value in (None, ""):
                return None
            try:
                return IPv4(str(value).strip())
            except ValueError:
                raise SettingsError(f"{key.replace('_', ' ')}: {value!r} is not an IPv4 address")
        interface = data.get("interface") or None
        if interface is not None and (not isinstance(interface, str) or len(interface) > 32):
            raise SettingsError("interface: not an interface name")
        dns_raw = data.get("dns") or []
        if isinstance(dns_raw, str):
            dns_raw = [part for part in dns_raw.replace(",", " ").split() if part]
        dns = []
        for value in dns_raw:
            try:
                dns.append(IPv4(str(value).strip()))
            except ValueError:
                raise SettingsError(f"DNS: {value!r} is not an IPv4 address")
        try:
            lease_time = int(data.get("lease_time") or 3600)
        except (TypeError, ValueError):
            raise SettingsError("lease time must be a number of seconds")
        settings = cls(interface=interface, server_ip=ip("server_ip"),
                       dhcp_mode=str(data.get("dhcp_mode") or "bsdp-only"),
                       dhcp_clients=str(data.get("dhcp_clients") or "netboot"),
                       pool_start=ip("pool_start"), pool_end=ip("pool_end"),
                       router=ip("router"), dns=dns, lease_time=lease_time)
        if settings.dhcp_mode not in MODES:
            raise SettingsError(f"DHCP mode must be one of {', '.join(MODES)}")
        if settings.dhcp_clients not in CLIENTS:
            raise SettingsError(f"DHCP clients must be one of {', '.join(CLIENTS)}")
        if not 60 <= settings.lease_time <= 7 * 24 * 3600:
            raise SettingsError("lease time must be 60 s to 7 days")
        return settings


FIELD_NAMES = tuple(f.name for f in fields(NetworkSettings))


def merge(saved: Optional[NetworkSettings], given: Dict[str, Any]) -> NetworkSettings:
    """Saved settings with the command-line values in ``given`` on top."""
    base = saved or NetworkSettings()
    values = {name: getattr(base, name) for name in FIELD_NAMES}
    values.update({k: v for k, v in given.items() if k in FIELD_NAMES})
    return NetworkSettings(**values)


def check(settings: NetworkSettings, interfaces: Sequence[Interface]
          ) -> Tuple[Interface, ipaddress.IPv4Interface]:
    """The interface and address to serve on; raises SettingsError if they won't work."""
    if not settings.interface:
        raise SettingsError("choose the network interface the Macs are on")
    iface = next((i for i in interfaces if i.name == settings.interface), None)
    if iface is None:
        raise SettingsError(f"there's no network interface {settings.interface} on this host")
    if settings.server_ip is not None:
        address = next((a for a in iface.addresses if a.ip == settings.server_ip), None)
        if address is None:
            raise SettingsError(f"{settings.server_ip} is not configured on {iface.name}")
    else:
        address = iface.primary
        if address is None:
            raise SettingsError(f"interface {iface.name} has no IPv4 address")
    if settings.dhcp_mode == "full":
        if settings.pool_start is None or settings.pool_end is None:
            raise SettingsError("full DHCP mode needs the first and last pool address")
        for ip in (settings.pool_start, settings.pool_end):
            if ip not in address.network:
                raise SettingsError(f"pool address {ip} is outside {address.network}")
        if int(settings.pool_start) > int(settings.pool_end):
            raise SettingsError("the pool's first address is after its last")
        if settings.pool_start <= address.ip <= settings.pool_end:
            raise SettingsError(f"the pool includes this server's own address {address.ip}")
        if settings.router is not None and settings.router not in address.network:
            raise SettingsError(f"router {settings.router} is outside {address.network}")
    return iface, address

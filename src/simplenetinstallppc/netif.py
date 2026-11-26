"""Network interface discovery and per-interface socket binding.

Uses only the standard library: ``ip -j addr`` on Linux, ``ifconfig -a`` on
macOS.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from typing import List, Optional

# Linux: SOL_SOCKET / SO_BINDTODEVICE.  macOS: IPPROTO_IP / IP_BOUND_IF.
# Both happen to be 25.
_SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)
_IP_BOUND_IF = 25


@dataclass
class Interface:
    name: str
    mac: Optional[str] = None
    up: bool = False
    addresses: List[ipaddress.IPv4Interface] = field(default_factory=list)

    @property
    def primary(self) -> Optional[ipaddress.IPv4Interface]:
        return self.addresses[0] if self.addresses else None


def list_interfaces() -> List[Interface]:
    if sys.platform.startswith("linux"):
        out = subprocess.run(["ip", "-j", "addr", "show"], capture_output=True,
                             text=True, check=True).stdout
        return parse_ip_json(out)
    out = subprocess.run(["ifconfig", "-a"], capture_output=True, text=True,
                         check=True).stdout
    return parse_ifconfig(out)


def get_interface(name: str) -> Interface:
    for iface in list_interfaces():
        if iface.name == name:
            return iface
    raise LookupError(f"no such network interface: {name}")


def parse_ip_json(text: str) -> List[Interface]:
    result = []
    for entry in json.loads(text):
        iface = Interface(
            name=entry["ifname"],
            mac=entry.get("address") if entry.get("link_type") == "ether" else None,
            up="UP" in entry.get("flags", []),
        )
        for info in entry.get("addr_info", []):
            if info.get("family") == "inet":
                iface.addresses.append(
                    ipaddress.IPv4Interface(f"{info['local']}/{info['prefixlen']}"))
        result.append(iface)
    return result


_IFCONFIG_HEADER = re.compile(r"^(\S+?):\s+flags=\w+<([^>]*)>")
_IFCONFIG_INET = re.compile(r"^\s+inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+)")
_IFCONFIG_ETHER = re.compile(r"^\s+ether ([0-9a-fA-F:]{17})")


def parse_ifconfig(text: str) -> List[Interface]:
    result: List[Interface] = []
    current: Optional[Interface] = None
    for line in text.splitlines():
        header = _IFCONFIG_HEADER.match(line)
        if header:
            current = Interface(name=header.group(1),
                                up="UP" in header.group(2).split(","))
            result.append(current)
            continue
        if current is None:
            continue
        inet = _IFCONFIG_INET.match(line)
        if inet:
            prefix = bin(int(inet.group(2), 16)).count("1")
            current.addresses.append(
                ipaddress.IPv4Interface(f"{inet.group(1)}/{prefix}"))
            continue
        ether = _IFCONFIG_ETHER.match(line)
        if ether:
            current.mac = ether.group(1).lower()
    return result


def bind_to_interface(sock: socket.socket, name: str) -> None:
    """Restrict a socket to one interface (send and receive)."""
    if sys.platform.startswith("linux"):
        sock.setsockopt(socket.SOL_SOCKET, _SO_BINDTODEVICE, name.encode() + b"\0")
    elif sys.platform == "darwin":
        sock.setsockopt(socket.IPPROTO_IP, _IP_BOUND_IF,
                        struct.pack("I", socket.if_nametoindex(name)))
    else:
        raise OSError(f"binding to an interface is not supported on {sys.platform}")


def make_udp_socket(bind_ip: str, port: int, interface: Optional[str] = None,
                    broadcast: bool = False) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if broadcast:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if interface:
            bind_to_interface(sock, interface)
        sock.bind((bind_ip, port))
        sock.setblocking(False)
    except Exception:
        sock.close()
        raise
    return sock

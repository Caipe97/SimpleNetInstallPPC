"""Look for other DHCP servers on the NetBoot interface (DHCP safety).

The probe broadcasts a DHCPDISCOVER with the host's own MAC address and
collects the OFFERs that come back. It never sends a REQUEST, so no lease is
taken. ``assess`` turns the answers into startup messages: other servers are
normal next to a NetBoot-only full mode, and required for BSDP-only mode, but
a server offering addresses inside our pool, or any other server when we
answer every client, stops the start-up unless the user overrides it.
"""

from __future__ import annotations

import ipaddress
import os
import select
import socket
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple

from ..netif import bind_to_interface
from .portmap import parse_ipv4_udp
from .dhcp_packet import (BOOTREPLY, BOOTREQUEST, FLAG_BROADCAST, DhcpPacket, MessageType,
                          Opt)

IPv4 = ipaddress.IPv4Address
PROBE_SECONDS = 3.0


class ProbeUnavailable(Exception):
    """The probe couldn't listen for answers (usually: port 68 is taken)."""


@dataclass
class DhcpAnswer:
    server: IPv4                  # server identifier (option 54), else the source address
    source: str                   # address the OFFER came from
    offered: Optional[IPv4] = None
    router: Optional[IPv4] = None
    dns: List[IPv4] = field(default_factory=list)

    def describe(self) -> str:
        parts = [f"offered {self.offered}"] if self.offered else []
        if self.router:
            parts.append(f"router {self.router}")
        if self.dns:
            parts.append("DNS " + ", ".join(map(str, self.dns)))
        return f"{self.server}" + (f" ({', '.join(parts)})" if parts else "")


def build_discover(mac: bytes, xid: int) -> DhcpPacket:
    packet = DhcpPacket(op=BOOTREQUEST, xid=xid, flags=FLAG_BROADCAST, chaddr=mac)
    packet.options[Opt.MESSAGE_TYPE] = bytes((MessageType.DISCOVER,))
    packet.options[Opt.PARAMETER_REQUEST] = bytes((Opt.SUBNET_MASK, Opt.ROUTER, Opt.DNS,
                                                   Opt.LEASE_TIME, Opt.SERVER_ID))
    return packet


def _ips(raw: Optional[bytes]) -> List[IPv4]:
    if not raw:
        return []
    return [IPv4(raw[i:i + 4]) for i in range(0, len(raw) - len(raw) % 4, 4)]


def parse_answer(data: bytes, xid: int, source: str) -> Optional[DhcpAnswer]:
    try:
        packet = DhcpPacket.decode(data)
    except ValueError:
        return None
    if packet.op != BOOTREPLY or packet.xid != xid or packet.message_type != MessageType.OFFER:
        return None
    server = packet.option_ip(Opt.SERVER_ID) or IPv4(source)
    routers = _ips(packet.options.get(Opt.ROUTER))
    return DhcpAnswer(server=server, source=source,
                      offered=packet.yiaddr if int(packet.yiaddr) else None,
                      router=routers[0] if routers else None,
                      dns=_ips(packet.options.get(Opt.DNS)))


def _listen(interface: Optional[str], port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            # Share port 68 with the host's own DHCP client where it allows that.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if interface:
            bind_to_interface(sock, interface)
        sock.bind(("0.0.0.0", port))
    except OSError as exc:
        sock.close()
        raise ProbeUnavailable(f"can't listen on UDP {port} for DHCP offers "
                               f"({exc.strerror or exc})") from exc
    return sock


ETH_P_IP = 0x0800


def _packet_socket(interface: Optional[str]) -> Optional[socket.socket]:
    """Linux: a packet socket on the interface, which sees the offers before the
    firewall does. Fedora's default zone, for one, drops UDP 68, so a plain
    socket would hear nothing (the host's DHCP client uses a packet socket too)."""
    if not interface or not hasattr(socket, "AF_PACKET"):
        return None
    try:
        raw = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM,  # type: ignore[attr-defined]
                            socket.htons(ETH_P_IP))
        raw.bind((interface, ETH_P_IP))
    except OSError:
        return None
    return raw


def probe(interface: Optional[str], mac: str, *, ignore: Iterable[str] = (),
          timeout: float = PROBE_SECONDS, client_port: int = 68, server_port: int = 67,
          target: str = "255.255.255.255") -> List[DhcpAnswer]:
    """Broadcast DHCPDISCOVERs and return one answer per server (blocking)."""
    chaddr = bytes.fromhex(mac.replace(":", ""))
    xid = int.from_bytes(os.urandom(4), "big")
    ignored = set(ignore)
    sock = _listen(interface, client_port)
    raw = _packet_socket(interface)
    readers = [sock] + ([raw] if raw else [])
    answers: List[DhcpAnswer] = []
    try:
        data = build_discover(chaddr, xid).encode()
        # A second DISCOVER halfway through, as clients retry; some servers
        # skip the first one from a MAC they haven't seen.
        sends = [0.0, timeout / 2]
        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            while sends and sends[0] <= elapsed:
                sends.pop(0)
                sock.sendto(data, (target, server_port))
            if elapsed >= timeout:
                break
            wait = min(timeout, sends[0] if sends else timeout) - elapsed
            readable, _, _ = select.select(readers, [], [], max(wait, 0.0))
            for reader in readable:
                if reader is raw:
                    parsed = parse_ipv4_udp(raw.recv(65535))
                    if parsed is None or parsed[3] != client_port:
                        continue
                    source, reply = parsed[0], parsed[4]
                else:
                    reply, (source, _port) = sock.recvfrom(4096)
                if source in ignored:
                    continue
                answer = parse_answer(reply, xid, source)
                if answer and all(a.server != answer.server for a in answers):
                    answers.append(answer)
    finally:
        sock.close()
        if raw:
            raw.close()
    return answers


def assess(answers: List[DhcpAnswer], mode: str, clients: str,
           pool: Optional[Tuple[IPv4, IPv4]] = None) -> Tuple[List[Tuple[int, str]], Optional[str]]:
    """Turn probe answers into (level, message) lines, plus a reason to refuse
    to start (None if the start may go ahead)."""
    lines: List[Tuple[int, str]] = []
    others = ", ".join(a.describe() for a in answers)
    if mode == "bsdp-only":
        if answers:
            lines.append((20, f"DHCP server(s) on this network: {others}"))
        else:
            lines.append((30, "no DHCP server answered: in BSDP-only mode the Macs need one "
                              "to get an address (use --dhcp-mode full on a network without one)"))
        return lines, None
    if not answers:
        lines.append((20, "no other DHCP server answered"))
        return lines, None
    if clients == "all":
        return lines, (f"another DHCP server is on this network: {others}. With "
                       "--dhcp-clients all both would answer every device. Use "
                       "--dhcp-clients netboot to answer only NetBoot Macs")
    lines.append((20, f"other DHCP server(s) on this network: {others}; answering "
                      "NetBoot Macs only"))
    if pool:
        start, end = pool
        inside = [a for a in answers if a.offered and int(start) <= int(a.offered) <= int(end)]
        if inside:
            names = ", ".join(f"{a.server} offered {a.offered}" for a in inside)
            return lines, (f"our pool {start}-{end} overlaps another DHCP server's ({names}). "
                           "Pick a pool outside its range")
    return lines, None

"""Minimal reader for classic pcap files (Ethernet, IPv4, UDP only), for test fixtures."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import List

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class UdpPacket:
    time: float
    src_mac: str
    dst_mac: str
    src: str
    sport: int
    dst: str
    dport: int
    payload: bytes


def _mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


def _ip(raw: bytes) -> str:
    return ".".join(str(b) for b in raw)


def read_udp(path: Path) -> List[UdpPacket]:
    data = path.read_bytes()
    magic = data[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        endian = "<"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        endian = ">"
    else:
        raise ValueError(f"{path.name}: not a classic pcap file")
    nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    linktype = struct.unpack_from(endian + "I", data, 20)[0]
    if linktype != 1:
        raise ValueError(f"{path.name}: link type {linktype}, expected Ethernet")
    packets, offset = [], 24
    while offset + 16 <= len(data):
        sec, frac, caplen, _origlen = struct.unpack_from(endian + "IIII", data, offset)
        frame = data[offset + 16:offset + 16 + caplen]
        offset += 16 + caplen
        if len(frame) < 14 + 20 + 8 or frame[12:14] != b"\x08\x00":
            continue
        ip = frame[14:]
        ihl = (ip[0] & 0x0F) * 4
        if ip[9] != 17:
            continue
        total = struct.unpack_from(">H", ip, 2)[0]
        udp = ip[ihl:total]
        sport, dport, length = struct.unpack_from(">HHH", udp)
        packets.append(UdpPacket(
            time=sec + frac / (1e9 if nano else 1e6),
            src_mac=_mac(frame[6:12]), dst_mac=_mac(frame[0:6]),
            src=_ip(ip[12:16]), sport=sport, dst=_ip(ip[16:20]), dport=dport,
            payload=udp[8:length]))
    return packets

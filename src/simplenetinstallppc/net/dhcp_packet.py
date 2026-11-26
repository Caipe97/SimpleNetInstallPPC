"""DHCP / BOOTP packet encoding (RFC 2131, RFC 2132, RFC 3396)."""

from __future__ import annotations

import ipaddress
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, List, Optional

MAGIC_COOKIE = b"\x63\x82\x53\x63"
BOOTREQUEST = 1
BOOTREPLY = 2
FLAG_BROADCAST = 0x8000
_HEADER = struct.Struct("!BBBBIHH4s4s4s4s16s64s128s")   # 236 bytes
MIN_PACKET_SIZE = 300   # BOOTP minimum; some firmware drops shorter replies
ZERO_IP = ipaddress.IPv4Address(0)


class MessageType(IntEnum):
    DISCOVER = 1
    OFFER = 2
    REQUEST = 3
    DECLINE = 4
    ACK = 5
    NAK = 6
    RELEASE = 7
    INFORM = 8


class Opt(IntEnum):
    PAD = 0
    SUBNET_MASK = 1
    ROUTER = 3
    DNS = 6
    HOSTNAME = 12
    ROOT_PATH = 17
    BROADCAST_ADDRESS = 28
    VENDOR_SPECIFIC = 43
    REQUESTED_IP = 50
    LEASE_TIME = 51
    OVERLOAD = 52
    MESSAGE_TYPE = 53
    SERVER_ID = 54
    PARAMETER_REQUEST = 55
    MESSAGE = 56
    MAX_MESSAGE_SIZE = 57
    RENEWAL_TIME = 58
    REBINDING_TIME = 59
    VENDOR_CLASS = 60
    CLIENT_ID = 61
    END = 255


@dataclass
class DhcpPacket:
    op: int = BOOTREPLY
    htype: int = 1
    hlen: int = 6
    hops: int = 0
    xid: int = 0
    secs: int = 0
    flags: int = 0
    ciaddr: ipaddress.IPv4Address = ZERO_IP
    yiaddr: ipaddress.IPv4Address = ZERO_IP
    siaddr: ipaddress.IPv4Address = ZERO_IP
    giaddr: ipaddress.IPv4Address = ZERO_IP
    chaddr: bytes = b"\0" * 16
    sname: bytes = b""
    file: bytes = b""
    # Insertion order is kept when encoding. Repeated options are already
    # concatenated (RFC 3396) when decoding.
    options: Dict[int, bytes] = field(default_factory=dict)

    # -- convenience -------------------------------------------------------
    @property
    def mac(self) -> str:
        return ":".join(f"{b:02x}" for b in self.chaddr[:self.hlen])

    @property
    def message_type(self) -> Optional[MessageType]:
        raw = self.options.get(Opt.MESSAGE_TYPE)
        if not raw:
            return None
        try:
            return MessageType(raw[0])
        except ValueError:
            return None

    def option_ip(self, code: int) -> Optional[ipaddress.IPv4Address]:
        raw = self.options.get(code)
        if raw is None or len(raw) < 4:
            return None
        return ipaddress.IPv4Address(raw[:4])

    # -- decoding ------------------------------------------------------------
    @classmethod
    def decode(cls, data: bytes) -> "DhcpPacket":
        if len(data) < _HEADER.size:
            raise ValueError(f"packet too short ({len(data)} bytes)")
        (op, htype, hlen, hops, xid, secs, flags, ciaddr, yiaddr, siaddr, giaddr,
         chaddr, sname, file) = _HEADER.unpack_from(data)
        packet = cls(op=op, htype=htype, hlen=min(hlen, 16), hops=hops, xid=xid,
                     secs=secs, flags=flags,
                     ciaddr=ipaddress.IPv4Address(ciaddr),
                     yiaddr=ipaddress.IPv4Address(yiaddr),
                     siaddr=ipaddress.IPv4Address(siaddr),
                     giaddr=ipaddress.IPv4Address(giaddr),
                     chaddr=chaddr, sname=sname.split(b"\0", 1)[0],
                     file=file.split(b"\0", 1)[0])
        rest = data[_HEADER.size:]
        if rest[:4] == MAGIC_COOKIE:
            _decode_option_area(rest[4:], packet.options)
            overload = packet.options.get(Opt.OVERLOAD, b"\0")[0]
            if overload & 1:
                packet.file = b""
                _decode_option_area(file, packet.options)
            if overload & 2:
                packet.sname = b""
                _decode_option_area(sname, packet.options)
        return packet

    # -- encoding ------------------------------------------------------------
    def encode(self) -> bytes:
        if len(self.sname) > 63 or len(self.file) > 127:
            raise ValueError("sname/file field too long")
        header = _HEADER.pack(self.op, self.htype, self.hlen, self.hops, self.xid,
                              self.secs, self.flags, self.ciaddr.packed,
                              self.yiaddr.packed, self.siaddr.packed,
                              self.giaddr.packed, self.chaddr.ljust(16, b"\0")[:16],
                              self.sname, self.file)
        body = bytearray(MAGIC_COOKIE)
        for code, value in self.options.items():
            # RFC 3396: options longer than 255 bytes are split into several.
            for start in range(0, max(len(value), 1), 255):
                chunk = value[start:start + 255]
                body += bytes((int(code), len(chunk))) + chunk
        body.append(Opt.END)
        packet = header + bytes(body)
        if len(packet) < MIN_PACKET_SIZE:
            packet += b"\0" * (MIN_PACKET_SIZE - len(packet))
        return packet

    def make_reply(self, message_type: MessageType, server_ip: ipaddress.IPv4Address) -> "DhcpPacket":
        reply = DhcpPacket(op=BOOTREPLY, htype=self.htype, hlen=self.hlen, xid=self.xid,
                           flags=self.flags, giaddr=self.giaddr, chaddr=self.chaddr)
        reply.options[Opt.MESSAGE_TYPE] = bytes((message_type,))
        reply.options[Opt.SERVER_ID] = server_ip.packed
        return reply


def _decode_option_area(data: bytes, options: Dict[int, bytes]) -> None:
    i = 0
    while i < len(data):
        code = data[i]
        if code == Opt.PAD:
            i += 1
            continue
        if code == Opt.END or i + 1 >= len(data):
            return
        length = data[i + 1]
        value = data[i + 2:i + 2 + length]
        if len(value) != length:
            raise ValueError(f"truncated DHCP option {code}")
        options[code] = options.get(code, b"") + value
        i += 2 + length


def ip_list(addresses: List[ipaddress.IPv4Address]) -> bytes:
    return b"".join(a.packed for a in addresses)

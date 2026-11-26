from __future__ import annotations

import ipaddress
import plistlib
from pathlib import Path
from typing import Iterable

from simplenetinstallppc.events import EventBus
from simplenetinstallppc.net import bsdp
from simplenetinstallppc.net.dhcp_packet import BOOTREQUEST, DhcpPacket, MessageType, Opt

SERVER_IP = ipaddress.IPv4Address("10.0.0.1")
CLIENT_MAC = bytes.fromhex("000a95aabbcc")


def make_image(root: Path, index: int, name: str, archs: Iterable[str] = ("ppc",),
               protocol: str = "HTTP", install: bool = False, dir_name: str = "",
               **extra) -> Path:
    directory = root / (dir_name or f"img{index}")
    directory.mkdir(parents=True)
    for arch in archs:
        (directory / arch).mkdir()
        (directory / arch / "booter").write_bytes(b"BOOTER-" + arch.encode())
        (directory / arch / "mach.macosx").write_bytes(b"KERNEL")
    (directory / "root.dmg").write_bytes(b"\0" * 4096)
    info = {"Index": index, "Name": name, "Architectures": list(archs), "RootPath": "root.dmg",
            "Type": protocol, "IsInstall": install, "Kind": 1, "IsEnabled": True}
    info.update(extra)
    with open(directory / "NBImageInfo.plist", "wb") as fh:
        plistlib.dump(info, fh)
    return directory


def bsdp_inform(message: int, arch: str = "ppc", model: str = "PowerMac3,4",
                extra: Iterable = (), ciaddr: str = "10.0.0.50") -> DhcpPacket:
    packet = DhcpPacket(op=BOOTREQUEST, xid=0x1234, ciaddr=ipaddress.IPv4Address(ciaddr),
                        chaddr=CLIENT_MAC)
    packet.options[Opt.MESSAGE_TYPE] = bytes((MessageType.INFORM,))
    packet.options[Opt.VENDOR_CLASS] = f"AAPLBSDPC/{arch}/{model}".encode()
    items = [(bsdp.BsdpTag.MESSAGE_TYPE, bytes((message,))),
             (bsdp.BsdpTag.VERSION, bsdp.u16(bsdp.BSDP_VERSION_1_1))] + list(extra)
    packet.options[Opt.VENDOR_SPECIFIC] = bsdp.encode_options(items)
    return packet


class RecordingBus(EventBus):
    def kinds(self):
        return [(e.subsystem, e.kind) for e in self.recent(10000)]

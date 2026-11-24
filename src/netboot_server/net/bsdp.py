"""Boot Service Discovery Protocol (BSDP) encoding.

BSDP is Apple's NetBoot discovery protocol, carried inside DHCP INFORM/ACK
packets. Clients identify themselves with vendor class (option 60)
``AAPLBSDPC/<arch>/<model>``, and the BSDP fields travel as TLVs inside
DHCP option 43.

Reference: Apple's open-source ``bootp`` project (bsdp.h, bsdpd.c).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

VENDOR_CLASS = "AAPLBSDPC"
BSDP_VERSION_1_0 = 0x0100
BSDP_VERSION_1_1 = 0x0101


class BsdpTag(IntEnum):
    MESSAGE_TYPE = 1
    VERSION = 2
    SERVER_IDENTIFIER = 3
    SERVER_PRIORITY = 4
    REPLY_PORT = 5
    BOOT_IMAGE_LIST_PATH = 6      # unused by clients
    DEFAULT_BOOT_IMAGE = 7
    SELECTED_BOOT_IMAGE = 8
    BOOT_IMAGE_LIST = 9
    NETBOOT_1_0_FIRMWARE = 10
    BOOT_IMAGE_ATTRIBUTES_FILTER = 11
    MAX_MESSAGE_SIZE = 12


class BsdpMessageType(IntEnum):
    LIST = 1
    SELECT = 2
    FAILED = 3


class ImageKind(IntEnum):
    MACOS9 = 0
    MACOSX = 1
    MACOSX_SERVER = 2
    DIAGNOSTICS = 3


ATTR_INSTALL = 0x8000
ATTR_KIND_MASK = 0x7F00
INDEX_LOCAL_MAX = 0x0FFF   # 1..4095 are local to one server; 4096+ are shared


def make_image_id(index: int, kind: int = ImageKind.MACOSX, install: bool = False) -> int:
    if not 1 <= index <= 0xFFFF:
        raise ValueError(f"image index out of range: {index}")
    attributes = (ATTR_INSTALL if install else 0) | ((int(kind) << 8) & ATTR_KIND_MASK)
    return (attributes << 16) | index


def split_image_id(image_id: int) -> Tuple[int, int, bool]:
    """Return (index, kind, install) for a 32-bit image ID."""
    attributes = (image_id >> 16) & 0xFFFF
    return image_id & 0xFFFF, (attributes & ATTR_KIND_MASK) >> 8, bool(attributes & ATTR_INSTALL)


@dataclass(frozen=True)
class ClientClass:
    """Parsed ``AAPLBSDPC/<arch>/<model>`` vendor class identifier."""
    arch: Optional[str]     # "ppc" or "i386"
    model: Optional[str]    # e.g. "PowerMac3,1"

    @classmethod
    def parse(cls, vendor_class: Optional[bytes]) -> Optional["ClientClass"]:
        if not vendor_class:
            return None
        text = vendor_class.decode("ascii", "replace")
        parts = text.split("/")
        if parts[0] != VENDOR_CLASS:
            return None
        arch = parts[1] if len(parts) > 1 and parts[1] else None
        model = parts[2] if len(parts) > 2 and parts[2] else None
        return cls(arch, model)


def decode_options(data: bytes) -> Dict[int, bytes]:
    """Decode BSDP TLVs (1-byte tag, 1-byte length). Tag 0 is padding, 255 ends."""
    result: Dict[int, bytes] = {}
    i = 0
    while i < len(data):
        tag = data[i]
        if tag == 0:
            i += 1
            continue
        if tag == 255 or i + 1 >= len(data):
            break
        length = data[i + 1]
        value = data[i + 2:i + 2 + length]
        if len(value) != length:
            raise ValueError("truncated BSDP option")
        result[tag] = result.get(tag, b"") + value
        i += 2 + length
    return result


def encode_options(items: Iterable[Tuple[int, bytes]]) -> bytes:
    out = bytearray()
    for tag, value in items:
        if len(value) > 255:
            raise ValueError(f"BSDP option {tag} too long ({len(value)} bytes)")
        out += bytes((int(tag), len(value))) + value
    return bytes(out)


def encode_image_list(images: Sequence[Tuple[int, str]], budget: int) -> Tuple[bytes, int]:
    """Encode boot image list entries, stopping before ``budget`` bytes.

    Each entry is image ID (4) + name length (1) + name (Mac Roman).
    Returns (payload, number_of_images_included).
    """
    out = bytearray()
    count = 0
    for image_id, name in images:
        raw_name = name.encode("mac_roman", "replace")[:255]
        entry = struct.pack("!IB", image_id, len(raw_name)) + raw_name
        if len(out) + len(entry) > budget:
            break
        out += entry
        count += 1
    return bytes(out), count


def decode_image_list(data: bytes) -> List[Tuple[int, str]]:
    result = []
    i = 0
    while i + 5 <= len(data):
        image_id, length = struct.unpack_from("!IB", data, i)
        name = data[i + 5:i + 5 + length].decode("mac_roman", "replace")
        result.append((image_id, name))
        i += 5 + length
    return result


def u16(value: int) -> bytes:
    return struct.pack("!H", value)


def u32(value: int) -> bytes:
    return struct.pack("!I", value)

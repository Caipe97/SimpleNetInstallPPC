"""Find HFS / HFS+ volumes inside raw disk images.

Handles Apple Partition Map (CDs, PPC disks), GPT (Intel disks), MBR, bare
volumes, and HFS+ volumes embedded in an HFS wrapper (common on 10.0-10.2
era media).
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, List, Optional

HFS_PLUS_GPT_TYPE = uuid.UUID("48465300-0000-11AA-AA11-00306543ECAC")
JOURNALED_BIT = 1 << 13


class UnsupportedImage(Exception):
    pass


@dataclass(frozen=True)
class Volume:
    offset: int          # byte offset of the volume inside the image file
    length: int          # byte length (0 if unknown)
    kind: str            # "hfs+", "hfsx", "hfs", "hfs-wrapper" (HFS wrapping an HFS+ volume)
    scheme: str          # "apm", "gpt", "mbr", "none", or "embedded"
    name: str = ""       # partition name or HFS volume name
    journaled: bool = False

    @property
    def is_hfs_plus(self) -> bool:
        return self.kind in ("hfs+", "hfsx")


def _read(fh: BinaryIO, offset: int, size: int) -> bytes:
    fh.seek(offset)
    return fh.read(size)


def probe_volume(fh: BinaryIO, offset: int, scheme: str, name: str = "",
                 length: int = 0) -> Optional[Volume]:
    """Return a Volume if an HFS-family volume header is found at ``offset``."""
    header = _read(fh, offset + 1024, 512)
    if len(header) < 512:
        return None
    signature = header[:2]
    if signature in (b"H+", b"HX"):
        attributes, = struct.unpack_from(">I", header, 4)
        block_size, total_blocks = struct.unpack_from(">II", header, 40)
        return Volume(offset, length or block_size * total_blocks,
                      "hfs+" if signature == b"H+" else "hfsx", scheme, name,
                      bool(attributes & JOURNALED_BIT))
    if signature == b"BD":
        name_length = header[0x24]
        hfs_name = header[0x25:0x25 + min(name_length, 27)].decode("mac_roman", "replace")
        kind = "hfs-wrapper" if header[0x7C:0x7E] == b"H+" else "hfs"
        if not length:
            alloc_blocks, = struct.unpack_from(">H", header, 0x12)
            block_size, = struct.unpack_from(">I", header, 0x14)
            first_block, = struct.unpack_from(">H", header, 0x1C)
            length = first_block * 512 + alloc_blocks * block_size + 1024
        return Volume(offset, length, kind, scheme, name or hfs_name)
    return None


def unwrap(fh: BinaryIO, volume: Volume) -> Volume:
    """For an HFS wrapper, return the HFS+ volume embedded in it."""
    if volume.kind != "hfs-wrapper":
        return volume
    mdb = _read(fh, volume.offset + 1024, 512)
    block_size, = struct.unpack_from(">I", mdb, 0x14)
    first_block, = struct.unpack_from(">H", mdb, 0x1C)
    embed_start, embed_count = struct.unpack_from(">HH", mdb, 0x7E)
    offset = volume.offset + first_block * 512 + embed_start * block_size
    inner = probe_volume(fh, offset, "embedded", volume.name, embed_count * block_size)
    if inner is None or not inner.is_hfs_plus:
        raise UnsupportedImage("HFS wrapper does not contain a readable HFS+ volume")
    return replace(inner, name=volume.name)


def _apm_volumes(fh: BinaryIO) -> List[Volume]:
    block0 = _read(fh, 0, 512)
    device_block = struct.unpack_from(">H", block0, 2)[0] if block0[:2] == b"ER" else 512
    strides = [512] + ([device_block] if device_block not in (0, 512) else [])
    for stride in strides:
        first = _read(fh, stride, 512)
        if first[:2] != b"PM":
            continue
        count, = struct.unpack_from(">I", first, 4)
        volumes = []
        for i in range(1, min(count, 256) + 1):
            entry = _read(fh, i * stride, 512)
            if entry[:2] != b"PM":
                break
            start, blocks = struct.unpack_from(">II", entry, 8)
            name = entry[16:48].split(b"\0", 1)[0].decode("mac_roman", "replace")
            ptype = entry[48:80].split(b"\0", 1)[0].decode("ascii", "replace")
            if ptype not in ("Apple_HFS", "Apple_HFSX"):
                continue
            # Start/size are in 512-byte blocks on disks, but some CDs use the
            # device block size, so try both and keep whichever finds a header.
            for unit in dict.fromkeys((512, stride, device_block)):
                volume = probe_volume(fh, start * unit, "apm", name, blocks * unit)
                if volume:
                    volumes.append(volume)
                    break
        return volumes
    return []


def _gpt_volumes(fh: BinaryIO) -> List[Volume]:
    header = _read(fh, 512, 512)
    if header[:8] != b"EFI PART":
        return []
    entries_lba, = struct.unpack_from("<Q", header, 72)
    count, entry_size = struct.unpack_from("<II", header, 80)
    volumes = []
    table = _read(fh, entries_lba * 512, min(count, 256) * entry_size)
    for i in range(min(count, 256)):
        entry = table[i * entry_size:(i + 1) * entry_size]
        if len(entry) < 128:
            break
        if uuid.UUID(bytes_le=entry[:16]) != HFS_PLUS_GPT_TYPE:
            continue
        first, last = struct.unpack_from("<QQ", entry, 32)
        name = entry[56:128].decode("utf-16-le", "replace").rstrip("\0")
        volume = probe_volume(fh, first * 512, "gpt", name, (last - first + 1) * 512)
        if volume:
            volumes.append(volume)
    return volumes


def _mbr_volumes(fh: BinaryIO) -> List[Volume]:
    sector = _read(fh, 0, 512)
    if len(sector) < 512 or sector[510:512] != b"\x55\xaa":
        return []
    volumes = []
    for i in range(4):
        entry = sector[446 + 16 * i:462 + 16 * i]
        ptype = entry[4]
        start, blocks = struct.unpack_from("<II", entry, 8)
        if ptype == 0xAF and start:
            volume = probe_volume(fh, start * 512, "mbr", f"partition {i + 1}", blocks * 512)
            if volume:
                volumes.append(volume)
    return volumes


def is_udif(path: Path) -> bool:
    from .udif import is_udif as _is_udif
    return _is_udif(path)


def open_image(path: Path) -> BinaryIO:
    """The image's raw bytes: the file itself, or the disk inside a UDIF .dmg."""
    if is_udif(path):
        from .udif import UdifError, UdifFile
        try:
            return UdifFile(path)  # type: ignore[return-value]
        except UdifError as exc:
            raise UnsupportedImage(f"{path.name}: {exc}") from exc
    return open(path, "rb")


def find_volumes(path: Path) -> List[Volume]:
    """List HFS-family volumes in a raw image or a .dmg. HFS wrappers are unwrapped."""
    with open_image(path) as fh:
        volumes = _gpt_volumes(fh) or _apm_volumes(fh) or _mbr_volumes(fh)
        if not volumes:
            bare = probe_volume(fh, 0, "none")
            volumes = [bare] if bare else []
        return [unwrap(fh, v) for v in volumes]


def pick_volume(volumes: List[Volume], index: Optional[int] = None) -> Volume:
    candidates = [v for v in volumes if v.is_hfs_plus]
    if index is not None:
        if not 0 <= index < len(volumes):
            raise UnsupportedImage(f"there is no volume #{index}")
        chosen = volumes[index]
        if not chosen.is_hfs_plus:
            raise UnsupportedImage(f"volume #{index} is {chosen.kind}, not HFS+")
        return chosen
    if not candidates:
        raise UnsupportedImage("no HFS+ volume found (Mac OS X needs HFS+)")
    return max(candidates, key=lambda v: v.length)

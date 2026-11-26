"""Read Apple UDIF disk images (.dmg) without hdiutil.

A UDIF file is the image's data, chunk by chunk, followed by a 512-byte
``koly`` trailer. The trailer points at the block maps (``blkx``): one
``mish`` table per partition, listing for each run of sectors where its
bytes are and how they're stored (raw, zeros, zlib, bzip2, ADC, LZMA).
Images from Mac OS X 10.2 on keep the block maps in an XML property list
after the data. Earlier ones keep them in a classic resource fork inside the
file, which this reader handles too.

Only reading is needed: the image pipeline converts a .dmg to a raw image
once (``convert_to_raw``) and works on that.
"""

from __future__ import annotations

import bisect
import bz2
import io
import lzma
import plistlib
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, List, Optional

SECTOR = 512
KOLY_SIZE = 512

# Chunk types in a mish table.
ZERO_FILL = 0x00000000
RAW = 0x00000001
IGNORED = 0x00000002            # unallocated: reads as zeros
ADC = 0x80000004
ZLIB = 0x80000005
BZIP2 = 0x80000006
LZFSE = 0x80000007
LZMA = 0x80000008
COMMENT = 0x7FFFFFFE
TERMINATOR = 0xFFFFFFFF

MISH_HEADER = 0xCC              # 204 bytes before the chunk table
CHUNK_SIZE = 40


class UdifError(ValueError):
    pass


@dataclass
class Chunk:
    kind: int
    sector: int                 # first sector in the image
    sectors: int
    offset: int                 # absolute offset of the stored bytes in the file
    length: int                 # stored length


@dataclass
class Trailer:
    data_fork_offset: int
    data_fork_length: int
    rsrc_fork_offset: int
    rsrc_fork_length: int
    xml_offset: int
    xml_length: int
    sector_count: int


def is_udif(path: Path) -> bool:
    try:
        size = path.stat().st_size
        if size < KOLY_SIZE:
            return False
        with open(path, "rb") as fh:
            fh.seek(size - KOLY_SIZE)
            return fh.read(4) == b"koly"
    except OSError:
        return False


def read_trailer(fh: BinaryIO, size: int) -> Trailer:
    fh.seek(size - KOLY_SIZE)
    koly = fh.read(KOLY_SIZE)
    if len(koly) != KOLY_SIZE or koly[:4] != b"koly":
        raise UdifError("no UDIF trailer (koly)")
    u64 = lambda at: struct.unpack_from(">Q", koly, at)[0]   # noqa: E731
    return Trailer(data_fork_offset=u64(0x18), data_fork_length=u64(0x20),
                   rsrc_fork_offset=u64(0x28), rsrc_fork_length=u64(0x30),
                   xml_offset=u64(0xD8), xml_length=u64(0xE0), sector_count=u64(0x1EC))


def parse_mish(data: bytes, data_fork_offset: int) -> List[Chunk]:
    """The chunks of one block map, with absolute sectors and file offsets."""
    if len(data) < MISH_HEADER or data[:4] != b"mish":
        raise UdifError("block map without a mish header")
    first_sector, _count, data_offset = struct.unpack_from(">QQQ", data, 8)
    (count,) = struct.unpack_from(">I", data, 0xC8)
    if MISH_HEADER + count * CHUNK_SIZE > len(data):
        raise UdifError("block map shorter than its chunk count")
    chunks = []
    for i in range(count):
        kind, _comment, sector, sectors, offset, length = struct.unpack_from(
            ">IIQQQQ", data, MISH_HEADER + i * CHUNK_SIZE)
        if kind == TERMINATOR:
            break
        if kind == COMMENT:
            continue
        chunks.append(Chunk(kind, first_sector + sector, sectors,
                            data_fork_offset + data_offset + offset, length))
    return chunks


def resource_fork_blkx(fork: bytes) -> List[bytes]:
    """The 'blkx' resources of a classic Mac resource fork (pre-10.2 UDIF)."""
    if len(fork) < 16:
        raise UdifError("resource fork too short")
    data_start, map_start, _data_len, map_len = struct.unpack_from(">IIII", fork, 0)
    if map_start + map_len > len(fork) or map_start + 30 > len(fork):
        raise UdifError("resource map outside the fork")
    type_list = map_start + struct.unpack_from(">H", fork, map_start + 24)[0]
    (types_minus_one,) = struct.unpack_from(">H", fork, type_list)
    found = []
    for t in range(types_minus_one + 1):
        res_type, refs_minus_one, ref_list = struct.unpack_from(">4sHH", fork, type_list + 2 + 8 * t)
        if res_type != b"blkx":
            continue
        for r in range(refs_minus_one + 1):
            entry = type_list + ref_list + 12 * r
            attrs_and_offset = struct.unpack_from(">I", fork, entry + 4)[0]
            at = data_start + (attrs_and_offset & 0xFFFFFF)
            (length,) = struct.unpack_from(">I", fork, at)
            found.append(fork[at + 4:at + 4 + length])
    return found


def read_chunks(fh: BinaryIO, size: int) -> tuple:
    """(Trailer, chunks sorted by sector) for an open UDIF file."""
    trailer = read_trailer(fh, size)
    maps: List[bytes] = []
    if trailer.xml_offset and trailer.xml_length:
        fh.seek(trailer.xml_offset)
        try:
            plist = plistlib.loads(fh.read(trailer.xml_length))
            maps = [entry["Data"] for entry in plist["resource-fork"]["blkx"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise UdifError(f"unreadable block map property list: {exc}") from exc
    elif trailer.rsrc_fork_offset and trailer.rsrc_fork_length:
        fh.seek(trailer.rsrc_fork_offset)
        maps = resource_fork_blkx(fh.read(trailer.rsrc_fork_length))
    if not maps:
        raise UdifError("no block maps (blkx) in the image")
    chunks: List[Chunk] = []
    for data in maps:
        chunks.extend(parse_mish(data, trailer.data_fork_offset))
    chunks.sort(key=lambda c: c.sector)
    return trailer, chunks


def adc_decompress(data: bytes, size: int) -> bytes:
    """Apple Data Compression (UDCO images): an LZ77 variant."""
    out = bytearray()
    i, end = 0, len(data)
    while i < end and len(out) < size:
        byte = data[i]
        if byte & 0x80:                         # literal run of 1-128 bytes
            run = (byte & 0x7F) + 1
            out += data[i + 1:i + 1 + run]
            i += 1 + run
            continue
        if byte & 0x40:                         # 3 bytes: length 4-67, distance 16 bits
            if i + 2 >= end:
                raise UdifError("truncated ADC data")
            run = (byte & 0x3F) + 4
            distance = (data[i + 1] << 8) | data[i + 2]
            i += 3
        else:                                   # 2 bytes: length 3-18, distance 10 bits
            if i + 1 >= end:
                raise UdifError("truncated ADC data")
            run = ((byte & 0x3F) >> 2) + 3
            distance = ((byte & 0x03) << 8) | data[i + 1]
            i += 2
        start = len(out) - distance - 1
        if start < 0:
            raise UdifError("ADC back-reference before the start")
        if distance + 1 >= run:
            out += out[start:start + run]
        else:                                   # overlaps what it writes: byte by byte
            for k in range(run):
                out.append(out[start + k])
    return bytes(out[:size])


def decode(chunk: Chunk, stored: bytes) -> bytes:
    size = chunk.sectors * SECTOR
    if chunk.kind == RAW:
        data = stored
    elif chunk.kind == ZLIB:
        data = zlib.decompress(stored)
    elif chunk.kind == BZIP2:
        data = bz2.decompress(stored)
    elif chunk.kind == ADC:
        data = adc_decompress(stored, size)
    elif chunk.kind == LZMA:
        data = lzma.decompress(stored)
    elif chunk.kind == LZFSE:
        raise UdifError("LZFSE-compressed image (made by macOS 10.11 or later): convert it on "
                        "a Mac with hdiutil convert -format UDTO")
    else:
        raise UdifError(f"unknown chunk type 0x{chunk.kind:08x}")
    if len(data) != size:
        raise UdifError(f"chunk at sector {chunk.sector} decodes to {len(data)} bytes, "
                        f"expected {size}")
    return data


class UdifFile(io.RawIOBase):
    """Read-only, seekable view of the raw image inside a UDIF file.

    Decodes only the chunks a read touches (and keeps the last one), so the
    partition map and volume headers of a multi-GB .dmg can be read quickly.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._fh = open(path, "rb")
        try:
            trailer, chunks = read_chunks(self._fh, path.stat().st_size)
        except Exception:
            self._fh.close()
            raise
        self._chunks = chunks
        self._starts = [c.sector * SECTOR for c in chunks]
        end = max((c.sector + c.sectors for c in chunks), default=0) * SECTOR
        self.size = max(end, trailer.sector_count * SECTOR)
        self._pos = 0
        self._cached: Optional[tuple] = None    # (chunk index, decoded bytes)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self.size}[whence]
        self._pos = max(base + offset, 0)
        return self._pos

    def _chunk_bytes(self, index: int) -> bytes:
        if self._cached and self._cached[0] == index:
            return self._cached[1]
        chunk = self._chunks[index]
        if chunk.kind in (ZERO_FILL, IGNORED):
            data = bytes(chunk.sectors * SECTOR)
        else:
            self._fh.seek(chunk.offset)
            data = decode(chunk, self._fh.read(chunk.length))
        self._cached = (index, data)
        return data

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self.size - self._pos
        out = bytearray()
        while size > 0 and self._pos < self.size:
            index = bisect.bisect_right(self._starts, self._pos) - 1
            chunk = self._chunks[index] if index >= 0 else None
            if chunk is None or self._pos >= (chunk.sector + chunk.sectors) * SECTOR:
                # A gap no chunk covers reads as zeros, up to the next chunk.
                following = self._starts[index + 1] if index + 1 < len(self._starts) else self.size
                take = min(size, following - self._pos)
                out += bytes(take)
            else:
                data = self._chunk_bytes(index)
                at = self._pos - chunk.sector * SECTOR
                piece = data[at:at + size]
                take = len(piece)
                out += piece
            self._pos += take
            size -= take
        return bytes(out)

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def close(self) -> None:
        if not self.closed:
            self._fh.close()
        super().close()


def convert_to_raw(source: Path, target: Path,
                   progress: Optional[Callable[[str], None]] = None) -> Path:
    """Write the raw image a UDIF file holds. Zero runs become holes (sparse)."""
    size = source.stat().st_size
    report_every = 256 * 1024 * 1024
    with open(source, "rb") as src:
        trailer, chunks = read_chunks(src, size)
        total = max((c.sector + c.sectors for c in chunks), default=0) * SECTOR
        if trailer.sector_count:
            total = max(total, trailer.sector_count * SECTOR)
        done = next_report = 0
        with open(target, "wb") as out:
            for chunk in chunks:
                if chunk.kind in (ZERO_FILL, IGNORED) or chunk.sectors == 0:
                    continue                        # left as a hole: reads as zeros
                src.seek(chunk.offset)
                stored = src.read(chunk.length)
                if len(stored) != chunk.length:
                    raise UdifError("image file ends inside a chunk (truncated download?)")
                out.seek(chunk.sector * SECTOR)
                out.write(decode(chunk, stored))
                done = (chunk.sector + chunk.sectors) * SECTOR
                if progress and done >= next_report:
                    progress(f"  decompressed {done // 2**20} / {total // 2**20} MiB")
                    next_report = done + report_every
            out.truncate(total)
    return target


def chunk_kinds(path: Path) -> Iterable[int]:
    """The chunk types an image uses (for messages and tests)."""
    size = path.stat().st_size
    with open(path, "rb") as fh:
        _trailer, chunks = read_chunks(fh, size)
    return sorted({c.kind for c in chunks})

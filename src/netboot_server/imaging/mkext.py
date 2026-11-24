"""mkext v1: the kernel extension archive BootX / boot.efi load at boot.

A NetBooting Mac can't read ``/System/Library/Extensions`` from the server,
so every driver needed before the root image is mounted (platform, Ethernet,
storage, filesystems) must be in ``mach.macosx.mkext``, which is fetched over
TFTP. Install CDs such as 10.0.x boot from the disc and read the folder
directly, so they don't have an mkext, and we build one here.

Format (all fields big-endian), as used by Mac OS X 10.0-10.5::

    header (32 bytes):
        magic 'MKXT', signature 'MOSX', length, adler32 (of bytes 16..end),
        version 0x01008000, numkexts, cputype, cpusubtype
    numkexts x 32-byte entries:
        plist  { offset, compsize, realsize, modifiedsecs }
        module { offset, compsize, realsize, modifiedsecs }   (all 0 = no binary)
    data: file contents, LZSS-compressed when compsize != 0

LZSS is Okumura's variant (4096-byte ring buffer pre-filled with spaces,
match lengths 3-18), as in Apple's ``compress_lzss`` / ``decompress_lzss``.
"""

from __future__ import annotations

import logging
import plistlib
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

log = logging.getLogger("netboot.mkext")

MAGIC = b"MKXT"
SIGNATURE = b"MOSX"
VERSION = 0x01008000
CPU_ANY = 0xFFFFFFFF
HEADER = struct.Struct(">4s4sIIIIII")
FILE_ENTRY = struct.Struct(">IIII")

# Drivers needed before the root filesystem is mounted. 10.0-10.1 only use
# Root and Local-Root; Network-Root appears later.
BOOT_REQUIRED = ("Root", "Local-Root", "Network-Root", "Console", "Safe Boot")

CPU_TYPES = {"ppc": 18, "i386": 7}
FAT_MAGIC = 0xCAFEBABE

# -- LZSS ------------------------------------------------------------------------
N = 4096          # ring buffer size
F = 18            # longest match
THRESHOLD = 2     # matches must be longer than this
_START = N - F    # where the decoder starts writing in the ring


def decompress_lzss(data: bytes, expected_size: Optional[int] = None) -> bytes:
    ring = bytearray(b" " * (N - F) + b"\0" * F)
    r = _START
    out = bytearray()
    flags = 0
    i = 0
    length = len(data)
    while True:
        flags >>= 1
        if not flags & 0x100:
            if i >= length:
                break
            flags = data[i] | 0xFF00
            i += 1
        if flags & 1:
            if i >= length:
                break
            c = data[i]
            i += 1
            out.append(c)
            ring[r] = c
            r = (r + 1) & (N - 1)
        else:
            if i + 1 >= length:
                break
            position = data[i] | ((data[i + 1] & 0xF0) << 4)
            count = (data[i + 1] & 0x0F) + THRESHOLD + 1
            i += 2
            for k in range(count):
                c = ring[(position + k) & (N - 1)]
                out.append(c)
                ring[r] = c
                r = (r + 1) & (N - 1)
        if expected_size is not None and len(out) >= expected_size:
            break
    return bytes(out)


def compress_lzss(data: bytes) -> bytes:
    """Greedy LZSS encoder. Its output decodes with :func:`decompress_lzss`.

    Matches are found with ``bytes.rfind`` (C speed) inside the last
    ``N - F`` bytes, so the source bytes are always still in the decoder's
    ring buffer.
    """
    out = bytearray()
    n = len(data)
    p = 0
    while p < n:
        flag_index = len(out)
        out.append(0)
        flags = 0
        for bit in range(8):
            if p >= n:
                break
            window = max(0, p - (N - F))
            best_len, best_pos = 0, 0
            length = THRESHOLD + 1
            while length <= F and p + length <= n:
                q = data.rfind(data[p:p + length], window, p + length - 1)
                if q < 0:
                    break
                best_len, best_pos = length, q
                length += 1
            if best_len > THRESHOLD:
                ring_index = (_START + best_pos) & (N - 1)
                out.append(ring_index & 0xFF)
                out.append(((ring_index >> 4) & 0xF0) | (best_len - THRESHOLD - 1))
                p += best_len
            else:
                flags |= 1 << bit
                out.append(data[p])
                p += 1
        out[flag_index] = flags
    return bytes(out)


def adler32(data: bytes) -> int:
    return zlib.adler32(data) & 0xFFFFFFFF


# -- Mach-O ------------------------------------------------------------------------
def thin(binary: bytes, arch: Optional[str]) -> bytes:
    """Return the slice for ``arch`` from a fat (universal) binary."""
    if arch is None or len(binary) < 8 or struct.unpack_from(">I", binary)[0] != FAT_MAGIC:
        return binary
    count = struct.unpack_from(">I", binary, 4)[0]
    wanted = CPU_TYPES[arch]
    for i in range(count):
        cputype, _sub, offset, size, _align = struct.unpack_from(">iiIII", binary, 8 + 20 * i)
        if cputype == wanted:
            return binary[offset:offset + size]
    raise ValueError(f"fat binary has no {arch} slice")


# -- reading -------------------------------------------------------------------------
@dataclass
class MkextEntry:
    plist: bytes
    module: Optional[bytes]
    plist_mtime: int = 0
    module_mtime: int = 0

    @property
    def info(self) -> dict:
        return plistlib.loads(self.plist)

    @property
    def identifier(self) -> str:
        return str(self.info.get("CFBundleIdentifier", "?"))


@dataclass
class Mkext:
    entries: List[MkextEntry]
    cputype: int = CPU_ANY
    cpusubtype: int = CPU_ANY


def parse_mkext(data: bytes, verify: bool = True) -> Mkext:
    magic, signature, length, checksum, version, count, cputype, cpusubtype = \
        HEADER.unpack_from(data)
    if magic != MAGIC or signature != SIGNATURE:
        raise ValueError("not an mkext (bad magic)")
    if version != VERSION:
        raise ValueError(f"unsupported mkext version 0x{version:08x}")
    if length != len(data):
        raise ValueError(f"mkext length field {length} != file size {len(data)}")
    if verify and adler32(data[16:]) != checksum:
        raise ValueError("mkext checksum mismatch")

    def read_file(offset_in_table: int) -> Tuple[Optional[bytes], int]:
        offset, compsize, realsize, mtime = FILE_ENTRY.unpack_from(data, offset_in_table)
        if offset == 0:
            return None, 0
        if compsize:
            content = decompress_lzss(data[offset:offset + compsize], realsize)
            if len(content) != realsize:
                raise ValueError(f"entry at {offset}: decompressed {len(content)} != {realsize}")
            return content, mtime
        return data[offset:offset + realsize], mtime

    entries = []
    for i in range(count):
        table = HEADER.size + i * 32
        plist, plist_mtime = read_file(table)
        module, module_mtime = read_file(table + 16)
        entries.append(MkextEntry(plist or b"", module, plist_mtime, module_mtime))
    return Mkext(entries, cputype, cpusubtype)


# -- writing -------------------------------------------------------------------------
def build_mkext(entries: Sequence[MkextEntry], compress: bool = True,
                cputype: int = CPU_ANY, cpusubtype: int = CPU_ANY) -> bytes:
    table = bytearray()
    blobs = bytearray()
    data_start = HEADER.size + 32 * len(entries)

    def add(content: Optional[bytes], mtime: int) -> bytes:
        if content is None:
            return FILE_ENTRY.pack(0, 0, 0, 0)
        offset = data_start + len(blobs)
        if compress:
            packed = compress_lzss(content)
            if len(packed) < len(content):
                blobs.extend(packed)
                return FILE_ENTRY.pack(offset, len(packed), len(content), mtime)
        blobs.extend(content)
        return FILE_ENTRY.pack(offset, 0, len(content), mtime)

    for entry in entries:
        table += add(entry.plist, entry.plist_mtime)
        table += add(entry.module, entry.module_mtime)
    body = struct.pack(">IIII", VERSION, len(entries), cputype, cpusubtype) + bytes(table) + bytes(blobs)
    length = 16 + len(body)
    return struct.pack(">4s4sII", MAGIC, SIGNATURE, length, adler32(body)) + body


# -- collecting kexts from a volume ------------------------------------------------------
@dataclass
class KextSource:
    path: Path
    info: dict
    plist_path: Path
    executable: Optional[Path]

    @property
    def identifier(self) -> str:
        return str(self.info.get("CFBundleIdentifier", self.path.name))

    @property
    def required(self) -> Optional[str]:
        value = self.info.get("OSBundleRequired")
        return str(value) if value is not None else None


def _read_kext(path: Path) -> Optional[KextSource]:
    contents = path / "Contents"
    if contents.is_dir():
        plist_path, binary_dir = contents / "Info.plist", contents / "MacOS"
    else:
        # Pre-10.0 style bundle: Info-macos.xml (or Info.plist) at the top.
        plist_path = path / "Info-macos.xml"
        if not plist_path.is_file():
            plist_path = path / "Info.plist"
        binary_dir = path
    if not plist_path.is_file():
        log.warning("skipping %s: no Info.plist", path)
        return None
    try:
        with open(plist_path, "rb") as fh:
            info = plistlib.load(fh)
    except Exception as exc:
        log.warning("skipping %s: unreadable Info.plist (%s)", path, exc)
        return None
    executable = None
    name = info.get("CFBundleExecutable")
    if name and (binary_dir / str(name)).is_file():
        executable = binary_dir / str(name)
    return KextSource(path, info, plist_path, executable)


def iter_kexts(extensions: Path) -> Iterator[KextSource]:
    """All kexts in an Extensions folder, including kexts in PlugIns."""
    def walk(folder: Path) -> Iterator[KextSource]:
        for child in sorted(folder.iterdir()):
            if child.suffix != ".kext" or not child.is_dir():
                continue
            kext = _read_kext(child)
            if kext is not None:
                yield kext
            for plugins in (child / "Contents" / "PlugIns", child / "PlugIns"):
                if plugins.is_dir():
                    yield from walk(plugins)
    yield from walk(extensions)


@dataclass
class BuildReport:
    included: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    size: int = 0
    uncompressed: int = 0


def collect_entries(extensions: Path, arch: Optional[str],
                    required: Iterable[str] = BOOT_REQUIRED,
                    include_all: bool = False) -> Tuple[List[MkextEntry], BuildReport]:
    wanted = set(required)
    report = BuildReport()
    entries: List[MkextEntry] = []
    seen: Dict[str, Path] = {}
    for kext in iter_kexts(extensions):
        if not include_all and kext.required not in wanted:
            report.skipped.append(kext.identifier)
            continue
        if kext.identifier in seen:
            log.warning("duplicate %s at %s (keeping %s)", kext.identifier, kext.path,
                        seen[kext.identifier])
            continue
        seen[kext.identifier] = kext.path
        module = None
        module_mtime = 0
        if kext.executable is not None:
            try:
                module = thin(kext.executable.read_bytes(), arch)
            except ValueError as exc:
                log.warning("skipping %s: %s", kext.identifier, exc)
                continue
            module_mtime = int(kext.executable.stat().st_mtime)
        plist = kext.plist_path.read_bytes()
        entries.append(MkextEntry(plist, module, int(kext.plist_path.stat().st_mtime),
                                  module_mtime))
        report.included.append(kext.identifier)
        report.uncompressed += len(plist) + (len(module) if module else 0)
    return entries, report


def build_from_volume(volume: Path, arch: Optional[str], output: Path,
                      compress: bool = True, include_all: bool = False) -> BuildReport:
    extensions = volume / "System" / "Library" / "Extensions"
    if not extensions.is_dir():
        raise FileNotFoundError(f"{extensions} not found")
    entries, report = collect_entries(extensions, arch, include_all=include_all)
    if not entries:
        raise ValueError("no boot-time kexts found")
    data = build_mkext(entries, compress=compress)
    # Check our own output before a Mac ever sees it.
    parsed = parse_mkext(data)
    if len(parsed.entries) != len(entries):
        raise AssertionError("mkext round trip lost entries")
    output.write_bytes(data)
    report.size = len(data)
    return report

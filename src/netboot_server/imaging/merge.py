"""Merge a multi-CD install set into one volume.

Disc 1 of a multi-CD set (Jaguar, Panther, Tiger) holds a *proxy package*
for every package that lives on a later disc. A proxy is a ``.pkg`` bundle
with no payload whose ``Info.plist`` says where the real one is::

    IFPkgFlagProxyPackageLocation = disc://Mac OS X Install Disc 2/AdditionalFonts.pkg

The installer asks for that disc when it reaches the proxy. Merging replaces
each proxy with the real package from the other disc, so nothing is asked
for. That's exactly how a hand-made merged image that boots fine differs
from disc 1, so nothing else is changed.

Disc 1's volume is block-copied (owners, forks and hard links stay exactly as
on the disc) and then grown in place by :func:`grow_hfsplus`, so only the
replacement packages are copied file by file.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import stat
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

from .partitions import UnsupportedImage

PROXY_KEY = "IFPkgFlagProxyPackageLocation"
PACKAGES_DIR = "System/Installation/Packages"
PACKAGE_SUFFIXES = (".pkg", ".mpkg")

JOURNALED_BIT = 1 << 13
VH_OFFSET = 1024


# -- growing an HFS+ volume -----------------------------------------------------------
@dataclass
class GrowResult:
    block_size: int
    old_blocks: int
    new_blocks: int
    free_blocks: int


def _bitmap_extents(vh: bytes, block_size: int) -> Tuple[int, List[Tuple[int, int]]]:
    logical, _clump, total = struct.unpack_from(">QII", vh, 0x70)
    extents = [struct.unpack_from(">II", vh, 0x80 + 8 * i) for i in range(8)]
    extents = [(start, count) for start, count in extents if count]
    if sum(count for _s, count in extents) != total:
        raise UnsupportedImage("the volume bitmap continues in the extents overflow file; "
                               "growing that volume isn't supported")
    return logical, extents


def grow_hfsplus(path: Path, new_size: int) -> GrowResult:
    """Grow the bare HFS+ volume in ``path`` to ``new_size`` bytes (rounded down to blocks).

    The volume bitmap must already be large enough to describe the new size.
    Install CDs are mastered with a 4 MiB bitmap (128 GiB at 4 KiB blocks), so
    it always is for them. Nothing is moved: the header's block counts change,
    the alternate volume header moves to the new end, and the bitmap marks the
    new last block (which holds it) as used and the old one as free.
    """
    with open(path, "r+b") as fh:
        fh.seek(VH_OFFSET)
        vh = bytearray(fh.read(512))
        if vh[:2] not in (b"H+", b"HX"):
            raise UnsupportedImage(f"{path.name} doesn't start with an HFS+ volume header")
        attributes, = struct.unpack_from(">I", vh, 4)
        if attributes & JOURNALED_BIT:
            raise UnsupportedImage("can't grow a journaled volume")
        block_size, old_blocks, free_blocks = struct.unpack_from(">III", vh, 40)
        new_blocks = new_size // block_size
        if new_blocks <= old_blocks:
            return GrowResult(block_size, old_blocks, old_blocks, free_blocks)
        logical, extents = _bitmap_extents(vh, block_size)
        if logical * 8 < new_blocks:
            raise UnsupportedImage(
                f"the volume bitmap only covers {logical * 8 * block_size // 2**20} MiB; "
                "growing past that isn't supported")

        bitmap = bytearray()
        for start, count in extents:
            fh.seek(start * block_size)
            bitmap += fh.read(count * block_size)
        bitmap = bitmap[:logical]

        def set_bit(block: int, used: bool) -> None:
            mask = 0x80 >> (block % 8)
            if used:
                bitmap[block // 8] |= mask
            else:
                bitmap[block // 8] &= ~mask & 0xFF

        for block in range(old_blocks, new_blocks):   # new space starts out free
            if bitmap[block // 8] & (0x80 >> (block % 8)):
                set_bit(block, False)
        set_bit(old_blocks - 1, False)   # held the old alternate volume header
        set_bit(new_blocks - 1, True)    # holds the new one
        used = sum(bin(b).count("1") for b in bitmap[:new_blocks // 8])
        used += sum(1 for block in range(new_blocks // 8 * 8, new_blocks)
                    if bitmap[block // 8] & (0x80 >> (block % 8)))
        free_blocks = new_blocks - used

        offset = 0
        for start, count in extents:
            piece = bitmap[offset:offset + count * block_size]
            fh.seek(start * block_size)
            fh.write(piece)
            offset += count * block_size
            if offset >= logical:
                break

        struct.pack_into(">II", vh, 44, new_blocks, free_blocks)
        fh.seek(old_blocks * block_size - VH_OFFSET)
        fh.write(b"\0" * 512)                       # old alternate header
        fh.truncate(new_blocks * block_size)        # sparse on Linux and APFS
        fh.seek(VH_OFFSET)
        fh.write(vh)
        fh.seek(new_blocks * block_size - VH_OFFSET)
        fh.write(vh)
    return GrowResult(block_size, old_blocks, new_blocks, free_blocks)


# -- proxy packages ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Proxy:
    path: str            # proxy bundle, relative to the volume root
    disc: str            # volume name of the disc holding the real package
    location: str        # real package's path on that disc


def parse_location(value: str) -> Optional[Tuple[str, str]]:
    """('Mac OS X Install Disc 2', 'AdditionalFonts.pkg') from a disc:// URL."""
    if not value.startswith("disc://"):
        return None
    disc, _, location = value[len("disc://"):].partition("/")
    location = location.strip("/")
    if not disc or not location or ".." in location.split("/"):
        return None
    return disc, location


def _package_dirs(root: Path) -> Iterator[Path]:
    """Package bundles under System/Installation, not descending into packages' payloads."""
    base = root / "System/Installation"
    if not base.is_dir():
        return
    stack = [base]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir():
                continue
            if entry.name.endswith(PACKAGE_SUFFIXES):
                yield entry
                nested = entry / "Contents/Packages"   # metapackages may hold packages
                if nested.is_dir():
                    stack.append(nested)
            elif directory == base or entry.name == "Packages":
                stack.append(entry)


def find_proxies(root: Path) -> List[Proxy]:
    proxies = []
    for package in _package_dirs(root):
        info = package / "Contents/Info.plist"
        if not info.is_file():
            continue
        try:
            with open(info, "rb") as fh:
                plist = plistlib.load(fh)
        except (plistlib.InvalidFileException, OSError, ValueError):
            continue
        parsed = parse_location(str(plist.get(PROXY_KEY, ""))) if isinstance(plist, dict) else None
        if parsed:
            proxies.append(Proxy(str(package.relative_to(root)), *parsed))
    return proxies


@dataclass
class Disc:
    root: Path           # where it's mounted
    name: str = ""       # volume name, when known
    label: str = ""      # for messages (the source file name)


@dataclass
class MergePlan:
    replacements: List[Tuple[Proxy, Disc, Path]] = field(default_factory=list)
    missing: List[Proxy] = field(default_factory=list)

    @property
    def payload_bytes(self) -> int:
        return sum(tree_size(source) for _p, _d, source in self.replacements)


def plan_merge(disc1: Path, others: Sequence[Disc]) -> MergePlan:
    """Match each proxy on disc 1 with the real package on another disc.

    A disc whose volume name matches the proxy's is preferred; otherwise the
    first disc holding a package at that path is used.
    """
    plan = MergePlan()
    for proxy in find_proxies(disc1):
        named = [d for d in others if d.name == proxy.disc]
        candidates = named + [d for d in others if d not in named]
        for disc in candidates:
            source = disc.root / proxy.location
            if (source / "Contents").is_dir() and not find_proxies_in_package(source):
                plan.replacements.append((proxy, disc, source))
                break
        else:
            plan.missing.append(proxy)
    return plan


def find_proxies_in_package(package: Path) -> bool:
    """True if ``package`` is itself a proxy (so it can't stand in for one)."""
    try:
        with open(package / "Contents/Info.plist", "rb") as fh:
            plist = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False
    return isinstance(plist, dict) and PROXY_KEY in plist


def tree_size(path: Path, block: int = 4096) -> int:
    """Bytes a tree takes on disk, rounding every file and fork up to ``block``."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        total += block
        for name in filenames:
            file = Path(dirpath, name)
            try:
                size = file.lstat().st_size
            except OSError:
                continue
            total += -(-size // block) * block
            total += -(-_rsrc_size(file) // block) * block
    return total


# -- copying packages, forks included ------------------------------------------------------
def _rsrc_path(file: Path) -> Path:
    # macOS exposes a file's resource fork at file/..namedfork/rsrc, Linux
    # hfsplus at file/rsrc.
    return file / ("..namedfork/rsrc" if sys.platform == "darwin" else "rsrc")


def _rsrc_size(file: Path) -> int:
    try:
        return os.stat(_rsrc_path(file)).st_size
    except OSError:
        return 0


def copy_file_with_forks(source: str, target: str) -> None:
    shutil.copy2(source, target)   # data fork, mode, times, and xattrs on Linux
    size = _rsrc_size(Path(source))
    if size:
        with open(_rsrc_path(Path(source)), "rb") as src, open(_rsrc_path(Path(target)), "wb") as dst:
            shutil.copyfileobj(src, dst)


def copy_tree_portable(source: Path, target: Path) -> None:
    """Copy a package tree with data and resource forks, modes, times and owners."""
    shutil.copytree(source, target, symlinks=True, copy_function=copy_file_with_forks)
    if os.geteuid() != 0:
        return
    for dirpath, dirnames, filenames in os.walk(source):
        rel = Path(dirpath).relative_to(source)
        for name in [""] + dirnames + filenames:
            src = Path(dirpath, name) if name else Path(dirpath)
            dst = target / rel / name if name else target / rel
            st = src.lstat()
            os.lchown(dst, st.st_uid, st.st_gid)
            if not stat.S_ISLNK(st.st_mode):
                os.chmod(dst, stat.S_IMODE(st.st_mode))   # chown clears setuid bits


def copy_tree(source: Path, target: Path) -> None:
    if sys.platform == "darwin":
        # ditto keeps resource forks, extended attributes, ACLs and (as root) owners.
        subprocess.run(["ditto", str(source), str(target)], check=True)
    else:
        copy_tree_portable(source, target)


def apply_merge(plan: MergePlan, merged_root: Path,
                copier: Callable[[Path, Path], None] = copy_tree,
                progress: Callable[[str], None] = lambda message: None) -> List[str]:
    """Replace each planned proxy inside ``merged_root``; returns the replaced paths."""
    done = []
    for proxy, disc, source in plan.replacements:
        target = merged_root / proxy.path
        progress(f"  {proxy.path} <- {disc.label or disc.name}:{proxy.location}")
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        copier(source, target)
        done.append(proxy.path)
    return done

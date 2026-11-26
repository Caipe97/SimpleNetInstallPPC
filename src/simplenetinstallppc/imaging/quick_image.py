"""Phase 0 test-image maker.

Turns a disk image of a Mac OS X install CD/DVD into a NetInstall image
folder, with no changes to its contents:

1. Convert to raw if needed (``hdiutil`` on macOS).
2. Copy the HFS+ volume out to ``<image>/root.dmg``, without a partition map
   or HFS wrapper.
3. Mount it read-only and copy BootX / boot.efi, ``mach_kernel`` and the
   existing ``Extensions.mkext`` into ``ppc/`` and ``i386/``.
4. Write ``NBImageInfo.plist``.

Given several discs of one install set, step 2 copies disc 1 and merges the
others into it (see merge.py), so the installer never asks for another disc.

Phase 2 replaces this with the full pipeline (image customization, a mkext
we build ourselves). This version is enough to check what each OS X release
accepts on real hardware.
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from ..catalog import INFO_PLIST, Catalog
from ..net.bsdp import ImageKind
from ..roots import default_root_directory, is_folder_style
from .merge import Disc, apply_merge, find_proxies, grow_hfsplus, plan_merge
from .mkext import build_from_volume
from .partitions import UnsupportedImage, Volume, find_volumes, is_udif, pick_volume

ROOT_IMAGE = "root.dmg"
COPY_CHUNK = 8 * 1024 * 1024

BOOTERS = {
    "ppc": ["System/Library/CoreServices/BootX"],
    "i386": ["System/Library/CoreServices/boot.efi", "usr/standalone/i386/boot.efi"],
}
KERNELS = ["mach_kernel"]
MKEXTS = ["System/Library/Extensions.mkext"]
BOOT_PLIST = "Library/Preferences/SystemConfiguration/com.apple.Boot.plist"

Progress = Callable[[str], None]


@dataclass
class ImageReport:
    directory: Path
    name: str
    index: int
    version: str
    build: str
    is_install: bool
    architectures: List[str]
    volume: Volume
    warnings: List[str] = field(default_factory=list)
    merged: List[str] = field(default_factory=list)   # proxy packages replaced from other discs


def _default_progress(message: str) -> None:
    print(message, file=sys.stderr)


# -- raw conversion -------------------------------------------------------------
def ensure_raw(source: Path, work: Path, progress: Progress) -> Path:
    if not is_udif(source):
        return source
    if sys.platform != "darwin":
        raise UnsupportedImage(
            f"{source.name} is a compressed UDIF image. On Linux, convert it first: "
            f"dmg2img {source.name} {source.stem}.img")
    target = work / (source.stem + ".cdr")
    progress(f"Converting {source.name} to a raw image with hdiutil...")
    subprocess.run(["hdiutil", "convert", str(source), "-format", "UDTO",
                    "-o", str(work / source.stem)], check=True,
                   stdout=subprocess.DEVNULL)
    return target


def copy_volume(source: Path, volume: Volume, target: Path, progress: Progress) -> None:
    total = volume.length
    done = 0
    last_report = 0.0
    with open(source, "rb") as src, open(target, "wb") as dst:
        src.seek(volume.offset)
        while done < total:
            chunk = src.read(min(COPY_CHUNK, total - done))
            if not chunk:
                raise UnsupportedImage("image file ends before the volume does (truncated image?)")
            dst.write(chunk)
            done += len(chunk)
            now = time.monotonic()
            if now - last_report > 1 or done == total:
                progress(f"  copied {done // (1024 * 1024)} / {total // (1024 * 1024)} MiB")
                last_report = now


# -- mounting ---------------------------------------------------------------------
@contextlib.contextmanager
def mount_volume(image: Path, writable: bool = False, volume: Optional[Volume] = None
                 ) -> Iterator[Path]:
    """Mount the HFS+ volume in ``image``: a bare volume, or ``volume`` inside a disc image.

    Writable mounts keep file owners (we run as root), so copied files keep theirs.
    """
    if sys.platform == "darwin":
        args = ["hdiutil", "attach", "-nobrowse", "-noverify", "-noautoopen",
                "-mountrandom", tempfile.gettempdir(), "-plist"]
        if writable:
            # Owners only matter (and only work) as root; as root, copies keep theirs.
            args += ["-readwrite", "-owners", "on" if os.geteuid() == 0 else "off"]
        else:
            args += ["-readonly"]
        if volume is None or volume.scheme == "none":
            args += ["-imagekey", "diskimage-class=CRawDiskImage"]
        out = subprocess.run(args + [str(image)], check=True, capture_output=True).stdout
        entities = plistlib.loads(out).get("system-entities", [])
        mounts = [e for e in entities if "mount-point" in e]
        hfs = [e for e in mounts if str(e.get("volume-kind", "hfs")).startswith("hfs")]
        devices = [e["dev-entry"] for e in entities if "dev-entry" in e]
        if not (hfs or mounts):
            for dev in sorted(devices, key=len)[:1]:
                subprocess.run(["hdiutil", "detach", dev], check=False,
                               stdout=subprocess.DEVNULL)
            raise UnsupportedImage("hdiutil attached the image but mounted no volume")
        mount_point = Path((hfs or mounts)[0]["mount-point"])
        try:
            yield mount_point
        finally:
            target = sorted(devices, key=len)[0] if devices else str(mount_point)
            subprocess.run(["hdiutil", "detach", target], check=False,
                           stdout=subprocess.DEVNULL)
    else:
        mount_point = Path(tempfile.mkdtemp(prefix="netboot-mnt-"))
        options = ("rw" if writable else "ro") + ",loop"
        if volume is not None and volume.scheme != "none":
            options += f",offset={volume.offset},sizelimit={volume.length}"
        try:
            subprocess.run(["mount", "-t", "hfsplus", "-o", options, str(image),
                            str(mount_point)], check=True)
        except subprocess.CalledProcessError:
            mount_point.rmdir()
            raise UnsupportedImage(
                "mount failed: run as root, and install hfsprogs (Debian) or "
                "hfsplus-tools (Fedora) so the kernel's hfsplus module is available")
        try:
            yield mount_point
        finally:
            subprocess.run(["umount", str(mount_point)], check=False)
            mount_point.rmdir()


def mount_readonly(image: Path) -> "contextlib.AbstractContextManager[Path]":
    return mount_volume(image)


def volume_name(mount: Path) -> str:
    """The mounted volume's name, where the host can tell us (macOS)."""
    if sys.platform != "darwin":
        return ""
    try:
        out = subprocess.run(["diskutil", "info", "-plist", str(mount)], capture_output=True,
                             check=True, timeout=10).stdout
        return str(plistlib.loads(out).get("VolumeName", ""))
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
        return ""


# -- main entry point -----------------------------------------------------------------
def _first_existing(root: Path, candidates: List[str]) -> Optional[Path]:
    for candidate in candidates:
        path = root / candidate
        if path.is_file():
            return path
    return None


def next_free_index(images_root: Path) -> int:
    catalog = Catalog(images_root)
    catalog.reload()
    used = {image.index for image in catalog.images}
    index = 1
    while index in used:
        index += 1
    return index


MERGE_SLACK = 64 * 1024 * 1024


def _merge_discs(sources: List[Path], work: Path, root_image: Path, progress: Progress
                 ) -> Tuple[Path, Volume, List[str], List[str]]:
    """Copy disc 1 of a multi-disc set to ``root_image`` and merge the other discs in.

    Returns (disc 1's source, its volume, replaced packages, warnings).
    """
    warnings: List[str] = []
    raws: List[Path] = []
    try:
        for source in sources:
            raws.append(ensure_raw(source, work, progress))
        volumes = [pick_volume(find_volumes(raw)) for raw in raws]
        with contextlib.ExitStack() as stack:
            discs = []
            for source, raw, volume in zip(sources, raws, volumes):
                root = stack.enter_context(mount_volume(raw, volume=volume))
                discs.append(Disc(root, volume_name(root), source.name))
            first = next((i for i, d in enumerate(discs)
                          if (d.root / "System/Library/CoreServices/BootX").is_file()
                          and find_proxies(d.root)), None)
            if first is None:
                raise UnsupportedImage(
                    "none of these discs has proxy packages pointing to the others, so "
                    "there's nothing to merge (is one of them disc 1 of the set?)")
            disc1 = discs[first]
            others = [d for i, d in enumerate(discs) if i != first]
            plan = plan_merge(disc1.root, others)
            payload = plan.payload_bytes
            progress(f"Disc 1 is {disc1.label} ({disc1.name or 'volume name unknown'}): "
                     f"{len(plan.replacements)} package(s) to merge from the other discs, "
                     f"{payload // 2**20} MiB")
            if plan.missing:
                discs_needed = sorted({p.disc for p in plan.missing})
                warnings.append(f"{len(plan.missing)} package(s) not found on the discs given "
                                f"(from {', '.join(discs_needed)}); the installer will ask "
                                "for those discs: " + ", ".join(p.path for p in plan.missing))
            unused = [d.label for d in others
                      if not any(disc is d for _p, disc, _s in plan.replacements)]
            if unused:
                warnings.append("no packages were needed from " + ", ".join(unused))

            volume = volumes[first]
            progress(f"Copying disc 1's volume {volume.name!r} to {root_image}...")
            copy_volume(raws[first], volume, root_image, progress)
        new_size = volume.length + payload * 11 // 10 + MERGE_SLACK
        grown = grow_hfsplus(root_image, new_size)
        progress(f"Grew the volume from {grown.old_blocks * grown.block_size // 2**20} to "
                 f"{grown.new_blocks * grown.block_size // 2**20} MiB")
        with contextlib.ExitStack() as stack:
            # Re-mount the discs and the copy (disc 1's mount had to go first:
            # macOS won't mount two volumes with the same UUID at once).
            for disc, raw, vol in zip(others, [r for i, r in enumerate(raws) if i != first],
                                      [v for i, v in enumerate(volumes) if i != first]):
                disc.root = stack.enter_context(mount_volume(raw, volume=vol))
            plan = plan_merge_remounted(plan, others)
            merged_root = stack.enter_context(mount_volume(root_image, writable=True))
            progress("Merging packages:")
            replaced = apply_merge(plan, merged_root, progress=progress)
            left = find_proxies(merged_root)
            if len(left) != len(plan.missing):
                warnings.append(f"{len(left)} proxy package(s) remain after merging")
        return sources[first], volume, replaced, warnings
    finally:
        for source, raw in zip(sources, raws):
            if raw != source:
                raw.unlink(missing_ok=True)


def plan_merge_remounted(plan, others: List[Disc]):
    """The same plan with source paths moved to the discs' new mount points."""
    from .merge import MergePlan
    moved = MergePlan(missing=list(plan.missing))
    for proxy, disc, _source in plan.replacements:
        moved.replacements.append((proxy, disc, disc.root / proxy.location))
    return moved


def make_test_image(source: Path, images_root: Path, *, dir_name: Optional[str] = None,
                    name: Optional[str] = None, index: Optional[int] = None,
                    protocol: str = "HTTP", volume_index: Optional[int] = None,
                    force: bool = False, progress: Progress = _default_progress,
                    extra_sources: Sequence[Path] = ()) -> ImageReport:
    source = source.resolve()
    sources = [source] + [s.resolve() for s in extra_sources]
    images_root.mkdir(parents=True, exist_ok=True)
    work = images_root / ".work"
    work.mkdir(exist_ok=True)
    if index is None:
        index = next_free_index(images_root)
    dir_name = dir_name or f"img{index}"
    target = images_root / dir_name
    if target.exists():
        if not force:
            raise FileExistsError(f"{target} already exists (use --force to replace it)")
        shutil.rmtree(target)
    target.mkdir()

    warnings: List[str] = []
    merged: List[str] = []
    if len(sources) > 1:
        source, volume, merged, merge_warnings = _merge_discs(sources, work,
                                                              target / ROOT_IMAGE, progress)
        warnings += merge_warnings
    else:
        raw = ensure_raw(source, work, progress)
        try:
            volumes = find_volumes(raw)
            for i, volume in enumerate(volumes):
                progress(f"  volume #{i}: {volume.kind} {volume.name!r} at {volume.offset}, "
                         f"{volume.length // (1024 * 1024)} MiB ({volume.scheme})")
            volume = pick_volume(volumes, volume_index)
            progress(f"Copying volume {volume.name!r} to {target / ROOT_IMAGE}...")
            copy_volume(raw, volume, target / ROOT_IMAGE, progress)
        finally:
            if raw != source:
                raw.unlink(missing_ok=True)

    if volume.journaled:
        warnings.append("volume is journaled; served read-only, which is fine for NetBoot")
    architectures: List[str] = []
    version_info: Dict[str, str] = {}
    with mount_readonly(target / ROOT_IMAGE) as mount:
        is_install = (mount / "System/Installation").is_dir()
    if not is_install:
        shutil.rmtree(target)
        raise UnsupportedImage(
            "this is an installed system, not an install CD/DVD; only NetInstall images "
            "are supported")
    with mount_readonly(target / ROOT_IMAGE) as mount:
        plist_path = mount / "System/Library/CoreServices/SystemVersion.plist"
        if plist_path.is_file():
            with open(plist_path, "rb") as fh:
                version_info = plistlib.load(fh)
        else:
            warnings.append("no SystemVersion.plist; is this a Mac OS X volume?")
        kernel = _first_existing(mount, KERNELS)
        mkext = _first_existing(mount, MKEXTS)
        if kernel is None:
            warnings.append("no /mach_kernel on the volume")
        if mkext is None:
            warnings.append("no Extensions.mkext on the volume; built one from "
                            "/System/Library/Extensions")
        for arch, candidates in BOOTERS.items():
            booter = _first_existing(mount, candidates)
            if booter is None:
                continue
            architectures.append(arch)
            arch_dir = target / arch
            arch_dir.mkdir()
            shutil.copyfile(booter, arch_dir / "booter")
            if kernel:
                shutil.copyfile(kernel, arch_dir / "mach.macosx")
            if mkext:
                shutil.copyfile(mkext, arch_dir / "mach.macosx.mkext")
            else:
                progress(f"Building mach.macosx.mkext for {arch} (this takes a minute)...")
                built = build_from_volume(mount, arch, arch_dir / "mach.macosx.mkext")
                progress(f"  {len(built.included)} kexts, {built.size // 1024} KiB")
            if arch == "i386" and (mount / BOOT_PLIST).is_file():
                shutil.copyfile(mount / BOOT_PLIST, arch_dir / "com.apple.Boot.plist")
    if not architectures:
        warnings.append("no BootX or boot.efi found; the image cannot be booted")

    version = str(version_info.get("ProductVersion", "unknown"))
    build = str(version_info.get("ProductBuildVersion", ""))
    product = str(version_info.get("ProductName", "Mac OS X"))
    name = name or f"{product} {version} Install"
    root_directory = None
    if is_folder_style(version):
        # 10.0/10.1 kernels mount an NFS directory, not root.dmg; the server
        # mounts the volume there and exports it (see roots.py).
        protocol = "NFS"
        root_directory = str(default_root_directory(images_root, target.name))
        warnings.append(f"Mac OS X {version} boots from an NFS folder: the server will mount "
                        f"root.dmg at {root_directory} and export it")
    info = {
        "Architectures": architectures,
        "BootFile": "booter",
        "Description": f"{product} {version} ({build}) from {source.name}"
                       + (f" + {len(sources) - 1} more disc(s)" if len(sources) > 1 else ""),
        "Index": index,
        "IsDefault": False,
        "IsEnabled": True,
        "IsInstall": is_install,
        "Kind": int(ImageKind.MACOSX),
        "Name": name,
        "RootPath": ROOT_IMAGE,
        "SupportsDiskless": False,
        "Type": protocol.upper(),
        "osVersion": version,
        **({"RootDirectory": root_directory} if root_directory else {}),
        "NetBootServer": {
            "Source": str(source),
            **({"Sources": [str(s) for s in sources], "MergedPackages": merged}
               if len(sources) > 1 else {}),
            "SourceVolume": volume.name,
            "Build": build,
            "Created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "Tool": "make-test-image",
        },
    }
    with open(target / INFO_PLIST, "wb") as fh:
        plistlib.dump(info, fh)
    (target / ROOT_IMAGE).chmod(0o444)
    return ImageReport(target, name, index, version, build, is_install, architectures,
                       volume, warnings, merged)

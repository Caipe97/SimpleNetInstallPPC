"""Phase 0 test-image maker.

Turns a disk image of an installed Mac OS X system, or of an install CD/DVD,
into an image folder the server can boot, with no changes to its contents:

1. Convert to raw if needed (``hdiutil`` on macOS).
2. Copy the HFS+ volume out to ``<image>/root.dmg``, without a partition map
   or HFS wrapper.
3. Mount it read-only and copy BootX / boot.efi, ``mach_kernel`` and the
   existing ``Extensions.mkext`` into ``ppc/`` and ``i386/``.
4. Write ``NBImageInfo.plist``.

Phase 2 replaces this with the full pipeline (image customization, a mkext
we build ourselves, multi-CD merging). This version is enough to check what
each OS X release accepts on real hardware.
"""

from __future__ import annotations

import contextlib
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

from ..catalog import INFO_PLIST, Catalog
from ..net.bsdp import ImageKind
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
def mount_readonly(image: Path) -> Iterator[Path]:
    if sys.platform == "darwin":
        out = subprocess.run(
            ["hdiutil", "attach", "-readonly", "-nobrowse", "-noverify", "-noautoopen",
             "-mountrandom", tempfile.gettempdir(), "-plist",
             "-imagekey", "diskimage-class=CRawDiskImage", str(image)],
            check=True, capture_output=True).stdout
        entities = plistlib.loads(out).get("system-entities", [])
        mounts = [e for e in entities if "mount-point" in e]
        if not mounts:
            devices = [e["dev-entry"] for e in entities if "dev-entry" in e]
            for dev in devices[:1]:
                subprocess.run(["hdiutil", "detach", dev], check=False,
                               stdout=subprocess.DEVNULL)
            raise UnsupportedImage("hdiutil attached the image but mounted no volume")
        mount_point = Path(mounts[0]["mount-point"])
        try:
            yield mount_point
        finally:
            subprocess.run(["hdiutil", "detach", str(mount_point)], check=False,
                           stdout=subprocess.DEVNULL)
    else:
        mount_point = Path(tempfile.mkdtemp(prefix="netboot-mnt-"))
        try:
            subprocess.run(["mount", "-t", "hfsplus", "-o", "ro,loop", str(image),
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


def make_test_image(source: Path, images_root: Path, *, dir_name: Optional[str] = None,
                    name: Optional[str] = None, index: Optional[int] = None,
                    protocol: str = "HTTP", volume_index: Optional[int] = None,
                    force: bool = False, progress: Progress = _default_progress) -> ImageReport:
    source = source.resolve()
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

    warnings: List[str] = []
    if volume.journaled:
        warnings.append("volume is journaled; served read-only, which is fine for NetBoot")
    architectures: List[str] = []
    version_info: Dict[str, str] = {}
    with mount_readonly(target / ROOT_IMAGE) as mount:
        plist_path = mount / "System/Library/CoreServices/SystemVersion.plist"
        if plist_path.is_file():
            with open(plist_path, "rb") as fh:
                version_info = plistlib.load(fh)
        else:
            warnings.append("no SystemVersion.plist; is this a Mac OS X volume?")
        is_install = (mount / "System/Installation").is_dir()
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
    name = name or f"{product} {version} {'Install' if is_install else 'NetBoot'}"
    info = {
        "Architectures": architectures,
        "BootFile": "booter",
        "Description": f"{product} {version} ({build}) from {source.name}",
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
        "NetBootServer": {
            "Source": str(source),
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
                       volume, warnings)

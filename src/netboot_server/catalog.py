"""The catalog of NetBoot images on disk.

Each image is a folder under the images root, laid out like Apple's ``.nbi``
bundles::

    <images root>/<image dir>/
        NBImageInfo.plist
        ppc/booter  ppc/mach.macosx  ppc/mach.macosx.mkext
        i386/booter i386/mach.macosx i386/mach.macosx.mkext
        root.dmg                     (the RootPath key names this file)

Older single-architecture bundles that keep ``booter`` at the top level are
also accepted.
"""

from __future__ import annotations

import logging
import plistlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, FrozenSet, List, Optional, Tuple

from .net.bsdp import ImageKind, make_image_id

log = logging.getLogger("netboot.catalog")

INFO_PLIST = "NBImageInfo.plist"
PROTOCOLS = ("NFS", "HTTP")


@dataclass
class NetBootImage:
    index: int
    name: str
    directory: Path          # absolute path to the image folder
    rel_dir: str             # same folder, relative to the images root (POSIX)
    root_image: str          # root disk image file name inside the folder
    kind: int = ImageKind.MACOSX
    is_install: bool = False
    architectures: Tuple[str, ...] = ("ppc",)
    protocol: str = "HTTP"
    enabled: bool = True
    is_default: bool = False
    supports_diskless: bool = False
    description: str = ""
    enabled_models: FrozenSet[str] = field(default_factory=frozenset)
    disabled_models: FrozenSet[str] = field(default_factory=frozenset)
    boot_file: str = "booter"

    @property
    def image_id(self) -> int:
        return make_image_id(self.index, self.kind, self.is_install)

    def booter_relpath(self, arch: Optional[str]) -> Optional[str]:
        """TFTP path (relative to the images root) of the booter for ``arch``."""
        candidates = []
        if arch:
            candidates.append(PurePosixPath(arch, self.boot_file))
        candidates.append(PurePosixPath(self.boot_file))
        for candidate in candidates:
            if (self.directory / candidate).is_file():
                return str(PurePosixPath(self.rel_dir) / candidate)
        return None

    def supports(self, arch: Optional[str], model: Optional[str]) -> bool:
        if not self.enabled:
            return False
        if arch and arch not in self.architectures:
            return False
        if model and self.enabled_models and model not in self.enabled_models:
            return False
        if model and model in self.disabled_models:
            return False
        return True

    def problems(self) -> List[str]:
        """Things that will stop this image booting, for display at startup."""
        issues = []
        if not (self.directory / self.root_image).is_file():
            issues.append(f"root image {self.root_image!r} is missing")
        for arch in self.architectures:
            if self.booter_relpath(arch) is None:
                issues.append(f"no booter for {arch}")
            elif not self._kernel_present(arch):
                issues.append(f"no mach.macosx for {arch}")
        if self.protocol not in PROTOCOLS:
            issues.append(f"unknown protocol {self.protocol!r}")
        return issues

    def _kernel_present(self, arch: str) -> bool:
        return ((self.directory / arch / "mach.macosx").is_file()
                or (self.directory / "mach.macosx").is_file())


def load_image(directory: Path, images_root: Path) -> NetBootImage:
    with open(directory / INFO_PLIST, "rb") as fh:
        info = plistlib.load(fh)
    rel_dir = directory.resolve().relative_to(images_root.resolve()).as_posix()
    return NetBootImage(
        index=int(info["Index"]),
        name=str(info.get("Name", directory.name)),
        directory=directory.resolve(),
        rel_dir=rel_dir,
        root_image=str(info["RootPath"]),
        kind=int(info.get("Kind", ImageKind.MACOSX)),
        is_install=bool(info.get("IsInstall", False)),
        architectures=tuple(info.get("Architectures", ["ppc"])),
        protocol=str(info.get("Type", "HTTP")).upper(),
        enabled=bool(info.get("IsEnabled", True)),
        is_default=bool(info.get("IsDefault", False)),
        supports_diskless=bool(info.get("SupportsDiskless", False)),
        description=str(info.get("Description", "")),
        enabled_models=frozenset(info.get("EnabledSystemIdentifiers", [])),
        disabled_models=frozenset(info.get("DisabledSystemIdentifiers", [])),
        boot_file=str(info.get("BootFile", "booter")),
    )


class Catalog:
    def __init__(self, images_root: Path) -> None:
        self.images_root = images_root.resolve()
        self.images: List[NetBootImage] = []
        # Per-architecture default image index set by the operator. Falls
        # back to images marked IsDefault, then to the first match.
        self.default_index: Dict[str, int] = {}

    def reload(self) -> None:
        images: List[NetBootImage] = []
        seen: Dict[int, Path] = {}
        if not self.images_root.is_dir():
            log.warning("images folder %s does not exist", self.images_root)
        else:
            for plist in sorted(self.images_root.glob(f"*/{INFO_PLIST}")):
                try:
                    image = load_image(plist.parent, self.images_root)
                except Exception as exc:
                    log.error("skipping %s: %s", plist.parent, exc)
                    continue
                if image.index in seen:
                    log.error("skipping %s: index %d already used by %s",
                              plist.parent, image.index, seen[image.index])
                    continue
                seen[image.index] = plist.parent
                images.append(image)
        self.images = images

    def by_id(self, image_id: int) -> Optional[NetBootImage]:
        for image in self.images:
            if image.image_id == image_id:
                return image
        return None

    def by_index(self, index: int) -> Optional[NetBootImage]:
        for image in self.images:
            if image.index == index:
                return image
        return None

    def available_for(self, arch: Optional[str], model: Optional[str]) -> List[NetBootImage]:
        return [image for image in self.images if image.supports(arch, model)]

    def default_for(self, arch: Optional[str], model: Optional[str]) -> Optional[NetBootImage]:
        candidates = self.available_for(arch, model)
        if not candidates:
            return None
        chosen = self.default_index.get(arch or "")
        for image in candidates:
            if image.index == chosen:
                return image
        for image in candidates:
            if image.is_default:
                return image
        return candidates[0]

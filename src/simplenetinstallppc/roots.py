"""Folder-style roots for Mac OS X 10.0 / 10.1.

Those kernels can't boot from a disk image. They mount an NFS v2 *directory*
as root (see net/bootparamd.py). For every enabled image with a
``RootDirectory``, :class:`RootManager` makes sure that:

1. ``root.dmg`` is mounted read-only at ``RootDirectory``, keeping its owners
   (``hdiutil`` on macOS, ``mount -t hfsplus`` on Linux), and
2. the folder is exported over NFS v2:

   * macOS (``system`` mode): a block in ``/etc/exports`` owned by this
     server, between ``# BEGIN netboot-server`` and ``# END netboot-server``.
     Lines outside the block are never changed. Paths you exported yourself
     are left alone.
   * Linux (``builtin`` mode): Linux's kernel NFS server can't export
     hfsplus, and often lacks NFS v2, so our own read-only NFS v2 server
     (net/nfs2.py) serves the mounted folders. This module only lists them.

Mounts are left in place when the server stops, so stopping it never pulls a
root out from under a booted Mac.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from .catalog import Catalog, NetBootImage
from .events import EventBus

log = logging.getLogger("netboot.roots")

BEGIN = "# BEGIN netboot-server (managed automatically; edit outside this block)"
END = "# END netboot-server"
EXPORTS = Path("/etc/exports")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True)


def is_folder_style(version: str) -> bool:
    """Mac OS X 10.0.x and 10.1.x boot from an NFS directory, not an image."""
    parts = version.split(".")
    return len(parts) >= 2 and parts[0] == "10" and parts[1] in ("0", "1")


def default_root_directory(images_root: Path, dir_name: str) -> Path:
    # Paths without spaces, so an rp= boot-arg still works as a fallback.
    if sys.platform == "darwin":
        return Path("/private/var/netboot") / dir_name
    return images_root.parent / "roots" / dir_name


def default_nfs_mode() -> str:
    return "system" if sys.platform == "darwin" else "builtin"


# -- /etc/exports block (macOS) ---------------------------------------------------
def _quote(path: str) -> str:
    return f'"{path}"' if " " in path else path


def _exported_paths(lines: Sequence[str]) -> List[str]:
    paths = []
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith('"'):
            paths.append(text[1:].split('"', 1)[0])
        else:
            paths.append(text.split()[0])
    return paths


def render_exports(existing: str, paths: Sequence[str],
                   network: ipaddress.IPv4Network) -> Tuple[str, List[str]]:
    """Return (new /etc/exports text, paths skipped because already exported by hand)."""
    lines = existing.splitlines()
    outside: List[str] = []
    inside = False
    for line in lines:
        if line.strip() == BEGIN:
            inside = True
            continue
        if line.strip() == END:
            inside = False
            continue
        if not inside:
            outside.append(line)
    manual = set(_exported_paths(outside))
    skipped = [p for p in paths if p in manual]
    managed = [p for p in paths if p not in manual]
    while outside and not outside[-1].strip():
        outside.pop()
    result = list(outside)
    if managed:
        if result:
            result.append("")
        result.append(BEGIN)
        for path in managed:
            result.append(f"{_quote(path)} -ro -maproot=root -network {network.network_address} "
                          f"-mask {network.netmask}")
        result.append(END)
    return "\n".join(result) + ("\n" if result else ""), skipped


@dataclass
class RootStatus:
    image: NetBootImage
    mounted: bool
    message: str = ""


class RootManager:
    def __init__(self, nfs_mode: str, network: ipaddress.IPv4Network, events: EventBus,
                 exports_file: Path = EXPORTS, runner: Runner = _run) -> None:
        if nfs_mode not in ("system", "builtin"):
            raise ValueError(f"unknown NFS mode {nfs_mode!r}")
        self.nfs_mode = nfs_mode
        self.network = network
        self.events = events
        self.exports_file = exports_file
        self.run = runner

    # -- mounting ----------------------------------------------------------------
    @staticmethod
    def is_mounted(path: Path) -> bool:
        return os.path.ismount(path) and (path / "mach_kernel").is_file()

    def ensure_mounted(self, image: NetBootImage) -> RootStatus:
        target = Path(image.root_directory or "")
        if self.is_mounted(target):
            return RootStatus(image, True, "already mounted")
        source = image.directory / image.root_image
        if not source.is_file():
            return RootStatus(image, False, f"{source} is missing")
        target.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            cmd = ["hdiutil", "attach", "-readonly", "-nobrowse", "-owners", "on", "-noverify",
                   "-noautoopen", "-mountpoint", str(target),
                   "-imagekey", "diskimage-class=CRawDiskImage", str(source)]
        else:
            self.run(["modprobe", "hfsplus"])
            cmd = ["mount", "-t", "hfsplus", "-o", "ro,loop", str(source), str(target)]
        result = self.run(cmd)
        if result.returncode != 0 or not self.is_mounted(target):
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            hint = "" if sys.platform == "darwin" else \
                " (needs root and the hfsplus module: hfsprogs on Debian, kernel-modules-extra on Fedora)"
            return RootStatus(image, False,
                              f"mount failed: {detail[-1] if detail else result.returncode}{hint}")
        return RootStatus(image, True, "mounted")

    # -- exporting (macOS nfsd) ------------------------------------------------------
    def export_system(self, paths: Sequence[str]) -> Optional[str]:
        """Update the managed exports block. Returns an error message or None."""
        try:
            existing = self.exports_file.read_text() if self.exports_file.exists() else ""
        except OSError as exc:
            return f"cannot read {self.exports_file}: {exc}"
        new_text, skipped = render_exports(existing, paths, self.network)
        for path in skipped:
            log.info("%s is already exported outside the managed block; leaving it", path)
        if new_text == existing:
            return None
        backup = self.exports_file.with_name(self.exports_file.name + ".netboot-server.bak")
        try:
            if self.exports_file.exists():
                shutil.copy2(self.exports_file, backup)
            self.exports_file.write_text(new_text)
        except OSError as exc:
            return f"cannot write {self.exports_file}: {exc} (run as root)"
        check = self.run(["nfsd", "checkexports"])
        if check.returncode != 0:
            if backup.exists():
                shutil.copy2(backup, self.exports_file)
            else:
                self.exports_file.write_text(existing)
            return f"nfsd rejected the exports, restored the old file: {check.stderr.strip()}"
        status = self.run(["nfsd", "status"])
        if "is running" not in (status.stdout or ""):
            self.run(["nfsd", "enable"])
        self.run(["nfsd", "update"])
        self.events.emit("roots", "exports", f"updated NFS exports ({len(paths)} folder roots)")
        return None

    # -- everything ----------------------------------------------------------------------
    def sync(self, catalog: Catalog) -> List[RootStatus]:
        images = [i for i in catalog.images if i.enabled and i.root_directory]
        statuses = []
        for image in images:
            try:
                status = self.ensure_mounted(image)
            except OSError as exc:
                status = RootStatus(image, False, str(exc))
            statuses.append(status)
            level = 20 if status.mounted else 40
            self.events.emit("roots", "mount",
                             f"image {image.index} {image.name!r}: {image.root_directory} "
                             f"{status.message}", level=level)
        mounted = [s.image.root_directory for s in statuses if s.mounted]
        if self.nfs_mode == "system" and mounted:
            if sys.platform != "darwin":
                self.events.emit("roots", "unsupported",
                                 "Linux's kernel NFS server can't export HFS+ volumes; "
                                 "use --nfs builtin for 10.0/10.1 images", level=40)
            else:
                error = self.export_system([str(p) for p in mounted])
                if error:
                    self.events.emit("roots", "exports-failed", error, level=40)
        return statuses

    def builtin_exports(self, statuses: Sequence[RootStatus]) -> List[str]:
        return [str(s.image.root_directory) for s in statuses if s.mounted]

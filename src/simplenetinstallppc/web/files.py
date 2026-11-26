"""Uploading disc images for a build.

Uploads put discs from the browser's computer into ``<data root>/sources``;
builds from the web UI use only those. (Discs already on the server are built
in place from the command line, with ``make-test-image``.) Discs are often several
GB, so the page sends each file in chunks (``POST /api/uploads``, one chunk
per request) to ``sources/.uploads/<name>.part``, and can ask how much has
arrived to resume after an interruption. The last chunk moves the file into
``sources/``.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Dict

from .jobs import SOURCE_SUFFIXES

UPLOADS = ".uploads"
MAX_CHUNK = 64 * 1024 * 1024
SPARE_BYTES = 256 * 1024 * 1024       # leave this much free after an upload
COPY_BLOCK = 1024 * 1024


class FileError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def is_disc_image(name: str) -> bool:
    return not name.startswith(".") and Path(name).suffix.lower() in SOURCE_SUFFIXES


class Uploads:
    def __init__(self, sources_dir: Path) -> None:
        self.sources_dir = sources_dir
        self.partial_dir = sources_dir / UPLOADS
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _check_name(self, name: str) -> str:
        if (not name or len(name) > 200 or "/" in name or "\\" in name or "\0" in name
                or not name.isprintable() or not is_disc_image(name)):
            raise FileError(400, f"{name!r}: not a disc image name (.iso, .cdr, .dmg, .img, "
                                 ".toast, .dc42, .image)")
        return name

    def _lock(self, name: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(name, threading.Lock())

    def _partial(self, name: str) -> Path:
        return self.partial_dir / (name + ".part")

    def status(self, name: str) -> Dict[str, object]:
        name = self._check_name(name)
        final = self.sources_dir / name
        partial = self._partial(name)
        return {"name": name, "path": str(final),
                "done": final.is_file(), "size": final.stat().st_size if final.is_file() else None,
                "received": partial.stat().st_size if partial.is_file() else 0}

    def write(self, name: str, offset: int, total: int, length: int, body) -> Dict[str, object]:
        """Append one chunk (``length`` bytes read from ``body``) at ``offset``."""
        name = self._check_name(name)
        if total < 0 or offset < 0 or length < 0 or offset + length > total:
            raise FileError(400, "bad offset, length or total")
        if length > MAX_CHUNK:
            raise FileError(413, f"chunks are at most {MAX_CHUNK // (1024 * 1024)} MiB")
        final = self.sources_dir / name
        with self._lock(name):
            if final.exists():
                raise FileError(409, f"{name} is already in {self.sources_dir}")
            self.partial_dir.mkdir(parents=True, exist_ok=True)
            partial = self._partial(name)
            have = partial.stat().st_size if partial.exists() else 0
            if offset != have:
                raise FileError(409, f"expected offset {have}")
            if offset == 0:
                free = shutil.disk_usage(self.partial_dir).free
                if free < total + SPARE_BYTES:
                    raise FileError(507, f"not enough space in {self.sources_dir}: "
                                         f"{free // 2**20} MiB free, {total // 2**20} MiB needed")
            with open(partial, "ab") as out:
                left = length
                while left:
                    block = body.read(min(COPY_BLOCK, left))
                    if not block:
                        raise FileError(400, "upload ended early")
                    out.write(block)
                    left -= len(block)
            received = offset + length
            if received == total:
                partial.replace(final)
                try:
                    self.partial_dir.rmdir()          # only if empty
                except OSError:
                    pass
        return {"name": name, "received": received, "done": received == total,
                "path": str(final)}

    def cancel(self, name: str) -> None:
        name = self._check_name(name)
        with self._lock(name):
            self._partial(name).unlink(missing_ok=True)

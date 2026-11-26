"""Image builds started from the web UI.

A build runs ``simplenetinstallppc make-test-image`` as a child process, one at a
time, and keeps its output for the UI. The child is the same CLI command, so
a build from the UI and one from a shell behave the same.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..events import EventBus

SOURCE_SUFFIXES = (".dmg", ".img", ".iso", ".cdr", ".toast", ".dc42", ".image")
DIR_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
MAX_LINES = 5000
PACKAGE_PARENT = str(Path(__file__).resolve().parents[2])   # the folder holding simplenetinstallppc


class BuildError(ValueError):
    pass


@dataclass
class BuildRequest:
    sources: List[str]            # absolute paths, or names in <data root>/sources
    name: Optional[str] = None
    index: Optional[int] = None
    dir_name: Optional[str] = None
    protocol: str = "HTTP"
    force: bool = False


@dataclass
class BuildJob:
    id: int
    request: BuildRequest
    state: str = "running"        # running, done, failed
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None
    returncode: Optional[int] = None
    lines: List[str] = field(default_factory=list)

    def summary(self, with_log: bool = False) -> Dict[str, object]:
        out: Dict[str, object] = {
            "id": self.id, "state": self.state, "started": self.started,
            "finished": self.finished, "returncode": self.returncode,
            "sources": self.request.sources, "name": self.request.name,
            "index": self.request.index, "dir": self.request.dir_name,
            "protocol": self.request.protocol,
            "last_line": self.lines[-1] if self.lines else "",
        }
        if with_log:
            out["log"] = self.lines
        return out


def list_sources(sources_dir: Path) -> List[Dict[str, object]]:
    """Disk images in the sources folder (symbolic links are followed)."""
    if not sources_dir.is_dir():
        return []
    found = []
    for entry in sorted(sources_dir.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith(".") or entry.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        try:
            if not entry.is_file():
                continue
            size = entry.stat().st_size
        except OSError:
            continue
        found.append({"name": entry.name, "size": size})
    return found


class BuildJobs:
    def __init__(self, data_root: Path, events: EventBus,
                 on_success: Optional[Callable[[], None]] = None,
                 command_for: Optional[Callable[[BuildRequest, List[Path]], List[str]]] = None
                 ) -> None:
        self.data_root = data_root
        self.sources_dir = data_root / "sources"
        self.events = events
        self.on_success = on_success
        self.command_for = command_for or self._command
        self.jobs: Dict[int, BuildJob] = {}
        self._lock = threading.Lock()
        self._next = 1

    def _command(self, request: BuildRequest, sources: List[Path]) -> List[str]:
        command = [sys.executable, "-m", "simplenetinstallppc", "--data", str(self.data_root),
                   "make-test-image", *map(str, sources), "--protocol", request.protocol]
        if request.name:
            command += ["--name", request.name]
        if request.index is not None:
            command += ["--index", str(request.index)]
        if request.dir_name:
            command += ["--dir", request.dir_name]
        if request.force:
            command.append("--force")
        return command

    def validate(self, request: BuildRequest) -> List[Path]:
        if not request.sources:
            raise BuildError("pick at least one source disc")
        paths = []
        for source in request.sources:
            if os.path.isabs(source):
                path = Path(os.path.normpath(source))
            elif "/" in source or source in (".", ".."):
                raise BuildError(f"{source!r}: give an absolute path or a name in "
                                 f"{self.sources_dir}")
            else:
                path = self.sources_dir / source
            if path.suffix.lower() not in SOURCE_SUFFIXES or path.name.startswith("."):
                raise BuildError(f"{path.name}: not a disc image")
            if not path.is_file():
                raise BuildError(f"{path} doesn't exist")
            if path in paths:
                raise BuildError(f"{path.name} is picked twice")
            paths.append(path)
        if request.protocol not in ("HTTP", "NFS"):
            raise BuildError("protocol must be HTTP or NFS")
        if request.index is not None and not 1 <= request.index <= 4095:
            raise BuildError("index must be 1-4095")
        if request.dir_name and not DIR_NAME.match(request.dir_name):
            raise BuildError("folder name: up to 32 letters, digits, '.', '_' or '-'")
        if request.name is not None and (len(request.name) > 64 or not request.name.isprintable()):
            raise BuildError("name: up to 64 printable characters")
        return paths

    def start(self, request: BuildRequest) -> BuildJob:
        paths = self.validate(request)
        with self._lock:
            if any(job.state == "running" for job in self.jobs.values()):
                raise BuildError("a build is already running; wait for it to finish")
            job = BuildJob(self._next, request)
            self._next += 1
            self.jobs[job.id] = job
        command = self.command_for(request, paths)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [PACKAGE_PARENT] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        env["PYTHONUNBUFFERED"] = "1"
        try:
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=env)
        except OSError as exc:
            job.state, job.finished = "failed", time.time()
            job.lines.append(f"could not start the build: {exc}")
            raise BuildError(job.lines[-1]) from exc
        self.events.emit("build", "started",
                         f"build {job.id} started: {', '.join(request.sources)}")
        threading.Thread(target=self._run, args=(job, proc), name=f"build-{job.id}",
                         daemon=True).start()
        return job

    def _run(self, job: BuildJob, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            job.lines.append(line)
            if len(job.lines) > MAX_LINES:
                del job.lines[:len(job.lines) - MAX_LINES]
            self.events.emit("build", "output", line, level=10, job=job.id)
        proc.stdout.close()
        job.returncode = proc.wait()
        job.finished = time.time()
        if job.returncode == 0:
            job.state = "done"
            self.events.emit("build", "done",
                             f"build {job.id} finished in {job.finished - job.started:.0f}s")
            if self.on_success is not None:
                try:
                    self.on_success()
                except Exception as exc:  # reload problems are reported, not fatal
                    self.events.emit("build", "reload-failed",
                                     f"build {job.id}: reloading images failed: {exc}", level=40)
        else:
            job.state = "failed"
            self.events.emit("build", "failed", f"build {job.id} failed (exit {job.returncode}): "
                             + (job.lines[-1] if job.lines else "no output"), level=40)

    def list(self) -> List[Dict[str, object]]:
        with self._lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.id, reverse=True)
        return [job.summary() for job in jobs]

    def get(self, job_id: int) -> Optional[BuildJob]:
        with self._lock:
            return self.jobs.get(job_id)

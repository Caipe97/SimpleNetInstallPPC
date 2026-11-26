"""Open the web UI in the user's browser when ``serve`` starts on their desktop.

Only when it's started from a terminal on the machine's own desktop: not
over SSH (the browser would pop up on a screen nobody's looking at), not as
a service, and never as root. Under sudo the browser runs as the user who
ran sudo, in their desktop session:

* Linux: the session's settings (``WAYLAND_DISPLAY``, ``DISPLAY``, the D-Bus
  address...) are read from the shell that started ``serve``, which has them
  when it runs in a desktop terminal and not when it came in over SSH. Then
  ``runuser -u <user> -- env -i ... xdg-open <url>``.
* macOS: when that user owns the screen session (``/dev/console``),
  ``launchctl asuser <uid> sudo -u <user> open <url>``.
"""

from __future__ import annotations

import os
import pwd
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# What a browser needs from the desktop session.
SESSION_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR",
                "DBUS_SESSION_BUS_ADDRESS", "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE",
                "XDG_SESSION_DESKTOP", "DESKTOP_SESSION", "XDG_DATA_DIRS", "LANG")
PATH = "/usr/local/bin:/usr/bin:/bin"


@dataclass
class Proc:
    pid: int
    uid: int
    name: str
    environ: Optional[Dict[str, str]] = None


@dataclass
class User:
    name: str
    uid: int
    home: str


@dataclass
class Situation:
    platform: str
    euid: int
    user: Optional[User]          # who the browser is for: the sudo user, or ourselves
    ancestors: List[Proc] = field(default_factory=list)   # our parent, its parent...
    interactive: bool = True      # started from a terminal
    console_uid: Optional[int] = None    # macOS: who owns the screen session


def browser_command(url: str, s: Situation) -> Tuple[Optional[List[str]], str]:
    """The command that opens url for the user, or None and why not."""
    if not s.interactive:
        return None, "not started from a terminal"
    if any(p.name.startswith("sshd") for p in s.ancestors):
        return None, "started over SSH"
    if s.user is None:
        return None, "running as root without sudo"
    if s.platform == "darwin":
        if s.console_uid != s.user.uid:
            return None, f"{s.user.name} isn't using this Mac's screen"
        if s.euid != 0:
            return ["open", url], "opened"
        return ["launchctl", "asuser", str(s.user.uid), "sudo", "-u", s.user.name,
                "open", url], "opened"
    session = next((p.environ for p in s.ancestors
                    if p.uid == s.user.uid and p.environ
                    and (p.environ.get("WAYLAND_DISPLAY") or p.environ.get("DISPLAY"))), None)
    if session is None:
        return None, "not started from a desktop session"
    env = {"HOME": s.user.home, "USER": s.user.name, "LOGNAME": s.user.name, "PATH": PATH}
    env.update({k: session[k] for k in SESSION_VARS if k in session})
    command = ["env", "-i"] + [f"{k}={v}" for k, v in env.items()] + ["xdg-open", url]
    if s.euid == 0:
        command = ["runuser", "-u", s.user.name, "--"] + command
    return command, "opened"


# -- the real machine ------------------------------------------------------------------
def _linux_ancestors() -> List[Proc]:
    found, pid = [], os.getppid()
    while pid > 1 and len(found) < 30:
        try:
            with open(f"/proc/{pid}/status") as fh:
                status = dict(line.split(":", 1) for line in fh if ":" in line)
            uid = int(status["Uid"].split()[0])
            ppid = int(status["PPid"].strip())
            name = status["Name"].strip()
        except (OSError, KeyError, ValueError):
            break
        environ = None
        try:
            with open(f"/proc/{pid}/environ", "rb") as fh:
                pairs = (item.split(b"=", 1) for item in fh.read().split(b"\0") if b"=" in item)
                environ = {k.decode(errors="replace"): v.decode(errors="replace") for k, v in pairs}
        except OSError:
            pass
        found.append(Proc(pid, uid, name, environ))
        pid = ppid
    return found


def _ps_ancestors() -> List[Proc]:
    found, pid = [], os.getppid()
    while pid > 1 and len(found) < 30:
        try:
            out = subprocess.run(["ps", "-o", "ppid=,uid=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2).stdout.split(None, 2)
            ppid, uid, name = int(out[0]), int(out[1]), os.path.basename(out[2].strip())
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            break
        found.append(Proc(pid, uid, name))
        pid = ppid
    return found


def _user() -> Optional[User]:
    euid = os.geteuid()
    name = os.environ.get("SUDO_USER") if euid == 0 else None
    try:
        entry = pwd.getpwnam(name) if name else (None if euid == 0 else pwd.getpwuid(euid))
    except KeyError:
        return None
    if entry is None or entry.pw_uid == 0:
        return None
    return User(entry.pw_name, entry.pw_uid, entry.pw_dir)


def current_situation() -> Situation:
    platform = sys.platform
    console = None
    if platform == "darwin":
        try:
            console = os.stat("/dev/console").st_uid
        except OSError:
            pass
    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, ValueError):
        interactive = False
    return Situation(platform=platform, euid=os.geteuid(), user=_user(),
                     ancestors=_linux_ancestors() if platform.startswith("linux") else _ps_ancestors(),
                     interactive=interactive, console_uid=console)


def open_in_browser(url: str, situation: Optional[Situation] = None) -> Tuple[bool, str]:
    """Open url for the user if it makes sense here. Returns (opened, why)."""
    command, why = browser_command(url, situation or current_situation())
    if command is None:
        return False, why
    try:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        return False, f"{command[0]}: {exc.strerror or exc}"
    try:
        code = proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        return True, "opened"            # still starting the browser: leave it be
    return (code == 0), ("opened" if code == 0 else f"{command[-2]} exited with {code}")


def ui_url(bind: str, port: int) -> str:
    host = "127.0.0.1" if bind in ("", "0.0.0.0", "::") else bind
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/"


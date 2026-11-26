"""``simplenetinstallppc check``: is this host ready to serve?

Lists what the server needs from the host (commands, the HFS+ kernel module,
firewall ports, the service) and how to fix what's missing. The install
script runs it too. Every probe goes through ``Host`` so tests can fake one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence

SERVICE_NAME = "simplenetinstallppc"
SYSTEMD_UNIT = Path("/etc/systemd/system/simplenetinstallppc.service")
LAUNCHD_LABEL = "local.simplenetinstallppc"
LAUNCHD_PLIST = Path(f"/Library/LaunchDaemons/{LAUNCHD_LABEL}.plist")

# Fixed ports: DHCP/BSDP, TFTP, HTTP (root images), portmapper (10.0/10.1).
FIXED_PORTS = [("67/udp", "DHCP/BSDP"), ("69/udp", "TFTP"), ("80/tcp", "HTTP"),
               ("111/udp", "portmapper")]
# firewalld service names that open the same ports.
FIREWALLD_SERVICES = {"dhcp": ["67/udp"], "tftp": ["69/udp"], "http": ["80/tcp"],
                      "rpc-bind": ["111/udp", "111/tcp"]}

OK, WARN, MISSING = "ok", "warn", "missing"


@dataclass
class Check:
    name: str
    state: str                 # ok, warn (works, but read the fix), missing (won't work)
    detail: str = ""
    fix: List[str] = field(default_factory=list)


@dataclass
class Host:
    platform: str = sys.platform
    which: Callable[[str], Optional[str]] = shutil.which
    run: Callable[[Sequence[str]], subprocess.CompletedProcess] = None  # type: ignore[assignment]
    exists: Callable[[Path], bool] = Path.exists
    read: Callable[[Path], str] = Path.read_text
    euid: int = -1
    version: tuple = tuple(sys.version_info[:3])

    def __post_init__(self) -> None:
        if self.run is None:
            self.run = _run
        if self.euid < 0:
            self.euid = os.geteuid()


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(list(cmd), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(list(cmd), 127, "", str(exc))


def installed_service(host: Optional[Host] = None) -> Optional[str]:
    """How to control the installed service, if there is one: 'systemd' or 'launchd'."""
    host = host or Host()
    if host.platform == "darwin":
        return "launchd" if host.exists(LAUNCHD_PLIST) else None
    return "systemd" if host.exists(SYSTEMD_UNIT) else None


def service_hint(host: Optional[Host] = None) -> str:
    kind = installed_service(host)
    if kind == "systemd":
        return (f"If it's the installed service: sudo systemctl reload {SERVICE_NAME} "
                f"(new images) or sudo systemctl stop {SERVICE_NAME}.")
    if kind == "launchd":
        return f"If it's the installed service: sudo launchctl bootout system/{LAUNCHD_LABEL}."
    return ""


# -- the checks ---------------------------------------------------------------------
def check_python(host: Host) -> Check:
    version = ".".join(str(v) for v in host.version)
    if host.version < (3, 9):
        return Check("Python", MISSING, f"{version}; 3.9 or later is needed",
                     ["install Python 3.9 or later"])
    return Check("Python", OK, version)


def check_root(host: Host) -> Check:
    if host.euid == 0:
        return Check("Running as root", OK, "yes")
    return Check("Running as root", WARN,
                 "no: serving and building need root (ports 67/69/80/111, mounting disc images)",
                 ["run serve and builds with sudo, or install the service"])


def check_commands(host: Host) -> List[Check]:
    if host.platform == "darwin":
        needed = {"hdiutil": "mounts disc images", "ditto": "copies files when merging discs",
                  "diskutil": "reads mounted volumes", "nfsd": "NFS for 10.0-10.2",
                  "caffeinate": "keeps the Mac awake while serving"}
        fix = "these come with macOS"
    else:
        needed = {"ip": "lists network interfaces (iproute2)", "mount": "mounts disc images",
                  "umount": "unmounts them", "modprobe": "loads the hfsplus module (kmod)"}
        fix = "install iproute2, util-linux and kmod with your package manager"
    missing = [name for name in needed if not host.which(name)]
    if missing:
        return [Check("Commands", MISSING,
                      "missing: " + ", ".join(f"{n} ({needed[n]})" for n in missing), [fix])]
    checks = [Check("Commands", OK, ", ".join(needed))]
    if host.platform != "darwin" and not host.which("systemd-inhibit"):
        checks.append(Check("Keep awake", WARN, "no systemd-inhibit: the host may suspend "
                            "during an install", ["turn off automatic suspend while serving"]))
    return checks


def check_hfsplus(host: Host) -> Check:
    """Linux needs the kernel's hfsplus module to mount Mac discs."""
    try:
        if re.search(r"\bhfsplus\b", host.read(Path("/proc/filesystems"))):
            return Check("HFS+ support", OK, "hfsplus is loaded")
    except OSError:
        pass
    found = host.run(["modinfo", "-n", "hfsplus"])
    if found.returncode == 0 and found.stdout.strip():
        return Check("HFS+ support", OK, f"module {found.stdout.strip()}")
    return Check("HFS+ support", MISSING,
                 "the kernel's hfsplus module isn't installed: builds and 10.0/10.1 images "
                 "can't mount Mac discs",
                 ["Fedora: sudo dnf install kernel-modules-core kernel-modules-extra (then "
                  "reboot if the kernel was updated)",
                  "Debian/Ubuntu: sudo apt install linux-modules-extra-$(uname -r) "
                  "(Debian's standard kernel already has it)"])


class FirewallUnreadable(Exception):
    pass


def _firewalld_open(host: Host) -> Optional[List[str]]:
    """Ports open in firewalld's default zone, or None if firewalld isn't running.

    Listing works without root (``--state`` doesn't, through polkit).
    """
    if not host.which("firewall-cmd"):
        return None
    listing = host.run(["firewall-cmd", "--list-ports"])
    if listing.returncode != 0:
        output = (listing.stdout + listing.stderr).lower()
        if "not running" in output or listing.returncode == 252:
            return None
        raise FirewallUnreadable(output.strip().splitlines()[0] if output.strip() else
                                 f"firewall-cmd exited {listing.returncode}")
    ports = listing.stdout.split()
    for service in host.run(["firewall-cmd", "--list-services"]).stdout.split():
        ports += FIREWALLD_SERVICES.get(service, [])
    return ports


def _port_open(port: str, open_ports: List[str]) -> bool:
    number, proto = port.split("/")
    for entry in open_ports:
        span, _, entry_proto = entry.partition("/")
        if entry_proto != proto:
            continue
        low, _, high = span.partition("-")
        if low.isdigit() and int(low) <= int(number) <= int(high or low):
            return True
    return False


def check_firewall(host: Host) -> Optional[Check]:
    if host.platform == "darwin":
        return None
    try:
        open_ports = _firewalld_open(host)
    except FirewallUnreadable as exc:
        return Check("Firewall", WARN, f"couldn't read firewalld's rules: {exc}",
                     ["run the check with sudo"])
    if open_ports is None:
        if host.which("ufw"):
            return Check("Firewall", WARN, "ufw is installed; if it's active, it must let "
                         "the Macs in",
                         ["sudo ufw allow 67/udp; sudo ufw allow 69/udp; sudo ufw allow 80/tcp; "
                          "sudo ufw allow 111/udp; sudo ufw allow 1024:65535/udp"])
        return Check("Firewall", OK, "no firewalld")
    closed = [f"{port} ({what})" for port, what in FIXED_PORTS if not _port_open(port, open_ports)]
    dynamic = _port_open("40000/udp", open_ports)
    if not closed and dynamic:
        return Check("Firewall", OK, "firewalld's default zone lets the Macs in")
    fix = [f"sudo firewall-cmd --permanent --add-port={port}" for port, _ in FIXED_PORTS
           if not _port_open(port, open_ports)]
    if not dynamic:
        fix.append("sudo firewall-cmd --permanent --add-port=1024-65535/udp")
    fix.append("sudo firewall-cmd --reload")
    detail = "firewalld's default zone blocks " + ", ".join(
        closed + ([] if dynamic else ["UDP above 1024 (NFS and bootparamd for 10.0-10.2)"]))
    return Check("Firewall", MISSING, detail, fix)


def filesystem_of(path: Path, host: Host) -> Optional[str]:
    """The filesystem type holding path (e.g. ext4, vfat, exfat, apfs, msdos)."""
    target = str(path)
    mounts = []                      # (mount point, type)
    if host.platform == "darwin":
        # /dev/disk4s1 on /Volumes/STICK (msdos, local, nodev, nosuid)
        for line in host.run(["mount"]).stdout.splitlines():
            match = re.match(r".+? on (.+) \(([^,)]+)", line)
            if match:
                mounts.append((match.group(1), match.group(2)))
    else:
        try:
            text = host.read(Path("/proc/self/mounts"))
        except OSError:
            return None
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                mounts.append((parts[1].replace("\\040", " "), parts[2]))
    best = None
    for point, kind in mounts:
        inside = target == point or target.startswith(point.rstrip("/") + "/")
        if inside and (best is None or len(point) > len(best[0])):
            best = (point, kind)
    return best[1] if best else None


def check_data_folder(host: Host, data_root: Optional[Path], reason: str) -> Optional[Check]:
    if data_root is None:
        return None
    where = f"{data_root} ({reason})"
    probe = data_root if host.exists(data_root) else data_root.parent
    kind = filesystem_of(probe, host)
    if kind in ("vfat", "msdos", "fat", "fat32"):
        return Check("Data folder", WARN,
                     f"{where}, on FAT: no file there can be 4 GiB or larger, and the "
                     "images of 10.3-10.5 DVDs are",
                     ["format the drive as exFAT (Linux and Mac), or ext4 (Linux) or "
                      "Mac OS Extended (Mac)"])
    if not host.exists(data_root):
        return Check("Data folder", OK, f"{where}; made when serve first starts")
    return Check("Data folder", OK, where + (f", {kind}" if kind else ""))


def check_service(host: Host) -> Check:
    kind = installed_service(host)
    if kind == "systemd":
        state = host.run(["systemctl", "is-active", SERVICE_NAME]).stdout.strip() or "unknown"
        return Check("Service", OK if state == "active" else WARN, f"systemd unit, {state}",
                     [] if state == "active" else [f"sudo systemctl start {SERVICE_NAME}",
                                                   f"journalctl -u {SERVICE_NAME} for its log"])
    if kind == "launchd":
        return Check("Service", OK, f"launchd daemon {LAUNCHD_LABEL}")
    return Check("Service", OK, "none: serve runs until you stop it (install.sh --service "
                 "adds one that starts at boot)")


def run_checks(host: Optional[Host] = None, data_root: Optional[Path] = None,
               data_reason: str = "") -> List[Check]:
    host = host or Host()
    checks = [check_python(host), check_root(host)] + check_commands(host)
    data = check_data_folder(host, data_root, data_reason)
    if data is not None:
        checks.append(data)
    if host.platform != "darwin":
        checks.append(check_hfsplus(host))
    firewall = check_firewall(host)
    if firewall is not None:
        checks.append(firewall)
    checks.append(check_service(host))
    return checks


def report(checks: List[Check]) -> str:
    marks = {OK: "ok  ", WARN: "warn", MISSING: "FAIL"}
    lines = []
    for c in checks:
        lines.append(f"[{marks[c.state]}] {c.name}: {c.detail}")
        lines += [f"         fix: {f}" for f in c.fix]
    return "\n".join(lines)

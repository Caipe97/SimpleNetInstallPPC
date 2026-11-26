#!/usr/bin/env python3
"""The real web UI with made-up data, for the README screenshots.

    PYTHONPATH=src python3 docs/screenshots/demo.py            # serve on :18200
    PYTHONPATH=src python3 docs/screenshots/demo.py --shoot    # write docs/screenshots/*.png

Nothing here touches the network or this machine's settings: the images,
Macs, interfaces, log lines and DHCP answers are all invented (placeholder
addresses), and the UI's pages and JSON API are the real ones. --shoot uses
headless Google Chrome (or --chrome PATH).
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from simplenetinstallppc.catalog import Catalog
from simplenetinstallppc.events import EventBus
from simplenetinstallppc.net.dhcp_probe import DhcpAnswer
from simplenetinstallppc.netsettings import NetworkSettings
from simplenetinstallppc.store import Store
from simplenetinstallppc.web.app import WebContext, start_web_server
from simplenetinstallppc.web.jobs import BuildJobs, BuildRequest

HERE = Path(__file__).resolve().parent
SERVER = "192.168.50.10"
MAC = "84:ba:59:00:00:d4"
GiB = 1024 ** 3
MiB = 1024 ** 2

IMAGES = [  # index, folder, name, version, protocol, root size, extra plist keys
    (1, "tiger", "Mac OS X 10.4 Tiger Install", "10.4.6", "HTTP", int(3.5 * GiB), {"IsDefault": True}),
    (2, "cheetah", "Mac OS X 10.0 Cheetah Install", "10.0.3", "NFS", 572 * MiB,
     {"RootDirectory": "roots/cheetah"}),
    (3, "puma", "Mac OS X 10.1 Puma Install", "10.1.5", "NFS", 626 * MiB,
     {"RootDirectory": "roots/puma"}),
    (4, "jaguar", "Mac OS X 10.2 Jaguar Install", "10.2.8", "NFS", int(1.1 * GiB), {}),
    (5, "panther", "Mac OS X 10.3 Panther Install", "10.3.9", "HTTP", int(4.0 * GiB), {}),
    (6, "leopard", "Mac OS X 10.5 Leopard Install", "10.5.6", "HTTP", int(7.2 * GiB),
     {"IsEnabled": False}),
]
MACS = [  # MAC, model, address, image it chose
    ("00:05:02:00:00:a1", "iMac,1", "192.168.50.51", None),
    ("00:30:65:00:00:b2", "PowerMac2,1", "192.168.50.50", 4),
    ("00:11:24:00:00:c3", "PowerBook5,9", "192.168.50.52", 5),
    ("00:0a:95:00:00:e4", "PowerMac3,6", "192.168.50.53", None),
]


def make_images(root: Path) -> None:
    for index, folder, name, version, protocol, size, extra in IMAGES:
        directory = root / "images" / folder
        (directory / "ppc").mkdir(parents=True)
        (directory / "ppc" / "booter").write_bytes(b"booter")
        (directory / "ppc" / "mach.macosx").write_bytes(b"kernel")
        (directory / "ppc" / "mach.macosx.mkext").write_bytes(b"mkext")
        with open(directory / "root.dmg", "wb") as fh:
            fh.truncate(size)                       # sparse: the size the page shows
        info = {"Index": index, "Name": name, "Architectures": ["ppc"], "RootPath": "root.dmg",
                "Type": protocol, "IsInstall": True, "Kind": 1, "IsEnabled": True,
                "IsDefault": False, "osVersion": version,
                "Description": f"Mac OS X {version} from the install discs"}
        info.update(extra)
        if "RootDirectory" in info:                 # a "mounted" 10.0/10.1 root
            mounted = root / info["RootDirectory"]
            mounted.mkdir(parents=True)
            (mounted / "mach_kernel").write_bytes(b"kernel")
        with open(directory / "NBImageInfo.plist", "wb") as fh:
            plistlib.dump(info, fh)


def sample_events(events: EventBus) -> None:
    lines = [
        ("catalog", "image", "image 1 'Mac OS X 10.4 Tiger Install' [ppc] HTTP, enabled", None, 20),
        ("catalog", "image", "image 4 'Mac OS X 10.2 Jaguar Install' [ppc] NFS, enabled", None, 20),
        ("catalog", "default", "default for ppc: 'Mac OS X 10.4 Tiger Install'", None, 20),
        ("dhcp", "probe", "other DHCP server(s) on this network: 192.168.50.1 (offered "
         "192.168.50.120, router 192.168.50.1); answering NetBoot Macs only", None, 20),
        ("server", "started", f"serving on eth0 ({SERVER}/24), DHCP mode full (NetBoot Macs only), "
         "HTTP port 80, NFS disk images (10.2+) from the images folder", None, 20),
        ("dhcp", "offer", "OFFER 192.168.50.50", MACS[1][0], 20),
        ("bsdp", "list", "LIST from PowerMac2,1 (ppc): offered 5 image(s), selected "
         "'Mac OS X 10.2 Jaguar Install'", MACS[1][0], 20),
        ("bsdp", "select", "SELECT 'Mac OS X 10.2 Jaguar Install': booter jaguar/ppc/booter",
         MACS[1][0], 20),
        ("tftp", "sent", "sent 'jaguar/ppc/booter': 112818 bytes in 0.4s", MACS[1][0], 20),
        ("tftp", "sent", "sent 'jaguar/ppc/mach.macosx': 4039824 bytes in 3.1s", MACS[1][0], 20),
        ("dhcp", "ack", "ACK 192.168.50.52", MACS[2][0], 20),
        ("bsdp", "list", "LIST from PowerBook5,9 (ppc): offered 5 image(s), selected "
         "'Mac OS X 10.3 Panther Install'", MACS[2][0], 20),
        ("http", "served", "panther/root.dmg: 412 MiB of 4.0 GiB served", MACS[2][0], 20),
        ("nfs", "mount", "MOUNT /images/jaguar from 192.168.50.50", MACS[1][0], 20),
    ]
    # Spread over the last half hour, as a morning of installs would be.
    start = time.time() - 1800
    for i, (subsystem, kind, message, client, level) in enumerate(lines):
        event = events.emit(subsystem, kind, message, client=client, level=level)
        event.timestamp = start + i * (1700 / len(lines)) + (i % 3) * 7


def build_context(root: Path) -> WebContext:
    make_images(root)
    (root / "sources").mkdir()
    for disc, size in (("Mac OS X 10.4 Install DVD.iso", int(6.8 * GiB)),
                       ("Jaguar Disc 1.cdr", 650 * MiB), ("Jaguar Disc 2.cdr", 610 * MiB)):
        with open(root / "sources" / disc, "wb") as fh:
            fh.truncate(size)
    catalog = Catalog(root / "images")
    catalog.reload()
    events = EventBus()
    store = Store()
    now = time.time()
    for i, (mac, model, ip, chosen) in enumerate(MACS):
        store.update_client(mac, arch="ppc", model=model, ip=ip, first_seen=now - 86400 * (i + 1),
                            last_seen=now - 60 * (i * 7 + 2), selected_image=chosen,
                            last_message=["DISCOVER", "LIST", "SELECT", "INFORM"][i])
    settings = NetworkSettings.from_json({
        "interface": "eth0", "dhcp_mode": "full", "dhcp_clients": "netboot",
        "pool_start": "192.168.50.50", "pool_end": "192.168.50.59",
        "router": "192.168.50.1", "dns": ["192.168.50.1"], "lease_time": 3600})
    interfaces = [{"name": "eth0", "mac": MAC, "up": True, "addresses": [f"{SERVER}/24"]},
                  {"name": "wlan0", "mac": "84:ba:59:00:00:d5", "up": False, "addresses": []}]

    def status():
        lights = [
            {"name": "DHCP / BSDP", "state": "ok",
             "detail": "full DHCP on eth0, pool 192.168.50.50 - 192.168.50.59"},
            {"name": "TFTP", "state": "ok", "detail": f"{SERVER} port 69"},
            {"name": "HTTP", "state": "ok", "detail": "port 80 (10.3+ root images)"},
            {"name": "NFS", "state": "ok",
             "detail": "disk images from the images folder; built-in server on UDP 41251"},
            {"name": "bootparamd", "state": "ok",
             "detail": "UDP 38712; our own portmapper on UDP 111 (eth0)"},
            {"name": "Images", "state": "ok", "detail": "5 enabled"},
        ]
        summary = [["Interface", f"eth0 ({SERVER}/24, {MAC})"], ["Running since", time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 1850))],
                   ["DHCP", "full, answering NetBoot Macs only"],
                   ["Address pool", "192.168.50.50 - 192.168.50.59, lease 3600s"],
                   ["Router / DNS", "192.168.50.1 / 192.168.50.1"],
                   ["TFTP", f"{SERVER} port 69"], ["HTTP images", "port 80"],
                   ["NFS disk images (10.2+)", "images folder"],
                   ["NFS folder roots (10.0/10.1)", "built-in server on UDP 41251"],
                   ["bootparamd", "UDP 38712"], ["Web UI", "http://127.0.0.1:8080"],
                   ["Data folder", "simplenetinstallppc-data next to simplenetinstallppc.pyz"]]
        return {"summary": summary, "running": True, "lights": lights}

    def fake_build(request, sources):
        lines = ["volume #0: hfs+ 'Mac OS X Install DVD' at 32768, 6.6 GiB",
                 "Copying volume 'Mac OS X Install DVD' to images/tiger-dvd/root.dmg...",
                 "  copied 6758 / 6758 MiB", "Building mach.macosx.mkext for ppc...",
                 "  64 kexts, 1719 KiB", "Created images/tiger-dvd",
                 "  name:    Mac OS X 10.4 Tiger Install (index 7)",
                 "  system:  10.4.6 8I127  installer"]
        return [sys.executable, "-c", "".join(f"print({line!r});" for line in lines)]

    jobs = BuildJobs(root, events, command_for=fake_build)
    jobs.start(BuildRequest(["Mac OS X 10.4 Install DVD.iso"], protocol="HTTP"))
    sample_events(events)
    return WebContext(
        events=events, catalog=catalog, data_root=root, store=store, status=status,
        reload=catalog.reload,
        probe=lambda interface=None: [DhcpAnswer(server=ipaddress.IPv4Address("192.168.50.1"),
                                                 source="192.168.50.1",
                                                 offered=ipaddress.IPv4Address("192.168.50.120"))],
        jobs=jobs, cli_defaults={},
        network=lambda: {"settings": settings.to_json(), "running": True, "error": None,
                         "command_line": [], "interfaces": interfaces},
        apply_network=lambda new, force: None)


PAGES = [("start", 520), ("dashboard", 720), ("network", 500), ("images", 450),
         ("build", 540), ("clients", 420), ("logs", 480)]


def find_chrome(given):
    for candidate in ([given] if given else []) + [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            shutil.which("google-chrome") or "", shutil.which("chromium") or ""]:
        if candidate and os.path.exists(candidate):
            return candidate
    raise SystemExit("needs Google Chrome or Chromium for --shoot (give --chrome PATH)")


def shoot(port: int, chrome: str) -> None:
    with tempfile.TemporaryDirectory() as profile:
        for page, height in PAGES:
            out = HERE / f"{page}.png"
            subprocess.run([chrome, "--headless=new", f"--user-data-dir={profile}",
                            "--hide-scrollbars", "--force-device-scale-factor=2",
                            f"--window-size=1000,{height}", "--timeout=3000",
                            f"--screenshot={out}", f"http://127.0.0.1:{port}/#{page}"],
                           check=True, capture_output=True, timeout=60)
            print("wrote", out.relative_to(HERE.parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=18200)
    parser.add_argument("--shoot", action="store_true", help="write the screenshots and exit")
    parser.add_argument("--chrome", help="Chrome or Chromium to take them with")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        ctx = build_context(Path(tmp))
        server = start_web_server("127.0.0.1", args.port, ctx, None)
        time.sleep(1.0)                              # let the sample build finish
        try:
            if args.shoot:
                shoot(args.port, find_chrome(args.chrome))
            else:
                print(f"demo UI on http://127.0.0.1:{args.port}/ (Ctrl-C stops it)")
                while True:
                    time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

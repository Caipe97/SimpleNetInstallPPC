"""Command-line interface: ``simplenetinstallppc <command>``."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import __version__
from .catalog import Catalog
from .events import EventBus, setup_logging


PORTABLE_DIR = "simplenetinstallppc-data"
FILE_ENV = "SIMPLENETINSTALLPPC_FILE"     # the .pyz this runs from, before the private copy


def program_file() -> Optional[Path]:
    """The simplenetinstallppc.pyz being run, or None when run from the source."""
    path = os.environ.get(FILE_ENV) or getattr(globals().get("__loader__"), "archive", None)
    return Path(path) if path else None


def system_data_root(platform: str = sys.platform) -> Path:
    if platform == "darwin":
        return Path("/Library/Application Support/NetBootServer")
    return Path("/var/lib/netboot-server")


def find_data_root(given: Optional[Path] = None) -> Tuple[Path, str]:
    """The data folder (images, settings, uploads, password) and why it's that one:

    1. --data, or $NETBOOT_DATA;
    2. a simplenetinstallppc-data folder next to the .pyz, if it exists, so a
       USB stick can carry the program and everything it serves;
    3. the system folder.
    """
    if given:
        return given, "--data"
    env = os.environ.get("NETBOOT_DATA")
    if env:
        return Path(env), "$NETBOOT_DATA"
    program = program_file()
    if program is not None:
        beside = program.resolve().parent / PORTABLE_DIR
        if beside.is_dir():
            return beside, f"next to {program.name}"
    return system_data_root(), "the system folder"


def default_data_root() -> Path:
    return find_data_root()[0]


def _ip(value: str) -> ipaddress.IPv4Address:
    return ipaddress.IPv4Address(value)


def _defaults(values: List[str]) -> Dict[str, int]:
    result = {}
    for value in values:
        arch, sep, index = value.partition("=")
        if not sep or arch not in ("ppc", "i386") or not index.isdigit():
            raise argparse.ArgumentTypeError(f"--default expects ppc=N or i386=N, got {value!r}")
        result[arch] = int(index)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="simplenetinstallppc",
                                     description="SimpleNetInstallPPC: NetInstall server for Mac OS X "
                                                 "10.0-10.5")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--data", type=Path, default=None,
                        help=f"data folder (default: $NETBOOT_DATA, else a {PORTABLE_DIR} "
                             f"folder next to the .pyz if there is one, else "
                             f"{system_data_root()})")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="check this host has what the server needs, and how to fix it")

    sub.add_parser("interfaces", help="list network interfaces")

    sub.add_parser("images", help="list images and check they are complete")

    inspect = sub.add_parser("inspect", help="list the volumes inside a disk image")
    inspect.add_argument("source", type=Path)

    make = sub.add_parser("make-test-image",
                          help="build an unmodified test image from a disk image (Phase 0)")
    make.add_argument("source", type=Path, nargs="+",
                      help=".dmg/.img/.iso/.cdr of an install CD/DVD; give every disc "
                           "of a multi-CD set (any order) to merge them into one image")
    make.add_argument("--name", help="name shown in Startup Disk / the Option-key picker")
    make.add_argument("--index", type=int, help="image index 1-4095 (default: next free)")
    make.add_argument("--dir", dest="dir_name", help="folder name (keep it short)")
    make.add_argument("--protocol", choices=["HTTP", "NFS"], default="HTTP",
                      help="how the client mounts the root image (10.0-10.2 need NFS)")
    make.add_argument("--volume", type=int, help="volume number from 'inspect'")
    make.add_argument("--force", action="store_true", help="replace an existing folder")

    mk = sub.add_parser("build-mkext",
                        help="(re)build an image's mach.macosx.mkext from its root volume")
    mk.add_argument("image", help="image folder name or index (see 'images')")
    mk.add_argument("--all", action="store_true",
                    help="include every kext, not only those needed at boot")
    mk.add_argument("--no-compress", action="store_true", help="store kexts uncompressed")
    mk.add_argument("--output", type=Path,
                    help="write here instead of <image>/<arch>/mach.macosx.mkext (one arch only)")

    serve = sub.add_parser("serve", help="run the DHCP/BSDP, TFTP and HTTP services")
    # Network settings: when left out, the ones last applied (here or in the web UI) are used.
    serve.add_argument("--interface", "-i",
                       help="interface to serve on, e.g. en0 (default: the last one used)")
    serve.add_argument("--server-ip", type=_ip, help="address to use if the interface has several")
    serve.add_argument("--dhcp-mode", choices=["bsdp-only", "full"],
                       help="bsdp-only: an existing DHCP server hands out addresses (default)")
    serve.add_argument("--dhcp-clients", choices=["netboot", "all"],
                       help="full mode: answer only NetBoot Macs (default, safe next to "
                            "another DHCP server) or every DHCP client (isolated LAN)")
    serve.add_argument("--pool-start", type=_ip)
    serve.add_argument("--pool-end", type=_ip)
    serve.add_argument("--router", type=_ip)
    serve.add_argument("--dns", type=_ip, action="append")
    serve.add_argument("--lease-time", type=int)
    serve.add_argument("--http-port", type=int, default=80, help="0 disables HTTP")
    serve.add_argument("--nfs-export",
                       help="NFS export path that contains the images folder (needed for NFS images)")
    serve.add_argument("--default", action="append", default=[], metavar="ARCH=INDEX",
                       help="default image per architecture, e.g. ppc=3")
    serve.add_argument("--tftp-max-blksize", type=int, default=1468)
    serve.add_argument("--no-bootparamd", action="store_true",
                       help="don't run bootparamd (used by Mac OS X 10.0 clients)")
    serve.add_argument("--nfs", dest="nfs_mode", choices=["auto", "system", "builtin"],
                       default="auto",
                       help="NFS for 10.0/10.1 folder roots: macOS nfsd (system), our own "
                            "read-only NFS v2 server (builtin), or auto (system on macOS, "
                            "builtin on Linux)")
    serve.add_argument("--allow-sleep", action="store_true",
                       help="let the host sleep while serving (by default it's kept awake)")
    serve.add_argument("--no-manage-roots", action="store_true",
                       help="don't mount/export 10.0/10.1 RootDirectory folders automatically")
    serve.add_argument("--no-dhcp-probe", action="store_true",
                       help="don't look for other DHCP servers before starting")
    serve.add_argument("--ignore-other-dhcp", action="store_true",
                       help="start even if the probe finds a DHCP server that conflicts "
                            "(overlapping pool, or --dhcp-clients all)")
    serve.add_argument("--ui-port", type=int, default=8080,
                       help="web UI port (default 8080; 0 turns the UI off)")
    serve.add_argument("--ui-bind", default="127.0.0.1",
                       help="web UI address (default 127.0.0.1: reach it from another "
                            "computer with ssh -L 8080:127.0.0.1:8080 <this host>)")
    serve.add_argument("--ui-password", action="store_true",
                       help="ask for the web UI password on localhost too (on a machine other "
                            "people log in to); on any other --ui-bind address it's always asked")
    serve.add_argument("--log-file", type=Path)

    sub.add_parser("clients", help="list the Macs seen so far and the image each one saved")
    setimg = sub.add_parser("set-image",
                            help="change the image a Mac boots next (works while serve runs)")
    setimg.add_argument("mac", help="the Mac's Ethernet address, e.g. 00:30:65:00:00:b2")
    setimg.add_argument("image", help="image index, or 'default' to clear the Mac's choice")

    probe = sub.add_parser("probe-dhcp", help="list the DHCP servers that answer on an interface")
    probe.add_argument("--interface", "-i", required=True)
    probe.add_argument("--timeout", type=float, default=3.0)
    return parser


def cmd_check(data_root: Path, data_reason: str) -> int:
    from .checks import MISSING, report, run_checks
    checks = run_checks(data_root=data_root, data_reason=data_reason)
    print(report(checks))
    return 1 if any(c.state == MISSING for c in checks) else 0


def cmd_interfaces() -> int:
    from .netif import list_interfaces
    for iface in list_interfaces():
        addresses = ", ".join(str(a) for a in iface.addresses) or "-"
        print(f"{iface.name:10} {'up' if iface.up else 'down':5} {iface.mac or '-':17}  {addresses}")
    return 0


def cmd_images(images_root: Path) -> int:
    catalog = Catalog(images_root)
    catalog.reload()
    if not catalog.images:
        print(f"no images in {images_root}")
        return 1
    for image in catalog.images:
        problems = image.problems()
        print(f"{image.index:5}  {image.name}  [{', '.join(image.architectures)}]  "
              f"{image.protocol}  {'install' if image.is_install else 'netboot'}  "
              f"id=0x{image.image_id:08x}  {image.rel_dir}/")
        for problem in problems:
            print(f"        ! {problem}")
    return 0


def cmd_inspect(source: Path) -> int:
    from .imaging.partitions import find_volumes
    for i, volume in enumerate(find_volumes(source)):
        print(f"#{i}  {volume.kind:11} {volume.scheme:8} offset {volume.offset:>12}  "
              f"{volume.length // (1024 * 1024):>6} MiB  {volume.name!r}"
              f"{'  journaled' if volume.journaled else ''}")
    return 0


def cmd_make(args: argparse.Namespace, images_root: Path) -> int:
    from .imaging.quick_image import make_test_image
    report = make_test_image(args.source[0], images_root, dir_name=args.dir_name,
                             name=args.name, index=args.index, protocol=args.protocol,
                             volume_index=args.volume, force=args.force,
                             extra_sources=args.source[1:])
    print(f"Created {report.directory}")
    print(f"  name:    {report.name} (index {report.index})")
    print(f"  system:  {report.version} {report.build}  installer")
    print(f"  arch:    {', '.join(report.architectures) or 'none'}")
    if report.merged:
        print(f"  merged:  {len(report.merged)} package(s) from the other discs")
    for warning in report.warnings:
        print(f"  warning: {warning}")
    return 0


def cmd_build_mkext(args: argparse.Namespace, images_root: Path) -> int:
    import time
    from .imaging.mkext import build_from_volume
    from .imaging.quick_image import mount_readonly
    catalog = Catalog(images_root)
    catalog.reload()
    image = next((i for i in catalog.images
                  if i.rel_dir == args.image or str(i.index) == args.image), None)
    if image is None:
        print(f"error: no image {args.image!r} in {images_root}", file=sys.stderr)
        return 1
    if args.output and len(image.architectures) != 1:
        print("error: --output needs a single-architecture image", file=sys.stderr)
        return 1
    with mount_readonly(image.directory / image.root_image) as volume:
        for arch in image.architectures:
            output = args.output or image.directory / arch / "mach.macosx.mkext"
            started = time.monotonic()
            print(f"Building {output} ...")
            report = build_from_volume(volume, arch, output, compress=not args.no_compress,
                                       include_all=args.all)
            print(f"  {len(report.included)} kexts included, {len(report.skipped)} skipped "
                  f"(not needed at boot); {report.uncompressed // 1024} KiB -> "
                  f"{report.size // 1024} KiB in {time.monotonic() - started:.0f}s")
    return 0


def _open_store(data_root: Path):
    import sqlite3
    from .store import DB_NAME, Store
    try:
        return Store.open(data_root)
    except sqlite3.Error as exc:
        raise OSError(f"can't open {data_root / DB_NAME}: {exc} (run with sudo?)") from exc


def cmd_clients(data_root: Path, images_root: Path) -> int:
    import time
    catalog = Catalog(images_root)
    catalog.reload()
    store = _open_store(data_root)
    rows = store.clients()
    store.close()
    if not rows:
        print("no Macs seen yet")
        return 0
    print(f"{'MAC':17}  {'model':14} {'arch':5} {'IP':15} {'last seen':16}  saved image")
    for row in rows:
        seen = (time.strftime("%Y-%m-%d %H:%M", time.localtime(row.last_seen))
                if row.last_seen else "-")
        if row.selected_image is None:
            saved = "- (boots the default)"
        else:
            image = catalog.by_index(row.selected_image)
            saved = (f"{row.selected_image} {image.name!r}" if image
                     else f"{row.selected_image} (no such image: boots the default)")
        print(f"{row.mac:17}  {row.model or '-':14} {row.arch or '-':5} {row.ip or '-':15} "
              f"{seen:16}  {saved}")
    return 0


def _mac(text: str) -> str:
    digits = re.sub(r"[^0-9a-fA-F]", "", text)
    if len(digits) != 12:
        raise ValueError(f"not an Ethernet address: {text!r}")
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)).lower()


def cmd_set_image(args: argparse.Namespace, data_root: Path, images_root: Path) -> int:
    mac = _mac(args.mac)
    catalog = Catalog(images_root)
    catalog.reload()
    index = None
    if args.image.lower() not in ("default", "none"):
        index = int(args.image)
        image = catalog.by_index(index)
        if image is None:
            print(f"error: no image with index {index} in {images_root}", file=sys.stderr)
            return 1
    store = _open_store(data_root)
    known = store.client(mac)
    store.set_selected_image(mac, index)
    store.close()
    if index is None:
        print(f"{mac} will boot the default image")
    else:
        print(f"{mac} will boot {image.name!r} (index {index})")
        if known and known.arch and not image.supports(known.arch, known.model):
            print(f"warning: {image.name!r} isn't offered to this Mac "
                  f"({known.model or known.arch}); it will boot the default", file=sys.stderr)
    if not known:
        print("note: this Mac hasn't been seen yet")
    print("A running server uses this from the Mac's next request; no restart needed.")
    return 0


def cmd_probe_dhcp(args: argparse.Namespace) -> int:
    from .net.dhcp_probe import probe
    from .netif import get_interface
    iface = get_interface(args.interface)
    if not iface.mac:
        print(f"error: {iface.name} has no Ethernet address", file=sys.stderr)
        return 1
    own = [str(a.ip) for a in iface.addresses]
    answers = probe(iface.name, iface.mac, ignore=own, timeout=args.timeout)
    if not answers:
        print(f"no DHCP server answered on {iface.name} within {args.timeout:g}s")
        return 1
    for answer in answers:
        print(answer.describe())
    return 0


def cmd_serve(args: argparse.Namespace, images_root: Path, state_dir: Path,
              data_reason: str = "--data") -> int:
    import logging
    from .netsettings import FIELD_NAMES, NetworkSettings
    from .server import ServeOptions, StartupError, serve
    setup_logging(args.verbose, args.log_file)
    logging.getLogger("netboot.server").info("data folder %s (%s)", state_dir, data_reason)
    try:
        # First start (e.g. as a service): make the data folder for the
        # settings, the web UI password and the images.
        images_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    given = {name: getattr(args, name) for name in FIELD_NAMES if getattr(args, name) is not None}
    network = {**NetworkSettings().__dict__, **given}
    options = ServeOptions(
        images_root=images_root, given=set(given), **network,
        http_port=args.http_port, nfs_export=args.nfs_export,
        defaults=_defaults(args.default), tftp_max_blksize=args.tftp_max_blksize,
        bootparamd=not args.no_bootparamd,
        nfs_mode=args.nfs_mode, manage_roots=not args.no_manage_roots,
        keep_awake=not args.allow_sleep,
        dhcp_probe=not args.no_dhcp_probe, ignore_other_dhcp=args.ignore_other_dhcp,
        ui_port=args.ui_port, ui_bind=args.ui_bind, ui_password=args.ui_password,
        state_dir=state_dir if state_dir.is_dir() else None)
    try:
        asyncio.run(serve(options, EventBus()))
    except StartupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


RUN_COPY_ENV = "SIMPLENETINSTALLPPC_RUN_COPY"


def run_from_private_copy() -> None:
    """For ``serve`` from a .pyz: carry on from a private copy of the file.

    Python reads modules and the web pages out of the .pyz while it runs, by
    path. Replacing that file (a newer build, a copy over it) or removing it
    (unplugging the USB stick it's on) would break the running server, so it
    runs from a copy in a temporary folder, deleted when it stops.
    """
    archive = getattr(globals().get("__loader__"), "archive", None)
    if not archive or os.environ.get(RUN_COPY_ENV) or not os.path.isfile(archive):
        return
    import shutil
    import tempfile
    folder = tempfile.mkdtemp(prefix="simplenetinstallppc-")
    copy = os.path.join(folder, os.path.basename(archive))
    try:
        shutil.copy2(archive, copy)
    except OSError:
        shutil.rmtree(folder, ignore_errors=True)
        return                                  # carry on from the original
    os.environ[RUN_COPY_ENV] = folder
    os.environ[FILE_ENV] = archive          # its data folder may be next to the original
    os.execv(sys.executable, [sys.executable, copy] + sys.argv[1:])


def _remove_run_copy() -> None:
    folder = os.environ.get(RUN_COPY_ENV)
    if folder and os.path.basename(folder).startswith("simplenetinstallppc-"):
        import shutil
        shutil.rmtree(folder, ignore_errors=True)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve" and argv is None:
        run_from_private_copy()
        if os.environ.get(RUN_COPY_ENV):
            import atexit
            atexit.register(_remove_run_copy)
    data_root, data_reason = find_data_root(args.data)
    images_root = data_root / "images"
    setup_logging(args.verbose)
    try:
        if args.command == "check":
            return cmd_check(data_root, data_reason)
        if args.command == "interfaces":
            return cmd_interfaces()
        if args.command == "images":
            return cmd_images(images_root)
        if args.command == "inspect":
            return cmd_inspect(args.source)
        if args.command == "make-test-image":
            return cmd_make(args, images_root)
        if args.command == "build-mkext":
            return cmd_build_mkext(args, images_root)
        if args.command == "clients":
            return cmd_clients(data_root, images_root)
        if args.command == "set-image":
            return cmd_set_image(args, data_root, images_root)
        if args.command == "probe-dhcp":
            return cmd_probe_dhcp(args)
        if args.command == "serve":
            return cmd_serve(args, images_root, data_root, data_reason)
    except (OSError, ValueError, argparse.ArgumentTypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # UnsupportedImage and friends
        if args.verbose:
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2

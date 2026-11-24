"""Command-line interface: ``netboot-server <command>``."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__
from .catalog import Catalog
from .events import EventBus, setup_logging


def default_data_root() -> Path:
    env = os.environ.get("NETBOOT_DATA")
    if env:
        return Path(env)
    if sys.platform == "darwin":
        return Path("/Library/Application Support/NetBootServer")
    return Path("/var/lib/netboot-server")


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
    parser = argparse.ArgumentParser(prog="netboot-server",
                                     description="NetBoot server for Mac OS X 10.0-10.5")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--data", type=Path, default=None,
                        help=f"data folder (default {default_data_root()}, or $NETBOOT_DATA)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("interfaces", help="list network interfaces")

    sub.add_parser("images", help="list images and check they are complete")

    inspect = sub.add_parser("inspect", help="list the volumes inside a disk image")
    inspect.add_argument("source", type=Path)

    make = sub.add_parser("make-test-image",
                          help="build an unmodified test image from a disk image (Phase 0)")
    make.add_argument("source", type=Path, help=".dmg/.img/.iso/.cdr of a system or install disc")
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
    serve.add_argument("--interface", "-i", required=True, help="interface to serve on, e.g. en0")
    serve.add_argument("--server-ip", type=_ip, help="address to use if the interface has several")
    serve.add_argument("--dhcp-mode", choices=["bsdp-only", "full"], default="bsdp-only",
                       help="bsdp-only: an existing DHCP server hands out addresses (default)")
    serve.add_argument("--pool-start", type=_ip)
    serve.add_argument("--pool-end", type=_ip)
    serve.add_argument("--router", type=_ip)
    serve.add_argument("--dns", type=_ip, action="append", default=[])
    serve.add_argument("--lease-time", type=int, default=3600)
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
    serve.add_argument("--no-manage-roots", action="store_true",
                       help="don't mount/export 10.0/10.1 RootDirectory folders automatically")
    serve.add_argument("--log-file", type=Path)
    return parser


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
    report = make_test_image(args.source, images_root, dir_name=args.dir_name, name=args.name,
                             index=args.index, protocol=args.protocol,
                             volume_index=args.volume, force=args.force)
    print(f"Created {report.directory}")
    print(f"  name:    {report.name} (index {report.index})")
    print(f"  system:  {report.version} {report.build}  "
          f"{'installer' if report.is_install else 'installed system'}")
    print(f"  arch:    {', '.join(report.architectures) or 'none'}")
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


def cmd_serve(args: argparse.Namespace, images_root: Path, state_dir: Path) -> int:
    from .server import ServeOptions, StartupError, serve
    setup_logging(args.verbose, args.log_file)
    options = ServeOptions(
        interface=args.interface, images_root=images_root, server_ip=args.server_ip,
        dhcp_mode=args.dhcp_mode, pool_start=args.pool_start, pool_end=args.pool_end,
        router=args.router, dns=args.dns, lease_time=args.lease_time,
        http_port=args.http_port, nfs_export=args.nfs_export,
        defaults=_defaults(args.default), tftp_max_blksize=args.tftp_max_blksize,
        bootparamd=not args.no_bootparamd,
        nfs_mode=args.nfs_mode, manage_roots=not args.no_manage_roots,
        state_dir=state_dir if state_dir.is_dir() else None)
    try:
        asyncio.run(serve(options, EventBus()))
    except StartupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    data_root = args.data or default_data_root()
    images_root = data_root / "images"
    setup_logging(args.verbose)
    try:
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
        if args.command == "serve":
            return cmd_serve(args, images_root, data_root)
    except (OSError, ValueError, argparse.ArgumentTypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # UnsupportedImage and friends
        if args.verbose:
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2

# netboot-server

NetInstall for Mac OS X 10.0 – 10.5 (PowerPC and Intel), hosted on Linux
(Debian, Fedora) or macOS 14+. No more USB sticks or optical drives to
install old Macs.

**Status: Phase 0.** The network core and a test-image maker are working. They
are used to check, on real hardware, what each OS X release needs. See
[PLAN.md](PLAN.md) for the roadmap and [docs/PHASE0.md](docs/PHASE0.md) for
the test procedure.

## What works now

- **DHCP/BSDP server.** BSDP-only mode next to an existing DHCP server, or
  full DHCP with an address pool. Per-architecture default image, model
  filters, and the NetBoot 1.0 firmware fallback.
- **TFTP server.** Read-only and path-jailed, with blksize/tsize/timeout options.
- **HTTP server** for root images, with byte ranges, keep-alive and `sendfile`.
- **Mac OS X 10.0 / 10.1:** BOOTP, bootparamd and folder roots are set up
  automatically: auto-mount, a managed `/etc/exports` block on macOS, and a
  built-in read-only NFS v2 server on Linux. The image maker builds an mkext
  when the media lacks one.
- **Image maker.** `make-test-image` turns a `.dmg`/`.img`/`.iso`/`.cdr` of
  an install CD/DVD into a NetInstall image folder. It handles APM, GPT,
  MBR, bare volumes and HFS wrappers. Given all the discs of a multi-CD set,
  it merges them into one image, so the installer never asks for another
  disc.
- **Startup Disk:** each Mac's last selected image is remembered and offered
  as its selected image, as Apple's server does.

## Quick start

```bash
sudo PYTHONPATH=src python3 -m netboot_server make-test-image ~/Images/Panther-Disc1.toast --dir panther
```

```bash
sudo PYTHONPATH=src python3 -m netboot_server serve -i en7 --dhcp-mode full --pool-start 10.0.0.100 --pool-end 10.0.0.150
```

Hold **N** on the Mac at startup.

## Tests

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests
```

## Layout

```
src/netboot_server/
  cli.py              command-line entry point
  server.py           starts DHCP/BSDP + TFTP + HTTP together
  catalog.py          image folders and NBImageInfo.plist
  events.py           logging + event bus (feeds the web UI later)
  netif.py            interface discovery and binding (Linux/macOS)
  net/                dhcp_packet, bsdp, dhcp_server, leases, tftp, http_server
  imaging/            partitions (APM/GPT/MBR/HFS wrapper), quick_image, mkext, merge (multi-CD)
tests/                unit tests (stdlib unittest)
docs/PHASE0.md        hardware test procedure
```

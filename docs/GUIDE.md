# SimpleNetInstallPPC user guide

SimpleNetInstallPPC lets PowerPC Macs start the Mac OS X installer over the
network, without a CD, DVD or USB stick. You give it images of your install
discs. It turns them into NetInstall images and serves them to the Macs on
your network.

- **Mac OS X:** 10.0 Cheetah to 10.5 Leopard.
- **Macs:** New World PowerPC Macs: iMac G3 and later, Blue & White G3, G4,
  G5, iBook, and PowerBook G3 (Lombard, Pismo) and later. Old World Macs
  (beige G3 and earlier) can't NetBoot. Intel Macs aren't supported yet.
- **Server:** Linux (Debian 12+, Fedora 40+, or any distribution with
  systemd) or macOS 14+, with Python 3.9 or later. A wired Ethernet
  connection to the Macs' network.

## 1. Get it running

SimpleNetInstallPPC is one file, `simplenetinstallppc.pyz`. It contains its
own DHCP/BSDP, TFTP, HTTP and NFS servers, so it needs no other server
software: only Python 3.9 or later, which Linux distributions and macOS
already have. Nothing is installed. Copy the file anywhere, a USB stick or
a home folder, and run it:

```bash
sudo ./simplenetinstallppc.pyz serve
```

It serves until you press **Ctrl-C**. While it runs, it keeps the host from
going to sleep. Started in a terminal on the machine's desktop, it also opens
the web UI in your browser (as you, not as root); `--no-browser` stops that.
Over SSH it doesn't, since the browser would open on the server's screen:
use the tunnel in section 2. Settings, images and each Mac's chosen image are kept in the
data folder (section 8), so the next start carries on where you left off.

To make the file from the source folder:

```bash
python3 packaging/build_pyz.py
```

It's written to `dist/simplenetinstallppc.pyz`. To update a copy, stop the
server first: Python reads the file while it runs, so copying a new one over
it can crash it. The commands in this guide
write it as `simplenetinstallppc`; with the file, use
`./simplenetinstallppc.pyz` (or `python3 simplenetinstallppc.pyz`) instead.

### Everything stays next to the file

The images, settings and uploads go in a `simplenetinstallppc-data` folder
next to the file, made the first time it starts:

```
USB stick/
  simplenetinstallppc.pyz
  simplenetinstallppc-data/      images, settings, uploads
```

So the file and what it serves travel together: plug the stick into another
Linux or Mac, run the file, and the same images are there. `check` and the
first line `serve` prints say which data folder is in use. To use another
folder, give `--data <folder>`.

On a USB stick:

- **Format the stick as exFAT** (Linux and Mac), ext4 (Linux only) or Mac OS
  Extended (Mac only). On FAT32 no file can be 4 GiB or larger, and the
  images of 10.3–10.5 DVDs are; `check` warns about it.
- **A stick is slower than an internal disk.** Building merges discs and
  copies gigabytes; serving reads the image once per Mac.
- **Don't unplug it while serving.** The program runs from a private copy,
  but the images are still read from the stick.
- **macOS:** the first time, Terminal asks for access to removable volumes.
  Allow it.

### What the host needs

```bash
./simplenetinstallppc.pyz check
```

lists anything missing and how to fix it. Run it with sudo to check that it
runs as root. In short:

- **Linux:** the kernel's `hfsplus` module, to mount Mac discs. Debian's and
  Fedora's standard kernels include it. If `check` says it's missing on
  Fedora, install `kernel-modules-core` and `kernel-modules-extra`; on
  Ubuntu, install `linux-modules-extra-$(uname -r)`. The `ip`, `mount` and
  `modprobe` commands are there on any normal install.
- **Linux firewall:** the Macs must reach UDP 67 (DHCP and BSDP), UDP 69
  (TFTP), TCP 80 (HTTP) and UDP 111 (portmapper), and UDP ports above 1024.
  Those carry the built-in NFS server and bootparamd, which 10.0–10.2 use.
  `check` reads firewalld's rules and prints the commands to open what's
  closed. Fedora Workstation already opens ports above 1024.
- **No other DHCP or TFTP server on the same machine.** One already running
  there (dnsmasq, a DHCP server, tftpd, or macOS Internet Sharing) holds
  port 67 or 69, and `serve` says so when it starts. A DHCP server on
  another machine, such as your router, is fine (section 3).
- **macOS:** nothing to install. If the Application Firewall asks, allow
  Python to accept incoming connections.

### Optional: install the command, or a service

To type `simplenetinstallppc` instead of the file's path, install it from the
source folder:

```bash
sudo ./packaging/install.sh
```

This copies the file to `/usr/local/bin/simplenetinstallppc`. For a server
that's always on, add `--service`. That also installs a service that starts
it at boot: systemd on Linux, launchd on macOS. Run the script again from a
newer source folder to upgrade. `--uninstall` removes the command and the
service, and keeps the data folder.

## 2. Open the web UI

The web UI listens only on the server itself, at http://127.0.0.1:8080/. On
the server's own desktop it opens by itself when `serve` starts. From another
computer, open an SSH tunnel to the server and use the same
address in your browser:

```bash
ssh -N -L 8080:127.0.0.1:8080 <server>
```

There's no password: only the server itself can connect to that address, and
the SSH tunnel already asked you to log in. Two cases bring back a login
page, with the password in the `ui-password` file in the data folder:

- **`--ui-bind <address>`** makes the UI reachable on the network at that
  address. Anyone who can reach it there and has the password controls the
  server, so prefer the tunnel.
- **`--ui-password`** asks for it on localhost too, for a machine other
  people log in to.

It's the `ui-password` file in the data folder (`check` shows where), readable
only by root.

Each `?` next to an option explains it. Until the server is set up, the UI
opens on **Get started**, which walks through the next four sections.

## 3. Set up the network

On the **Network** page:

1. **Interface:** the wired interface on the Macs' network.
2. **DHCP mode:**
   - **BSDP only:** your router hands out addresses and this server only
     answers the Macs' NetBoot questions. This works for 10.2 and later on
     most Macs.
   - **Full DHCP:** this server also hands out addresses, from its own pool.
     Mac OS X 10.0 and 10.1 need it, and so do the first (tray-loading) iMac
     G3s, whose firmware uses NetBoot 1.0.
3. **Full DHCP on a network with a router:** it's safe if the pool doesn't
   overlap the router's. With **Answer: NetBoot Macs only** (the default),
   the server ignores every other device and leaves it to the router. Pick a
   pool outside the router's range, for example .50–.59 when the router
   leases from .100. Use **every device** only on a network of its own.
4. **Apply:** the server first checks for other DHCP servers. It refuses a
   pool that overlaps theirs, unless you choose to start anyway. If the
   services fail to start, it goes back to the previous settings.

Tips:

- Give the server a fixed address, or a reservation in your router. The
  server announces its own address to the Macs, so it mustn't move between
  boots.
- If the server is also on Wi-Fi in the same network, turn Wi-Fi off. Linux
  may answer the Macs on its Wi-Fi interface, where the server isn't
  listening.

## 4. Build install images

On the **Build** page, drop the disc images and press **Build**:

- **Drop them on the page,** files or whole folders, or use **Choose
  files…**. They upload to the `sources` folder in the data folder and are
  picked for the build. An interrupted upload resumes where it stopped.
- **Uploaded earlier** lists the discs already in `sources`: tick them to
  build from them again.
- **Files it reads:** `.iso`, `.cdr`, `.toast`, `.img`, and `.dmg`,
  including compressed ones. A `.dmg` made by macOS 10.11 or later with
  LZFSE compression must be converted on a Mac first: `hdiutil convert
  in.dmg -format UDTO -o out`.
- **Multi-CD sets:** pick every disc of the set, in any order. They're
  merged into one image, so the installer never asks for another disc.
- **Root:** how the Mac loads the installer.

  | Mac OS X | Root | Notes |
  |---|---|---|
  | 10.0, 10.1 | NFS (folder) | Chosen automatically. Needs full DHCP. |
  | 10.2 | NFS | Choose NFS. |
  | 10.3 – 10.5 | HTTP | The default; faster to set up. NFS works too. |

  On macOS, NFS images of 10.2 and later also need the images folder
  exported and `--nfs-export` added to `serve` (see section 7). On Linux,
  the built-in NFS server handles it.
- **Name:** shown in the Mac's startup picker. **Folder** is the folder's
  name on the server; keep it short (16 characters or fewer).

A build takes from seconds to a few minutes and shows its log as it goes.
The new image appears on the **Images** page, enabled.

**Discs already on the server** (a downloads folder, a USB drive) don't need
uploading. Build them where they are from the command line:

```bash
sudo simplenetinstallppc make-test-image Jaguar-Disc1.iso Jaguar-Disc2.iso --dir jaguar --name "Jaguar Install" --protocol NFS
```

The image appears on the **Images** page after **Reload**.

## 5. Choose the default image

On the **Images** page:

- **Enabled:** only enabled images are offered to Macs.
- **Default:** the image a Mac starts when you hold **N**. If none is
  chosen, it's the enabled image with the lowest index.

These are saved in each image's `NBImageInfo.plist`, so they survive a
restart.

## 6. Start a Mac from the network

Connect the Mac to the same network by Ethernet, then:

| How | What you do |
|---|---|
| **N key** | Hold **N** at the startup chime. Starts the default image. |
| **Startup Manager** | Hold **Option** at the chime, pick the globe, then the image. |
| **Startup Disk** | On a Mac running 10.2 or later: System Preferences → Startup Disk → pick the network image → Restart. |
| **Open Firmware** | Hold **Cmd-Opt-O-F** at the chime, then type `boot enet:<server address>,<folder>\ppc\booter`. Loads only the booter; for diagnosis. |

A flashing globe means the Mac is looking for a server, and a spinning globe
means it's loading. A broken folder or ⊘ means the start failed (see
section 9). The installer opens when the start succeeds.

Tips:

- **The Mac remembers the image chosen with Option or Startup Disk.** That
  choice wins over the default, so holding N won't pick another image. To
  change or clear it, use the **Clients** page, or `sudo simplenetinstallppc
  set-image <mac address> <index|default>`.
- **N or Option does nothing?** Connect only one keyboard. With two, some
  iMacs miss the held keys.
- **Old choices sticking?** Reset NVRAM: hold **Cmd-Opt-P-R** at the chime
  until it chimes again.

## 7. Command line and options

```bash
simplenetinstallppc --help
```

The main commands:

- `serve`: run the server (the service runs this);
- `check`: what this host is missing;
- `images`: the images and whether they're complete;
- `inspect <disc image>`: the volumes inside a disc image;
- `make-test-image`: build an image;
- `clients`: the Macs seen so far;
- `set-image`: change the image a Mac boots;
- `probe-dhcp -i <interface>`: list the DHCP servers on a network.

Each command also takes `--help`, for example `simplenetinstallppc serve --help`.

### Options for every command

These go before the command name, e.g. `simplenetinstallppc -v --data /srv/netboot serve`.

| Option | What it does |
|---|---|
| `--data <folder>` | The data folder (images, settings, uploads). Default: `$NETBOOT_DATA`, else `simplenetinstallppc-data` next to the `.pyz`, else the system folder (section 8). |
| `-v`, `--verbose` | Debug logging. |
| `--version` | Print the version and exit. |

### `serve`

The network options (`--interface` to `--lease-time`) are the same settings as
the web UI's Network page. Left out, the ones last applied (here or in the UI)
are used. Given, they win over the saved ones and are saved in turn.

| Option | Default | What it does |
|---|---|---|
| `-i`, `--interface <name>` | the last one used | Interface to serve on, e.g. `en0` or `eth0` (see `interfaces`). |
| `--server-ip <address>` | the interface's address | Which address to use if the interface has several. |
| `--dhcp-mode bsdp-only\|full` | `bsdp-only` | `bsdp-only`: an existing DHCP server (the router) hands out addresses and this server only answers BSDP. `full`: this server hands out addresses too, from its own pool. |
| `--dhcp-clients netboot\|all` | `netboot` | Full mode only: answer only NetBooting Macs (safe next to another DHCP server), or every DHCP client (an isolated network). |
| `--pool-start <address>`, `--pool-end <address>` | — | Full mode: the first and last address to hand out. Both are required in full mode, must be in the interface's network and mustn't include the server's own address. |
| `--router <address>` | — | Full mode: the gateway given to the Macs. |
| `--dns <address>` | — | Full mode: a DNS server given to the Macs. Repeat it for several. |
| `--lease-time <seconds>` | `3600` | Full mode: lease length. |
| `--http-port <port>` | `80` | Port for the HTTP root images (10.3 to 10.5). `0` turns HTTP off. |
| `--nfs-export <path>` | — | NFS export that contains the images folder. Needed for NFS disk images (10.2) served by macOS's `nfsd`; see below. |
| `--default ARCH=INDEX` | the image set in the UI | Default image per architecture, e.g. `ppc=3`. Repeat it for `i386`. |
| `--tftp-max-blksize <bytes>` | `1468` | Largest TFTP block size offered to the Macs. |
| `--nfs auto\|system\|builtin` | `auto` | NFS for 10.0/10.1 folder roots: macOS's `nfsd` (`system`), the built-in read-only NFS v2 server (`builtin`), or `auto` (system on macOS, built-in on Linux). |
| `--no-bootparamd` | | Don't run bootparamd (used by 10.0 and 10.1). |
| `--no-manage-roots` | | Don't mount and export the 10.0/10.1 root folders automatically. |
| `--no-dhcp-probe` | | Don't look for other DHCP servers before starting. |
| `--ignore-other-dhcp` | | Start even if the probe finds a DHCP server that conflicts (an overlapping pool, or `--dhcp-clients all`). |
| `--allow-sleep` | | Let the host sleep while serving. By default it's kept awake. |
| `--ui-port <port>` | `8080` | Web UI port. `0` turns the UI off. |
| `--ui-bind <address>` | `127.0.0.1` | Web UI address. Any address other than localhost asks for the password (section 2). |
| `--ui-password` | | Ask for the web UI password on localhost too. |
| `--no-browser` | | Don't open the web UI in a browser when `serve` starts on the desktop. |
| `--log-file <path>` | | Also write the log to this file. |

### Other commands

| Command | Arguments and options |
|---|---|
| `check` | none. Exits with 1 if something required is missing. |
| `interfaces` | none. |
| `images` | none. |
| `clients` | none. |
| `inspect <disc image>` | The disc image to list the volumes of. |
| `make-test-image <disc image>...` | One or more `.dmg`/`.img`/`.iso`/`.cdr` files; give every disc of a multi-CD set, in any order, to merge them. `--name <text>`: the name shown in Startup Disk and the Option-key picker. `--index <1-4095>`: image index (default: the next free one). `--dir <folder>`: folder name, keep it short. `--protocol HTTP\|NFS` (default `HTTP`): how the Mac mounts the root; 10.0 to 10.2 need `NFS`. `--volume <n>`: which volume, numbered as `inspect` lists them. `--force`: replace an existing folder. |
| `build-mkext <image>` | (Re)build an image's driver cache. `<image>`: folder name or index (see `images`). `--all`: include every kext, not only those needed at boot. `--no-compress`: store kexts uncompressed. `--output <path>`: write there instead of into the image (single-architecture images only). |
| `set-image <mac> <index\|default>` | The Mac's Ethernet address, and the image index it should boot next, or `default` to clear its choice. Works while `serve` runs. |
| `probe-dhcp -i <interface>` | `-i`, `--interface` (required). `--timeout <seconds>` (default `3`): how long to wait for answers. |

### Giving options to the service

If you installed the service, give it options this way:

- **Linux:** `sudo systemctl edit simplenetinstallppc`, then add
  ```
  [Service]
  ExecStart=
  ExecStart=/usr/local/bin/simplenetinstallppc serve --nfs-export /srv/images
  ```
- **macOS:** add them after `serve` in
  `packaging/local.simplenetinstallppc.plist`, then run the install script
  again.

**NFS images (10.2+) on macOS:** export the images folder read-only to the
Macs' network, then add `--nfs-export` with the same path:

```bash
echo '"/Library/Application Support/NetBootServer/images" -ro -maproot=nobody -network 192.168.1.0 -mask 255.255.255.0' | sudo tee -a /etc/exports
```

```bash
sudo nfsd checkexports && sudo nfsd enable && sudo nfsd update
```

For 10.0 and 10.1, the server manages `/etc/exports` itself, in a block it
marks.

**Service control** (only with `install.sh --service`):

| | Linux | macOS |
|---|---|---|
| Status | `systemctl status simplenetinstallppc` | `sudo launchctl print system/local.simplenetinstallppc` |
| Log | `journalctl -u simplenetinstallppc -f` | `/Library/Logs/SimpleNetInstallPPC.log` |
| Reload images | `sudo systemctl reload simplenetinstallppc` | the Images page's **Reload** |
| Restart | `sudo systemctl restart simplenetinstallppc` | `sudo launchctl kickstart -k system/local.simplenetinstallppc` |

## 8. Files and folders

| | Linux | macOS |
|---|---|---|
| Data folder | `simplenetinstallppc-data` next to `simplenetinstallppc.pyz` | same |
| Data folder when installed in `/usr/local/bin`, or run from the source folder | `/var/lib/netboot-server` | `/Library/Application Support/NetBootServer` |
| Images | `images/` in the data folder, one folder per image | same |
| Uploads | `sources/` | same |
| Settings, Macs, leases | `db.sqlite` | same |
| Web UI password (only with `--ui-bind` or `--ui-password`) | `ui-password` | same |
| Program | wherever you put `simplenetinstallppc.pyz`; `/usr/local/bin/simplenetinstallppc` if installed | same |

To put the data somewhere else, give `--data <folder>` or set
`$NETBOOT_DATA`; either wins over the defaults. Deleting an image folder (then **Reload**) removes the
image.

## 9. Troubleshooting

The **Logs** page shows what each Mac asked for. Read the dashboard's status
lights and **Recent warnings** too. Where a start stops tells you why:

| Last thing in the log for that Mac | What's wrong |
|---|---|
| nothing | The Mac's request doesn't reach the server: wrong interface, firewall, cable, or an Old World Mac. |
| `left to the network's DHCP server` (shown at the debug level) and nothing after | BSDP only, but no router answered the Mac, or the Mac needs full DHCP (10.0, 10.1, tray-loading iMac). Use full DHCP. |
| `LIST` but no `SELECT` | The Mac didn't like the image list. Try with only one image enabled. |
| `sent '.../booter'` and nothing after | The booter didn't find its kernel. Check the image on the Images page. |
| `sent '.../mach.macosx'` and the `.mkext`, then nothing | The kernel started but couldn't reach its root: for NFS, the firewall (UDP above 1024) or the NFS export. |
| root image served, then a hang | Start the Mac verbose to see where: in Open Firmware, `setenv boot-args -v`, then `reset-all`, then hold N. Undo it with `set-default boot-args`. |

Other problems:

- **The server host went to sleep mid-install:** the server keeps the host
  awake while it runs (look for `keeping this host awake` in the log). On
  Linux this needs systemd-inhibit.
- **"port … is already in use":** another server is running, maybe one
  started by hand next to the service. Stop one of them.
- **"another DHCP server offers an address inside the pool":** move the pool
  outside the router's range (section 3).

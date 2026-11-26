#!/bin/bash
# Optional: put the simplenetinstallppc command on this host's PATH, and if you
# want, a service that starts it at boot. Not needed to use it: the single
# file from packaging/build_pyz.py runs from anywhere (sudo ./simplenetinstallppc.pyz serve).
#
#   sudo ./packaging/install.sh              install the command in /usr/local/bin
#   sudo ./packaging/install.sh --service    ... and a service that starts at boot
#   sudo ./packaging/install.sh --uninstall  remove both (images and settings are kept)
#
# Run it again from a newer source folder to upgrade.
set -euo pipefail

BIN=/usr/local/bin/simplenetinstallppc
UNIT=/etc/systemd/system/simplenetinstallppc.service
LABEL=local.simplenetinstallppc
PLIST=/Library/LaunchDaemons/$LABEL.plist
SRC="$(cd "$(dirname "$0")/.." && pwd)"

service=no
action=install
for arg in "$@"; do
    case "$arg" in
        --service) service=yes ;;
        --uninstall) action=uninstall ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
    esac
done

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run it with sudo"
case "$(uname -s)" in
    Linux) os=linux ;;
    Darwin) os=mac ;;
    *) die "unsupported system: $(uname -s)" ;;
esac
if [ "$os" = mac ]; then
    DATA="/Library/Application Support/NetBootServer"
else
    DATA=/var/lib/netboot-server
fi

service_installed() { [ -f "$UNIT" ] || [ -f "$PLIST" ]; }

# The first version of this script installed into its own Python environment
# here; the single file replaces it.
OLD_PREFIX=/opt/simplenetinstallppc
remove_old_layout() {
    if [ -d "$OLD_PREFIX" ]; then
        say "removing the old install in $OLD_PREFIX"
        rm -rf "$OLD_PREFIX"
    fi
}

stop_service() {
    if [ -f "$UNIT" ]; then
        systemctl disable --now simplenetinstallppc >/dev/null 2>&1 || true
    fi
    if [ -f "$PLIST" ]; then
        launchctl bootout "system/$LABEL" >/dev/null 2>&1 || true
    fi
}

start_service() {
    if [ "$os" = linux ]; then
        systemctl daemon-reload
        systemctl enable --now simplenetinstallppc >/dev/null 2>&1 \
            || die "the service didn't start: journalctl -u simplenetinstallppc"
    else
        launchctl bootstrap system "$PLIST"
    fi
}

if [ "$action" = uninstall ]; then
    if service_installed; then
        say "stopping and removing the service"
        stop_service
        rm -f "$UNIT" "$PLIST"
        [ "$os" = linux ] && systemctl daemon-reload
    fi
    rm -f "$BIN"
    remove_old_layout
    say "removed. Images and settings are still in $DATA"
    exit 0
fi

[ -f "$SRC/packaging/build_pyz.py" ] || die "run it from the SimpleNetInstallPPC source folder"
PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] && "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
    || die "needs Python 3.9 or later as python3"

# Build into a temporary file, then swap it in: a running server keeps the old
# file open, so it's never cut off mid-way.
say "installing $BIN"
mkdir -p "$(dirname "$BIN")"
"$PYTHON" "$SRC/packaging/build_pyz.py" --output "$BIN.new" >/dev/null
rm -f "$BIN"          # may be the old install's symlink into $OLD_PREFIX
mv -f "$BIN.new" "$BIN"
remove_old_layout

if [ "$service" = yes ] || service_installed; then
    stop_service
    # A server started by hand would hold the ports (looked for after stopping ours).
    other="$(pgrep -f 'simplenetinstallppc(\.pyz)? .*serve' | grep -v "^$$\$" || true)"
    if [ "$os" = linux ]; then
        command -v systemctl >/dev/null || die "--service needs systemd"
        say "installing the service $UNIT"
        install -m 0644 "$SRC/packaging/simplenetinstallppc.service" "$UNIT"
    else
        say "installing the service $PLIST"
        install -m 0644 -o root -g wheel "$SRC/packaging/$LABEL.plist" "$PLIST"
    fi
    if [ -n "$other" ]; then
        say "not starting it yet: a server you started by hand is running (pid $(echo $other | tr ' ' ','))."
        say "stop that one, then: sudo ./packaging/install.sh --service"
        [ "$os" = linux ] && systemctl daemon-reload && systemctl enable simplenetinstallppc >/dev/null 2>&1
    else
        start_service
        say "service started"
    fi
fi

echo
"$BIN" check || true
echo
if service_installed; then
    if [ "$os" = linux ]; then
        echo "Service:  runs at boot. sudo systemctl stop|start|status simplenetinstallppc"
        echo "Log:      journalctl -u simplenetinstallppc -f"
    else
        echo "Service:  runs at boot. sudo launchctl bootout|bootstrap system $PLIST"
        echo "Log:      /Library/Logs/SimpleNetInstallPPC.log"
    fi
else
    echo "Run it:   sudo simplenetinstallppc serve      (Ctrl-C stops it)"
fi
cat <<EOF
Web UI:   http://127.0.0.1:8080/ while it runs (from another computer:
          ssh -N -L 8080:127.0.0.1:8080 <this host>, then open that address)
EOF

#!/bin/sh
# Shareable diagnostics: deliberately exclude calendar contents and secrets.
set -u
INSTALL_DIR=${FLIGHTWALL_INSTALL_DIR:-/userdata/roms/ports/.data/flightwall}
DATA_DIR=${FLIGHTWALL_DATA_DIR:-/userdata/system/configs/flightwall}
printf 'Flight Wall diagnostics\nArchitecture: '
uname -m
printf 'Kernel: '
uname -r
if [ -f /usr/share/batocera/batocera.version ]; then
    printf 'Batocera: '
    cat /usr/share/batocera/batocera.version
fi
printf 'C library: '
if command -v getconf >/dev/null 2>&1; then getconf GNU_LIBC_VERSION 2>/dev/null || true; else echo 'getconf unavailable'; fi
printf 'Display variable: %s\n' "${DISPLAY:-unset (normal over SSH)}"
printf 'Install directory present: '
if [ -d "$INSTALL_DIR" ]; then echo yes; else echo no; fi
printf 'Current release: '
if [ -L "$INSTALL_DIR/current" ]; then readlink "$INSTALL_DIR/current"; else echo missing; fi
printf 'Google credential file present: '
if [ -f "$DATA_DIR/google-client.json" ] || [ -f "$DATA_DIR/accounts/chris/google-client.json" ] || [ -f "$DATA_DIR/accounts/lisa/google-client.json" ]; then echo yes; else echo no; fi
if [ -x "$INSTALL_DIR/flightwall" ]; then
    "$INSTALL_DIR/flightwall" diagnose
else
    echo 'App CLI missing; installation is incomplete.'
fi
printf '\nLogs are under %s/logs. Review them locally before sharing.\n' "$DATA_DIR"

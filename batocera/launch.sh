#!/bin/sh
# Stable Ports launcher: activate verified updates at a safe GUI boundary.
set -u
INSTALL_DIR=${FLIGHTWALL_INSTALL_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}
DATA_DIR=${FLIGHTWALL_DATA_DIR:-/userdata/system/configs/flightwall}
if [ -z "${FLIGHTWALL_DATA_DIR:-}" ] && [ -f "$INSTALL_DIR/data-path" ]; then
    IFS= read -r DATA_DIR < "$INSTALL_DIR/data-path"
fi
mkdir -p "$DATA_DIR/logs"
chmod 700 "$DATA_DIR" "$DATA_DIR/logs"
export FLIGHTWALL_INSTALL_DIR="$INSTALL_DIR" FLIGHTWALL_DATA_DIR="$DATA_DIR"
if [ -z "${SSL_CERT_FILE:-}" ] && [ -r /etc/ssl/certs/ca-certificates.crt ]; then
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
    export SSL_CERT_FILE
fi
export DISPLAY=${DISPLAY:-:0.0}
SERVICE_SCRIPT=${FLIGHTWALL_SERVICE_FILE:-/userdata/system/services/flightwall}
RESTORE_SERVICE=0
GUI_LOCK="$DATA_DIR/gui-launch.lock"
if ! mkdir "$GUI_LOCK" 2>/dev/null; then
    OWNER_PID=
    if [ -r "$GUI_LOCK/pid" ]; then IFS= read -r OWNER_PID < "$GUI_LOCK/pid"; fi
    case "$OWNER_PID" in
        ''|*[!0-9]*) echo 'The GUI launcher is already starting, or its lock needs local diagnosis.' >&2; exit 1;;
    esac
    if kill -0 "$OWNER_PID" 2>/dev/null; then
        if [ -x "$INSTALL_DIR/current/FlightWall" ] &&
                "$INSTALL_DIR/current/FlightWall" --install-dir "$INSTALL_DIR" --data-dir "$DATA_DIR" focus >>"$DATA_DIR/logs/launcher.log" 2>&1; then
            exit 0
        fi
        echo 'Flight Wall is already open.' >&2
        exit 1
    fi
    rm -f "$GUI_LOCK/pid"
    rmdir "$GUI_LOCK" 2>/dev/null || exit 1
    mkdir "$GUI_LOCK" 2>/dev/null || exit 1
fi
printf '%s\n' "$$" > "$GUI_LOCK/pid"
restore_service() {
    if [ "$RESTORE_SERVICE" -eq 1 ]; then
        "$SERVICE_SCRIPT" start >>"$DATA_DIR/logs/launcher.log" 2>&1 || true
    fi
    rm -f "$GUI_LOCK/pid"
    rmdir "$GUI_LOCK" 2>/dev/null || true
}
trap restore_service EXIT
trap 'exit 143' HUP INT TERM
if [ -d "$INSTALL_DIR/.install-lock" ]; then
    echo 'Flight Wall installation is running. Wait for it to finish before opening the app.' >&2
    exit 1
fi
if [ -f "$SERVICE_SCRIPT" ] && "$SERVICE_SCRIPT" status >/dev/null 2>&1; then
    if ! "$SERVICE_SCRIPT" stop >>"$DATA_DIR/logs/launcher.log" 2>&1; then
        echo 'Could not stop the previous Flight Wall service safely.' >&2
        exit 1
    fi
    RESTORE_SERVICE=1
fi

test -x "$INSTALL_DIR/update-helper/FlightWall" || { echo 'Missing Flight Wall recovery helper.' >&2; exit 1; }
if ! "$INSTALL_DIR/update-helper/FlightWall" --install-dir "$INSTALL_DIR" --data-dir "$DATA_DIR" activate-pending >>"$DATA_DIR/logs/launcher.log" 2>&1; then
    echo 'Update activation failed; keeping the installed release.' >>"$DATA_DIR/logs/launcher.log"
fi
test -x "$INSTALL_DIR/current/FlightWall" || { echo 'Missing Flight Wall executable after recovery.' >&2; exit 1; }

# Parent app marks a new release healthy only after the first rendered frame.
# rollback-update is a no-op after that health marker has been recorded.
"$INSTALL_DIR/current/FlightWall" --install-dir "$INSTALL_DIR" --data-dir "$DATA_DIR" gui "$@" >>"$DATA_DIR/logs/launcher.log" 2>&1
RESULT=$?
if [ "$RESULT" -ne 0 ]; then
    "$INSTALL_DIR/update-helper/FlightWall" --install-dir "$INSTALL_DIR" --data-dir "$DATA_DIR" rollback-update >>"$DATA_DIR/logs/launcher.log" 2>&1 || true
fi
exit "$RESULT"

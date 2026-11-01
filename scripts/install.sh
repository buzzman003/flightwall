#!/bin/sh
# Install the supplied, offline-capable x86_64 Batocera package.
set -eu
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -d "$SCRIPT_DIR/releases" ]; then
    PACKAGE_DIR=$SCRIPT_DIR
else
    PACKAGE_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
fi
INSTALL_DIR=${FLIGHTWALL_INSTALL_DIR:-/userdata/roms/ports/.data/flightwall}
DATA_DIR=${FLIGHTWALL_DATA_DIR:-/userdata/system/configs/flightwall}
PORTS_DIR=${FLIGHTWALL_PORTS_DIR:-/userdata/roms/ports}
SERVICES_DIR=${FLIGHTWALL_SERVICES_DIR:-/userdata/system/services}
if [ -n "${FLIGHTWALL_PACKAGE_VERSION:-}" ]; then
    VERSION=$FLIGHTWALL_PACKAGE_VERSION
elif [ -f "$PACKAGE_DIR/VERSION" ]; then
    IFS= read -r VERSION < "$PACKAGE_DIR/VERSION"
else
    echo 'Missing package VERSION. Extract the complete installer archive first.' >&2
    exit 1
fi

printf '%s\n' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || { echo 'Package version must be major.minor.patch.' >&2; exit 1; }
case "$(uname -m)" in x86_64|amd64) ;; *) echo 'This package requires an x86_64 PC, not a Raspberry Pi.' >&2; exit 1;; esac
for item in launch.sh flightwall service-runner.sh flightwall-service flightwall-tailscale-service; do
    test -f "$PACKAGE_DIR/batocera/$item" || { echo "Incomplete package: missing batocera/$item" >&2; exit 1; }
done
test -f "$PACKAGE_DIR/releases/$VERSION/FlightWall" || { echo "Missing releases/$VERSION/FlightWall. Extract the complete package first." >&2; exit 1; }

# Catch a mismatched glibc/loader before installing any app files.
chmod +x "$PACKAGE_DIR/releases/$VERSION/FlightWall"
if ! "$PACKAGE_DIR/releases/$VERSION/FlightWall" --help >/dev/null; then
    echo 'The bundled executable cannot run on this system. Keep the error above for diagnosis.' >&2
    exit 1
fi
PACKAGE_EXECUTABLE_VERSION=$("$PACKAGE_DIR/releases/$VERSION/FlightWall" --version)
if [ "$PACKAGE_EXECUTABLE_VERSION" != "$VERSION" ]; then
    echo 'The package VERSION does not match its executable.' >&2
    exit 1
fi

mkdir -p "$INSTALL_DIR/releases" "$DATA_DIR" "$PORTS_DIR" "$SERVICES_DIR"
chmod 700 "$DATA_DIR"
if ! mkdir "$INSTALL_DIR/.install-lock" 2>/dev/null; then
    echo 'Another install is running, or a previous interrupted install left .install-lock.' >&2
    exit 1
fi
TEMP_RELEASE="$INSTALL_DIR/releases/.install-$VERSION-$$"
SERVICE_FILE=${FLIGHTWALL_SERVICE_FILE:-$SERVICES_DIR/flightwall}
RESTORE_SERVICE=0
export FLIGHTWALL_INSTALL_DIR="$INSTALL_DIR" FLIGHTWALL_DATA_DIR="$DATA_DIR"
cleanup() {
    rm -rf "$TEMP_RELEASE"
    if [ "$RESTORE_SERVICE" -eq 1 ]; then
        "$SERVICE_FILE" start || echo 'The previous background service needs to be restarted manually.' >&2
    fi
    rmdir "$INSTALL_DIR/.install-lock" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 143' HUP INT TERM

# The GUI checks the installer lock after taking its own launch lock. Check
# that same GUI lock only after taking the installer lock, so neither starts
# while the other is modifying or using the launch/service scripts.
if [ -d "$DATA_DIR/gui-launch.lock" ]; then
    GUI_PID=
    if [ -r "$DATA_DIR/gui-launch.lock/pid" ]; then IFS= read -r GUI_PID < "$DATA_DIR/gui-launch.lock/pid"; fi
    case "$GUI_PID" in
        ''|*[!0-9]*) echo 'Close Flight Wall before installing; its GUI lock needs local diagnosis.' >&2; exit 1;;
    esac
    if kill -0 "$GUI_PID" 2>/dev/null && [ -s "/proc/$GUI_PID/cmdline" ]; then
        echo 'Close Flight Wall on the TV before installing this package.' >&2
        exit 1
    fi
    rm -f "$DATA_DIR/gui-launch.lock/pid"
    rmdir "$DATA_DIR/gui-launch.lock" 2>/dev/null || exit 1
fi
if [ -f "$SERVICE_FILE" ] && "$SERVICE_FILE" status >/dev/null 2>&1; then
    "$SERVICE_FILE" stop || { echo 'Could not stop the existing Flight Wall service safely.' >&2; exit 1; }
    RESTORE_SERVICE=1
fi

if [ -e "$INSTALL_DIR/releases/$VERSION" ]; then
    echo "Keeping existing release $VERSION and all settings."
else
    mkdir "$TEMP_RELEASE"
    cp -R "$PACKAGE_DIR/releases/$VERSION/." "$TEMP_RELEASE/"
    chmod +x "$TEMP_RELEASE/FlightWall"
    mv "$TEMP_RELEASE" "$INSTALL_DIR/releases/$VERSION"
fi
INSTALLED_EXECUTABLE_VERSION=$("$INSTALL_DIR/releases/$VERSION/FlightWall" --version)
if [ "$INSTALLED_EXECUTABLE_VERSION" != "$VERSION" ]; then
    echo 'The existing release directory contains a different executable; it was not overwritten.' >&2
    exit 1
fi
for item in launch.sh flightwall service-runner.sh; do
    cp "$PACKAGE_DIR/batocera/$item" "$INSTALL_DIR/$item"
    chmod 755 "$INSTALL_DIR/$item"
done
cp "$PACKAGE_DIR/scripts/diagnose.sh" "$INSTALL_DIR/diagnose.sh"
chmod 755 "$INSTALL_DIR/diagnose.sh"
if [ -d "$PACKAGE_DIR/docs" ]; then
    mkdir -p "$INSTALL_DIR/docs"
    cp -R "$PACKAGE_DIR/docs/." "$INSTALL_DIR/docs/"
fi
if [ ! -e "$INSTALL_DIR/current" ] && [ ! -L "$INSTALL_DIR/current" ]; then
    ln -s "releases/$VERSION" "$INSTALL_DIR/current"
fi
if [ ! -e "$INSTALL_DIR/update-helper" ] && [ ! -L "$INSTALL_DIR/update-helper" ]; then
    ln -s "releases/$VERSION" "$INSTALL_DIR/update-helper"
fi

# This trusted, locally downloaded package may be installed without a release
# feed. Automatic internet updates continue to require the pinned signature.
# Leave current unchanged until the next GUI launch can confirm startup health.
"$PACKAGE_DIR/releases/$VERSION/FlightWall" --install-dir "$INSTALL_DIR" --data-dir "$DATA_DIR" \
    stage-local-release --release-version "$VERSION"

# Persist custom installation paths without interpreting shell source text.
printf '%s\n' "$INSTALL_DIR" > "$DATA_DIR/install-path"
printf '%s\n' "$DATA_DIR" > "$INSTALL_DIR/data-path"
printf '%s\n' '#!/bin/sh' 'set -eu' \
    'INSTALL_DIR=${FLIGHTWALL_INSTALL_DIR:-/userdata/roms/ports/.data/flightwall}' \
    'exec "$INSTALL_DIR/launch.sh" "$@"' > "$PORTS_DIR/Flight Wall.sh"
chmod 755 "$PORTS_DIR/Flight Wall.sh"
cp "$PACKAGE_DIR/batocera/flightwall-service" "$SERVICES_DIR/flightwall"
cp "$PACKAGE_DIR/batocera/flightwall-tailscale-service" "$SERVICES_DIR/flightwall_tailscale"
chmod 755 "$SERVICES_DIR/flightwall" "$SERVICES_DIR/flightwall_tailscale"

MEDIA_DIR="$PACKAGE_DIR/releases/$VERSION/_internal/assets/ports"
if [ -f "$PACKAGE_DIR/scripts/register-ports-media.py" ] && [ -d "$MEDIA_DIR" ]; then
    if command -v python3 >/dev/null 2>&1; then
        python3 "$PACKAGE_DIR/scripts/register-ports-media.py" --ports-dir "$PORTS_DIR" \
            --assets-dir "$MEDIA_DIR" --backup-dir "$DATA_DIR/ports-media-backups" --apply \
            || echo 'Flight Wall is installed; Ports media needs local review. Existing metadata backups were retained.' >&2
    else
        echo 'Flight Wall is installed; automatic Ports artwork setup needs the system python3 command.' >&2
    fi
fi

echo "Installed Flight Wall $VERSION. No additional background services were enabled."
echo 'If this is an upgrade, the new release activates when you next open Flight Wall from Ports.'
echo "In Batocera: MAIN MENU > GAME SETTINGS > UPDATE GAMELISTS, then PORTS > Flight Wall."
echo "Command line: $INSTALL_DIR/flightwall --help"
echo "Detailed instructions: $INSTALL_DIR/docs/INSTALL.txt"

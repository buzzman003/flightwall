#!/bin/sh
# Developer build; the end user installs the supplied archive instead.
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ -n "${FLIGHTWALL_PACKAGE_VERSION:-}" ]; then
    VERSION=$FLIGHTWALL_PACKAGE_VERSION
elif [ -f "$PROJECT_DIR/VERSION" ]; then
    IFS= read -r VERSION < "$PROJECT_DIR/VERSION"
else
    VERSION=0.1.0
fi
IMAGE=${FLIGHTWALL_BUILD_IMAGE:-flightwall-builder:$VERSION}
OUTPUT_DIR=${FLIGHTWALL_BUILD_OUTPUT:-$PROJECT_DIR/releases/$VERSION}
CONTAINER="flightwall-export-$$"
CA_BUNDLE=${FLIGHTWALL_CA_BUNDLE:-/etc/ssl/certs/ca-certificates.crt}
# Preserve the caller's Docker context, configuration and registry credentials.
printf '%s\n' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || { echo 'Version must be major.minor.patch.' >&2; exit 1; }
test -f "$PROJECT_DIR/flightwall_entry.py" || { echo 'Missing flightwall_entry.py.' >&2; exit 1; }
if [ -e "$OUTPUT_DIR" ]; then
    echo "Output already exists: $OUTPUT_DIR. Choose a new FLIGHTWALL_BUILD_OUTPUT directory." >&2
    exit 1
fi
# Docker supplies any configured reachable proxy; do not replace it with an
# outer-container-only hostname or change its registry credential settings.
docker build --secret "id=flightwall_ca,src=$CA_BUNDLE" \
    -f "$PROJECT_DIR/Dockerfile.batocera" -t "$IMAGE" "$PROJECT_DIR"
cleanup() { docker rm "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT HUP INT TERM
docker create --name "$CONTAINER" "$IMAGE" >/dev/null
mkdir -p "$OUTPUT_DIR"
docker cp "$CONTAINER:/src/dist/FlightWall/." "$OUTPUT_DIR/"
chmod +x "$OUTPUT_DIR/FlightWall"
"$OUTPUT_DIR/FlightWall" --help >/dev/null
BUILT_VERSION=$("$OUTPUT_DIR/FlightWall" --version)
if [ "$BUILT_VERSION" != "$VERSION" ]; then
    echo "Version mismatch: package $VERSION, executable $BUILT_VERSION" >&2
    exit 1
fi
printf '%s\n' "$BUILT_VERSION" > "$OUTPUT_DIR/VERSION"
printf 'Built %s\n' "$OUTPUT_DIR"

#!/bin/sh
# Keep a service pinned to its starting release until an explicit restart.
set -eu
INSTALL_DIR=${FLIGHTWALL_INSTALL_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}
DATA_DIR=${FLIGHTWALL_DATA_DIR:-/userdata/system/configs/flightwall}
if [ -z "${FLIGHTWALL_DATA_DIR:-}" ] && [ -f "$INSTALL_DIR/data-path" ]; then
    IFS= read -r DATA_DIR < "$INSTALL_DIR/data-path"
fi
export FLIGHTWALL_INSTALL_DIR="$INSTALL_DIR" FLIGHTWALL_DATA_DIR="$DATA_DIR"
if [ -z "${SSL_CERT_FILE:-}" ] && [ -r /etc/ssl/certs/ca-certificates.crt ]; then
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
    export SSL_CERT_FILE
fi
RELEASE_DIR=$(CDPATH= cd -- "$INSTALL_DIR/current" && pwd -P)
exec "$RELEASE_DIR/FlightWall" --install-dir "$INSTALL_DIR" --data-dir "$DATA_DIR" service

#!/bin/bash
# Shared helpers for the end-to-end scripts (sourced, not executed).
# shellcheck disable=SC2034
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export VPS_PANEL_SRC="$REPO" VPS_PANEL_SKIP_PULL=1
PASS="ci-password-123"
DOMAIN="panel.test"
# certbot layout: live/*.pem are symlinks into archive/, renewal repoints the symlinks
LE_DIR="/etc/letsencrypt"
CERT_DIR="$LE_DIR/live/$DOMAIN"
ARCHIVE_DIR="$LE_DIR/archive/$DOMAIN"

step() { echo; echo "=== $* ==="; }
check() { echo "  ok: $*"; }
die() { echo "FAIL: $*" >&2; journalctl -u vps-traffic-panel -n 30 --no-pager || true; exit 1; }

make_cert() {
    openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=$DOMAIN" \
        -addext "subjectAltName=DNS:$DOMAIN" -keyout "$1" -out "$2" 2>/dev/null
}

served_serial() {
    echo | openssl s_client -connect "127.0.0.1:$1" -servername "$DOMAIN" 2>/dev/null \
        | openssl x509 -noout -serial 2>/dev/null || true
}

wait_for_serial_change() {
    local port="$1" old="$2" _
    for _ in $(seq 1 40); do
        [ "$(served_serial "$port")" != "$old" ] && return 0
        sleep 1
    done
    return 1
}


#!/bin/bash
# End-to-end test of direct-HTTPS mode: a certificate but no domain, so the panel itself serves TLS
# on its own port (and runs as root to read the private key). Covers install, HTTPS access, switching
# the certificate from the panel (which restarts the panel), renewal and uninstall.
# Run as root on a throwaway machine.
set -euo pipefail

# shellcheck source=tests/e2e/lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

PORT=8088
FIRST=/root/cert/first
SECOND=/root/cert/second
api() { curl -sk -u "admin:$PASS" -H 'X-Panel-Request: 1' -H 'Content-Type: application/json' "https://127.0.0.1:$PORT$1" "${@:2}"; }
file_serial() { openssl x509 -in "$1" -noout -serial; }

step "Install with a certificate and no domain"
mkdir -p "$FIRST"
make_cert "$FIRST/private.key" "$FIRST/fullchain.pem"
VPS_PANEL_USERNAME=admin VPS_PANEL_PASSWORD="$PASS" \
VPS_PANEL_CERT="$FIRST/fullchain.pem" VPS_PANEL_KEY="$FIRST/private.key" VPS_PANEL_DOMAIN='' \
    bash "$REPO/panel.sh" install </dev/null

grep -q '^HTTPS_MODE=uvicorn$' /opt/vps-traffic-panel/.env || die "expected direct HTTPS mode"
[ "$(systemctl show -p User --value vps-traffic-panel)" = root ] || die "direct HTTPS mode should run as root (it reads the key)"
[ "$(stat -c %U /opt/vps-traffic-panel/.env)" = root ] || die ".env should stay root-owned in direct mode"
# the CI checkout belongs to the runner user; the installed copy must still be root's
[ "$(stat -c %U /opt/vps-traffic-panel)" = root ] || die "install dir should be root-owned in direct mode"
[ "$(stat -c %U /opt/vps-traffic-panel/app/main.py)" = root ] || die "installed code should be root-owned in direct mode"
check "direct HTTPS mode, running as root"

code=$(api /api/ping -o /dev/null -w '%{http_code}')
[ "$code" = 200 ] || die "HTTPS ping returned $code"
[ "$(served_serial $PORT)" = "$(file_serial "$FIRST/fullchain.pem")" ] || die "panel does not serve the installed certificate"
api "/api/ssl-status?refresh=1" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d["enabled"] and d["mode"] == "uvicorn" and d["port"] == 8088 and d["reachable"], d
assert d["subject"] == "panel.test" and d["renewal_watch"] is True, d
' || die "ssl-status wrong in direct mode"
check "panel serves HTTPS itself and reports it"

step "Switch certificate from the panel (the panel restarts itself)"
mkdir -p "$SECOND"
make_cert "$SECOND/privkey.pem" "$SECOND/fullchain.pem"
expected=$(file_serial "$SECOND/fullchain.pem")
candidate=$(api /api/ssl/scan -X POST | python3 -c '
import json, sys
print([c["id"] for c in json.load(sys.stdin)["candidates"] if "/root/cert/second/" in c["cert"] and c["usable"]][0])
') || die "second certificate not offered"
# The helper restarts the panel while this request waits, so the connection may drop: verify by serial.
api /api/ssl/apply -X POST -d "{\"candidate\": \"$candidate\"}" -o /tmp/apply-direct.json -m 90 || true
for _ in $(seq 1 40); do
    [ "$(served_serial $PORT)" = "$expected" ] && break
    sleep 1
done
[ "$(served_serial $PORT)" = "$expected" ] || die "panel does not serve the new certificate: $(cat /tmp/apply-direct.json 2>/dev/null)"
grep -q "^SSL_CERTFILE=$SECOND/fullchain.pem$" /opt/vps-traffic-panel/.env || die ".env not updated"
check "switched; the restarted panel serves the new certificate"

step "Renewal of the new certificate restarts the panel with it"
before=$(served_serial $PORT)
make_cert /tmp/direct-renew.key /tmp/direct-renew.pem
cat /tmp/direct-renew.key > "$SECOND/privkey.pem"
cat /tmp/direct-renew.pem > "$SECOND/fullchain.pem"
for _ in $(seq 1 60); do
    now=$(served_serial $PORT)
    [ -n "$now" ] && [ "$now" != "$before" ] && break
    sleep 1
done
[ "$(served_serial $PORT)" = "$(file_serial "$SECOND/fullchain.pem")" ] || die "renewal not applied in direct mode"
check "renewal applied"

step "Uninstall"
vps-panel uninstall -y </dev/null
systemctl list-unit-files | grep -q '^vps-traffic-panel' && die "units left behind"
[ ! -d /opt/vps-traffic-panel ] || die "install dir left behind"
[ ! -d /usr/local/lib/vps-traffic-panel ] || die "helper dir left behind"
check "uninstall cleaned up"

echo
echo "ALL DIRECT-HTTPS E2E CHECKS PASSED"

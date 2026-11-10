#!/bin/bash
# End-to-end test of panel.sh install / cert renewal / update / uninstall on a real systemd host.
# Simulates a typical VPS: ports 443 and 80 already used by another program (like xray),
# an existing certificate in /root/cert, and no Nginx installed yet. Run as root on a throwaway machine.
set -euo pipefail

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

step "Simulate a VPS: other programs on 443 and 80, no Nginx, existing certificate"
apt-get purge -y -qq 'nginx*' >/dev/null 2>&1 || true
rm -rf /etc/nginx
systemctl stop apache2 >/dev/null 2>&1 || true
nohup python3 -m http.server 443 >/dev/null 2>&1 &
nohup python3 -m http.server 80 >/dev/null 2>&1 &
sleep 1
mkdir -p "$CERT_DIR" "$ARCHIVE_DIR"
make_cert "$ARCHIVE_DIR/privkey1.pem" "$ARCHIVE_DIR/fullchain1.pem"
ln -s "../../archive/$DOMAIN/privkey1.pem" "$CERT_DIR/privkey.pem"
ln -s "../../archive/$DOMAIN/fullchain1.pem" "$CERT_DIR/fullchain.pem"

step "Install (non-interactive)"
VPS_PANEL_USERNAME=admin VPS_PANEL_PASSWORD="$PASS" \
VPS_PANEL_CERT="$CERT_DIR/fullchain.pem" VPS_PANEL_KEY="$CERT_DIR/privkey.pem" VPS_PANEL_DOMAIN="$DOMAIN" \
    bash "$REPO/panel.sh" install </dev/null

grep -q '^HTTPS_PORT=8443$' /opt/vps-traffic-panel/.env || die "expected fallback HTTPS port 8443"
check "443 busy -> fell back to 8443"
ss -Htlnp 'sport = :443' | grep -q python || die "panel must not take over port 443"
check "other program still owns 443"
ss -Htln 'sport = :8088' | grep -q '127.0.0.1:8088' || die "backend should listen on 127.0.0.1 only"
ss -Htln 'sport = :8088' | grep -q '0.0.0.0:8088' && die "backend exposed on 0.0.0.0"
check "backend bound to localhost"
[ "$(systemctl show -p User --value vps-traffic-panel)" = "vps-traffic-panel" ] || die "service should run as vps-traffic-panel"
check "runs as unprivileged user"
[ -L /usr/local/bin/vps-panel ] || die "vps-panel shortcut missing"
check "vps-panel shortcut installed"

URL="https://$DOMAIN:8443"
RESOLVE=(--resolve "$DOMAIN:8443:127.0.0.1")
code=$(curl -sk "${RESOLVE[@]}" -o /dev/null -w '%{http_code}' -u "admin:$PASS" "$URL/api/ping")
[ "$code" = 200 ] || die "HTTPS ping via Nginx returned $code"
curl -sk "${RESOLVE[@]}" -u "admin:$PASS" "$URL/api/ping" | grep -q '"version":"1.5.0"' || die "version not served"
check "HTTPS through Nginx works"
code=$(curl -sk "${RESOLVE[@]}" -o /dev/null -w '%{http_code}' -u "admin:$PASS" -X POST "$URL/api/telegram-test")
[ "$code" = 403 ] || die "POST without CSRF header returned $code"
check "CSRF guard active"

ssl_json=$(curl -s -u "admin:$PASS" "http://127.0.0.1:8088/api/ssl-status?refresh=1")
echo "  ssl-status: $ssl_json"
echo "$ssl_json" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d["enabled"] and d["mode"] == "nginx" and d["port"] == 8443, d
assert d["reachable"] and d["subject"] == "panel.test", d
assert d["trusted"] is False and d["error"], d      # self-signed in CI
assert d["renewal_watch"] is True, d
' || die "ssl-status did not describe the served certificate"
check "panel reads its own served certificate"

step "Certificate renewal: certbot-style symlink swap"
systemctl is-active --quiet vps-traffic-panel-cert.path || die "cert watcher not active"
before=$(served_serial 8443)
make_cert "$ARCHIVE_DIR/privkey2.pem" "$ARCHIVE_DIR/fullchain2.pem"
# certbot creates a temporary symlink and renames it over the live one
ln -s "../../archive/$DOMAIN/privkey2.pem" "$CERT_DIR/privkey.pem.tmp" && mv -T "$CERT_DIR/privkey.pem.tmp" "$CERT_DIR/privkey.pem"
ln -s "../../archive/$DOMAIN/fullchain2.pem" "$CERT_DIR/fullchain.pem.tmp" && mv -T "$CERT_DIR/fullchain.pem.tmp" "$CERT_DIR/fullchain.pem"
wait_for_serial_change 8443 "$before" || die "Nginx still serves the old certificate after symlink swap"
check "new certificate served after symlink swap ($before -> $(served_serial 8443))"

step "Certificate renewal: in-place overwrite (acme.sh --install-cert / x-ui style)"
before=$(served_serial 8443)
make_cert /tmp/new3.key /tmp/new3.pem
cat /tmp/new3.key > "$CERT_DIR/privkey.pem"
cat /tmp/new3.pem > "$CERT_DIR/fullchain.pem"
wait_for_serial_change 8443 "$before" || die "Nginx still serves the old certificate after overwrite"
check "new certificate served after overwrite"

step "Certificate renewal: plain rename of a regular file"
before=$(served_serial 8443)
make_cert /tmp/new4.key /tmp/new4.pem
mv /tmp/new4.key "$CERT_DIR/privkey.pem"
mv /tmp/new4.pem "$CERT_DIR/fullchain.pem"
wait_for_serial_change 8443 "$before" || die "Nginx still serves the old certificate after rename"
check "new certificate served after rename"

step "Status"
vps-panel status | tee /tmp/status.txt
grep -q "https://$DOMAIN:8443" /tmp/status.txt || die "status shows wrong URL"
check "status shows URL"
grep -q "PathChanged=$CERT_DIR$" /etc/systemd/system/vps-traffic-panel-cert.path && check "symlinked cert dir is watched" || die "symlinked cert dir not watched"

step "Update (with the old standalone cf-ddns script present)"
printf '[Unit]\nDescription=fake legacy cf-ddns\n[Timer]\nOnUnitActiveSec=5min\n[Install]\nWantedBy=timers.target\n' > /etc/systemd/system/cf-ddns.timer
printf '[Service]\nType=oneshot\nExecStart=/bin/true\n' > /etc/systemd/system/cf-ddns.service
systemctl daemon-reload && systemctl enable --now cf-ddns.timer >/dev/null 2>&1
vps-panel update </dev/null | tee /tmp/update.txt
grep -q "standalone Cloudflare DDNS script" /tmp/update.txt || die "update did not warn about the old cf-ddns script"
check "update warns about the old cf-ddns script"
bash /opt/vps-traffic-panel/scripts/legacy-check.sh remove
[ ! -e /etc/systemd/system/cf-ddns.timer ] || die "legacy-check remove left cf-ddns.timer"
systemctl is-active --quiet cf-ddns.timer && die "cf-ddns.timer still active"
check "legacy-check remove cleaned up cf-ddns"
systemctl is-active --quiet vps-traffic-panel || die "service down after update"
ls /opt/vps-traffic-panel/backups/data.db.*.bak >/dev/null || die "no database backup"
[ "$(stat -c %U /opt/vps-traffic-panel/.env)" = "vps-traffic-panel" ] || die ".env owner changed by update"
code=$(curl -sk "${RESOLVE[@]}" -o /dev/null -w '%{http_code}' -u "admin:$PASS" "$URL/api/ping")
[ "$code" = 200 ] || die "HTTPS ping after update returned $code"
check "update keeps service, backups and permissions"

step "Uninstall"
vps-panel uninstall -y --keep-data </dev/null
systemctl list-unit-files | grep -q '^vps-traffic-panel' && die "units left behind"
[ ! -e /etc/nginx/conf.d/vps-traffic-panel.conf ] || die "nginx config left behind"
[ ! -d /opt/vps-traffic-panel ] || die "install dir left behind"
[ ! -e /usr/local/bin/vps-panel ] || die "shortcut left behind"
id vps-traffic-panel >/dev/null 2>&1 && die "service user left behind"
ls -d /root/vps-traffic-panel-backup-*/data.db >/dev/null || die "--keep-data backup missing"
[ -d "$REPO/.git" ] || die "uninstall without --purge must keep the source"
check "uninstall cleaned up and kept a backup"

echo
echo "ALL E2E CHECKS PASSED"

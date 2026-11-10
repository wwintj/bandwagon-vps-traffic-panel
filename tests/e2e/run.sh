#!/bin/bash
# End-to-end test of panel.sh install / cert renewal / update / uninstall on a real systemd host.
# Simulates a typical VPS: ports 443 and 80 already used by another program (like xray),
# an existing certificate in /root/cert, and no Nginx installed yet. Run as root on a throwaway machine.
set -euo pipefail

# shellcheck source=tests/e2e/lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

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
grep -q '^AUTH_PASSWORD=' /opt/vps-traffic-panel/.env && die "plaintext password written to .env"
grep -q '^AUTH_PASSWORD_HASH=pbkdf2_sha256:' /opt/vps-traffic-panel/.env || die "password hash missing from .env"
grep -q "$PASS" /opt/vps-traffic-panel/.env && die "password appears in .env"
check "only a password hash is stored"
[ -L /usr/local/bin/vps-panel ] || die "vps-panel shortcut missing"
check "vps-panel shortcut installed"

URL="https://$DOMAIN:8443"
RESOLVE=(--resolve "$DOMAIN:8443:127.0.0.1")
code=$(curl -sk "${RESOLVE[@]}" -o /dev/null -w '%{http_code}' -u "admin:$PASS" "$URL/api/ping")
[ "$code" = 200 ] || die "HTTPS ping via Nginx returned $code"
curl -sk "${RESOLVE[@]}" -u "admin:$PASS" "$URL/api/ping" | grep -q '"version":"1.9.0"' || die "version not served"
check "HTTPS through Nginx works"
code=$(curl -sk "${RESOLVE[@]}" -o /dev/null -w '%{http_code}' -u "admin:$PASS" -X POST "$URL/api/telegram-test")
[ "$code" = 403 ] || die "POST without CSRF header returned $code"
check "CSRF guard active"

JAR=/tmp/e2e-cookies.txt
rm -f "$JAR"
code=$(curl -sk "${RESOLVE[@]}" -o /dev/null -w '%{http_code}' "$URL/")
[ "$code" = 302 ] || die "page without login should redirect, got $code"
code=$(curl -sk "${RESOLVE[@]}" -c "$JAR" -o /dev/null -w '%{http_code}' -H 'X-Panel-Request: 1' -H 'Content-Type: application/json' \
    -d "{\"username\": \"admin\", \"password\": \"$PASS\"}" "$URL/api/login")
[ "$code" = 200 ] || die "login failed: $code"
grep -q "vps_panel_session" "$JAR" || die "no session cookie"
# Netscape cookie file: domain, include-subdomains, path, secure, expiry, name, value
awk '$6 == "vps_panel_session" && $4 == "TRUE" {found=1} END {exit !found}' "$JAR" || { cat "$JAR"; die "session cookie not marked Secure over HTTPS"; }
code=$(curl -sk "${RESOLVE[@]}" -b "$JAR" -o /dev/null -w '%{http_code}' "$URL/")
[ "$code" = 200 ] || die "logged-in page returned $code"
curl -sk "${RESOLVE[@]}" -b "$JAR" -c "$JAR" -o /dev/null -X POST -H 'X-Panel-Request: 1' "$URL/api/logout"
code=$(curl -sk "${RESOLVE[@]}" -b "$JAR" -o /dev/null -w '%{http_code}' "$URL/api/summary")
[ "$code" = 401 ] || die "still logged in after logout: $code"
check "login page, secure session cookie and logout through Nginx"

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

step "Root-run code lives outside the panel user's reach"
LIB=/usr/local/lib/vps-traffic-panel
[ "$(stat -c %U "$LIB/cert_helper.py")" = root ] || die "cert helper not owned by root"
runuser -u vps-traffic-panel -- test -w "$LIB/cert_helper.py" && die "panel user can modify the root helper"
runuser -u vps-traffic-panel -- test -w "$LIB" && die "panel user can write the helper directory"
grep -q "ExecStart=/bin/bash $LIB/cert-watch.sh reload" /etc/systemd/system/vps-traffic-panel-cert.service || die "renewal unit still runs code from the install dir"
systemctl is-active --quiet vps-traffic-panel-certctl.path || die "certificate switcher not active"
[ "$(stat -c %U /var/lib/vps-traffic-panel-certctl/requests)" = vps-traffic-panel ] || die "request dir has wrong owner"
check "helper and units are root-owned and outside the install dir"

step "Certificate switcher: scan"
api() { curl -s -u "admin:$PASS" -H 'X-Panel-Request: 1' -H 'Content-Type: application/json' "http://127.0.0.1:8088$1" "${@:2}"; }
NEW_DIR=/root/cert/panel.test
mkdir -p "$NEW_DIR"
make_cert "$NEW_DIR/private.key" "$NEW_DIR/fullchain.pem"
mkdir -p /root/badcert
make_cert /tmp/bad1.key /root/badcert/server.crt
make_cert /root/badcert/server.key /tmp/bad2.crt
scan_json=$(api /api/ssl/scan -X POST)
pick() {  # pick <path-fragment> <field>
    echo "$scan_json" | python3 -c '
import json, sys
c = [x for x in json.load(sys.stdin)["candidates"] if sys.argv[1] in x["cert"]][0]
print(json.dumps(c[sys.argv[2]], ensure_ascii=False) if not isinstance(c[sys.argv[2]], str) else c[sys.argv[2]])
' "$1" "$2"
}
[ "$(pick /etc/letsencrypt/live/panel.test current)" = true ] || die "current certificate not marked: $scan_json"
[ "$(pick /root/cert/panel.test usable)" = true ] || die "new certificate should be usable: $scan_json"
[ "$(pick /root/badcert usable)" = false ] || die "mismatched pair should not be usable"
pick /root/badcert problems | grep -q "不配對" || die "mismatch not explained"
check "scan lists current, usable and broken certificates"

step "Certificate switcher: refuse a broken pair, keep serving the old cert"
before=$(served_serial 8443)
bad_id=$(pick /root/badcert id)
code=$(api /api/ssl/apply -X POST -d "{\"candidate\": \"$bad_id\"}" -o /tmp/apply-bad.json -w '%{http_code}')
[ "$code" = 400 ] && grep -q "不配對" /tmp/apply-bad.json || die "broken pair was not refused: $code $(cat /tmp/apply-bad.json)"
[ "$(served_serial 8443)" = "$before" ] || die "served certificate changed after a refused switch"
check "broken pair refused"

step "Certificate switcher: switch to the new certificate"
new_id=$(pick /root/cert/panel.test id)
expected=$(openssl x509 -in "$NEW_DIR/fullchain.pem" -noout -serial)
code=$(api /api/ssl/apply -X POST -d "{\"candidate\": \"$new_id\"}" -o /tmp/apply.json -w '%{http_code}')
[ "$code" = 200 ] || die "switch failed: $code $(cat /tmp/apply.json)"
[ "$(served_serial 8443)" = "$expected" ] || die "Nginx does not serve the new certificate"
grep -q "^SSL_CERTFILE=$NEW_DIR/fullchain.pem$" /opt/vps-traffic-panel/.env || die ".env not updated"
grep -q "ssl_certificate $NEW_DIR/fullchain.pem;" /etc/nginx/conf.d/vps-traffic-panel.conf || die "nginx config not updated"
check "switched; Nginx serves the new certificate"

before=$(served_serial 8443)
make_cert /tmp/renew.key /tmp/renew.pem
cat /tmp/renew.key > "$NEW_DIR/private.key"
cat /tmp/renew.pem > "$NEW_DIR/fullchain.pem"
wait_for_serial_change 8443 "$before" || die "renewal of the switched-to certificate not picked up"
check "renewal watcher follows the new certificate"

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

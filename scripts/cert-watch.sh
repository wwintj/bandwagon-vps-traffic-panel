#!/bin/bash
# Keeps HTTPS working after certificate renewal (certbot, acme.sh, x-ui, manual copy...).
#
#   cert-watch.sh sync     install or remove the watcher to match .env (called by install/update)
#   cert-watch.sh remove   remove the watcher
#   cert-watch.sh reload   reload Nginx or restart the panel so the new certificate is served
#
# A systemd path unit watches SSL_CERTFILE / SSL_KEYFILE; any change (in-place write or
# symlink swap) triggers the reload service.
set -e

SERVICE_NAME="vps-traffic-panel"
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$INSTALL_DIR/.env"
PATH_UNIT="/etc/systemd/system/${SERVICE_NAME}-cert.path"
RELOAD_UNIT="/etc/systemd/system/${SERVICE_NAME}-cert.service"

env_value() {
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | sed -e 's/^"\(.*\)"$/\1/'
}

remove_units() {
    if [ -f "$PATH_UNIT" ] || [ -f "$RELOAD_UNIT" ]; then
        systemctl disable --now "${SERVICE_NAME}-cert.path" >/dev/null 2>&1 || true
        rm -f "$PATH_UNIT" "$RELOAD_UNIT"
        systemctl daemon-reload
        echo "Certificate watcher removed."
    fi
}

install_units() {
    local cert="$1" key="$2"
    cat > "$RELOAD_UNIT" <<EOF
[Unit]
Description=Apply renewed certificate for VPS Traffic Panel

[Service]
Type=oneshot
# Give the renewal tool a moment to finish writing both the certificate and the key.
ExecStartPre=/bin/sleep 5
ExecStart=/bin/bash $INSTALL_DIR/scripts/cert-watch.sh reload
EOF

    cat > "$PATH_UNIT" <<EOF
[Unit]
Description=Watch VPS Traffic Panel certificate for renewal

[Path]
PathChanged=$cert
PathChanged=$key
Unit=${SERVICE_NAME}-cert.service

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}-cert.path" >/dev/null 2>&1
    systemctl restart "${SERVICE_NAME}-cert.path"
    echo "Certificate watcher enabled for $cert"
}

reload_https() {
    local mode
    mode=$(env_value HTTPS_MODE)
    case "$mode" in
        nginx)
            if nginx -t >/dev/null 2>&1; then
                systemctl reload nginx
                echo "Certificate changed: Nginx reloaded."
            else
                echo "Certificate changed but nginx -t failed; not reloading." >&2
                nginx -t || true
                exit 1
            fi
            ;;
        uvicorn)
            systemctl restart "$SERVICE_NAME"
            echo "Certificate changed: panel restarted."
            ;;
        *)
            echo "HTTPS is not enabled; nothing to reload."
            ;;
    esac
}

case "${1:-}" in
    sync)
        if [ "$(env_value SSL_ENABLED)" = "1" ] && [ -n "$(env_value SSL_CERTFILE)" ] && [ -n "$(env_value SSL_KEYFILE)" ]; then
            install_units "$(env_value SSL_CERTFILE)" "$(env_value SSL_KEYFILE)"
        else
            remove_units
        fi
        ;;
    remove)
        remove_units
        ;;
    reload)
        reload_https
        ;;
    *)
        echo "Usage: $0 {sync|remove|reload}" >&2
        exit 2
        ;;
esac

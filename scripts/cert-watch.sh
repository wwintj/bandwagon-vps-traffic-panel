#!/bin/bash
# Root-side certificate plumbing for VPS Traffic Panel.
#
#   cert-watch.sh sync            install / refresh / remove everything to match .env (install & update call this)
#   cert-watch.sh watch CERT KEY  watch these files for renewal (the certificate switcher calls this)
#   cert-watch.sh reload          reload Nginx or restart the panel so a renewed certificate is served
#   cert-watch.sh remove          remove all units and helper files
#
# What sync sets up when HTTPS is enabled:
#   - vps-traffic-panel-cert.path / .service      reload when the certificate files change
#   - vps-traffic-panel-certctl.path / .service   run the root helper when the panel asks to
#                                                 list or switch certificates
#
# Units only ever execute copies in $LIB_DIR (root-owned). The install directory belongs to the
# unprivileged panel user, so root must never run code from there.
set -e

SERVICE_NAME="vps-traffic-panel"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
LIB_DIR="/usr/local/lib/${SERVICE_NAME}"
CERTCTL_DIR="/var/lib/${SERVICE_NAME}-certctl"
WATCH_PATH_UNIT="/etc/systemd/system/${SERVICE_NAME}-cert.path"
WATCH_SERVICE_UNIT="/etc/systemd/system/${SERVICE_NAME}-cert.service"
CTL_PATH_UNIT="/etc/systemd/system/${SERVICE_NAME}-certctl.path"
CTL_SERVICE_UNIT="/etc/systemd/system/${SERVICE_NAME}-certctl.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INSTALL_DIR="/opt/${SERVICE_NAME}"
RUN_USER="root"
if [ -f "$SERVICE_FILE" ]; then
    dir=$(awk -F= '/^WorkingDirectory=/{print $2; exit}' "$SERVICE_FILE" || true)
    user=$(awk -F= '/^User=/{print $2; exit}' "$SERVICE_FILE" || true)
    [ -n "$dir" ] && INSTALL_DIR="$dir"
    [ -n "$user" ] && RUN_USER="$user"
fi
ENV_FILE="$INSTALL_DIR/.env"

# .env is owned by the panel user: only ever read values from it, never execute it.
env_value() {
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | sed -e 's/^"\(.*\)"$/\1/'
}

install_lib() {
    install -d -o root -g root -m 755 "$LIB_DIR"
    # When run from $LIB_DIR itself (e.g. by the helper) the copies are already in place.
    if [ "$SCRIPT_DIR" != "$LIB_DIR" ]; then
        install -o root -g root -m 755 "$SCRIPT_DIR/cert-watch.sh" "$LIB_DIR/cert-watch.sh"
        install -o root -g root -m 755 "$SCRIPT_DIR/cert_helper.py" "$LIB_DIR/cert_helper.py"
    fi
}

install_watch_units() {
    local cert="$1" key="$2"
    cat > "$WATCH_SERVICE_UNIT" <<EOF
[Unit]
Description=Apply renewed certificate for VPS Traffic Panel

[Service]
Type=oneshot
# Give the renewal tool a moment to finish writing both the certificate and the key.
ExecStartPre=/bin/sleep 5
ExecStart=/bin/bash $LIB_DIR/cert-watch.sh reload
EOF

    # A watch on a symlink follows it to the current target. certbot renews by repointing the
    # live/ symlinks to new files and never touches the old target, so for symlinked paths also
    # watch the containing directory, where the swap shows up as a create/rename.
    local watch_lines="PathChanged=$cert"$'\n'"PathChanged=$key"
    local dirs="" path dir
    for path in "$cert" "$key"; do
        if [ -L "$path" ]; then
            dir=$(dirname "$path")
            case " $dirs " in
                *" $dir "*) ;;
                *) dirs="$dirs $dir"; watch_lines="$watch_lines"$'\n'"PathChanged=$dir" ;;
            esac
        fi
    done

    cat > "$WATCH_PATH_UNIT" <<EOF
[Unit]
Description=Watch VPS Traffic Panel certificate for renewal

[Path]
$watch_lines
Unit=${SERVICE_NAME}-cert.service

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}-cert.path" >/dev/null 2>&1
    systemctl restart "${SERVICE_NAME}-cert.path"
    echo "Certificate watcher enabled for $cert"
}

install_ctl_units() {
    # requests/ is the only place the panel user can write; the result file and the parent
    # directory stay root-owned so the helper never writes into a directory the panel controls.
    install -d -o root -g root -m 755 "$CERTCTL_DIR"
    install -d -o "$RUN_USER" -g "$RUN_USER" -m 700 "$CERTCTL_DIR/requests"

    cat > "$CTL_SERVICE_UNIT" <<EOF
[Unit]
Description=VPS Traffic Panel certificate helper (list / switch certificates)

[Service]
Type=oneshot
TimeoutStartSec=180
ExecStart=/usr/bin/python3 -I $LIB_DIR/cert_helper.py process --install-dir $INSTALL_DIR
EOF

    cat > "$CTL_PATH_UNIT" <<EOF
[Unit]
Description=Run the VPS Traffic Panel certificate helper on request

[Path]
PathChanged=$CERTCTL_DIR/requests
Unit=${SERVICE_NAME}-certctl.service

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}-certctl.path" >/dev/null 2>&1
    systemctl restart "${SERVICE_NAME}-certctl.path"
    echo "Certificate switcher enabled."
}

remove_units() {
    local unit changed=0
    for unit in "${SERVICE_NAME}-cert.path" "${SERVICE_NAME}-cert.service" "${SERVICE_NAME}-certctl.path" "${SERVICE_NAME}-certctl.service"; do
        if [ -f "/etc/systemd/system/$unit" ]; then
            systemctl disable --now "$unit" >/dev/null 2>&1 || true
            rm -f "/etc/systemd/system/$unit"
            changed=1
        fi
    done
    if [ "$changed" -eq 1 ]; then
        systemctl daemon-reload
        echo "Certificate watcher and switcher removed."
    fi
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
            install_lib
            install_watch_units "$(env_value SSL_CERTFILE)" "$(env_value SSL_KEYFILE)"
            install_ctl_units
        else
            remove_units
        fi
        ;;
    watch)
        [ -n "${2:-}" ] && [ -n "${3:-}" ] || { echo "Usage: $0 watch CERT KEY" >&2; exit 2; }
        install_watch_units "$2" "$3"
        ;;
    reload)
        reload_https
        ;;
    remove)
        remove_units
        rm -rf "$LIB_DIR" "$CERTCTL_DIR"
        ;;
    *)
        echo "Usage: $0 {sync|watch CERT KEY|reload|remove}" >&2
        exit 2
        ;;
esac

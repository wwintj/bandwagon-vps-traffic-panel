#!/bin/bash
# Usage: sudo ./scripts/uninstall.sh [--keep-data]
#   --keep-data  copy .env and data.db to /root/vps-traffic-panel-backup-<time> before removal
set -e

SERVICE_NAME="vps-traffic-panel"
SERVICE_USER="vps-traffic-panel"
INSTALL_DIR="${INSTALL_DIR:-/opt/vps-traffic-panel}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
NGINX_CONFIG_FILE="/etc/nginx/conf.d/${SERVICE_NAME}.conf"
KEEP_DATA=0

for arg in "$@"; do
    case "$arg" in
        --keep-data) KEEP_DATA=1 ;;
    esac
done

if [ "$EUID" -ne 0 ]; then
    echo "Error: Please run as root."
    exit 1
fi

if [[ -z "$INSTALL_DIR" || "$INSTALL_DIR" == "/" || "$INSTALL_DIR" == "/opt" ]]; then
    echo "Refusing to remove unsafe install directory: $INSTALL_DIR"
    exit 1
fi

echo "=== VPS Traffic Panel Uninstaller ==="

if [ "$KEEP_DATA" -eq 1 ] && [ -d "$INSTALL_DIR" ]; then
    backup_dir="/root/vps-traffic-panel-backup-$(date +"%Y%m%d_%H%M%S")"
    mkdir -p "$backup_dir"
    chmod 700 "$backup_dir"
    [ -f "$INSTALL_DIR/.env" ] && cp -p "$INSTALL_DIR/.env" "$backup_dir/"
    if [ -f "$INSTALL_DIR/data.db" ]; then
        if command -v sqlite3 >/dev/null 2>&1; then
            sqlite3 "$INSTALL_DIR/data.db" ".backup '$backup_dir/data.db'"
        else
            cp "$INSTALL_DIR/data.db" "$backup_dir/"
        fi
    fi
    echo "Saved .env and data.db to: $backup_dir"
fi

if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
    echo "Stopping and disabling service..."
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl disable "$SERVICE_NAME" 2>/dev/null || true
fi

if [[ -f "$SERVICE_FILE" ]]; then
    echo "Removing systemd service file..."
    rm -f "$SERVICE_FILE"
fi

for unit in "${SERVICE_NAME}-cert.path" "${SERVICE_NAME}-cert.service"; do
    if [[ -f "/etc/systemd/system/$unit" ]]; then
        systemctl disable --now "$unit" >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/$unit"
    fi
done

systemctl daemon-reload

if [[ -f "$NGINX_CONFIG_FILE" ]]; then
    echo "Removing Nginx config: $NGINX_CONFIG_FILE"
    rm -f "$NGINX_CONFIG_FILE"
    if command -v nginx >/dev/null 2>&1 && nginx -t >/dev/null 2>&1; then
        systemctl reload nginx || true
    fi
fi

if [[ -d "$INSTALL_DIR" ]]; then
    echo "Removing install directory: $INSTALL_DIR"
    cd /
    rm -rf "$INSTALL_DIR"
fi

if id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "Removing service user: $SERVICE_USER"
    userdel "$SERVICE_USER" 2>/dev/null || true
fi

echo "Uninstall completed."

#!/bin/bash
# Everything runs inside main(), so bash has parsed the whole file before `git reset`
# can replace this script on disk.
set -e

GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"
SERVICE_NAME="vps-traffic-panel"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEEP_BACKUPS=10

prune_backups() {
    local pattern="$1"
    # Keep the newest $KEEP_BACKUPS files matching the pattern.
    ls -1t "$INSTALL_DIR"/backups/$pattern 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | while IFS= read -r old; do
        rm -f "$old"
    done
}

main() {
    if [ "$EUID" -ne 0 ]; then
        echo -e "${RED}Error: Please run as root.${RESET}"
        exit 1
    fi

    echo -e "${GREEN}=== VPS Traffic Panel Updater ===${RESET}"

    INSTALL_DIR="/opt/vps-traffic-panel"
    RUN_USER="root"
    if [ -f "$SERVICE_FILE" ]; then
        local service_dir service_user
        service_dir=$(awk -F= '/^WorkingDirectory=/{print $2; exit}' "$SERVICE_FILE" || true)
        service_user=$(awk -F= '/^User=/{print $2; exit}' "$SERVICE_FILE" || true)
        [ -n "$service_dir" ] && INSTALL_DIR="$service_dir"
        [ -n "$service_user" ] && RUN_USER="$service_user"
    fi

    if [ ! -d "$INSTALL_DIR" ]; then
        echo -e "${RED}Error: Install directory not found: $INSTALL_DIR${RESET}"
        echo "Run sudo ./scripts/install.sh first."
        exit 1
    fi

    local old_version
    old_version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$INSTALL_DIR/app/__init__.py" 2>/dev/null || true)

    mkdir -p "$INSTALL_DIR/backups"
    local timestamp
    timestamp=$(date +"%Y%m%d_%H%M%S")
    if [ -f "$INSTALL_DIR/.env" ]; then
        cp -p "$INSTALL_DIR/.env" "$INSTALL_DIR/backups/.env.$timestamp.bak"
        chmod 600 "$INSTALL_DIR/backups/.env.$timestamp.bak"
    fi
    if [ -f "$INSTALL_DIR/data.db" ]; then
        # sqlite3 .backup gives a consistent copy even while the service is writing (WAL mode).
        if command -v sqlite3 >/dev/null 2>&1; then
            sqlite3 "$INSTALL_DIR/data.db" ".backup '$INSTALL_DIR/backups/data.db.$timestamp.bak'"
        else
            cp "$INSTALL_DIR/data.db" "$INSTALL_DIR/backups/data.db.$timestamp.bak"
        fi
    fi
    prune_backups '.env.*.bak'
    prune_backups 'data.db.*.bak'

    if [ "${VPS_PANEL_SKIP_PULL:-0}" = "1" ]; then
        echo -e "${YELLOW}Using source already in $SOURCE_DIR.${RESET}"
    elif [ -d "$SOURCE_DIR/.git" ]; then
        echo -e "${YELLOW}Pulling latest source from GitHub...${RESET}"
        git -C "$SOURCE_DIR" fetch origin
        git -C "$SOURCE_DIR" reset --hard origin/main
    else
        echo -e "${YELLOW}Source directory is not a Git repository. Using local files only.${RESET}"
    fi

    echo -e "${YELLOW}Syncing application files...${RESET}"
    rsync -a --delete \
        --exclude={'.git','.github','tests','venv','__pycache__','*.db','*.db-shm','*.db-wal','.env','.env.example','backups','*.bak'} \
        "$SOURCE_DIR/" "$INSTALL_DIR/"
    chmod +x "$INSTALL_DIR"/scripts/*.sh

    cd "$INSTALL_DIR"
    if [ ! -d venv ]; then
        python3 -m venv venv
    fi

    echo -e "${YELLOW}Updating Python dependencies...${RESET}"
    . venv/bin/activate
    pip install -q -r requirements.txt

    # Older installs bound to 0.0.0.0 even behind the Nginx HTTPS proxy, exposing plain HTTP publicly.
    if [ -f .env ] && grep -q '^HTTPS_MODE=nginx' .env && grep -q '^HOST=0\.0\.0\.0' .env; then
        echo -e "${YELLOW}Nginx HTTPS mode detected: binding the panel to 127.0.0.1 so it is only reachable through Nginx.${RESET}"
        sed -i 's/^HOST=0\.0\.0\.0$/HOST=127.0.0.1/' .env
    fi

    # rsync resets the directory owner; the service user must be able to write .env and data.db.
    if [ "$RUN_USER" != "root" ] && id "$RUN_USER" >/dev/null 2>&1; then
        chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR"
        chown root:root "$INSTALL_DIR/backups" && chmod 700 "$INSTALL_DIR/backups"
    fi

    echo -e "${YELLOW}Restarting service...${RESET}"
    systemctl daemon-reload
    systemctl restart "$SERVICE_NAME"

    # Keep HTTPS working across certificate renewals (also enables it for older installs).
    bash "$INSTALL_DIR/scripts/cert-watch.sh" sync

    local new_version
    new_version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$INSTALL_DIR/app/__init__.py" 2>/dev/null || true)
    echo -e "${GREEN}Update completed: v${old_version:-unknown} -> v${new_version:-unknown}${RESET}"
    bash "$INSTALL_DIR/scripts/legacy-check.sh"
    echo -e "Install directory : $INSTALL_DIR"
    echo -e "Backups           : $INSTALL_DIR/backups (newest $KEEP_BACKUPS kept)"
    echo -e "Service status    : systemctl status $SERVICE_NAME"
    echo -e "Logs              : journalctl -u $SERVICE_NAME -f"
}

main "$@"
exit $?

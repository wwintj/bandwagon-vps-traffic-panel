#!/bin/bash
# VPS Traffic Panel — one-command manager.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/wwintj/bandwagon-vps-traffic-panel/main/panel.sh)
#
# After installing, run `vps-panel` for the same menu, or call a command directly:
#   vps-panel install | update | uninstall | status | logs | restart
# Options for uninstall: --keep-data, --purge (also delete the downloaded source), -y
set -e

REPO_URL="${VPS_PANEL_REPO:-https://github.com/wwintj/bandwagon-vps-traffic-panel.git}"
SRC_DIR="${VPS_PANEL_SRC:-/opt/vps-traffic-panel-src}"
INSTALL_DIR="/opt/vps-traffic-panel"
SERVICE_NAME="vps-traffic-panel"
SHORTCUT="/usr/local/bin/vps-panel"

GREEN="\033[32m"
RED="\033[31m"
YELLOW="\033[33m"
CYAN="\033[36m"
RESET="\033[0m"

info() { echo -e "${YELLOW}$*${RESET}"; }
ok() { echo -e "${GREEN}$*${RESET}"; }
fail() { echo -e "${RED}Error: $*${RESET}" >&2; exit 1; }

INTERACTIVE=1
[ -t 0 ] || INTERACTIVE=0

confirm() {
    local prompt="$1" default="${2:-N}" answer
    if [ "$ASSUME_YES" = "1" ]; then
        return 0
    fi
    if [ "$INTERACTIVE" -eq 0 ]; then
        [ "$default" = "Y" ]
        return
    fi
    read -r -p "$prompt " answer
    answer=${answer:-$default}
    [[ "$answer" =~ ^[Yy]$ ]]
}

require_root() {
    [ "$EUID" -eq 0 ] || fail "Please run as root (sudo -i first)."
}

ensure_tools() {
    local missing=()
    command -v git >/dev/null 2>&1 || missing+=(git)
    command -v curl >/dev/null 2>&1 || missing+=(curl)
    if [ "${#missing[@]}" -gt 0 ]; then
        command -v apt-get >/dev/null 2>&1 || fail "This script supports Debian / Ubuntu (apt-get) only."
        info "Installing ${missing[*]}..."
        apt-get update -qq
        apt-get install -y -qq "${missing[@]}" >/dev/null
    fi
}

is_installed() {
    [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ] && [ -d "$INSTALL_DIR" ]
}

env_value() {
    [ -f "$INSTALL_DIR/.env" ] || return 0
    sed -n "s/^$1=//p" "$INSTALL_DIR/.env" | tail -n 1 | sed -e 's/^"\(.*\)"$/\1/'
}

version_of() {
    sed -n 's/^__version__ = "\(.*\)"/\1/p' "$1/app/__init__.py" 2>/dev/null || true
}

fetch_source() {
    if [ -d "$SRC_DIR/.git" ]; then
        if [ "${VPS_PANEL_SKIP_PULL:-0}" != "1" ]; then
            info "Updating source in $SRC_DIR ..."
            git -C "$SRC_DIR" fetch --quiet origin
            git -C "$SRC_DIR" reset --quiet --hard origin/main
        fi
    else
        [ -e "$SRC_DIR" ] && fail "$SRC_DIR exists but is not a git checkout. Move it away and retry."
        info "Downloading source to $SRC_DIR ..."
        git clone --quiet "$REPO_URL" "$SRC_DIR"
    fi
    chmod +x "$SRC_DIR"/panel.sh "$SRC_DIR"/scripts/*.sh
}

install_shortcut() {
    ln -sf "$SRC_DIR/panel.sh" "$SHORTCUT"
}

panel_url() {
    local mode port domain https_port ip
    mode=$(env_value HTTPS_MODE)
    port=$(env_value PORT)
    domain=$(env_value PANEL_DOMAIN)
    https_port=$(env_value HTTPS_PORT)
    ip=$(curl -fsS --max-time 3 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
    case "$mode" in
        nginx)
            if [ -n "$https_port" ] && [ "$https_port" != "443" ]; then
                echo "https://$domain:$https_port"
            else
                echo "https://$domain"
            fi
            ;;
        uvicorn) echo "https://$ip:$port" ;;
        *) echo "http://$ip:$port" ;;
    esac
}

cmd_install() {
    require_root
    ensure_tools
    if is_installed; then
        info "VPS Traffic Panel v$(version_of "$INSTALL_DIR") is already installed."
        info "Reinstalling keeps your traffic history but asks for login and HTTPS settings again."
        confirm "Reinstall? [y/N]:" N || { echo "Cancelled. Use 'update' to upgrade in place."; return; }
    fi
    fetch_source
    bash "$SRC_DIR/scripts/install.sh" "$@"
    install_shortcut
}

cmd_update() {
    require_root
    ensure_tools
    is_installed || fail "VPS Traffic Panel is not installed. Choose install first."
    fetch_source
    VPS_PANEL_SKIP_PULL=1 bash "$SRC_DIR/scripts/update.sh"
    install_shortcut
}

cmd_uninstall() {
    require_root
    local keep_data=0 purge=0 arg
    for arg in "$@"; do
        case "$arg" in
            --keep-data) keep_data=1 ;;
            --purge) purge=1 ;;
            -y|--yes) ASSUME_YES=1 ;;
        esac
    done

    if ! is_installed && [ ! -d "$INSTALL_DIR" ]; then
        info "VPS Traffic Panel is not installed."
    else
        if [ "$ASSUME_YES" != "1" ]; then
            confirm "Uninstall VPS Traffic Panel? [y/N]:" N || { echo "Cancelled."; return; }
            if [ "$keep_data" -eq 0 ] && confirm "Keep a backup of .env and traffic data in /root? [Y/n]:" Y; then
                keep_data=1
            fi
        fi
        local script="$INSTALL_DIR/scripts/uninstall.sh"
        [ -f "$script" ] || script="$SRC_DIR/scripts/uninstall.sh"
        if [ "$keep_data" -eq 1 ]; then
            bash "$script" --keep-data
        else
            bash "$script"
        fi
    fi

    if [ -L "$SHORTCUT" ]; then
        rm -f "$SHORTCUT"
    fi
    if [ -d "$SRC_DIR/.git" ]; then
        if [ "$purge" -eq 1 ] || { [ "$ASSUME_YES" != "1" ] && confirm "Also delete the downloaded source in $SRC_DIR? [y/N]:" N; }; then
            rm -rf "$SRC_DIR"
            echo "Removed $SRC_DIR"
        fi
    fi
}

cmd_status() {
    if ! is_installed; then
        info "VPS Traffic Panel is not installed."
        return
    fi
    local state
    state=$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)
    echo -e "Version     : v$(version_of "$INSTALL_DIR")"
    echo -e "Service     : $state"
    echo -e "HTTPS mode  : $(env_value HTTPS_MODE)"
    echo -e "Panel URL   : $(panel_url)"
    if systemctl is-enabled --quiet "${SERVICE_NAME}-cert.path" 2>/dev/null; then
        echo -e "Cert watch  : on ($(env_value SSL_CERTFILE))"
    fi
    if [ -d "$SRC_DIR/.git" ] && [ "${VPS_PANEL_SKIP_PULL:-0}" != "1" ]; then
        if git -C "$SRC_DIR" fetch --quiet origin 2>/dev/null; then
            local latest
            latest=$(git -C "$SRC_DIR" show origin/main:app/__init__.py 2>/dev/null | sed -n 's/^__version__ = "\(.*\)"/\1/p')
            if [ -n "$latest" ] && [ "$latest" != "$(version_of "$INSTALL_DIR")" ]; then
                echo -e "${CYAN}Update available: v$latest  (run: vps-panel update)${RESET}"
            fi
        fi
    fi
}

cmd_logs() {
    journalctl -u "$SERVICE_NAME" -n 100 --no-pager
    if [ "$INTERACTIVE" -eq 1 ]; then
        info "Following logs, press Ctrl+C to stop..."
        journalctl -u "$SERVICE_NAME" -f -n 0
    fi
}

cmd_restart() {
    require_root
    systemctl restart "$SERVICE_NAME"
    ok "Restarted."
}

show_menu() {
    echo ""
    echo -e "${GREEN}======== VPS Traffic Panel ========${RESET}"
    if is_installed; then
        echo -e "  Installed v$(version_of "$INSTALL_DIR"), service: $(systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)"
    else
        echo -e "  Not installed"
    fi
    echo "-----------------------------------"
    echo "  1) Install / 安裝"
    echo "  2) Update  / 升級"
    echo "  3) Uninstall / 卸載"
    echo "  4) Status / 狀態"
    echo "  5) Logs / 日誌"
    echo "  6) Restart / 重啟"
    echo "  0) Exit / 退出"
    echo "-----------------------------------"
    local choice
    read -r -p "Choose [0-6]: " choice
    case "$choice" in
        1) cmd_install ;;
        2) cmd_update ;;
        3) cmd_uninstall ;;
        4) cmd_status ;;
        5) cmd_logs ;;
        6) cmd_restart ;;
        *) exit 0 ;;
    esac
}

ASSUME_YES="${VPS_PANEL_YES:-0}"
command="${1:-}"
[ $# -gt 0 ] && shift
case "$command" in
    install) cmd_install "$@" ;;
    update|upgrade) cmd_update "$@" ;;
    uninstall|remove) cmd_uninstall "$@" ;;
    status) cmd_status ;;
    logs|log) cmd_logs ;;
    restart) cmd_restart ;;
    "")
        [ "$INTERACTIVE" -eq 1 ] || fail "No command given. Use: install | update | uninstall | status | logs | restart"
        show_menu
        ;;
    -h|--help|help)
        cat <<'USAGE'
Usage: vps-panel [install | update | uninstall | status | logs | restart]
  No command opens the menu.
  install    download (or refresh) the source and run the installer; extra args go to install.sh
  update     pull the latest version and upgrade in place, keeping data and settings
  uninstall  remove the panel; --keep-data saves .env and data.db to /root, --purge deletes the source, -y skips questions
  status     version, service state, panel URL, update check
  logs       recent logs, then follow
  restart    restart the service
USAGE
        ;;
    *) fail "Unknown command: $command" ;;
esac

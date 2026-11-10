#!/bin/bash
# VPS Traffic Panel installer.
#
# Interactive by default. For unattended installs, set any of:
#   VPS_PANEL_USERNAME      login user (default admin)
#   VPS_PANEL_PASSWORD      login password (required when not interactive)
#   VPS_PANEL_RESET_DAY     monthly reset day (default 1)
#   VPS_PANEL_PORT          backend port (default 8088, next free port if taken)
#   VPS_PANEL_SSL           auto | off  (non-interactive default: off unless a cert is given)
#   VPS_PANEL_CERT / VPS_PANEL_KEY   certificate + private key paths
#   VPS_PANEL_DOMAIN        domain for Nginx HTTPS; set to empty for direct HTTPS on the IP
#   VPS_PANEL_HTTPS_PORT    Nginx HTTPS port (default 443, or 8443 if 443 is taken)
#   VPS_PANEL_YES=1         never prompt, take defaults
set -e

GREEN="\033[32m"
RED="\033[31m"
YELLOW="\033[33m"
RESET="\033[0m"
SERVICE_NAME="vps-traffic-panel"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NGINX_CONFIG_FILE="/etc/nginx/conf.d/${SERVICE_NAME}.conf"
INSTALLER_VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$PROJECT_ROOT/app/__init__.py" 2>/dev/null)"
SERVICE_USER="vps-traffic-panel"
INSTALL_DIR="/opt/vps-traffic-panel"
PREVIOUS_INSTALL_BACKUP=""
DEBUG_SSL=0

INTERACTIVE=1
if [ ! -t 0 ] || [ "${VPS_PANEL_YES:-0}" = "1" ]; then
    INTERACTIVE=0
fi

for arg in "$@"; do
    case "$arg" in
        --debug-ssl) DEBUG_SSL=1 ;;
        -y|--yes) INTERACTIVE=0 ;;
    esac
done

info() { echo -e "${YELLOW}$*${RESET}"; }
ok() { echo -e "${GREEN}$*${RESET}"; }
fail() { echo -e "${RED}Error: $*${RESET}" >&2; exit 1; }

# ask VAR "Prompt" default  -> prompts only when interactive and VAR was not preset
ask() {
    local var="$1" prompt="$2" default="$3" value
    if [ -n "${!var}" ]; then
        return
    fi
    if [ "$INTERACTIVE" -eq 1 ]; then
        read -r -p "$prompt [$default]: " value
        printf -v "$var" '%s' "${value:-$default}"
    else
        printf -v "$var" '%s' "$default"
    fi
}

port_in_use() {
    [ -n "$(ss -Htln "sport = :$1" 2>/dev/null)" ]
}

# Name of the process listening on a TCP port (needs root), empty if free/unknown.
port_owner() {
    ss -Htlnp "sport = :$1" 2>/dev/null | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' | head -n 1
}

# A port nginx can listen on: free, or already held by nginx (server_name keeps sites apart).
port_usable_by_nginx() {
    ! port_in_use "$1" || [ "$(port_owner "$1")" = "nginx" ]
}

cleanup_previous_install() {
    local install_dir="$1"
    local timestamp
    timestamp=$(date +"%Y%m%d_%H%M%S")
    local old_dirs=()

    info "Checking for previous VPS Traffic Panel installation..."

    if [ -f "$SERVICE_FILE" ]; then
        local service_dir
        service_dir=$(awk -F= '/^WorkingDirectory=/{print $2; exit}' "$SERVICE_FILE" || true)
        [ -n "$service_dir" ] && old_dirs+=("$service_dir")

        info "Previous systemd service found. Stopping and disabling it..."
        systemctl stop "$SERVICE_NAME" 2>/dev/null || true
        systemctl disable "$SERVICE_NAME" 2>/dev/null || true
        rm -f "$SERVICE_FILE"
        systemctl daemon-reload
    fi

    old_dirs+=("$install_dir")

    local dir
    local handled=" "
    for dir in "${old_dirs[@]}"; do
        [ -z "$dir" ] && continue
        case "$handled" in
            *" $dir "*) continue ;;
        esac
        handled="$handled$dir "

        if [ -d "$dir" ]; then
            if [ "$(readlink -f "$dir")" = "$(readlink -f "$PROJECT_ROOT")" ]; then
                info "Install directory is the current source directory; keeping source files in place."
                continue
            fi

            local backup_dir="${dir}.old.${timestamp}"
            info "Moving previous install directory to: $backup_dir"
            mv "$dir" "$backup_dir"
            if [ "$dir" = "$install_dir" ] && [ -f "$backup_dir/data.db" ]; then
                PREVIOUS_INSTALL_BACKUP="$backup_dir"
            fi
        fi
    done
}

choose_port() {
    local port="${VPS_PANEL_PORT:-8088}"
    while true; do
        if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
            [ "$INTERACTIVE" -eq 1 ] || fail "Invalid port: $port"
            echo -e "${RED}Error: Port must be a number between 1 and 65535.${RESET}" >&2
        elif ! port_in_use "$port"; then
            echo "$port"
            return
        else
            echo -e "${YELLOW}Port $port is already in use.${RESET}" >&2
            [ -n "$VPS_PANEL_PORT" ] && [ "$INTERACTIVE" -eq 0 ] && fail "VPS_PANEL_PORT=$VPS_PANEL_PORT is in use"
        fi
        local next=$((port + 1))
        if [ "$INTERACTIVE" -eq 1 ]; then
            read -r -p "Web Port [$next]: " port
            port=${port:-$next}
        else
            port=$next
        fi
    done
}

choose_https_port() {
    local port="${VPS_PANEL_HTTPS_PORT:-443}"
    local candidates=(8443 9443 10443 18443)
    while true; do
        if [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ] && port_usable_by_nginx "$port"; then
            echo "$port"
            return
        fi
        local owner
        owner=$(port_owner "$port")
        echo -e "${YELLOW}HTTPS port $port is already used by ${owner:-another program} (for example xray / x-ui).${RESET}" >&2
        if [ -n "$VPS_PANEL_HTTPS_PORT" ] && [ "$INTERACTIVE" -eq 0 ]; then
            fail "VPS_PANEL_HTTPS_PORT=$VPS_PANEL_HTTPS_PORT is in use"
        fi
        local suggestion=""
        local candidate
        for candidate in "${candidates[@]}"; do
            if port_usable_by_nginx "$candidate"; then
                suggestion="$candidate"
                break
            fi
        done
        [ -n "$suggestion" ] || suggestion=$((port + 1))
        if [ "$INTERACTIVE" -eq 1 ]; then
            read -r -p "Panel HTTPS port [$suggestion]: " port
            port=${port:-$suggestion}
        else
            port="$suggestion"
        fi
    done
}

detect_public_ip() {
    local ip=""
    if command -v curl >/dev/null 2>&1; then
        ip=$(curl -fsS --max-time 3 https://api.ipify.org 2>/dev/null || true)
    fi
    if [ -z "$ip" ]; then
        ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
    fi
    echo "${ip:-127.0.0.1}"
}

domain_from_cert() {
    local cert="$1" domain=""
    if command -v openssl >/dev/null 2>&1; then
        domain=$(openssl x509 -in "$cert" -noout -ext subjectAltName 2>/dev/null | sed -n 's/.*DNS:\([^, ]*\).*/\1/p' | head -n 1 || true)
        if [ -z "$domain" ]; then
            domain=$(openssl x509 -in "$cert" -noout -subject 2>/dev/null | sed -n 's/.*CN[ =]*\([^,\/]*\).*/\1/p' | head -n 1 || true)
        fi
    fi
    if [ -z "$domain" ]; then
        domain=$(basename "$(dirname "$cert")")
        case "$domain" in archive|cert|certs|ssl|tls|root) domain="" ;; esac
    fi
    echo "$domain"
}

detect_ssl_certificates() {
    SSL_ENABLED=0
    SSL_CERTFILE=""
    SSL_KEYFILE=""
    HTTPS_MODE="none"
    PANEL_DOMAIN=""

    if [ -n "$VPS_PANEL_CERT" ] || [ -n "$VPS_PANEL_KEY" ]; then
        [ -f "$VPS_PANEL_CERT" ] || fail "Certificate not found: $VPS_PANEL_CERT"
        [ -f "$VPS_PANEL_KEY" ] || fail "Private key not found: $VPS_PANEL_KEY"
        SSL_CERTFILE="$VPS_PANEL_CERT"
        SSL_KEYFILE="$VPS_PANEL_KEY"
        select_https_mode
        return
    fi
    if [ "${VPS_PANEL_SSL:-}" = "off" ] || { [ "$INTERACTIVE" -eq 0 ] && [ "${VPS_PANEL_SSL:-}" != "auto" ] && [ "$DEBUG_SSL" -eq 0 ]; }; then
        info "SSL disabled for this install. The panel will run over HTTP."
        return
    fi

    local candidates=()
    add_cert_candidate() {
        local cert_file="$1"
        local key_file="$2"
        local label="$3"
        if [ -n "$cert_file" ] && [ -n "$key_file" ] && [ -f "$cert_file" ] && [ -f "$key_file" ]; then
            candidates+=("$cert_file|$key_file|$label")
            if [ "$DEBUG_SSL" -eq 1 ]; then
                echo "[ssl] candidate: $label | cert=$cert_file | key=$key_file"
            fi
        fi
    }

    if [ -d /etc/letsencrypt/live ]; then
        while IFS= read -r cert_dir; do
            add_cert_candidate "$cert_dir/fullchain.pem" "$cert_dir/privkey.pem" "Let's Encrypt: $(basename "$cert_dir")"
        done < <(find /etc/letsencrypt/live -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort || true)
    fi

    if [ -d /etc/nginx ]; then
        while IFS= read -r nginx_file; do
            local cert_file
            local key_file
            cert_file=$(awk '/ssl_certificate[[:space:]]+/ && $1 == "ssl_certificate" {gsub(/;/, "", $2); print $2; exit}' "$nginx_file" 2>/dev/null || true)
            key_file=$(awk '/ssl_certificate_key[[:space:]]+/ {gsub(/;/, "", $2); print $2; exit}' "$nginx_file" 2>/dev/null || true)
            add_cert_candidate "$cert_file" "$key_file" "Nginx config: $nginx_file"
        done < <(find /etc/nginx \( -type f -o -type l \) \( -name "*.conf" -o -path "*/sites-enabled/*" -o -path "*/sites-available/*" \) 2>/dev/null | sort || true)
    fi

    if command -v sqlite3 >/dev/null 2>&1; then
        while IFS= read -r db_file; do
            local cert_file=""
            local key_file=""
            while IFS='=' read -r setting_key setting_value; do
                case "$setting_key" in
                    *[Cc]ert*) cert_file="$setting_value" ;;
                    *Key*|*key*) key_file="$setting_value" ;;
                esac
            done < <(sqlite3 "$db_file" "SELECT key || '=' || value FROM settings;" 2>/dev/null || true)
            add_cert_candidate "$cert_file" "$key_file" "x-ui database: $db_file"
        done < <(find /etc/x-ui /usr/local/x-ui /usr/local/etc/x-ui -maxdepth 3 -type f \( -name "*.db" -o -name "*.sqlite" \) 2>/dev/null | sort || true)
    fi

    while IFS= read -r config_dir; do
        local cert_file=""
        local key_file=""
        while IFS= read -r path_value; do
            case "$path_value" in
                *key*|*priv*) key_file="$path_value" ;;
                *) cert_file="$path_value" ;;
            esac
        done < <(grep -RohE '/[^"'\'' ;]+[.](pem|crt|cer|key)' "$config_dir" 2>/dev/null | sort -u || true)
        add_cert_candidate "$cert_file" "$key_file" "Config path scan: $config_dir"
    done < <(find /etc/x-ui /usr/local/x-ui /usr/local/etc/x-ui -maxdepth 2 -type d 2>/dev/null | sort || true)

    local scan_roots=(
        "/etc/x-ui"
        "/usr/local/x-ui"
        "/usr/local/etc/x-ui"
        "/root"
        "/root/cert"
        "/root/.acme.sh"
        "/opt"
        "/usr/local/etc"
        "/var/lib"
        "/etc/ssl"
        "/etc/v2ray-agent/tls"
        "/etc/hysteria"
        "/etc/sing-box"
        "/etc/trojan"
    )
    local root
    for root in "${scan_roots[@]}"; do
        [ -d "$root" ] || continue
        while IFS= read -r cert_file; do
            local cert_dir
            local key_file
            local cert_name
            cert_dir=$(dirname "$cert_file")
            cert_name=$(basename "$cert_file")
            case "$cert_name" in
                *key*|*priv*) continue ;;
            esac
            for key_file in \
                "$cert_dir/privkey.pem" \
                "$cert_dir/private.key" \
                "$cert_dir/private.pem" \
                "$cert_dir/key.pem" \
                "$cert_dir/server.key" \
                "$cert_dir/cert.key" \
                "$cert_dir/ssl.key" \
                "$cert_dir/$(basename "$cert_dir").key" \
                "$cert_dir"/*.key \
                "$cert_dir"/*key*.pem; do
                if [ -f "$key_file" ]; then
                    add_cert_candidate "$cert_file" "$key_file" "Auto scan: $cert_dir"
                    break
                fi
            done
        done < <(find "$root" -maxdepth 5 -type f \( -name "fullchain.pem" -o -name "cert.pem" -o -name "certificate.pem" -o -name "server.crt" -o -name "server.pem" -o -name "*.crt" -o -name "*.cer" -o -name "*.pem" \) 2>/dev/null | sort || true)
    done

    local unique_candidates=()
    local seen=" "
    local candidate
    for candidate in "${candidates[@]}"; do
        local cert_path="${candidate%%|*}"
        local rest="${candidate#*|}"
        local key_path="${rest%%|*}"
        local key="${cert_path}|${key_path}"
        case "$seen" in
            *" $key "*) continue ;;
        esac
        seen="$seen$key "
        unique_candidates+=("$candidate")
    done

    if [ "${#unique_candidates[@]}" -eq 0 ]; then
        info "No SSL certificate was detected automatically."
        if [ "$DEBUG_SSL" -eq 1 ]; then
            info "Raw certificate/key-like files found:"
            find /etc /usr/local /root /opt -maxdepth 6 -type f \( -name "*.pem" -o -name "*.crt" -o -name "*.cer" -o -name "*.key" \) 2>/dev/null | sort || true
            exit 0
        fi
        if [ "$INTERACTIVE" -eq 0 ]; then
            info "The panel will run over HTTP."
            return
        fi
        read -r -p "Enter certificate paths manually? [y/N]: " manual_ssl
        if [[ ! "$manual_ssl" =~ ^[Yy]$ ]]; then
            info "The panel will run over HTTP."
            return
        fi

        while true; do
            read -r -p "SSL certificate fullchain path: " SSL_CERTFILE
            read -r -p "SSL private key path: " SSL_KEYFILE
            if [ -f "$SSL_CERTFILE" ] && [ -f "$SSL_KEYFILE" ]; then
                unique_candidates+=("$SSL_CERTFILE|$SSL_KEYFILE|Manual input")
                break
            fi
            echo -e "${RED}Error: Certificate or key file not found. Please try again.${RESET}"
        done
    fi

    ok "Detected SSL certificate(s):"
    local i
    for i in "${!unique_candidates[@]}"; do
        local item="${unique_candidates[$i]}"
        local cert_path="${item%%|*}"
        local rest="${item#*|}"
        local key_path="${rest%%|*}"
        local label="${rest#*|}"
        echo "  $((i + 1))) $label"
        echo "      cert: $cert_path"
        echo "      key : $key_path"
    done

    if [ "$DEBUG_SSL" -eq 1 ]; then
        ok "SSL debug completed. No installation changes were made."
        exit 0
    fi

    local choice=1
    if [ "${#unique_candidates[@]}" -eq 1 ]; then
        if [ "$INTERACTIVE" -eq 1 ]; then
            local use_it
            read -r -p "Use this certificate for HTTPS? [Y/n]: " use_it
            if [[ "$use_it" =~ ^[Nn]$ ]]; then
                info "The panel will run over HTTP."
                return
            fi
        else
            ok "Using the only detected certificate automatically."
        fi
    elif [ "$INTERACTIVE" -eq 1 ]; then
        echo "  0) Do not use HTTPS"
        while true; do
            read -r -p "Select certificate number [1]: " choice
            choice=${choice:-1}
            if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 0 ] && [ "$choice" -le "${#unique_candidates[@]}" ]; then
                break
            fi
            echo -e "${RED}Error: Please choose a valid certificate number.${RESET}"
        done
        if [ "$choice" -eq 0 ]; then
            info "The panel will run over HTTP."
            return
        fi
    fi

    local selected="${unique_candidates[$((choice - 1))]}"
    local rest="${selected#*|}"
    SSL_CERTFILE="${selected%%|*}"
    SSL_KEYFILE="${rest%%|*}"
    select_https_mode
}

# With SSL_CERTFILE / SSL_KEYFILE chosen: pick Nginx (domain) or direct uvicorn HTTPS (no domain).
select_https_mode() {
    SSL_ENABLED=1
    local default_domain
    default_domain=$(domain_from_cert "$SSL_CERTFILE")

    if [ -n "${VPS_PANEL_DOMAIN+x}" ]; then
        PANEL_DOMAIN="$VPS_PANEL_DOMAIN"
    elif [ "$INTERACTIVE" -eq 1 ]; then
        echo "The certificate is issued for: ${default_domain:-unknown}"
        echo "Enter the domain you will open the panel with (leave empty to use https://IP:port directly)."
        read -r -p "Panel domain for HTTPS [$default_domain]: " PANEL_DOMAIN
        PANEL_DOMAIN=${PANEL_DOMAIN:-$default_domain}
    else
        PANEL_DOMAIN="$default_domain"
    fi

    if [ -n "$PANEL_DOMAIN" ]; then
        HTTPS_MODE="nginx"
        ok "HTTPS will be served by Nginx for $PANEL_DOMAIN."
    else
        HTTPS_MODE="uvicorn"
        info "No panel domain. HTTPS will be served directly by the panel (browsers will warn when opened by IP)."
    fi
}

# Install nginx without letting the package start it on a port another program already owns.
install_nginx_safely() {
    if command -v nginx >/dev/null 2>&1; then
        return
    fi
    local blocked_autostart=0
    if port_in_use 80 && [ ! -e /usr/sbin/policy-rc.d ]; then
        printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
        chmod +x /usr/sbin/policy-rc.d
        blocked_autostart=1
    fi
    if ! apt-get install -y nginx; then
        [ "$blocked_autostart" -eq 1 ] && rm -f /usr/sbin/policy-rc.d
        fail "Failed to install nginx"
    fi
    [ "$blocked_autostart" -eq 1 ] && rm -f /usr/sbin/policy-rc.d
    # The stock "default" site listens on :80; drop it when something else already owns :80.
    if ! port_usable_by_nginx 80; then
        info "Port 80 is used by $(port_owner 80); disabling the stock Nginx default site."
        rm -f /etc/nginx/sites-enabled/default
        [ -f /etc/nginx/conf.d/default.conf ] && mv /etc/nginx/conf.d/default.conf /etc/nginx/conf.d/default.conf.disabled
    fi
    return 0
}

fall_back_to_http() {
    echo -e "${RED}$1${RESET}"
    echo -e "${RED}Removing the generated Nginx config; the panel will be reachable over plain HTTP instead.${RESET}"
    rm -f "$NGINX_CONFIG_FILE"
    if command -v nginx >/dev/null 2>&1 && nginx -t >/dev/null 2>&1 && systemctl is-active --quiet nginx; then
        systemctl reload nginx || true
    fi
    HTTPS_MODE="none"
    SSL_ENABLED=0
    HOST="0.0.0.0"
    sed -i -e "s/^HOST=.*/HOST=$HOST/" -e "s/^HTTPS_MODE=.*/HTTPS_MODE=none/" -e "s/^SSL_ENABLED=.*/SSL_ENABLED=0/" "$INSTALL_DIR/.env"
    systemctl restart "$SERVICE_NAME"
}

configure_nginx_proxy() {
    if [ "$HTTPS_MODE" != "nginx" ]; then
        return
    fi

    info "Configuring Nginx HTTPS reverse proxy on port $HTTPS_PORT..."
    mkdir -p "$(dirname "$NGINX_CONFIG_FILE")"
    if [ -f "$NGINX_CONFIG_FILE" ]; then
        cp "$NGINX_CONFIG_FILE" "${NGINX_CONFIG_FILE}.$(date +"%Y%m%d_%H%M%S").bak"
    fi

    local redirect_target="https://\$host\$request_uri"
    [ "$HTTPS_PORT" != "443" ] && redirect_target="https://\$host:$HTTPS_PORT\$request_uri"

    {
        if port_usable_by_nginx 80; then
            cat <<EOF
server {
    listen 80;
    server_name $PANEL_DOMAIN;
    return 301 $redirect_target;
}

EOF
        else
            echo "# Port 80 is used by another program; no HTTP->HTTPS redirect."
            echo ""
        fi
        cat <<EOF
server {
    listen $HTTPS_PORT ssl http2;
    server_name $PANEL_DOMAIN;

    ssl_certificate $SSL_CERTFILE;
    ssl_certificate_key $SSL_KEYFILE;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }
}
EOF
    } > "$NGINX_CONFIG_FILE"

    if ! nginx -t; then
        fall_back_to_http "Nginx config test failed."
        return
    fi
    if systemctl is-active --quiet nginx; then
        systemctl reload nginx || { fall_back_to_http "Nginx reload failed."; return; }
    else
        systemctl enable nginx >/dev/null 2>&1 || true
        if ! systemctl start nginx; then
            journalctl -u nginx -n 15 --no-pager || true
            fall_back_to_http "Nginx failed to start (often a port conflict with another program)."
            return
        fi
    fi

    # Self-check: an unauthenticated request through Nginx should reach the panel and get 401.
    local code="" _
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        code=$(curl -sk -o /dev/null -w "%{http_code}" --resolve "$PANEL_DOMAIN:$HTTPS_PORT:127.0.0.1" "https://$PANEL_DOMAIN:$HTTPS_PORT/api/ping" || true)
        [ "$code" = "401" ] && break
        sleep 1
    done
    if [ "$code" = "401" ]; then
        ok "HTTPS self-check passed."
    else
        info "HTTPS self-check returned '$code'. Check: journalctl -u $SERVICE_NAME -n 50 and nginx -t"
    fi
}

open_firewall_port() {
    local port="$1"
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
        info "ufw is active; allowing TCP port $port."
        ufw allow "$port/tcp" >/dev/null || true
    fi
}

# ---------------------------------------------------------------------------

if [ "$EUID" -ne 0 ]; then
    fail "Please run as root."
fi

ok "=== VPS Traffic Panel Installer v$INSTALLER_VERSION ==="

HOST="0.0.0.0"
HTTPS_PORT=""
PUBLIC_IP=$(detect_public_ip)

if [ "$DEBUG_SSL" -eq 1 ]; then
    detect_ssl_certificates
    exit 0
fi

if ! command -v ss >/dev/null 2>&1; then
    info "Installing basic network tools for port detection..."
    apt-get update -qq && apt-get install -y iproute2 -qq >/dev/null
fi

cleanup_previous_install "$INSTALL_DIR"

PORT=$(choose_port)
INTERFACE=""

AUTH_USERNAME="${VPS_PANEL_USERNAME:-}"
while true; do
    ask AUTH_USERNAME "Login Username" "admin"
    if [[ "$AUTH_USERNAME" =~ ^[a-zA-Z0-9@_\.\!\%\-]+$ ]]; then
        break
    fi
    [ "$INTERACTIVE" -eq 1 ] || fail "Username may only contain letters, numbers and @ _ - . ! %"
    echo -e "${RED}Error: Username may only contain letters, numbers and @ _ - . ! %${RESET}"
    AUTH_USERNAME=""
done

AUTH_PASSWORD="${VPS_PANEL_PASSWORD:-}"
while true; do
    if [ -z "$AUTH_PASSWORD" ]; then
        [ "$INTERACTIVE" -eq 1 ] || fail "Set VPS_PANEL_PASSWORD for a non-interactive install."
        read -r -s -p "Login Password: " AUTH_PASSWORD
        echo ""
        if [ -z "$AUTH_PASSWORD" ]; then
            echo -e "${RED}Error: Password cannot be empty.${RESET}"
            continue
        fi
    fi
    if [[ ! "$AUTH_PASSWORD" =~ ^[a-zA-Z0-9@_\.\!\%\-]+$ ]]; then
        [ "$INTERACTIVE" -eq 1 ] || fail "Password may only contain letters, numbers and @ _ - . ! %"
        echo -e "${RED}Error: Password may only contain letters, numbers and @ _ - . ! %${RESET}"
        AUTH_PASSWORD=""
        continue
    fi
    break
done

MONTH_RESET_DAY="${VPS_PANEL_RESET_DAY:-}"
while true; do
    ask MONTH_RESET_DAY "Monthly traffic reset day" "1"
    if [[ "$MONTH_RESET_DAY" =~ ^[0-9]+$ ]] && [ "$MONTH_RESET_DAY" -ge 1 ] && [ "$MONTH_RESET_DAY" -le 31 ]; then
        break
    fi
    [ "$INTERACTIVE" -eq 1 ] || fail "Reset day must be between 1 and 31."
    echo -e "${RED}Error: Reset day must be between 1 and 31.${RESET}"
    MONTH_RESET_DAY=""
done

if ! command -v sqlite3 >/dev/null 2>&1 || ! command -v openssl >/dev/null 2>&1; then
    info "Installing SSL detection tools..."
    apt-get update
    apt-get install -y sqlite3 openssl
fi

detect_ssl_certificates

if [ "$HTTPS_MODE" = "nginx" ]; then
    # Behind the Nginx HTTPS proxy, keep the plain-HTTP backend off the public interface.
    HOST="127.0.0.1"
    HTTPS_PORT=$(choose_https_port)
fi

ok "===================================="
ok "Install Summary"
echo -e "Install directory    : $INSTALL_DIR"
echo -e "Public IP            : $PUBLIC_IP"
echo -e "Bind address         : $HOST"
echo -e "Web port             : $PORT"
echo -e "Monthly reset day    : $MONTH_RESET_DAY"
echo -e "HTTPS mode           : $HTTPS_MODE"
[ -n "$PREVIOUS_INSTALL_BACKUP" ] && echo -e "Restore history from : $PREVIOUS_INSTALL_BACKUP/data.db"
if [ "$SSL_ENABLED" -eq 1 ]; then
    echo -e "SSL certificate      : $SSL_CERTFILE"
    echo -e "SSL private key      : $SSL_KEYFILE"
fi
if [ "$HTTPS_MODE" = "nginx" ]; then
    echo -e "Panel domain         : $PANEL_DOMAIN"
    echo -e "HTTPS port           : $HTTPS_PORT"
fi
ok "===================================="

info "Installing dependencies..."
apt-get update
apt-get install -y python3 python3-venv python3-pip python3-dev gcc sqlite3 curl rsync git iproute2 openssl
if [ "$HTTPS_MODE" = "nginx" ]; then
    install_nginx_safely
fi

info "Copying project files..."
mkdir -p "$INSTALL_DIR"
rsync -a --exclude={'.git','.github','tests','venv','__pycache__','*.db','*.db-shm','*.db-wal','.env','.env.example','backups','*.bak'} "$PROJECT_ROOT/" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR"/scripts/*.sh

cd "$INSTALL_DIR"

info "Setting up Python virtual environment..."
python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

info "Generating .env file..."
cat > .env <<EOF
HOST=$HOST
PORT=$PORT
AUTH_USERNAME=$AUTH_USERNAME
AUTH_PASSWORD=$AUTH_PASSWORD
PANEL_TITLE="VPS 監控流量面板"
PANEL_SUBTITLE="Tim哥在三更半夜改好的"
TELEGRAM_ENABLED=0
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_PUSH_HOUR=20
TELEGRAM_PUSH_MINUTE=0
TELEGRAM_TIMEZONE_OFFSET=8
TELEGRAM_TIMEZONE_LABEL=中國時間
BANDWAGON_VEID=
BANDWAGON_API_KEY=
TRAFFIC_QUOTA_BYTES=0
CLOUDFLARE_DDNS_ENABLED=0
CLOUDFLARE_API_TOKEN=
CLOUDFLARE_ZONE_ID=
CLOUDFLARE_RECORD_NAME=
CLOUDFLARE_PROXIED=0
INTERFACE=$INTERFACE
MONTH_RESET_DAY=$MONTH_RESET_DAY
SSL_ENABLED=$SSL_ENABLED
SSL_CERTFILE=$SSL_CERTFILE
SSL_KEYFILE=$SSL_KEYFILE
HTTPS_MODE=$HTTPS_MODE
HTTPS_PORT=$HTTPS_PORT
PANEL_DOMAIN=$PANEL_DOMAIN
EOF
chmod 600 .env

if [ -n "$PREVIOUS_INSTALL_BACKUP" ]; then
    info "Restoring traffic history from $PREVIOUS_INSTALL_BACKUP/data.db ..."
    sqlite3 "$PREVIOUS_INSTALL_BACKUP/data.db" ".backup '$INSTALL_DIR/data.db'" || cp "$PREVIOUS_INSTALL_BACKUP/data.db" "$INSTALL_DIR/data.db"
fi

# Direct uvicorn HTTPS needs to read private keys that are usually root-only; otherwise run unprivileged.
if [ "$HTTPS_MODE" = "uvicorn" ]; then
    RUN_USER="root"
else
    RUN_USER="$SERVICE_USER"
    if ! id "$SERVICE_USER" >/dev/null 2>&1; then
        useradd --system --no-create-home --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
fi

info "Configuring systemd service..."
if [ "$HTTPS_MODE" = "uvicorn" ]; then
    EXEC_START="$INSTALL_DIR/venv/bin/uvicorn app.main:app --host \$HOST --port \$PORT --ssl-certfile \$SSL_CERTFILE --ssl-keyfile \$SSL_KEYFILE"
else
    EXEC_START="$INSTALL_DIR/venv/bin/uvicorn app.main:app --host \$HOST --port \$PORT"
fi

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=VPS Traffic Panel
After=network.target

[Service]
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$EXEC_START
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

configure_nginx_proxy

# Reload Nginx / restart the panel automatically whenever the certificate is renewed.
bash "$INSTALL_DIR/scripts/cert-watch.sh" sync

ACCESS_URL="http://$PUBLIC_IP:$PORT"
if [ "$HTTPS_MODE" = "nginx" ]; then
    ACCESS_URL="https://$PANEL_DOMAIN"
    [ "$HTTPS_PORT" != "443" ] && ACCESS_URL="$ACCESS_URL:$HTTPS_PORT"
    open_firewall_port "$HTTPS_PORT"
else
    [ "$HTTPS_MODE" = "uvicorn" ] && ACCESS_URL="https://$PUBLIC_IP:$PORT"
    open_firewall_port "$PORT"
fi

bash "$INSTALL_DIR/scripts/legacy-check.sh"

ok "===================================="
ok "Installation Successful!"
echo -e "Access your panel at : $ACCESS_URL"
echo -e "Panel version        : v$INSTALLER_VERSION"
echo -e "Service user         : $RUN_USER"
echo -e "Manage               : vps-panel   (menu: update / uninstall / status / logs)"
echo -e "Service status       : systemctl status $SERVICE_NAME"
echo -e "View running logs    : journalctl -u $SERVICE_NAME -f"
if [ "$HTTPS_MODE" = "nginx" ] && [ "$HTTPS_PORT" != "443" ]; then
    echo -e "${YELLOW}Note: port 443 is used by another program, so the panel uses port $HTTPS_PORT.${RESET}"
    echo -e "${YELLOW}      Allow TCP $HTTPS_PORT in your cloud provider's firewall / security group if needed.${RESET}"
fi
ok "===================================="

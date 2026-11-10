#!/bin/bash
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
PREVIOUS_INSTALL_BACKUP=""
DEBUG_SSL=0

for arg in "$@"; do
    case "$arg" in
        --debug-ssl) DEBUG_SSL=1 ;;
    esac
done

cleanup_previous_install() {
    local install_dir="$1"
    local timestamp
    timestamp=$(date +"%Y%m%d_%H%M%S")
    local old_dirs=()

    echo -e "${YELLOW}Checking for previous VPS Traffic Panel installation...${RESET}"

    if [ -f "$SERVICE_FILE" ]; then
        local service_dir
        service_dir=$(awk -F= '/^WorkingDirectory=/{print $2; exit}' "$SERVICE_FILE" || true)
        [ -n "$service_dir" ] && old_dirs+=("$service_dir")

        echo -e "${YELLOW}Previous systemd service found. Stopping and disabling it...${RESET}"
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
                echo -e "${YELLOW}Install directory is the current source directory; keeping source files in place.${RESET}"
                continue
            fi

            local backup_dir="${dir}.old.${timestamp}"
            echo -e "${YELLOW}Moving previous install directory to: $backup_dir${RESET}"
            mv "$dir" "$backup_dir"
            if [ "$dir" = "$install_dir" ] && [ -f "$backup_dir/data.db" ]; then
                PREVIOUS_INSTALL_BACKUP="$backup_dir"
            fi
        fi
    done
}

choose_port() {
    local port=8088
    if ! ss -tuln | awk '{print $5}' | grep -qE ":$port$"; then
        echo "$port"
        return
    fi

    echo -e "${YELLOW}Port 8088 is already in use.${RESET}" >&2
    while true; do
        read -p "Web Port [8089]: " port
        port=${port:-8089}

        if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
            echo -e "${RED}Error: Port must be a number between 1 and 65535.${RESET}" >&2
            continue
        fi

        if ss -tuln | awk '{print $5}' | grep -qE ":$port$"; then
            echo -e "${RED}Error: Port $port is already in use. Please choose another port.${RESET}" >&2
        else
            echo "$port"
            return
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

detect_ssl_certificates() {
    SSL_ENABLED=0
    SSL_CERTFILE=""
    SSL_KEYFILE=""
    HTTPS_MODE="none"
    PANEL_DOMAIN=""

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
                    *Cert*|*cert*|*Certificate*|*certificate*) cert_file="$setting_value" ;;
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
        echo -e "${YELLOW}No SSL certificate was detected automatically.${RESET}"
        if [ "$DEBUG_SSL" -eq 1 ]; then
            echo -e "${YELLOW}Raw certificate/key-like files found:${RESET}"
            find /etc /usr/local /root /opt -maxdepth 6 -type f \( -name "*.pem" -o -name "*.crt" -o -name "*.cer" -o -name "*.key" \) 2>/dev/null | sort || true
            exit 0
        fi
        read -p "Enter certificate paths manually? [y/N]: " manual_ssl
        if [[ ! "$manual_ssl" =~ ^[Yy]$ ]]; then
            echo -e "${YELLOW}The panel will run over HTTP.${RESET}"
            return
        fi

        while true; do
            read -p "SSL certificate fullchain path: " SSL_CERTFILE
            read -p "SSL private key path: " SSL_KEYFILE
            if [ -f "$SSL_CERTFILE" ] && [ -f "$SSL_KEYFILE" ]; then
                unique_candidates+=("$SSL_CERTFILE|$SSL_KEYFILE|Manual input")
                break
            fi
            echo -e "${RED}Error: Certificate or key file not found. Please try again.${RESET}"
        done
    fi

    echo -e "${GREEN}Detected SSL certificate(s):${RESET}"
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
        echo -e "${GREEN}SSL debug completed. No installation changes were made.${RESET}"
        exit 0
    fi

    local choice
    if [ "${#unique_candidates[@]}" -eq 1 ]; then
        choice=1
        echo -e "${GREEN}Using the only detected certificate automatically.${RESET}"
    else
        while true; do
            read -p "Select certificate number [1]: " choice
            choice=${choice:-1}
            if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#unique_candidates[@]}" ]; then
                break
            fi
            echo -e "${RED}Error: Please choose a valid certificate number.${RESET}"
        done
    fi

    local selected="${unique_candidates[$((choice - 1))]}"
    local rest="${selected#*|}"
    SSL_ENABLED=1
    SSL_CERTFILE="${selected%%|*}"
    SSL_KEYFILE="${rest%%|*}"

    local default_domain
    default_domain=""
    if command -v openssl >/dev/null 2>&1; then
        default_domain=$(openssl x509 -in "$SSL_CERTFILE" -noout -ext subjectAltName 2>/dev/null | sed -n 's/.*DNS:\([^, ]*\).*/\1/p' | head -n 1 || true)
        if [ -z "$default_domain" ]; then
            default_domain=$(openssl x509 -in "$SSL_CERTFILE" -noout -subject 2>/dev/null | sed -n 's/.*CN[ =]*\([^,\/]*\).*/\1/p' | head -n 1 || true)
        fi
    fi
    if [ -z "$default_domain" ]; then
        default_domain=$(basename "$(dirname "$SSL_CERTFILE")")
        [ "$default_domain" = "archive" ] && default_domain=""
        [ "$default_domain" = "cert" ] && default_domain=""
    fi

    read -p "Panel domain for HTTPS [$default_domain]: " PANEL_DOMAIN
    PANEL_DOMAIN=${PANEL_DOMAIN:-$default_domain}

    if [ -n "$PANEL_DOMAIN" ]; then
        HTTPS_MODE="nginx"
        echo -e "${GREEN}HTTPS will be configured through Nginx reverse proxy.${RESET}"
        return
    fi

    HTTPS_MODE="uvicorn"
    echo -e "${YELLOW}No panel domain was provided. HTTPS will use direct service binding.${RESET}"
}

configure_nginx_proxy() {
    if [ "$HTTPS_MODE" != "nginx" ]; then
        return
    fi

    echo -e "${YELLOW}Configuring Nginx HTTPS reverse proxy...${RESET}"
    mkdir -p "$(dirname "$NGINX_CONFIG_FILE")"

    if [ -f "$NGINX_CONFIG_FILE" ]; then
        cp "$NGINX_CONFIG_FILE" "${NGINX_CONFIG_FILE}.$(date +"%Y%m%d_%H%M%S").bak"
    fi

    cat > "$NGINX_CONFIG_FILE" <<EOF
server {
    listen 80;
    server_name $PANEL_DOMAIN;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl http2;
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

    if nginx -t; then
        systemctl reload nginx
    else
        echo -e "${RED}Nginx config test failed. Removing generated config and keeping the panel service running over HTTP.${RESET}"
        rm -f "$NGINX_CONFIG_FILE"
        HTTPS_MODE="none"
        SSL_ENABLED=0
        # The service was bound to localhost for Nginx; expose it directly again so the panel stays reachable.
        HOST="0.0.0.0"
        sed -i -e "s/^HOST=.*/HOST=$HOST/" -e "s/^HTTPS_MODE=.*/HTTPS_MODE=none/" -e "s/^SSL_ENABLED=.*/SSL_ENABLED=0/" "$INSTALL_DIR/.env"
        systemctl restart "$SERVICE_NAME"
    fi
}

if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}Error: Please run as root.${RESET}"
  exit 1
fi

echo -e "${GREEN}=== VPS Traffic Panel Installer ===${RESET}"
echo -e "${GREEN}Installer version: $INSTALLER_VERSION${RESET}"

INSTALL_DIR="/opt/vps-traffic-panel"
HOST="0.0.0.0"
PUBLIC_IP=$(detect_public_ip)

if [ "$DEBUG_SSL" -eq 1 ]; then
    detect_ssl_certificates
    exit 0
fi

if ! command -v ss >/dev/null 2>&1; then
    echo -e "${YELLOW}Installing basic network tools for port detection...${RESET}"
    apt-get update -qq && apt-get install -y iproute2 -qq >/dev/null
fi

cleanup_previous_install "$INSTALL_DIR"

PORT=$(choose_port)
INTERFACE=""

while true; do
    read -p "Login Username [admin]: " AUTH_USERNAME
    AUTH_USERNAME=${AUTH_USERNAME:-admin}
    if [[ "$AUTH_USERNAME" =~ ^[a-zA-Z0-9@_\.\!\%\-]+$ ]]; then
        break
    fi
    echo -e "${RED}Error: Username may only contain letters, numbers and @ _ - . ! %${RESET}"
done

while true; do
    read -s -p "Login Password: " AUTH_PASSWORD
    echo ""
    
    if [[ ${#AUTH_PASSWORD} -lt 8 || "$AUTH_PASSWORD" == "password" || "$AUTH_PASSWORD" == "12345678" ]]; then
        echo -e "${RED}Error: Password must be at least 8 characters and not a common default. Please enter a secure password.${RESET}"
        continue
    fi
    
    if [[ ! "$AUTH_PASSWORD" =~ ^[a-zA-Z0-9@_\.\!\%\-]+$ ]]; then
        echo -e "${RED}Error: Password contains invalid characters. Only alphanumeric and @ _ - . ! % are allowed.${RESET}"
        continue
    fi
    
    break
done

while true; do
    read -p "Monthly traffic reset day [1]: " MONTH_RESET_DAY
    MONTH_RESET_DAY=${MONTH_RESET_DAY:-1}
    if [[ "$MONTH_RESET_DAY" =~ ^[0-9]+$ ]] && [ "$MONTH_RESET_DAY" -ge 1 ] && [ "$MONTH_RESET_DAY" -le 31 ]; then
        break
    fi
    echo -e "${RED}Error: Reset day must be between 1 and 31.${RESET}"
done

if ! command -v sqlite3 >/dev/null 2>&1 || ! command -v openssl >/dev/null 2>&1; then
    echo -e "${YELLOW}Installing SSL detection tools...${RESET}"
    apt-get update
    apt-get install -y sqlite3 openssl
fi

detect_ssl_certificates

# Behind the Nginx HTTPS proxy, keep the plain-HTTP backend off the public interface.
if [ "$HTTPS_MODE" = "nginx" ]; then
    HOST="127.0.0.1"
fi

echo -e "${GREEN}====================================${RESET}"
echo -e "${GREEN}Install Summary${RESET}"
echo -e "Install directory    : $INSTALL_DIR"
echo -e "Public IP            : $PUBLIC_IP"
echo -e "Bind address         : $HOST"
echo -e "Web port             : $PORT"
echo -e "Network interface    : auto-detect"
echo -e "Monthly reset day    : $MONTH_RESET_DAY"
echo -e "HTTPS mode           : $HTTPS_MODE"
[ -n "$PREVIOUS_INSTALL_BACKUP" ] && echo -e "Restore history from : $PREVIOUS_INSTALL_BACKUP/data.db"
if [ "$SSL_ENABLED" -eq 1 ]; then
    echo -e "SSL certificate      : $SSL_CERTFILE"
    echo -e "SSL private key      : $SSL_KEYFILE"
fi
if [ "$HTTPS_MODE" = "nginx" ]; then
    echo -e "Panel domain         : $PANEL_DOMAIN"
    echo -e "Nginx config         : $NGINX_CONFIG_FILE"
fi
echo -e "${GREEN}====================================${RESET}"

echo -e "${YELLOW}Installing dependencies...${RESET}"
apt-get update
PACKAGES=(python3 python3-venv python3-pip python3-dev gcc sqlite3 curl rsync git iproute2)
if [ "$HTTPS_MODE" = "nginx" ]; then
    PACKAGES+=(nginx openssl)
fi
apt-get install -y "${PACKAGES[@]}"

echo -e "${YELLOW}Copying project files...${RESET}"
mkdir -p "$INSTALL_DIR"

rsync -a --exclude={'.git','.github','tests','venv','__pycache__','*.db','*.db-shm','*.db-wal','.env','.env.example','backups','*.bak'} "$PROJECT_ROOT/" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR"/scripts/*.sh

cd "$INSTALL_DIR"

echo -e "${YELLOW}Setting up Python Virtual Environment...${RESET}"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo -e "${YELLOW}Generating .env file...${RESET}"
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
PANEL_DOMAIN=$PANEL_DOMAIN
EOF
chmod 600 .env

if [ -n "$PREVIOUS_INSTALL_BACKUP" ]; then
    echo -e "${YELLOW}Restoring traffic history from $PREVIOUS_INSTALL_BACKUP/data.db ...${RESET}"
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

echo -e "${YELLOW}Configuring systemd service...${RESET}"
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

SCHEME="http"
ACCESS_HOST="$PUBLIC_IP"
if [ "$HTTPS_MODE" = "nginx" ]; then
    SCHEME="https"
    ACCESS_HOST="$PANEL_DOMAIN"
elif [ "$HTTPS_MODE" = "uvicorn" ]; then
    SCHEME="https"
fi

echo -e "${GREEN}====================================${RESET}"
echo -e "${GREEN}Installation Successful!${RESET}"
if [ "$HTTPS_MODE" = "nginx" ]; then
    echo -e "Access your panel at : $SCHEME://$ACCESS_HOST"
else
    echo -e "Access your panel at : $SCHEME://$ACCESS_HOST:$PORT"
fi
echo -e "Check service status : systemctl status $SERVICE_NAME"
echo -e "View running logs    : journalctl -u $SERVICE_NAME -f"
echo -e "Install directory    : $INSTALL_DIR"
echo -e "Panel version        : v$INSTALLER_VERSION"
echo -e "Service user         : $RUN_USER"
echo -e "Bind address         : $HOST"
echo -e "Public IP            : $PUBLIC_IP"
echo -e "Web port             : $PORT"
echo -e "Network interface    : auto-detect"
echo -e "Monthly reset day    : $MONTH_RESET_DAY"
echo -e "${GREEN}====================================${RESET}"

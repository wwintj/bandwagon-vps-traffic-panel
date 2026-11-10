import os
import asyncio
import secrets
import platform
import psutil
import json
import time
import html
import re
import socket
import urllib.request
import urllib.parse
import urllib.error
import hashlib
import ipaddress
from collections import deque
from datetime import datetime, timedelta
from calendar import monthrange
from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import logging
import tempfile

from app.config import AUTH_USERNAME, AUTH_PASSWORD, BASE_DIR, ENV_PATH, MONTH_RESET_DAY, PANEL_TITLE, PANEL_SUBTITLE, TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PUSH_HOUR, TELEGRAM_PUSH_MINUTE, TELEGRAM_TIMEZONE_OFFSET, TELEGRAM_TIMEZONE_LABEL, BANDWAGON_VEID, BANDWAGON_API_KEY, TRAFFIC_QUOTA_BYTES, CLOUDFLARE_DDNS_ENABLED, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ZONE_ID, CLOUDFLARE_RECORD_NAME, CLOUDFLARE_PROXIED, CLOUDFLARE_DDNS_INTERVAL, CLOUDFLARE_TTL, CLOUDFLARE_DDNS_NOTIFY, TRAFFIC_ALERT_ENABLED, TRAFFIC_ALERT_THRESHOLDS, VPS_EXPIRY_DATE, VPS_EXPIRY_NOTIFY
from app.database import init_db, get_db, get_meta_value, set_meta_value
from app.collector import collector_instance
from app import timeutil
from app import __version__

security = HTTPBasic()

logger = logging.getLogger("uvicorn.error")

TRUSTED_PROXIES = {"127.0.0.1", "::1"}
AUTH_FAILURE_WINDOW = 15 * 60
AUTH_FAILURE_LIMIT = 10
AUTH_FAILURES = {}

def get_client_ip(request: Request):
    """Real client IP. Proxy headers are only trusted when the request comes from a local reverse proxy."""
    peer = request.client.host if request.client else ""
    if peer in TRUSTED_PROXIES:
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            return real_ip
        forwarded_for = request.headers.get("x-forwarded-for", "")
        if forwarded_for:
            # Nginx appends the connecting address last; earlier entries are client-supplied.
            return forwarded_for.split(",")[-1].strip()
    return peer or "Unknown"

def _recent_auth_failures(client_ip, now):
    failures = AUTH_FAILURES.get(client_ip)
    if failures is None:
        return None
    while failures and now - failures[0][0] > AUTH_FAILURE_WINDOW:
        failures.popleft()
    if not failures:
        AUTH_FAILURES.pop(client_ip, None)
        return None
    return failures

def is_auth_locked(client_ip):
    failures = _recent_auth_failures(client_ip, time.time())
    # Count distinct wrong credentials, so a browser retrying one stale password while polling
    # does not lock its owner out; guessing many passwords does.
    return bool(failures) and len({digest for _ts, digest in failures}) >= AUTH_FAILURE_LIMIT

def record_auth_failure(client_ip, username, password):
    now = time.time()
    if len(AUTH_FAILURES) > 10000:
        AUTH_FAILURES.clear()
    failures = _recent_auth_failures(client_ip, now)
    if failures is None:
        failures = AUTH_FAILURES.setdefault(client_ip, deque(maxlen=200))
    digest = hashlib.sha256(f"{username}\0{password}".encode("utf-8")).hexdigest()[:16]
    failures.append((now, digest))

def verify_auth(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    client_ip = get_client_ip(request)
    if is_auth_locked(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed logins. Try again in 15 minutes.",
        )
    if not AUTH_USERNAME or not AUTH_PASSWORD:
        # Missing credentials in .env must lock the panel, never fall back to a default password.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AUTH_USERNAME / AUTH_PASSWORD is not configured in .env",
        )
    is_user_ok = secrets.compare_digest(credentials.username.encode("utf8"), AUTH_USERNAME.encode("utf8"))
    is_pass_ok = secrets.compare_digest(credentials.password.encode("utf8"), AUTH_PASSWORD.encode("utf8"))
    
    if not (is_user_ok and is_pass_ok):
        record_auth_failure(client_ip, credentials.username, credentials.password)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    AUTH_FAILURES.pop(client_ip, None)
    return credentials.username

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not AUTH_USERNAME or not AUTH_PASSWORD:
        logger.error("AUTH_USERNAME / AUTH_PASSWORD is empty; the panel will reject all logins until .env is fixed.")
    init_db()
    collector_task = asyncio.create_task(collector_instance.start())
    telegram_task = asyncio.create_task(telegram_daily_notifier())
    ddns_task = asyncio.create_task(ddns_background_updater())
    alert_task = asyncio.create_task(traffic_alert_watcher())
    yield
    collector_instance.running = False
    collector_task.cancel()
    telegram_task.cancel()
    ddns_task.cancel()
    alert_task.cancel()
    for task in (collector_task, telegram_task, ddns_task, alert_task):
        try:
            await task
        except asyncio.CancelledError:
            pass

app = FastAPI(lifespan=lifespan)

CSRF_HEADER = "x-panel-request"

@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    # Browsers resend cached Basic Auth credentials on cross-site form posts. A custom header
    # cannot be set by a plain form, and cross-origin fetch with it needs a CORS preflight we never allow.
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
        return JSONResponse({"detail": "Missing X-Panel-Request header"}, status_code=403)
    return await call_next(request)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "app/templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "app/static")), name="static")
SYSTEM_INFO_CACHE = {"public_ip": None, "ip_info": None, "updated_at": 0}
IP_LOOKUP_CACHE = {}
BANDWAGON_INFO_CACHE = {"data": None, "updated_at": 0}
SYSTEM_INFO_CACHE_TTL = 600
BANDWAGON_INFO_CACHE_TTL = 300

async def run_blocking(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args))

def get_os_pretty_name():
    try:
        with open('/etc/os-release', 'r') as f:
            for line in f:
                if line.startswith('PRETTY_NAME='):
                    return line.split('=', 1)[1].strip().strip('"')
    except Exception:
        pass
    return f"{platform.system()} {platform.release()}"

def clamp_day(year, month, day):
    return min(day, monthrange(year, month)[1])

def previous_month(year, month):
    if month == 1:
        return year - 1, 12
    return year, month - 1

def recent_months(count):
    now = timeutil.now()
    months = []
    year, month = now.year, now.month
    for _ in range(count):
        months.append((year, month))
        year, month = previous_month(year, month)
    return months[::-1]

def get_current_cycle_start(now):
    reset_day = clamp_day(now.year, now.month, MONTH_RESET_DAY)
    if now.day >= reset_day:
        return datetime(now.year, now.month, reset_day)

    prev_year, prev_month = previous_month(now.year, now.month)
    prev_reset_day = clamp_day(prev_year, prev_month, MONTH_RESET_DAY)
    return datetime(prev_year, prev_month, prev_reset_day)

def get_next_cycle_start(now):
    start = get_current_cycle_start(now)
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return datetime(year, month, clamp_day(year, month, MONTH_RESET_DAY))

def days_until(reset_date, today):
    return max(0, (reset_date - today).days)

def parse_expiry_date(value):
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None

def get_vps_expiry(today=None):
    """Days until the VPS expires; negative once it has expired. None when no date is set."""
    expiry = parse_expiry_date(VPS_EXPIRY_DATE)
    if not expiry:
        return None
    today = today or timeutil.now().date()
    return {"date": expiry.isoformat(), "days_left": (expiry - today).days}

def format_meta_time(value):
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value

def format_env_value(value):
    # Values are single-line; drop embedded newlines so one setting can never spill into another key.
    value = str(value).replace("\r", "").replace("\n", "")
    if any(ch.isspace() for ch in value) or "#" in value or '"' in value:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value

def parse_env_value(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value

def update_env_values(updates):
    existing = {}
    order = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                raw = line.rstrip("\n")
                if "=" in raw and not raw.lstrip().startswith("#"):
                    key, value = raw.split("=", 1)
                    existing[key] = parse_env_value(value)
                    order.append(key)

    for key, value in updates.items():
        existing[key] = str(value)
        if key not in order:
            order.append(key)

    # Write to a temp file and rename, so a crash or full disk never leaves a truncated .env behind.
    env_dir = os.path.dirname(ENV_PATH)
    fd, tmp_path = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=env_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for key in order:
                f.write(f"{key}={format_env_value(existing[key])}\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, ENV_PATH)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

def mask_secret(value):
    value = str(value or "")
    if not value:
        return ""
    return "********" + (value[-4:] if len(value) > 12 else "")

def resolve_secret(submitted, current):
    """Keep the stored secret when the browser sends back the masked placeholder."""
    submitted = str(submitted or "").strip()
    if current and submitted == mask_secret(current):
        return current
    return submitted

def is_safe_auth_value(value):
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@_.!%-")
    return bool(value) and all(ch in allowed for ch in value)

def to_traditional_text(value):
    phrase_map = {
        "服务器": "伺服器",
        "服务": "服務",
        "监控": "監控",
        "流量": "流量",
        "面板": "面板",
        "台湾": "臺灣",
        "后台": "後台",
        "现在": "現在",
        "这里": "這裡",
        "这个": "這個",
        "那个": "那個",
        "设置": "設定",
    }
    char_map = str.maketrans({
        "监": "監", "控": "控", "务": "務", "器": "器", "汉": "漢", "简": "簡", "体": "體",
        "国": "國", "东": "東", "广": "廣", "韩": "韓",
        "龙": "龍", "马": "馬", "门": "門", "云": "雲", "电": "電", "脑": "腦", "网": "網",
        "页": "頁", "题": "題", "标": "標", "副": "副", "时": "時", "间": "間", "运": "運",
        "行": "行", "总": "總", "传": "傳", "载": "載", "数": "數", "据": "據", "统": "統",
        "计": "計", "节": "節", "点": "點", "后": "後", "台": "臺", "机": "機", "场": "場",
        "区": "區", "设": "設", "置": "置", "开": "開", "关": "關", "启": "啟", "动": "動",
        "态": "態", "显": "顯", "示": "示", "维": "維", "护": "護", "者": "者", "密": "密",
        "码": "碼", "用": "用", "户": "戶", "名": "名", "登": "登", "录": "錄", "链": "鏈",
        "接": "接", "实": "實", "测": "測", "试": "試", "线": "線", "宽": "寬", "带": "帶",
        "现": "現", "这": "這", "个": "個", "为": "為", "发": "發", "过": "過", "会": "會",
        "与": "與", "对": "對", "归": "歸", "类": "類", "项": "項", "选": "選", "择": "擇",
        "认": "認", "证": "證", "书": "書", "删": "刪", "除": "除", "读": "讀", "写": "寫",
        "画": "畫", "图": "圖", "档": "檔", "锁": "鎖", "长": "長", "应": "應", "当": "當",
        "处": "處", "弹": "彈", "窗": "窗", "关": "關", "闭": "閉", "错": "錯", "误": "誤",
        "压": "壓", "缩": "縮", "径": "徑", "验": "驗", "坏": "壞",
        "导": "導", "页": "頁", "进": "進", "级": "級", "单": "單", "双": "雙", "临": "臨",
        "钟": "鐘", "块": "塊", "仅": "僅", "该": "該", "从": "從", "无": "無",
        "优": "優", "侧": "側", "栏": "欄", "号": "號", "软": "軟", "硬": "硬", "盘": "盤",
    })
    text = str(value or "")
    for source, target in phrase_map.items():
        text = text.replace(source, target)
    return text.translate(char_map)

def classify_ip_info(info):
    if not info or info.get("status") != "success":
        return "未知"
    if info.get("hosting"):
        return "機房 / VPS"
    if info.get("proxy"):
        return "代理 / VPN"
    if info.get("mobile"):
        return "行動網路"
    return "家寬 / 商寬"

def lookup_ip_info(ip):
    if not ip or ip == "Unknown":
        return {}
    now = time.time()
    cached = IP_LOOKUP_CACHE.get(ip)
    if cached and now - cached["updated_at"] < SYSTEM_INFO_CACHE_TTL:
        return cached["info"]

    try:
        fields = "status,message,country,regionName,city,lat,lon,timezone,isp,org,as,query,hosting,proxy,mobile"
        url = f"http://ip-api.com/json/{urllib.parse.quote(ip)}?fields={fields}&lang=zh-CN"
        with urllib.request.urlopen(url, timeout=3) as response:
            info = json.loads(response.read().decode("utf-8"))
    except Exception:
        info = {}
    IP_LOOKUP_CACHE[ip] = {"info": info, "updated_at": now}
    return info

def format_ip_location(info):
    if not info or info.get("status") != "success":
        return "Unknown"
    return to_traditional_text(" / ".join(filter(None, [
        info.get("country"),
        info.get("regionName"),
        info.get("city"),
    ])) or "Unknown")

def normalize_carrier_name(info):
    text = " ".join(filter(None, [
        str(info.get("isp") or ""),
        str(info.get("org") or ""),
        str(info.get("as") or ""),
    ])).lower()
    if any(word in text for word in ("mobile", "cmcc", "china mobile", "移动", "移動")):
        return "中國移動"
    if any(word in text for word in ("telecom", "chinanet", "china telecom", "电信", "電信")):
        return "中國電信"
    if any(word in text for word in ("unicom", "china unicom", "联通", "聯通")):
        return "中國聯通"
    if any(word in text for word in ("broadnet", "cernet", "教育网", "教育網")):
        return "中國教育網"
    return info.get("isp") or info.get("org") or "Unknown"

def classify_access_network(info):
    if not info or info.get("status") != "success":
        return "Unknown"
    carrier = normalize_carrier_name(info)
    if info.get("mobile"):
        access_type = "手機"
    elif info.get("hosting"):
        access_type = "機房"
    elif info.get("proxy"):
        access_type = "代理"
    else:
        access_type = "寬帶"
    return f"{carrier}{access_type}" if carrier != "Unknown" else access_type

# IPv4-only endpoints, tried in order until one answers with a valid address.
PUBLIC_IP_SOURCES = (
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://v4.ident.me",
    "https://checkip.amazonaws.com",
)

def fetch_public_ipv4():
    for url in PUBLIC_IP_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "VPS-Traffic-Panel"})
            with urllib.request.urlopen(req, timeout=4) as response:
                candidate = response.read().decode("utf-8").strip()
            return str(ipaddress.IPv4Address(candidate))
        except Exception:
            continue
    return None

def get_cached_public_ip_info(force=False):
    now = time.time()
    if not force and SYSTEM_INFO_CACHE["public_ip"] and now - SYSTEM_INFO_CACHE["updated_at"] < SYSTEM_INFO_CACHE_TTL:
        return SYSTEM_INFO_CACHE["public_ip"], SYSTEM_INFO_CACHE["ip_info"]

    public_ip = fetch_public_ipv4() or "Unknown"

    ip_info = lookup_ip_info(public_ip) if public_ip != "Unknown" else {}

    SYSTEM_INFO_CACHE["public_ip"] = public_ip
    SYSTEM_INFO_CACHE["ip_info"] = ip_info
    SYSTEM_INFO_CACHE["updated_at"] = now
    return public_ip, ip_info

def format_bytes(value):
    value = int(value or 0)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(value)
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    return f"{size:.2f} {units[unit_index]}"

def escape_html(value):
    return html.escape(str(value), quote=False)

def format_unix_time(value):
    try:
        timestamp = int(value)
        if timestamp <= 0:
            return ""
        return timeutil.from_unix(timestamp).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return str(value or "")

def request_json(url, method="GET", payload=None, headers=None, timeout=10):
    data = None
    request_headers = headers or {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers = {"Content-Type": "application/json", **request_headers}
    req = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # APIs like Cloudflare explain the failure in a JSON body on 4xx; keep it.
        raw = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            raise ValueError(f"HTTP {exc.code}: {raw[:200]}") from exc
    return json.loads(raw) if raw else {}

def get_traffic_summary(pending=None):
    """Blocking (SQLite + Bandwagon API). Call via run_blocking with collector_instance.snapshot_pending()."""
    if pending is None:
        pending = {}
    now = timeutil.now()
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''SELECT SUM(rx_bytes), SUM(tx_bytes) FROM hourly_traffic
                      WHERE year=? AND month=? AND day=?''', (now.year, now.month, now.day))
    row = cursor.fetchone()
    today_rx, today_tx = (row[0] or 0), (row[1] or 0)

    cycle_start = get_current_cycle_start(now)

    cursor.execute('''SELECT SUM(rx_bytes), SUM(tx_bytes) FROM hourly_traffic
                      WHERE datetime(
                          printf('%04d-%02d-%02d %02d:00:00', year, month, day, hour)
                      ) >= datetime(?)''', (cycle_start.strftime('%Y-%m-%d %H:%M:%S'),))
    row = cursor.fetchone()
    month_rx, month_tx = (row[0] or 0), (row[1] or 0)

    cursor.execute('SELECT SUM(rx_bytes), SUM(tx_bytes) FROM hourly_traffic')
    row = cursor.fetchone()
    total_rx, total_tx = (row[0] or 0), (row[1] or 0)
    conn.close()

    for (y, m, d, h), traffic in pending.items():
        if y == now.year and m == now.month and d == now.day:
            today_rx += traffic["rx"]
            today_tx += traffic["tx"]
        if datetime(y, m, d, h) >= cycle_start:
            month_rx += traffic["rx"]
            month_tx += traffic["tx"]
        total_rx += traffic["rx"]
        total_tx += traffic["tx"]

    summary = {
        "today_rx": today_rx, "today_tx": today_tx,
        "month_rx": month_rx, "month_tx": month_tx,
        "total_rx": total_rx, "total_tx": total_tx,
        "month_reset_day": MONTH_RESET_DAY,
        "cycle_start": cycle_start.isoformat(),
        "total_since": format_meta_time(get_meta_value("install_time", ""))
    }
    summary["traffic_quota"] = get_traffic_quota_summary(summary)
    summary["vps_expiry"] = get_vps_expiry(now.date())
    return summary

def fetch_bandwagon_info():
    if not BANDWAGON_VEID or not BANDWAGON_API_KEY:
        return None
    params = urllib.parse.urlencode({"veid": BANDWAGON_VEID, "api_key": BANDWAGON_API_KEY})
    url = f"https://api.64clouds.com/v1/getServiceInfo?{params}"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; VPS-Traffic-Panel/1.0)",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"error": "暫時無法連接 Bandwagon 官方 API，請稍後再試或檢查 VEID / API Key。"}
    if data.get("error"):
        return {"error": data.get("message") or data.get("error")}
    used = int(data.get("data_counter") or 0) * int(data.get("monthly_data_multiplier") or 1)
    limit = int(data.get("plan_monthly_data") or 0) * int(data.get("monthly_data_multiplier") or 1)
    return {
        "used": used,
        "limit": limit,
        "reset": data.get("data_next_reset") or "",
        "hostname": data.get("hostname") or "",
        "node": data.get("node_location") or data.get("node_alias") or data.get("location") or data.get("datacenter") or "",
    }

def fetch_bandwagon_info_cached(force=False):
    now = time.time()
    if not force and BANDWAGON_INFO_CACHE["data"] is not None and now - BANDWAGON_INFO_CACHE["updated_at"] < BANDWAGON_INFO_CACHE_TTL:
        return BANDWAGON_INFO_CACHE["data"]
    data = fetch_bandwagon_info()
    BANDWAGON_INFO_CACHE["data"] = data
    BANDWAGON_INFO_CACHE["updated_at"] = now
    return data

def get_traffic_quota_summary(summary):
    today = timeutil.now().date()
    bw = fetch_bandwagon_info_cached()
    if bw and not bw.get("error") and bw.get("limit"):
        used = int(bw.get("used") or 0)
        limit = int(bw.get("limit") or 0)
        try:
            reset_date = timeutil.from_unix(bw.get("reset")).date() if bw.get("reset") else None
        except (TypeError, ValueError, OSError):
            reset_date = None
        return {
            "source": "bandwagon",
            "source_label": "Bandwagon 官方",
            "used": used,
            "limit": limit,
            "percent": round((used / limit * 100) if limit else 0, 1),
            "available": True,
            "error": "",
            "reset_date": reset_date.isoformat() if reset_date else "",
            "days_left": days_until(reset_date, today) if reset_date else None,
        }

    used = int(summary.get("month_rx", 0) or 0) + int(summary.get("month_tx", 0) or 0)
    limit = int(TRAFFIC_QUOTA_BYTES or 0)
    reset_date = get_next_cycle_start(timeutil.now()).date()
    return {
        "source": "manual",
        "source_label": "手動配額",
        "used": used,
        "limit": limit,
        "percent": round((used / limit * 100) if limit else 0, 1),
        "available": bool(limit),
        "error": "" if limit else (bw.get("error") if bw and bw.get("error") else "尚未設定月流量配額"),
        "reset_date": reset_date.isoformat(),
        "days_left": days_until(reset_date, today),
    }

def parse_alert_thresholds(text):
    values = set()
    for part in re.split(r"[,\s，、]+", str(text or "")):
        if part.strip().isdigit():
            value = int(part)
            if 1 <= value <= 200:
                values.add(value)
    return sorted(values)

def check_traffic_alert(quota):
    """Send one Telegram alert when usage first crosses a threshold in the current billing cycle.
    Returns the threshold that was announced, or None."""
    if TRAFFIC_ALERT_ENABLED != "1" or not quota or not quota.get("available") or not quota.get("limit"):
        return None
    thresholds = parse_alert_thresholds(TRAFFIC_ALERT_THRESHOLDS)
    if not thresholds:
        return None
    used, limit = int(quota["used"]), int(quota["limit"])
    percent = used / limit * 100

    # A new billing cycle (or a different quota source) starts with a clean slate.
    cycle = f"{quota.get('source')}:{quota.get('reset_date', '')}"
    if get_meta_value("traffic_alert_cycle", "") != cycle:
        set_meta_value("traffic_alert_cycle", cycle)
        set_meta_value("traffic_alert_sent", "")
    sent = set(parse_alert_thresholds(get_meta_value("traffic_alert_sent", "")))

    crossed = [t for t in thresholds if percent >= t]
    fresh = [t for t in crossed if t not in sent]
    if not fresh:
        return None
    # Crossing several at once (e.g. enabling alerts at 95%) sends only the highest.
    top = max(fresh)
    title = f"流量已用完（{percent:.1f}%）" if top >= 100 else f"流量預警：已使用 {percent:.1f}%"
    lines = [
        f"來源：{escape_html(quota.get('source_label', ''))}",
        f"已用：<b>{format_bytes(used)}</b> / {format_bytes(limit)}",
        f"剩餘：<b>{format_bytes(max(0, limit - used))}</b>",
    ]
    if quota.get("days_left") is not None:
        lines.append(f"距離重置：<b>{quota['days_left']} 天</b>（{escape_html(quota.get('reset_date', ''))}）")
    lines.append(f"已達提醒門檻 {top}%，本週期不會再重複提醒這一檔。")
    if not send_telegram_notice(migration_telegram_message(title, lines)):
        return None
    set_meta_value("traffic_alert_sent", ",".join(str(t) for t in sorted(sent | set(crossed))))
    return top

def expiry_reminder_stage(days_left, today):
    """30 days out, 7 days out, then every day from 3 days out (including the day and after expiry)."""
    if days_left > 30:
        return None
    if days_left > 7:
        return "30"
    if days_left > 3:
        return "7"
    return f"day-{today.isoformat()}"

def check_expiry_reminder(now=None):
    """Telegram reminder before the VPS expires. Sent at the daily report time, not at midnight.
    Returns the stage that was sent, or None."""
    if VPS_EXPIRY_NOTIFY != "1":
        return None
    now = now or configured_now()
    expiry = get_vps_expiry(now.date())
    if not expiry:
        return None
    if (now.hour, now.minute) < (TELEGRAM_PUSH_HOUR, TELEGRAM_PUSH_MINUTE):
        return None
    days = expiry["days_left"]
    stage = expiry_reminder_stage(days, now.date())
    if not stage:
        return None

    # A new expiry date (e.g. after renewing) starts the reminder schedule over.
    if get_meta_value("expiry_reminder_date", "") != expiry["date"]:
        set_meta_value("expiry_reminder_date", expiry["date"])
        set_meta_value("expiry_reminder_sent", "")
    sent = [item for item in get_meta_value("expiry_reminder_sent", "").split(",") if item]
    if stage in sent:
        return None

    if days < 0:
        title = f"VPS 已過期 {-days} 天"
    elif days == 0:
        title = "VPS 今天到期"
    else:
        title = f"VPS 即將到期：剩 {days} 天"
    lines = [
        f"到期日：<b>{escape_html(expiry['date'])}</b>",
        "到期後 VPS 會停機，面板也將無法訪問，請盡快到 KiwiVM 續費。",
        "續費後請到面板「設定流量」更新到期日。",
    ]
    if days <= 3:
        lines.append("到期前 3 天起每天提醒一次。")
    if not send_telegram_notice(migration_telegram_message(title, lines)):
        return None
    set_meta_value("expiry_reminder_sent", ",".join((sent + [stage])[-10:]))
    return stage

def run_traffic_alert_check(pending):
    summary = get_traffic_summary(pending)
    return check_traffic_alert(summary.get("traffic_quota"))

async def traffic_alert_watcher():
    await asyncio.sleep(60)
    while True:
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            try:
                if TRAFFIC_ALERT_ENABLED == "1":
                    await run_blocking(run_traffic_alert_check, collector_instance.snapshot_pending())
                await run_blocking(check_expiry_reminder)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Traffic / expiry alert check failed: %s", exc)
        await asyncio.sleep(600)

def bandwagon_request(action, params=None):
    if not BANDWAGON_VEID or not BANDWAGON_API_KEY:
        raise ValueError("Bandwagon VEID / API Key is empty")
    query = {"veid": BANDWAGON_VEID, "api_key": BANDWAGON_API_KEY}
    if params:
        query.update(params)
    url = f"https://api.64clouds.com/v1/{action}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; VPS-Traffic-Panel/1.0)",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read().decode("utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}

BANDWAGON_LOCATION_FALLBACKS = {
    "CABC_1": "CA: British Columbia, Vancouver (AMD-F+NVMe) [CABC_1]",
    "CABC_6": "CA: British Columbia, Vancouver (AMD-F+NVMe, CN2GIA-E, CMIN2, CUP) [CABC_6]",
    "HKHK_1": "HK: Hong Kong (PCCW) [HKHK_1]",
    "HKHK_3": "HK: Hong Kong [HKHK_3]",
    "HKHK_8": "HK: Hong Kong (CN2 GIA) [HKHK_8]",
    "JPTYO_8": "JP: Tokyo (CN2 GIA) [JPTYO_8]",
    "JPOS_1": "JP: Osaka (SoftBank) [JPOS_1]",
    "JPOS_2": "JP: Osaka (SoftBank) [JPOS_2]",
    "EUNL_1": "NL: Amsterdam [EUNL_1]",
    "EUNL_2": "NL: Amsterdam [EUNL_2]",
    "EUNL_3": "NL: Amsterdam [EUNL_3]",
    "EUNL_9": "NL: Amsterdam (China Unicom Premium / AS9929) [EUNL_9]",
    "AEDXB_1": "AE: Dubai [AEDXB_1]",
    "AUSYD_1": "AU: New South Wales, Sydney (AS9929) [AUSYD_1]",
    "USCA_FMT": "US: California, Fremont [USCA_FMT]",
    "USCA": "US: California, Los Angeles [USCA]",
    "USCA_2": "US: California, Los Angeles (DC2) [USCA_2]",
    "USCA_3": "US: California, Los Angeles (DC3 CN2) [USCA_3]",
    "USCA_4": "US: California, Los Angeles (DC4 MCOM) [USCA_4]",
    "USCA_6": "US: California, Los Angeles (DC6 CT CN2GIA-E) [USCA_6]",
    "USCA_8": "US: California, Los Angeles (DC8 CN2) [USCA_8]",
    "USCA_9": "US: California, Los Angeles (DC9 CT CN2GIA, CMIN2, CUP) [USCA_9]",
    "USNY": "US: New York [USNY]",
    "USNY_2": "US: New York [USNY_2]",
    "USNY_6": "US: New York (Coresite NY1) [USNY_6]",
    "USNY_8": "US: New York (CN2 GIA + Premium) [USNY_8]",
    "USNJ": "US: New Jersey [USNJ]",
    "USNJ_2": "US: New Jersey [USNJ_2]",
    "USFL": "US: Florida [USFL]",
    "USGA": "US: Georgia, Atlanta [USGA]",
    "USIL": "US: Illinois, Chicago [USIL]",
    "USWA": "US: Washington, Seattle [USWA]",
    "USAZ": "US: Arizona, Phoenix [USAZ]",
    "USAZ_2": "US: Arizona, Phoenix [USAZ_2]",
}

def infer_bandwagon_location_label(code):
    if code in BANDWAGON_LOCATION_FALLBACKS:
        return BANDWAGON_LOCATION_FALLBACKS[code]
    prefix_labels = (
        ("HKHK", "HK: Hong Kong"),
        ("JPTYO", "JP: Tokyo"),
        ("JPOS", "JP: Osaka"),
        ("EUNL", "NL: Amsterdam"),
        ("AEDXB", "AE: Dubai"),
        ("AUSYD", "AU: New South Wales, Sydney"),
        ("CABC", "CA: British Columbia, Vancouver"),
        ("USCA_FMT", "US: California, Fremont"),
        ("USCA", "US: California, Los Angeles"),
        ("USNY", "US: New York"),
        ("USNJ", "US: New Jersey"),
        ("USFL", "US: Florida"),
        ("USGA", "US: Georgia, Atlanta"),
        ("USIL", "US: Illinois, Chicago"),
        ("USWA", "US: Washington, Seattle"),
        ("USAZ", "US: Arizona, Phoenix"),
    )
    for prefix, label in prefix_labels:
        if code.startswith(prefix):
            return f"{label} [{code}]"
    return code

def extract_bandwagon_code(value):
    text = str(value or "").strip()
    bracket_match = re.search(r"\[([A-Z0-9_]+)\]", text)
    if bracket_match:
        return bracket_match.group(1)
    plain_match = re.search(r"\b([A-Z]{2,6}(?:_[A-Z0-9]+)?)\b", text)
    return plain_match.group(1) if plain_match else text

def normalize_bandwagon_locations(data):
    def pick(item, keys, fallback=""):
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                return str(value)
        return fallback

    def build_location_label(code, item):
        if not isinstance(item, dict):
            return str(item)

        country_code = pick(item, ("country_code", "countryCode", "country", "iso", "region_code"))
        state = pick(item, ("state", "province", "region", "regionName"))
        city = pick(item, ("city", "city_name", "location_city"))
        base = pick(item, ("display", "display_name", "label", "name", "location", "description"), code)

        if country_code and city:
            place_parts = [part for part in (state, city) if part]
            base = f"{country_code}: {', '.join(place_parts)}"

        features = pick(item, ("features", "feature", "plan_features", "route", "routes", "network", "network_type"))
        if isinstance(item.get("features"), list):
            features = ", ".join(str(part) for part in item["features"])
        if not features and "(" in base and ")" in base:
            features = ""
        if features and features not in base:
            base = f"{base} ({features})"

        if code and f"[{code}]" not in base:
            base = f"{base} [{code}]"
        return base

    source = data
    for key in ("locations", "data", "available_locations"):
        if isinstance(source, dict) and key in source:
            source = source[key]
            break

    locations = []
    if isinstance(source, dict):
        iterable = source.items()
    elif isinstance(source, list):
        iterable = enumerate(source)
    else:
        iterable = []

    for key, item in iterable:
        if isinstance(item, dict):
            code = extract_bandwagon_code(item.get("id") or item.get("location") or item.get("code") or key)
            name = build_location_label(code, item)
            available = item.get("available", item.get("enabled", True))
        else:
            code = extract_bandwagon_code(key if str(key) in BANDWAGON_LOCATION_FALLBACKS else item)
            name = str(item)
            available = True
        if name == code or len(name) <= len(code) + 8:
            name = infer_bandwagon_location_label(code)
        locations.append({"code": code, "name": name, "available": bool(available)})
    return locations

def normalize_current_bandwagon_location(value):
    if not value:
        return ""
    text = str(value).strip()
    code = extract_bandwagon_code(text)
    if text == code or len(text) <= len(code) + 8:
        return infer_bandwagon_location_label(code)
    return text

def match_current_location_label(current_value, locations):
    code = extract_bandwagon_code(current_value)
    for location in locations:
        if location.get("code") == code:
            return location.get("name") or normalize_current_bandwagon_location(current_value)
    current_text = str(current_value or "").strip().lower()
    if current_text:
        for location in locations:
            name = str(location.get("name") or "")
            if current_text in name.lower():
                return name
    return normalize_current_bandwagon_location(current_value)

def resolve_domain_ips(domain):
    if not domain:
        return []
    try:
        return sorted(set(socket.gethostbyname_ex(domain)[2]))
    except Exception:
        return []

def migration_telegram_message(title, lines):
    now = configured_now().strftime("%Y-%m-%d %H:%M")
    body = [f"<b>{escape_html(title)}</b>", f"<code>{now} {escape_html(TELEGRAM_TIMEZONE_LABEL)}</code>", ""]
    body.extend(lines)
    return "\n".join(body)

def migration_meta_snapshot():
    return {
        "active": get_meta_value("migration_active", "0") == "1",
        "started_at": format_meta_time(get_meta_value("migration_started_at", "")),
        "target": get_meta_value("migration_target", ""),
        "old_ip": get_meta_value("migration_old_ip", ""),
        "new_ip": get_meta_value("migration_new_ip", ""),
        "domain": get_meta_value("migration_domain", ""),
    }

def build_migration_status():
    meta = migration_meta_snapshot()
    current_ip, _ = get_cached_public_ip_info(force=True)
    old_ip = meta["old_ip"]
    new_ip = meta["new_ip"]
    old_ip_known = bool(old_ip) and old_ip != "Unknown"
    ip_changed = bool(old_ip_known and current_ip != "Unknown" and current_ip != old_ip)
    if ip_changed and not new_ip:
        new_ip = current_ip
        set_meta_value("migration_new_ip", new_ip)

    ddns = {
        "enabled": CLOUDFLARE_DDNS_ENABLED == "1",
        "synced": False,
        "record": CLOUDFLARE_RECORD_NAME,
        "proxied": CLOUDFLARE_PROXIED == "1",
        "error": "",
    }
    if ip_changed and ddns["enabled"]:
        try:
            sync_cloudflare_ddns(current_ip)
            ddns["synced"] = True
        except Exception as exc:
            ddns["error"] = str(exc)

    domain_ips = resolve_domain_ips(CLOUDFLARE_RECORD_NAME) if CLOUDFLARE_RECORD_NAME else []
    domain_ok = bool(new_ip and ((ddns["proxied"] and ddns["synced"]) or new_ip in domain_ips))

    if ip_changed and get_meta_value("migration_notified_ip", "0") != "1":
        send_telegram_message_if_ready(migration_telegram_message("服務器機房切換成功", [
            f"目標機房：<b>{escape_html(meta['target'] or '未知')}</b>",
            f"舊 IP：<code>{escape_html(old_ip)}</code>",
            f"新 IP：<code>{escape_html(new_ip)}</code>",
        ]))
        set_meta_value("migration_notified_ip", "1")

    if domain_ok and get_meta_value("migration_notified_dns", "0") != "1":
        dns_line = "Cloudflare 代理已同步到新 IP。" if ddns["proxied"] else f"解析 IP：<code>{escape_html(new_ip)}</code>"
        send_telegram_message_if_ready(migration_telegram_message("域名解析已同步", [
            f"域名：<code>{escape_html(CLOUDFLARE_RECORD_NAME)}</code>",
            dns_line,
            "現在可以嘗試使用域名訪問面板。",
        ]))
        set_meta_value("migration_notified_dns", "1")
        set_meta_value("migration_active", "0")
    elif ip_changed and not ddns["enabled"]:
        set_meta_value("migration_active", "0")

    return {
        **meta,
        "active": get_meta_value("migration_active", "0") == "1",
        "current_ip": current_ip,
        "new_ip": new_ip,
        "ip_changed": ip_changed,
        "ddns": ddns,
        "domain_ips": domain_ips,
        "domain_ok": domain_ok,
    }

def cloudflare_headers():
    return {
        "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
        "Accept": "application/json",
    }

CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
ZONE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
CLOUDFLARE_ZONE_CACHE = {}
# Re-check the DNS record at least this often even if our IP did not change,
# so a record edited by hand (or by another tool) is put back.
DDNS_RECHECK_SECONDS = 6 * 3600

def cloudflare_error_text(response, fallback):
    errors = response.get("errors") or []
    parts = []
    for err in errors:
        if isinstance(err, dict):
            parts.append(f"{err.get('code', '')} {err.get('message', '')}".strip())
        else:
            parts.append(str(err))
    text = "；".join(p for p in parts if p) or fallback
    codes = {err.get("code") for err in errors if isinstance(err, dict)}
    if codes & {9109, 10000, 9103, 6003, 6111}:
        text += "（請確認 API Token 正確，並具有該網域的 Zone.DNS:Edit 權限）"
    return text

def resolve_cloudflare_zone_id():
    """CLOUDFLARE_ZONE_ID may hold a zone ID, a zone name (example.com), or be empty;
    when empty the zone is found from the record name (www.example.com -> example.com)."""
    zone = CLOUDFLARE_ZONE_ID.strip()
    if ZONE_ID_PATTERN.match(zone.lower()):
        return zone
    record = CLOUDFLARE_RECORD_NAME.strip().rstrip(".").lower()
    cache_key = (zone.lower(), record)
    if cache_key in CLOUDFLARE_ZONE_CACHE:
        return CLOUDFLARE_ZONE_CACHE[cache_key]

    if zone:
        candidates = [zone.rstrip(".").lower()]
    else:
        labels = record.split(".")
        candidates = [".".join(labels[i:]) for i in range(len(labels) - 1)]
    for name in candidates:
        response = request_json(f"{CLOUDFLARE_API}/zones?{urllib.parse.urlencode({'name': name})}", headers=cloudflare_headers())
        if not response.get("success"):
            raise ValueError("Cloudflare 查詢 Zone 失敗：" + cloudflare_error_text(response, "未知錯誤"))
        result = response.get("result") or []
        if result:
            CLOUDFLARE_ZONE_CACHE[cache_key] = result[0]["id"]
            return result[0]["id"]
    raise ValueError(f"在這個 Cloudflare Token 可存取的網域中找不到 {zone or record}")

def sync_cloudflare_ddns(public_ip=None):
    if not CLOUDFLARE_API_TOKEN or not CLOUDFLARE_RECORD_NAME:
        raise ValueError("請填寫 Cloudflare API Token 與完整域名")

    if not public_ip:
        public_ip, _ = get_cached_public_ip_info(force=True)
    if not public_ip or public_ip == "Unknown":
        raise ValueError("無法取得目前 VPS 公網 IP")

    zone_id = resolve_cloudflare_zone_id()
    base_url = f"{CLOUDFLARE_API}/zones/{urllib.parse.quote(zone_id)}/dns_records"
    query = urllib.parse.urlencode({"type": "A", "name": CLOUDFLARE_RECORD_NAME})
    records = request_json(f"{base_url}?{query}", headers=cloudflare_headers())
    if not records.get("success"):
        raise ValueError("Cloudflare 查詢 DNS 記錄失敗：" + cloudflare_error_text(records, "未知錯誤"))

    proxied = CLOUDFLARE_PROXIED == "1"
    # Proxied records always use automatic TTL on Cloudflare.
    ttl = 1 if proxied else CLOUDFLARE_TTL
    body = {
        "type": "A",
        "name": CLOUDFLARE_RECORD_NAME,
        "content": public_ip,
        "ttl": ttl,
        "proxied": proxied,
    }
    existing = records.get("result") or []
    current = existing[0] if existing else None
    previous_ip = current.get("content", "") if current else ""
    if current and current.get("content") == public_ip and bool(current.get("proxied")) == proxied \
            and (proxied or current.get("ttl") == ttl):
        action = "unchanged"
    else:
        if current:
            result = request_json(f"{base_url}/{current['id']}", method="PATCH", payload=body, headers=cloudflare_headers())
            action = "updated"
        else:
            result = request_json(base_url, method="POST", payload=body, headers=cloudflare_headers())
            action = "created"
        if not result.get("success"):
            raise ValueError("Cloudflare 更新 DNS 記錄失敗：" + cloudflare_error_text(result, "未知錯誤"))

    set_meta_value("cloudflare_ddns_last_ip", public_ip)
    set_meta_value("cloudflare_ddns_last_sync", timeutil.now().isoformat())
    return {
        "ok": True,
        "action": action,
        "ip": public_ip,
        "previous_ip": previous_ip,
        "record": CLOUDFLARE_RECORD_NAME,
    }

def send_telegram_notice(text):
    """Event notices (DDNS) only need a bot token and chat id; the daily-report switch is separate."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        send_telegram_message(text)
        return True
    except Exception:
        return False

def ddns_notice(title, lines):
    if CLOUDFLARE_DDNS_NOTIFY != "1":
        return False
    return send_telegram_notice(migration_telegram_message(title, lines))

def _ddns_sync_is_stale():
    last_sync = get_meta_value("cloudflare_ddns_last_sync", "")
    if not last_sync:
        return True
    try:
        age = (timeutil.now() - datetime.fromisoformat(last_sync)).total_seconds()
    except ValueError:
        return True
    return age > DDNS_RECHECK_SECONDS

def ddns_check_once():
    """One DDNS pass: refresh the public IP, sync Cloudflare when needed, notify on change or failure."""
    set_meta_value("cloudflare_ddns_last_check", timeutil.now().isoformat())
    public_ip, _ = get_cached_public_ip_info(force=True)
    last_ip = get_meta_value("cloudflare_ddns_last_ip", "")
    last_error = get_meta_value("cloudflare_ddns_last_error", "")

    if public_ip != "Unknown" and public_ip == last_ip and not last_error and not _ddns_sync_is_stale():
        return {"ok": True, "action": "skipped", "ip": public_ip}

    try:
        if public_ip == "Unknown":
            raise ValueError("無法從任何來源取得 VPS 公網 IPv4")
        result = sync_cloudflare_ddns(public_ip)
    except Exception as exc:
        error = str(exc)
        set_meta_value("cloudflare_ddns_last_error", error)
        # Notify once per distinct error, not on every retry.
        if error != last_error:
            ddns_notice("DDNS 更新失敗", [
                f"域名：<code>{escape_html(CLOUDFLARE_RECORD_NAME)}</code>",
                f"IP：<code>{escape_html(public_ip)}</code>",
                f"錯誤：{escape_html(error)}",
                f"面板會每 {CLOUDFLARE_DDNS_INTERVAL} 分鐘自動重試。",
            ])
        logger.warning("Cloudflare DDNS update failed: %s", error)
        return {"ok": False, "error": error}

    if last_error:
        set_meta_value("cloudflare_ddns_last_error", "")
    # During a Bandwagon migration the migration flow sends its own messages.
    migrating = get_meta_value("migration_active", "0") == "1"
    if result["action"] in ("updated", "created") and not migrating:
        ddns_notice("DDNS 已更新", [
            f"域名：<code>{escape_html(CLOUDFLARE_RECORD_NAME)}</code>",
            f"舊 IP：<code>{escape_html(result.get('previous_ip') or last_ip or '無')}</code>",
            f"新 IP：<code>{escape_html(public_ip)}</code>",
        ])
    elif last_error and not migrating:
        ddns_notice("DDNS 已恢復正常", [
            f"域名：<code>{escape_html(CLOUDFLARE_RECORD_NAME)}</code>",
            f"IP：<code>{escape_html(public_ip)}</code>",
        ])
    return result

async def ddns_background_updater():
    # First check shortly after start (e.g. right after a reboot or migration), then every interval.
    await asyncio.sleep(20)
    while True:
        if CLOUDFLARE_DDNS_ENABLED == "1":
            try:
                await run_blocking(ddns_check_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Cloudflare DDNS check failed: %s", exc)
        await asyncio.sleep(CLOUDFLARE_DDNS_INTERVAL * 60)

def build_telegram_message(pending=None):
    summary = get_traffic_summary(pending)
    ip, ip_info = get_cached_public_ip_info()
    location = " / ".join(filter(None, [ip_info.get('country'), ip_info.get('regionName'), ip_info.get('city')])) or "Unknown"
    now = configured_now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"<b>{escape_html(PANEL_TITLE)}</b>",
        f"<code>{now} {escape_html(TELEGRAM_TIMEZONE_LABEL)}</code>",
        "",
        "<b>本期重點</b>",
        f"下載        <b>{format_bytes(summary['month_rx'])}</b>",
        f"上傳        <b>{format_bytes(summary['month_tx'])}</b>",
        f"重置日      每月 {summary['month_reset_day']} 日",
        f"距離重置    {days_until(get_next_cycle_start(configured_now()).date(), configured_now().date())} 天",
    ]
    expiry = get_vps_expiry(configured_now().date())
    if expiry:
        days = expiry["days_left"]
        state = f"已過期 {-days} 天" if days < 0 else f"<b>{days} 天</b>" if days <= 14 else f"{days} 天"
        lines.append(f"VPS 到期    {state}（{expiry['date']}）")
    lines += [
        "",
        "<b>今日流量</b>",
        f"下載        {format_bytes(summary['today_rx'])}",
        f"上傳        {format_bytes(summary['today_tx'])}",
    ]

    bw = fetch_bandwagon_info()
    if bw:
        lines.append("")
        lines.append("<b>Bandwagon 官方配額</b>")
        if bw.get("error"):
            lines.append(f"狀態：{escape_html(bw['error'])}")
        else:
            percent = (bw["used"] / bw["limit"] * 100) if bw.get("limit") else 0
            remaining = max(0, int(bw.get("limit") or 0) - int(bw.get("used") or 0))
            lines.append(f"已用        <b>{format_bytes(bw['used'])}</b>")
            lines.append(f"配額        {format_bytes(bw['limit'])}")
            lines.append(f"剩餘        <b>{format_bytes(remaining)}</b>")
            lines.append(f"使用率      <b>{percent:.1f}%</b>")
            if bw.get("reset"):
                lines.append(f"重置        {escape_html(format_unix_time(bw['reset']))} {escape_html(TELEGRAM_TIMEZONE_LABEL)}")
                try:
                    lines.append(f"剩餘天數    <b>{days_until(timeutil.from_unix(bw['reset']).date(), configured_now().date())} 天</b>")
                except (TypeError, ValueError, OSError):
                    pass
            if bw.get("node"):
                lines.append(f"節點        {escape_html(bw['node'])}")

    lines.extend([
        "",
        "<b>服務器信息</b>",
        f"IP          <code>{escape_html(ip)}</code>",
        f"位置        {escape_html(location)}",
        f"類型        <b>{escape_html(classify_ip_info(ip_info))}</b>",
    ])
    return "\n".join(lines)

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("Telegram token or chat id is empty")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))

def send_daily_report(pending):
    return send_telegram_message(build_telegram_message(pending))

def mark_today_sent_if_past_push_time():
    """After settings change, don't fire a surprise catch-up report for a push time already passed today."""
    now = configured_now()
    if (now.hour, now.minute) > (TELEGRAM_PUSH_HOUR, TELEGRAM_PUSH_MINUTE):
        set_meta_value("telegram_last_daily", now.strftime("%Y-%m-%d"))

TELEGRAM_CATCHUP = timedelta(hours=2)

def should_send_daily_report(now, last_sent_day):
    """True from the push time until TELEGRAM_CATCHUP later, once per day. The window means a late
    loop tick or a failed send is retried instead of silently skipping the whole day."""
    target = now.replace(hour=TELEGRAM_PUSH_HOUR, minute=TELEGRAM_PUSH_MINUTE, second=0, microsecond=0)
    if now < target or now - target > TELEGRAM_CATCHUP:
        return False
    return last_sent_day != now.strftime("%Y-%m-%d")

def send_telegram_message_if_ready(text):
    if TELEGRAM_ENABLED != "1" or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        send_telegram_message(text)
        return True
    except Exception:
        return False

def configured_now():
    return timeutil.now()

async def telegram_daily_notifier():
    while True:
        try:
            await asyncio.sleep(30)
            if TELEGRAM_ENABLED != "1" or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
                continue
            now = configured_now()
            if not should_send_daily_report(now, get_meta_value("telegram_last_daily")):
                continue
            today_key = now.strftime("%Y-%m-%d")
            await run_blocking(send_daily_report, collector_instance.snapshot_pending())
            set_meta_value("telegram_last_daily", today_key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Telegram daily report failed, will retry: %s", exc)
            await asyncio.sleep(300)

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, username: str = Depends(verify_auth)):
    return templates.TemplateResponse(request, "index.html", {
        "interface": collector_instance.interface,
        "panel_title": PANEL_TITLE,
        "panel_subtitle": PANEL_SUBTITLE,
        "version": __version__,
    })

@app.get("/logout")
async def logout():
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Logged out",
        headers={"WWW-Authenticate": "Basic realm=\"Logged out\""},
    )

@app.get("/api/settings")
async def api_get_settings(username: str = Depends(verify_auth)):
    return {
        "auth_username": AUTH_USERNAME,
        "panel_title": PANEL_TITLE,
        "panel_subtitle": PANEL_SUBTITLE,
    }

@app.post("/api/settings")
async def api_update_settings(payload: dict, username: str = Depends(verify_auth)):
    global AUTH_USERNAME, AUTH_PASSWORD, PANEL_TITLE, PANEL_SUBTITLE

    new_username = str(payload.get("auth_username", "")).strip()
    new_password = str(payload.get("auth_password", "")).strip()
    new_title = to_traditional_text(payload.get("panel_title", "")).strip()
    new_subtitle = to_traditional_text(payload.get("panel_subtitle", "")).strip()

    if not is_safe_auth_value(new_username):
        raise HTTPException(status_code=400, detail="Invalid username")
    if new_password and not is_safe_auth_value(new_password):
        raise HTTPException(status_code=400, detail="Invalid password")
    if not new_title or not new_subtitle:
        raise HTTPException(status_code=400, detail="Panel title and subtitle cannot be empty")

    updates = {
        "AUTH_USERNAME": new_username,
        "PANEL_TITLE": new_title,
        "PANEL_SUBTITLE": new_subtitle,
    }
    if new_password:
        updates["AUTH_PASSWORD"] = new_password

    update_env_values(updates)
    AUTH_USERNAME = new_username
    if new_password:
        AUTH_PASSWORD = new_password
    PANEL_TITLE = new_title
    PANEL_SUBTITLE = new_subtitle

    return {"ok": True, "panel_title": PANEL_TITLE, "panel_subtitle": PANEL_SUBTITLE}

@app.get("/api/telegram-settings")
async def api_get_telegram_settings(username: str = Depends(verify_auth)):
    return {
        "telegram_enabled": TELEGRAM_ENABLED == "1",
        "telegram_chat_id": TELEGRAM_CHAT_ID,
        "telegram_bot_token": mask_secret(TELEGRAM_BOT_TOKEN),
        "telegram_push_hour": TELEGRAM_PUSH_HOUR,
        "telegram_push_minute": TELEGRAM_PUSH_MINUTE,
        "telegram_timezone_offset": TELEGRAM_TIMEZONE_OFFSET,
        "telegram_timezone_label": TELEGRAM_TIMEZONE_LABEL,
        "bandwagon_veid": BANDWAGON_VEID,
        "bandwagon_api_key": mask_secret(BANDWAGON_API_KEY),
    }

@app.post("/api/telegram-settings")
async def api_update_telegram_settings(payload: dict, username: str = Depends(verify_auth)):
    global TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PUSH_HOUR, TELEGRAM_PUSH_MINUTE, TELEGRAM_TIMEZONE_OFFSET, TELEGRAM_TIMEZONE_LABEL, BANDWAGON_VEID, BANDWAGON_API_KEY

    TELEGRAM_ENABLED = "1" if payload.get("telegram_enabled") else "0"
    TELEGRAM_BOT_TOKEN = resolve_secret(payload.get("telegram_bot_token", ""), TELEGRAM_BOT_TOKEN)
    TELEGRAM_CHAT_ID = str(payload.get("telegram_chat_id", "")).strip()
    try:
        TELEGRAM_PUSH_HOUR = int(payload.get("telegram_push_hour", 20))
    except (TypeError, ValueError):
        TELEGRAM_PUSH_HOUR = 20
    TELEGRAM_PUSH_HOUR = max(0, min(23, TELEGRAM_PUSH_HOUR))
    try:
        TELEGRAM_PUSH_MINUTE = int(payload.get("telegram_push_minute", 0))
    except (TypeError, ValueError):
        TELEGRAM_PUSH_MINUTE = 0
    TELEGRAM_PUSH_MINUTE = max(0, min(59, TELEGRAM_PUSH_MINUTE))
    try:
        TELEGRAM_TIMEZONE_OFFSET = int(payload.get("telegram_timezone_offset", 8))
    except (TypeError, ValueError):
        TELEGRAM_TIMEZONE_OFFSET = 8
    TELEGRAM_TIMEZONE_OFFSET = max(-12, min(14, TELEGRAM_TIMEZONE_OFFSET))
    timeutil.set_offset(TELEGRAM_TIMEZONE_OFFSET)
    TELEGRAM_TIMEZONE_LABEL = str(payload.get("telegram_timezone_label", "中國時間")).strip() or "自訂時區"
    BANDWAGON_VEID = str(payload.get("bandwagon_veid", "")).strip()
    BANDWAGON_API_KEY = resolve_secret(payload.get("bandwagon_api_key", ""), BANDWAGON_API_KEY)
    BANDWAGON_INFO_CACHE["data"] = None
    BANDWAGON_INFO_CACHE["updated_at"] = 0

    update_env_values({
        "TELEGRAM_ENABLED": TELEGRAM_ENABLED,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "TELEGRAM_PUSH_HOUR": TELEGRAM_PUSH_HOUR,
        "TELEGRAM_PUSH_MINUTE": TELEGRAM_PUSH_MINUTE,
        "TELEGRAM_TIMEZONE_OFFSET": TELEGRAM_TIMEZONE_OFFSET,
        "TELEGRAM_TIMEZONE_LABEL": TELEGRAM_TIMEZONE_LABEL,
        "BANDWAGON_VEID": BANDWAGON_VEID,
        "BANDWAGON_API_KEY": BANDWAGON_API_KEY,
    })
    mark_today_sent_if_past_push_time()
    return {"ok": True}

@app.post("/api/telegram-test")
async def api_telegram_test(username: str = Depends(verify_auth)):
    try:
        await run_blocking(send_daily_report, collector_instance.snapshot_pending())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}

@app.get("/api/ddns-settings")
async def api_get_ddns_settings(username: str = Depends(verify_auth)):
    return {
        "cloudflare_ddns_enabled": CLOUDFLARE_DDNS_ENABLED == "1",
        "cloudflare_zone_id": CLOUDFLARE_ZONE_ID,
        "cloudflare_record_name": CLOUDFLARE_RECORD_NAME,
        "cloudflare_api_token": mask_secret(CLOUDFLARE_API_TOKEN),
        "cloudflare_proxied": CLOUDFLARE_PROXIED == "1",
        "cloudflare_ddns_interval": CLOUDFLARE_DDNS_INTERVAL,
        "cloudflare_ttl": CLOUDFLARE_TTL,
        "cloudflare_ddns_notify": CLOUDFLARE_DDNS_NOTIFY == "1",
        "telegram_ready": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "last_ip": get_meta_value("cloudflare_ddns_last_ip", ""),
        "last_sync": format_meta_time(get_meta_value("cloudflare_ddns_last_sync", "")),
        "last_check": format_meta_time(get_meta_value("cloudflare_ddns_last_check", "")),
        "last_error": get_meta_value("cloudflare_ddns_last_error", ""),
    }

@app.post("/api/ddns-settings")
async def api_update_ddns_settings(payload: dict, username: str = Depends(verify_auth)):
    global CLOUDFLARE_DDNS_ENABLED, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ZONE_ID, CLOUDFLARE_RECORD_NAME, CLOUDFLARE_PROXIED
    global CLOUDFLARE_DDNS_INTERVAL, CLOUDFLARE_TTL, CLOUDFLARE_DDNS_NOTIFY

    CLOUDFLARE_DDNS_ENABLED = "1" if payload.get("cloudflare_ddns_enabled") else "0"
    CLOUDFLARE_API_TOKEN = resolve_secret(payload.get("cloudflare_api_token", ""), CLOUDFLARE_API_TOKEN)
    CLOUDFLARE_ZONE_ID = str(payload.get("cloudflare_zone_id", "")).strip()
    CLOUDFLARE_RECORD_NAME = str(payload.get("cloudflare_record_name", "")).strip()
    CLOUDFLARE_PROXIED = "1" if payload.get("cloudflare_proxied") else "0"
    try:
        CLOUDFLARE_DDNS_INTERVAL = max(1, min(60, int(payload.get("cloudflare_ddns_interval", CLOUDFLARE_DDNS_INTERVAL))))
        ttl = int(payload.get("cloudflare_ttl", CLOUDFLARE_TTL))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid check interval or TTL")
    CLOUDFLARE_TTL = 1 if ttl < 60 else min(86400, ttl)
    CLOUDFLARE_DDNS_NOTIFY = "1" if payload.get("cloudflare_ddns_notify", True) else "0"
    CLOUDFLARE_ZONE_CACHE.clear()
    # Settings changed: make the next check talk to Cloudflare instead of trusting the last result.
    set_meta_value("cloudflare_ddns_last_sync", "")
    update_env_values({
        "CLOUDFLARE_DDNS_ENABLED": CLOUDFLARE_DDNS_ENABLED,
        "CLOUDFLARE_API_TOKEN": CLOUDFLARE_API_TOKEN,
        "CLOUDFLARE_ZONE_ID": CLOUDFLARE_ZONE_ID,
        "CLOUDFLARE_RECORD_NAME": CLOUDFLARE_RECORD_NAME,
        "CLOUDFLARE_PROXIED": CLOUDFLARE_PROXIED,
        "CLOUDFLARE_DDNS_INTERVAL": CLOUDFLARE_DDNS_INTERVAL,
        "CLOUDFLARE_TTL": CLOUDFLARE_TTL,
        "CLOUDFLARE_DDNS_NOTIFY": CLOUDFLARE_DDNS_NOTIFY,
    })
    return {"ok": True}

@app.post("/api/ddns-sync")
async def api_ddns_sync(username: str = Depends(verify_auth)):
    try:
        result = await run_blocking(sync_cloudflare_ddns)
    except Exception as exc:
        set_meta_value("cloudflare_ddns_last_error", str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    set_meta_value("cloudflare_ddns_last_error", "")
    return result

@app.get("/api/bandwagon-settings")
async def api_get_bandwagon_settings(username: str = Depends(verify_auth)):
    return {"bandwagon_veid": BANDWAGON_VEID, "bandwagon_api_key": mask_secret(BANDWAGON_API_KEY)}

@app.post("/api/bandwagon-settings")
async def api_update_bandwagon_settings(payload: dict, username: str = Depends(verify_auth)):
    global BANDWAGON_VEID, BANDWAGON_API_KEY
    BANDWAGON_VEID = str(payload.get("bandwagon_veid", "")).strip()
    BANDWAGON_API_KEY = resolve_secret(payload.get("bandwagon_api_key", ""), BANDWAGON_API_KEY)
    BANDWAGON_INFO_CACHE["data"] = None
    BANDWAGON_INFO_CACHE["updated_at"] = 0
    update_env_values({"BANDWAGON_VEID": BANDWAGON_VEID, "BANDWAGON_API_KEY": BANDWAGON_API_KEY})
    return {"ok": True}

@app.get("/api/bandwagon/locations")
async def api_bandwagon_locations(username: str = Depends(verify_auth)):
    errors = []
    current_location = ""
    for action in ("migrate/getLocations", "getAvailableLocations"):
        try:
            data = await run_blocking(bandwagon_request, action)
            if data.get("error"):
                errors.append(str(data.get("message") or data.get("error")))
                continue
            locations = normalize_bandwagon_locations(data)
            if locations:
                service_info = await run_blocking(fetch_bandwagon_info)
                if service_info and not service_info.get("error"):
                    current_location = match_current_location_label(service_info.get("node"), locations)
                return {"ok": True, "locations": locations, "current_location": current_location}
        except Exception as exc:
            errors.append(str(exc))
    raise HTTPException(status_code=400, detail="無法讀取可切換機房：" + "；".join(errors))

@app.post("/api/bandwagon/migrate")
async def api_bandwagon_migrate(payload: dict, username: str = Depends(verify_auth)):
    location = str(payload.get("location", "")).strip()
    location_label = str(payload.get("location_label", "")).strip() or location
    if not location:
        raise HTTPException(status_code=400, detail="請先選擇機房")

    last_known_ip = SYSTEM_INFO_CACHE.get("public_ip")
    old_ip, _ = await run_blocking(get_cached_public_ip_info, True)
    if old_ip == "Unknown" and last_known_ip and last_known_ip != "Unknown":
        old_ip = last_known_ip
    if old_ip == "Unknown":
        # Without the current IP we cannot tell when the migration finished; refuse instead of guessing.
        raise HTTPException(status_code=503, detail="暫時無法取得目前公網 IP，請稍後再試")
    set_meta_value("migration_active", "1")
    set_meta_value("migration_started_at", timeutil.now().isoformat())
    set_meta_value("migration_target", location_label)
    set_meta_value("migration_old_ip", old_ip)
    set_meta_value("migration_new_ip", "")
    set_meta_value("migration_domain", CLOUDFLARE_RECORD_NAME)
    set_meta_value("migration_notified_ip", "0")
    set_meta_value("migration_notified_dns", "0")

    errors = []
    for action in ("migrate/start", "migrateToLocation"):
        try:
            data = await run_blocking(bandwagon_request, action, {"location": location})
            if data.get("error"):
                errors.append(str(data.get("message") or data.get("error")))
                continue
            await run_blocking(send_telegram_message_if_ready, migration_telegram_message("服務器機房切換已提交", [
                f"目標機房：<b>{escape_html(location_label)}</b>",
                f"當前 IP：<code>{escape_html(old_ip)}</code>",
                "面板會持續檢測新 IP、DDNS 與域名解析狀態。",
            ]))
            return {"ok": True, "result": data}
        except Exception as exc:
            errors.append(str(exc))
    set_meta_value("migration_active", "0")
    raise HTTPException(status_code=400, detail="切換機房請求失敗：" + "；".join(errors))

@app.get("/api/bandwagon/migrate-status")
async def api_bandwagon_migrate_status(username: str = Depends(verify_auth)):
    return await run_blocking(build_migration_status)

@app.post("/api/traffic/reset")
async def api_reset_traffic(username: str = Depends(verify_auth)):
    collector_instance.flush_to_db()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM hourly_traffic')
    conn.commit()
    conn.close()
    reset_time = timeutil.now().isoformat()
    set_meta_value("install_time", reset_time)
    return {"ok": True, "total_since": format_meta_time(reset_time)}

@app.post("/api/reset-day")
async def api_update_reset_day(payload: dict, username: str = Depends(verify_auth)):
    global MONTH_RESET_DAY

    try:
        new_day = int(payload.get("month_reset_day", 1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid reset day")

    if new_day < 1 or new_day > 31:
        raise HTTPException(status_code=400, detail="Reset day must be between 1 and 31")

    update_env_values({"MONTH_RESET_DAY": new_day})
    MONTH_RESET_DAY = new_day
    return {"ok": True, "month_reset_day": MONTH_RESET_DAY}

@app.get("/api/traffic-quota")
async def api_get_traffic_quota(username: str = Depends(verify_auth)):
    bw = await run_blocking(fetch_bandwagon_info_cached)
    return {
        "traffic_quota_bytes": TRAFFIC_QUOTA_BYTES,
        "bandwagon_available": bool(bw and not bw.get("error") and bw.get("limit")),
        "bandwagon_error": bw.get("error") if bw and bw.get("error") else "",
        "vps_expiry_date": VPS_EXPIRY_DATE,
        "vps_expiry_notify": VPS_EXPIRY_NOTIFY == "1",
        "traffic_alert_enabled": TRAFFIC_ALERT_ENABLED == "1",
        "traffic_alert_thresholds": ",".join(str(t) for t in parse_alert_thresholds(TRAFFIC_ALERT_THRESHOLDS)),
        "telegram_ready": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
    }

@app.post("/api/traffic-quota")
async def api_update_traffic_quota(payload: dict, username: str = Depends(verify_auth)):
    global TRAFFIC_QUOTA_BYTES, TRAFFIC_ALERT_ENABLED, TRAFFIC_ALERT_THRESHOLDS, VPS_EXPIRY_DATE, VPS_EXPIRY_NOTIFY

    try:
        quota_bytes = int(payload.get("traffic_quota_bytes", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid traffic quota")
    if quota_bytes < 0:
        raise HTTPException(status_code=400, detail="Traffic quota cannot be negative")

    updates = {"TRAFFIC_QUOTA_BYTES": quota_bytes}
    if "traffic_alert_enabled" in payload:
        TRAFFIC_ALERT_ENABLED = "1" if payload.get("traffic_alert_enabled") else "0"
        updates["TRAFFIC_ALERT_ENABLED"] = TRAFFIC_ALERT_ENABLED
    if "traffic_alert_thresholds" in payload:
        thresholds = parse_alert_thresholds(payload.get("traffic_alert_thresholds"))
        if not thresholds and TRAFFIC_ALERT_ENABLED == "1":
            raise HTTPException(status_code=400, detail="Alert thresholds must be numbers between 1 and 200")
        TRAFFIC_ALERT_THRESHOLDS = ",".join(str(t) for t in thresholds) or "80,90,100"
        updates["TRAFFIC_ALERT_THRESHOLDS"] = TRAFFIC_ALERT_THRESHOLDS

    if "vps_expiry_date" in payload:
        raw = str(payload.get("vps_expiry_date") or "").strip()
        expiry = parse_expiry_date(raw)
        if raw and not expiry:
            raise HTTPException(status_code=400, detail="Invalid expiry date, use YYYY-MM-DD")
        VPS_EXPIRY_DATE = expiry.isoformat() if expiry else ""
        updates["VPS_EXPIRY_DATE"] = VPS_EXPIRY_DATE
    if "vps_expiry_notify" in payload:
        VPS_EXPIRY_NOTIFY = "1" if payload.get("vps_expiry_notify") else "0"
        updates["VPS_EXPIRY_NOTIFY"] = VPS_EXPIRY_NOTIFY

    TRAFFIC_QUOTA_BYTES = quota_bytes
    update_env_values(updates)
    summary = await run_blocking(get_traffic_summary, collector_instance.snapshot_pending())
    return {
        "ok": True,
        "traffic_quota_bytes": TRAFFIC_QUOTA_BYTES,
        "traffic_quota": summary.get("traffic_quota", {}),
        "vps_expiry": summary.get("vps_expiry"),
    }

@app.get("/api/realtime")
async def api_realtime(username: str = Depends(verify_auth)):
    return {
        "rx_speed": collector_instance.current_rx_speed,
        "tx_speed": collector_instance.current_tx_speed
    }

@app.get("/api/ping")
async def api_ping(username: str = Depends(verify_auth)):
    return {"ok": True, "server_time": timeutil.now().isoformat(), "version": __version__}

def get_load_average():
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except (AttributeError, OSError):
        return None

def build_system_info(client_ip):
    """Blocking: public IP lookups, ip-api calls and a 0.1s CPU sample. Run via run_blocking."""
    ip, ip_info = get_cached_public_ip_info()
    client_ip_info = lookup_ip_info(client_ip)

    uptime = "Unknown"
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_seconds = float(f.readline().split()[0])
            uptime = str(timedelta(seconds=int(uptime_seconds)))
    except Exception:
        pass

    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')

    return {
        "public_ip": ip,
        "client_ip": client_ip,
        "client_ip_location": format_ip_location(client_ip_info),
        "client_ip_lat": client_ip_info.get("lat"),
        "client_ip_lon": client_ip_info.get("lon"),
        "client_ip_timezone": client_ip_info.get("timezone") or "Unknown",
        "client_ip_isp": client_ip_info.get("isp") or "Unknown",
        "client_ip_org": client_ip_info.get("org") or "Unknown",
        "client_ip_access_type": classify_access_network(client_ip_info),
        "ip_location": format_ip_location(ip_info),
        "ip_isp": ip_info.get("isp") or "Unknown",
        "ip_org": ip_info.get("org") or "Unknown",
        "ip_as": ip_info.get("as") or "Unknown",
        "ip_type": classify_ip_info(ip_info),
        "hostname": platform.node(),
        "uptime": uptime,
        "cpu_count": psutil.cpu_count(logical=True),
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "load_avg": get_load_average(),
        "memory_total": mem.total,
        "memory_used": mem.used,
        "memory_percent": mem.percent,
        "disk_total": disk.total,
        "disk_used": disk.used,
        "disk_percent": disk.percent,
        "os_version": get_os_pretty_name(),
        "kernel_release": platform.release(),
        "kernel_version": platform.version(),
    }

@app.get("/api/system")
async def api_system(request: Request, username: str = Depends(verify_auth)):
    info = await run_blocking(build_system_info, get_client_ip(request))
    info["interface"] = collector_instance.interface
    info["interface_status"] = collector_instance.interface_status
    return info

@app.get("/api/summary")
async def api_summary(username: str = Depends(verify_auth)):
    return await run_blocking(get_traffic_summary, collector_instance.snapshot_pending())

@app.get("/api/hourly")
async def api_hourly(username: str = Depends(verify_auth)):
    current_hour = timeutil.now().replace(minute=0, second=0, microsecond=0)
    hours = [current_hour - timedelta(hours=offset) for offset in range(23, -1, -1)]
    data_by_hour = {
        (h.year, h.month, h.day, h.hour): {
            "year": h.year, "month": h.month, "day": h.day, "hour": h.hour,
            "rx_bytes": 0, "tx_bytes": 0,
        }
        for h in hours
    }

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT year, month, day, hour, rx_bytes, tx_bytes
        FROM hourly_traffic
        WHERE datetime(printf('%04d-%02d-%02d %02d:00:00', year, month, day, hour)) >= datetime(?)
    ''', (hours[0].strftime('%Y-%m-%d %H:%M:%S'),))
    for row in cursor.fetchall():
        key = (row["year"], row["month"], row["day"], row["hour"])
        if key in data_by_hour:
            data_by_hour[key]["rx_bytes"] += row["rx_bytes"] or 0
            data_by_hour[key]["tx_bytes"] += row["tx_bytes"] or 0
    conn.close()

    for key, traffic in collector_instance.pending_buckets.items():
        if key in data_by_hour:
            data_by_hour[key]["rx_bytes"] += traffic["rx"]
            data_by_hour[key]["tx_bytes"] += traffic["tx"]

    return [data_by_hour[(h.year, h.month, h.day, h.hour)] for h in hours]

@app.get("/api/daily")
async def api_daily(username: str = Depends(verify_auth)):
    today = timeutil.now().date()
    days = [today - timedelta(days=offset) for offset in range(29, -1, -1)]
    data_by_day = {
        (day.year, day.month, day.day): {
            "year": day.year,
            "month": day.month,
            "day": day.day,
            "rx_bytes": 0,
            "tx_bytes": 0,
        }
        for day in days
    }

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT year, month, day, SUM(rx_bytes) as rx_bytes, SUM(tx_bytes) as tx_bytes 
        FROM hourly_traffic
        WHERE date(printf('%04d-%02d-%02d', year, month, day)) >= date(?)
        GROUP BY year, month, day 
        ORDER BY year, month, day
    ''', (days[0].isoformat(),))
    for row in cursor.fetchall():
        key = (row["year"], row["month"], row["day"])
        if key in data_by_day:
            data_by_day[key]["rx_bytes"] = row["rx_bytes"] or 0
            data_by_day[key]["tx_bytes"] = row["tx_bytes"] or 0
    conn.close()

    for (y, m, d, _h), traffic in collector_instance.pending_buckets.items():
        key = (y, m, d)
        if key in data_by_day:
            data_by_day[key]["rx_bytes"] += traffic["rx"]
            data_by_day[key]["tx_bytes"] += traffic["tx"]

    return [data_by_day[(day.year, day.month, day.day)] for day in days]

@app.get("/api/monthly")
async def api_monthly(username: str = Depends(verify_auth)):
    months = recent_months(12)
    data_by_month = {
        (year, month): {
            "year": year,
            "month": month,
            "rx_bytes": 0,
            "tx_bytes": 0,
        }
        for year, month in months
    }
    start_year, start_month = months[0]

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT year, month, SUM(rx_bytes) as rx_bytes, SUM(tx_bytes) as tx_bytes 
        FROM hourly_traffic
        WHERE (year > ? OR (year = ? AND month >= ?))
        GROUP BY year, month 
        ORDER BY year, month
    ''', (start_year, start_year, start_month))
    for row in cursor.fetchall():
        key = (row["year"], row["month"])
        if key in data_by_month:
            data_by_month[key]["rx_bytes"] = row["rx_bytes"] or 0
            data_by_month[key]["tx_bytes"] = row["tx_bytes"] or 0
    conn.close()

    for (y, m, _d, _h), traffic in collector_instance.pending_buckets.items():
        key = (y, m)
        if key in data_by_month:
            data_by_month[key]["rx_bytes"] += traffic["rx"]
            data_by_month[key]["tx_bytes"] += traffic["tx"]

    return [data_by_month[key] for key in months]

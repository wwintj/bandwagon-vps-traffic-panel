"""Outbound HTTP, public IP discovery and IP geolocation."""
import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from app import util


SYSTEM_INFO_CACHE = {"public_ip": None, "ip_info": None, "updated_at": 0}


IP_LOOKUP_CACHE = {}


SYSTEM_INFO_CACHE_TTL = 600


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
    return util.to_traditional_text(" / ".join(filter(None, [
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


def resolve_domain_ips(domain):
    if not domain:
        return []
    try:
        return sorted(set(socket.gethostbyname_ex(domain)[2]))
    except Exception:
        return []

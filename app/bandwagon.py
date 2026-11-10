"""BandwagonHost / KiwiVM API: service info, locations and migration tracking."""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from app import config, ddns, net, telegram, util
from app.database import get_meta_value, set_meta_value


BANDWAGON_INFO_CACHE = {"data": None, "updated_at": 0}


BANDWAGON_INFO_CACHE_TTL = 300


def fetch_bandwagon_info():
    if not config.BANDWAGON_VEID or not config.BANDWAGON_API_KEY:
        return None
    params = urllib.parse.urlencode({"veid": config.BANDWAGON_VEID, "api_key": config.BANDWAGON_API_KEY})
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
    except Exception:
        return {"error": "暫時無法連接 Bandwagon 官方 API，請稍後再試或檢查 VEID / API Key。"}
    if data.get("error"):
        return {"error": data.get("message") or data.get("error")}
    multiplier = int(data.get("monthly_data_multiplier") or 1)
    used = int(data.get("data_counter") or 0) * multiplier
    limit = int(data.get("plan_monthly_data") or 0) * multiplier
    return {
        "used": used,
        "limit": limit,
        "reset": data.get("data_next_reset") or "",
        "hostname": data.get("hostname") or "",
        "node": data.get("node_location") or data.get("node_alias") or data.get("location") or data.get("datacenter") or "",
        "multiplier": multiplier,
        "suspended": bool(data.get("suspended")),
        "plan_ram": int(data.get("plan_ram") or 0),
        "plan_swap": int(data.get("plan_swap") or 0),
        "plan_disk": int(data.get("plan_disk") or 0),
        "os": data.get("os") or "",
    }


def fetch_bandwagon_info_cached(force=False):
    now = time.time()
    if not force and BANDWAGON_INFO_CACHE["data"] is not None and now - BANDWAGON_INFO_CACHE["updated_at"] < BANDWAGON_INFO_CACHE_TTL:
        return BANDWAGON_INFO_CACHE["data"]
    data = fetch_bandwagon_info()
    BANDWAGON_INFO_CACHE["data"] = data
    BANDWAGON_INFO_CACHE["updated_at"] = now
    return data


def bandwagon_request(action, params=None):
    if not config.BANDWAGON_VEID or not config.BANDWAGON_API_KEY:
        raise ValueError("Bandwagon VEID / API Key is empty")
    query = {"veid": config.BANDWAGON_VEID, "api_key": config.BANDWAGON_API_KEY}
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

    # KiwiVM's migrate/getLocations: {"currentLocation": "USCA_8", "locations": ["USCA_2", ...],
    # "descriptions": {"USCA_2": "US: California, Los Angeles (DC2) ..."}, "dataTransferMultipliers": {...}}
    if isinstance(data, dict) and isinstance(data.get("locations"), list) and isinstance(data.get("descriptions"), dict):
        descriptions = data["descriptions"]
        multipliers = data.get("dataTransferMultipliers") if isinstance(data.get("dataTransferMultipliers"), dict) else {}
        result = []
        for code in data["locations"]:
            code = str(code)
            name = str(descriptions.get(code) or "").strip() or infer_bandwagon_location_label(code)
            if f"[{code}]" not in name:
                name = f"{name} [{code}]"
            try:
                multiplier = float(multipliers.get(code) or 1)
            except (TypeError, ValueError):
                multiplier = 1
            if multiplier > 1:
                name = f"{name}（流量 ×{multiplier:g}）"
            result.append({"code": code, "name": name, "available": True})
        if result:
            return result

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


def migration_meta_snapshot():
    return {
        "active": get_meta_value("migration_active", "0") == "1",
        "started_at": util.format_meta_time(get_meta_value("migration_started_at", "")),
        "target": get_meta_value("migration_target", ""),
        "old_ip": get_meta_value("migration_old_ip", ""),
        "new_ip": get_meta_value("migration_new_ip", ""),
        "domain": get_meta_value("migration_domain", ""),
    }


def build_migration_status():
    meta = migration_meta_snapshot()
    current_ip, _ = net.get_cached_public_ip_info(force=True)
    old_ip = meta["old_ip"]
    new_ip = meta["new_ip"]
    old_ip_known = bool(old_ip) and old_ip != "Unknown"
    ip_changed = bool(old_ip_known and current_ip != "Unknown" and current_ip != old_ip)
    if ip_changed and not new_ip:
        new_ip = current_ip
        set_meta_value("migration_new_ip", new_ip)

    ddns_state = {
        "enabled": config.CLOUDFLARE_DDNS_ENABLED == "1",
        "synced": False,
        "record": config.CLOUDFLARE_RECORD_NAME,
        "proxied": config.CLOUDFLARE_PROXIED == "1",
        "error": "",
    }
    if ip_changed and ddns_state["enabled"]:
        try:
            ddns.sync_cloudflare_ddns(current_ip)
            ddns_state["synced"] = True
        except Exception as exc:
            ddns_state["error"] = str(exc)

    domain_ips = net.resolve_domain_ips(config.CLOUDFLARE_RECORD_NAME) if config.CLOUDFLARE_RECORD_NAME else []
    domain_ok = bool(new_ip and ((ddns_state["proxied"] and ddns_state["synced"]) or new_ip in domain_ips))

    if ip_changed and get_meta_value("migration_notified_ip", "0") != "1":
        telegram.send_telegram_message_if_ready(telegram.migration_telegram_message("服務器機房切換成功", [
            f"目標機房：<b>{util.escape_html(meta['target'] or '未知')}</b>",
            f"舊 IP：<code>{util.escape_html(old_ip)}</code>",
            f"新 IP：<code>{util.escape_html(new_ip)}</code>",
        ]))
        set_meta_value("migration_notified_ip", "1")

    if domain_ok and get_meta_value("migration_notified_dns", "0") != "1":
        dns_line = "Cloudflare 代理已同步到新 IP。" if ddns_state["proxied"] else f"解析 IP：<code>{util.escape_html(new_ip)}</code>"
        telegram.send_telegram_message_if_ready(telegram.migration_telegram_message("域名解析已同步", [
            f"域名：<code>{util.escape_html(config.CLOUDFLARE_RECORD_NAME)}</code>",
            dns_line,
            "現在可以嘗試使用域名訪問面板。",
        ]))
        set_meta_value("migration_notified_dns", "1")
        set_meta_value("migration_active", "0")
    elif ip_changed and not ddns_state["enabled"]:
        set_meta_value("migration_active", "0")

    return {
        **meta,
        "active": get_meta_value("migration_active", "0") == "1",
        "current_ip": current_ip,
        "new_ip": new_ip,
        "ip_changed": ip_changed,
        "ddns": ddns_state,
        "domain_ips": domain_ips,
        "domain_ok": domain_ok,
    }


def bandwagon_plan_summary():
    bw = fetch_bandwagon_info_cached()
    if not bw or bw.get("error"):
        return None
    return {key: bw.get(key) for key in ("suspended", "plan_ram", "plan_swap", "plan_disk", "os", "multiplier", "node")}

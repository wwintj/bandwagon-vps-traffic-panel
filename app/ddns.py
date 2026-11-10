"""Cloudflare DDNS: zone lookup, record sync and the background updater."""
import asyncio
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from app import config, net, telegram, timeutil, util
from app.database import get_meta_value, set_meta_value


def cloudflare_headers():
    return {
        "Authorization": f"Bearer {config.CLOUDFLARE_API_TOKEN}",
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
    zone = config.CLOUDFLARE_ZONE_ID.strip()
    if ZONE_ID_PATTERN.match(zone.lower()):
        return zone
    record = config.CLOUDFLARE_RECORD_NAME.strip().rstrip(".").lower()
    cache_key = (zone.lower(), record)
    if cache_key in CLOUDFLARE_ZONE_CACHE:
        return CLOUDFLARE_ZONE_CACHE[cache_key]

    if zone:
        candidates = [zone.rstrip(".").lower()]
    else:
        labels = record.split(".")
        candidates = [".".join(labels[i:]) for i in range(len(labels) - 1)]
    for name in candidates:
        response = net.request_json(f"{CLOUDFLARE_API}/zones?{urllib.parse.urlencode({'name': name})}", headers=cloudflare_headers())
        if not response.get("success"):
            raise ValueError("Cloudflare 查詢 Zone 失敗：" + cloudflare_error_text(response, "未知錯誤"))
        result = response.get("result") or []
        if result:
            CLOUDFLARE_ZONE_CACHE[cache_key] = result[0]["id"]
            return result[0]["id"]
    raise ValueError(f"在這個 Cloudflare Token 可存取的網域中找不到 {zone or record}")


def sync_cloudflare_ddns(public_ip=None):
    if not config.CLOUDFLARE_API_TOKEN or not config.CLOUDFLARE_RECORD_NAME:
        raise ValueError("請填寫 Cloudflare API Token 與完整域名")

    if not public_ip:
        public_ip, _ = net.get_cached_public_ip_info(force=True)
    if not public_ip or public_ip == "Unknown":
        raise ValueError("無法取得目前 VPS 公網 IP")

    zone_id = resolve_cloudflare_zone_id()
    base_url = f"{CLOUDFLARE_API}/zones/{urllib.parse.quote(zone_id)}/dns_records"
    query = urllib.parse.urlencode({"type": "A", "name": config.CLOUDFLARE_RECORD_NAME})
    records = net.request_json(f"{base_url}?{query}", headers=cloudflare_headers())
    if not records.get("success"):
        raise ValueError("Cloudflare 查詢 DNS 記錄失敗：" + cloudflare_error_text(records, "未知錯誤"))

    proxied = config.CLOUDFLARE_PROXIED == "1"
    # Proxied records always use automatic TTL on Cloudflare.
    ttl = 1 if proxied else config.CLOUDFLARE_TTL
    body = {
        "type": "A",
        "name": config.CLOUDFLARE_RECORD_NAME,
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
            result = net.request_json(f"{base_url}/{current['id']}", method="PATCH", payload=body, headers=cloudflare_headers())
            action = "updated"
        else:
            result = net.request_json(base_url, method="POST", payload=body, headers=cloudflare_headers())
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
        "record": config.CLOUDFLARE_RECORD_NAME,
    }


def ddns_notice(title, lines):
    if config.CLOUDFLARE_DDNS_NOTIFY != "1":
        return False
    return telegram.send_telegram_notice(telegram.migration_telegram_message(title, lines))


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
    public_ip, _ = net.get_cached_public_ip_info(force=True)
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
                f"域名：<code>{util.escape_html(config.CLOUDFLARE_RECORD_NAME)}</code>",
                f"IP：<code>{util.escape_html(public_ip)}</code>",
                f"錯誤：{util.escape_html(error)}",
                f"面板會每 {config.CLOUDFLARE_DDNS_INTERVAL} 分鐘自動重試。",
            ])
        util.logger.warning("Cloudflare DDNS update failed: %s", error)
        return {"ok": False, "error": error}

    if last_error:
        set_meta_value("cloudflare_ddns_last_error", "")
    # During a Bandwagon migration the migration flow sends its own messages.
    migrating = get_meta_value("migration_active", "0") == "1"
    if result["action"] in ("updated", "created") and not migrating:
        ddns_notice("DDNS 已更新", [
            f"域名：<code>{util.escape_html(config.CLOUDFLARE_RECORD_NAME)}</code>",
            f"舊 IP：<code>{util.escape_html(result.get('previous_ip') or last_ip or '無')}</code>",
            f"新 IP：<code>{util.escape_html(public_ip)}</code>",
        ])
    elif last_error and not migrating:
        ddns_notice("DDNS 已恢復正常", [
            f"域名：<code>{util.escape_html(config.CLOUDFLARE_RECORD_NAME)}</code>",
            f"IP：<code>{util.escape_html(public_ip)}</code>",
        ])
    return result


async def ddns_background_updater():
    # First check shortly after start (e.g. right after a reboot or migration), then every interval.
    await asyncio.sleep(20)
    while True:
        if config.CLOUDFLARE_DDNS_ENABLED == "1":
            try:
                await util.run_blocking(ddns_check_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                util.logger.warning("Cloudflare DDNS check failed: %s", exc)
        await asyncio.sleep(config.CLOUDFLARE_DDNS_INTERVAL * 60)

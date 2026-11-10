"""The served SSL certificate, its expiry reminder and the root certificate helper channel."""
import asyncio
import json
import os
import secrets
import tempfile
import time

from fastapi import HTTPException

from app import config, sslinfo, telegram, util
from app.database import get_meta_value, set_meta_value


SSL_STATUS_CACHE = {"data": None, "updated_at": 0}


SSL_STATUS_TTL = 600


def get_ssl_status(force=False):
    """Certificate the panel is serving right now, as a browser would see it."""
    now = time.time()
    if not force and SSL_STATUS_CACHE["data"] is not None and now - SSL_STATUS_CACHE["updated_at"] < SSL_STATUS_TTL:
        return SSL_STATUS_CACHE["data"]
    status = {
        "enabled": config.HTTPS_MODE in ("nginx", "uvicorn"),
        "mode": config.HTTPS_MODE,
        "domain": config.PANEL_DOMAIN,
        "port": None,
        "cert_file": config.SSL_CERTFILE,
    }
    if status["enabled"]:
        if config.HTTPS_MODE == "nginx":
            status["port"] = int(config.HTTPS_PORT or 443)
            target = (config.PANEL_DOMAIN or None)
        else:
            status["port"] = int(config.PORT)
            target = None  # direct HTTPS is reached by IP; only the chain can be checked
        status.update(sslinfo.inspect_tls("127.0.0.1", status["port"], target))
        status["renewal_watch"] = sslinfo.cert_watch_active()
    SSL_STATUS_CACHE["data"] = status
    SSL_STATUS_CACHE["updated_at"] = now
    return status


def check_ssl_expiry_reminder(now=None, status=None):
    """Telegram reminder when the served certificate is close to expiry, which means automatic
    renewal is not working (Let's Encrypt renews ~30 days ahead). 14 and 7 days out, then daily."""
    if config.SSL_EXPIRY_NOTIFY != "1":
        return None
    now = now or util.configured_now()
    if (now.hour, now.minute) < (config.TELEGRAM_PUSH_HOUR, config.TELEGRAM_PUSH_MINUTE):
        return None
    status = status or get_ssl_status(force=True)
    days = status.get("days_left") if status.get("enabled") else None
    if days is None or days > 14:
        return None
    stage = "14" if days > 7 else "7" if days > 3 else f"day-{now.date().isoformat()}"

    key = status.get("not_after", "")
    if get_meta_value("ssl_reminder_cert", "") != key:
        set_meta_value("ssl_reminder_cert", key)
        set_meta_value("ssl_reminder_sent", "")
    sent = [item for item in get_meta_value("ssl_reminder_sent", "").split(",") if item]
    if stage in sent:
        return None

    title = f"SSL 證書已過期 {-days} 天" if days < 0 else f"SSL 證書剩 {days} 天到期"
    lines = [
        f"網域：<code>{util.escape_html(status.get('domain') or status.get('subject') or '未知')}</code>",
        f"到期日：<b>{util.escape_html(status.get('not_after', ''))}</b>",
        f"簽發者：{util.escape_html(status.get('issuer') or '未知')}",
        "Let's Encrypt 會在到期前約 30 天自動續簽，收到這則提醒代表自動續簽可能失敗。",
        "請檢查 acme.sh / certbot / x-ui 的續簽紀錄；續簽成功後面板會自動套用新證書。",
    ]
    if not telegram.send_telegram_notice(telegram.migration_telegram_message(title, lines)):
        return None
    set_meta_value("ssl_reminder_sent", ",".join((sent + [stage])[-10:]))
    return stage


CERTCTL_PICKUP_SECONDS = 6


CERTCTL_TIMEOUT_SECONDS = 180


def _read_certctl_result():
    try:
        with open(os.path.join(config.CERTCTL_DIR, "result.json"), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def read_certctl_result(request_id):
    result = _read_certctl_result()
    if not result or result.get("id") != request_id:
        return {"id": request_id, "state": "unknown"}
    return result


async def certctl_request(action, wait_until="done", **fields):
    """Hand a request to the root certificate helper and wait for its answer.

    wait_until="running" returns as soon as the helper has picked the request up. Direct-HTTPS
    switching needs that: the helper restarts this very process, and an open request would hold
    up the shutdown (and with it the switch) until it timed out.

    The helper only accepts a candidate id from its own fresh scan, so nothing sent here can make
    root read or write an arbitrary path."""
    request_dir = os.path.join(config.CERTCTL_DIR, "requests")
    if not os.path.isdir(request_dir) or not os.access(request_dir, os.W_OK):
        raise HTTPException(status_code=503, detail="證書切換功能尚未啟用，請在 VPS 執行 vps-panel update")
    request_id = secrets.token_hex(8)
    fd, tmp_path = tempfile.mkstemp(dir=request_dir, prefix=".req-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"id": request_id, "action": action, **fields}, handle)
    os.replace(tmp_path, os.path.join(request_dir, "request.json"))

    started = time.time()
    picked_up = False
    while time.time() - started < CERTCTL_TIMEOUT_SECONDS:
        await asyncio.sleep(0.5)
        result = _read_certctl_result()
        if result and result.get("id") == request_id:
            picked_up = True
            if result.get("state") == "done" or wait_until == "running":
                return result
        if not picked_up and time.time() - started > CERTCTL_PICKUP_SECONDS:
            raise HTTPException(status_code=503, detail="證書切換服務沒有回應，請在 VPS 執行 vps-panel update 後再試")
    raise HTTPException(status_code=504, detail="證書切換逾時，請稍後再試")

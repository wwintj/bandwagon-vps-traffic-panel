"""Panel, Telegram, DDNS and Bandwagon settings."""

from fastapi import APIRouter, Depends, HTTPException, Request

from app import auth, bandwagon, config, ddns, envfile, passwords, sessions, telegram, timeutil, util
from app.collector import collector_instance
from app.database import get_meta_value, set_meta_value

router = APIRouter()


@router.get("/api/settings")
async def api_get_settings(username: str = Depends(auth.verify_auth)):
    return {
        "auth_username": config.AUTH_USERNAME,
        "panel_title": config.PANEL_TITLE,
        "panel_subtitle": config.PANEL_SUBTITLE,
    }


@router.post("/api/settings")
async def api_update_settings(payload: dict, request: Request, username: str = Depends(auth.verify_auth)):

    new_username = str(payload.get("auth_username", "")).strip()
    new_password = str(payload.get("auth_password", "")).strip()
    new_title = util.to_traditional_text(payload.get("panel_title", "")).strip()
    new_subtitle = util.to_traditional_text(payload.get("panel_subtitle", "")).strip()

    if not util.is_safe_auth_value(new_username):
        raise HTTPException(status_code=400, detail="Invalid username")
    if new_password and not util.is_safe_auth_value(new_password):
        raise HTTPException(status_code=400, detail="Invalid password")
    if not new_title or not new_subtitle:
        raise HTTPException(status_code=400, detail="Panel title and subtitle cannot be empty")

    updates = {
        "AUTH_USERNAME": new_username,
        "PANEL_TITLE": new_title,
        "PANEL_SUBTITLE": new_subtitle,
    }
    if new_password:
        new_hash = passwords.hash_password(new_password)
        updates["AUTH_PASSWORD_HASH"] = new_hash
        updates["AUTH_PASSWORD"] = None

    envfile.update_env_values(updates)
    config.AUTH_USERNAME = new_username
    if new_password:
        config.AUTH_PASSWORD_HASH, config.AUTH_PASSWORD = new_hash, ""
        auth.VERIFIED_CREDENTIALS.clear()
        # New password: sign out every other browser, keep this one logged in.
        sessions.delete_all_except(request.cookies.get(auth.SESSION_COOKIE) or None)
    config.PANEL_TITLE = new_title
    config.PANEL_SUBTITLE = new_subtitle

    return {"ok": True, "panel_title": config.PANEL_TITLE, "panel_subtitle": config.PANEL_SUBTITLE}


@router.get("/api/telegram-settings")
async def api_get_telegram_settings(username: str = Depends(auth.verify_auth)):
    return {
        "telegram_enabled": config.TELEGRAM_ENABLED == "1",
        "telegram_chat_id": config.TELEGRAM_CHAT_ID,
        "telegram_bot_token": envfile.mask_secret(config.TELEGRAM_BOT_TOKEN),
        "telegram_push_hour": config.TELEGRAM_PUSH_HOUR,
        "telegram_push_minute": config.TELEGRAM_PUSH_MINUTE,
        "telegram_timezone_offset": config.TELEGRAM_TIMEZONE_OFFSET,
        "telegram_timezone_label": config.TELEGRAM_TIMEZONE_LABEL,
        "bandwagon_veid": config.BANDWAGON_VEID,
        "bandwagon_api_key": envfile.mask_secret(config.BANDWAGON_API_KEY),
    }


@router.post("/api/telegram-settings")
async def api_update_telegram_settings(payload: dict, username: str = Depends(auth.verify_auth)):

    config.TELEGRAM_ENABLED = "1" if payload.get("telegram_enabled") else "0"
    config.TELEGRAM_BOT_TOKEN = envfile.resolve_secret(payload.get("telegram_bot_token", ""), config.TELEGRAM_BOT_TOKEN)
    config.TELEGRAM_CHAT_ID = str(payload.get("telegram_chat_id", "")).strip()
    try:
        config.TELEGRAM_PUSH_HOUR = int(payload.get("telegram_push_hour", 20))
    except (TypeError, ValueError):
        config.TELEGRAM_PUSH_HOUR = 20
    config.TELEGRAM_PUSH_HOUR = max(0, min(23, config.TELEGRAM_PUSH_HOUR))
    try:
        config.TELEGRAM_PUSH_MINUTE = int(payload.get("telegram_push_minute", 0))
    except (TypeError, ValueError):
        config.TELEGRAM_PUSH_MINUTE = 0
    config.TELEGRAM_PUSH_MINUTE = max(0, min(59, config.TELEGRAM_PUSH_MINUTE))
    try:
        config.TELEGRAM_TIMEZONE_OFFSET = int(payload.get("telegram_timezone_offset", 8))
    except (TypeError, ValueError):
        config.TELEGRAM_TIMEZONE_OFFSET = 8
    config.TELEGRAM_TIMEZONE_OFFSET = max(-12, min(14, config.TELEGRAM_TIMEZONE_OFFSET))
    timeutil.set_offset(config.TELEGRAM_TIMEZONE_OFFSET)
    config.TELEGRAM_TIMEZONE_LABEL = str(payload.get("telegram_timezone_label", "中國時間")).strip() or "自訂時區"
    config.BANDWAGON_VEID = str(payload.get("bandwagon_veid", "")).strip()
    config.BANDWAGON_API_KEY = envfile.resolve_secret(payload.get("bandwagon_api_key", ""), config.BANDWAGON_API_KEY)
    bandwagon.BANDWAGON_INFO_CACHE["data"] = None
    bandwagon.BANDWAGON_INFO_CACHE["updated_at"] = 0

    envfile.update_env_values({
        "TELEGRAM_ENABLED": config.TELEGRAM_ENABLED,
        "TELEGRAM_BOT_TOKEN": config.TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": config.TELEGRAM_CHAT_ID,
        "TELEGRAM_PUSH_HOUR": config.TELEGRAM_PUSH_HOUR,
        "TELEGRAM_PUSH_MINUTE": config.TELEGRAM_PUSH_MINUTE,
        "TELEGRAM_TIMEZONE_OFFSET": config.TELEGRAM_TIMEZONE_OFFSET,
        "TELEGRAM_TIMEZONE_LABEL": config.TELEGRAM_TIMEZONE_LABEL,
        "BANDWAGON_VEID": config.BANDWAGON_VEID,
        "BANDWAGON_API_KEY": config.BANDWAGON_API_KEY,
    })
    telegram.mark_today_sent_if_past_push_time()
    return {"ok": True}


@router.post("/api/telegram-test")
async def api_telegram_test(username: str = Depends(auth.verify_auth)):
    try:
        await util.run_blocking(telegram.send_daily_report, collector_instance.snapshot_pending())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@router.get("/api/ddns-settings")
async def api_get_ddns_settings(username: str = Depends(auth.verify_auth)):
    return {
        "cloudflare_ddns_enabled": config.CLOUDFLARE_DDNS_ENABLED == "1",
        "cloudflare_zone_id": config.CLOUDFLARE_ZONE_ID,
        "cloudflare_record_name": config.CLOUDFLARE_RECORD_NAME,
        "cloudflare_api_token": envfile.mask_secret(config.CLOUDFLARE_API_TOKEN),
        "cloudflare_proxied": config.CLOUDFLARE_PROXIED == "1",
        "cloudflare_ddns_interval": config.CLOUDFLARE_DDNS_INTERVAL,
        "cloudflare_ttl": config.CLOUDFLARE_TTL,
        "cloudflare_ddns_notify": config.CLOUDFLARE_DDNS_NOTIFY == "1",
        "telegram_ready": bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID),
        "last_ip": get_meta_value("cloudflare_ddns_last_ip", ""),
        "last_sync": util.format_meta_time(get_meta_value("cloudflare_ddns_last_sync", "")),
        "last_check": util.format_meta_time(get_meta_value("cloudflare_ddns_last_check", "")),
        "last_error": get_meta_value("cloudflare_ddns_last_error", ""),
    }


@router.post("/api/ddns-settings")
async def api_update_ddns_settings(payload: dict, username: str = Depends(auth.verify_auth)):

    config.CLOUDFLARE_DDNS_ENABLED = "1" if payload.get("cloudflare_ddns_enabled") else "0"
    config.CLOUDFLARE_API_TOKEN = envfile.resolve_secret(payload.get("cloudflare_api_token", ""), config.CLOUDFLARE_API_TOKEN)
    config.CLOUDFLARE_ZONE_ID = str(payload.get("cloudflare_zone_id", "")).strip()
    config.CLOUDFLARE_RECORD_NAME = str(payload.get("cloudflare_record_name", "")).strip()
    config.CLOUDFLARE_PROXIED = "1" if payload.get("cloudflare_proxied") else "0"
    try:
        config.CLOUDFLARE_DDNS_INTERVAL = max(1, min(60, int(payload.get("cloudflare_ddns_interval", config.CLOUDFLARE_DDNS_INTERVAL))))
        ttl = int(payload.get("cloudflare_ttl", config.CLOUDFLARE_TTL))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid check interval or TTL")
    config.CLOUDFLARE_TTL = 1 if ttl < 60 else min(86400, ttl)
    config.CLOUDFLARE_DDNS_NOTIFY = "1" if payload.get("cloudflare_ddns_notify", True) else "0"
    ddns.CLOUDFLARE_ZONE_CACHE.clear()
    # Settings changed: make the next check talk to Cloudflare instead of trusting the last result.
    set_meta_value("cloudflare_ddns_last_sync", "")
    envfile.update_env_values({
        "CLOUDFLARE_DDNS_ENABLED": config.CLOUDFLARE_DDNS_ENABLED,
        "CLOUDFLARE_API_TOKEN": config.CLOUDFLARE_API_TOKEN,
        "CLOUDFLARE_ZONE_ID": config.CLOUDFLARE_ZONE_ID,
        "CLOUDFLARE_RECORD_NAME": config.CLOUDFLARE_RECORD_NAME,
        "CLOUDFLARE_PROXIED": config.CLOUDFLARE_PROXIED,
        "CLOUDFLARE_DDNS_INTERVAL": config.CLOUDFLARE_DDNS_INTERVAL,
        "CLOUDFLARE_TTL": config.CLOUDFLARE_TTL,
        "CLOUDFLARE_DDNS_NOTIFY": config.CLOUDFLARE_DDNS_NOTIFY,
    })
    return {"ok": True}


@router.post("/api/ddns-sync")
async def api_ddns_sync(username: str = Depends(auth.verify_auth)):
    try:
        result = await util.run_blocking(ddns.sync_cloudflare_ddns)
    except Exception as exc:
        set_meta_value("cloudflare_ddns_last_error", str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    set_meta_value("cloudflare_ddns_last_error", "")
    return result


@router.get("/api/bandwagon-settings")
async def api_get_bandwagon_settings(username: str = Depends(auth.verify_auth)):
    return {"bandwagon_veid": config.BANDWAGON_VEID, "bandwagon_api_key": envfile.mask_secret(config.BANDWAGON_API_KEY)}


@router.post("/api/bandwagon-settings")
async def api_update_bandwagon_settings(payload: dict, username: str = Depends(auth.verify_auth)):
    config.BANDWAGON_VEID = str(payload.get("bandwagon_veid", "")).strip()
    config.BANDWAGON_API_KEY = envfile.resolve_secret(payload.get("bandwagon_api_key", ""), config.BANDWAGON_API_KEY)
    bandwagon.BANDWAGON_INFO_CACHE["data"] = None
    bandwagon.BANDWAGON_INFO_CACHE["updated_at"] = 0
    envfile.update_env_values({"BANDWAGON_VEID": config.BANDWAGON_VEID, "BANDWAGON_API_KEY": config.BANDWAGON_API_KEY})
    return {"ok": True}

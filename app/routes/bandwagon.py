"""Bandwagon migration and restart."""

from fastapi import APIRouter, Depends, HTTPException

from app import auth, bandwagon, config, net, telegram, timeutil, util
from app.database import set_meta_value

router = APIRouter()


@router.get("/api/bandwagon/locations")
async def api_bandwagon_locations(username: str = Depends(auth.verify_auth)):
    errors = []
    current_location = ""
    for action in ("migrate/getLocations", "getAvailableLocations"):
        try:
            data = await util.run_blocking(bandwagon.bandwagon_request, action)
            if data.get("error"):
                errors.append(str(data.get("message") or data.get("error")))
                continue
            locations = bandwagon.normalize_bandwagon_locations(data)
            if locations:
                current_code = str(data.get("currentLocation") or "") if isinstance(data, dict) else ""
                descriptions = data.get("descriptions") if isinstance(data, dict) and isinstance(data.get("descriptions"), dict) else {}
                if current_code:
                    # The API names the current location directly; it is not in the migratable list.
                    current_location = str(descriptions.get(current_code) or "").strip() or bandwagon.infer_bandwagon_location_label(current_code)
                    if f"[{current_code}]" not in current_location:
                        current_location = f"{current_location} [{current_code}]"
                else:
                    service_info = await util.run_blocking(bandwagon.fetch_bandwagon_info)
                    if service_info and not service_info.get("error"):
                        current_location = bandwagon.match_current_location_label(service_info.get("node"), locations)
                return {"ok": True, "locations": locations, "current_location": current_location}
        except Exception as exc:
            errors.append(str(exc))
    raise HTTPException(status_code=400, detail="無法讀取可切換機房：" + "；".join(errors))


@router.post("/api/bandwagon/migrate")
async def api_bandwagon_migrate(payload: dict, username: str = Depends(auth.verify_auth)):
    location = str(payload.get("location", "")).strip()
    location_label = str(payload.get("location_label", "")).strip() or location
    if not location:
        raise HTTPException(status_code=400, detail="請先選擇機房")

    last_known_ip = net.SYSTEM_INFO_CACHE.get("public_ip")
    old_ip, _ = await util.run_blocking(net.get_cached_public_ip_info, True)
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
    set_meta_value("migration_domain", config.CLOUDFLARE_RECORD_NAME)
    set_meta_value("migration_notified_ip", "0")
    set_meta_value("migration_notified_dns", "0")

    errors = []
    for action in ("migrate/start", "migrateToLocation"):
        try:
            data = await util.run_blocking(bandwagon.bandwagon_request, action, {"location": location})
            if data.get("error"):
                errors.append(str(data.get("message") or data.get("error")))
                continue
            await util.run_blocking(telegram.send_telegram_message_if_ready, telegram.migration_telegram_message("服務器機房切換已提交", [
                f"目標機房：<b>{util.escape_html(location_label)}</b>",
                f"當前 IP：<code>{util.escape_html(old_ip)}</code>",
                "面板會持續檢測新 IP、DDNS 與域名解析狀態。",
            ]))
            return {"ok": True, "result": data}
        except Exception as exc:
            errors.append(str(exc))
    set_meta_value("migration_active", "0")
    raise HTTPException(status_code=400, detail="切換機房請求失敗：" + "；".join(errors))


@router.post("/api/bandwagon/restart")
async def api_bandwagon_restart(username: str = Depends(auth.verify_auth)):
    try:
        data = await util.run_blocking(bandwagon.bandwagon_request, "restart")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"重啟請求失敗：{exc}")
    if data.get("error"):
        raise HTTPException(status_code=400, detail=f"重啟請求失敗：{data.get('message') or data.get('error')}")
    await util.run_blocking(telegram.send_telegram_notice, telegram.migration_telegram_message("VPS 重啟指令已送出", [
        "由面板發起重啟，約 1 分鐘內恢復。",
    ]))
    return {"ok": True}


@router.get("/api/bandwagon/migrate-status")
async def api_bandwagon_migrate_status(username: str = Depends(auth.verify_auth)):
    return await util.run_blocking(bandwagon.build_migration_status)

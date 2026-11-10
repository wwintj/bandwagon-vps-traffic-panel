"""Traffic, quota, system and interface endpoints."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from app import __version__, auth, bandwagon, config, envfile, system, timeutil, traffic, util
from app.collector import collector_instance
from app.database import get_db, set_meta_value

router = APIRouter()


@router.post("/api/traffic/reset")
async def api_reset_traffic(username: str = Depends(auth.verify_auth)):
    collector_instance.flush_to_db()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM hourly_traffic')
    conn.commit()
    conn.close()
    reset_time = timeutil.now().isoformat()
    set_meta_value("install_time", reset_time)
    return {"ok": True, "total_since": util.format_meta_time(reset_time)}


@router.post("/api/reset-day")
async def api_update_reset_day(payload: dict, username: str = Depends(auth.verify_auth)):

    try:
        new_day = int(payload.get("month_reset_day", 1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid reset day")

    if new_day < 1 or new_day > 31:
        raise HTTPException(status_code=400, detail="Reset day must be between 1 and 31")

    envfile.update_env_values({"MONTH_RESET_DAY": new_day})
    config.MONTH_RESET_DAY = new_day
    return {"ok": True, "month_reset_day": config.MONTH_RESET_DAY}


@router.get("/api/traffic-quota")
async def api_get_traffic_quota(username: str = Depends(auth.verify_auth)):
    bw = await util.run_blocking(bandwagon.fetch_bandwagon_info_cached)
    return {
        "traffic_quota_bytes": config.TRAFFIC_QUOTA_BYTES,
        "bandwagon_available": bool(bw and not bw.get("error") and bw.get("limit")),
        "bandwagon_error": bw.get("error") if bw and bw.get("error") else "",
        "vps_expiry_date": config.VPS_EXPIRY_DATE,
        "vps_expiry_notify": config.VPS_EXPIRY_NOTIFY == "1",
        "traffic_alert_enabled": config.TRAFFIC_ALERT_ENABLED == "1",
        "traffic_alert_thresholds": ",".join(str(t) for t in traffic.parse_alert_thresholds(config.TRAFFIC_ALERT_THRESHOLDS)),
        "telegram_ready": bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID),
    }


@router.post("/api/traffic-quota")
async def api_update_traffic_quota(payload: dict, username: str = Depends(auth.verify_auth)):

    try:
        quota_bytes = int(payload.get("traffic_quota_bytes", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid traffic quota")
    if quota_bytes < 0:
        raise HTTPException(status_code=400, detail="Traffic quota cannot be negative")

    updates = {"TRAFFIC_QUOTA_BYTES": quota_bytes}
    if "traffic_alert_enabled" in payload:
        config.TRAFFIC_ALERT_ENABLED = "1" if payload.get("traffic_alert_enabled") else "0"
        updates["TRAFFIC_ALERT_ENABLED"] = config.TRAFFIC_ALERT_ENABLED
    if "traffic_alert_thresholds" in payload:
        thresholds = traffic.parse_alert_thresholds(payload.get("traffic_alert_thresholds"))
        if not thresholds and config.TRAFFIC_ALERT_ENABLED == "1":
            raise HTTPException(status_code=400, detail="Alert thresholds must be numbers between 1 and 200")
        config.TRAFFIC_ALERT_THRESHOLDS = ",".join(str(t) for t in thresholds) or "80,90,100"
        updates["TRAFFIC_ALERT_THRESHOLDS"] = config.TRAFFIC_ALERT_THRESHOLDS

    if "vps_expiry_date" in payload:
        raw = str(payload.get("vps_expiry_date") or "").strip()
        expiry = traffic.parse_expiry_date(raw)
        if raw and not expiry:
            raise HTTPException(status_code=400, detail="Invalid expiry date, use YYYY-MM-DD")
        config.VPS_EXPIRY_DATE = expiry.isoformat() if expiry else ""
        updates["VPS_EXPIRY_DATE"] = config.VPS_EXPIRY_DATE
    if "vps_expiry_notify" in payload:
        config.VPS_EXPIRY_NOTIFY = "1" if payload.get("vps_expiry_notify") else "0"
        updates["VPS_EXPIRY_NOTIFY"] = config.VPS_EXPIRY_NOTIFY

    config.TRAFFIC_QUOTA_BYTES = quota_bytes
    envfile.update_env_values(updates)
    summary = await util.run_blocking(traffic.get_traffic_summary, collector_instance.snapshot_pending())
    return {
        "ok": True,
        "traffic_quota_bytes": config.TRAFFIC_QUOTA_BYTES,
        "traffic_quota": summary.get("traffic_quota", {}),
        "vps_expiry": summary.get("vps_expiry"),
    }


@router.get("/api/interfaces")
async def api_interfaces(username: str = Depends(auth.verify_auth)):
    return {
        "interfaces": collector_instance.list_interfaces(),
        "pinned": collector_instance.pinned,
        "current": collector_instance.interface,
    }


@router.post("/api/interface")
async def api_set_interface(payload: dict, username: str = Depends(auth.verify_auth)):
    name = str(payload.get("interface", "")).strip()
    if name and name not in {item["name"] for item in collector_instance.list_interfaces()}:
        raise HTTPException(status_code=400, detail="找不到這張網卡")
    envfile.update_env_values({"INTERFACE": name})
    collector_instance.set_interface(name)
    return {"ok": True, "pinned": name, "current": collector_instance.interface}


@router.get("/api/realtime")
async def api_realtime(username: str = Depends(auth.verify_auth)):
    return {
        "rx_speed": collector_instance.current_rx_speed,
        "tx_speed": collector_instance.current_tx_speed
    }


@router.get("/api/ping")
async def api_ping(username: str = Depends(auth.verify_auth)):
    return {"ok": True, "server_time": timeutil.now().isoformat(), "version": __version__}


@router.get("/api/system")
async def api_system(request: Request, username: str = Depends(auth.verify_auth)):
    info = await util.run_blocking(system.build_system_info, auth.get_client_ip(request))
    info["interface"] = collector_instance.interface
    info["interface_status"] = collector_instance.interface_status
    return info


@router.get("/api/summary")
async def api_summary(username: str = Depends(auth.verify_auth)):
    return await util.run_blocking(traffic.get_traffic_summary, collector_instance.snapshot_pending())


@router.get("/api/hourly")
async def api_hourly(username: str = Depends(auth.verify_auth)):
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

    for key, counts in collector_instance.pending_buckets.items():
        if key in data_by_hour:
            data_by_hour[key]["rx_bytes"] += counts["rx"]
            data_by_hour[key]["tx_bytes"] += counts["tx"]

    return [data_by_hour[(h.year, h.month, h.day, h.hour)] for h in hours]


@router.get("/api/daily")
async def api_daily(username: str = Depends(auth.verify_auth)):
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

    for (y, m, d, _h), counts in collector_instance.pending_buckets.items():
        key = (y, m, d)
        if key in data_by_day:
            data_by_day[key]["rx_bytes"] += counts["rx"]
            data_by_day[key]["tx_bytes"] += counts["tx"]

    return [data_by_day[(day.year, day.month, day.day)] for day in days]


@router.get("/api/monthly")
async def api_monthly(username: str = Depends(auth.verify_auth)):
    months = traffic.recent_months(12)
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

    for (y, m, _d, _h), counts in collector_instance.pending_buckets.items():
        key = (y, m)
        if key in data_by_month:
            data_by_month[key]["rx_bytes"] += counts["rx"]
            data_by_month[key]["tx_bytes"] += counts["tx"]

    return [data_by_month[key] for key in months]

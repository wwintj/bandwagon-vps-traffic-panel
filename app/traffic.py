"""Billing cycles, traffic summary, quota, usage alerts and VPS expiry reminders."""
import asyncio
import re
from calendar import monthrange
from datetime import datetime

from app import bandwagon, config, ssl_status, telegram, timeutil, util
from app.collector import collector_instance
from app.database import get_db, get_meta_value, set_meta_value


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
    reset_day = clamp_day(now.year, now.month, config.MONTH_RESET_DAY)
    if now.day >= reset_day:
        return datetime(now.year, now.month, reset_day)

    prev_year, prev_month = previous_month(now.year, now.month)
    prev_reset_day = clamp_day(prev_year, prev_month, config.MONTH_RESET_DAY)
    return datetime(prev_year, prev_month, prev_reset_day)


def get_next_cycle_start(now):
    start = get_current_cycle_start(now)
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return datetime(year, month, clamp_day(year, month, config.MONTH_RESET_DAY))


def days_until(reset_date, today):
    return max(0, (reset_date - today).days)


def parse_expiry_date(value):
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def get_vps_expiry(today=None):
    """Days until the VPS expires; negative once it has expired. None when no date is set."""
    expiry = parse_expiry_date(config.VPS_EXPIRY_DATE)
    if not expiry:
        return None
    today = today or timeutil.now().date()
    return {"date": expiry.isoformat(), "days_left": (expiry - today).days}


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
        "month_reset_day": config.MONTH_RESET_DAY,
        "cycle_start": cycle_start.isoformat(),
        "total_since": util.format_meta_time(get_meta_value("install_time", ""))
    }
    summary["traffic_quota"] = get_traffic_quota_summary(summary)
    summary["vps_expiry"] = get_vps_expiry(now.date())
    return summary


def get_traffic_quota_summary(summary):
    today = timeutil.now().date()
    bw = bandwagon.fetch_bandwagon_info_cached()
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
    limit = int(config.TRAFFIC_QUOTA_BYTES or 0)
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
    if config.TRAFFIC_ALERT_ENABLED != "1" or not quota or not quota.get("available") or not quota.get("limit"):
        return None
    thresholds = parse_alert_thresholds(config.TRAFFIC_ALERT_THRESHOLDS)
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
        f"來源：{util.escape_html(quota.get('source_label', ''))}",
        f"已用：<b>{util.format_bytes(used)}</b> / {util.format_bytes(limit)}",
        f"剩餘：<b>{util.format_bytes(max(0, limit - used))}</b>",
    ]
    if quota.get("days_left") is not None:
        lines.append(f"距離重置：<b>{quota['days_left']} 天</b>（{util.escape_html(quota.get('reset_date', ''))}）")
    lines.append(f"已達提醒門檻 {top}%，本週期不會再重複提醒這一檔。")
    if not telegram.send_telegram_notice(telegram.migration_telegram_message(title, lines)):
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
    if config.VPS_EXPIRY_NOTIFY != "1":
        return None
    now = now or util.configured_now()
    expiry = get_vps_expiry(now.date())
    if not expiry:
        return None
    if (now.hour, now.minute) < (config.TELEGRAM_PUSH_HOUR, config.TELEGRAM_PUSH_MINUTE):
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
        f"到期日：<b>{util.escape_html(expiry['date'])}</b>",
        "到期後 VPS 會停機，面板也將無法訪問，請盡快到 KiwiVM 續費。",
        "續費後請到面板「設定流量」更新到期日。",
    ]
    if days <= 3:
        lines.append("到期前 3 天起每天提醒一次。")
    if not telegram.send_telegram_notice(telegram.migration_telegram_message(title, lines)):
        return None
    set_meta_value("expiry_reminder_sent", ",".join((sent + [stage])[-10:]))
    return stage


def run_traffic_alert_check(pending):
    summary = get_traffic_summary(pending)
    return check_traffic_alert(summary.get("traffic_quota"))


async def traffic_alert_watcher():
    await asyncio.sleep(60)
    while True:
        if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
            try:
                if config.TRAFFIC_ALERT_ENABLED == "1":
                    await util.run_blocking(run_traffic_alert_check, collector_instance.snapshot_pending())
                await util.run_blocking(check_expiry_reminder)
                await util.run_blocking(ssl_status.check_ssl_expiry_reminder)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                util.logger.warning("Traffic / expiry alert check failed: %s", exc)
        await asyncio.sleep(600)

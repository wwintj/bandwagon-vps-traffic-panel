"""Telegram notices and the daily report."""
import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from app import bandwagon, config, net, timeutil, traffic, util
from app.collector import collector_instance
from app.database import get_meta_value, set_meta_value


def migration_telegram_message(title, lines):
    now = util.configured_now().strftime("%Y-%m-%d %H:%M")
    body = [f"<b>{util.escape_html(title)}</b>", f"<code>{now} {util.escape_html(config.TELEGRAM_TIMEZONE_LABEL)}</code>", ""]
    body.extend(lines)
    return "\n".join(body)


def send_telegram_notice(text):
    """Event notices (DDNS) only need a bot token and chat id; the daily-report switch is separate."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False
    try:
        send_telegram_message(text)
        return True
    except Exception:
        return False


def build_telegram_message(pending=None):
    summary = traffic.get_traffic_summary(pending)
    ip, ip_info = net.get_cached_public_ip_info()
    location = " / ".join(filter(None, [ip_info.get('country'), ip_info.get('regionName'), ip_info.get('city')])) or "Unknown"
    now = util.configured_now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"<b>{util.escape_html(config.PANEL_TITLE)}</b>",
        f"<code>{now} {util.escape_html(config.TELEGRAM_TIMEZONE_LABEL)}</code>",
        "",
        "<b>本期重點</b>",
        f"下載        <b>{util.format_bytes(summary['month_rx'])}</b>",
        f"上傳        <b>{util.format_bytes(summary['month_tx'])}</b>",
        f"重置日      每月 {summary['month_reset_day']} 日",
        f"距離重置    {traffic.days_until(traffic.get_next_cycle_start(util.configured_now()).date(), util.configured_now().date())} 天",
    ]
    expiry = traffic.get_vps_expiry(util.configured_now().date())
    if expiry:
        days = expiry["days_left"]
        state = f"已過期 {-days} 天" if days < 0 else f"<b>{days} 天</b>" if days <= 14 else f"{days} 天"
        lines.append(f"VPS 到期    {state}（{expiry['date']}）")
    lines += [
        "",
        "<b>今日流量</b>",
        f"下載        {util.format_bytes(summary['today_rx'])}",
        f"上傳        {util.format_bytes(summary['today_tx'])}",
    ]

    bw = bandwagon.fetch_bandwagon_info()
    if bw:
        lines.append("")
        lines.append("<b>Bandwagon 官方配額</b>")
        if bw.get("error"):
            lines.append(f"狀態：{util.escape_html(bw['error'])}")
        else:
            percent = (bw["used"] / bw["limit"] * 100) if bw.get("limit") else 0
            remaining = max(0, int(bw.get("limit") or 0) - int(bw.get("used") or 0))
            lines.append(f"已用        <b>{util.format_bytes(bw['used'])}</b>")
            lines.append(f"配額        {util.format_bytes(bw['limit'])}")
            lines.append(f"剩餘        <b>{util.format_bytes(remaining)}</b>")
            lines.append(f"使用率      <b>{percent:.1f}%</b>")
            if bw.get("reset"):
                lines.append(f"重置        {util.escape_html(util.format_unix_time(bw['reset']))} {util.escape_html(config.TELEGRAM_TIMEZONE_LABEL)}")
                try:
                    lines.append(f"剩餘天數    <b>{traffic.days_until(timeutil.from_unix(bw['reset']).date(), util.configured_now().date())} 天</b>")
                except (TypeError, ValueError, OSError):
                    pass
            if bw.get("node"):
                lines.append(f"節點        {util.escape_html(bw['node'])}")

    lines.extend([
        "",
        "<b>服務器信息</b>",
        f"IP          <code>{util.escape_html(ip)}</code>",
        f"位置        {util.escape_html(location)}",
        f"類型        <b>{util.escape_html(net.classify_ip_info(ip_info))}</b>",
    ])
    return "\n".join(lines)


def send_telegram_message(text):
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        raise ValueError("Telegram token or chat id is empty")
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def send_daily_report(pending):
    return send_telegram_message(build_telegram_message(pending))


def mark_today_sent_if_past_push_time():
    """After settings change, don't fire a surprise catch-up report for a push time already passed today."""
    now = util.configured_now()
    if (now.hour, now.minute) > (config.TELEGRAM_PUSH_HOUR, config.TELEGRAM_PUSH_MINUTE):
        set_meta_value("telegram_last_daily", now.strftime("%Y-%m-%d"))


TELEGRAM_CATCHUP = timedelta(hours=2)


def should_send_daily_report(now, last_sent_day):
    """True from the push time until TELEGRAM_CATCHUP later, once per day. The window means a late
    loop tick or a failed send is retried instead of silently skipping the whole day."""
    target = now.replace(hour=config.TELEGRAM_PUSH_HOUR, minute=config.TELEGRAM_PUSH_MINUTE, second=0, microsecond=0)
    if now < target or now - target > TELEGRAM_CATCHUP:
        return False
    return last_sent_day != now.strftime("%Y-%m-%d")


def send_telegram_message_if_ready(text):
    if config.TELEGRAM_ENABLED != "1" or not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False
    try:
        send_telegram_message(text)
        return True
    except Exception:
        return False


async def telegram_daily_notifier():
    while True:
        try:
            await asyncio.sleep(30)
            if config.TELEGRAM_ENABLED != "1" or not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
                continue
            now = util.configured_now()
            if not should_send_daily_report(now, get_meta_value("telegram_last_daily")):
                continue
            today_key = now.strftime("%Y-%m-%d")
            await util.run_blocking(send_daily_report, collector_instance.snapshot_pending())
            set_meta_value("telegram_last_daily", today_key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            util.logger.warning("Telegram daily report failed, will retry: %s", exc)
            await asyncio.sleep(300)

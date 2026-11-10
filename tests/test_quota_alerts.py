from datetime import date, datetime

import pytest

from app import timeutil

GB = 1024 ** 3


@pytest.fixture
def alerts(m, monkeypatch):
    monkeypatch.setattr(m, "TRAFFIC_ALERT_ENABLED", "1")
    monkeypatch.setattr(m, "TRAFFIC_ALERT_THRESHOLDS", "80,90,100")
    monkeypatch.setattr(m, "TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "chat")
    m.set_meta_value("traffic_alert_cycle", "")
    m.set_meta_value("traffic_alert_sent", "")
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda text: sent.append(text))
    m._sent = sent
    return m


def quota(used_gb, limit_gb=100, reset="2026-12-01", source="bandwagon"):
    return {
        "available": True, "source": source, "source_label": "Bandwagon 官方",
        "used": int(used_gb * GB), "limit": int(limit_gb * GB),
        "reset_date": reset, "days_left": 5,
    }


# ---- days until reset -------------------------------------------------------

@pytest.mark.parametrize("now, reset_day, expected", [
    (datetime(2026, 11, 10), 1, datetime(2026, 12, 1)),
    (datetime(2026, 11, 10), 15, datetime(2026, 11, 15)),
    (datetime(2026, 11, 15), 15, datetime(2026, 12, 15)),   # reset day itself starts a new cycle
    (datetime(2026, 12, 20), 5, datetime(2027, 1, 5)),      # across the year end
    (datetime(2027, 1, 31), 31, datetime(2027, 2, 28)),     # 31st clamps to month end
    (datetime(2027, 2, 28), 31, datetime(2027, 3, 31)),
])
def test_next_cycle_start(m, monkeypatch, now, reset_day, expected):
    monkeypatch.setattr(m, "MONTH_RESET_DAY", reset_day)
    assert m.get_next_cycle_start(now) == expected


def test_days_until_never_negative(m):
    assert m.days_until(date(2026, 11, 16), date(2026, 11, 10)) == 6
    assert m.days_until(date(2026, 11, 10), date(2026, 11, 10)) == 0
    assert m.days_until(date(2026, 11, 1), date(2026, 11, 10)) == 0


def test_quota_summary_reports_days_left_from_bandwagon_reset(m, monkeypatch):
    reset_ts = int((datetime(2026, 1, 1) - datetime(1970, 1, 1)).total_seconds()) + 20 * 86400
    monkeypatch.setattr(m, "fetch_bandwagon_info_cached", lambda force=False: {"used": 10 * GB, "limit": 100 * GB, "reset": reset_ts})
    q = m.get_traffic_quota_summary({"month_rx": 0, "month_tx": 0})
    expected = timeutil.from_unix(reset_ts).date()
    assert q["source"] == "bandwagon"
    assert q["reset_date"] == expected.isoformat()
    assert q["days_left"] == m.days_until(expected, timeutil.now().date())


def test_quota_summary_reports_days_left_for_manual_quota(m, monkeypatch):
    monkeypatch.setattr(m, "TRAFFIC_QUOTA_BYTES", 100 * GB)
    q = m.get_traffic_quota_summary({"month_rx": 1, "month_tx": 2})
    assert q["source"] == "manual"
    assert q["reset_date"] == m.get_next_cycle_start(timeutil.now()).date().isoformat()
    assert isinstance(q["days_left"], int) and q["days_left"] >= 0


# ---- threshold alerts -------------------------------------------------------

def test_threshold_parsing(m):
    assert m.parse_alert_thresholds("90, 80，100 、 abc 0 250 80") == [80, 90, 100]
    assert m.parse_alert_thresholds("") == []


def test_no_alert_below_first_threshold(alerts):
    assert alerts.check_traffic_alert(quota(79.9)) is None
    assert alerts._sent == []


def test_each_threshold_alerts_once_per_cycle(alerts):
    assert alerts.check_traffic_alert(quota(81)) == 80
    assert alerts.check_traffic_alert(quota(85)) is None
    assert alerts.check_traffic_alert(quota(91)) == 90
    assert alerts.check_traffic_alert(quota(95)) is None
    assert alerts.check_traffic_alert(quota(100)) == 100
    assert alerts.check_traffic_alert(quota(120)) is None
    assert len(alerts._sent) == 3
    assert "已用完" in alerts._sent[-1]


def test_crossing_several_thresholds_sends_only_the_highest(alerts):
    assert alerts.check_traffic_alert(quota(95)) == 90
    assert len(alerts._sent) == 1 and "95.0%" in alerts._sent[0]
    # 80 was marked too, so it never fires later in this cycle
    assert alerts.check_traffic_alert(quota(96)) is None


def test_new_cycle_starts_over(alerts):
    alerts.check_traffic_alert(quota(85, reset="2026-12-01"))
    assert alerts.check_traffic_alert(quota(85, reset="2027-01-01")) == 80
    assert len(alerts._sent) == 2


def test_disabled_or_unavailable_quota_never_alerts(alerts, monkeypatch):
    assert alerts.check_traffic_alert({"available": False}) is None
    monkeypatch.setattr(alerts, "TRAFFIC_ALERT_ENABLED", "0")
    assert alerts.check_traffic_alert(quota(99)) is None
    assert alerts._sent == []


def test_failed_send_is_retried_next_round(alerts, monkeypatch):
    def broken(text):
        raise OSError("telegram down")

    monkeypatch.setattr(alerts, "send_telegram_message", broken)
    assert alerts.check_traffic_alert(quota(85)) is None
    monkeypatch.setattr(alerts, "send_telegram_message", lambda text: alerts._sent.append(text))
    assert alerts.check_traffic_alert(quota(85)) == 80


def test_alert_message_contents(alerts):
    alerts.check_traffic_alert(quota(85, limit_gb=100))
    text = alerts._sent[0]
    assert "85.0%" in text and "剩餘" in text and "距離重置" in text and "5 天" in text


def test_daily_report_shows_days_left(m, monkeypatch):
    reset_ts = int(datetime.utcnow().timestamp()) + 3 * 86400
    monkeypatch.setattr(m, "fetch_bandwagon_info", lambda: {"used": GB, "limit": 10 * GB, "reset": reset_ts, "node": ""})
    text = m.build_telegram_message({})
    assert "距離重置" in text and "剩餘天數" in text


def test_quota_settings_round_trip(m, client, auth, post_headers, monkeypatch):
    monkeypatch.setattr(m, "update_env_values", lambda updates: None)
    monkeypatch.setattr(m, "TRAFFIC_ALERT_ENABLED", m.TRAFFIC_ALERT_ENABLED)
    monkeypatch.setattr(m, "TRAFFIC_ALERT_THRESHOLDS", m.TRAFFIC_ALERT_THRESHOLDS)
    monkeypatch.setattr(m, "TRAFFIC_QUOTA_BYTES", m.TRAFFIC_QUOTA_BYTES)
    payload = {"traffic_quota_bytes": 0, "traffic_alert_enabled": True, "traffic_alert_thresholds": "95, 70"}
    r = client.post("/api/traffic-quota", headers=post_headers, json=payload)
    assert r.status_code == 200
    assert "days_left" in r.json()["traffic_quota"]
    data = client.get("/api/traffic-quota", headers=auth).json()
    assert data["traffic_alert_thresholds"] == "70,95" and data["traffic_alert_enabled"] is True
    bad = dict(payload, traffic_alert_thresholds="abc")
    assert client.post("/api/traffic-quota", headers=post_headers, json=bad).status_code == 400


# ---- VPS expiry -------------------------------------------------------------

@pytest.mark.parametrize("value, today, expected", [
    ("", date(2026, 11, 11), None),
    ("not-a-date", date(2026, 11, 11), None),
    ("2027-01-25", date(2026, 11, 11), 75),
    ("2026-11-11", date(2026, 11, 11), 0),
    ("2026-11-01", date(2026, 11, 11), -10),
])
def test_vps_expiry_days(m, monkeypatch, value, today, expected):
    monkeypatch.setattr(m, "VPS_EXPIRY_DATE", value)
    result = m.get_vps_expiry(today)
    if expected is None:
        assert result is None
    else:
        assert result == {"date": value, "days_left": expected}


def test_summary_includes_expiry(m, monkeypatch):
    monkeypatch.setattr(m, "VPS_EXPIRY_DATE", "2099-01-01")
    assert m.get_traffic_summary({})["vps_expiry"]["date"] == "2099-01-01"
    monkeypatch.setattr(m, "VPS_EXPIRY_DATE", "")
    assert m.get_traffic_summary({})["vps_expiry"] is None


def test_daily_report_shows_expiry(m, monkeypatch):
    monkeypatch.setattr(m, "fetch_bandwagon_info", lambda: None)
    monkeypatch.setattr(m, "VPS_EXPIRY_DATE", (timeutil.now().date().replace(year=timeutil.now().year + 1)).isoformat())
    assert "VPS 到期" in m.build_telegram_message({})
    monkeypatch.setattr(m, "VPS_EXPIRY_DATE", "2000-01-01")
    assert "已過期" in m.build_telegram_message({})
    monkeypatch.setattr(m, "VPS_EXPIRY_DATE", "")
    assert "VPS 到期" not in m.build_telegram_message({})


def test_expiry_date_settings(m, client, auth, post_headers, monkeypatch):
    monkeypatch.setattr(m, "update_env_values", lambda updates: None)
    monkeypatch.setattr(m, "VPS_EXPIRY_DATE", m.VPS_EXPIRY_DATE)
    monkeypatch.setattr(m, "TRAFFIC_QUOTA_BYTES", m.TRAFFIC_QUOTA_BYTES)
    base = {"traffic_quota_bytes": 0}
    r = client.post("/api/traffic-quota", headers=post_headers, json=dict(base, vps_expiry_date="2027-01-25"))
    assert r.status_code == 200 and r.json()["vps_expiry"]["date"] == "2027-01-25"
    assert client.get("/api/traffic-quota", headers=auth).json()["vps_expiry_date"] == "2027-01-25"
    assert client.post("/api/traffic-quota", headers=post_headers, json=dict(base, vps_expiry_date="25/01/2027")).status_code == 400
    r = client.post("/api/traffic-quota", headers=post_headers, json=dict(base, vps_expiry_date=""))
    assert r.status_code == 200 and r.json()["vps_expiry"] is None


# ---- VPS expiry reminders ---------------------------------------------------

from datetime import timedelta  # noqa: E402


@pytest.fixture
def expiry(m, monkeypatch):
    monkeypatch.setattr(m, "VPS_EXPIRY_DATE", "2026-12-31")
    monkeypatch.setattr(m, "VPS_EXPIRY_NOTIFY", "1")
    monkeypatch.setattr(m, "TELEGRAM_PUSH_HOUR", 20)
    monkeypatch.setattr(m, "TELEGRAM_PUSH_MINUTE", 0)
    monkeypatch.setattr(m, "TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "chat")
    m.set_meta_value("expiry_reminder_date", "")
    m.set_meta_value("expiry_reminder_sent", "")
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda text: sent.append(text))
    m._sent = sent
    return m


def test_expiry_reminder_schedule(expiry):
    reminded_days = []
    day = datetime(2026, 11, 25)
    while day <= datetime(2027, 1, 2):
        for hour in (8, 20, 23):  # 8:00 is before the push time and must not send
            before = len(expiry._sent)
            expiry.check_expiry_reminder(day.replace(hour=hour, minute=30))
            if len(expiry._sent) > before:
                assert hour != 8
                reminded_days.append((datetime(2026, 12, 31) - day).days)
        day += timedelta(days=1)
    # 30 days, 7 days, then every day from 3 days out, also after expiry while the panel is still up
    assert reminded_days == [30, 7, 3, 2, 1, 0, -1, -2]
    assert "剩 30 天" in expiry._sent[0]
    assert "今天到期" in expiry._sent[5]
    assert "已過期 1 天" in expiry._sent[6]


def test_expiry_reminder_first_set_inside_window(expiry, monkeypatch):
    monkeypatch.setattr(expiry, "VPS_EXPIRY_DATE", "2026-12-21")
    assert expiry.check_expiry_reminder(datetime(2026, 12, 1, 21)) == "30"   # 20 days left
    assert expiry.check_expiry_reminder(datetime(2026, 12, 2, 21)) is None


def test_renewal_restarts_schedule(expiry, monkeypatch):
    assert expiry.check_expiry_reminder(datetime(2026, 12, 29, 21)) is not None
    monkeypatch.setattr(expiry, "VPS_EXPIRY_DATE", "2027-12-31")   # renewed
    assert expiry.check_expiry_reminder(datetime(2026, 12, 30, 21)) is None
    assert expiry.check_expiry_reminder(datetime(2027, 12, 1, 21)) == "30"


def test_expiry_reminder_off_or_unset(expiry, monkeypatch):
    monkeypatch.setattr(expiry, "VPS_EXPIRY_NOTIFY", "0")
    assert expiry.check_expiry_reminder(datetime(2026, 12, 30, 21)) is None
    monkeypatch.setattr(expiry, "VPS_EXPIRY_NOTIFY", "1")
    monkeypatch.setattr(expiry, "VPS_EXPIRY_DATE", "")
    assert expiry.check_expiry_reminder(datetime(2026, 12, 30, 21)) is None
    assert expiry._sent == []


def test_expiry_reminder_retries_after_failed_send(expiry, monkeypatch):
    def broken(text):
        raise OSError("down")

    monkeypatch.setattr(expiry, "send_telegram_message", broken)
    assert expiry.check_expiry_reminder(datetime(2026, 12, 30, 21)) is None
    monkeypatch.setattr(expiry, "send_telegram_message", lambda text: expiry._sent.append(text))
    assert expiry.check_expiry_reminder(datetime(2026, 12, 30, 21, 10)) is not None

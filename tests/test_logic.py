import asyncio
from datetime import datetime

from app import collector as collector_module
from app import timeutil


def run_collector(sequence, start_iface="eth0", detected="ens3"):
    it = iter(sequence)

    def read_all():
        try:
            return next(it)
        except StopIteration:
            return sequence[-1]

    tc = collector_module.TrafficCollector()
    tc.interface = start_iface
    tc._read_all_interfaces = read_all
    tc._get_active_interface = lambda: detected
    tc._get_default_interface = lambda: detected
    tc.flush_to_db = lambda: None

    async def go():
        real_sleep = asyncio.sleep

        async def fast_sleep(_):
            await real_sleep(0)

        collector_module.asyncio.sleep = fast_sleep
        try:
            task = asyncio.ensure_future(tc.start())
            for _ in range(30):
                await real_sleep(0)
            tc.running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            collector_module.asyncio.sleep = real_sleep

    asyncio.new_event_loop().run_until_complete(go())
    return tc


def test_interface_switch_does_not_count_counters_since_boot():
    sequence = [{"eth0": (100, 100)}, {"eth0": (200, 150)}] + [
        {"ens3": (5_000_000_000 + i * 1000, 6_000_000_000 + i * 500)} for i in range(4)
    ]
    tc = run_collector(sequence)
    assert tc.interface == "ens3"
    total_rx = sum(b["rx"] for b in tc.pending_buckets.values())
    assert total_rx == 100 + 3000


def test_counter_reset_counts_new_value_only():
    sequence = [{"eth0": (1000, 1000)}, {"eth0": (2000, 2000)}, {"eth0": (50, 50)}, {"eth0": (150, 150)}]
    tc = run_collector(sequence, detected="eth0")
    assert sum(b["rx"] for b in tc.pending_buckets.values()) == 1000 + 50 + 100


def test_migration_with_unknown_old_ip_is_not_reported_as_success(m, monkeypatch):
    m.set_meta_value("migration_active", "1")
    m.set_meta_value("migration_old_ip", "Unknown")
    m.set_meta_value("migration_new_ip", "")
    m.set_meta_value("migration_notified_ip", "0")
    sent = []
    monkeypatch.setattr(m, "send_telegram_message_if_ready", lambda text: sent.append(text))
    status = m.build_migration_status()
    assert status["ip_changed"] is False
    assert sent == []


def test_migration_detects_real_ip_change(m, monkeypatch):
    m.set_meta_value("migration_active", "1")
    m.set_meta_value("migration_old_ip", "9.9.9.9")
    m.set_meta_value("migration_new_ip", "")
    m.set_meta_value("migration_notified_ip", "0")
    monkeypatch.setattr(m, "send_telegram_message_if_ready", lambda text: True)
    assert m.build_migration_status()["ip_changed"] is True


def test_daily_report_window(m, monkeypatch):
    monkeypatch.setattr(m, "TELEGRAM_PUSH_HOUR", 20)
    monkeypatch.setattr(m, "TELEGRAM_PUSH_MINUTE", 0)
    assert not m.should_send_daily_report(datetime(2026, 1, 1, 19, 59), None)
    assert m.should_send_daily_report(datetime(2026, 1, 1, 20, 0), None)
    # A missed minute or failed send is retried later in the window.
    assert m.should_send_daily_report(datetime(2026, 1, 1, 21, 30), "2025-12-31")
    assert not m.should_send_daily_report(datetime(2026, 1, 1, 20, 5), "2026-01-01")
    assert not m.should_send_daily_report(datetime(2026, 1, 1, 23, 0), None)


def test_hourly_returns_last_24_hours_including_pending(m, client, auth, monkeypatch):
    now = timeutil.now()
    key = (now.year, now.month, now.day, now.hour)
    monkeypatch.setattr(m.collector_instance, "pending_buckets", {key: {"rx": 123, "tx": 45}})
    data = client.get("/api/hourly", headers=auth).json()
    assert len(data) == 24
    assert (data[-1]["hour"], data[-1]["rx_bytes"], data[-1]["tx_bytes"]) == (now.hour, 123, 45)


def test_summary_includes_pending_snapshot(m):
    now = timeutil.now()
    pending = {(now.year, now.month, now.day, now.hour): {"rx": 1000, "tx": 2000}}
    before = m.get_traffic_summary({})
    after = m.get_traffic_summary(pending)
    assert after["today_rx"] - before["today_rx"] == 1000
    assert after["month_tx"] - before["month_tx"] == 2000


def test_timezone_offset_moves_today(monkeypatch):
    timeutil.set_offset(0)
    utc_now = timeutil.now()
    timeutil.set_offset(8)
    try:
        diff = timeutil.now() - utc_now
        assert 7.99 * 3600 < diff.total_seconds() < 8.01 * 3600
    finally:
        timeutil.set_offset(8)

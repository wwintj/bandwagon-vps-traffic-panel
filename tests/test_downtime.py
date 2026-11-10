import json
import time
from datetime import datetime

import pytest

from app import collector as collector_module
from app import timeutil


@pytest.fixture(autouse=True)
def clean_traffic(m):
    """Recovered traffic lands in real hourly buckets; keep other tests' numbers untouched."""
    def wipe():
        conn = m.get_db()
        conn.execute("DELETE FROM hourly_traffic")
        conn.execute("DELETE FROM meta_info WHERE key = 'collector_state'")
        conn.commit()
        conn.close()
    wipe()
    yield
    wipe()


def test_spread_over_hours_is_proportional_and_exact():
    parts = collector_module.spread_over_hours(datetime(2026, 1, 1, 10, 30), datetime(2026, 1, 1, 12, 30), 4000, 400)
    assert [p[0] for p in parts] == [(2026, 1, 1, 10), (2026, 1, 1, 11), (2026, 1, 1, 12)]
    assert [p[1] for p in parts] == [1000, 2000, 1000]
    assert sum(p[2] for p in parts) == 400


def make_collector(m, monkeypatch, boot_id="boot-a", iface="eth0"):
    monkeypatch.setattr(collector_module, "read_boot_id", lambda: boot_id)
    tc = collector_module.TrafficCollector()
    tc.interface = iface
    tc.interface_status = True
    return tc


def db_total(m):
    conn = m.get_db()
    row = conn.execute("SELECT COALESCE(SUM(rx_bytes),0), COALESCE(SUM(tx_bytes),0) FROM hourly_traffic").fetchone()
    conn.close()
    return row[0], row[1]


def save_state(m, **overrides):
    state = {"boot_id": "boot-a", "iface": "eth0", "rx": 1_000_000, "tx": 500_000, "at": time.time() - 2 * 3600}
    state.update(overrides)
    m.set_meta_value("collector_state", json.dumps(state))


def test_recovers_traffic_counted_while_panel_was_stopped(m, monkeypatch):
    before = db_total(m)
    save_state(m)
    tc = make_collector(m, monkeypatch)
    assert tc.recover_downtime(1_000_000 + 7_200, 500_000 + 3_600) == (7_200, 3_600)
    after = db_total(m)
    assert (after[0] - before[0], after[1] - before[1]) == (7_200, 3_600)


def test_no_recovery_after_reboot_iface_change_or_counter_reset(m, monkeypatch):
    before = db_total(m)
    save_state(m, boot_id="boot-old")
    assert make_collector(m, monkeypatch).recover_downtime(2_000_000, 900_000) == (0, 0)
    save_state(m, iface="ens3")
    assert make_collector(m, monkeypatch).recover_downtime(2_000_000, 900_000) == (0, 0)
    save_state(m)
    assert make_collector(m, monkeypatch).recover_downtime(10, 10) == (0, 0)
    assert db_total(m) == before


def test_flush_saves_counter_state(m, monkeypatch):
    tc = make_collector(m, monkeypatch)
    tc.last_rx, tc.last_tx = 123, 456
    tc.flush_to_db()
    state = json.loads(m.get_meta_value("collector_state"))
    assert (state["boot_id"], state["iface"], state["rx"], state["tx"]) == ("boot-a", "eth0", 123, 456)
    assert abs(state["at"] - time.time()) < 5
    assert timeutil.from_unix(state["at"]).year >= 2026

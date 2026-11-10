import io
import json
import urllib.error
from datetime import timedelta

import pytest

from app import timeutil

ZONE_ID = "0123456789abcdef0123456789abcdef"


class FakeCloudflare:
    """Minimal stand-in for the Cloudflare v4 API used by the DDNS code."""

    def __init__(self, zones=None, records=None, fail=None):
        self.zones = zones if zones is not None else {"example.com": ZONE_ID}
        self.records = records if records is not None else []
        self.fail = fail
        self.calls = []

    def __call__(self, url, method="GET", payload=None, headers=None, timeout=10):
        self.calls.append((method, url, payload))
        if self.fail:
            return {"success": False, "errors": [self.fail]}
        if "/zones?" in url:
            name = url.split("name=")[1]
            zone_id = self.zones.get(name)
            return {"success": True, "result": [{"id": zone_id, "name": name}] if zone_id else []}
        if url.endswith("/dns_records") and method == "POST":
            record = dict(payload, id="new-record")
            self.records.append(record)
            return {"success": True, "result": record}
        if "/dns_records/" in url and method == "PATCH":
            self.records[0].update(payload)
            return {"success": True, "result": self.records[0]}
        if "/dns_records?" in url:
            return {"success": True, "result": list(self.records)}
        raise AssertionError(f"unexpected call {method} {url}")

    def writes(self):
        return [c for c in self.calls if c[0] in ("PATCH", "POST")]


@pytest.fixture
def ddns(m, monkeypatch):
    monkeypatch.setattr(m, "CLOUDFLARE_API_TOKEN", "token")
    monkeypatch.setattr(m, "CLOUDFLARE_RECORD_NAME", "vps.example.com")
    monkeypatch.setattr(m, "CLOUDFLARE_ZONE_ID", "")
    monkeypatch.setattr(m, "CLOUDFLARE_PROXIED", "0")
    monkeypatch.setattr(m, "CLOUDFLARE_TTL", 60)
    monkeypatch.setattr(m, "CLOUDFLARE_DDNS_NOTIFY", "1")
    monkeypatch.setattr(m, "TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "chat")
    m.CLOUDFLARE_ZONE_CACHE.clear()
    for key in ("cloudflare_ddns_last_ip", "cloudflare_ddns_last_sync", "cloudflare_ddns_last_error", "migration_active"):
        m.set_meta_value(key, "")
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda text: sent.append(text))
    m._sent = sent
    return m


def use_ip(m, monkeypatch, ip):
    monkeypatch.setattr(m, "get_cached_public_ip_info", lambda force=False: (ip, {}))


def test_public_ip_falls_back_and_rejects_non_ipv4(m, monkeypatch):
    answers = iter([OSError("down"), b"2001:db8::1", b"not-an-ip", b"203.0.113.7\n"])

    def fake_urlopen(req, timeout=0):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(answer)

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    assert m.fetch_public_ipv4() == "203.0.113.7"


def test_public_ip_none_when_all_sources_fail(m, monkeypatch):
    def fake_urlopen(req, timeout=0):
        raise OSError("down")

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    assert m.fetch_public_ipv4() is None


def test_request_json_returns_cloudflare_error_body(m, monkeypatch):
    body = json.dumps({"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}).encode()

    def fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(body))

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    result = m.request_json("https://api.cloudflare.com/client/v4/zones")
    assert result["errors"][0]["code"] == 10000
    assert "Zone.DNS:Edit" in m.cloudflare_error_text(result, "x")


def test_zone_detected_from_record_name(ddns, monkeypatch):
    cf = FakeCloudflare()
    monkeypatch.setattr(ddns, "request_json", cf)
    monkeypatch.setattr(ddns, "CLOUDFLARE_RECORD_NAME", "a.vps.example.com")
    assert ddns.resolve_cloudflare_zone_id() == ZONE_ID
    looked_up = [c[1].split("name=")[1] for c in cf.calls]
    assert looked_up == ["a.vps.example.com", "vps.example.com", "example.com"]
    # cached afterwards
    ddns.resolve_cloudflare_zone_id()
    assert len(cf.calls) == 3


def test_zone_id_used_directly_and_zone_name_looked_up(ddns, monkeypatch):
    cf = FakeCloudflare()
    monkeypatch.setattr(ddns, "request_json", cf)
    monkeypatch.setattr(ddns, "CLOUDFLARE_ZONE_ID", ZONE_ID.upper())
    assert ddns.resolve_cloudflare_zone_id() == ZONE_ID.upper()
    assert cf.calls == []
    monkeypatch.setattr(ddns, "CLOUDFLARE_ZONE_ID", "example.com")
    assert ddns.resolve_cloudflare_zone_id() == ZONE_ID


def test_unknown_zone_is_a_clear_error(ddns, monkeypatch):
    monkeypatch.setattr(ddns, "request_json", FakeCloudflare(zones={}))
    with pytest.raises(ValueError, match="找不到"):
        ddns.resolve_cloudflare_zone_id()


def test_sync_creates_missing_record_with_ttl(ddns, monkeypatch):
    cf = FakeCloudflare()
    monkeypatch.setattr(ddns, "request_json", cf)
    result = ddns.sync_cloudflare_ddns("198.51.100.1")
    assert result["action"] == "created"
    assert cf.records[0]["content"] == "198.51.100.1"
    assert cf.records[0]["ttl"] == 60 and cf.records[0]["proxied"] is False


def test_sync_skips_write_when_record_already_correct(ddns, monkeypatch):
    cf = FakeCloudflare(records=[{"id": "r1", "content": "198.51.100.1", "ttl": 60, "proxied": False}])
    monkeypatch.setattr(ddns, "request_json", cf)
    assert ddns.sync_cloudflare_ddns("198.51.100.1")["action"] == "unchanged"
    assert cf.writes() == []


def test_sync_updates_changed_ip_and_forces_auto_ttl_when_proxied(ddns, monkeypatch):
    cf = FakeCloudflare(records=[{"id": "r1", "content": "198.51.100.1", "ttl": 60, "proxied": False}])
    monkeypatch.setattr(ddns, "request_json", cf)
    monkeypatch.setattr(ddns, "CLOUDFLARE_PROXIED", "1")
    result = ddns.sync_cloudflare_ddns("198.51.100.2")
    assert result["action"] == "updated" and result["previous_ip"] == "198.51.100.1"
    assert cf.records[0]["content"] == "198.51.100.2"
    assert cf.records[0]["ttl"] == 1 and cf.records[0]["proxied"] is True


def test_check_once_updates_and_notifies(ddns, monkeypatch):
    cf = FakeCloudflare(records=[{"id": "r1", "content": "198.51.100.1", "ttl": 60, "proxied": False}])
    monkeypatch.setattr(ddns, "request_json", cf)
    ddns.set_meta_value("cloudflare_ddns_last_ip", "198.51.100.1")
    use_ip(ddns, monkeypatch, "198.51.100.9")
    assert ddns.ddns_check_once()["action"] == "updated"
    assert len(ddns._sent) == 1 and "198.51.100.9" in ddns._sent[0] and "198.51.100.1" in ddns._sent[0]


def test_check_once_skips_when_ip_unchanged_and_recently_synced(ddns, monkeypatch):
    cf = FakeCloudflare()
    monkeypatch.setattr(ddns, "request_json", cf)
    ddns.set_meta_value("cloudflare_ddns_last_ip", "198.51.100.1")
    ddns.set_meta_value("cloudflare_ddns_last_sync", timeutil.now().isoformat())
    use_ip(ddns, monkeypatch, "198.51.100.1")
    assert ddns.ddns_check_once()["action"] == "skipped"
    assert cf.calls == []


def test_check_once_rechecks_record_after_six_hours(ddns, monkeypatch):
    # Someone changed the record by hand; the periodic re-check puts it back.
    cf = FakeCloudflare(records=[{"id": "r1", "content": "192.0.2.50", "ttl": 60, "proxied": False}])
    monkeypatch.setattr(ddns, "request_json", cf)
    ddns.set_meta_value("cloudflare_ddns_last_ip", "198.51.100.1")
    ddns.set_meta_value("cloudflare_ddns_last_sync", (timeutil.now() - timedelta(hours=7)).isoformat())
    use_ip(ddns, monkeypatch, "198.51.100.1")
    assert ddns.ddns_check_once()["action"] == "updated"
    assert cf.records[0]["content"] == "198.51.100.1"


def test_failure_notifies_once_then_recovery_notifies(ddns, monkeypatch):
    use_ip(ddns, monkeypatch, "198.51.100.1")
    failing = FakeCloudflare(fail={"code": 9109, "message": "Invalid access token"})
    monkeypatch.setattr(ddns, "request_json", failing)
    for _ in range(3):
        assert ddns.ddns_check_once()["ok"] is False
    assert len(ddns._sent) == 1 and "DDNS 更新失敗" in ddns._sent[0]
    assert "Zone.DNS:Edit" in ddns.get_meta_value("cloudflare_ddns_last_error")

    monkeypatch.setattr(ddns, "request_json", FakeCloudflare(records=[{"id": "r1", "content": "198.51.100.1", "ttl": 60, "proxied": False}]))
    assert ddns.ddns_check_once()["ok"] is True
    assert len(ddns._sent) == 2 and "恢復正常" in ddns._sent[1]
    assert ddns.get_meta_value("cloudflare_ddns_last_error") == ""


def test_no_ip_from_any_source_is_reported(ddns, monkeypatch):
    monkeypatch.setattr(ddns, "request_json", FakeCloudflare())
    use_ip(ddns, monkeypatch, "Unknown")
    result = ddns.ddns_check_once()
    assert result["ok"] is False and "IPv4" in result["error"]


def test_notify_switch_and_migration_silence_notices(ddns, monkeypatch):
    cf = FakeCloudflare(records=[{"id": "r1", "content": "198.51.100.1", "ttl": 60, "proxied": False}])
    monkeypatch.setattr(ddns, "request_json", cf)
    use_ip(ddns, monkeypatch, "198.51.100.2")
    monkeypatch.setattr(ddns, "CLOUDFLARE_DDNS_NOTIFY", "0")
    ddns.ddns_check_once()
    monkeypatch.setattr(ddns, "CLOUDFLARE_DDNS_NOTIFY", "1")
    ddns.set_meta_value("migration_active", "1")
    use_ip(ddns, monkeypatch, "198.51.100.3")
    ddns.ddns_check_once()
    assert ddns._sent == []


def test_ddns_settings_round_trip(ddns, client, auth, post_headers, monkeypatch):
    monkeypatch.setattr(ddns, "update_env_values", lambda updates: None)
    settings = client.get("/api/ddns-settings", headers=auth).json()
    settings.update({"cloudflare_ddns_interval": 2, "cloudflare_ttl": 120, "cloudflare_ddns_notify": False, "cloudflare_zone_id": ""})
    assert client.post("/api/ddns-settings", headers=post_headers, json=settings).status_code == 200
    again = client.get("/api/ddns-settings", headers=auth).json()
    assert again["cloudflare_ddns_interval"] == 2
    assert again["cloudflare_ttl"] == 120
    assert again["cloudflare_ddns_notify"] is False
    assert "token" not in again["cloudflare_api_token"]
    # out-of-range values are clamped
    settings.update({"cloudflare_ddns_interval": 999, "cloudflare_ttl": 5})
    client.post("/api/ddns-settings", headers=post_headers, json=settings)
    again = client.get("/api/ddns-settings", headers=auth).json()
    assert again["cloudflare_ddns_interval"] == 60 and again["cloudflare_ttl"] == 1

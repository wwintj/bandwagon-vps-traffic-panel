import pytest

from app.bandwagon import fetch_bandwagon_info as REAL_FETCH_BANDWAGON_INFO  # the shared fixture stubs it out


def test_locations_use_api_descriptions(m):
    data = {
        "currentLocation": "USCA_8",
        "locations": ["USCA_2", "JPOS_1", "NEWDC_9"],
        "descriptions": {"USCA_2": "US: California, Los Angeles (DC2)", "JPOS_1": "JP: Osaka (SoftBank)"},
        "dataTransferMultipliers": {"JPOS_1": 2, "USCA_2": 1},
    }
    result = {loc["code"]: loc["name"] for loc in m.normalize_bandwagon_locations(data)}
    assert result["USCA_2"] == "US: California, Los Angeles (DC2) [USCA_2]"
    assert result["JPOS_1"] == "JP: Osaka (SoftBank) [JPOS_1]（流量 ×2）"
    assert result["NEWDC_9"].endswith("[NEWDC_9]")   # no description: still listed, never dropped


def test_locations_endpoint_reports_current_from_api(m, client, auth, monkeypatch):
    data = {"currentLocation": "USCA_8", "locations": ["USCA_2"], "descriptions": {"USCA_8": "US: Los Angeles (DC8 CN2)"}}
    monkeypatch.setattr(m, "bandwagon_request", lambda action, params=None: data)
    body = client.get("/api/bandwagon/locations", headers=auth).json()
    assert body["current_location"] == "US: Los Angeles (DC8 CN2) [USCA_8]"


def test_service_info_includes_plan_and_suspension(m, monkeypatch):
    payload = {"data_counter": 10, "plan_monthly_data": 100, "monthly_data_multiplier": 2, "suspended": True,
               "plan_ram": 1073741824, "plan_swap": 536870912, "plan_disk": 21474836480, "os": "debian-12", "error": 0}

    class Resp:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import json
    monkeypatch.setattr(m, "BANDWAGON_VEID", "1")
    monkeypatch.setattr(m, "BANDWAGON_API_KEY", "k")
    monkeypatch.setattr(m.urllib.request, "urlopen", lambda req, timeout=0: Resp(json.dumps(payload).encode()))
    info = REAL_FETCH_BANDWAGON_INFO()
    assert info["used"] == 20 and info["limit"] == 200 and info["multiplier"] == 2
    assert info["suspended"] is True and info["plan_ram"] == 1073741824 and info["os"] == "debian-12"


def test_restart_endpoint(m, client, post_headers, monkeypatch):
    calls = []
    monkeypatch.setattr(m, "bandwagon_request", lambda action, params=None: calls.append(action) or {"error": 0})
    monkeypatch.setattr(m, "send_telegram_notice", lambda text: True)
    assert client.post("/api/bandwagon/restart", headers=post_headers).status_code == 200
    assert calls == ["restart"]
    monkeypatch.setattr(m, "bandwagon_request", lambda action, params=None: {"error": 1, "message": "Invalid API key"})
    r = client.post("/api/bandwagon/restart", headers=post_headers)
    assert r.status_code == 400 and "Invalid API key" in r.json()["detail"]


def test_restart_needs_csrf_header(client, auth):
    assert client.post("/api/bandwagon/restart", headers=auth).status_code == 403

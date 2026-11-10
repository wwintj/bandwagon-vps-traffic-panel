from app import collector as collector_module

COUNTERS = {"lo": (5, 5), "eth0": (1000, 2000), "ens3": (300, 400), "docker0": (1, 1)}


def make(monkeypatch, default="eth0"):
    tc = collector_module.TrafficCollector()
    monkeypatch.setattr(tc, "_read_all_interfaces", lambda: dict(COUNTERS))
    monkeypatch.setattr(tc, "_get_default_interface", lambda: default)
    tc.pinned, tc.interface = "", default
    return tc


def test_list_skips_loopback_and_marks_current_and_default(monkeypatch):
    tc = make(monkeypatch)
    items = {i["name"]: i for i in tc.list_interfaces()}
    assert "lo" not in items and set(items) == {"eth0", "ens3", "docker0"}
    assert items["eth0"]["current"] and items["eth0"]["default"] and items["eth0"]["rx"] == 1000
    assert not items["ens3"]["current"]


def test_pin_and_unpin(monkeypatch):
    tc = make(monkeypatch)
    tc.set_interface("ens3")
    assert tc.pinned == "ens3" and tc.interface == "ens3"
    assert tc._read_net_dev() == (300, 400)
    tc.set_interface("")
    assert tc.pinned == "" and tc.interface == "eth0"


def test_interface_api(m, client, auth, post_headers, monkeypatch):
    tc = make(monkeypatch)
    monkeypatch.setattr(m, "collector_instance", tc)
    saved = []
    monkeypatch.setattr(m, "update_env_values", lambda updates: saved.append(updates))
    data = client.get("/api/interfaces", headers=auth).json()
    assert data["current"] == "eth0" and data["pinned"] == ""
    assert client.post("/api/interface", headers=post_headers, json={"interface": "ens3"}).json()["current"] == "ens3"
    assert saved[-1] == {"INTERFACE": "ens3"} and tc.pinned == "ens3"
    assert client.post("/api/interface", headers=post_headers, json={"interface": "nope0"}).status_code == 400
    assert client.post("/api/interface", headers=post_headers, json={"interface": ""}).json()["pinned"] == ""

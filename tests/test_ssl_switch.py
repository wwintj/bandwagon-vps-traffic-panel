"""Panel side of the certificate switcher: request/response with the root helper."""
import json
import threading
import time

import pytest


@pytest.fixture
def certctl(m, tmp_path, monkeypatch):
    (tmp_path / "requests").mkdir()
    monkeypatch.setattr(m, "CERTCTL_DIR", str(tmp_path))
    monkeypatch.setattr(m, "CERTCTL_PICKUP_SECONDS", 1.5)
    monkeypatch.setattr(m, "HTTPS_MODE", "nginx")
    monkeypatch.setattr(m, "PANEL_DOMAIN", "panel.test")
    monkeypatch.setattr(m, "SSL_CERTFILE", "/old/fullchain.pem")
    monkeypatch.setattr(m, "get_ssl_status", lambda force=False: {"enabled": True, "mode": "nginx"})
    env_updates = []
    monkeypatch.setattr(m, "update_env_values", lambda updates: env_updates.append(updates))
    m._env_updates = env_updates
    return tmp_path


def fake_helper(ctl, respond):
    """Stand-in for the root helper: answer the next request with respond(request)."""
    stop = threading.Event()

    def loop():
        req = ctl / "requests" / "request.json"
        while not stop.is_set():
            if req.exists():
                request = json.loads(req.read_text())
                req.unlink()
                (ctl / "result.json").write_text(json.dumps({"id": request["id"], "state": "running"}))
                payload = dict({"id": request["id"], "state": "done"}, **respond(request))
                (ctl / "result.json").write_text(json.dumps(payload))
            time.sleep(0.05)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return stop


CANDIDATE = {"id": "a" * 16, "cert": "/etc/letsencrypt/live/panel.test/fullchain.pem",
             "key": "/etc/letsencrypt/live/panel.test/privkey.pem", "subject": "panel.test", "usable": True}


def test_scan_returns_candidates(certctl, client, post_headers):
    stop = fake_helper(certctl, lambda r: {"ok": True, "candidates": [CANDIDATE]})
    try:
        r = client.post("/api/ssl/scan", headers=post_headers)
    finally:
        stop.set()
    assert r.status_code == 200
    assert r.json()["candidates"][0]["id"] == "a" * 16


def test_apply_success_records_new_paths(m, certctl, client, post_headers):
    seen = []
    stop = fake_helper(certctl, lambda r: (seen.append(r), {"ok": True, "applied": CANDIDATE})[1])
    try:
        r = client.post("/api/ssl/apply", headers=post_headers, json={"candidate": "a" * 16})
    finally:
        stop.set()
    assert r.status_code == 200
    assert seen[0] == {"id": seen[0]["id"], "action": "apply", "candidate": "a" * 16}
    assert m._env_updates[-1] == {"SSL_CERTFILE": CANDIDATE["cert"], "SSL_KEYFILE": CANDIDATE["key"]}
    assert m.SSL_CERTFILE == CANDIDATE["cert"]


def test_apply_failure_is_reported(certctl, client, post_headers):
    stop = fake_helper(certctl, lambda r: {"ok": False, "error": "Nginx 設定檢查失敗，已自動還原為原本的證書"})
    try:
        r = client.post("/api/ssl/apply", headers=post_headers, json={"candidate": "a" * 16})
    finally:
        stop.set()
    assert r.status_code == 400 and "還原" in r.json()["detail"]


def test_candidate_must_be_an_id_not_a_path(certctl, client, post_headers):
    r = client.post("/api/ssl/apply", headers=post_headers, json={"candidate": "/etc/shadow"})
    assert r.status_code == 400
    assert not (certctl / "requests" / "request.json").exists()


def test_helper_not_running(certctl, client, post_headers):
    r = client.post("/api/ssl/scan", headers=post_headers)
    assert r.status_code == 503 and "vps-panel update" in r.json()["detail"]


def test_helper_not_installed(m, certctl, client, post_headers, monkeypatch):
    monkeypatch.setattr(m, "CERTCTL_DIR", str(certctl / "missing"))
    r = client.post("/api/ssl/scan", headers=post_headers)
    assert r.status_code == 503 and "尚未啟用" in r.json()["detail"]


def test_scan_requires_https(m, certctl, client, post_headers, monkeypatch):
    monkeypatch.setattr(m, "HTTPS_MODE", "none")
    assert client.post("/api/ssl/scan", headers=post_headers).status_code == 400

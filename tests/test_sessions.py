import pytest
from fastapi.testclient import TestClient

from app import sessions

LOGIN = {"username": "admin", "password": "s3cretpass"}
CSRF = {"X-Panel-Request": "1"}


@pytest.fixture
def browser(m):
    """A fresh client with its own cookie jar and no Basic credentials, like a browser."""
    sessions.delete_all_except(None)
    m.AUTH_FAILURES.clear()
    return TestClient(m.app)


def test_unauthenticated_page_redirects_to_login(browser):
    r = browser.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"
    assert browser.get("/login").status_code == 200


def test_api_without_login_is_401_without_browser_popup(browser):
    r = browser.get("/api/summary")
    assert r.status_code == 401
    assert "www-authenticate" not in {k.lower() for k in r.headers}


def test_login_sets_a_safe_cookie_and_grants_access(browser):
    r = browser.post("/api/login", json=LOGIN, headers=CSRF)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "vps_panel_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert "max-age" not in cookie            # not remembered: browser-session cookie
    assert browser.get("/api/settings").status_code == 200
    assert browser.get("/", follow_redirects=False).status_code == 200
    assert browser.get("/login", follow_redirects=False).status_code == 302   # already logged in


def test_remember_me_sets_max_age(browser):
    r = browser.post("/api/login", json=dict(LOGIN, remember=True), headers=CSRF)
    assert "max-age=2592000" in r.headers["set-cookie"].lower()


def test_secure_flag_over_https(m):
    sessions.delete_all_except(None)
    client = TestClient(m.app, base_url="https://panel.test")
    r = client.post("/api/login", json=LOGIN, headers=CSRF)
    assert "secure" in r.headers["set-cookie"].lower()


def test_wrong_password_and_lockout(browser):
    assert browser.post("/api/login", json=dict(LOGIN, password="nope"), headers=CSRF).status_code == 401
    for i in range(10):
        browser.post("/api/login", json=dict(LOGIN, password=f"guess{i}"), headers=CSRF)
    assert browser.post("/api/login", json=LOGIN, headers=CSRF).status_code == 429


def test_login_requires_csrf_header(browser):
    assert browser.post("/api/login", json=LOGIN).status_code == 403


def test_logout_really_logs_out(browser):
    browser.post("/api/login", json=LOGIN, headers=CSRF)
    token = browser.cookies.get("vps_panel_session")
    assert browser.post("/api/logout", headers=CSRF).status_code == 200
    assert not sessions.is_valid(token)
    browser.cookies.set("vps_panel_session", token)   # replaying the old cookie does not work
    assert browser.get("/api/settings").status_code == 401


def test_session_expires(browser, monkeypatch):
    browser.post("/api/login", json=LOGIN, headers=CSRF)
    real_time = sessions.time.time
    monkeypatch.setattr(sessions.time, "time", lambda: real_time() + 13 * 3600)
    assert browser.get("/api/settings").status_code == 401


def test_password_change_signs_out_other_browsers(m, monkeypatch):
    sessions.delete_all_except(None)
    monkeypatch.setattr(m, "update_env_values", lambda updates: None)
    monkeypatch.setattr(m, "AUTH_PASSWORD", m.AUTH_PASSWORD)
    monkeypatch.setattr(m, "AUTH_PASSWORD_HASH", m.AUTH_PASSWORD_HASH)
    me, other = TestClient(m.app), TestClient(m.app)
    me.post("/api/login", json=LOGIN, headers=CSRF)
    other.post("/api/login", json=LOGIN, headers=CSRF)
    payload = {"auth_username": "admin", "auth_password": "newpass99", "panel_title": "t", "panel_subtitle": "s"}
    assert me.post("/api/settings", json=payload, headers=CSRF).status_code == 200
    assert me.get("/api/settings").status_code == 200
    assert other.get("/api/settings").status_code == 401
    m.VERIFIED_CREDENTIALS.clear()


def test_only_token_hash_is_stored(m, browser):
    browser.post("/api/login", json=LOGIN, headers=CSRF)
    token = browser.cookies.get("vps_panel_session")
    conn = m.get_db()
    rows = [r[0] for r in conn.execute("SELECT token_hash FROM sessions")]
    conn.close()
    assert token not in rows and len(rows) == 1


def test_basic_auth_still_works_for_scripts(client, auth):
    assert client.get("/api/summary", headers=auth).status_code == 200

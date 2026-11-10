import base64
import os


def basic(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_post_without_csrf_header_is_rejected(client, auth):
    assert client.post("/api/traffic/reset", headers=auth).status_code == 403


def test_post_with_csrf_header_is_allowed(client, post_headers):
    assert client.post("/api/traffic/reset", headers=post_headers).status_code == 200


def test_get_does_not_need_csrf_header(client, auth):
    assert client.get("/api/settings", headers=auth).status_code == 200


def test_secrets_are_masked(client, auth):
    tg = client.get("/api/telegram-settings", headers=auth).json()
    assert "ABCDEFGH" not in tg["telegram_bot_token"]
    assert "private" not in tg["bandwagon_api_key"]
    dd = client.get("/api/ddns-settings", headers=auth).json()
    assert "cf_token" not in dd["cloudflare_api_token"]
    bw = client.get("/api/bandwagon-settings", headers=auth).json()
    assert "private" not in bw["bandwagon_api_key"]


def test_masked_value_round_trip_keeps_real_secret(m, client, auth, post_headers, data_dir):
    tg = client.get("/api/telegram-settings", headers=auth).json()
    tg["telegram_enabled"] = False
    assert client.post("/api/telegram-settings", headers=post_headers, json=tg).status_code == 200
    assert m.TELEGRAM_BOT_TOKEN == "123456:ABCDEFGHIJKLMNOP"
    assert m.BANDWAGON_API_KEY == "private_bw_key_1234"
    env = open(os.path.join(data_dir, ".env"), encoding="utf-8").read()
    assert "TELEGRAM_BOT_TOKEN=123456:ABCDEFGHIJKLMNOP" in env


def test_new_secret_replaces_old(m, client, auth, post_headers):
    dd = client.get("/api/ddns-settings", headers=auth).json()
    original = m.CLOUDFLARE_API_TOKEN
    dd["cloudflare_api_token"] = "brand_new_token"
    client.post("/api/ddns-settings", headers=post_headers, json=dd)
    assert m.CLOUDFLARE_API_TOKEN == "brand_new_token"
    dd["cloudflare_api_token"] = original
    client.post("/api/ddns-settings", headers=post_headers, json=dd)


def test_env_write_is_atomic_and_private(m, data_dir):
    m.update_env_values({"PANEL_TITLE": "x"})
    path = os.path.join(data_dir, ".env")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert not [f for f in os.listdir(data_dir) if f.endswith(".tmp")]


def test_newline_cannot_inject_env_key(m, data_dir):
    m.update_env_values({"TELEGRAM_TIMEZONE_LABEL": "x\nAUTH_PASSWORD=pwned"})
    lines = open(os.path.join(data_dir, ".env"), encoding="utf-8").read().splitlines()
    assert [line for line in lines if line.startswith("AUTH_PASSWORD=")] == ["AUTH_PASSWORD=s3cretpass"]


def test_empty_password_locks_panel(m, client, monkeypatch):
    monkeypatch.setattr(m, "AUTH_PASSWORD", "")
    assert client.get("/api/settings", headers=basic("admin", "")).status_code == 503
    assert client.get("/api/settings", headers=basic("admin", "admin")).status_code == 503


def test_brute_force_is_locked_out(client, auth):
    for i in range(10):
        assert client.get("/api/settings", headers=basic("admin", f"guess{i}")).status_code == 401
    assert client.get("/api/settings", headers=basic("admin", "guess-more")).status_code == 429
    # Even the right password is refused during lockout.
    assert client.get("/api/settings", headers=auth).status_code == 429


def test_repeating_one_stale_password_does_not_lock_out(client, auth):
    for _ in range(30):
        assert client.get("/api/settings", headers=basic("admin", "old-password")).status_code == 401
    assert client.get("/api/settings", headers=auth).status_code == 200


def test_proxy_headers_only_trusted_from_localhost(m):
    class FakeRequest:
        def __init__(self, peer, headers):
            self.client = type("C", (), {"host": peer})()
            self.headers = headers

    assert m.get_client_ip(FakeRequest("203.0.113.9", {"x-forwarded-for": "6.6.6.6"})) == "203.0.113.9"
    assert m.get_client_ip(FakeRequest("127.0.0.1", {"x-real-ip": "198.51.100.7"})) == "198.51.100.7"
    assert m.get_client_ip(FakeRequest("127.0.0.1", {"x-forwarded-for": "6.6.6.6, 198.51.100.7"})) == "198.51.100.7"


def test_short_password_allowed_in_panel(m, client, post_headers, monkeypatch):
    monkeypatch.setattr(m, "update_env_values", lambda updates: None)
    monkeypatch.setattr(m, "AUTH_PASSWORD", m.AUTH_PASSWORD)
    monkeypatch.setattr(m, "AUTH_PASSWORD_HASH", m.AUTH_PASSWORD_HASH)
    payload = {"auth_username": "admin", "auth_password": "abc", "panel_title": "t", "panel_subtitle": "s"}
    assert client.post("/api/settings", headers=post_headers, json=payload).status_code == 200
    assert m.passwords.verify_password("abc", m.AUTH_PASSWORD_HASH)
    m.VERIFIED_CREDENTIALS.clear()


def test_password_with_unsafe_characters_rejected(client, post_headers):
    payload = {"auth_username": "admin", "auth_password": "a b$c", "panel_title": "t", "panel_subtitle": "s"}
    assert client.post("/api/settings", headers=post_headers, json=payload).status_code == 400


def test_static_and_index(client, auth):
    css = client.get("/static/panel.css")
    assert css.status_code == 200 and len(css.content) < 60_000   # prebuilt, not the 450 KB runtime
    page = client.get("/", headers=auth)
    assert page.status_code == 200
    assert "v" + __import__("app").__version__ in page.text

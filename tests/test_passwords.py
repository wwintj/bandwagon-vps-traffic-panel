import base64
import os

from app import passwords


def basic(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_hash_roundtrip_and_format():
    h = passwords.hash_password("s3cret")
    assert h.startswith("pbkdf2_sha256:") and "$" not in h
    assert passwords.verify_password("s3cret", h)
    assert not passwords.verify_password("wrong", h)
    assert not passwords.verify_password("s3cret", "garbage")
    assert passwords.hash_password("s3cret") != h   # salted


def test_plaintext_password_is_migrated(m, monkeypatch, data_dir):
    env_path = os.path.join(data_dir, ".env")
    original_env = open(env_path, encoding="utf-8").read()
    try:
        _check_migration(m, monkeypatch, data_dir)
    finally:
        with open(env_path, "w", encoding="utf-8") as handle:   # other tests share this .env
            handle.write(original_env)


def _check_migration(m, monkeypatch, data_dir):
    monkeypatch.setattr(m, "AUTH_PASSWORD", "s3cretpass")
    monkeypatch.setattr(m, "AUTH_PASSWORD_HASH", "")
    assert m.migrate_plaintext_password() is True
    env = open(os.path.join(data_dir, ".env"), encoding="utf-8").read()
    assert "AUTH_PASSWORD=" not in env.replace("AUTH_PASSWORD_HASH=", "")
    assert "s3cretpass" not in env and "AUTH_PASSWORD_HASH=pbkdf2_sha256:" in env
    assert m.AUTH_PASSWORD == "" and passwords.verify_password("s3cretpass", m.AUTH_PASSWORD_HASH)
    assert m.migrate_plaintext_password() is False   # idempotent


def test_login_works_with_hash_and_uses_cache(m, client, monkeypatch):
    monkeypatch.setattr(m, "AUTH_PASSWORD", "")
    monkeypatch.setattr(m, "AUTH_PASSWORD_HASH", passwords.hash_password("hashed-pass"))
    m.VERIFIED_CREDENTIALS.clear()
    calls = []
    real = passwords.verify_password
    monkeypatch.setattr(m.passwords, "verify_password", lambda p, h: calls.append(1) or real(p, h))
    for _ in range(5):
        assert client.get("/api/settings", headers=basic("admin", "hashed-pass")).status_code == 200
    assert len(calls) == 1   # PBKDF2 runs once, then the in-memory cache answers
    assert client.get("/api/settings", headers=basic("admin", "nope")).status_code == 401
    m.VERIFIED_CREDENTIALS.clear()
    m.AUTH_FAILURES.clear()


def test_password_change_stores_only_hash(m, client, post_headers, monkeypatch):
    saved = []
    monkeypatch.setattr(m, "update_env_values", lambda updates: saved.append(updates))
    monkeypatch.setattr(m, "AUTH_PASSWORD", m.AUTH_PASSWORD)
    monkeypatch.setattr(m, "AUTH_PASSWORD_HASH", m.AUTH_PASSWORD_HASH)
    payload = {"auth_username": "admin", "auth_password": "brandnew", "panel_title": "t", "panel_subtitle": "s"}
    assert client.post("/api/settings", headers=post_headers, json=payload).status_code == 200
    assert saved[-1]["AUTH_PASSWORD"] is None and passwords.verify_password("brandnew", saved[-1]["AUTH_PASSWORD_HASH"])
    assert client.get("/api/settings", headers=basic("admin", "brandnew")).status_code == 200
    m.VERIFIED_CREDENTIALS.clear()


def test_env_value_none_removes_key(m, data_dir):
    m.update_env_values({"TEMP_KEY": "x"})
    assert "TEMP_KEY=x" in open(os.path.join(data_dir, ".env")).read()
    m.update_env_values({"TEMP_KEY": None})
    assert "TEMP_KEY" not in open(os.path.join(data_dir, ".env")).read()

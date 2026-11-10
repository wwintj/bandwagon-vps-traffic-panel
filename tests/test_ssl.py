import shutil
import socket
import ssl
import subprocess
import threading
from datetime import datetime

import pytest

from app import sslinfo

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI not available")


@pytest.fixture(scope="module")
def tls_server(tmp_path_factory):
    """A real local HTTPS endpoint serving a self-signed certificate for panel.test."""
    d = tmp_path_factory.mktemp("cert")
    cert, key = d / "cert.pem", d / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30",
         "-subj", "/CN=panel.test", "-addext", "subjectAltName=DNS:panel.test,DNS:www.panel.test",
         "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    stop = threading.Event()

    def serve():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            try:
                with ctx.wrap_socket(conn, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass  # the strict client aborts on the self-signed cert

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield listener.getsockname()[1]
    stop.set()
    thread.join(timeout=2)
    listener.close()


def test_reads_served_certificate(tls_server):
    info = sslinfo.inspect_tls("127.0.0.1", tls_server, "panel.test")
    assert info["reachable"] is True
    assert info["subject"] == "panel.test"
    assert set(info["san"]) == {"panel.test", "www.panel.test"}
    assert 28 <= info["days_left"] <= 30
    # self-signed: readable, but a browser would not trust it
    assert info["trusted"] is False
    assert "自簽" in info["error"]


def test_unreachable_port_is_reported():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    info = sslinfo.inspect_tls("127.0.0.1", free_port, "panel.test", timeout=1)
    assert info["reachable"] is False and "無法連線" in info["error"]


def test_http_mode_reports_disabled(m, monkeypatch):
    monkeypatch.setattr(m, "HTTPS_MODE", "none")
    status = m.get_ssl_status(force=True)
    assert status["enabled"] is False


def test_nginx_mode_inspects_own_https_port(m, monkeypatch, tls_server):
    monkeypatch.setattr(m, "HTTPS_MODE", "nginx")
    monkeypatch.setattr(m, "HTTPS_PORT", str(tls_server))
    monkeypatch.setattr(m, "PANEL_DOMAIN", "panel.test")
    status = m.get_ssl_status(force=True)
    assert status["enabled"] and status["port"] == tls_server
    assert status["subject"] == "panel.test" and status["domain"] == "panel.test"


def test_ssl_status_api(m, client, auth, monkeypatch):
    monkeypatch.setattr(m, "HTTPS_MODE", "none")
    data = client.get("/api/ssl-status?refresh=1", headers=auth).json()
    assert data["enabled"] is False and "ssl_expiry_notify" in data


# ---- reminders ---------------------------------------------------------------

@pytest.fixture
def reminders(m, monkeypatch):
    monkeypatch.setattr(m, "SSL_EXPIRY_NOTIFY", "1")
    monkeypatch.setattr(m, "TELEGRAM_PUSH_HOUR", 20)
    monkeypatch.setattr(m, "TELEGRAM_PUSH_MINUTE", 0)
    monkeypatch.setattr(m, "TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "chat")
    m.set_meta_value("ssl_reminder_cert", "")
    m.set_meta_value("ssl_reminder_sent", "")
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda text: sent.append(text))
    m._sent = sent
    return m


def cert(days, not_after="2027-01-24"):
    return {"enabled": True, "days_left": days, "not_after": not_after, "domain": "ssrchina.net", "issuer": "Let's Encrypt / YE1"}


def test_ssl_reminder_schedule(reminders):
    at = lambda day: datetime(2027, 1, day, 21)  # noqa: E731
    results = [reminders.check_ssl_expiry_reminder(at(d), cert(24 - d)) for d in range(1, 26)]
    reminded = [24 - d for d, r in zip(range(1, 26), results) if r]
    assert reminded == [14, 7, 3, 2, 1, 0, -1]
    assert "剩 14 天" in reminders._sent[0] and "自動續簽" in reminders._sent[0]
    assert "已過期 1 天" in reminders._sent[-1]


def test_ssl_reminder_respects_push_time_and_switch(reminders, monkeypatch):
    assert reminders.check_ssl_expiry_reminder(datetime(2027, 1, 10, 8), cert(10)) is None
    monkeypatch.setattr(reminders, "SSL_EXPIRY_NOTIFY", "0")
    assert reminders.check_ssl_expiry_reminder(datetime(2027, 1, 10, 21), cert(10)) is None
    assert reminders._sent == []


def test_ssl_reminder_resets_after_renewal(reminders):
    assert reminders.check_ssl_expiry_reminder(datetime(2027, 1, 12, 21), cert(12)) == "14"
    # renewed: new certificate with 90 days left, nothing to send
    assert reminders.check_ssl_expiry_reminder(datetime(2027, 1, 13, 21), cert(90, "2027-04-13")) is None
    # its own schedule starts fresh later
    assert reminders.check_ssl_expiry_reminder(datetime(2027, 3, 31, 21), cert(13, "2027-04-13")) == "14"


def test_ssl_reminder_ignores_http_mode(reminders):
    assert reminders.check_ssl_expiry_reminder(datetime(2027, 1, 10, 21), {"enabled": False}) is None

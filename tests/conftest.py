import os
import sys
import tempfile

import pytest

# Point .env / data.db at a throwaway directory before the app is imported.
_DATA_DIR = tempfile.mkdtemp(prefix="vps-panel-test-")
os.environ["VPS_PANEL_DATA_DIR"] = _DATA_DIR
with open(os.path.join(_DATA_DIR, ".env"), "w", encoding="utf-8") as f:
    f.write(
        "AUTH_USERNAME=admin\n"
        "AUTH_PASSWORD=s3cretpass\n"
        "TELEGRAM_BOT_TOKEN=123456:ABCDEFGHIJKLMNOP\n"
        "CLOUDFLARE_API_TOKEN=cf_token_value_xyz9\n"
        "BANDWAGON_API_KEY=private_bw_key_1234\n"
        "TELEGRAM_TIMEZONE_OFFSET=8\n"
    )
for key in ("AUTH_USERNAME", "AUTH_PASSWORD", "TELEGRAM_BOT_TOKEN", "CLOUDFLARE_API_TOKEN", "BANDWAGON_API_KEY", "BANDWAGON_VEID"):
    os.environ.pop(key, None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app.main as main_module  # noqa: E402,F401  (imports the whole app)
from appproxy import AppProxy  # noqa: E402


@pytest.fixture(scope="session")
def data_dir():
    return _DATA_DIR


APP = AppProxy()


@pytest.fixture
def m(monkeypatch):
    # No network in tests.
    monkeypatch.setattr(APP, "fetch_bandwagon_info_cached", lambda force=False: None)
    monkeypatch.setattr(APP, "fetch_bandwagon_info", lambda: None)
    monkeypatch.setattr(APP, "get_cached_public_ip_info", lambda force=False: ("1.2.3.4", {}))
    monkeypatch.setattr(APP, "lookup_ip_info", lambda ip: {})
    APP.init_db()
    APP.AUTH_FAILURES.clear()
    return APP


@pytest.fixture
def client(m):
    from fastapi.testclient import TestClient
    return TestClient(m.app)


@pytest.fixture
def auth():
    import base64
    return {"Authorization": "Basic " + base64.b64encode(b"admin:s3cretpass").decode()}


@pytest.fixture
def post_headers(auth):
    return {**auth, "X-Panel-Request": "1"}

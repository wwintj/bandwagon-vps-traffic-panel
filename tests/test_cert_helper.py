import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI not available")

HELPER_PATH = Path(__file__).resolve().parent.parent / "scripts" / "cert_helper.py"


@pytest.fixture(scope="module")
def helper():
    spec = importlib.util.spec_from_file_location("cert_helper", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_cert(cert, key, cn, sans=None, days=30):
    cert.parent.mkdir(parents=True, exist_ok=True)
    san = ",".join(f"DNS:{n}" for n in (sans or [cn]))
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", str(days), "-subj", f"/CN={cn}",
         "-addext", f"subjectAltName={san}", "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    root = tmp_path_factory.mktemp("scan")
    make_cert(root / "good" / "fullchain.pem", root / "good" / "privkey.pem", "panel.test")
    make_cert(root / "wild" / "cert.pem", root / "wild" / "key.pem", "*.panel.test")
    make_cert(root / "other" / "cert.crt", root / "other" / "private.key", "other.test")
    # mismatched pair: certificate from one key, private key from another
    make_cert(root / "tmp1" / "c.pem", root / "tmp1" / "k.pem", "panel.test")
    make_cert(root / "tmp2" / "c.pem", root / "tmp2" / "k.pem", "panel.test")
    (root / "bad").mkdir()
    shutil.copy(root / "tmp1" / "c.pem", root / "bad" / "server.crt")
    shutil.copy(root / "tmp2" / "k.pem", root / "bad" / "server.key")
    shutil.rmtree(root / "tmp1")
    shutil.rmtree(root / "tmp2")
    # a CA bundle with no key next to it is not a candidate
    (root / "ca").mkdir()
    shutil.copy(root / "other" / "cert.crt", root / "ca" / "bundle.pem")
    return root


def env(root, mode="nginx", domain="panel.test", current=""):
    return {"HTTPS_MODE": mode, "PANEL_DOMAIN": domain, "HTTPS_PORT": "8443", "PORT": "8088",
            "SSL_CERTFILE": current or str(root / "other" / "cert.crt")}


def by_dir(candidates, name):
    return next(c for c in candidates if f"/{name}/" in c["cert"])


def test_discover_pairs_cert_with_key_in_same_folder(helper, certs):
    found = {(Path(c).parent.name, Path(c).name, Path(k).name) for c, k, _ in helper.discover([str(certs)], extra_sources=False)}
    assert found == {
        ("good", "fullchain.pem", "privkey.pem"),
        ("wild", "cert.pem", "key.pem"),
        ("other", "cert.crt", "private.key"),
        ("bad", "server.crt", "server.key"),
    }


def test_scan_flags_each_candidate(helper, certs):
    result = helper.scan(env(certs), [str(certs)], extra_sources=False)
    good, wild, other, bad = (by_dir(result, n) for n in ("good", "wild", "other", "bad"))
    assert good["usable"] and good["matches_domain"] and good["key_ok"] and good["self_signed"]
    assert other["current"] and "證書不包含 panel.test" in other["problems"]
    assert "私鑰與證書不配對" in bad["problems"] and not bad["usable"]
    assert wild["matches_domain"] is False  # *.panel.test does not cover the apex
    assert result[0]["current"]             # in-use certificate is listed first
    assert all(len(c["id"]) == 16 for c in result)


def test_domain_matching(helper):
    assert helper.domain_matches("vps.panel.test", ["*.panel.test"])
    assert not helper.domain_matches("a.b.panel.test", ["*.panel.test"])
    assert not helper.domain_matches("panel.test", ["*.panel.test"])
    assert helper.domain_matches("Panel.Test.", ["panel.test"])


def test_rewrite_nginx_conf(helper):
    conf = "server {\n    listen 8443 ssl;\n    ssl_certificate /old/c.pem;\n    ssl_certificate_key /old/k.pem;\n}\n"
    out = helper.rewrite_nginx_conf(conf, "/new/fullchain.pem", "/new/privkey.pem")
    assert "ssl_certificate /new/fullchain.pem;" in out and "ssl_certificate_key /new/privkey.pem;" in out
    assert "/old/" not in out
    with pytest.raises(ValueError):
        helper.rewrite_nginx_conf("server { listen 80; }", "/a", "/b")


def test_take_request_rejects_symlinks_oversize_and_bad_ids(helper, tmp_path):
    req_dir = tmp_path / "requests"
    req_dir.mkdir()
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"id": "abc", "action": "scan"}))
    os.symlink(secret, req_dir / "request.json")
    assert helper.take_request(str(req_dir)) is None
    assert not (req_dir / "request.json").exists() and secret.exists()   # link removed, target untouched

    (req_dir / "request.json").write_text("x" * 5000)
    assert helper.take_request(str(req_dir)) is None

    (req_dir / "request.json").write_text(json.dumps({"id": "../../etc", "action": "scan"}))
    assert helper.take_request(str(req_dir)) is None

    (req_dir / "request.json").write_text(json.dumps({"id": "ok_1", "action": "scan"}))
    assert helper.take_request(str(req_dir)) == {"id": "ok_1", "action": "scan"}
    assert not (req_dir / "request.json").exists()


def install_dir_with_env(tmp_path, values):
    d = tmp_path / "install"
    d.mkdir()
    (d / ".env").write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return d


def test_process_scan_writes_result(helper, certs, tmp_path):
    ctl = tmp_path / "ctl"
    (ctl / "requests").mkdir(parents=True)
    install = install_dir_with_env(tmp_path, env(certs))
    (ctl / "requests" / "request.json").write_text(json.dumps({"id": "r1", "action": "scan"}))
    helper.process(str(install), str(ctl), scan_roots=[str(certs)], extra_sources=False)
    result = json.loads((ctl / "result.json").read_text())
    assert result["id"] == "r1" and result["state"] == "done" and result["ok"]
    assert len(result["candidates"]) == 4


@pytest.fixture
def nginx(helper, tmp_path, monkeypatch):
    conf = tmp_path / "panel.conf"
    conf.write_text("server {\n    ssl_certificate /old/c.pem;\n    ssl_certificate_key /old/k.pem;\n}\n")
    monkeypatch.setattr(helper, "NGINX_CONF", str(conf))
    monkeypatch.setattr(helper, "LIB_DIR", str(tmp_path / "nolib"))
    calls = []
    outcome = {"nginx_t": 0, "served": True}

    class Done:
        def __init__(self, code, err=""):
            self.returncode, self.stdout, self.stderr = code, "", err

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        if cmd[:2] == ["nginx", "-t"]:
            return Done(outcome["nginx_t"], "nginx: [emerg] bad certificate")
        return Done(0)

    monkeypatch.setattr(helper, "run", fake_run)
    monkeypatch.setattr(helper, "wait_for_fingerprint", lambda port, sni, fp, seconds: outcome["served"])
    return conf, calls, outcome


def test_apply_switches_nginx_and_verifies(helper, certs, nginx):
    conf, calls, _ = nginx
    good = by_dir(helper.scan(env(certs), [str(certs)], False), "good")
    applied = helper.apply(env(certs), good["id"], "/unused", [str(certs)], False)
    assert applied["cert"].endswith("good/fullchain.pem")
    assert f"ssl_certificate {good['cert']};" in conf.read_text()
    assert ["systemctl", "reload", "nginx"] in calls


def test_apply_rolls_back_when_nginx_rejects(helper, certs, nginx):
    conf, _, outcome = nginx
    before = conf.read_text()
    outcome["nginx_t"] = 1
    good = by_dir(helper.scan(env(certs), [str(certs)], False), "good")
    with pytest.raises(ValueError, match="還原"):
        helper.apply(env(certs), good["id"], "/unused", [str(certs)], False)
    assert conf.read_text() == before


def test_apply_rolls_back_when_new_cert_is_not_served(helper, certs, nginx):
    conf, _, outcome = nginx
    before = conf.read_text()
    outcome["served"] = False
    good = by_dir(helper.scan(env(certs), [str(certs)], False), "good")
    with pytest.raises(ValueError, match="驗證失敗"):
        helper.apply(env(certs), good["id"], "/unused", [str(certs)], False)
    assert conf.read_text() == before


@pytest.mark.parametrize("pick, message", [
    ("bad", "私鑰與證書不配對"),
    ("wild", "不包含"),
    ("other", "已經在使用中"),
])
def test_apply_refuses_unusable_candidates(helper, certs, nginx, pick, message):
    conf, _, _ = nginx
    before = conf.read_text()
    target = by_dir(helper.scan(env(certs), [str(certs)], False), pick)
    with pytest.raises(ValueError, match=message):
        helper.apply(env(certs), target["id"], "/unused", [str(certs)], False)
    assert conf.read_text() == before


def test_apply_refuses_unknown_id_and_http_mode(helper, certs, nginx):
    with pytest.raises(ValueError, match="找不到"):
        helper.apply(env(certs), "0" * 16, "/unused", [str(certs)], False)
    with pytest.raises(ValueError, match="沒有啟用 HTTPS"):
        helper.apply(env(certs, mode="none"), "0" * 16, "/unused", [str(certs)], False)


def test_paths_with_unsafe_characters_are_not_usable(helper, tmp_path):
    root = tmp_path / "space dir"
    make_cert(root / "fullchain.pem", root / "privkey.pem", "panel.test")
    result = helper.scan(env(root, current="/nope"), [str(root)], False)
    assert result and not result[0]["usable"] and "路徑含有不支援的字元" in result[0]["problems"]

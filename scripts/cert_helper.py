#!/usr/bin/env python3
"""Root-side certificate helper for VPS Traffic Panel.

The panel runs unprivileged. To list the certificates on this server or switch the one the panel
serves, it drops a small JSON request into REQUEST_DIR; a systemd path unit runs
`cert_helper.py process` as root, which writes the outcome to RESULT_FILE.

Everything in a request is untrusted: the panel can only pick a candidate *id* from a fresh scan
done here, never a file path, and every file is opened without following symlinks in directories
the panel user controls. Only the Python standard library is used (system python3, no venv).

  cert_helper.py process --install-dir /opt/vps-traffic-panel
  cert_helper.py scan    --install-dir /opt/vps-traffic-panel     (debug: print candidates)
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

CERTCTL_DIR = os.environ.get("VPS_PANEL_CERTCTL_DIR", "/var/lib/vps-traffic-panel-certctl")
REQUEST_NAME = "request.json"
NGINX_CONF = os.environ.get("VPS_PANEL_NGINX_CONF", "/etc/nginx/conf.d/vps-traffic-panel.conf")
LIB_DIR = os.environ.get("VPS_PANEL_LIB_DIR", "/usr/local/lib/vps-traffic-panel")
SERVICE_NAME = "vps-traffic-panel"

LETSENCRYPT_LIVE = "/etc/letsencrypt/live"
NGINX_DIR = "/etc/nginx"
XUI_DIRS = ("/etc/x-ui", "/usr/local/x-ui", "/usr/local/etc/x-ui")
SCAN_ROOTS = (
    "/etc/x-ui", "/usr/local/x-ui", "/usr/local/etc/x-ui", "/root", "/root/cert", "/root/.acme.sh",
    "/opt", "/usr/local/etc", "/var/lib", "/etc/ssl", "/etc/v2ray-agent/tls", "/etc/hysteria",
    "/etc/sing-box", "/etc/trojan",
)
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "proc", "docker", "containerd"}
MAX_FILES = 20000
# Paths go into an Nginx config line; keep them to characters that cannot break it.
SAFE_PATH = re.compile(r"^/[A-Za-z0-9._/@+=,~-]+$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------- files

def read_text_nofollow(path, limit):
    """Read a small regular file without following a symlink at the last path component."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f"refusing to read {path}")
        return os.read(fd, limit).decode("utf-8", "replace")
    finally:
        os.close(fd)


def write_atomic(path, text, mode=0o644):
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_env(install_dir):
    values = {}
    try:
        text = read_text_nofollow(os.path.join(install_dir, ".env"), 65536)
    except (OSError, ValueError):
        return values
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            values[key.strip()] = value
    return values


# ---------------------------------------------------------------------------- certificates

def first_pem_der(path):
    # Candidate paths come from this helper's own scan (e.g. Let's Encrypt live/ symlinks), so
    # following symlinks is intended here; panel-controlled files use read_text_nofollow.
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read(1024 * 1024)
    except OSError:
        return None
    match = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", text, re.S)
    if not match:
        return None
    try:
        return ssl.PEM_cert_to_DER_cert(match.group(0))
    except ValueError:
        return None


def decode_cert(path):
    der = first_pem_der(path)
    if not der:
        return None, None
    fd, tmp = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(ssl.DER_cert_to_PEM_cert(der))
        return ssl._ssl._test_decode_cert(tmp), der
    except Exception:
        return None, der
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _name(entries, key):
    for rdn in entries or ():
        for k, v in rdn:
            if k == key:
                return v
    return ""


def cert_names(info):
    names = [value for kind, value in info.get("subjectAltName", ()) if kind == "DNS"]
    common = _name(info.get("subject"), "commonName")
    if common and common not in names:
        names.insert(0, common)
    return names


def domain_matches(domain, names):
    domain = (domain or "").lower().rstrip(".")
    for name in names:
        name = name.lower().rstrip(".")
        if name == domain:
            return True
        if name.startswith("*.") and "." in domain and domain.split(".", 1)[1] == name[2:]:
            return True
    return False


def key_matches(cert, key):
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # A password callback stops OpenSSL from prompting on a terminal for encrypted keys.
        context.load_cert_chain(cert, key, password=lambda: b"")
        return True
    except (ssl.SSLError, OSError, ValueError):
        return False


# ---------------------------------------------------------------------------- discovery

def _walk_files(root, max_depth):
    if not os.path.isdir(root):
        return
    base_depth = root.rstrip("/").count("/")
    seen = 0
    for current, dirs, files in os.walk(root):
        depth = current.rstrip("/").count("/") - base_depth
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and depth < max_depth - 1]
        for name in files:
            seen += 1
            if seen > MAX_FILES:
                return
            yield os.path.join(current, name)


def discover(scan_roots=None, extra_sources=True):
    """Yield (cert, key, label) the same way the installer searches."""
    found = []

    def add(cert, key, label):
        if cert and key and os.path.isfile(cert) and os.path.isfile(key):
            found.append((cert, key, label))

    if extra_sources:
        if os.path.isdir(LETSENCRYPT_LIVE):
            for name in sorted(os.listdir(LETSENCRYPT_LIVE)):
                live = os.path.join(LETSENCRYPT_LIVE, name)
                if os.path.isdir(live):
                    add(os.path.join(live, "fullchain.pem"), os.path.join(live, "privkey.pem"), f"Let's Encrypt：{name}")

        for path in sorted(_walk_files(NGINX_DIR, 4)):
            if not (path.endswith(".conf") or "/sites-enabled/" in path or "/sites-available/" in path):
                continue
            try:
                text = open(path, encoding="utf-8", errors="replace").read(256 * 1024)
            except OSError:
                continue
            cert = re.search(r"^\s*ssl_certificate\s+([^;\s]+)\s*;", text, re.M)
            key = re.search(r"^\s*ssl_certificate_key\s+([^;\s]+)\s*;", text, re.M)
            if cert and key:
                add(cert.group(1), key.group(1), f"Nginx 設定：{path}")

        for root in XUI_DIRS:
            for path in _walk_files(root, 3):
                if not path.endswith((".db", ".sqlite")):
                    continue
                cert = key = ""
                try:
                    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
                    for setting_key, value in conn.execute("SELECT key, value FROM settings"):
                        if "cert" in str(setting_key).lower():
                            cert = str(value)
                        elif "key" in str(setting_key).lower():
                            key = str(value)
                    conn.close()
                except sqlite3.Error:
                    continue
                add(cert, key, f"x-ui 資料庫：{path}")

    key_names = ("privkey.pem", "private.key", "private.pem", "key.pem", "server.key", "cert.key", "ssl.key")
    for root in (scan_roots if scan_roots is not None else SCAN_ROOTS):
        for path in sorted(_walk_files(root, 5)):
            name = os.path.basename(path)
            if not name.endswith((".crt", ".cer", ".pem")) or "key" in name or "priv" in name:
                continue
            folder = os.path.dirname(path)
            options = [os.path.join(folder, k) for k in key_names]
            options.append(os.path.join(folder, os.path.basename(folder) + ".key"))
            try:
                listing = sorted(os.listdir(folder))
            except OSError:
                listing = []
            options += [os.path.join(folder, n) for n in listing if n.endswith(".key") or ("key" in n and n.endswith(".pem"))]
            for key in options:
                if os.path.isfile(key):
                    add(path, key, f"自動搜尋：{folder}")
                    break

    unique, seen = [], set()
    for cert, key, label in found:
        if (cert, key) in seen:
            continue
        seen.add((cert, key))
        unique.append((cert, key, label))
    return unique


def candidate_id(cert, key):
    return hashlib.sha256(f"{cert}\0{key}".encode()).hexdigest()[:16]


def describe(cert, key, label, domain, current_cert):
    info, der = decode_cert(cert)
    if not info:
        return None  # not a server certificate (CA bundle, garbage, unreadable)
    names = cert_names(info)
    expires = None
    if info.get("notAfter"):
        expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(info["notAfter"]), timezone.utc)
    days_left = (expires - now()).days if expires else None
    expired = expires is not None and expires <= now()
    subject = _name(info.get("subject"), "commonName")
    issuer = " / ".join(p for p in (_name(info.get("issuer"), "organizationName"), _name(info.get("issuer"), "commonName")) if p)
    self_signed = info.get("subject") == info.get("issuer")
    key_ok = key_matches(cert, key)
    matches = domain_matches(domain, names) if domain else None
    safe = bool(SAFE_PATH.match(cert) and SAFE_PATH.match(key))

    problems = []
    if not key_ok:
        problems.append("私鑰與證書不配對")
    if expired:
        problems.append("證書已過期")
    if matches is False:
        problems.append(f"證書不包含 {domain}")
    if not safe:
        problems.append("路徑含有不支援的字元")
    current = bool(current_cert) and (cert == current_cert or os.path.realpath(cert) == os.path.realpath(current_cert))
    return {
        "id": candidate_id(cert, key),
        "cert": cert,
        "key": key,
        "label": label,
        "subject": subject,
        "names": names,
        "issuer": issuer,
        "self_signed": self_signed,
        "not_after": expires.date().isoformat() if expires else "",
        "days_left": days_left,
        "matches_domain": matches,
        "key_ok": key_ok,
        "current": current,
        "usable": not problems,
        "problems": problems,
        "fingerprint": hashlib.sha256(der).hexdigest() if der else "",
    }


def scan(env, scan_roots=None, extra_sources=True):
    domain = env.get("PANEL_DOMAIN", "") if env.get("HTTPS_MODE") == "nginx" else ""
    current = env.get("SSL_CERTFILE", "")
    result = []
    for cert, key, label in discover(scan_roots, extra_sources):
        item = describe(cert, key, label, domain, current)
        if item:
            result.append(item)
    result.sort(key=lambda c: (not c["current"], not c["usable"], -(c["days_left"] if c["days_left"] is not None else -10**6)))
    return result


# ---------------------------------------------------------------------------- applying

def run(command, timeout=60):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def served_fingerprint(port, server_name):
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", int(port)), timeout=5) as sock:
        with context.wrap_socket(sock, server_hostname=server_name or None) as tls:
            return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()


def wait_for_fingerprint(port, server_name, expected, seconds):
    deadline = time.time() + seconds
    last = ""
    while time.time() < deadline:
        try:
            last = served_fingerprint(port, server_name)
            if last == expected:
                return True
        except (OSError, ssl.SSLError):
            pass
        time.sleep(1)
    return False


def rewrite_nginx_conf(text, cert, key):
    new, n_cert = re.subn(r"(^\s*ssl_certificate\s+)[^;\n]+;", lambda m: f"{m.group(1)}{cert};", text, flags=re.M)
    new, n_key = re.subn(r"(^\s*ssl_certificate_key\s+)[^;\n]+;", lambda m: f"{m.group(1)}{key};", new, flags=re.M)
    if n_cert != 1 or n_key != 1:
        raise ValueError("面板的 Nginx 設定格式不符，無法自動切換（請用 vps-panel 重新安裝）")
    return new


def apply_nginx(candidate, env):
    if not os.path.isfile(NGINX_CONF) or os.path.islink(NGINX_CONF):
        raise ValueError(f"找不到面板的 Nginx 設定 {NGINX_CONF}")
    original = open(NGINX_CONF, encoding="utf-8").read()
    updated = rewrite_nginx_conf(original, candidate["cert"], candidate["key"])
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    write_atomic(f"{NGINX_CONF}.{stamp}.bak", original)
    write_atomic(NGINX_CONF, updated)

    def restore(reason):
        write_atomic(NGINX_CONF, original)
        run(["nginx", "-t"])
        run(["systemctl", "reload", "nginx"])
        raise ValueError(f"{reason}，已自動還原為原本的證書")

    test = run(["nginx", "-t"])
    if test.returncode != 0:
        restore("Nginx 設定檢查失敗：" + (test.stderr or test.stdout).strip().splitlines()[-1][:200])
    reload = run(["systemctl", "reload", "nginx"])
    if reload.returncode != 0:
        restore("Nginx 重新載入失敗")
    port = int(env.get("HTTPS_PORT") or 443)
    if not wait_for_fingerprint(port, env.get("PANEL_DOMAIN"), candidate["fingerprint"], 15):
        restore("套用後驗證失敗：HTTPS 端口送出的不是新證書")


def apply_uvicorn(candidate, env, install_dir):
    # In direct-HTTPS mode the panel itself runs as root, so its .env is root-owned; refuse otherwise.
    env_path = os.path.join(install_dir, ".env")
    info = os.lstat(env_path)
    if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or os.stat(install_dir).st_uid != 0:
        raise ValueError("直連 HTTPS 模式的 .env 應由 root 擁有，已拒絕修改")
    original = read_text_nofollow(env_path, 65536)
    replacements = {"SSL_CERTFILE": candidate["cert"], "SSL_KEYFILE": candidate["key"]}
    lines, seen = [], set()
    for line in original.splitlines():
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in replacements:
            line = f"{key}={replacements[key]}"
            seen.add(key)
        lines.append(line)
    for key, value in replacements.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    write_atomic(env_path, "\n".join(lines) + "\n", 0o600)
    run(["systemctl", "restart", SERVICE_NAME])
    if not wait_for_fingerprint(int(env.get("PORT") or 8088), None, candidate["fingerprint"], 30):
        write_atomic(env_path, original, 0o600)
        run(["systemctl", "restart", SERVICE_NAME])
        raise ValueError("套用後驗證失敗：面板沒有送出新證書，已自動還原為原本的證書")


def apply(env, wanted_id, install_dir, scan_roots=None, extra_sources=True):
    mode = env.get("HTTPS_MODE")
    if mode not in ("nginx", "uvicorn"):
        raise ValueError("面板目前沒有啟用 HTTPS，請用 vps-panel 重新安裝並選擇證書")
    candidate = next((c for c in scan(env, scan_roots, extra_sources) if c["id"] == wanted_id), None)
    if not candidate:
        raise ValueError("找不到這張證書（可能已被移除或更名），請重新搜尋")
    if candidate["current"]:
        raise ValueError("這張證書已經在使用中")
    if not candidate["usable"]:
        raise ValueError("無法使用這張證書：" + "、".join(candidate["problems"]))
    if mode == "nginx":
        apply_nginx(candidate, env)
    else:
        apply_uvicorn(candidate, env, install_dir)
    watcher = os.path.join(LIB_DIR, "cert-watch.sh")
    if os.path.isfile(watcher):
        run(["/bin/bash", watcher, "watch", candidate["cert"], candidate["key"]])
    return candidate


# ---------------------------------------------------------------------------- request handling

def take_request(request_dir):
    path = os.path.join(request_dir, REQUEST_NAME)
    try:
        text = read_text_nofollow(path, 4096)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    try:
        os.unlink(path)
    except OSError:
        pass
    try:
        request = json.loads(text)
    except ValueError:
        return None
    if not isinstance(request, dict) or not REQUEST_ID.match(str(request.get("id", ""))):
        return None
    return request


def write_result(result_file, payload):
    payload["finished_at" if payload.get("state") == "done" else "started_at"] = now().isoformat()
    write_atomic(result_file, json.dumps(payload, ensure_ascii=False))


def process(install_dir, certctl_dir=CERTCTL_DIR, scan_roots=None, extra_sources=True):
    request_dir = os.path.join(certctl_dir, "requests")
    result_file = os.path.join(certctl_dir, "result.json")
    with open(os.path.join(certctl_dir, ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        request = take_request(request_dir)
        if not request:
            return None
        request_id, action = str(request["id"]), request.get("action")
        write_result(result_file, {"id": request_id, "action": action, "state": "running"})
        payload = {"id": request_id, "action": action, "state": "done", "ok": False, "error": ""}
        try:
            env = read_env(install_dir)
            if action == "scan":
                payload["candidates"] = scan(env, scan_roots, extra_sources)
                payload["ok"] = True
            elif action == "apply":
                chosen = apply(env, str(request.get("candidate", "")), install_dir, scan_roots, extra_sources)
                payload.update(ok=True, applied=chosen)
            else:
                payload["error"] = "未知的操作"
        except Exception as exc:  # report everything back to the panel
            payload["error"] = str(exc)
        write_result(result_file, payload)
        return payload


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("process", "scan"))
    parser.add_argument("--install-dir", default="/opt/vps-traffic-panel")
    args = parser.parse_args(argv)
    if args.command == "scan":
        print(json.dumps(scan(read_env(args.install_dir)), ensure_ascii=False, indent=2))
        return 0
    process(args.install_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())

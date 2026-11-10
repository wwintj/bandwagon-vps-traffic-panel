"""Inspect the certificate the panel is actually serving.

The panel normally runs unprivileged and cannot read the certificate files, so instead it
opens a TLS connection to its own HTTPS endpoint and looks at what a browser would get.
"""
import os
import socket
import ssl
import subprocess
import tempfile
from datetime import datetime, timezone


def _decode_der(der):
    """DER certificate -> dict in the same shape as SSLSocket.getpeercert()."""
    pem = ssl.DER_cert_to_PEM_cert(der)
    fd, path = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(pem)
        return ssl._ssl._test_decode_cert(path)  # stable CPython helper, used by the stdlib tests
    except Exception:
        return {}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _first(name_entries, key):
    for rdn in name_entries or ():
        for k, v in rdn:
            if k == key:
                return v
    return ""


def _explain(error):
    text = str(error)
    lowered = text.lower()
    if "expired" in lowered:
        return "證書已過期"
    if "self-signed" in lowered or "self signed" in lowered:
        return "自簽證書，瀏覽器不會信任"
    if "hostname mismatch" in lowered or "doesn't match" in lowered or "not valid for" in lowered:
        return "證書上的網域與面板網域不符"
    if "unable to get local issuer" in lowered:
        return "簽發者不受信任，或缺少中繼證書（請使用 fullchain）"
    if "not yet valid" in lowered:
        return "證書尚未生效（請檢查伺服器時間）"
    return text


def inspect_tls(host, port, server_name=None, timeout=5.0):
    """Connect to host:port and describe the served certificate.

    server_name is sent as SNI and used for the hostname check; leave it empty when the
    panel is reached by IP (direct HTTPS mode), where only the chain is checked.
    """
    result = {
        "reachable": False,
        "trusted": False,
        "error": "",
        "subject": "",
        "san": [],
        "issuer": "",
        "not_after": "",
        "days_left": None,
    }
    sni = server_name or None

    plain = ssl.create_default_context()
    plain.check_hostname = False
    plain.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            with plain.wrap_socket(sock, server_hostname=sni) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as exc:
        result["error"] = f"無法連線到 {host}:{port}（{exc}）"
        return result
    result["reachable"] = True

    info = _decode_der(der) if der else {}
    result["subject"] = _first(info.get("subject"), "commonName")
    result["san"] = [value for kind, value in info.get("subjectAltName", ()) if kind == "DNS"]
    organization = _first(info.get("issuer"), "organizationName")
    issuer_cn = _first(info.get("issuer"), "commonName")
    result["issuer"] = " / ".join(part for part in (organization, issuer_cn) if part)
    if info.get("notAfter"):
        expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(info["notAfter"]), timezone.utc)
        result["not_after"] = expires.date().isoformat()
        result["days_left"] = (expires - datetime.now(timezone.utc)).days

    strict = ssl.create_default_context()
    if not sni:
        strict.check_hostname = False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            with strict.wrap_socket(sock, server_hostname=sni):
                pass
        result["trusted"] = True
    except ssl.SSLCertVerificationError as exc:
        result["error"] = _explain(getattr(exc, "verify_message", "") or exc)
    except (OSError, ssl.SSLError) as exc:
        result["error"] = _explain(exc)
    return result


def cert_watch_active():
    """Whether the renewal watcher (systemd path unit) is running; None when unknown."""
    try:
        state = subprocess.run(
            ["systemctl", "is-active", "vps-traffic-panel-cert.path"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return state == "active"

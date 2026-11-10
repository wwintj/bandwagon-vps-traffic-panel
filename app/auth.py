"""Login: Basic auth for scripts, session cookies for browsers, brute-force lockout."""
import hashlib
import secrets
import time
from collections import deque

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app import config, envfile, passwords, sessions, util


# Basic auth stays available for scripts (curl -u); browsers use the login page and a session cookie.
security = HTTPBasic(auto_error=False)


SESSION_COOKIE = "vps_panel_session"


SESSION_TTL = 12 * 3600


SESSION_TTL_REMEMBER = 30 * 24 * 3600


TRUSTED_PROXIES = {"127.0.0.1", "::1"}


AUTH_FAILURE_WINDOW = 15 * 60


AUTH_FAILURE_LIMIT = 10


AUTH_FAILURES = {}


def get_client_ip(request: Request):
    """Real client IP. Proxy headers are only trusted when the request comes from a local reverse proxy."""
    peer = request.client.host if request.client else ""
    if peer in TRUSTED_PROXIES:
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            return real_ip
        forwarded_for = request.headers.get("x-forwarded-for", "")
        if forwarded_for:
            # Nginx appends the connecting address last; earlier entries are client-supplied.
            return forwarded_for.split(",")[-1].strip()
    return peer or "Unknown"


def _recent_auth_failures(client_ip, now):
    failures = AUTH_FAILURES.get(client_ip)
    if failures is None:
        return None
    while failures and now - failures[0][0] > AUTH_FAILURE_WINDOW:
        failures.popleft()
    if not failures:
        AUTH_FAILURES.pop(client_ip, None)
        return None
    return failures


def is_auth_locked(client_ip):
    failures = _recent_auth_failures(client_ip, time.time())
    # Count distinct wrong credentials, so a browser retrying one stale password while polling
    # does not lock its owner out; guessing many passwords does.
    return bool(failures) and len({digest for _ts, digest in failures}) >= AUTH_FAILURE_LIMIT


def record_auth_failure(client_ip, username, password):
    now = time.time()
    if len(AUTH_FAILURES) > 10000:
        AUTH_FAILURES.clear()
    failures = _recent_auth_failures(client_ip, now)
    if failures is None:
        failures = AUTH_FAILURES.setdefault(client_ip, deque(maxlen=200))
    digest = hashlib.sha256(f"{username}\0{password}".encode("utf-8")).hexdigest()[:16]
    failures.append((now, digest))


VERIFIED_CREDENTIALS = set()


def credentials_configured():
    return bool(config.AUTH_USERNAME and (config.AUTH_PASSWORD_HASH or config.AUTH_PASSWORD))


def check_credentials(username, password):
    """Compare against the stored hash. PBKDF2 is deliberately slow, and the page polls several
    endpoints a second, so remember credentials that already verified (in memory only)."""
    user_ok = secrets.compare_digest(username.encode("utf8"), config.AUTH_USERNAME.encode("utf8"))
    if config.AUTH_PASSWORD_HASH:
        key = hashlib.sha256(f"{username}\0{password}\0{config.AUTH_PASSWORD_HASH}".encode("utf8")).digest()
        if key in VERIFIED_CREDENTIALS:
            return user_ok
        if user_ok and passwords.verify_password(password, config.AUTH_PASSWORD_HASH):
            if len(VERIFIED_CREDENTIALS) > 16:
                VERIFIED_CREDENTIALS.clear()
            VERIFIED_CREDENTIALS.add(key)
            return True
        return False
    return user_ok and secrets.compare_digest(password.encode("utf8"), config.AUTH_PASSWORD.encode("utf8"))


def migrate_plaintext_password():
    """Replace a plaintext AUTH_PASSWORD in .env (older installs) with AUTH_PASSWORD_HASH."""
    if not config.AUTH_PASSWORD or config.AUTH_PASSWORD_HASH:
        return False
    hashed = passwords.hash_password(config.AUTH_PASSWORD)
    envfile.update_env_values({"AUTH_PASSWORD_HASH": hashed, "AUTH_PASSWORD": None})
    config.AUTH_PASSWORD_HASH, config.AUTH_PASSWORD = hashed, ""
    util.logger.info("Stored the panel password as a salted hash and removed the plaintext from .env")
    return True


def has_valid_session(request: Request):
    return sessions.is_valid(request.cookies.get(SESSION_COOKIE, ""))


def request_is_https(request: Request):
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else ""
    return peer in TRUSTED_PROXIES and request.headers.get("x-forwarded-proto", "").lower() == "https"


def verify_auth(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    if has_valid_session(request):
        return config.AUTH_USERNAME
    if credentials is None:
        # No WWW-Authenticate header: browsers must not pop up their own login dialog.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登入")
    client_ip = get_client_ip(request)
    if is_auth_locked(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed logins. Try again in 15 minutes.",
        )
    if not credentials_configured():
        # Missing credentials in .env must lock the panel, never fall back to a default password.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AUTH_USERNAME / AUTH_PASSWORD is not configured in .env",
        )
    if not check_credentials(credentials.username, credentials.password):
        record_auth_failure(client_ip, credentials.username, credentials.password)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password")
    AUTH_FAILURES.pop(client_ip, None)
    return credentials.username

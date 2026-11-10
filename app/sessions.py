"""Login sessions for the panel (cookie -> server-side record).

Only a SHA-256 of the cookie token is stored, so a copy of data.db cannot be replayed as a login.
Sessions slide: each use pushes the expiry forward by the session's lifetime (written at most
once a minute to keep SQLite writes low).
"""
import hashlib
import secrets
import time

from app.database import get_db

TOUCH_INTERVAL = 60


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def init_table(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            created REAL NOT NULL,
            expires REAL NOT NULL,
            ttl INTEGER NOT NULL,
            last_seen REAL NOT NULL
        )
    ''')


def create(ttl):
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn = get_db()
    init_table(conn)
    conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
    conn.execute(
        "INSERT INTO sessions (token_hash, created, expires, ttl, last_seen) VALUES (?, ?, ?, ?, ?)",
        (_hash(token), now, now + ttl, int(ttl), now),
    )
    conn.commit()
    conn.close()
    return token


def is_valid(token):
    if not token or len(token) > 256:
        return False
    now = time.time()
    conn = get_db()
    init_table(conn)
    row = conn.execute("SELECT expires, ttl, last_seen FROM sessions WHERE token_hash = ?", (_hash(token),)).fetchone()
    if not row:
        conn.close()
        return False
    if row["expires"] < now:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))
        conn.commit()
        conn.close()
        return False
    if now - row["last_seen"] > TOUCH_INTERVAL:
        conn.execute(
            "UPDATE sessions SET expires = ?, last_seen = ? WHERE token_hash = ?",
            (now + row["ttl"], now, _hash(token)),
        )
        conn.commit()
    conn.close()
    return True


def delete(token):
    if not token:
        return
    conn = get_db()
    init_table(conn)
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))
    conn.commit()
    conn.close()


def delete_all_except(token=None):
    conn = get_db()
    init_table(conn)
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash != ?", (_hash(token),))
    else:
        conn.execute("DELETE FROM sessions")
    conn.commit()
    conn.close()

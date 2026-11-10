"""Password hashing for the panel login (standard library only).

Stored format: pbkdf2_sha256:<iterations>:<salt>:<hash>, base64url without padding. No "$" in the
value, so neither systemd's EnvironmentFile nor python-dotenv can mistake it for a variable.
"""
import base64
import hashlib
import hmac
import os

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 240_000


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password, iterations=ITERATIONS):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{ALGORITHM}:{iterations}:{_b64(salt)}:{_b64(digest)}"


def verify_password(password, encoded):
    try:
        algorithm, iterations, salt, expected = str(encoded).split(":")
        if algorithm != ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _unb64(salt), int(iterations))
        return hmac.compare_digest(digest, _unb64(expected))
    except (ValueError, TypeError):
        return False


def is_hash(value):
    return str(value or "").startswith(ALGORITHM + ":")

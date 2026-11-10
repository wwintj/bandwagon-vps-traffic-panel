"""Reading and atomically rewriting the panel's .env file."""
import os
import tempfile

from app import config


def format_env_value(value):
    # Values are single-line; drop embedded newlines so one setting can never spill into another key.
    value = str(value).replace("\r", "").replace("\n", "")
    if any(ch.isspace() for ch in value) or "#" in value or '"' in value:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def parse_env_value(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def update_env_values(updates):
    existing = {}
    order = []
    if os.path.exists(config.ENV_PATH):
        with open(config.ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                raw = line.rstrip("\n")
                if "=" in raw and not raw.lstrip().startswith("#"):
                    key, value = raw.split("=", 1)
                    existing[key] = parse_env_value(value)
                    order.append(key)

    for key, value in updates.items():
        if value is None:
            # None removes the key from .env
            existing.pop(key, None)
            order = [k for k in order if k != key]
            continue
        existing[key] = str(value)
        if key not in order:
            order.append(key)

    # Write to a temp file and rename, so a crash or full disk never leaves a truncated .env behind.
    env_dir = os.path.dirname(config.ENV_PATH)
    fd, tmp_path = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=env_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for key in order:
                f.write(f"{key}={format_env_value(existing[key])}\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, config.ENV_PATH)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def mask_secret(value):
    value = str(value or "")
    if not value:
        return ""
    return "********" + (value[-4:] if len(value) > 12 else "")


def resolve_secret(submitted, current):
    """Keep the stored secret when the browser sends back the masked placeholder."""
    submitted = str(submitted or "").strip()
    if current and submitted == mask_secret(current):
        return current
    return submitted

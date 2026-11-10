"""Single source of "now" for the panel.

All traffic buckets, billing cycles, daily reports and displayed timestamps use the
configured panel timezone (TELEGRAM_TIMEZONE_OFFSET, hours from UTC), so "today" means
the same thing in the charts and in the Telegram report regardless of the server clock.
"""
from datetime import datetime, timedelta, timezone

from app import config

_offset_hours = config.TELEGRAM_TIMEZONE_OFFSET


def set_offset(hours):
    global _offset_hours
    _offset_hours = int(hours)


def offset():
    return _offset_hours


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=_offset_hours)


def from_unix(timestamp):
    return datetime.fromtimestamp(int(timestamp), timezone.utc).replace(tzinfo=None) + timedelta(hours=_offset_hours)

"""Parsing and formatting of time values given as seconds, m:ss or h:mm:ss."""

from __future__ import annotations

import re

_HMS = re.compile(r"^(?:(\d+):)?(\d+):(\d+(?:\.\d*)?)$")


def parse_time(value: str | float | int | None) -> float | None:
    """Accept 359, "359", "359.5", "5:59", "5:59.5", "1:05:59" or "0:05:59". None and "" give None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"negative time: {value}")
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        m = _HMS.match(s)
        if not m:
            raise ValueError(f"cannot parse time {value!r} (use seconds, m:ss or h:mm:ss)") from None
        h = int(m.group(1) or 0)
        mnt = int(m.group(2))
        sec = float(m.group(3))
        if sec >= 60 or (m.group(1) is not None and mnt >= 60):
            raise ValueError(f"bad time {value!r}")
        return h * 3600 + mnt * 60 + sec
    if v < 0:
        raise ValueError(f"negative time: {value}")
    return v


def format_time(sec: float) -> str:
    m, s = divmod(sec, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:06.3f}" if h else f"{m}:{s:06.3f}"

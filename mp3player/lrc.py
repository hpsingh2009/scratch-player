"""LRC / plain lyrics parser."""
from __future__ import annotations

import re

_TIME_TAG_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")


def parse_lrc(text: str, fallback_dur: float = 180.0) -> list[dict]:
    """Parse synced ([mm:ss.xx]) or plain lyrics into [{'t': seconds, 'text': str}, ...]."""
    if not text or not isinstance(text, str):
        return []
    synced: list[dict] = []
    plain: list[str] = []
    offset = 0.0

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^\[offset:\s*([+-]?\d+)\s*\]$", line, re.IGNORECASE)
        if m:
            offset = int(m.group(1)) / 1000.0
            continue
        if re.match(r"^\[(id|ar|ti|al|by|re|ve|length|tool|au):", line, re.IGNORECASE):
            continue

        tags = list(_TIME_TAG_RE.finditer(line))
        if tags:
            lyric = _TIME_TAG_RE.sub("", line).strip()
            if not lyric:
                continue
            for t in tags:
                frac_s = t.group(3) or "0"
                frac = int(frac_s) / (1000.0 if len(frac_s) == 3 else 100.0 if len(frac_s) == 2 else 10.0)
                secs = int(t.group(1)) * 60.0 + int(t.group(2)) + frac - offset
                synced.append({"t": max(0.0, round(secs, 3)), "text": lyric})
        else:
            cleaned = re.sub(r"^\[[^\]]*\]", "", line).strip()
            if cleaned:
                plain.append(cleaned)

    if synced:
        synced.sort(key=lambda x: x["t"])
        return synced
    if not plain:
        return []
    dur = max(float(fallback_dur or 180.0), len(plain) * 2.5)
    step = dur / (len(plain) + 1)
    return [{"t": round(step * (i + 1) * 0.85, 2), "text": ln} for i, ln in enumerate(plain)]


def active_index(lines: list[dict], t: float) -> int:
    """Index of the lyric line active at time t (seconds), or -1 before the first line."""
    lo, hi, ans = 0, len(lines) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if lines[mid]["t"] <= t:
            ans, lo = mid, mid + 1
        else:
            hi = mid - 1
    return ans

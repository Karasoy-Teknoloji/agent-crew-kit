#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""USD -> TRY rate for the status lines (stdlib only). The ONLY network access of the cost tools.

Source: TCMB (Turkish central bank) daily rates https://www.tcmb.gov.tr/kurlar/today.xml, USD "ForexSelling" (foreign exchange selling rate).
TCMB publishes at about 15:30 Istanbul time on working days; weekends and holidays keep the previous day's file.
Cache: <config dir>/cost/fx.json {rate, date (TCMB date), t (fetched_at), tried (last attempt)} (0600).

refresh() is called only from the background refresh (cost_core.refresh_status), never from the status-line render path:
it fetches when the cached rate is older than the latest 15:30 publication and at most once per hour (a failed attempt counts),
with a 5 s timeout. A failed fetch keeps the cached rate. Computation everywhere stays in USD; only the displayed value is
multiplied by this rate (cost.money), so old sums are shown at today's rate.
"""
import json
import os
import re
import time
import urllib.request

CLAUDE = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
FILE = os.path.join(CLAUDE, "cost", "fx.json")
URL = "https://www.tcmb.gov.tr/kurlar/today.xml"
TIMEOUT = 5
ATTEMPT_INTERVAL = 3600
TR = 3 * 3600          # Turkey is UTC+3 all year (no daylight saving since 2016)
RELEASE = 15 * 3600 + 30 * 60

_USD = re.compile(r'<Currency\b[^>]*CurrencyCode="USD"[^>]*>(.*?)</Currency>', re.S)
_SELLING = re.compile(r"<ForexSelling>\s*([0-9]+(?:[.,][0-9]+)?)\s*</ForexSelling>")
_DATE = re.compile(r'<Tarih_Date\b[^>]*\bTarih="(\d{2})\.(\d{2})\.(\d{4})"')   # TCMB's own (Turkish) tag and attribute names


def parse(xml):
    """TCMB today.xml text -> (rate, 'YYYY-MM-DD') or None. Rejects a rate outside 1..1000."""
    m = _USD.search(xml)
    s = _SELLING.search(m.group(1)) if m else None
    if not s:
        return None
    rate = float(s.group(1).replace(",", "."))
    if not 1.0 <= rate <= 1000.0:
        return None
    t = _DATE.search(xml)
    return rate, ("%s-%s-%s" % (t.group(3), t.group(2), t.group(1)) if t else "")


def last_release(now):
    """Epoch of the latest 15:30 Istanbul time that is not after `now`."""
    local = now + TR
    day = local - local % 86400
    y = day + RELEASE
    if y > local:
        y -= 86400
    return y - TR


def read():
    try:
        with open(FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def required(d, now):
    if now - float(d.get("tried") or 0) < ATTEMPT_INTERVAL:
        return False
    return not d.get("rate") or float(d.get("t") or 0) < last_release(now)


def write(d):
    os.makedirs(os.path.dirname(FILE), mode=0o700, exist_ok=True)
    tmp = "%s.tmp%d" % (FILE, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, separators=(",", ":"))
    os.replace(tmp, FILE)


def refresh(now=None, fetch=None):
    """Fetch and cache the rate when due. fetch: injectable fetcher (tests) returning the XML text. Never raises."""
    now = time.time() if now is None else now
    d = read()
    if not required(d, now):
        return d
    d["tried"] = now
    try:
        if fetch is None:
            request = urllib.request.Request(URL, headers={"User-Agent": "cost/1"})
            with urllib.request.urlopen(request, timeout=TIMEOUT) as r:
                xml = r.read(2_000_000).decode("utf-8", "replace")
        else:
            xml = fetch()
        result = parse(xml)
        if result:
            d.update(rate=result[0], date=result[1], t=now)
    except Exception:
        pass
    try:
        write(d)
    except OSError:
        pass
    return d

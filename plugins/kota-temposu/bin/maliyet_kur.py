#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""USD -> TRY rate for the status lines (stdlib only). The ONLY network access of the maliyet tools.

Source: TCMB (Turkish central bank) daily rates https://www.tcmb.gov.tr/kurlar/today.xml, USD "ForexSelling" (doviz satis).
TCMB publishes at about 15:30 Istanbul time on working days; weekends and holidays keep the previous day's file.
Cache: <config dir>/maliyet/kur.json {kur, tarih (TCMB date), t (fetched_at), denendi (last attempt)} (0600).

yenile() is called only from the background refresh (maliyet_cekirdek.durum_yenile), never from the status-line render path:
it fetches when the cached rate is older than the latest 15:30 publication and at most once per hour (a failed attempt counts),
with a 5 s timeout. A failed fetch keeps the cached rate. Computation everywhere stays in USD; only the displayed value is
multiplied by this rate (maliyet.para), so old sums are shown at today's rate.
"""
import json
import os
import re
import time
import urllib.request

CLAUDE = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
DOSYA = os.path.join(CLAUDE, "maliyet", "kur.json")
URL = "https://www.tcmb.gov.tr/kurlar/today.xml"
ZAMAN_ASIMI = 5
DENEME_ARASI = 3600
TR = 3 * 3600          # Turkey is UTC+3 all year (no daylight saving since 2016)
YAYIN = 15 * 3600 + 30 * 60

_USD = re.compile(r'<Currency\b[^>]*CurrencyCode="USD"[^>]*>(.*?)</Currency>', re.S)
_SATIS = re.compile(r"<ForexSelling>\s*([0-9]+(?:[.,][0-9]+)?)\s*</ForexSelling>")
_TARIH = re.compile(r'<Tarih_Date\b[^>]*\bTarih="(\d{2})\.(\d{2})\.(\d{4})"')


def ayristir(xml):
    """TCMB today.xml text -> (rate, 'YYYY-MM-DD') or None. Rejects a rate outside 1..1000."""
    m = _USD.search(xml)
    s = _SATIS.search(m.group(1)) if m else None
    if not s:
        return None
    kur = float(s.group(1).replace(",", "."))
    if not 1.0 <= kur <= 1000.0:
        return None
    t = _TARIH.search(xml)
    return kur, ("%s-%s-%s" % (t.group(3), t.group(2), t.group(1)) if t else "")


def son_yayin(simdi):
    """Epoch of the latest 15:30 Istanbul time that is not after `simdi`."""
    yerel = simdi + TR
    gun = yerel - yerel % 86400
    y = gun + YAYIN
    if y > yerel:
        y -= 86400
    return y - TR


def oku():
    try:
        with open(DOSYA, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def gerekli(d, simdi):
    if simdi - float(d.get("denendi") or 0) < DENEME_ARASI:
        return False
    return not d.get("kur") or float(d.get("t") or 0) < son_yayin(simdi)


def yaz(d):
    os.makedirs(os.path.dirname(DOSYA), mode=0o700, exist_ok=True)
    tmp = "%s.tmp%d" % (DOSYA, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, separators=(",", ":"))
    os.replace(tmp, DOSYA)


def yenile(simdi=None, getir=None):
    """Fetch and cache the rate when due. getir: injectable fetcher (tests) returning the XML text. Never raises."""
    simdi = time.time() if simdi is None else simdi
    d = oku()
    if not gerekli(d, simdi):
        return d
    d["denendi"] = simdi
    try:
        if getir is None:
            istek = urllib.request.Request(URL, headers={"User-Agent": "maliyet/1"})
            with urllib.request.urlopen(istek, timeout=ZAMAN_ASIMI) as r:
                xml = r.read(2_000_000).decode("utf-8", "replace")
        else:
            xml = getir()
        sonuc = ayristir(xml)
        if sonuc:
            d.update(kur=sonuc[0], tarih=sonuc[1], t=simdi)
    except Exception:
        pass
    try:
        yaz(d)
    except OSError:
        pass
    return d

# -*- coding: utf-8 -*-
"""maliyet_verim: the weekly cost-per-finished-work side of the usage leaderboard (SIRALAMA.md).

Plain Python, spends no model tokens. It adds NO new job: maliyet_paylas (the existing 2-hourly `maliyet --kaydet --paylas` run) calls it.

Published per person (aggregates only), next to the daily row in the same shared clone and the same commit:
  raporlar/kullanim/<kisi>.hafta.json = {surum, kisi, hafta (ISO), bas, bit, kur, bitti, toplam, projeler}
    toplam / projeler[<repo>] = {tok: {in, out, cr, cw5, cw1h}, usd, tl_api, tl_abonelik, kota_yuzde}
  No job names, no conversation text, no file or session paths (job names may carry customer names): only the repo name of the project.
  tl_api = API-list-price equivalent in TL; tl_abonelik = the share of the weekly subscription fee the usage used (weekly quota % x weekly
  plan price), None when the quota calibration (oran.json) is missing; kota_yuzde = the weekly quota percent the usage consumed.

Cost numbers come from tools/maliyet_raporu.py (one code path: transcript scan, price table, USD->TL rate, quota calibration, project
attribution), imported from the clone. A copy of this plugin without the clone's tools/ publishes nothing (logged), the daily row is unaffected.

Finished work ("bitti") = issues with the person as assignee whose board Status was SET to Done during the ISO week (Mon-Sun, local time).
Source: tools/gh_board.list_items (GraphQL Projects, 100 items per request, board ids from ~/.claude/gh-board-ids.json), the Status value's
own `updatedAt` (the moment the Status was last set, so moving a Done card around other fields does not count). Boards: every product board
with a Done option and the org's shared test-queue project; the boards in the config key MALIYET_AYRI_PANOLAR (project numbers, e.g. a management mirror or a personal scratch board) are skipped; one
issue on several boards counts once. Cached 6 h per (login, week); skipped when the GraphQL budget is below gh_board.MIN_GQL.
"""
import datetime
import importlib
import json
import os
import re
import subprocess
import sys
import time

AYRI_PANOLAR = tuple(x for x in os.environ.get("MALIYET_AYRI_PANOLAR", "").replace(",", " ").split() if x)   # config key: boards never counted
ONBELLEK_SAAT = 6
DOSYA_SONEKI = ".hafta.json"
SURUM = 1
ANAHTAR_USD_ADIM = 100       # change-only: a person's weekly dollars count as changed per this step
BELIRSIZ = "belirsiz"
ANA_OTURUM = "ana-oturum"    # main (coordinator) sessions that started no sub-agent job: their cost has no project to be split over
JETONLAR = ("in", "out", "cr", "cw5", "cw1h")
_PLUGIN_BIN = os.path.dirname(os.path.abspath(__file__))


def hafta_siniri(bugun):
    """(ISO week id, Monday, Sunday) of a date (YYYY-MM-DD or date)."""
    d = datetime.date.fromisoformat(bugun) if isinstance(bugun, str) else bugun
    y, w, _ = d.isocalendar()
    bas = d - datetime.timedelta(days=d.weekday())
    return "%d-W%02d" % (y, w), bas, bas + datetime.timedelta(days=6)


def tools_dizin():
    """tools/ of the clone this plugin lives in (plugins/kota-temposu/bin -> repo root); None in a copy without it."""
    d = os.path.normpath(os.path.join(_PLUGIN_BIN, "..", "..", "..", "tools"))
    return d if os.path.isfile(os.path.join(d, "maliyet_raporu.py")) and os.path.isfile(os.path.join(d, "gh_board.py")) else None


def _tools_modul(ad):
    d = tools_dizin()
    if not d:
        raise RuntimeError("tools/ yok (klon disi kopya)")
    if d not in sys.path:
        sys.path.insert(0, d)
    return importlib.import_module(ad)


# ------------------------------------------------------------------ cost aggregation (pure given the maliyet_raporu result)
def _bos():
    return {"tok": {k: 0 for k in JETONLAR}, "usd": 0.0, "tl_api": 0.0, "tl_abonelik": 0.0, "kota_yuzde": 0.0,
            "_tl": False, "_ab": False, "_ko": False}


def _ekle(h, T):
    """Add the usage record T (a job or session of maliyet_raporu.donusum) to the bucket h."""
    for k in JETONLAR:
        h["tok"][k] += int(T.get(k, 0))
    h["usd"] += T.get("usd_toplam", 0.0)
    for anahtar, alan, bayrak in (("tl_api", "tl", "_tl"), ("tl_abonelik", "kota_payi_tl", "_ab"), ("kota_yuzde", "hafta_yuzde", "_ko")):
        if T.get(alan) is not None:
            h[anahtar] += T[alan]
            h[bayrak] = True


def _bitir(h):
    out = {"tok": h["tok"], "usd": round(h["usd"], 2)}
    for anahtar, bayrak in (("tl_api", "_tl"), ("tl_abonelik", "_ab"), ("kota_yuzde", "_ko")):
        out[anahtar] = round(h[anahtar], 2) if h[bayrak] else None
    return out


def ozet_kur(R, kisi, bugun, bitti=None, kur=None):
    """The publishable weekly summary from a maliyet_raporu.donusum(...) result (built with ana_dagit=True, so a job's cost includes
    its share of the main session that started it). Only the repo name of each project survives: job names (R[...]['isler'][*]['is']),
    ids, session ids and project folders are never copied. Sessions that started no job go to ANA_OTURUM."""
    hid, bas, bit = hafta_siniri(bugun)
    projeler, toplam, job_oturum = {}, _bos(), {}
    for g in R.get("projeler", []):
        for j in g["isler"]:
            job_oturum[j.get("oturum")] = job_oturum.get(j.get("oturum"), 0) + 1
    oturum = {S.get("oturum"): S for S in R.get("oturumlar", [])}
    for g in R.get("projeler", []):
        h = projeler.setdefault(g["proje"] or BELIRSIZ, _bos())
        for j in g["isler"]:
            _ekle(h, j)
            _ekle(toplam, j)
            S = oturum.get(j.get("oturum"))
            if S and job_oturum.get(j.get("oturum")):   # main-session tokens split over the jobs like its dollars (equal share)
                pay = 1.0 / job_oturum[j["oturum"]]
                for k in JETONLAR:
                    h["tok"][k] += int(round(S.get(k, 0) * pay))
                    toplam["tok"][k] += int(round(S.get(k, 0) * pay))
    for sid, S in oturum.items():
        if job_oturum.get(sid):
            continue
        for h in (projeler.setdefault(ANA_OTURUM, _bos()), toplam):
            _ekle(h, S)
    return {"surum": SURUM, "kisi": kisi, "hafta": hid, "bas": bas.isoformat(), "bit": bit.isoformat(),
            "kur": round(R["kur"], 4) if R.get("kur") else None, "bitti": bitti,
            "toplam": _bitir(toplam), "projeler": {p: _bitir(h) for p, h in sorted(projeler.items(), key=lambda kv: -kv[1]["usd"])}}


def hafta_ozeti(kisi, bugun):
    """This week's summary of the local transcripts, or None (logged by the caller) when the cost report cannot run."""
    from types import SimpleNamespace
    try:
        MR = _tools_modul("maliyet_raporu")
        a = SimpleNamespace(hafta="", gun=None, proje=None, ana_dagit=True, json=True, en_cok=0)
        bas, bit, etiket, isler, oturumlar = MR.veri_kur(a)
        return ozet_kur(MR.donusum(isler, oturumlar, a), kisi, bugun)
    except (Exception, SystemExit) as e:
        _log("hafta ozeti yok: %s: %s" % (type(e).__name__, e))
        return None


def _log(m):
    try:
        import maliyet_paylas
        maliyet_paylas.log_yaz(m)
    except Exception:
        pass


# ------------------------------------------------------------------ finished work (board Status set to Done this week)
def bitti_hesapla(ogeler, login, bugun, tz=None):
    """Number of distinct issues in `ogeler` (gh_board.list_items dicts of the Done column) assigned to `login` whose Status was set
    during the ISO week of `bugun`. status_at is UTC; the week is judged in `tz` (local time by default)."""
    _, bas, bit = hafta_siniri(bugun)
    gorulen = set()
    for o in ogeler:
        if login.lower() not in [x.lower() for x in o.get("assignees", [])] or not o.get("status_at"):
            continue
        try:
            t = datetime.datetime.fromisoformat(o["status_at"].replace("Z", "+00:00")).astimezone(tz).date()
        except ValueError:
            continue
        if bas <= t <= bit:
            gorulen.add(o.get("url") or "%s#%s" % (o.get("repo"), o.get("number")))
    return len(gorulen)


def _onbellek_yolu():
    import maliyet
    return os.path.join(maliyet.DIZIN, "bitti-onbellek.json")


def bitti_say(login, bugun, simdi=None):
    """Finished-work count of `login` for the week of `bugun` (None = unknown: no gh, no budget, API error). Cached ONBELLEK_SAAT hours."""
    simdi = time.time() if simdi is None else simdi
    hid = hafta_siniri(bugun)[0]
    anahtar = "%s|%s" % (login.lower(), hid)
    yol = _onbellek_yolu()
    try:
        with open(yol, encoding="utf-8") as f:
            ob = json.load(f)
        if not isinstance(ob, dict):
            ob = {}
    except (OSError, ValueError):
        ob = {}
    e = ob.get(anahtar)
    if isinstance(e, dict) and isinstance(e.get("n"), int) and 0 <= simdi - float(e.get("t", 0)) < ONBELLEK_SAAT * 3600:
        return e["n"]
    try:
        GB = _tools_modul("gh_board")
        ids = GB.load_ids()
        g, _ = GB.rates()
        if g["remaining"] < GB.MIN_GQL:
            raise RuntimeError("GraphQL butcesi dusuk (%s)" % g["remaining"])
        ogeler = []
        for k, b in sorted(ids["boards"].items(), key=lambda kv: int(kv[0])):
            if k in AYRI_PANOLAR or not any(GB.norm_name(o) == "done" for o in b["fields"].get("Status", {}).get("options", {})):
                continue
            ogeler += GB.list_items(ids, k, "Done")
        import maliyet_cekirdek as CK
        n = bitti_hesapla(ogeler, login, bugun, CK.TZ)
    except (Exception, SystemExit) as ex:
        _log("bitti sayisi yok: %s: %s" % (type(ex).__name__, str(ex)[:120]))
        return None
    ob = {k: v for k, v in ob.items() if k.endswith("|" + hid)}
    ob[anahtar] = {"n": n, "t": simdi}
    try:
        os.makedirs(os.path.dirname(yol), exist_ok=True)
        with open(yol, "w", encoding="utf-8") as f:
            json.dump(ob, f)
    except OSError:
        pass
    return n


# ------------------------------------------------------------------ the published file, change key, leaderboard text
def dosya_yolu(klon_rapor_dizini, dosya_stem):
    return os.path.join(klon_rapor_dizini, dosya_stem + DOSYA_SONEKI)


def ozet_yaz_metni(ozet):
    return json.dumps(ozet, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def ozet_oku(yol):
    try:
        with open(yol, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("surum") == SURUM and isinstance(d.get("toplam"), dict) else None


def verim_anahtari(ozetler, bugun):
    """Change key of the weekly summaries: (name, finished count, weekly dollars in ANAHTAR_USD_ADIM steps) of the summaries of the
    current week, sorted. Timestamps and small dollar drift never change it (change-only commits)."""
    hid = hafta_siniri(bugun)[0]
    return sorted((ad, o.get("bitti"), int((o["toplam"].get("usd") or 0) // ANAHTAR_USD_ADIM))
                  for ad, o in ozetler.items() if o and o.get("hafta") == hid)


def tl_metni(x):
    return "₺" + ("{:,.0f}".format(x).replace(",", ".")) if x is not None else "—"


def is_basina(tl, bitti):
    """TL per finished item; None when nothing finished (shown as an em dash) or the TL is unknown."""
    return tl / bitti if tl is not None and bitti else None


def verimlilik_sirasi(ozetler, bugun):
    """[(name, TL per finished item)] lowest first, current-week summaries with at least 1 finished item and a known API-equivalent TL."""
    hid = hafta_siniri(bugun)[0]
    s = [(ad, is_basina(o["toplam"].get("tl_api"), o.get("bitti"))) for ad, o in ozetler.items()
         if o and o.get("hafta") == hid and o.get("bitti")]
    return sorted(((ad, v) for ad, v in s if v is not None), key=lambda x: (x[1], x[0]))


def proje_dagilimi(o, en_cok=3):
    p = [(ad, v.get("usd") or 0.0) for ad, v in (o.get("projeler") or {}).items()]
    top = sum(u for _, u in p)
    if top <= 0:
        return "—"
    p.sort(key=lambda x: -x[1])
    s = " · ".join("%s %%%d" % (ad, round(100 * u / top)) for ad, u in p[:en_cok])
    return s + (" · +%d" % (len(p) - en_cok) if len(p) > en_cok else "")


def tablo_satirlari(sira_adlari, ozetler, bugun, madalya):
    """Markdown lines (no trailing blank) of the finished-work section; sira_adlari = people in the existing ranking order."""
    hid = hafta_siniri(bugun)[0]
    L = ["### Biten iş ve maliyeti (%s)" % hid, "",
         "| # | Kişi | Haftalık % | Biten iş (✅ Done) | ₺ API eşdeğeri | ₺ abonelik payı | ₺ / biten iş (API) | ₺ / biten iş (abonelik) | Proje dağılımı |",
         "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for i, ad in enumerate(sira_adlari, 1):
        o = ozetler.get(ad)
        if not o or o.get("hafta") != hid:
            L.append("| %s | %s | — | — | — | — | — | — | — |" % (madalya.get(i, str(i)), ad))
            continue
        t, b = o["toplam"], o.get("bitti")
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            madalya.get(i, str(i)), ad, "%%%d" % round(t["kota_yuzde"]) if t.get("kota_yuzde") is not None else "—",
            b if b is not None else "—", tl_metni(t.get("tl_api")), tl_metni(t.get("tl_abonelik")),
            tl_metni(is_basina(t.get("tl_api"), b)), tl_metni(is_basina(t.get("tl_abonelik"), b)), proje_dagilimi(o)))
    vs = verimlilik_sirasi(ozetler, bugun)
    L += ["", "**Verimlilik (en düşük ₺ / biten iş, API eşdeğeri; en az 1 iş):** " + (
        " · ".join("%s %s %s/iş" % (madalya.get(i, "%d." % i), ad, tl_metni(v)) for i, (ad, v) in enumerate(vs, 1))
        if vs else "_bu hafta biten iş yok_"), "",
         "Biten iş = atanan kişinin, haftanın (Pzt-Paz) içinde board Status'u ✅ Done yapılan issue sayısı (ürün panoları + ortak test kuyruğu; bir issue bir kez). "
         "₺ API eşdeğeri = API liste fiyatı × TCMB kuru; ₺ abonelik payı = haftalık kota %'si × haftalık plan ücreti. Sıralama kuralı değişmedi (7 gün $); "
         "verimlilik ayrı bir satırdır, düşük olan iyidir. Proje dağılımı dolar payıdır; iş adı ve konuşma içeriği paylaşılmaz."]
    return L

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""maliyet core: transcript scan, daily/hourly records, views, CLI (entry and the --durum fast path are in maliyet.py)."""
import argparse
import datetime
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I does not add the script directory

import maliyet_ayristir as AO  # noqa: E402  parser + price table (single source)
from maliyet import BETIK, CLAUDE, DIZIN, DURUM, TZ_AD, SAATLIK_GUN, PENCERELER, durum_yaz, pencere_parcalar, _tz  # noqa: E402,F401  (paths and window rule live in one place)

GUNLUK = os.path.join(DIZIN, "gunluk.jsonl")
SAATLIK = os.path.join(DIZIN, "saatlik.jsonl")
ONBELLEK = os.path.join(DIZIN, "onbellek.json")
SURUM = 1          # daily record schema; when it grows, old rows are overwritten on days that are recomputed
ONB_SURUM = 5      # per-file cache schema (4: + per-minute usd buckets "dk"; 5: + advisor tokens in "dan")
DK_GUN = 8         # days of per-minute buckets kept (the quota calibration looks back 7 days, maliyet_oran.GUN)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _uyari(metin):
    """A warning for the console and for the run log of a scheduled run (maliyet.py `_planli_calistir`). sys.stderr is None under
    pythonw (Task Scheduler): a bare .write() there raised AttributeError and ended a run that had only a warning to report."""
    try:
        if sys.stderr is not None:
            sys.stderr.write(metin)
    except Exception:
        pass
    try:
        lg = logging.getLogger("maliyet")
        if lg.handlers:
            lg.warning("%s", metin.strip())
    except Exception:
        pass


def _tz_nesnesi():
    """Day-boundary time zone. None = the system's local zone (MALIYET_TZ=local)."""
    if TZ_AD.lower() == "local":
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TZ_AD)
    except Exception:
        pass
    if TZ_AD.upper() == "UTC":
        return datetime.timezone.utc
    if TZ_AD == "Europe/Istanbul":
        return datetime.timezone(datetime.timedelta(hours=3))  # tzdata missing (e.g. Windows): Istanbul has been UTC+3 since 2016
    _uyari("UYARI: saat dilimi bulunamadı (%s), yerel saat kullanılıyor\n" % TZ_AD)
    return None


TZ = _tz_nesnesi()


_gun_onb = {}


def yerel_gun(e):
    k = int(e // 600)
    g = _gun_onb.get(k)
    if g is None:
        g = datetime.datetime.fromtimestamp(k * 600, TZ).strftime("%Y-%m-%d")
        if len(_gun_onb) > 50000:
            _gun_onb.clear()
        _gun_onb[k] = g
    return g


def bugun_str():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ per-file parsing + cache
def meta_oku(meta_yol):
    """Only agentType and parentAgentId; the description is never read or stored."""
    try:
        with open(meta_yol, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    return {k: d[k] for k in ("agentType", "parentAgentId") if isinstance(d, dict) and d.get(k)}


def dosya_isle(fiyat, tur, yol, proj, oturum, imza, meta, reqs):
    """Aggregate the (already de-duplicated) requests of one transcript into per-day / per-hour sums."""
    F = {"imza": imza, "ana": tur == "ana", "proj": proj, "sid": oturum,
         "tur": "" if tur == "ana" else (meta.get("agentType") or "bilinmiyor"),
         "ilk": None, "gun": {}, "saat": {}, "dk": {}, "fiyatsiz": 0}
    dk_sinir = time.time() - DK_GUN * 86400
    if not reqs:
        return F
    F["ilk"] = yerel_gun(min(r["ts"] for r in reqs))
    toplam = 0.0
    for r in reqs:
        d = yerel_gun(r["ts"])
        G = F["gun"].setdefault(d, {"model": {}, "dan": {}})
        a = G["model"].setdefault(r["model"], [0, 0, 0, 0, 0, 0, 0.0])
        a[0] += 1
        a[1] += r["in"]
        a[2] += r["out"]
        a[3] += r["cr"]
        a[4] += r["cw5"]
        a[5] += r["cw1h"]
        H = F["saat"].setdefault(str(int(r["ts"] // 3600)), [{}, 0.0])  # hour bucket: [model -> usd (advisor included), advisor usd]
        c, advler, fz = AO.istek_maliyeti(fiyat, r)
        F["fiyatsiz"] += fz
        if c is not None:
            a[6] += c
            H[0][r["model"]] = H[0].get(r["model"], 0.0) + c
            toplam += c
        rusd = c or 0.0
        for ad in r.get("adv") or []:   # advisor tokens (priced models only, like the advisor $) so the per-model Mtoken is not 0
            if fiyat.bul(ad["model"]) is not None:
                x = G["dan"].setdefault(ad["model"], [0, 0.0, 0, 0, 0, 0, 0])
                for j, key in enumerate(("in", "out", "cr", "cw5", "cw1h")):
                    x[2 + j] += ad[key]
        for am, ca in advler:
            rusd += ca
            x = G["dan"].setdefault(am, [0, 0.0, 0, 0, 0, 0, 0])
            x[0] += 1
            x[1] += ca
            H[0][am] = H[0].get(am, 0.0) + ca
            H[1] += ca
            toplam += ca
        if rusd and r["ts"] >= dk_sinir:
            km = str(int(r["ts"] // 60))   # minute bucket: usd per UTC minute, for the quota calibration (maliyet_oran.py)
            F["dk"][km] = F["dk"].get(km, 0.0) + rusd
    F["dk"] = {k: round(v, 5) for k, v in F["dk"].items()}
    for G in F["gun"].values():
        for a in G["model"].values():
            a[6] = round(a[6], 6)
        for x in G["dan"].values():
            x[1] = round(x[1], 6)
    for H in F["saat"].values():
        H[0] = {m: round(v, 6) for m, v in H[0].items()}
        H[1] = round(H[1], 6)
    return F


def onbellek_yukle(anahtar):
    try:
        with open(ONBELLEK, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("anahtar") == anahtar:
            return d.get("dosyalar") or {}
    except Exception:
        pass
    return {}


def yaz_0600(yol, metin):
    os.makedirs(os.path.dirname(yol), mode=0o700, exist_ok=True)
    tmp = "%s.tmp%d" % (yol, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(metin)
    os.replace(tmp, yol)


def sahiplik(girdiler):
    """One owner file per API response. girdiler: {rel: {"ana", "fork", "t0", "t1", "ids"}} -> {rel: set of key hashes owned by
    ANOTHER file (to be skipped there)}. A resumed session copies the earlier history into its own transcript (and the sub-agent
    files into its folder); each response must be counted once, in the file that owns it. Rank, smallest wins: main conversation
    before sub-agent, non-fork before fork agent, earlier first timestamp (the original session started before its resumed copy),
    earlier last timestamp (the original stopped when the copy was made), then path. Deterministic, so every run and every machine
    picks the same owner; the owner only decides ana/alt, session and project attribution, never the total."""
    inf = float("inf")
    sira = {}
    for rel, E in girdiler.items():
        sira[rel] = (0 if E["ana"] else 1, 1 if E["fork"] else 0, E["t0"] if E["t0"] is not None else inf,
                     E["t1"] if E["t1"] is not None else inf, rel)
    sahip = {}
    for rel, E in girdiler.items():
        for i in E["ids"]:
            c = sahip.get(i)
            if c is None or sira[rel] < sira[c]:
                sahip[i] = rel
    return {rel: {i for i in E["ids"] if sahip[i] != rel} for rel, E in girdiler.items()}


def tara(onbellek_kullan=True):
    """Scan all transcripts (unchanged ones come from the cache) -> {relative path: F}.
    Every response is counted once across ALL files (sahiplik). A file's cached sums depend on which of its responses it owns,
    so the cache entry remembers a hash of the excluded set ("hx") and the file is re-read when that set changes."""
    d = AO.fiyat_yukle()
    fiyat = AO.Fiyat(d)
    anahtar = "%d|%s|ao%s" % (ONB_SURUM, AO.fiyat_ozeti(d), AO.SURUM)
    onb = onbellek_yukle(anahtar) if onbellek_kullan else {}
    yeni = {}
    taze = {}      # rel -> parsed requests of files read in this run (unfiltered)
    bilgi = {}     # rel -> (tur, yol, proj, oturum, imza, meta)
    girdiler = {}
    for tur, yol, proj, oturum in AO.dosyalari_listele():
        rel = os.path.relpath(yol, AO.PROJ)
        imza = AO.stat_imza(yol)
        if imza is None:
            continue
        meta = {}
        if tur == "ajan":
            mi = AO.stat_imza(yol[:-6] + ".meta.json")
            if mi:
                imza = imza + mi
            meta = meta_oku(yol[:-6] + ".meta.json")
        bilgi[rel] = (tur, yol, proj, oturum, imza, meta)
        e = onb.get(rel)
        if e is not None and e.get("imza") == imza and "ids" in e:
            girdiler[rel] = e
            continue
        try:
            P = AO.ayristir(yol)
        except OSError:
            del bilgi[rel]
            continue
        taze[rel] = P["reqs"]
        girdiler[rel] = {"ana": tur == "ana", "fork": bool(meta.get("parentAgentId") or meta.get("agentType") == "fork"),
                         "t0": P["t0"], "t1": P["t1"], "ids": [r["k"] for r in P["reqs"]]}
    haric = sahiplik(girdiler)
    degisti = False
    islenen = 0
    for rel, (tur, yol, proj, oturum, imza, meta) in bilgi.items():
        G = girdiler[rel]
        hx = hashlib.md5(",".join(sorted(haric[rel])).encode("ascii")).hexdigest()[:12] if haric[rel] else ""
        if rel not in taze and G.get("hx", "") == hx:
            yeni[rel] = G
            continue
        try:
            if rel in taze:
                reqs = [r for r in taze[rel] if r["k"] not in haric[rel]] if haric[rel] else taze[rel]
            else:
                reqs = AO.ayristir(yol, haric[rel])["reqs"]
            F = dosya_isle(fiyat, tur, yol, proj, oturum, imza, meta, reqs)
        except OSError:
            continue
        F.update({"hx": hx, "t0": G["t0"], "t1": G["t1"], "fork": G["fork"], "ids": G["ids"]})
        yeni[rel] = F
        degisti = True
        islenen += 1
    # per-minute buckets older than DK_GUN days are dropped on every write, also from files that were not re-read (the cache would only grow)
    dk_esik = int((time.time() - DK_GUN * 86400) // 60)
    for F in yeni.values():
        dk = F.get("dk")
        if dk and any(int(k) < dk_esik for k in dk):
            F["dk"] = {k: v for k, v in dk.items() if int(k) >= dk_esik}
            degisti = True
    if degisti or set(yeni) != set(onb):
        try:
            yaz_0600(ONBELLEK, json.dumps({"anahtar": anahtar, "dosyalar": yeni}, ensure_ascii=False, separators=(",", ":")))
        except OSError as e:
            _uyari("önbellek yazılamadı: %s\n" % e)
    tara.islenen = islenen
    return yeni


# ------------------------------------------------------------------ daily records
def kova():
    return {"usd": 0.0, "n": 0, "in": 0, "out": 0, "cr": 0, "cw5": 0, "cw1h": 0}


def kova_ekle(k, a):
    k["n"] += a[0]
    k["in"] += a[1]
    k["out"] += a[2]
    k["cr"] += a[3]
    k["cw5"] += a[4]
    k["cw1h"] += a[5]
    k["usd"] += a[6]


def yeni_kayit(d):
    r = kova()
    r.update({"v": SURUM, "gun": d, "dan": {"usd": 0.0, "n": 0}, "ana": kova(), "alt": kova(),
              "model": {}, "tur": {}, "proje": {}, "oturum": defaultdict(float), "ajan_kosu": 0, "fiyatsiz": 0})
    return r


def kayitlari_kur(dosyalar):
    gunler = {}
    for F in dosyalar.values():
        if not F["gun"]:
            continue
        tur_ad = "ana" if F["ana"] else (F["tur"] or "bilinmiyor")
        grp = "ana" if F["ana"] else "alt"
        for d, G in F["gun"].items():
            R = gunler.get(d)
            if R is None:
                R = gunler[d] = yeni_kayit(d)
            ust = [R, R[grp], R["tur"].setdefault(tur_ad, kova()), R["proje"].setdefault(F["proj"], kova())]
            for m, a in G["model"].items():
                for k in ust:
                    kova_ekle(k, a)
                kova_ekle(R["model"].setdefault(m, kova()), a)
                R["oturum"][F["sid"]] += a[6]
            for am, (n, usd, *tok) in G["dan"].items():
                for k in ust:
                    k["usd"] += usd
                km = R["model"].setdefault(am, kova())
                km["usd"] += usd
                for key, v in zip(("in", "out", "cr", "cw5", "cw1h"), tok):
                    km[key] += v   # advisor tokens count under the advisor's model only (not in the totals, as before)
                R["dan"]["usd"] += usd
                R["dan"]["n"] += n
                R["oturum"][F["sid"]] += usd
        if not F["ana"] and F["ilk"] in gunler:
            R = gunler[F["ilk"]]
            R["ajan_kosu"] += 1
            R["tur"].setdefault(tur_ad, kova())["kosu"] = R["tur"][tur_ad].get("kosu", 0) + 1
        if F["fiyatsiz"]:
            gunler[sorted(F["gun"])[0]]["fiyatsiz"] += F["fiyatsiz"]
    return {d: temizle(R) for d, R in gunler.items()}


def temizle(o):
    """Round; drop zero-valued keys (v and gun stay)."""
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            v = temizle(v)
            if k in ("v", "gun"):
                out[k] = v
            elif isinstance(v, (dict,)) and not v:
                continue
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0:
                continue
            else:
                out[k] = v
        return out
    if isinstance(o, float):
        return round(o, 6)
    return o


def gunluk_oku():
    d = {}
    try:
        with open(GUNLUK, encoding="utf-8") as f:
            for s in f:
                s = s.strip()
                if not s:
                    continue
                try:
                    r = json.loads(s)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("gun"):
                    d[r["gun"]] = r
    except OSError:
        pass
    return d


def birlestir(kayitli, tarama, bugun, yeniden=False, koru=()):
    """Stored daily rows + a fresh scan. When a past day scans lower than the stored row (transcripts were deleted) the
    stored row is kept. Today, an old schema and --yeniden always take the scan, except the days in `koru` (--yeniden only:
    days whose transcripts are partly deleted, a scan would undercount them). Returns (day -> record, changed, kept)."""
    out = {}
    degisen = korunan = 0
    for d in set(kayitli) | set(tarama):
        s, n = kayitli.get(d), tarama.get(d)
        if n is None:
            out[d] = s
        elif s is None:
            out[d] = n
            degisen += 1
        elif yeniden and d in koru and d != bugun:
            out[d] = s
            korunan += 1
        elif yeniden or d == bugun or s.get("v", 0) < SURUM or n.get("usd", 0) >= s.get("usd", 0) - 0.005:
            out[d] = n
            if abs(n.get("usd", 0) - s.get("usd", 0)) > 1e-6:
                degisen += 1
        else:
            out[d] = s
            korunan += 1
    return out, degisen, korunan


def kaydet(yeniden=False, onbellek=True):
    tg, ts = tarama_ve_kayitlar(tara(onbellek))
    tarama = tg
    kayitli = gunluk_oku()
    bugun = bugun_str()
    koru = eksik_gunler(kayitli) if yeniden else set()
    son, degisen, korunan = birlestir(kayitli, tarama, bugun, yeniden, koru)
    yeni_say = len([d for d in son if d not in kayitli])
    satirlar = [json.dumps(son[d], ensure_ascii=False, separators=(",", ":")) for d in sorted(son)]
    os.makedirs(DIZIN, mode=0o700, exist_ok=True)
    os.chmod(DIZIN, 0o700)
    if os.path.exists(GUNLUK):
        shutil.copyfile(GUNLUK, GUNLUK + ".yedek")
        os.chmod(GUNLUK + ".yedek", 0o600)
    yaz_0600(GUNLUK, "\n".join(satirlar) + "\n")
    os.chmod(GUNLUK, 0o600)
    gunler = sorted(son)
    print("maliyet kayıt: %d gün (yeni %d, güncellenen %d, korunan %d) | %s … %s | toplam %.2f $ | %s"
          % (len(son), yeni_say, degisen - yeni_say, korunan, gunler[0] if gunler else "-", gunler[-1] if gunler else "-",
             sum(r.get("usd", 0) for r in son.values()), GUNLUK))
    saat = saatlik_birlestir(saatlik_oku(), ts, int(time.time() // 3600), yeniden, koru)
    saatlik_yaz(saat)
    os.chmod(SAATLIK, 0o600)
    print("saatlik kayıt: %d saat | %s" % (len(saat), SAATLIK))
    if yeniden:
        eski = sorted(d for d in son if d not in tarama or d in koru)
        print("yeniden hesaplanan gün: %d. Aynı yanıt tek sayıldı (devam ettirilen oturumların kopyaları çıkarıldı)." % (len(son) - len(eski)))
        if eski:
            print("UYARI: %d gün yeniden hesaplanamadı (transkriptleri silinmiş), eski -şişmiş olabilir- değerleri kaldı: %s"
                  % (len(eski), ", ".join(eski) if len(eski) <= 6 else "%s … %s" % (eski[0], eski[-1])))
    durum_yaz_cache(son, saat)
    return 0


def eksik_gunler(kayitli):
    """Stored days that cannot be recomputed completely: some session recorded in the day's `oturum` map no longer has a
    transcript (main file or session folder). A rescan would undercount them, so --yeniden keeps their stored row."""
    var = set()
    if os.path.isdir(AO.PROJ):
        for proj in os.listdir(AO.PROJ):
            pd = os.path.join(AO.PROJ, proj)
            if os.path.isdir(pd):
                for ad in os.listdir(pd):
                    var.add(ad[:-6] if ad.endswith(".jsonl") else ad)
    return {d for d, r in kayitli.items() if any(sid not in var for sid in (r.get("oturum") or {}))}


def saatleri_kur(dosyalar):
    """Transcript hour buckets -> {epoch_hour(int): {usd, ana, alt, dan, model{m: usd}}}. Hour = UTC epoch//3600 (matches local hours for whole-hour zone offsets)."""
    S = {}
    for F in dosyalar.values():
        grp = "ana" if F["ana"] else "alt"
        for hk, (modeller, dan) in (F.get("saat") or {}).items():
            R = S.setdefault(int(hk), {"usd": 0.0, "ana": 0.0, "alt": 0.0, "dan": 0.0, "model": {}})
            t = sum(modeller.values())
            R["usd"] += t
            R[grp] += t
            R["dan"] += dan
            for m, v in modeller.items():
                R["model"][m] = R["model"].get(m, 0.0) + v
    for R in S.values():
        for k in ("usd", "ana", "alt", "dan"):
            R[k] = round(R[k], 6)
        R["model"] = {m: round(v, 6) for m, v in R["model"].items() if v}
    return S


def saat_etiket(h):
    return datetime.datetime.fromtimestamp(h * 3600, TZ).strftime("%Y-%m-%dT%H")


def saatlik_oku():
    d = {}
    try:
        with open(SAATLIK, encoding="utf-8") as f:
            for s in f:
                try:
                    r = json.loads(s)
                    h = int(r["h"])
                except (ValueError, KeyError, TypeError):
                    continue
                d[h] = r
    except OSError:
        pass
    return d


def saatlik_birlestir(kayitli, tarama, simdi_h, yeniden=False, koru=()):
    """Last SAATLIK_GUN days. A fresh scan overwrites the stored row; for old hours whose transcript was deleted (scan lower
    than stored) the stored row is kept. The last 48 hours always come from the scan. yeniden: every hour that has a scan
    takes it (stored duplicates are dropped), except hours of the days in `koru` (partly deleted transcripts)."""
    bas = simdi_h - SAATLIK_GUN * 24
    out = {}
    for h in set(kayitli) | set(tarama):
        if h < bas:
            continue
        s, n = kayitli.get(h), tarama.get(h)
        if n is None:
            out[h] = s
        elif yeniden and h <= simdi_h - 48 and saat_etiket(h)[:10] in koru:
            out[h] = s
        elif yeniden or s is None or h > simdi_h - 48 or n.get("usd", 0) >= s.get("usd", 0) - 0.005:
            out[h] = dict(n, h=h)
        else:
            out[h] = s
    return out


def saatlik_yaz(saat):
    satirlar = []
    for h in sorted(saat):
        r = dict(saat[h])
        r["h"] = h
        r["saat"] = saat_etiket(h)
        satirlar.append(json.dumps(temizle_saat(r), ensure_ascii=False, separators=(",", ":")))
    yeni = "\n".join(satirlar) + "\n" if satirlar else ""
    try:
        with open(SAATLIK, encoding="utf-8") as f:
            if f.read() == yeni:
                return
    except OSError:
        pass
    yaz_0600(SAATLIK, yeni)


def temizle_saat(r):
    return {k: v for k, v in r.items() if k in ("h", "saat", "model") or (isinstance(v, (int, float)) and v != 0)}


def durum_yaz_cache(kayitlar, saat):
    bugun = datetime.datetime.now(TZ).date()
    gunler = {}
    for i in range(32):
        d = (bugun - datetime.timedelta(days=i)).isoformat()
        r = kayitlar.get(d)
        if r:
            gunler[d] = round(r.get("usd", 0.0), 4)
    simdi_h = int(time.time() // 3600)
    ilk = min(saat) if saat else None
    sk = {str(h): round(r.get("usd", 0.0), 4) for h, r in saat.items() if h > simdi_h - 31 * 24 and r.get("usd")}
    yaz_0600(DURUM, json.dumps({"t": time.time(), "gunler": gunler, "e": ilk, "saat": sk}, separators=(",", ":")))


def tarama_ve_kayitlar(dosyalar=None):
    """Daily + hourly records built from the scan. Returns (day records, hour records)."""
    d = dosyalar if dosyalar is not None else tara()
    tg = kayitlari_kur(d)
    ts = saatleri_kur(d)
    return tg, ts


def gunluk_yaz(son):
    """Write gunluk.jsonl (same rows as kaydet(), without the .yedek copy) unless the content is unchanged."""
    yeni = "\n".join(json.dumps(son[d], ensure_ascii=False, separators=(",", ":")) for d in sorted(son)) + "\n"
    if not son:
        return
    try:
        with open(GUNLUK, encoding="utf-8") as f:
            if f.read() == yeni:
                return
    except OSError:
        pass
    yaz_0600(GUNLUK, yeni)


def durum_yenile():
    """Incremental: unchanged transcripts come from the file cache, only changed ones are read; same merge as --kaydet.
    The hourly file is updated too (not rewritten when the content is unchanged)."""
    dosyalar = tara()
    tg, ts = tarama_ve_kayitlar(dosyalar)
    son, _, _ = birlestir(gunluk_oku(), tg, bugun_str())
    saat = saatlik_birlestir(saatlik_oku(), ts, int(time.time() // 3600))
    saatlik_yaz(saat)
    gunluk_yaz(son)    # the daily record too: macOS has no timer, and the refresh runs while Claude Code is in use
    durum_yaz_cache(son, saat)
    # background-only extras, each failing silently: quota calibration ("gerçek $", maliyet_oran.py) and the lira rate (maliyet_kur.py)
    try:
        import maliyet_oran
        maliyet_oran.yenile(dosyalar)
    except Exception as e:
        _uyari("oran hesaplanamadı: %s\n" % e)
    try:
        import maliyet_kur
        maliyet_kur.yenile()
    except Exception as e:
        _uyari("kur alınamadı: %s\n" % e)
    return 0


# ------------------------------------------------------------------ periods
def ekle(dst, src):
    for k, v in src.items():
        if k in ("v", "gun"):
            continue
        if isinstance(v, dict):
            ekle(dst.setdefault(k, {}), v)
        elif isinstance(v, (int, float)):
            dst[k] = dst.get(k, 0) + v


def hafta_anahtar(d):
    y, w, _ = d.isocalendar()
    return "%d-W%02d" % (y, w)


def donem_sinirlari(tur, d):
    if tur == "gun":
        return d, d
    if tur == "hafta":
        bas = d - datetime.timedelta(days=d.weekday())
        return bas, bas + datetime.timedelta(days=6)
    if tur == "ay":
        bas = d.replace(day=1)
        sonraki = (bas.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        return bas, sonraki - datetime.timedelta(days=1)
    return datetime.date(d.year, 1, 1), datetime.date(d.year, 12, 31)


def donem_anahtar(tur, d):
    if tur == "gun":
        return d.isoformat()
    if tur == "hafta":
        return hafta_anahtar(d)
    if tur == "ay":
        return d.strftime("%Y-%m")
    return str(d.year)


GUN_AD = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]


def etiket(tur, bas, bit):
    if tur == "gun":
        return "%s %s" % (bas.strftime("%d.%m"), GUN_AD[bas.weekday()])
    if tur == "hafta":
        return "%s (%s–%s)" % (hafta_anahtar(bas), bas.strftime("%d.%m"), bit.strftime("%d.%m"))
    if tur == "ay":
        return bas.strftime("%Y-%m")
    return str(bas.year)


def kisa_model(m):
    mm = re.match(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$", m)
    if not mm:
        return m
    s = mm.group(1).capitalize() + " " + mm.group(2)
    return s + "." + mm.group(3) if mm.group(3) else s


def _home_oneki():
    """Claude Code names a project folder after its working directory with every non-alphanumeric character replaced by '-'.
    The home directory prefix is stripped so projects show as '-work-repo' style short names (derived from $HOME, never hardcoded)."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.expanduser("~"))


def kisa_proje(p):
    s = p
    on = _home_oneki()
    if on and s.startswith(on):
        s = s[len(on):]
    return s.lstrip("-") or "~"


def isabet(k):
    t = k.get("in", 0) + k.get("cr", 0) + k.get("cw5", 0) + k.get("cw1h", 0)
    return round(k.get("cr", 0) / t, 4) if t else None


def kova_ozet(k, ayrinti=False):
    o = {"usd": round(k.get("usd", 0.0), 4), "n": int(k.get("n", 0))}
    if ayrinti:
        for a in ("in", "out", "cr", "cw5", "cw1h"):
            o[a] = int(k.get(a, 0))
        o["isabet"] = isabet(k)
    if k.get("kosu"):
        o["kosu"] = int(k["kosu"])
    return o


def ozet_hesapla(satirlar, tur):
    """Total, averages, busiest/cheapest. Partial periods (the running one, the one the history starts in) are left out of the
    average and the cheapest/busiest pick; with no full period they are all used and the note says so."""
    tam = [x for x in satirlar if not x["kismi"]]
    havuz = tam or satirlar
    ozet = {"donem_say": len(satirlar), "tam_donem_say": len(tam), "tam_yok": not tam}
    if not satirlar:
        return ozet
    toplam = sum(x["usd"] for x in satirlar)
    etkin_toplam = sum(x["etkin_gun"] for x in satirlar)
    ozet["toplam_usd"] = round(toplam, 4)
    ozet["etkin_gun"] = etkin_toplam
    ozet["ort_etkin_gun_usd"] = round(toplam / etkin_toplam, 4)
    ozet["ort_donem_usd"] = round(sum(x["usd"] for x in havuz) / len(havuz), 4)
    if tur == "gun":
        ozet["ort_donem_not"] = "bugün hariç" if tam else "yalnız bugün"
    else:
        ozet["ort_donem_not"] = "yalnız tam dönemler" if tam else "tam dönem yok, kısmi dönemler dahil"
    en = max(havuz, key=lambda x: x["usd"])
    az = min(havuz, key=lambda x: x["usd"])
    ozet["en_yogun"] = {"etiket": en["etiket"], "usd": en["usd"]}
    ozet["en_ucuz"] = {"etiket": az["etiket"], "usd": az["usd"]}
    ozet["en_ucuz_not"] = ozet["ort_donem_not"]
    ozet["havuz_say"] = len(havuz)
    return ozet


def gorunum(kayitlar, tur, bugun, ilk_gun_oturum):
    """tur: gun|hafta|ay|yil -> {satirlar, ozet}. Empty periods are never produced (built only from days that have data)."""
    gruplar = defaultdict(list)
    for d in sorted(kayitlar):
        r = kayitlar[d]
        if r.get("usd", 0) <= 0 and not r.get("n"):
            continue
        dt = datetime.date.fromisoformat(d)
        gruplar[donem_anahtar(tur, dt)].append((dt, r))
    ilk = min(kayitlar) if kayitlar else None
    ilk_dt = datetime.date.fromisoformat(ilk) if ilk else None
    bugun_dt = datetime.date.fromisoformat(bugun)
    satirlar = []
    for key in sorted(gruplar):
        recs = gruplar[key]
        bas, bit = donem_sinirlari(tur, recs[0][0])
        A = {}
        for _, r in recs:
            ekle(A, r)
        kismi = bas <= bugun_dt <= bit or (ilk_dt is not None and bas < ilk_dt)
        etkin = len(recs)
        oturum = A.get("oturum", {})
        top = sorted(oturum.items(), key=lambda kv: -kv[1])[:10]
        proje = sorted(((kisa_proje(p), v) for p, v in A.get("proje", {}).items()), key=lambda kv: -kv[1].get("usd", 0))
        satir = {
            "anahtar": key, "etiket": etiket(tur, bas, bit), "bas": bas.isoformat(), "bit": bit.isoformat(),
            "etkin_gun": etkin, "kismi": bool(kismi),
            "usd": round(A.get("usd", 0.0), 4), "usd_gun": round(A.get("usd", 0.0) / etkin, 4),
            "istek": int(A.get("n", 0)),
            "tok": {a: int(A.get(a, 0)) for a in ("in", "out", "cr", "cw5", "cw1h")},
            "isabet": isabet(A),
            "oturum_say": len(oturum), "ajan_kosu": int(A.get("ajan_kosu", 0)),
            "danisman": {"usd": round(A.get("dan", {}).get("usd", 0.0), 4), "n": int(A.get("dan", {}).get("n", 0))},
            "ana": kova_ozet(A.get("ana", {})), "alt": kova_ozet(A.get("alt", {})),
            "model": {m: kova_ozet(v, True) for m, v in sorted(A.get("model", {}).items(), key=lambda kv: -kv[1].get("usd", 0)) if v.get("usd", 0) > 0 or v.get("n")},
            "tur_": {t: kova_ozet(v) for t, v in sorted(A.get("tur", {}).items(), key=lambda kv: -kv[1].get("usd", 0)) if v.get("usd", 0) > 0 or v.get("n")},
            "proje": {p: kova_ozet(v) for p, v in proje[:10]},
            "en_oturum": [{"id": sid, "tarih": ilk_gun_oturum.get(sid, ""), "usd": round(v, 4)} for sid, v in top if v > 0],
        }
        if A.get("fiyatsiz"):
            satir["fiyatsiz_token"] = int(A["fiyatsiz"])
        satir["tur"] = satir.pop("tur_")
        satirlar.append(satir)
    ozet = ozet_hesapla(satirlar, tur)
    return {"tur": tur, "satirlar": satirlar, "ozet": ozet}


def tum_veri(kayitlar, bugun):
    ilk_gun_oturum = {}
    for d in sorted(kayitlar):
        for sid in kayitlar[d].get("oturum", {}):
            ilk_gun_oturum.setdefault(sid, d)
    toplam_oturum = defaultdict(float)
    for r in kayitlar.values():
        for sid, v in r.get("oturum", {}).items():
            toplam_oturum[sid] += v
    en = sorted(toplam_oturum.items(), key=lambda kv: -kv[1])[:10]
    gunler = sorted(kayitlar)
    V = {
        "olusturma": datetime.datetime.now(TZ).isoformat(timespec="seconds"),
        "saat_dilimi": TZ_AD,
        "bugun": bugun,
        "not": "API liste fiyatı karşılığı (USD), abonelik faturası değil. Danışman çağrıları dahil. İstem/başlık içermez.",
        "kapsam": {"ilk_gun": gunler[0] if gunler else None, "son_gun": gunler[-1] if gunler else None,
                   "etkin_gun": len(gunler)},
        "en_oturum": [{"id": sid, "tarih": ilk_gun_oturum.get(sid, ""), "usd": round(v, 4)} for sid, v in en],
    }
    for t in ("gun", "hafta", "ay", "yil"):
        V[t] = gorunum(kayitlar, t, bugun, ilk_gun_oturum)
    return V


def kayitlari_getir(onbellek=True):
    """CLI/JSON: stored history + a fresh scan (writes nothing). Returns the day records; the hour records are left in kayitlari_getir.saat."""
    tg, ts = tarama_ve_kayitlar(tara(onbellek))
    son, _, _ = birlestir(gunluk_oku(), tg, bugun_str())
    kayitlari_getir.saat = saatlik_birlestir(saatlik_oku(), ts, int(time.time() // 3600))
    return son


def pencere_hesapla(kayitlar, saat, simdi=None):
    """Three rolling windows (last 24h / 7d / 30d): hour buckets + daily completion at the boundary (pencere_parcalar, maliyet.py)."""
    simdi_h = int((time.time() if simdi is None else simdi) // 3600)
    e = min(saat) if saat else None
    su = {h: r.get("usd", 0.0) for h, r in saat.items()}
    gu = {d: r.get("usd", 0.0) for d, r in kayitlar.items()}
    out = []
    for ad, n in PENCERELER:
        P = {"etiket": ad, "gun": n, "usd": 0.0, "ana": {"usd": 0.0}, "alt": {"usd": 0.0}, "danisman": {"usd": 0.0, "n": 0},
             "model": {}, "saat_say": 0, "tamamlanan_usd": 0.0}
        for tur, k, w in pencere_parcalar(n, simdi_h, e, su, gu):
            if tur == "s":
                r = saat[k]
                P["usd"] += r.get("usd", 0.0)
                P["ana"]["usd"] += r.get("ana", 0.0)
                P["alt"]["usd"] += r.get("alt", 0.0)
                P["danisman"]["usd"] += r.get("dan", 0.0)
                for m, v in (r.get("model") or {}).items():
                    P["model"][m] = {"usd": P["model"].get(m, {"usd": 0.0})["usd"] + v}
                P["saat_say"] += 1
            else:
                r = kayitlar[k]
                P["usd"] += r.get("usd", 0.0) * w
                P["tamamlanan_usd"] += r.get("usd", 0.0) * w
                P["ana"]["usd"] += r.get("ana", {}).get("usd", 0.0) * w
                P["alt"]["usd"] += r.get("alt", {}).get("usd", 0.0) * w
                P["danisman"]["usd"] += r.get("dan", {}).get("usd", 0.0) * w
                for m, v in (r.get("model") or {}).items():
                    P["model"][m] = {"usd": P["model"].get(m, {"usd": 0.0})["usd"] + v.get("usd", 0.0) * w}
        P["model"] = dict(sorted(P["model"].items(), key=lambda kv: -kv[1]["usd"]))
        out.append(P)
    return out


def pencere_satirlari(PL):
    L = [baslik("KAYAN PENCERELER (şu andan geriye; saat kovaları, kısmi içinde bulunulan saat dahil)")]
    for P in PL:
        s = "  %-9s %s · ana %s · alt %s" % (P["etiket"], para(P["usd"]), para(P["ana"]["usd"]), para(P["alt"]["usd"]))
        if P["danisman"]["usd"] > 0:
            s += " · danışman %s" % para(P["danisman"]["usd"])
        L.append(s)
        if P["usd"] > 0:  # an empty window shows no model line
            L.append("    modeller: " + model_bolum(P))
        if P["tamamlanan_usd"] > 0.005:
            L.append("    (%s saatlik kayıttan önceki döneme ait; günlük toplamdan orantılı tamamlandı)" % para(P["tamamlanan_usd"]))
    return L


# ------------------------------------------------------------------ CLI output
def tr(x, nd=2):
    return format(x, ",.%df" % nd).translate({44: 46, 46: 44})


def para(x, nd=2):
    return "$" + tr(x, nd)


def yuzde(x, nd=1):
    return "%" + tr(x * 100, nd)


def model_bolum(satir, en_cok=4):
    toplam = satir["usd"] or 1
    parcalar = []
    for m, v in list(satir["model"].items()):
        if v["usd"] <= 0:
            continue
        parcalar.append((kisa_model(m), v["usd"]))
    ilk = parcalar[:en_cok]
    diger = sum(v for _, v in parcalar[en_cok:])
    s = " · ".join("%s %s (%s)" % (a, para(v, 0 if v >= 100 else 2), "%<1" if 100 * v / toplam < 0.5 else "%%%d" % round(100 * v / toplam)) for a, v in ilk)
    if diger > 0:
        s += " · diğer %s" % para(diger)
    return s


def ana_alt_bolum(satir):
    s = "ana oturum %s · alt ajan %s" % (para(satir["ana"]["usd"]), para(satir["alt"]["usd"]))
    if satir["danisman"]["usd"] > 0:
        s += " · danışman %s (%d çağrı, modeller içinde)" % (para(satir["danisman"]["usd"]), satir["danisman"]["n"])
    return s


def ozet_satir(satir):
    ib = satir["isabet"]
    return "istek %s · oturum %d · ajan koşusu %d%s" % (tr(satir["istek"], 0), satir["oturum_say"], satir["ajan_kosu"],
                                                         " · önbellek " + yuzde(ib) if ib is not None else "")


def tablo(satirlar, ilk_kolon, genislik=24):
    L = []
    L.append("  %-*s %9s %9s %9s %9s %7s %7s" % (genislik, ilk_kolon, "$", "ana", "alt", "$/gün", "istek", "önb"))
    for s in satirlar:
        ib = "%" + str(round(s["isabet"] * 100)) if s["isabet"] is not None else "-"
        L.append("  %-*s %9s %9s %9s %9s %7s %7s" % (
            genislik, s["etiket"] + ("*" if s["kismi"] else ""), tr(s["usd"]), tr(s["ana"]["usd"]), tr(s["alt"]["usd"]),
            tr(s["usd_gun"]), tr(s["istek"], 0), ib))
    return L


def ozet_sonu(satirlar, tur, ad):
    o = ozet_hesapla(satirlar, tur)
    if not o.get("toplam_usd"):
        return []
    L = []
    s = "  toplam %s · ort. %s/etkin gün (%d gün)" % (para(o["toplam_usd"]), para(o["ort_etkin_gun_usd"]), o["etkin_gun"])
    if o["havuz_say"] > 1:
        s += " · ort. %s/%s (%s)" % (para(o["ort_donem_usd"]), ad, o["ort_donem_not"])
    L.append(s)
    if o["havuz_say"] > 1:
        L.append("  en yoğun %s %s · en ucuz %s %s (%s)" % (o["en_yogun"]["etiket"], para(o["en_yogun"]["usd"]),
                                                           o["en_ucuz"]["etiket"], para(o["en_ucuz"]["usd"]), o["en_ucuz_not"]))
    return L


def birlesik_model(satirlar):
    A = {"usd": 0.0, "model": {}, "ana": {"usd": 0.0}, "alt": {"usd": 0.0}, "danisman": {"usd": 0.0, "n": 0}}
    for s in satirlar:
        A["usd"] += s["usd"]
        A["ana"]["usd"] += s["ana"]["usd"]
        A["alt"]["usd"] += s["alt"]["usd"]
        A["danisman"]["usd"] += s["danisman"]["usd"]
        A["danisman"]["n"] += s["danisman"]["n"]
        for m, v in s["model"].items():
            A["model"][m] = {"usd": A["model"].get(m, {"usd": 0.0})["usd"] + v["usd"]}
    A["model"] = dict(sorted(A["model"].items(), key=lambda kv: -kv[1]["usd"]))
    return A


def baslik(t):
    return "\n" + t + "\n" + "─" * min(len(t), 60)


def en_oturum_satirlari(liste, girinti="  "):
    L = []
    for i, o in enumerate(liste, 1):
        L.append("%s%2d. %s  %s  %s" % (girinti, i, o["id"][:8], o["tarih"][8:10] + "." + o["tarih"][5:7] if o["tarih"] else "?", para(o["usd"])))
    return L


def fiyatsiz_satiri(V):
    """Warning line when some tokens belong to a model that has no price (their cost is NOT in the totals)."""
    n = sum(x.get("fiyatsiz_token", 0) for x in V["gun"]["satirlar"])
    if not n:
        return []
    return ["", "UYARI: %s token fiyat tablosunda olmayan bir modele ait; maliyete dahil değil (maliyet-fiyatlar.json'a ekleyin veya yerel fiyat dosyasıyla geçersiz kılın)." % tr(n, 0)]


def varsayilan_rapor(V):
    """Summary report. A section with no data is not printed at all (no empty headings)."""
    L = []
    k = V["kapsam"]
    if not k["ilk_gun"]:
        return ["Veri yok (%s altında kullanılabilir transkript bulunamadı)." % os.path.join(CLAUDE, "projects")]
    L.append("Claude Code maliyeti · API liste fiyatı karşılığı (USD), fatura değil · danışman çağrıları dahil")
    L.append("Veri: %s … %s · %d etkin gün · gün sınırı %s · * = kısmi dönem" % (k["ilk_gun"], k["son_gun"], k["etkin_gun"], V["saat_dilimi"]))
    gun = V["gun"]["satirlar"]
    bugun = V["bugun"]
    if any(P["usd"] > 0 for P in V["pencere"]):
        L += pencere_satirlari(V["pencere"])
    b = [s for s in gun if s["anahtar"] == bugun]
    if b:
        s = b[0]
        L.append(baslik("BUGÜN"))
        L.append("  %s  %s · %s" % (s["etiket"], para(s["usd"]), ozet_satir(s)))
        L.append("  modeller: " + model_bolum(s))
        L.append("  " + ana_alt_bolum(s))
    bd = datetime.date.fromisoformat(bugun)
    s7 = [s for s in gun if datetime.date.fromisoformat(s["anahtar"]) > bd - datetime.timedelta(days=7)]
    if s7:
        L.append(baslik("SON 7 GÜN"))
        L += tablo(s7, "gün")
        t = sum(s["usd"] for s in s7)
        yog = max(s7, key=lambda s: s["usd"])
        ucuz = min((s for s in s7 if s["anahtar"] != bugun), key=lambda s: s["usd"], default=None)
        L.append("  toplam %s · ort. %s/etkin gün (%d gün)%s · en yoğun %s %s%s" % (
            para(t), para(t / len(s7)), len(s7), " (bugün dahil)" if b else "", yog["etiket"], para(yog["usd"]),
            " · en ucuz %s %s" % (ucuz["etiket"], para(ucuz["usd"])) if ucuz else ""))
        L.append("  modeller: " + model_bolum(birlesik_model(s7)))
        L.append("  " + ana_alt_bolum(birlesik_model(s7)))
    h = V["hafta"]["satirlar"]
    son4 = [s for s in h if datetime.date.fromisoformat(s["bit"]) >= bd - datetime.timedelta(days=bd.weekday() + 21)]
    if son4:
        L.append(baslik("SON 4 HAFTA"))
        L += tablo(son4, "hafta")
        L += ozet_sonu(son4, "hafta", "hafta")
        L.append("  modeller: " + model_bolum(birlesik_model(son4)))
    ay = V["ay"]["satirlar"][-6:]
    if ay:
        L.append(baslik("AYLAR"))
        L += tablo(ay, "ay")
        L += ozet_sonu(ay, "ay", "ay")
        L.append("  modeller: " + model_bolum(birlesik_model(ay)))
    yl = V["yil"]["satirlar"]
    if yl:
        L.append(baslik("YILLAR"))
        L += tablo(yl, "yıl")
        L += ozet_sonu(yl, "yil", "yıl")
        L.append("  modeller: " + model_bolum(birlesik_model(yl)))
        L.append("  (yıllık görünüm geçmiş biriktikçe dolar: Claude Code eski transkriptleri siler, günlük toplamlar gunluk.jsonl'de saklanır)")
    if V["en_oturum"]:
        L.append(baslik("EN PAHALI 10 OTURUM (tüm veri; kimlik ilk 8 karakter, tarih ilk görülen gün)"))
        L += en_oturum_satirlari(V["en_oturum"])
    return L + fiyatsiz_satiri(V)


def tek_gorunum(V, tur, son):
    ad = {"gun": "GÜN", "hafta": "HAFTA", "ay": "AY", "yil": "YIL"}[tur]
    tekil = {"gun": "gün", "hafta": "hafta", "ay": "ay", "yil": "yıl"}[tur]
    sat = V[tur]["satirlar"]
    if son:
        sat = sat[-son:]
    L = [baslik("%s GÖRÜNÜMÜ (veri olan dönemler)" % ad)]
    if not sat:
        return L + ["  Veri yok."]
    L += tablo(sat, tekil)
    L += ozet_sonu(sat, tur, tekil)
    A = birlesik_model(sat)
    L.append("  modeller: " + model_bolum(A, 6))
    L.append("  " + ana_alt_bolum(A))
    turler = defaultdict(lambda: [0.0, 0, 0])
    for s in sat:
        for t, v in s["tur"].items():
            turler[t][0] += v["usd"]
            turler[t][1] += v["n"]
            turler[t][2] += v.get("kosu", 0)
    if turler:
        L.append("  ajan türü: " + " · ".join("%s %s%s" % (t, para(v[0]), " (%d koşu)" % v[2] if v[2] else "")
                                             for t, v in sorted(turler.items(), key=lambda kv: -kv[1][0])[:8]))
    top = defaultdict(float)
    for s in sat:
        for o in s["en_oturum"]:
            top[(o["id"], o["tarih"])] += o["usd"]
    en = [{"id": i, "tarih": t, "usd": v} for (i, t), v in sorted(top.items(), key=lambda kv: -kv[1])[:10]]
    if en:
        L.append(baslik("EN PAHALI 10 OTURUM (gösterilen dönemler; kimlik ilk 8 karakter)"))
        L += en_oturum_satirlari(en)
    return L + fiyatsiz_satiri(V)


# ------------------------------------------------------------------ dashboard / flat tables
def kisa_etiket(tur, anahtar):
    if tur == "gun":
        return anahtar[8:10] + "." + anahtar[5:7]
    if tur == "hafta":
        return "H" + anahtar.split("-W")[1]
    if tur == "ay":
        return ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"][int(anahtar[5:7]) - 1] + " " + anahtar[2:4]
    return anahtar


def panel_tablolari(V):
    """V (tum_veri) -> {donemler, kirilimlar, ozet}: flat row lists (no text; only numbers, dates, model/type/project names, session ids)."""
    donemler, kirilim, ozet = [], [], []
    oturum_hepsi = set()
    for tur in ("gun", "hafta", "ay", "yil"):
        sat = V[tur]["satirlar"]
        oz = V[tur]["ozet"]
        tok = defaultdict(int)
        dan = ana = alt = istek = kosu = 0.0
        for s in sat:
            sat_id = "%s|%s" % (tur, s["anahtar"])
            donemler.append({
                "satir": sat_id, "gorunum": tur, "anahtar": s["anahtar"], "etiket": s["etiket"],
                "kisa": kisa_etiket(tur, s["anahtar"]), "bas": s["bas"], "bit": s["bit"], "etkin_gun": s["etkin_gun"],
                "kismi": 1 if s["kismi"] else 0, "usd": s["usd"], "usd_gun": s["usd_gun"],
                "ana_usd": s["ana"]["usd"], "alt_usd": s["alt"]["usd"], "danisman_usd": s["danisman"]["usd"],
                "danisman_n": s["danisman"]["n"], "istek": s["istek"], "oturum": s["oturum_say"],
                "ajan_kosu": s["ajan_kosu"], "isabet": s["isabet"]})
            for a, v in s["tok"].items():
                tok[a] += v
            dan += s["danisman"]["usd"]
            ana += s["ana"]["usd"]
            alt += s["alt"]["usd"]
            istek += s["istek"]
            kosu += s["ajan_kosu"]
            top = s["usd"] or 1.0
            for m, v in s["model"].items():
                if v["usd"] > 0:
                    kirilim.append({"satir": "%s|model|%s" % (sat_id, m), "gorunum": tur, "anahtar": s["anahtar"], "boyut": "model",
                                    "ad": kisa_model(m), "usd": v["usd"], "pay": round(v["usd"] / top, 4), "istek": v["n"]})
            for t, v in s["tur"].items():
                if v["usd"] > 0:
                    kirilim.append({"satir": "%s|tur|%s" % (sat_id, t), "gorunum": tur, "anahtar": s["anahtar"], "boyut": "tur",
                                    "ad": t, "usd": v["usd"], "pay": round(v["usd"] / top, 4), "istek": v["n"], "kosu": v.get("kosu", 0)})
            for pr, v in s["proje"].items():
                if v["usd"] > 0:
                    kirilim.append({"satir": "%s|proje|%s" % (sat_id, pr), "gorunum": tur, "anahtar": s["anahtar"], "boyut": "proje",
                                    "ad": pr, "usd": v["usd"], "pay": round(v["usd"] / top, 4), "istek": v["n"]})
            for i, o in enumerate(s["en_oturum"], 1):
                oturum_hepsi.add(o["id"])
                kirilim.append({"satir": "%s|oturum|%s" % (sat_id, o["id"]), "gorunum": tur, "anahtar": s["anahtar"], "boyut": "oturum",
                                "ad": o["id"][:8], "usd": o["usd"], "pay": round(o["usd"] / top, 4), "tarih": o["tarih"], "sira": i})
        t = tok["in"] + tok["cr"] + tok["cw5"] + tok["cw1h"]
        ozet.append({
            "satir": tur, "gorunum": tur, "toplam_usd": oz.get("toplam_usd", 0), "etkin_gun": oz.get("etkin_gun", 0),
            "ort_etkin_gun_usd": oz.get("ort_etkin_gun_usd", 0), "ort_donem_usd": oz.get("ort_donem_usd", 0),
            "ort_donem_not": oz.get("ort_donem_not", ""),
            "en_yogun_etiket": (oz.get("en_yogun") or {}).get("etiket", ""), "en_yogun_usd": (oz.get("en_yogun") or {}).get("usd", 0),
            "en_ucuz_etiket": (oz.get("en_ucuz") or {}).get("etiket", ""), "en_ucuz_usd": (oz.get("en_ucuz") or {}).get("usd", 0),
            "donem_say": oz.get("donem_say", 0), "tam_donem_say": oz.get("tam_donem_say", 0), "havuz_say": oz.get("havuz_say", 0),
            "isabet": round(tok["cr"] / t, 4) if t else None, "danisman_usd": round(dan, 4), "ana_usd": round(ana, 4),
            "alt_usd": round(alt, 4), "istek": int(istek), "ajan_kosu": int(kosu),
            "olusturma": V["olusturma"], "bugun": V["bugun"], "ilk_gun": V["kapsam"]["ilk_gun"], "son_gun": V["kapsam"]["son_gun"],
            "veri_gun": V["kapsam"]["etkin_gun"]})
    return {"donemler": donemler, "kirilimlar": kirilim, "ozet": ozet}


PANEL_SABLON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maliyet-panel.html")


def panel_html(tablolar):
    """The static dashboard: the template with the flat tables embedded as JSON (no network needed to view it)."""
    with open(PANEL_SABLON, encoding="utf-8") as f:
        sablon = f.read()
    veri = json.dumps(tablolar, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return sablon.replace("__MALIYET_VERI__", veri)


def panel_yaz(dizin):
    V = tum_veri(kayitlari_getir(), bugun_str())
    tablolar = panel_tablolari(V)
    os.makedirs(dizin, mode=0o700, exist_ok=True)
    os.chmod(dizin, 0o700)
    for ad, satirlar in tablolar.items():
        yaz_0600(os.path.join(dizin, ad + ".json"), json.dumps(satirlar, ensure_ascii=False, separators=(",", ":")))
        print("%s: %d satır -> %s" % (ad, len(satirlar), os.path.join(dizin, ad + ".json")))
    try:
        yol = os.path.join(dizin, "index.html")
        yaz_0600(yol, panel_html(tablolar))
        print("panel: %s (tarayıcıda dosya olarak açın; ağ gerekmez)" % yol)
    except OSError as e:
        _uyari("panel şablonu okunamadı (%s); yalnız JSON tabloları yazıldı\n" % e)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="maliyet", description="Claude Code harcama istatistikleri (API liste fiyatı karşılığı, fatura değil; her şey yerel)")
    ap.add_argument("--gun", action="store_true", help="günlük görünüm")
    ap.add_argument("--hafta", action="store_true", help="haftalık görünüm (ISO)")
    ap.add_argument("--ay", action="store_true", help="aylık görünüm")
    ap.add_argument("--yil", action="store_true", help="yıllık görünüm")
    ap.add_argument("--pencere", action="store_true", help="kayan pencereler: son 24 saat / 7 gün / 30 gün (model dökümüyle)")
    ap.add_argument("--son", type=int, default=0, help="görünümde son N dönem")
    ap.add_argument("--json", action="store_true", help="tüm görünümleri JSON olarak yaz")
    ap.add_argument("--kaydet", action="store_true", help="günlük toplamları gunluk.jsonl dosyasına yaz")
    ap.add_argument("--yeniden", action="store_true", help="--kaydet: tarananları kayıtlıların üzerine yaz (fiyat tablosu değişince)")
    ap.add_argument("--panel", nargs="?", const=os.path.join(DIZIN, "panel"), metavar="DİZİN", help="düz JSON tabloları ve statik panel index.html yaz (varsayılan: <claude dizini>/maliyet/panel)")
    ap.add_argument("--paylas", action="store_true", help="günlük toplamları kaydet, sonra bugünkü kullanım satırını ortak GitHub issue'suna yaz (isteğe bağlı; hata verirse sessizce günlüğe yazar)")
    ap.add_argument("--otomatik", action="store_true", help="--paylas ile: durum çubuğunun aralıklı çalıştırması (haftalık %% değiştiyse satırı günceller; yoksa hiçbir şey yazmaz)")
    ap.add_argument("--siralama", action="store_true", help="--paylas ile: ortak issue'daki \"Sıralama\" yorumunu da güncelle (issue yoksa oluşturur; tek makinede çalıştırın)")
    ap.add_argument("--kuru", action="store_true", help="--paylas ile: satırı yalnız yazdır, GitHub'a hiçbir şey yazma")
    ap.add_argument("--durum", action="store_true", help="durum çubuğu satırı (önbellekten)")
    ap.add_argument("--durum-yenile", action="store_true", help="durum önbelleğini yenile")
    ap.add_argument("--onbellek-yok", action="store_true", help="dosya önbelleğini kullanma (tam tarama)")
    a = ap.parse_args(argv)
    try:
        return calistir(a)
    except AO.FiyatHatasi as e:
        _uyari("HATA: %s\n" % e)
        return 2


def calistir(a):
    _tz()
    if a.durum:
        return durum_yaz()
    if a.durum_yenile:
        return durum_yenile()
    if a.panel:
        return panel_yaz(a.panel)
    if a.paylas and a.otomatik:
        import maliyet_paylas
        maliyet_paylas.paylas(otomatik=True, hazirla=lambda: kaydet(a.yeniden, not a.onbellek_yok))
        return 0
    if a.paylas or a.siralama:
        rc = kaydet(a.yeniden, not a.onbellek_yok)   # the totals are recorded first (same as --kaydet); only the sharing is silent on errors
        import maliyet_paylas
        maliyet_paylas.paylas(siralama=a.siralama, kuru=a.kuru)
        return rc
    if a.kaydet:
        return kaydet(a.yeniden, not a.onbellek_yok)
    kayitlar = kayitlari_getir(not a.onbellek_yok)
    V = tum_veri(kayitlar, bugun_str())
    V["pencere"] = pencere_hesapla(kayitlar, kayitlari_getir.saat)
    if a.json:
        json.dump(V, sys.stdout, ensure_ascii=False, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0
    secili = [t for t in ("gun", "hafta", "ay", "yil") if getattr(a, t)]
    if a.pencere:
        L = pencere_satirlari(V["pencere"])
    elif secili:
        L = []
        for t in secili:
            L += tek_gorunum(V, t, a.son)
    else:
        L = varsayilan_rapor(V)
    print("\n".join(L))
    return 0



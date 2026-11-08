#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Transcript reader and price table for maliyet (stdlib only, no network, no prompt text is kept).

Reads Claude Code session transcripts:
  <config dir>/projects/<project>/<session>.jsonl                          main conversation
  <config dir>/projects/<project>/<session>/subagents/agent-*.jsonl        sub-agents (+ .meta.json: agentType only)
  <config dir>/projects/<project>/<session>/subagents/workflows/<wf>/agent-*.jsonl   Workflow sub-agents
The config dir is $CLAUDE_CONFIG_DIR, else ~/.claude.

Duplicates: Claude Code copies the earlier history into a resumed session's transcript (and copies the sub-agent files into the
new session folder), so the same API response can sit in several files. A response is identified by istek_anahtari() and is
counted ONCE across all files (maliyet_cekirdek.sahiplik picks the single file that owns it).

Prices live in ONE data file, maliyet-fiyatlar.json, next to this file. A local file replaces single models
(or the write multipliers) without touching the packaged copy: $MALIYET_FIYATLAR (a path) or <config dir>/maliyet/fiyatlar.json.
"""
import calendar
import hashlib
import json
import os

SURUM = 2  # parser version: part of the file-cache key, bump when the parsing logic changes
HOME = os.path.expanduser("~")
CLAUDE = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
PROJ = os.path.join(CLAUDE, "projects")
FIYAT_PAKET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maliyet-fiyatlar.json")
FIYAT_YEREL = os.path.join(CLAUDE, "maliyet", "fiyatlar.json")


# ------------------------------------------------------------------ helpers
_ts_onbellek = {}


def ts_epoch(s):
    """'2026-10-02T10:25:33.736Z' -> epoch seconds (float, UTC)."""
    k = s[:19]
    base = _ts_onbellek.get(k)
    if base is None:
        base = calendar.timegm((int(s[0:4]), int(s[5:7]), int(s[8:10]), int(s[11:13]), int(s[14:16]), int(s[17:19]), 0, 0, 0))
        if len(_ts_onbellek) > 300000:
            _ts_onbellek.clear()
        _ts_onbellek[k] = base
    frac = 0.0
    if len(s) > 20 and s[19] == ".":
        j = 20
        while j < len(s) and s[j].isdigit():
            j += 1
        if j > 20:
            frac = float("0." + s[20:j])
    return base + frac


def _int(x):
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------ price table
class FiyatHatasi(Exception):
    pass


def _json_oku(yol):
    with open(yol, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict):
        raise ValueError("root is not an object")
    return d


def fiyat_yukle():
    """Packaged price table, with the local override merged on top (model keys replace, the rest is kept).
    Returns the merged dict; raises FiyatHatasi when the packaged file is unreadable."""
    try:
        d = _json_oku(FIYAT_PAKET)
    except (OSError, ValueError) as e:
        raise FiyatHatasi("Fiyat tablosu okunamadı: %s (%s)" % (FIYAT_PAKET, e))
    yerel = os.environ.get("MALIYET_FIYATLAR") or FIYAT_YEREL
    if os.path.isfile(yerel):
        try:
            o = _json_oku(yerel)
        except (OSError, ValueError) as e:
            raise FiyatHatasi("Yerel fiyat dosyası geçersiz: %s (%s)" % (yerel, e))
        d["modeller"] = dict(d.get("modeller") or {}, **(o.get("modeller") or {}))
        if o.get("yazim_carpani"):
            d["yazim_carpani"] = dict(d.get("yazim_carpani") or {}, **o["yazim_carpani"])
        d["yerel_dosya"] = yerel
    return d


def fiyat_ozeti(d):
    """Hash of the effective prices: changes whenever the packaged table or an override changes (cache key)."""
    return hashlib.md5(json.dumps(d, sort_keys=True).encode("utf-8")).hexdigest()


class Fiyat:
    """USD per 1M tokens; model id matched by PREFIX, longest prefix wins. Unknown model -> bul() returns None."""

    def __init__(self, d):
        self.m = d.get("modeller", {})
        c = d.get("yazim_carpani", {})
        self.w5 = float(c.get("5m", 1.25))
        self.w1 = float(c.get("1h", 2.0))
        self.anahtarlar = sorted(self.m, key=len, reverse=True)
        self._o = {}

    def bul(self, model):
        if model in self._o:
            return self._o[model]
        p = None
        for k in self.anahtarlar:
            if model.startswith(k):
                p = self.m[k]
                break
        self._o[model] = p
        return p


def istek_maliyeti(fiyat, r):
    """API list-price cost of ONE parsed response (the single cost formula: the daily/hourly records, the sub-agent row and the
    calibration all use it). r = a dict as ayristir() returns it. Returns (usd, advisor, unpriced_tokens):
    usd = cost of the response itself, None when its model is not in the price table (then its tokens count as unpriced);
    advisor = [(advisor model, usd)] for the advisor calls inside the response (an unpriced advisor model is skipped and its tokens
    counted as unpriced). Long-prompt tier (e.g. Haiku 5.5 over 100k prompt tokens) multiplies the whole response."""
    fiyatsiz = 0
    pr = fiyat.bul(r["model"])
    if pr is None:
        usd = None
        fiyatsiz += r["in"] + r["out"] + r["cr"] + r["cw5"] + r["cw1h"]
    else:
        prompt = r["in"] + r["cr"] + r["cw5"] + r["cw1h"]
        esik = pr.get("uzun_istem_esik")
        carpan = float(pr.get("uzun_istem_carpan", 1)) if esik and prompt > esik else 1.0
        i = pr["in"]
        usd = (r["in"] * i + r["out"] * pr["out"] + r["cr"] * pr["read"]
               + r["cw5"] * fiyat.w5 * i + r["cw1h"] * fiyat.w1 * i) / 1e6 * carpan
    adv = []
    for ad in r.get("adv") or []:
        pa = fiyat.bul(ad["model"])
        if pa is None:
            fiyatsiz += ad["in"] + ad["out"] + ad["cr"] + ad["cw5"] + ad["cw1h"]
            continue
        adv.append((ad["model"], (ad["in"] * pa["in"] + ad["out"] * pa["out"] + ad["cr"] * pa["read"]
                                  + (ad["cw5"] * fiyat.w5 + ad["cw1h"] * fiyat.w1) * pa["in"]) / 1e6))
    return usd, adv, fiyatsiz


# ------------------------------------------------------------------ transcript parsing
def istek_anahtari(o, m):
    """Stable identity of ONE API response: message.id, else requestId, else the line's uuid.
    Why message.id first: in real transcripts message.id and requestId are 1:1 (never one id with two requestIds or the other
    way round), and every copy of a response carries both, so either one identifies it; message.id is the one the API assigns.
    uuid is the last resort only: it is per transcript LINE (a streamed response is written as several lines with different
    uuids), so it cannot merge streaming chunks and counts each line once. Claude Code writes message.id on every assistant line."""
    k = m.get("id") or o.get("requestId") or o.get("uuid")
    return k if isinstance(k, str) and k else None


def anahtar_ozet(k):
    """Short stable hash of a request key (stored in the file cache instead of the id itself; 48 bits)."""
    return hashlib.blake2b(k.encode("utf-8", "replace"), digest_size=6).hexdigest()


def yeni_istek(ts, model, kh):
    """Empty accumulator for one API response (usage_birlestir fills it, istek_bitir finalizes it)."""
    return {"ts": ts, "model": model, "k": kh, "in": 0, "out": 0, "cr": 0, "_cc": 0, "_w1": 0, "_w5": 0, "adv": []}


def usage_birlestir(r, u):
    """Merge one transcript line's usage block into the response accumulator: per-field MAXIMUM (a streamed response is written as
    several lines with a growing usage; never summed). Advisor iterations: the last line that carries them wins."""
    r["in"] = max(r["in"], _int(u.get("input_tokens")))
    r["cr"] = max(r["cr"], _int(u.get("cache_read_input_tokens")))
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    r["_cc"] = max(r["_cc"], _int(u.get("cache_creation_input_tokens")))
    r["_w1"] = max(r["_w1"], _int(cc.get("ephemeral_1h_input_tokens")))
    r["_w5"] = max(r["_w5"], _int(cc.get("ephemeral_5m_input_tokens")))
    r["out"] = max(r["out"], _int(u.get("output_tokens")))
    adv = []
    for itr in u.get("iterations") or []:
        if not isinstance(itr, dict) or itr.get("type") != "advisor_message":
            continue
        acc = itr.get("cache_creation") if isinstance(itr.get("cache_creation"), dict) else {}
        a1 = _int(acc.get("ephemeral_1h_input_tokens"))
        a5 = _int(acc.get("ephemeral_5m_input_tokens"))
        atoplam = _int(itr.get("cache_creation_input_tokens"))
        if a1 + a5 < atoplam:
            a5 += atoplam - (a1 + a5)
        adv.append({"model": itr.get("model") or "", "in": _int(itr.get("input_tokens")),
                    "out": _int(itr.get("output_tokens")), "cr": _int(itr.get("cache_read_input_tokens")),
                    "cw5": a5, "cw1h": a1})
    if len(adv) >= len(r["adv"]):
        r["adv"] = adv   # the last line that carries the advisor iterations wins (never summed)


def istek_bitir(r):
    """Finalize an accumulator in place: cache writes split into 5m / 1h (unattributed remainder counts as 5m)."""
    w1, w5, toplam_cc = r.pop("_w1"), r.pop("_w5"), r.pop("_cc")
    if w1 + w5 < toplam_cc:
        w5 += toplam_cc - (w1 + w5)
    r["cw5"], r["cw1h"] = w5, w1
    return r


def ayristir(yol, haric=None):
    """Read one transcript line by line. One dict per API response (see istek_anahtari):
    ts (first line of the response), model, in, out, cr (cache read), cw5 / cw1h (cache write 5m / 1h), adv (advisor calls),
    k (hash of the key). Streaming repeats the same response as several lines with a growing usage: the FINAL usage counts,
    taken as the per-field maximum over the lines (usage only grows within one response), never the sum.
    haric: set of key hashes owned by another file (see maliyet_cekirdek.sahiplik); those responses are skipped here.
    Corrupt or partial lines are skipped. No prompt/answer text is read into the result. Returns {"reqs": [...], "t0": first / "t1": last timestamp of the file (epoch, or None)}."""
    istekler = {}
    sira = []
    son = None
    ilk = None
    with open(yol, encoding="utf-8", errors="replace") as f:
        for satir in f:
            if len(satir) < 3:
                continue
            try:
                o = json.loads(satir)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            ts = o.get("timestamp")
            e = None
            if isinstance(ts, str):
                try:
                    e = ts_epoch(ts)
                except (ValueError, IndexError):
                    e = None
            if e is not None and (son is None or e > son):
                son = e
            if e is not None and (ilk is None or e < ilk):
                ilk = e
            if o.get("type") != "assistant" or o.get("isApiErrorMessage"):
                continue
            m = o.get("message")
            if not isinstance(m, dict):
                continue
            model = m.get("model") or ""
            if not isinstance(model, str) or not model or model.startswith("<"):
                continue
            u = m.get("usage")
            if not isinstance(u, dict):
                u = {}
            mid = istek_anahtari(o, m)
            if mid is None:
                continue
            kh = anahtar_ozet(mid)
            if haric and kh in haric:
                continue
            r = istekler.get(mid)
            if r is None:
                r = yeni_istek(e if e is not None else (son or 0.0), model, kh)
                istekler[mid] = r
                sira.append(r)
            usage_birlestir(r, u)
    for r in sira:
        istek_bitir(r)
    return {"reqs": sira, "t0": ilk, "t1": son}


def dosyalari_listele():
    """[(kind, path, project, session)]; kind is 'ana' (main conversation) or 'ajan' (sub-agent, incl. Workflow agents)."""
    out = []
    if not os.path.isdir(PROJ):
        return out
    for proj in sorted(os.listdir(PROJ)):
        pd = os.path.join(PROJ, proj)
        if not os.path.isdir(pd):
            continue
        for ad in sorted(os.listdir(pd)):
            yol = os.path.join(pd, ad)
            if os.path.isfile(yol) and ad.endswith(".jsonl"):
                out.append(("ana", yol, proj, ad[:-6]))
            elif os.path.isdir(yol):
                sd = os.path.join(yol, "subagents")
                if not os.path.isdir(sd):
                    continue
                for a in sorted(os.listdir(sd)):
                    if a.startswith("agent-") and a.endswith(".jsonl"):
                        out.append(("ajan", os.path.join(sd, a), proj, ad))
                wd = os.path.join(sd, "workflows")
                if os.path.isdir(wd):
                    for wf in sorted(os.listdir(wd)):
                        wfd = os.path.join(wd, wf)
                        if not os.path.isdir(wfd):
                            continue
                        for a in sorted(os.listdir(wfd)):
                            if a.startswith("agent-") and a.endswith(".jsonl"):
                                out.append(("ajan", os.path.join(wfd, a), proj, ad))
    return out


def stat_imza(yol):
    try:
        st = os.stat(yol)
        return [st.st_size, st.st_mtime_ns]
    except OSError:
        return None

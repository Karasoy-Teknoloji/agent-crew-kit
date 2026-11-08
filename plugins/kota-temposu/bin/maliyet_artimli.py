#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Incremental API-price cost of ONE transcript file (used by the sub-agent status-line row; stdlib only, no network).

The status line refreshes often, so a transcript is never re-parsed from the start: per file a small state records the byte offset
after the last COMPLETE line, the summed cost of the closed responses and the one response that is still open (a streamed
response is written as several consecutive lines with a growing usage; its final usage counts, so it is closed only when the next
response starts). A refresh reads only the bytes appended since the last call. Parsing and pricing are the shared ones from
maliyet_ayristir (usage_birlestir, istek_maliyeti, maliyet-fiyatlar.json): one price table, one formula.

State: <config dir>/maliyet/ajan/<name>.json (0600, written atomically); a state older than 7 days is deleted when a new one is made.
Never raises to the caller: toplam() returns None when anything is wrong (the caller then shows no cost).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I does not add the script directory

import maliyet_ayristir as AO  # noqa: E402

DIZIN = os.path.join(AO.CLAUDE, "maliyet", "ajan")
ESKI = 7 * 86400
KUYRUK = 16 * 1024 * 1024    # first read of a long main transcript: only its last 16 MB
SON_KAPALI = 24     # keys of the last closed responses kept (guards against a response repeated later in the file)
_fiyat = None


def _fiyat_tablosu():
    global _fiyat
    if _fiyat is None:
        _fiyat = AO.Fiyat(AO.fiyat_yukle())
    return _fiyat


def _durum_yolu(ad):
    guvenli = "".join(c if c.isalnum() or c in "-_" else "_" for c in ad)[:120]
    return os.path.join(DIZIN, guvenli + ".json")


def _durum_oku(yol):
    try:
        with open(yol, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("v") == 1:
            return d
    except (OSError, ValueError):
        pass
    return None


def _durum_yaz(yol, d):
    os.makedirs(DIZIN, mode=0o700, exist_ok=True)
    tmp = "%s.tmp%d" % (yol, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, separators=(",", ":"))
    os.replace(tmp, yol)


def _eskileri_sil():
    sinir = time.time() - ESKI
    try:
        for ad in os.listdir(DIZIN):
            y = os.path.join(DIZIN, ad)
            try:
                if os.stat(y).st_mtime < sinir:
                    os.remove(y)
            except OSError:
                pass
    except OSError:
        pass


def _yeni_durum():
    return {"v": 1, "off": 0, "usd": 0.0, "acik": None, "kapali": [], "fz": 0}


def _kapat(d, fiyat):
    """Close the open response: add its cost to the sum."""
    a = d["acik"]
    if a is None:
        return
    r = AO.istek_bitir(dict(a, adv=list(a["adv"])))
    usd, advler, fz = AO.istek_maliyeti(fiyat, r)
    d["usd"] += (usd or 0.0) + sum(x for _m, x in advler)
    d["fz"] += fz
    d["kapali"] = (d["kapali"] + [a["k"]])[-SON_KAPALI:]
    d["acik"] = None


def _gercek_istem(o):
    """True for a line that is the user's own prompt (not a tool result, a meta/system injection or a compact summary)."""
    if o.get("type") != "user" or o.get("isSidechain") or o.get("isMeta") or o.get("isCompactSummary"):
        return False
    m = o.get("message")
    c = m.get("content") if isinstance(m, dict) else None
    if isinstance(c, str):
        return bool(c.strip())
    if isinstance(c, list):
        tipler = {x.get("type") for x in c if isinstance(x, dict)}
        return "tool_result" not in tipler and bool(tipler & {"text", "image"})
    return False


def _satir_isle(d, satir, fiyat, ana=False):
    """ana=True (the main session file): a real user prompt starts a new turn (tur0 = cost closed so far) and sidechain lines are skipped."""
    try:
        o = json.loads(satir)
    except ValueError:
        return
    if not isinstance(o, dict):
        return
    if ana and _gercek_istem(o):
        _kapat(d, fiyat)
        d["tur0"] = d["usd"]
        return
    if o.get("type") != "assistant" or o.get("isApiErrorMessage") or (ana and o.get("isSidechain")):
        return
    m = o.get("message")
    if not isinstance(m, dict):
        return
    model = m.get("model") or ""
    if not isinstance(model, str) or not model or model.startswith("<"):
        return
    mid = AO.istek_anahtari(o, m)
    if mid is None:
        return
    kh = AO.anahtar_ozet(mid)
    a = d["acik"]
    if a is None or a["k"] != kh:
        if kh in d["kapali"]:
            return              # a response already counted, repeated later in the file
        _kapat(d, fiyat)
        a = d["acik"] = AO.yeni_istek(0.0, model, kh)
    u = m.get("usage")
    AO.usage_birlestir(a, u if isinstance(u, dict) else {})


def toplam(yol, ad=None):
    """API-price USD of everything written to the transcript so far (the open response included), reading only new bytes.
    Returns None when the file is unreadable or the cost cannot be priced at all (unknown model only)."""
    r = _hesapla(yol, ad, False)
    return None if r is None else r[0]


def tur_toplam(yol, ad):
    """API-price USD of the MAIN session's own responses since its last real user prompt (the last action; agents excluded:
    they run in subagent files / sidechain lines and have their own rows). Incremental like toplam(); the first read of a long
    transcript starts at its last KUYRUK bytes. None when it cannot be computed."""
    r = _hesapla(yol, "ana-" + ad, True, yol)
    return None if r is None else r[1]


def _hesapla(yol, ad, ana, okunacak=None):
    """(total USD, USD since the last real user prompt) of one transcript file, or None."""
    yol = okunacak or yol
    try:
        fiyat = _fiyat_tablosu()
        durum_yol = _durum_yolu(ad or yol)
        st = os.stat(yol)
        d = _durum_oku(durum_yol)
        yeni = d is None
        if yeni:
            d = _yeni_durum()
        if st.st_size < d["off"]:      # truncated or replaced: start over
            d = _yeni_durum()
            yeni = True
        degisti = yeni
        if yeni and ana and st.st_size > KUYRUK:     # long transcript, no state yet: start at a line boundary near the end
            with open(yol, "rb") as f:
                f.seek(st.st_size - KUYRUK)
                i = f.read(65536).find(b"\n")
            d["off"] = st.st_size - KUYRUK + (i + 1 if i >= 0 else 0)
        if st.st_size > d["off"]:
            with open(yol, "rb") as f:
                f.seek(d["off"])
                ham = f.read()
            son = ham.rfind(b"\n")
            if son >= 0:               # only complete lines; a half-written last line is read on the next call
                for satir in ham[: son + 1].splitlines():
                    if len(satir) > 2:
                        _satir_isle(d, satir.decode("utf-8", "replace"), fiyat, ana)
                d["off"] += son + 1
                degisti = True
        if degisti:
            if yeni:
                _eskileri_sil()
            try:
                _durum_yaz(durum_yol, d)
            except OSError:      # e.g. Windows refuses os.replace while another refresh reads the file: recomputed next time
                pass
        a = d["acik"]
        acik_usd = 0.0
        if a is not None:
            usd, advler, _fz = AO.istek_maliyeti(fiyat, AO.istek_bitir(dict(a, adv=list(a["adv"]))))
            acik_usd = (usd or 0.0) + sum(x for _m, x in advler)
        return d["usd"] + acik_usd, d["usd"] + acik_usd - d.get("tur0", 0.0)
    except Exception:
        return None


def ajan_dosyasi(transcript_path, ajan_id):
    """Path of a sub-agent transcript: <session transcript minus .jsonl>/subagents/agent-<id>.jsonl, else under workflows/<wf>/.
    None when it is not there (yet)."""
    if not isinstance(transcript_path, str) or not transcript_path.endswith(".jsonl") or not isinstance(ajan_id, str):
        return None
    if not ajan_id or any(c in ajan_id for c in "/\\"):
        return None
    sd = os.path.join(transcript_path[:-6], "subagents")
    ad = "agent-%s.jsonl" % ajan_id
    yol = os.path.join(sd, ad)
    if os.path.isfile(yol):
        return yol
    wd = os.path.join(sd, "workflows")
    try:
        for wf in os.listdir(wd):
            yol = os.path.join(wd, wf, ad)
            if os.path.isfile(yol):
                return yol
    except OSError:
        pass
    return None


def main(argv):
    """maliyet_artimli.py <transcript.jsonl>: print its API-price USD (diagnostics)."""
    if len(argv) != 2:
        sys.stderr.write(__doc__)
        return 2
    v = toplam(argv[1])
    print("-" if v is None else "%.4f" % v)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

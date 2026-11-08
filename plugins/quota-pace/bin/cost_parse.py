#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Transcript reader and price table for cost (stdlib only, no network, no prompt text is kept).

Reads Claude Code session transcripts:
  <config dir>/projects/<project>/<session>.jsonl                          main conversation
  <config dir>/projects/<project>/<session>/subagents/agent-*.jsonl        sub-agents (+ .meta.json: agentType only)
  <config dir>/projects/<project>/<session>/subagents/workflows/<wf>/agent-*.jsonl   Workflow sub-agents
The config dir is $CLAUDE_CONFIG_DIR, else ~/.claude.

Duplicates: Claude Code copies the earlier history into a resumed session's transcript (and copies the sub-agent files into the
new session folder), so the same API response can sit in several files. A response is identified by request_key() and is
counted ONCE across all files (cost_core.ownership picks the single file that owns it).

Prices live in ONE data file, cost-prices.json, next to this file. A local file replaces single models
(or the write multipliers) without touching the packaged copy: $COST_PRICES (a path) or <config dir>/cost/fiyatlar.json.
"""
import calendar
import hashlib
import json
import os

VERSION = 2  # parser version: part of the file-cache key, bump when the parsing logic changes
HOME = os.path.expanduser("~")
CLAUDE = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
PROJ = os.path.join(CLAUDE, "projects")
PRICE_PACKAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cost-prices.json")
PRICE_LOCAL = os.path.join(CLAUDE, "cost", "prices.json")


# ------------------------------------------------------------------ helpers
_ts_cache = {}


def ts_epoch(s):
    """'2026-10-02T10:25:33.736Z' -> epoch seconds (float, UTC)."""
    k = s[:19]
    base = _ts_cache.get(k)
    if base is None:
        base = calendar.timegm((int(s[0:4]), int(s[5:7]), int(s[8:10]), int(s[11:13]), int(s[14:16]), int(s[17:19]), 0, 0, 0))
        if len(_ts_cache) > 300000:
            _ts_cache.clear()
        _ts_cache[k] = base
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
class PriceError(Exception):
    pass


def _json_oku(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict):
        raise ValueError("root is not an object")
    return d


def load_price():
    """Packaged price table, with the local override merged on top (model keys replace, the rest is kept).
    Returns the merged dict; raises PriceError when the packaged file is unreadable."""
    try:
        d = _json_oku(PRICE_PACKAGE)
    except (OSError, ValueError) as e:
        raise PriceError("Fiyat tablosu okunamadı: %s (%s)" % (PRICE_PACKAGE, e))
    local = os.environ.get("COST_PRICES") or PRICE_LOCAL
    if os.path.isfile(local):
        try:
            o = _json_oku(local)
        except (OSError, ValueError) as e:
            raise PriceError("Yerel fiyat dosyası geçersiz: %s (%s)" % (local, e))
        d["models"] = dict(d.get("models") or {}, **(o.get("models") or {}))
        if o.get("writing_multiplier"):
            d["writing_multiplier"] = dict(d.get("writing_multiplier") or {}, **o["writing_multiplier"])
        d["local_file"] = local
    return d


def price_summary(d):
    """Hash of the effective prices: changes whenever the packaged table or an override changes (cache key)."""
    return hashlib.md5(json.dumps(d, sort_keys=True).encode("utf-8")).hexdigest()


class Price:
    """USD per 1M tokens; model id matched by PREFIX, longest prefix wins. Unknown model -> bul() returns None."""

    def __init__(self, d):
        self.m = d.get("models", {})
        c = d.get("writing_multiplier", {})
        self.w5 = float(c.get("5m", 1.25))
        self.w1 = float(c.get("1h", 2.0))
        self.keys = sorted(self.m, key=len, reverse=True)
        self._o = {}

    def find(self, model):
        if model in self._o:
            return self._o[model]
        p = None
        for k in self.keys:
            if model.startswith(k):
                p = self.m[k]
                break
        self._o[model] = p
        return p


def request_cost(price, r):
    """API list-price cost of ONE parsed response (the single cost formula: the daily/hourly records, the sub-agent row and the
    calibration all use it). r = a dict as ayristir() returns it. Returns (usd, advisor, unpriced_tokens):
    usd = cost of the response itself, None when its model is not in the price table (then its tokens count as unpriced);
    advisor = [(advisor model, usd)] for the advisor calls inside the response (an unpriced advisor model is skipped and its tokens
    counted as unpriced). Long-prompt tier (e.g. Haiku 5.5 over 100k prompt tokens) multiplies the whole response."""
    unpriced = 0
    pr = price.find(r["model"])
    if pr is None:
        usd = None
        unpriced += r["in"] + r["out"] + r["cr"] + r["cw5"] + r["cw1h"]
    else:
        prompt = r["in"] + r["cr"] + r["cw5"] + r["cw1h"]
        threshold = pr.get("long_prompt_threshold")
        multiplier = float(pr.get("long_prompt_multiplier", 1)) if threshold and prompt > threshold else 1.0
        i = pr["in"]
        usd = (r["in"] * i + r["out"] * pr["out"] + r["cr"] * pr["read"]
               + r["cw5"] * price.w5 * i + r["cw1h"] * price.w1 * i) / 1e6 * multiplier
    adv = []
    for name in r.get("adv") or []:
        pa = price.find(name["model"])
        if pa is None:
            unpriced += name["in"] + name["out"] + name["cr"] + name["cw5"] + name["cw1h"]
            continue
        adv.append((name["model"], (name["in"] * pa["in"] + name["out"] * pa["out"] + name["cr"] * pa["read"]
                                  + (name["cw5"] * price.w5 + name["cw1h"] * price.w1) * pa["in"]) / 1e6))
    return usd, adv, unpriced


# ------------------------------------------------------------------ transcript parsing
def request_key(o, m):
    """Stable identity of ONE API response: message.id, else requestId, else the line's uuid.
    Why message.id first: in real transcripts message.id and requestId are 1:1 (never one id with two requestIds or the other
    way round), and every copy of a response carries both, so either one identifies it; message.id is the one the API assigns.
    uuid is the last resort only: it is per transcript LINE (a streamed response is written as several lines with different
    uuids), so it cannot merge streaming chunks and counts each line once. Claude Code writes message.id on every assistant line."""
    k = m.get("id") or o.get("requestId") or o.get("uuid")
    return k if isinstance(k, str) and k else None


def key_summary(k):
    """Short stable hash of a request key (stored in the file cache instead of the id itself; 48 bits)."""
    return hashlib.blake2b(k.encode("utf-8", "replace"), digest_size=6).hexdigest()


def new_request(ts, model, kh):
    """Empty accumulator for one API response (merge_usage fills it, request_burn finalizes it)."""
    return {"ts": ts, "model": model, "k": kh, "in": 0, "out": 0, "cr": 0, "_cc": 0, "_w1": 0, "_w5": 0, "adv": []}


def merge_usage(r, u):
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


def request_burn(r):
    """Finalize an accumulator in place: cache writes split into 5m / 1h (unattributed remainder counts as 5m)."""
    w1, w5, total_cc = r.pop("_w1"), r.pop("_w5"), r.pop("_cc")
    if w1 + w5 < total_cc:
        w5 += total_cc - (w1 + w5)
    r["cw5"], r["cw1h"] = w5, w1
    return r


def parse(path, excluding=None):
    """Read one transcript line by line. One dict per API response (see request_key):
    ts (first line of the response), model, in, out, cr (cache read), cw5 / cw1h (cache write 5m / 1h), adv (advisor calls),
    k (hash of the key). Streaming repeats the same response as several lines with a growing usage: the FINAL usage counts,
    taken as the per-field maximum over the lines (usage only grows within one response), never the sum.
    haric: set of key hashes owned by another file (see cost_core.ownership); those responses are skipped here.
    Corrupt or partial lines are skipped. No prompt/answer text is read into the result. Returns {"reqs": [...], "t0": first / "t1": last timestamp of the file (epoch, or None)}."""
    requests = {}
    order = []
    last = None
    first = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if len(line) < 3:
                continue
            try:
                o = json.loads(line)
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
            if e is not None and (last is None or e > last):
                last = e
            if e is not None and (first is None or e < first):
                first = e
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
            mid = request_key(o, m)
            if mid is None:
                continue
            kh = key_summary(mid)
            if excluding and kh in excluding:
                continue
            r = requests.get(mid)
            if r is None:
                r = new_request(e if e is not None else (last or 0.0), model, kh)
                requests[mid] = r
                order.append(r)
            merge_usage(r, u)
    for r in order:
        request_burn(r)
    return {"reqs": order, "t0": first, "t1": last}


def list_files():
    """[(kind, path, project, session)]; kind is 'main' (main conversation) or 'agent' (sub-agent, incl. Workflow agents)."""
    out = []
    if not os.path.isdir(PROJ):
        return out
    for proj in sorted(os.listdir(PROJ)):
        pd = os.path.join(PROJ, proj)
        if not os.path.isdir(pd):
            continue
        for name in sorted(os.listdir(pd)):
            path = os.path.join(pd, name)
            if os.path.isfile(path) and name.endswith(".jsonl"):
                out.append(("main", path, proj, name[:-6]))
            elif os.path.isdir(path):
                sd = os.path.join(path, "subagents")
                if not os.path.isdir(sd):
                    continue
                for a in sorted(os.listdir(sd)):
                    if a.startswith("agent-") and a.endswith(".jsonl"):
                        out.append(("agent", os.path.join(sd, a), proj, name))
                wd = os.path.join(sd, "workflows")
                if os.path.isdir(wd):
                    for wf in sorted(os.listdir(wd)):
                        wfd = os.path.join(wd, wf)
                        if not os.path.isdir(wfd):
                            continue
                        for a in sorted(os.listdir(wfd)):
                            if a.startswith("agent-") and a.endswith(".jsonl"):
                                out.append(("agent", os.path.join(wfd, a), proj, name))
    return out


def stat_signature(path):
    try:
        st = os.stat(path)
        return [st.st_size, st.st_mtime_ns]
    except OSError:
        return None

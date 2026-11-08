#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Incremental API-price cost of ONE transcript file (used by the sub-agent status-line row; stdlib only, no network).

The status line refreshes often, so a transcript is never re-parsed from the start: per file a small state records the byte offset
after the last COMPLETE line, the summed cost of the closed responses and the one response that is still open (a streamed
response is written as several consecutive lines with a growing usage; its final usage counts, so it is closed only when the next
response starts). A refresh reads only the bytes appended since the last call. Parsing and pricing are the shared ones from
cost_parse (merge_usage, request_cost, cost-prices.json): one price table, one formula.

State: <config dir>/cost/agent/<name>.json (0600, written atomically); a state older than 7 days is deleted when a new one is made.
Never raises to the caller: total() returns None when anything is wrong (the caller then shows no cost).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I does not add the script directory

import cost_parse as CP  # noqa: E402

DIR = os.path.join(CP.CLAUDE, "cost", "agent")
OLD = 7 * 86400
QUEUE = 16 * 1024 * 1024    # first read of a long main transcript: only its last 16 MB
LAST_CLOSED = 24     # keys of the last closed responses kept (guards against a response repeated later in the file)
_price = None


def _price_table():
    global _price
    if _price is None:
        _price = CP.Price(CP.load_price())
    return _price


def _status_path(name):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:120]
    return os.path.join(DIR, safe + ".json")


def _read_status(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("v") == 2:
            return d
    except (OSError, ValueError):
        pass
    return None


def _write_status(path, d):
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    tmp = "%s.tmp%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, separators=(",", ":"))
    os.replace(tmp, path)


def _delete_old():
    limit = time.time() - OLD
    try:
        for name in os.listdir(DIR):
            y = os.path.join(DIR, name)
            try:
                if os.stat(y).st_mtime < limit:
                    os.remove(y)
            except OSError:
                pass
    except OSError:
        pass


def _new_status():
    return {"v": 2, "off": 0, "usd": 0.0, "open": None, "closed": [], "unpriced": 0}   # v2: English keys


def _close(d, price):
    """Close the open response: add its cost to the sum."""
    a = d["open"]
    if a is None:
        return
    r = CP.request_burn(dict(a, adv=list(a["adv"])))
    usd, advs, unpriced = CP.request_cost(price, r)
    d["usd"] += (usd or 0.0) + sum(x for _m, x in advs)
    d["unpriced"] += unpriced
    d["closed"] = (d["closed"] + [a["k"]])[-LAST_CLOSED:]
    d["open"] = None


def _real_prompt(o):
    """True for a line that is the user's own prompt (not a tool result, a meta/system injection or a compact summary)."""
    if o.get("type") != "user" or o.get("isSidechain") or o.get("isMeta") or o.get("isCompactSummary"):
        return False
    m = o.get("message")
    c = m.get("content") if isinstance(m, dict) else None
    if isinstance(c, str):
        return bool(c.strip())
    if isinstance(c, list):
        types = {x.get("type") for x in c if isinstance(x, dict)}
        return "tool_result" not in types and bool(types & {"text", "image"})
    return False


def _process_line(d, line, price, main_=False):
    """main_=True (the main session file): a real user prompt starts a new turn (turn0 = cost closed so far) and sidechain lines are skipped."""
    try:
        o = json.loads(line)
    except ValueError:
        return
    if not isinstance(o, dict):
        return
    if main_ and _real_prompt(o):
        _close(d, price)
        d["turn0"] = d["usd"]
        return
    if o.get("type") != "assistant" or o.get("isApiErrorMessage") or (main_ and o.get("isSidechain")):
        return
    m = o.get("message")
    if not isinstance(m, dict):
        return
    model = m.get("model") or ""
    if not isinstance(model, str) or not model or model.startswith("<"):
        return
    mid = CP.request_key(o, m)
    if mid is None:
        return
    kh = CP.key_summary(mid)
    a = d["open"]
    if a is None or a["k"] != kh:
        if kh in d["closed"]:
            return              # a response already counted, repeated later in the file
        _close(d, price)
        a = d["open"] = CP.new_request(0.0, model, kh)
    u = m.get("usage")
    CP.merge_usage(a, u if isinstance(u, dict) else {})


def total(path, name=None):
    """API-price USD of everything written to the transcript so far (the open response included), reading only new bytes.
    Returns None when the file is unreadable or the cost cannot be priced at all (unknown model only)."""
    r = _compute(path, name, False)
    return None if r is None else r[0]


def turn_total(path, name):
    """API-price USD of the MAIN session's own responses since its last real user prompt (the last action; agents excluded:
    they run in subagent files / sidechain lines and have their own rows). Incremental like total(); the first read of a long
    transcript starts at its last KUYRUK bytes. None when it cannot be computed."""
    r = _compute(path, "main-" + name, True, path)
    return None if r is None else r[1]


def _compute(path, name, main_, to_read=None):
    """(total USD, USD since the last real user prompt) of one transcript file, or None."""
    path = to_read or path
    try:
        price = _price_table()
        status_path = _status_path(name or path)
        st = os.stat(path)
        d = _read_status(status_path)
        new = d is None
        if new:
            d = _new_status()
        if st.st_size < d["off"]:      # truncated or replaced: start over
            d = _new_status()
            new = True
        changed = new
        if new and main_ and st.st_size > QUEUE:     # long transcript, no state yet: start at a line boundary near the end
            with open(path, "rb") as f:
                f.seek(st.st_size - QUEUE)
                i = f.read(65536).find(b"\n")
            d["off"] = st.st_size - QUEUE + (i + 1 if i >= 0 else 0)
        if st.st_size > d["off"]:
            with open(path, "rb") as f:
                f.seek(d["off"])
                ham = f.read()
            last = ham.rfind(b"\n")
            if last >= 0:               # only complete lines; a half-written last line is read on the next call
                for line in ham[: last + 1].splitlines():
                    if len(line) > 2:
                        _process_line(d, line.decode("utf-8", "replace"), price, main_)
                d["off"] += last + 1
                changed = True
        if changed:
            if new:
                _delete_old()
            try:
                _write_status(status_path, d)
            except OSError:      # e.g. Windows refuses os.replace while another refresh reads the file: recomputed next time
                pass
        a = d["open"]
        open_usd = 0.0
        if a is not None:
            usd, advs, _unpriced = CP.request_cost(price, CP.request_burn(dict(a, adv=list(a["adv"]))))
            open_usd = (usd or 0.0) + sum(x for _m, x in advs)
        return d["usd"] + open_usd, d["usd"] + open_usd - d.get("turn0", 0.0)
    except Exception:
        return None


def agent_file(transcript_path, agent_id):
    """Path of a sub-agent transcript: <session transcript minus .jsonl>/subagents/agent-<id>.jsonl, else under workflows/<wf>/.
    None when it is not there (yet)."""
    if not isinstance(transcript_path, str) or not transcript_path.endswith(".jsonl") or not isinstance(agent_id, str):
        return None
    if not agent_id or any(c in agent_id for c in "/\\"):
        return None
    sd = os.path.join(transcript_path[:-6], "subagents")
    name = "agent-%s.jsonl" % agent_id
    path = os.path.join(sd, name)
    if os.path.isfile(path):
        return path
    wd = os.path.join(sd, "workflows")
    try:
        for wf in os.listdir(wd):
            path = os.path.join(wd, wf, name)
            if os.path.isfile(path):
                return path
    except OSError:
        pass
    return None


def main(argv):
    """cost_incremental.py <transcript.jsonl>: print its API-price USD (diagnostics)."""
    if len(argv) != 2:
        sys.stderr.write(__doc__)
        return 2
    v = total(argv[1])
    print("-" if v is None else "%.4f" % v)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

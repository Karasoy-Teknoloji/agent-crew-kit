#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cost core: transcript scan, daily/hourly records, views, CLI (entry and the --status fast path are in cost.py)."""
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

import cost_parse as CP  # noqa: E402  parser + price table (single source)
from cost import SCRIPT, CLAUDE, DIR, STATUS, TZ_NAME, HOURLY_DAYS, WINDOWS, write_status, window_parts, _tz  # noqa: E402,F401  (paths and window rule live in one place)

DAILY = os.path.join(DIR, "daily.jsonl")
HOURLY = os.path.join(DIR, "hourly.jsonl")
CACHE = os.path.join(DIR, "cache.json")
VERSION = 2          # daily record schema (2: English keys); when it grows, old rows are overwritten on days that are recomputed
CACHE_VERSION = 6      # per-file cache schema (4: + per-minute usd buckets "min"; 5: + advisor tokens; 6: English keys)
MIN_DAYS = 8        # days of per-minute buckets kept (the quota calibration looks back 7 days, cost_ratio.DAYS)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _warning(text):
    """A warning for the console and for the run log of a scheduled run (cost.py `_run_cmd_scheduled`). sys.stderr is None under
    pythonw (Task Scheduler): a bare .write() there raised AttributeError and ended a run that had only a warning to report."""
    try:
        if sys.stderr is not None:
            sys.stderr.write(text)
    except Exception:
        pass
    try:
        lg = logging.getLogger("cost")
        if lg.handlers:
            lg.warning("%s", text.strip())
    except Exception:
        pass


def _tz_obj():
    """Day-boundary time zone. None = the system's local zone (COST_TZ=local)."""
    if TZ_NAME.lower() == "local":
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TZ_NAME)
    except Exception:
        pass
    if TZ_NAME.upper() == "UTC":
        return datetime.timezone.utc
    if TZ_NAME == "Europe/Istanbul":
        return datetime.timezone(datetime.timedelta(hours=3))  # tzdata missing (e.g. Windows): Istanbul has been UTC+3 since 2016
    _warning("WARNING: time zone not found (%s), using local time\n" % TZ_NAME)
    return None


TZ = _tz_obj()


_day_cache = {}


def local_day(e):
    k = int(e // 600)
    g = _day_cache.get(k)
    if g is None:
        g = datetime.datetime.fromtimestamp(k * 600, TZ).strftime("%Y-%m-%d")
        if len(_day_cache) > 50000:
            _day_cache.clear()
        _day_cache[k] = g
    return g


def today_str():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ per-file parsing + cache
def read_meta(meta_path):
    """Only agentType and parentAgentId; the description is never read or stored."""
    try:
        with open(meta_path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    return {k: d[k] for k in ("agentType", "parentAgentId") if isinstance(d, dict) and d.get(k)}


def process_file(price, kind, path, proj, session, signature, meta, reqs):
    """Aggregate the (already de-duplicated) requests of one transcript into per-day / per-hour sums."""
    F = {"signature": signature, "main": kind == "main", "proj": proj, "sid": session,
         "type": "" if kind == "main" else (meta.get("agentType") or "unknown"),
         "first": None, "day": {}, "hour": {}, "min": {}, "unpriced": 0}
    min_limit = time.time() - MIN_DAYS * 86400
    if not reqs:
        return F
    F["first"] = local_day(min(r["ts"] for r in reqs))
    total = 0.0
    for r in reqs:
        d = local_day(r["ts"])
        G = F["day"].setdefault(d, {"model": {}, "adv": {}})
        a = G["model"].setdefault(r["model"], [0, 0, 0, 0, 0, 0, 0.0])
        a[0] += 1
        a[1] += r["in"]
        a[2] += r["out"]
        a[3] += r["cr"]
        a[4] += r["cw5"]
        a[5] += r["cw1h"]
        H = F["hour"].setdefault(str(int(r["ts"] // 3600)), [{}, 0.0])  # hour bucket: [model -> usd (advisor included), advisor usd]
        c, advs, fz = CP.request_cost(price, r)
        F["unpriced"] += fz
        if c is not None:
            a[6] += c
            H[0][r["model"]] = H[0].get(r["model"], 0.0) + c
            total += c
        rusd = c or 0.0
        for name in r.get("adv") or []:   # advisor tokens (priced models only, like the advisor $) so the per-model Mtoken is not 0
            if price.find(name["model"]) is not None:
                x = G["adv"].setdefault(name["model"], [0, 0.0, 0, 0, 0, 0, 0])
                for j, key in enumerate(("in", "out", "cr", "cw5", "cw1h")):
                    x[2 + j] += name[key]
        for am, ca in advs:
            rusd += ca
            x = G["adv"].setdefault(am, [0, 0.0, 0, 0, 0, 0, 0])
            x[0] += 1
            x[1] += ca
            H[0][am] = H[0].get(am, 0.0) + ca
            H[1] += ca
            total += ca
        if rusd and r["ts"] >= min_limit:
            km = str(int(r["ts"] // 60))   # minute bucket: usd per UTC minute, for the quota calibration (cost_ratio.py)
            F["min"][km] = F["min"].get(km, 0.0) + rusd
    F["min"] = {k: round(v, 5) for k, v in F["min"].items()}
    for G in F["day"].values():
        for a in G["model"].values():
            a[6] = round(a[6], 6)
        for x in G["adv"].values():
            x[1] = round(x[1], 6)
    for H in F["hour"].values():
        H[0] = {m: round(v, 6) for m, v in H[0].items()}
        H[1] = round(H[1], 6)
    return F


def load_cache(key_):
    try:
        with open(CACHE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("key") == key_:
            return d.get("files") or {}
    except Exception:
        pass
    return {}


def write_0600(path, text):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.tmp%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def ownership(inputs):
    """One owner file per API response. inputs: {rel: {"main", "fork", "t0", "t1", "ids"}} -> {rel: set of key hashes owned by
    ANOTHER file (to be skipped there)}. A resumed session copies the earlier history into its own transcript (and the sub-agent
    files into its folder); each response must be counted once, in the file that owns it. Rank, smallest wins: main conversation
    before sub-agent, non-fork before fork agent, earlier first timestamp (the original session started before its resumed copy),
    earlier last timestamp (the original stopped when the copy was made), then path. Deterministic, so every run and every machine
    picks the same owner; the owner only decides main/sub, session and project attribution, never the total."""
    inf = float("inf")
    order = {}
    for rel, E in inputs.items():
        order[rel] = (0 if E["main"] else 1, 1 if E["fork"] else 0, E["t0"] if E["t0"] is not None else inf,
                     E["t1"] if E["t1"] is not None else inf, rel)
    owner = {}
    for rel, E in inputs.items():
        for i in E["ids"]:
            c = owner.get(i)
            if c is None or order[rel] < order[c]:
                owner[i] = rel
    return {rel: {i for i in E["ids"] if owner[i] != rel} for rel, E in inputs.items()}


def scan(cache_use=True):
    """Scan all transcripts (unchanged ones come from the cache) -> {relative path: F}.
    Every response is counted once across ALL files (ownership). A file's cached sums depend on which of its responses it owns,
    so the cache entry remembers a hash of the excluded set ("hx") and the file is re-read when that set changes."""
    d = CP.load_price()
    price = CP.Price(d)
    key_ = "%d|%s|ao%s" % (CACHE_VERSION, CP.price_summary(d), CP.VERSION)
    cache = load_cache(key_) if cache_use else {}
    new = {}
    fresh = {}      # rel -> parsed requests of files read in this run (unfiltered)
    info = {}     # rel -> (kind, path, proj, session, signature, meta)
    inputs = {}
    for kind, path, proj, session in CP.list_files():
        rel = os.path.relpath(path, CP.PROJ)
        signature = CP.stat_signature(path)
        if signature is None:
            continue
        meta = {}
        if kind == "agent":
            mi = CP.stat_signature(path[:-6] + ".meta.json")
            if mi:
                signature = signature + mi
            meta = read_meta(path[:-6] + ".meta.json")
        info[rel] = (kind, path, proj, session, signature, meta)
        e = cache.get(rel)
        if e is not None and e.get("signature") == signature and "ids" in e:
            inputs[rel] = e
            continue
        try:
            P = CP.parse(path)
        except OSError:
            del info[rel]
            continue
        fresh[rel] = P["reqs"]
        inputs[rel] = {"main": kind == "main", "fork": bool(meta.get("parentAgentId") or meta.get("agentType") == "fork"),
                         "t0": P["t0"], "t1": P["t1"], "ids": [r["k"] for r in P["reqs"]]}
    excluding = ownership(inputs)
    changed = False
    processed = 0
    for rel, (kind, path, proj, session, signature, meta) in info.items():
        G = inputs[rel]
        hx = hashlib.md5(",".join(sorted(excluding[rel])).encode("ascii")).hexdigest()[:12] if excluding[rel] else ""
        if rel not in fresh and G.get("hx", "") == hx:
            new[rel] = G
            continue
        try:
            if rel in fresh:
                reqs = [r for r in fresh[rel] if r["k"] not in excluding[rel]] if excluding[rel] else fresh[rel]
            else:
                reqs = CP.parse(path, excluding[rel])["reqs"]
            F = process_file(price, kind, path, proj, session, signature, meta, reqs)
        except OSError:
            continue
        F.update({"hx": hx, "t0": G["t0"], "t1": G["t1"], "fork": G["fork"], "ids": G["ids"]})
        new[rel] = F
        changed = True
        processed += 1
    # per-minute buckets older than MIN_DAYS days are dropped on every write, also from files that were not re-read (the cache would only grow)
    min_threshold = int((time.time() - MIN_DAYS * 86400) // 60)
    for F in new.values():
        min_ = F.get("min")
        if min_ and any(int(k) < min_threshold for k in min_):
            F["min"] = {k: v for k, v in min_.items() if int(k) >= min_threshold}
            changed = True
    if changed or set(new) != set(cache):
        try:
            write_0600(CACHE, json.dumps({"key": key_, "files": new}, ensure_ascii=False, separators=(",", ":")))
        except OSError as e:
            _warning("could not write the cache: %s\n" % e)
    scan.processed = processed
    return new


# ------------------------------------------------------------------ daily records
def bucket():
    return {"usd": 0.0, "n": 0, "in": 0, "out": 0, "cr": 0, "cw5": 0, "cw1h": 0}


def add_bucket(k, a):
    k["n"] += a[0]
    k["in"] += a[1]
    k["out"] += a[2]
    k["cr"] += a[3]
    k["cw5"] += a[4]
    k["cw1h"] += a[5]
    k["usd"] += a[6]


def new_record(d):
    r = bucket()
    r.update({"v": VERSION, "day": d, "adv": {"usd": 0.0, "n": 0}, "main": bucket(), "sub": bucket(),
              "model": {}, "type": {}, "project": {}, "session": defaultdict(float), "agent_runs": 0, "unpriced": 0})
    return r


def build_records(files):
    days = {}
    for F in files.values():
        if not F["day"]:
            continue
        type_name = "main" if F["main"] else (F["type"] or "unknown")
        grp = "main" if F["main"] else "sub"
        for d, G in F["day"].items():
            R = days.get(d)
            if R is None:
                R = days[d] = new_record(d)
            upper = [R, R[grp], R["type"].setdefault(type_name, bucket()), R["project"].setdefault(F["proj"], bucket())]
            for m, a in G["model"].items():
                for k in upper:
                    add_bucket(k, a)
                add_bucket(R["model"].setdefault(m, bucket()), a)
                R["session"][F["sid"]] += a[6]
            for am, (n, usd, *tok) in G["adv"].items():
                for k in upper:
                    k["usd"] += usd
                km = R["model"].setdefault(am, bucket())
                km["usd"] += usd
                for key, v in zip(("in", "out", "cr", "cw5", "cw1h"), tok):
                    km[key] += v   # advisor tokens count under the advisor's model only (not in the totals, as before)
                R["adv"]["usd"] += usd
                R["adv"]["n"] += n
                R["session"][F["sid"]] += usd
        if not F["main"] and F["first"] in days:
            R = days[F["first"]]
            R["agent_runs"] += 1
            R["type"].setdefault(type_name, bucket())["runs"] = R["type"][type_name].get("runs", 0) + 1
        if F["unpriced"]:
            days[sorted(F["day"])[0]]["unpriced"] += F["unpriced"]
    return {d: clean(R) for d, R in days.items()}


def clean(o):
    """Round; drop zero-valued keys (v and day stay)."""
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            v = clean(v)
            if k in ("v", "day"):
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


def daily_read():
    d = {}
    try:
        with open(DAILY, encoding="utf-8") as f:
            for s in f:
                s = s.strip()
                if not s:
                    continue
                try:
                    r = json.loads(s)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("day"):
                    d[r["day"]] = r
    except OSError:
        pass
    return d


def merge(registered, scanning, today, again=False, keep=()):
    """Stored daily rows + a fresh scan. When a past day scans lower than the stored row (transcripts were deleted) the
    stored row is kept. Today, an old schema and --again always take the scan, except the days in `keep` (--again only:
    days whose transcripts are partly deleted, a scan would undercount them). Returns (day -> record, changed, kept)."""
    out = {}
    changing = kept = 0
    for d in set(registered) | set(scanning):
        s, n = registered.get(d), scanning.get(d)
        if n is None:
            out[d] = s
        elif s is None:
            out[d] = n
            changing += 1
        elif again and d in keep and d != today:
            out[d] = s
            kept += 1
        elif again or d == today or s.get("v", 0) < VERSION or n.get("usd", 0) >= s.get("usd", 0) - 0.005:
            out[d] = n
            if abs(n.get("usd", 0) - s.get("usd", 0)) > 1e-6:
                changing += 1
        else:
            out[d] = s
            kept += 1
    return out, changing, kept


def save(again=False, cache_=True):
    tg, ts = scan_records(scan(cache_))
    scanning = tg
    registered = daily_read()
    today = today_str()
    keep = missing_days(registered) if again else set()
    last, changing, kept = merge(registered, scanning, today, again, keep)
    new_count = len([d for d in last if d not in registered])
    lines = [json.dumps(last[d], ensure_ascii=False, separators=(",", ":")) for d in sorted(last)]
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    os.chmod(DIR, 0o700)
    if os.path.exists(DAILY):
        shutil.copyfile(DAILY, DAILY + ".backup")
        os.chmod(DAILY + ".backup", 0o600)
    write_0600(DAILY, "\n".join(lines) + "\n")
    os.chmod(DAILY, 0o600)
    days = sorted(last)
    print("cost record: %d days (new %d, updated %d, kept %d) | %s … %s | total %.2f $ | %s"
          % (len(last), new_count, changing - new_count, kept, days[0] if days else "-", days[-1] if days else "-",
             sum(r.get("usd", 0) for r in last.values()), DAILY))
    hour = merge_hourly(read_hourly(), ts, int(time.time() // 3600), again, keep)
    write_hourly(hour)
    os.chmod(HOURLY, 0o600)
    print("hourly record: %d hours | %s" % (len(hour), HOURLY))
    if again:
        old = sorted(d for d in last if d not in scanning or d in keep)
        print("recomputed days: %d. Each response counted once (copies in resumed sessions removed)." % (len(last) - len(old)))
        if old:
            print("WARNING: %d days could not be recomputed (transcripts deleted), their old (possibly inflated) values were kept: %s"
                  % (len(old), ", ".join(old) if len(old) <= 6 else "%s … %s" % (old[0], old[-1])))
    status_write_cache(last, hour)
    return 0


def missing_days(registered):
    """Stored days that cannot be recomputed completely: some session recorded in the day's `session` map no longer has a
    transcript (main file or session folder). A rescan would undercount them, so --again keeps their stored row."""
    present = set()
    if os.path.isdir(CP.PROJ):
        for proj in os.listdir(CP.PROJ):
            pd = os.path.join(CP.PROJ, proj)
            if os.path.isdir(pd):
                for name in os.listdir(pd):
                    present.add(name[:-6] if name.endswith(".jsonl") else name)
    return {d for d, r in registered.items() if any(sid not in present for sid in (r.get("session") or {}))}


def build_hours(files):
    """Transcript hour buckets -> {epoch_hour(int): {usd, main, sub, adv, model{m: usd}}}. Hour = UTC epoch//3600 (matches local hours for whole-hour zone offsets)."""
    S = {}
    for F in files.values():
        grp = "main" if F["main"] else "sub"
        for hk, (models, adv) in (F.get("hour") or {}).items():
            R = S.setdefault(int(hk), {"usd": 0.0, "main": 0.0, "sub": 0.0, "adv": 0.0, "model": {}})
            t = sum(models.values())
            R["usd"] += t
            R[grp] += t
            R["adv"] += adv
            for m, v in models.items():
                R["model"][m] = R["model"].get(m, 0.0) + v
    for R in S.values():
        for k in ("usd", "main", "sub", "adv"):
            R[k] = round(R[k], 6)
        R["model"] = {m: round(v, 6) for m, v in R["model"].items() if v}
    return S


def hour_label(h):
    return datetime.datetime.fromtimestamp(h * 3600, TZ).strftime("%Y-%m-%dT%H")


def read_hourly():
    d = {}
    try:
        with open(HOURLY, encoding="utf-8") as f:
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


def merge_hourly(registered, scanning, now_h, again=False, keep=()):
    """Last HOURLY_DAYS days. A fresh scan overwrites the stored row; for old hours whose transcript was deleted (scan lower
    than stored) the stored row is kept. The last 48 hours always come from the scan. again: every hour that has a scan
    takes it (stored duplicates are dropped), except hours of the days in `keep` (partly deleted transcripts)."""
    start = now_h - HOURLY_DAYS * 24
    out = {}
    for h in set(registered) | set(scanning):
        if h < start:
            continue
        s, n = registered.get(h), scanning.get(h)
        if n is None:
            out[h] = s
        elif again and h <= now_h - 48 and hour_label(h)[:10] in keep:
            out[h] = s
        elif again or s is None or h > now_h - 48 or n.get("usd", 0) >= s.get("usd", 0) - 0.005:
            out[h] = dict(n, h=h)
        else:
            out[h] = s
    return out


def write_hourly(hour):
    lines = []
    for h in sorted(hour):
        r = dict(hour[h])
        r["h"] = h
        r["hour"] = hour_label(h)
        lines.append(json.dumps(clean_hour(r), ensure_ascii=False, separators=(",", ":")))
    new = "\n".join(lines) + "\n" if lines else ""
    try:
        with open(HOURLY, encoding="utf-8") as f:
            if f.read() == new:
                return
    except OSError:
        pass
    write_0600(HOURLY, new)


def clean_hour(r):
    return {k: v for k, v in r.items() if k in ("h", "hour", "model") or (isinstance(v, (int, float)) and v != 0)}


def status_write_cache(records, hour):
    today = datetime.datetime.now(TZ).date()
    days = {}
    for i in range(32):
        d = (today - datetime.timedelta(days=i)).isoformat()
        r = records.get(d)
        if r:
            days[d] = round(r.get("usd", 0.0), 4)
    now_h = int(time.time() // 3600)
    first = min(hour) if hour else None
    sk = {str(h): round(r.get("usd", 0.0), 4) for h, r in hour.items() if h > now_h - 31 * 24 and r.get("usd")}
    write_0600(STATUS, json.dumps({"t": time.time(), "days": days, "e": first, "hour": sk}, separators=(",", ":")))


def scan_records(files=None):
    """Daily + hourly records built from the scan. Returns (day records, hour records)."""
    d = files if files is not None else scan()
    tg = build_records(d)
    ts = build_hours(d)
    return tg, ts


def daily_write(last):
    """Write daily.jsonl (same rows as save(), without the .backup copy) unless the content is unchanged."""
    new = "\n".join(json.dumps(last[d], ensure_ascii=False, separators=(",", ":")) for d in sorted(last)) + "\n"
    if not last:
        return
    try:
        with open(DAILY, encoding="utf-8") as f:
            if f.read() == new:
                return
    except OSError:
        pass
    write_0600(DAILY, new)


def refresh_status():
    """Incremental: unchanged transcripts come from the file cache, only changed ones are read; same merge as --save.
    The hourly file is updated too (not rewritten when the content is unchanged)."""
    files = scan()
    tg, ts = scan_records(files)
    last, _, _ = merge(daily_read(), tg, today_str())
    hour = merge_hourly(read_hourly(), ts, int(time.time() // 3600))
    write_hourly(hour)
    daily_write(last)    # the daily record too: macOS has no timer, and the refresh runs while Claude Code is in use
    status_write_cache(last, hour)
    # background-only extras, each failing silently: quota calibration ("real $", cost_ratio.py) and the lira rate (cost_fx.py)
    try:
        import cost_ratio
        cost_ratio.refresh(files)
    except Exception as e:
        _warning("could not compute the ratio: %s\n" % e)
    try:
        import cost_fx
        cost_fx.refresh()
    except Exception as e:
        _warning("could not fetch the exchange rate: %s\n" % e)
    return 0


# ------------------------------------------------------------------ periods
def add(dst, src):
    for k, v in src.items():
        if k in ("v", "day"):
            continue
        if isinstance(v, dict):
            add(dst.setdefault(k, {}), v)
        elif isinstance(v, (int, float)):
            dst[k] = dst.get(k, 0) + v


def week_key(d):
    y, w, _ = d.isocalendar()
    return "%d-W%02d" % (y, w)


def period_limits(period, d):
    if period == "day":
        return d, d
    if period == "week":
        start = d - datetime.timedelta(days=d.weekday())
        return start, start + datetime.timedelta(days=6)
    if period == "month":
        start = d.replace(day=1)
        following = (start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        return start, following - datetime.timedelta(days=1)
    return datetime.date(d.year, 1, 1), datetime.date(d.year, 12, 31)


def period_key(period, d):
    if period == "day":
        return d.isoformat()
    if period == "week":
        return week_key(d)
    if period == "month":
        return d.strftime("%Y-%m")
    return str(d.year)


DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def label(period, start, end):
    if period == "day":
        return "%s %s" % (start.strftime("%d.%m"), DAY_NAMES[start.weekday()])
    if period == "week":
        return "%s (%s–%s)" % (week_key(start), start.strftime("%d.%m"), end.strftime("%d.%m"))
    if period == "month":
        return start.strftime("%Y-%m")
    return str(start.year)


def short_model(m):
    mm = re.match(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$", m)
    if not mm:
        return m
    s = mm.group(1).capitalize() + " " + mm.group(2)
    return s + "." + mm.group(3) if mm.group(3) else s


def _home_prefix():
    """Claude Code names a project folder after its working directory with every non-alphanumeric character replaced by '-'.
    The home directory prefix is stripped so projects show as '-work-repo' style short names (derived from $HOME, never hardcoded)."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.expanduser("~"))


def short_project(p):
    s = p
    prefix = _home_prefix()
    if prefix and s.startswith(prefix):
        s = s[len(prefix):]
    return s.lstrip("-") or "~"


def hit(k):
    t = k.get("in", 0) + k.get("cr", 0) + k.get("cw5", 0) + k.get("cw1h", 0)
    return round(k.get("cr", 0) / t, 4) if t else None


def bucket_summary(k, detail=False):
    o = {"usd": round(k.get("usd", 0.0), 4), "n": int(k.get("n", 0))}
    if detail:
        for a in ("in", "out", "cr", "cw5", "cw1h"):
            o[a] = int(k.get(a, 0))
        o["hit"] = hit(k)
    if k.get("runs"):
        o["runs"] = int(k["runs"])
    return o


def compute_summary(lines, period):
    """Total, averages, busiest/cheapest. Partial periods (the running one, the one the history starts in) are left out of the
    average and the cheapest/busiest pick; with no full period they are all used and the note says so."""
    full = [x for x in lines if not x["partial"]]
    pool = full or lines
    summary = {"period_count": len(lines), "full_period_count": len(full), "no_full_period": not full}
    if not lines:
        return summary
    total = sum(x["usd"] for x in lines)
    enabled_total = sum(x["active_days"] for x in lines)
    summary["total_usd"] = round(total, 4)
    summary["active_days"] = enabled_total
    summary["avg_active_day_usd"] = round(total / enabled_total, 4)
    summary["avg_period_usd"] = round(sum(x["usd"] for x in pool) / len(pool), 4)
    if period == "day":
        summary["avg_period_note"] = "today excluded" if full else "today only"
    else:
        summary["avg_period_note"] = "full periods only" if full else "no full period, partial periods included"
    hi = max(pool, key=lambda x: x["usd"])
    lo = min(pool, key=lambda x: x["usd"])
    summary["busiest"] = {"label": hi["label"], "usd": hi["usd"]}
    summary["cheapest"] = {"label": lo["label"], "usd": lo["usd"]}
    summary["cheapest_note"] = summary["avg_period_note"]
    summary["pool_count"] = len(pool)
    return summary


def view(records, period, today, first_day_session):
    """period: day|week|month|year -> {lines, summary}. Empty periods are never produced (built only from days that have data)."""
    groups = defaultdict(list)
    for d in sorted(records):
        r = records[d]
        if r.get("usd", 0) <= 0 and not r.get("n"):
            continue
        dt = datetime.date.fromisoformat(d)
        groups[period_key(period, dt)].append((dt, r))
    first = min(records) if records else None
    first_dt = datetime.date.fromisoformat(first) if first else None
    today_dt = datetime.date.fromisoformat(today)
    lines = []
    for key in sorted(groups):
        recs = groups[key]
        start, end = period_limits(period, recs[0][0])
        A = {}
        for _, r in recs:
            add(A, r)
        partial = start <= today_dt <= end or (first_dt is not None and start < first_dt)
        enabled = len(recs)
        session = A.get("session", {})
        top = sorted(session.items(), key=lambda kv: -kv[1])[:10]
        project = sorted(((short_project(p), v) for p, v in A.get("project", {}).items()), key=lambda kv: -kv[1].get("usd", 0))
        line = {
            "key": key, "label": label(period, start, end), "start": start.isoformat(), "end": end.isoformat(),
            "active_days": enabled, "partial": bool(partial),
            "usd": round(A.get("usd", 0.0), 4), "usd_day": round(A.get("usd", 0.0) / enabled, 4),
            "request": int(A.get("n", 0)),
            "tok": {a: int(A.get(a, 0)) for a in ("in", "out", "cr", "cw5", "cw1h")},
            "hit": hit(A),
            "session_count": len(session), "agent_runs": int(A.get("agent_runs", 0)),
            "advisor": {"usd": round(A.get("adv", {}).get("usd", 0.0), 4), "n": int(A.get("adv", {}).get("n", 0))},
            "main": bucket_summary(A.get("main", {})), "sub": bucket_summary(A.get("sub", {})),
            "model": {m: bucket_summary(v, True) for m, v in sorted(A.get("model", {}).items(), key=lambda kv: -kv[1].get("usd", 0)) if v.get("usd", 0) > 0 or v.get("n")},
            "type_": {t: bucket_summary(v) for t, v in sorted(A.get("type", {}).items(), key=lambda kv: -kv[1].get("usd", 0)) if v.get("usd", 0) > 0 or v.get("n")},
            "project": {p: bucket_summary(v) for p, v in project[:10]},
            "top_sessions": [{"id": sid, "date": first_day_session.get(sid, ""), "usd": round(v, 4)} for sid, v in top if v > 0],
        }
        if A.get("unpriced"):
            line["unpriced_token"] = int(A["unpriced"])
        line["type"] = line.pop("type_")
        lines.append(line)
    summary = compute_summary(lines, period)
    return {"period": period, "lines": lines, "summary": summary}


def every_data(records, today):
    first_day_session = {}
    for d in sorted(records):
        for sid in records[d].get("session", {}):
            first_day_session.setdefault(sid, d)
    total_session = defaultdict(float)
    for r in records.values():
        for sid, v in r.get("session", {}).items():
            total_session[sid] += v
    top = sorted(total_session.items(), key=lambda kv: -kv[1])[:10]
    days = sorted(records)
    V = {
        "created": datetime.datetime.now(TZ).isoformat(timespec="seconds"),
        "time_zone": TZ_NAME,
        "today": today,
        "note": "API list-price equivalent (USD), not a subscription bill. Advisor calls included. No prompt or title text.",
        "scope": {"first_day": days[0] if days else None, "last_day": days[-1] if days else None,
                   "active_days": len(days)},
        "top_sessions": [{"id": sid, "date": first_day_session.get(sid, ""), "usd": round(v, 4)} for sid, v in top],
    }
    for t in ("day", "week", "month", "year"):
        V[t] = view(records, t, today, first_day_session)
    return V


def fetch_records(cache_=True):
    """CLI/JSON: stored history + a fresh scan (writes nothing). Returns the day records; the hour records are left in fetch_records.hour."""
    tg, ts = scan_records(scan(cache_))
    last, _, _ = merge(daily_read(), tg, today_str())
    fetch_records.hour = merge_hourly(read_hourly(), ts, int(time.time() // 3600))
    return last


def compute_window(records, hour, now=None):
    """Three rolling windows (last 24h / 7d / 30d): hour buckets + daily completion at the boundary (window_parts, cost.py)."""
    now_h = int((time.time() if now is None else now) // 3600)
    e = min(hour) if hour else None
    su = {h: r.get("usd", 0.0) for h, r in hour.items()}
    gu = {d: r.get("usd", 0.0) for d, r in records.items()}
    out = []
    for name, n in WINDOWS:
        P = {"label": name, "days": n, "usd": 0.0, "main": {"usd": 0.0}, "sub": {"usd": 0.0}, "advisor": {"usd": 0.0, "n": 0},
             "model": {}, "hour_count": 0, "completed_usd": 0.0}
        for kind, k, w in window_parts(n, now_h, e, su, gu):
            if kind == "s":
                r = hour[k]
                P["usd"] += r.get("usd", 0.0)
                P["main"]["usd"] += r.get("main", 0.0)
                P["sub"]["usd"] += r.get("sub", 0.0)
                P["advisor"]["usd"] += r.get("adv", 0.0)
                for m, v in (r.get("model") or {}).items():
                    P["model"][m] = {"usd": P["model"].get(m, {"usd": 0.0})["usd"] + v}
                P["hour_count"] += 1
            else:
                r = records[k]
                P["usd"] += r.get("usd", 0.0) * w
                P["completed_usd"] += r.get("usd", 0.0) * w
                P["main"]["usd"] += r.get("main", {}).get("usd", 0.0) * w
                P["sub"]["usd"] += r.get("sub", {}).get("usd", 0.0) * w
                P["advisor"]["usd"] += r.get("adv", {}).get("usd", 0.0) * w
                for m, v in (r.get("model") or {}).items():
                    P["model"][m] = {"usd": P["model"].get(m, {"usd": 0.0})["usd"] + v.get("usd", 0.0) * w}
        P["model"] = dict(sorted(P["model"].items(), key=lambda kv: -kv[1]["usd"]))
        out.append(P)
    return out


def window_lines(PL):
    L = [title("ROLLING WINDOWS (back from now; hour buckets, the current partial hour included)")]
    for P in PL:
        s = "  %-9s %s · main %s · sub %s" % (P["label"], money(P["usd"]), money(P["main"]["usd"]), money(P["sub"]["usd"]))
        if P["advisor"]["usd"] > 0:
            s += " · advisor %s" % money(P["advisor"]["usd"])
        L.append(s)
        if P["usd"] > 0:  # an empty window shows no model line
            L.append("    models: " + model_section(P))
        if P["completed_usd"] > 0.005:
            L.append("    (%s belongs to the time before the hourly record; completed pro rata from the daily total)" % money(P["completed_usd"]))
    return L


# ------------------------------------------------------------------ CLI output
def tr(x, nd=2):
    """Turkish number format: thousands '.', decimals ','."""
    return format(x, ",.%df" % nd).translate({44: 46, 46: 44})


def money(x, nd=2):
    return "$" + tr(x, nd)


def percent(x, nd=1):
    return tr(x * 100, nd) + "%"


def model_section(line, most=4):
    total = line["usd"] or 1
    parts = []
    for m, v in list(line["model"].items()):
        if v["usd"] <= 0:
            continue
        parts.append((short_model(m), v["usd"]))
    first = parts[:most]
    other = sum(v for _, v in parts[most:])
    s = " · ".join("%s %s (%s)" % (a, money(v, 0 if v >= 100 else 2), "<1%" if 100 * v / total < 0.5 else "%d%%" % round(100 * v / total)) for a, v in first)
    if other > 0:
        s += " · other %s" % money(other)
    return s


def main_sub_section(line):
    s = "main session %s · sub-agents %s" % (money(line["main"]["usd"]), money(line["sub"]["usd"]))
    if line["advisor"]["usd"] > 0:
        s += " · advisor %s (%d calls, included in the models)" % (money(line["advisor"]["usd"]), line["advisor"]["n"])
    return s


def summary_line(line):
    ib = line["hit"]
    return "requests %s · sessions %d · agent runs %d%s" % (tr(line["request"], 0), line["session_count"], line["agent_runs"],
                                                         " · cache " + percent(ib) if ib is not None else "")


def table(lines, first_column, width=24):
    L = []
    L.append("  %-*s %9s %9s %9s %9s %7s %7s" % (width, first_column, "$", "main", "sub", "$/day", "requests", "cache"))
    for s in lines:
        ib = str(round(s["hit"] * 100)) + "%" if s["hit"] is not None else "-"
        L.append("  %-*s %9s %9s %9s %9s %7s %7s" % (
            width, s["label"] + ("*" if s["partial"] else ""), tr(s["usd"]), tr(s["main"]["usd"]), tr(s["sub"]["usd"]),
            tr(s["usd_day"]), tr(s["request"], 0), ib))
    return L


def summary_end(lines, period, name):
    o = compute_summary(lines, period)
    if not o.get("total_usd"):
        return []
    L = []
    s = "  total %s · avg %s/active day (%d days)" % (money(o["total_usd"]), money(o["avg_active_day_usd"]), o["active_days"])
    if o["pool_count"] > 1:
        s += " · avg %s/%s (%s)" % (money(o["avg_period_usd"]), name, o["avg_period_note"])
    L.append(s)
    if o["pool_count"] > 1:
        L.append("  busiest %s %s · cheapest %s %s (%s)" % (o["busiest"]["label"], money(o["busiest"]["usd"]),
                                                     o["cheapest"]["label"], money(o["cheapest"]["usd"]), o["cheapest_note"]))
    return L


def combined_model(lines):
    A = {"usd": 0.0, "model": {}, "main": {"usd": 0.0}, "sub": {"usd": 0.0}, "advisor": {"usd": 0.0, "n": 0}}
    for s in lines:
        A["usd"] += s["usd"]
        A["main"]["usd"] += s["main"]["usd"]
        A["sub"]["usd"] += s["sub"]["usd"]
        A["advisor"]["usd"] += s["advisor"]["usd"]
        A["advisor"]["n"] += s["advisor"]["n"]
        for m, v in s["model"].items():
            A["model"][m] = {"usd": A["model"].get(m, {"usd": 0.0})["usd"] + v["usd"]}
    A["model"] = dict(sorted(A["model"].items(), key=lambda kv: -kv[1]["usd"]))
    return A


def title(t):
    return "\n" + t + "\n" + "─" * min(len(t), 60)


def top_session_lines(list_, indent="  "):
    L = []
    for i, o in enumerate(list_, 1):
        L.append("%s%2d. %s  %s  %s" % (indent, i, o["id"][:8], o["date"][8:10] + "." + o["date"][5:7] if o["date"] else "?", money(o["usd"])))
    return L


def unpriced_line(V):
    """Warning line when some tokens belong to a model that has no price (their cost is NOT in the totals)."""
    n = sum(x.get("unpriced_token", 0) for x in V["day"]["lines"])
    if not n:
        return []
    return ["", "WARNING: %s tokens belong to a model that is not in the price table; they are not in the cost (add it to cost-prices.json or override it with a local price file)." % tr(n, 0)]


def default_report(V):
    """Summary report. A section with no data is not printed at all (no empty headings)."""
    L = []
    k = V["scope"]
    if not k["first_day"]:
        return ["No data (no usable transcript found under %s)." % os.path.join(CLAUDE, "projects")]
    L.append("Claude Code cost · API list-price equivalent (USD), not a bill · advisor calls included")
    L.append("Data: %s … %s · %d active days · day boundary %s · * = partial period" % (k["first_day"], k["last_day"], k["active_days"], V["time_zone"]))
    day = V["day"]["lines"]
    today = V["today"]
    if any(P["usd"] > 0 for P in V["window"]):
        L += window_lines(V["window"])
    b = [s for s in day if s["key"] == today]
    if b:
        s = b[0]
        L.append(title("TODAY"))
        L.append("  %s  %s · %s" % (s["label"], money(s["usd"]), summary_line(s)))
        L.append("  models: " + model_section(s))
        L.append("  " + main_sub_section(s))
    bd = datetime.date.fromisoformat(today)
    s7 = [s for s in day if datetime.date.fromisoformat(s["key"]) > bd - datetime.timedelta(days=7)]
    if s7:
        L.append(title("LAST 7 DAYS"))
        L += table(s7, "day")
        t = sum(s["usd"] for s in s7)
        busiest = max(s7, key=lambda s: s["usd"])
        cheap = min((s for s in s7 if s["key"] != today), key=lambda s: s["usd"], default=None)
        L.append("  total %s · avg %s/active day (%d days)%s · busiest %s %s%s" % (
            money(t), money(t / len(s7)), len(s7), " (today included)" if b else "", busiest["label"], money(busiest["usd"]),
            " · cheapest %s %s" % (cheap["label"], money(cheap["usd"])) if cheap else ""))
        L.append("  models: " + model_section(combined_model(s7)))
        L.append("  " + main_sub_section(combined_model(s7)))
    h = V["week"]["lines"]
    last4 = [s for s in h if datetime.date.fromisoformat(s["end"]) >= bd - datetime.timedelta(days=bd.weekday() + 21)]
    if last4:
        L.append(title("LAST 4 WEEKS"))
        L += table(last4, "week")
        L += summary_end(last4, "week", "week")
        L.append("  models: " + model_section(combined_model(last4)))
    months = V["month"]["lines"][-6:]
    if months:
        L.append(title("MONTHS"))
        L += table(months, "month")
        L += summary_end(months, "month", "month")
        L.append("  models: " + model_section(combined_model(months)))
    yl = V["year"]["lines"]
    if yl:
        L.append(title("YEARS"))
        L += table(yl, "year")
        L += summary_end(yl, "year", "year")
        L.append("  models: " + model_section(combined_model(yl)))
        L.append("  (the yearly view fills up as history builds up: Claude Code deletes old transcripts, the daily totals are kept in daily.jsonl)")
    if V["top_sessions"]:
        L.append(title("10 MOST EXPENSIVE SESSIONS (all data; id = first 8 characters, date = first day seen)"))
        L += top_session_lines(V["top_sessions"])
    return L + unpriced_line(V)


def single_view(V, period, last):
    name = {"day": "DAY", "week": "WEEK", "month": "MONTH", "year": "YEAR"}[period]
    unit = period
    rows = V[period]["lines"]
    if last:
        rows = rows[-last:]
    L = [title("%s VIEW (periods with data)" % name)]
    if not rows:
        return L + ["  No data."]
    L += table(rows, unit)
    L += summary_end(rows, period, unit)
    A = combined_model(rows)
    L.append("  models: " + model_section(A, 6))
    L.append("  " + main_sub_section(A))
    types = defaultdict(lambda: [0.0, 0, 0])
    for s in rows:
        for t, v in s["type"].items():
            types[t][0] += v["usd"]
            types[t][1] += v["n"]
            types[t][2] += v.get("runs", 0)
    if types:
        L.append("  agent type: " + " · ".join("%s %s%s" % (t, money(v[0]), " (%d runs)" % v[2] if v[2] else "")
                                             for t, v in sorted(types.items(), key=lambda kv: -kv[1][0])[:8]))
    top = defaultdict(float)
    for s in rows:
        for o in s["top_sessions"]:
            top[(o["id"], o["date"])] += o["usd"]
    most = [{"id": i, "date": t, "usd": v} for (i, t), v in sorted(top.items(), key=lambda kv: -kv[1])[:10]]
    if most:
        L.append(title("10 MOST EXPENSIVE SESSIONS (periods shown; id = first 8 characters)"))
        L += top_session_lines(most)
    return L + unpriced_line(V)


# ------------------------------------------------------------------ dashboard / flat tables
def short_label(period, key_):
    if period == "day":
        return key_[8:10] + "." + key_[5:7]
    if period == "week":
        return "W" + key_.split("-W")[1]
    if period == "month":
        return ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][int(key_[5:7]) - 1] + " " + key_[2:4]
    return key_


def panel_tables(V):
    """V (every_data) -> {periods, breakdowns, summary}: flat row lists (no text; only numbers, dates, model/type/project names, session ids)."""
    periods, breakdown, summary = [], [], []
    session_all = set()
    for period in ("day", "week", "month", "year"):
        rows = V[period]["lines"]
        oz = V[period]["summary"]
        tok = defaultdict(int)
        adv = main_ = sub = request = runs = 0.0
        for s in rows:
            row_id = "%s|%s" % (period, s["key"])
            periods.append({
                "line": row_id, "view": period, "key": s["key"], "label": s["label"],
                "short": short_label(period, s["key"]), "start": s["start"], "end": s["end"], "active_days": s["active_days"],
                "partial": 1 if s["partial"] else 0, "usd": s["usd"], "usd_day": s["usd_day"],
                "main_usd": s["main"]["usd"], "sub_usd": s["sub"]["usd"], "advisor_usd": s["advisor"]["usd"],
                "advisor_n": s["advisor"]["n"], "request": s["request"], "session": s["session_count"],
                "agent_runs": s["agent_runs"], "hit": s["hit"]})
            for a, v in s["tok"].items():
                tok[a] += v
            adv += s["advisor"]["usd"]
            main_ += s["main"]["usd"]
            sub += s["sub"]["usd"]
            request += s["request"]
            runs += s["agent_runs"]
            top = s["usd"] or 1.0
            for m, v in s["model"].items():
                if v["usd"] > 0:
                    breakdown.append({"line": "%s|model|%s" % (row_id, m), "view": period, "key": s["key"], "dimension": "model",
                                    "name": short_model(m), "usd": v["usd"], "share": round(v["usd"] / top, 4), "request": v["n"]})
            for t, v in s["type"].items():
                if v["usd"] > 0:
                    breakdown.append({"line": "%s|type|%s" % (row_id, t), "view": period, "key": s["key"], "dimension": "type",
                                    "name": t, "usd": v["usd"], "share": round(v["usd"] / top, 4), "request": v["n"], "runs": v.get("runs", 0)})
            for pr, v in s["project"].items():
                if v["usd"] > 0:
                    breakdown.append({"line": "%s|project|%s" % (row_id, pr), "view": period, "key": s["key"], "dimension": "project",
                                    "name": pr, "usd": v["usd"], "share": round(v["usd"] / top, 4), "request": v["n"]})
            for i, o in enumerate(s["top_sessions"], 1):
                session_all.add(o["id"])
                breakdown.append({"line": "%s|session|%s" % (row_id, o["id"]), "view": period, "key": s["key"], "dimension": "session",
                                "name": o["id"][:8], "usd": o["usd"], "share": round(o["usd"] / top, 4), "date": o["date"], "order": i})
        t = tok["in"] + tok["cr"] + tok["cw5"] + tok["cw1h"]
        summary.append({
            "line": period, "view": period, "total_usd": oz.get("total_usd", 0), "active_days": oz.get("active_days", 0),
            "avg_active_day_usd": oz.get("avg_active_day_usd", 0), "avg_period_usd": oz.get("avg_period_usd", 0),
            "avg_period_note": oz.get("avg_period_note", ""),
            "busiest_label": (oz.get("busiest") or {}).get("label", ""), "busiest_usd": (oz.get("busiest") or {}).get("usd", 0),
            "cheapest_label": (oz.get("cheapest") or {}).get("label", ""), "cheapest_usd": (oz.get("cheapest") or {}).get("usd", 0),
            "period_count": oz.get("period_count", 0), "full_period_count": oz.get("full_period_count", 0), "pool_count": oz.get("pool_count", 0),
            "hit": round(tok["cr"] / t, 4) if t else None, "advisor_usd": round(adv, 4), "main_usd": round(main_, 4),
            "sub_usd": round(sub, 4), "request": int(request), "agent_runs": int(runs),
            "created": V["created"], "today": V["today"], "first_day": V["scope"]["first_day"], "last_day": V["scope"]["last_day"],
            "data_days": V["scope"]["active_days"]})
    return {"periods": periods, "breakdowns": breakdown, "summary": summary}


PANEL_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cost-panel.html")


def panel_html(tables):
    """The static dashboard: the template with the flat tables embedded as JSON (no network needed to view it)."""
    with open(PANEL_TEMPLATE, encoding="utf-8") as f:
        template = f.read()
    data = json.dumps(tables, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return template.replace("__COST_DATA__", data)


def write_panel(dir_):
    V = every_data(fetch_records(), today_str())
    tables = panel_tables(V)
    os.makedirs(dir_, mode=0o700, exist_ok=True)
    os.chmod(dir_, 0o700)
    for name, lines in tables.items():
        write_0600(os.path.join(dir_, name + ".json"), json.dumps(lines, ensure_ascii=False, separators=(",", ":")))
        print("%s: %d rows -> %s" % (name, len(lines), os.path.join(dir_, name + ".json")))
    try:
        path = os.path.join(dir_, "index.html")
        write_0600(path, panel_html(tables))
        print("panel: %s (open it as a file in a browser; no network needed)" % path)
    except OSError as e:
        _warning("could not read the panel template (%s); only the JSON tables were written\n" % e)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cost", description="Claude Code spending statistics (API list-price equivalent, not a bill; everything stays local)")
    ap.add_argument("--day", action="store_true", help="daily view")
    ap.add_argument("--week", action="store_true", help="weekly view (ISO)")
    ap.add_argument("--month", action="store_true", help="monthly view")
    ap.add_argument("--year", action="store_true", help="yearly view")
    ap.add_argument("--window", action="store_true", help="rolling windows: last 24 hours / 7 days / 30 days (with the per-model breakdown)")
    ap.add_argument("--last", type=int, default=0, help="last N periods in the view")
    ap.add_argument("--json", action="store_true", help="print all views as JSON")
    ap.add_argument("--save", action="store_true", help="write the daily totals to daily.jsonl")
    ap.add_argument("--again", action="store_true", help="--save: overwrite the stored rows with the scan (after a price table change)")
    ap.add_argument("--panel", nargs="?", const=os.path.join(DIR, "panel"), metavar="DIR", help="write flat JSON tables and a static dashboard index.html (default: <claude dir>/cost/panel)")
    ap.add_argument("--share", action="store_true", help="save the daily totals, then write today's usage row to the shared repository (optional; on an error it only writes to the log)")
    ap.add_argument("--automatic", action="store_true", help="with --share: the periodic run from the status line (updates the row when the weekly %% changed; else writes nothing)")
    ap.add_argument("--ranking", action="store_true", help="with --share: also regenerate the shared ranking (RANKING.md)")
    ap.add_argument("--dry", action="store_true", help="with --share: only print the row, write nothing to GitHub")
    ap.add_argument("--status", action="store_true", help="status-line string (from the cache)")
    ap.add_argument("--refresh-status", action="store_true", help="refresh the status cache")
    ap.add_argument("--no-cache", action="store_true", help="do not use the file cache (full scan)")
    a = ap.parse_args(argv)
    try:
        return run_cmd(a)
    except CP.PriceError as e:
        _warning("ERROR: %s\n" % e)
        return 2


def run_cmd(a):
    _tz()
    if a.status:
        return write_status()
    if a.refresh_status:
        return refresh_status()
    if a.panel:
        return write_panel(a.panel)
    if a.share and a.automatic:
        import cost_share
        cost_share.share(automatic=True, prepare=lambda: save(a.again, not a.no_cache))
        return 0
    if a.share or a.ranking:
        rc = save(a.again, not a.no_cache)   # the totals are recorded first (same as --save); only the sharing is silent on errors
        import cost_share
        cost_share.share(ranking=a.ranking, dry=a.dry)
        return rc
    if a.save:
        return save(a.again, not a.no_cache)
    records = fetch_records(not a.no_cache)
    V = every_data(records, today_str())
    V["window"] = compute_window(records, fetch_records.hour)
    if a.json:
        json.dump(V, sys.stdout, ensure_ascii=False, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0
    selected = [t for t in ("day", "week", "month", "year") if getattr(a, t)]
    if a.window:
        L = window_lines(V["window"])
    elif selected:
        L = []
        for t in selected:
            L += single_view(V, t, a.last)
    else:
        L = default_report(V)
    print("\n".join(L))
    return 0



#!/usr/bin/env python3
"""pace_forecast.py -- forecasts where the weekly Claude Code quota will land
at its reset. Python standard library only. Runs on Windows (python, 3.12)
and Linux (python3).

Inputs (under the base dir, default ~/.claude):
  pace.json, pace-history.csv, pace-plan.json (optional), projects/**/*.jsonl

Outputs (under the base dir):
  pace-forecast.json, pace-activity.json, pace-forecast.log, pace-forecast.lock

Model: projection = week_used + sum over the remaining days of (rate x day factor x active hours).
Active hours come from pace-plan.json, else from a finished day of the same weekday or of any day;
the rate is % per active hour (last 24 h blended with the whole week), and the day factor is the
measured day-to-day swing of that rate. 4000 trials give the P10/P50/P90 range; the mode has hysteresis.

CLI:
  (no args)   normal run, writes pace-forecast.json, prints nothing
  --print     normal run, then print the forecast JSON
  --selftest  run built-in checks against synthetic data, print PASS/FAIL
  --rebuild   delete pace-activity.json first, then run
"""

import argparse
import csv
import json
import os
import platform
import random
import sys
import tempfile
import time
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", "tools"))  # repo layout: tools/hidden.py (a --copy install ships it next to this file)
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from hidden_loader import hidden  # noqa: E402  (one hidden-launch helper)

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TEN_MIN = 600
BINS_PER_HOUR = 6
WEEK_SECONDS = 7 * 86400
ACTIVITY_WINDOW_DAYS = 56  # kept even after Claude Code deletes transcripts older than 30 days
WEEKS_KEPT = 8  # final weekly totals kept for the next week's anchor
P90_DAYS = 2  # the P90 thresholds count only this close to the reset; before that the median decides
LOCK_MAX_AGE_S = 120
PEER_SYNC_S = 1800  # swap activity bins with the other machine at most every 30 minutes
MC_TRIALS = 4000
LOG_MAX_BYTES = 100 * 1024
DEFAULT_TARGET = 90.0
DEFAULT_RATE = 2.0
DEFAULT_TARGET_HOURS = 5.0  # forecast and mode are measured this many hours before the weekly reset (0 = at the reset)
DEFAULT_MAX_TARGET_HOURS = 72.0  # default cap of the target hours; "max_target_hours" in pace-plan.json moves it (within the ceiling)
ABS_MAX_TARGET_HOURS = 96.0      # hard ceiling nobody can configure past (4 days before the reset)
DEFAULT_TARGET_PERCENT = 100.0   # the weekly % to be at the target (the modes and the forecast aim at it); 100 = the old behavior
MIN_TARGET_PERCENT = 50.0        # a goal below this is a typo, not a plan


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------

def as_float(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def r(x, n):
    return None if x is None else round(x, n)


def read_json_safe(path):
    try:
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def atomic_write_json(path, obj):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-pace-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def log_error(base_dir, msg):
    try:
        path = os.path.join(base_dir, "pace-forecast.log")
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            os.remove(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.now().isoformat(), msg))
    except Exception:
        pass


def parse_iso_utc(ts):
    """Parse '2026-09-25T14:03:11.123Z' (or without fractional seconds) to epoch."""
    if not ts:
        return None
    try:
        s = ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def to_local(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone()


def percentile(sorted_vals, pct):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


# --------------------------------------------------------------------------
# lock
# --------------------------------------------------------------------------

def acquire_lock(base_dir):
    path = os.path.join(base_dir, "pace-forecast.lock")

    def try_create():
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
            os.close(fd)
            return True
        except OSError:
            return False

    try:
        os.makedirs(base_dir, exist_ok=True)
    except OSError:
        pass

    if try_create():
        return path
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        age = LOCK_MAX_AGE_S + 1
    if age > LOCK_MAX_AGE_S:
        try:
            os.remove(path)
        except OSError:
            pass
        if try_create():
            return path
    return None


def release_lock(lock_path):
    if lock_path:
        try:
            os.remove(lock_path)
        except OSError:
            pass


# --------------------------------------------------------------------------
# input readers
# --------------------------------------------------------------------------

def read_pace(base_dir):
    return read_json_safe(os.path.join(base_dir, "pace.json"))


def read_history(base_dir):
    path = os.path.join(base_dir, "pace-history.csv")
    rows = []
    if not os.path.exists(path):
        return rows
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    fhr = row.get("five_hour_resets_at")
                    rows.append({
                        "ts": int(float(row["ts"])),
                        "week_used": float(row["week_used"]),
                        # empty = Claude Code did not report a 5-hour window in that row
                        "five_hour_used": as_float(row.get("five_hour_used") or None),
                        "week_resets_at": int(float(row["week_resets_at"])),
                        "five_hour_resets_at": int(float(fhr)) if fhr else None,
                        "host": row.get("host", ""),
                    })
                except (KeyError, ValueError, TypeError):
                    continue
    except Exception:
        return rows
    rows.sort(key=lambda x: x["ts"])
    return rows


def max_target_hours_setting(plan):
    """Cap of the target hours: "max_target_hours" in pace-plan.json, else DEFAULT_MAX_TARGET_HOURS; never above ABS_MAX_TARGET_HOURS."""
    v = as_float(plan.get("max_target_hours"), None)
    if v is None:
        v = DEFAULT_MAX_TARGET_HOURS
    return min(max(v, 0.0), ABS_MAX_TARGET_HOURS)


def target_hours_setting(plan):
    """Hours before the weekly reset at which the quota should be used up (the forecast target).
    PACE_TARGET_HOURS env var, else "target_hours" in pace-plan.json, else DEFAULT_TARGET_HOURS. 0 = old behavior.
    Capped by max_target_hours_setting (default 72, ceiling 96): 60 = Friday 09:00 for a Sunday 21:00 reset."""
    v = as_float(os.environ.get("PACE_TARGET_HOURS"), None)
    if v is None:
        v = as_float(plan.get("target_hours"), None)
    if v is None:
        v = DEFAULT_TARGET_HOURS
    return min(max(v, 0.0), max_target_hours_setting(plan))


def target_percent_setting(plan):
    """(percent, explicit): the weekly % to be at the target. PACE_TARGET_PERCENT env var, else "target_percent" in pace-plan.json, else
    DEFAULT_TARGET_PERCENT; clamped to MIN_TARGET_PERCENT..100. `explicit` tells whether the user set it (the legacy "target" key,
    which only fed need_rate, is then superseded)."""
    v = as_float(os.environ.get("PACE_TARGET_PERCENT"), None)
    if v is None:
        v = as_float(plan.get("target_percent"), None)
    if v is None:
        return DEFAULT_TARGET_PERCENT, False
    return min(max(v, MIN_TARGET_PERCENT), 100.0), True


def read_plan(base_dir):
    d = read_json_safe(os.path.join(base_dir, "pace-plan.json"))
    return d if isinstance(d, dict) else {}


# --------------------------------------------------------------------------
# activity index (10-minute bins over the last ACTIVITY_WINDOW_DAYS days)
# --------------------------------------------------------------------------

def iter_jsonl_files(root):
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".jsonl"):
                yield os.path.join(dirpath, fn)


def scan_jsonl_tail(path, offset):
    """Read new lines from offset. Returns (new_offset, set_of_bins).
    Never advances past an incomplete trailing line."""
    bins = set()
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    if not data:
        return offset, bins
    if data.endswith(b"\n"):
        chunk = data
    else:
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            return offset, bins
        chunk = data[: last_nl + 1]
    new_offset = offset + len(chunk)
    for raw in chunk.split(b"\n"):
        if not raw or b'"usage"' not in raw or b'"assistant"' not in raw:
            continue
        try:
            d = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        if d.get("type") != "assistant":
            continue
        msg = d.get("message")
        if not isinstance(msg, dict) or "usage" not in msg:
            continue
        epoch = parse_iso_utc(d.get("timestamp"))
        if epoch is None:
            continue
        bins.add(int(epoch) // TEN_MIN)
    return new_offset, bins


def history_rise_bins(history):
    bins = set()
    rows = sorted(history, key=lambda x: x["ts"])
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        if prev["week_resets_at"] == cur["week_resets_at"] and cur["week_used"] > prev["week_used"]:
            bins.add(cur["ts"] // TEN_MIN)
        pf, cf = prev.get("five_hour_resets_at"), cur.get("five_hour_resets_at")
        if (pf and cf and pf == cf and cur["five_hour_used"] is not None and prev["five_hour_used"] is not None
                and cur["five_hour_used"] > prev["five_hour_used"]):
            bins.add(cur["ts"] // TEN_MIN)
    return bins


def update_activity_index(base_dir, now, history):
    idx_path = os.path.join(base_dir, "pace-activity.json")
    idx = read_json_safe(idx_path) or {}
    files_idx = idx.get("files") if isinstance(idx.get("files"), dict) else {}
    bins = set(idx.get("bins") or [])

    cutoff_bin = int((now - ACTIVITY_WINDOW_DAYS * 86400) // TEN_MIN)
    bins = {b for b in bins if b >= cutoff_bin}

    projects_root = os.path.join(base_dir, "projects")
    seen = set()
    if os.path.isdir(projects_root):
        for path in iter_jsonl_files(projects_root):
            seen.add(path)
            try:
                st = os.stat(path)
            except OSError:
                continue
            size, mtime = st.st_size, st.st_mtime
            prev = files_idx.get(path)
            if prev is not None and prev.get("size") == size:
                continue  # unchanged since last run
            offset = prev.get("offset", 0) if (prev is not None and size >= prev.get("size", 0)) else 0
            try:
                new_offset, file_bins = scan_jsonl_tail(path, offset)
            except OSError:
                continue
            bins |= {b for b in file_bins if b >= cutoff_bin}
            files_idx[path] = {"offset": new_offset, "size": size, "mtime": mtime}

    for p in list(files_idx.keys()):
        if p not in seen:
            del files_idx[p]

    bins |= {b for b in history_rise_bins(history) if b >= cutoff_bin}

    # final weekly use per week (key: resets_at), the anchor for the next week; shared with the peer
    weeks = {k: v for k, v in (idx.get("weeks") or {}).items() if as_float(v) is not None}
    for row in history:
        k = str(row["week_resets_at"])
        weeks[k] = max(as_float(weeks.get(k), 0.0), row["week_used"])
    weeks = {k: v for k, v in weeks.items() if int(float(k)) >= now - WEEKS_KEPT * WEEK_SECONDS}

    try:
        atomic_write_json(idx_path, {"files": files_idx, "bins": sorted(bins), "weeks": weeks})
    except Exception:
        pass
    return bins, weeks


def read_peer_bins(base_dir, cutoff_bin):
    d = read_json_safe(os.path.join(base_dir, "pace-activity-peer.json")) or {}
    return {b for b in (d.get("bins") or []) if isinstance(b, int) and b >= cutoff_bin}


def merged_weeks(base_dir, local_weeks):
    d = read_json_safe(os.path.join(base_dir, "pace-activity-peer.json")) or {}
    weeks = {k: as_float(v, 0.0) for k, v in local_weeks.items()}
    for k, v in (d.get("weeks") or {}).items():
        if as_float(v) is not None:
            weeks[k] = max(weeks.get(k, 0.0), as_float(v))
    return weeks


def last_week_total(weeks, week_resets_at):
    """Final use of the week before the current one (reset times may drift by a few minutes)."""
    prev_reset = week_resets_at - WEEK_SECONDS
    near = [(abs(int(float(k)) - prev_reset), v) for k, v in weeks.items() if abs(int(float(k)) - prev_reset) <= 3600]
    return min(near)[1] if near else None


def sync_peer(base_dir, now, cfg, local_bins, local_weeks):
    """Both machines spend the same quota, so the rate and the daily hours need the activity of both.
    The machine with pace-peer.json ({"ssh_host": ..., "ssh_exe": ...}) pulls the other machine's bins
    into its pace-activity-peer.json and pushes its own bins into the other machine's."""
    state_path = os.path.join(base_dir, "pace-peer-sync.json")
    state = read_json_safe(state_path) or {}
    if now - as_float(state.get("at"), 0.0) < PEER_SYNC_S:
        return
    atomic_write_json(state_path, {"at": int(now)})
    ssh = [cfg.get("ssh_exe") or "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", cfg["ssh_host"]]
    r = hidden.run(ssh + ["cat ~/.claude/pace-activity.json"], capture_output=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("pull failed: " + r.stderr.decode("utf-8", "replace").strip()[:200])
    peer = json.loads(r.stdout.decode("utf-8-sig"))
    atomic_write_json(os.path.join(base_dir, "pace-activity-peer.json"),
                      {"bins": peer.get("bins") or [], "weeks": peer.get("weeks") or {},
                       "host": cfg["ssh_host"], "updated": int(now)})
    payload = json.dumps({"bins": sorted(local_bins), "weeks": local_weeks, "host": platform.node(),
                          "updated": int(now)}).encode("utf-8")
    remote = "cat > ~/.claude/pace-activity-peer.json.tmp && mv ~/.claude/pace-activity-peer.json.tmp ~/.claude/pace-activity-peer.json"
    r = hidden.run(ssh + [remote], input=payload, capture_output=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("push failed: " + r.stderr.decode("utf-8", "replace").strip()[:200])


# --------------------------------------------------------------------------
# rates
# --------------------------------------------------------------------------

def active_hours_between(bins, start_epoch, end_epoch):
    return sum(1 for b in bins if start_epoch <= b * TEN_MIN < end_epoch) / BINS_PER_HOUR


def compute_rate_week(week_used_now, week_start, now, bins):
    hours = active_hours_between(bins, week_start, now)
    if hours >= 2.0:
        return week_used_now / hours
    return None


def compute_rate_recent(history, week_resets_at, week_used_now, now, bins):
    target_then = now - 24 * 3600
    floor_ts = now - 36 * 3600
    same_week = [x for x in history if x["week_resets_at"] == week_resets_at]
    candidates = [x for x in same_week if x["ts"] >= floor_ts]
    r_then = None
    if candidates:
        r_then = min(candidates, key=lambda x: abs(x["ts"] - target_then))
    elif same_week:
        row0 = min(same_week, key=lambda x: x["ts"])
        if now - row0["ts"] >= 3 * 3600:
            r_then = row0
    if r_then is None:
        return None
    delta = week_used_now - r_then["week_used"]
    hours = active_hours_between(bins, r_then["ts"], now)
    if hours >= 1.5 and delta >= 0:
        return delta / hours
    return None


def compute_rate_window(history, week_resets_at, week_used_now, now, bins, window_h):
    """% per active hour over roughly the last window_h hours (None when too little activity)."""
    rows = [x for x in history if x["week_resets_at"] == week_resets_at and x["ts"] <= now - window_h * 3600]
    if not rows:
        return None
    r0 = max(rows, key=lambda x: x["ts"])
    if now - r0["ts"] > 2 * window_h * 3600:
        return None
    hours = active_hours_between(bins, r0["ts"], now)
    delta = week_used_now - r0["week_used"]
    return delta / hours if hours >= 1.0 and delta >= 0 else None


def current_stretch_start(bins, now, gap_h=3.0):
    """Start of the work stretch going on now: back from now until a gap of gap_h hours without
    activity (sleep, a long break). None when nothing was active within the last gap_h hours."""
    now_bin = int(now // TEN_MIN)
    gap_bins = int(gap_h * BINS_PER_HOUR)
    past = sorted((b for b in bins if b <= now_bin), reverse=True)
    if not past or now_bin - past[0] > gap_bins:
        return None
    start = past[0]
    for b in past[1:]:
        if start - b > gap_bins:
            break
        start = b
    return start * TEN_MIN


def persistence(bins, history, week_resets_at, week_used, now, rate, horizon_end=None):
    """Near the reset the work going on now says most about the rest of the week: carry the current
    stretch's share of active time and its recent rate forward. The weight grows from 0 a day before
    the reset to 0.95 at the reset; with no current stretch it is 0 and the profile decides alone."""
    horizon_h = max(((horizon_end if horizon_end is not None else week_resets_at) - now) / 3600.0, 0.0)
    start = current_stretch_start(bins, now)
    weight = min(0.95, max(0.0, 1.0 - horizon_h / 24.0)) if start is not None else 0.0
    if weight == 0.0:
        return {"weight": 0.0, "horizon_h": horizon_h, "frac": 0.0, "rate": rate}
    look_h = min(4.0, max(0.5, (now - start) / 3600.0))
    frac = min(1.0, active_hours_between(bins, now - look_h * 3600, now + 1) / look_h)
    rate_short = compute_rate_window(history, week_resets_at, week_used, now, bins, max(3.0, look_h))
    rate_p = 0.5 * rate_short + 0.5 * rate if rate_short is not None else rate
    return {"weight": weight, "horizon_h": horizon_h, "frac": frac, "rate": rate_p}


def daily_rate_ratios(history, bins, now):
    """Day-to-day swing of the rate: each finished day's % per active hour divided by the mean of
    those days. Compares the last history row of a day with the previous such row of the same week."""
    today = to_local(now).date()
    day_end = {}
    for row in history:  # sorted by ts, so the last row of a day wins
        d = to_local(row["ts"]).date()
        if d < today:
            day_end[(row["week_resets_at"], d)] = row
    ends = sorted(day_end.values(), key=lambda x: x["ts"])
    rates = []
    for prev, cur in zip(ends, ends[1:]):
        if prev["week_resets_at"] != cur["week_resets_at"]:
            continue
        hours = active_hours_between(bins, prev["ts"], cur["ts"])
        delta = cur["week_used"] - prev["week_used"]
        if hours >= 2.0 and delta >= 0:
            rates.append(delta / hours)
    if len(rates) < 4:
        return []
    mean = sum(rates) / len(rates)
    return [x / mean for x in rates] if mean > 0 else []


# --------------------------------------------------------------------------
# daily profile
# --------------------------------------------------------------------------

def build_daily_profile(bins, now):
    today_local = to_local(now).date()
    bins_by_date = defaultdict(set)
    for b in bins:
        d = to_local(b * TEN_MIN).date()
        if d < today_local:
            bins_by_date[d].add(b)

    # The oldest day in the window is cut short (by the retention cutoff, or where the data starts),
    # so it would look like a short work day: leave it out once there are other days
    if len(bins_by_date) >= 3:
        del bins_by_date[min(bins_by_date.keys())]

    if bins_by_date:
        first_date = min(bins_by_date.keys())
        last_date = today_local - timedelta(days=1)
        n_days = max((last_date - first_date).days + 1, 0)
        all_dates = [first_date + timedelta(days=i) for i in range(n_days)]
    else:
        all_dates = []

    daily_hours_by_date = {d: len(bins_by_date.get(d, ())) / BINS_PER_HOUR for d in all_dates}

    samples_by_weekday = defaultdict(list)
    date_count_by_weekday = defaultdict(int)
    for d in all_dates:
        wd = WEEKDAYS[d.weekday()]
        samples_by_weekday[wd].append(daily_hours_by_date[d])
        date_count_by_weekday[wd] += 1

    hour_bins_by_weekday = defaultdict(lambda: [0] * 24)
    hour_bins_all = [0] * 24
    for d, bset in bins_by_date.items():
        wd = WEEKDAYS[d.weekday()]
        for b in bset:
            hr = to_local(b * TEN_MIN).hour
            hour_bins_by_weekday[wd][hr] += 1
            hour_bins_all[hr] += 1

    def hour_share_for(wd):
        counts = hour_bins_by_weekday.get(wd, [0] * 24)
        if date_count_by_weekday.get(wd, 0) >= 3 and sum(counts) > 0:
            use = counts
        else:
            use = hour_bins_all
        total = sum(use)
        if total <= 0:
            return [1.0 / 24.0] * 24
        return [c / total for c in use]

    all_daily_values = list(daily_hours_by_date.values())
    return samples_by_weekday, hour_share_for, all_daily_values, len(bins_by_date)


def share_in_range(hour_share, start_dt, end_dt):
    total = 0.0
    day = start_dt.date()
    tz = start_dt.tzinfo
    for h in range(24):
        b_start = datetime(day.year, day.month, day.day, h, tzinfo=tz)
        b_end = b_start + timedelta(hours=1)
        ov_start = max(b_start, start_dt)
        ov_end = min(b_end, end_dt)
        overlap = (ov_end - ov_start).total_seconds()
        if overlap > 0:
            total += hour_share[h] * (overlap / 3600.0)
    return total


def daily_hours_resolved(date_d, wd, plan, samples_by_weekday, all_daily_values):
    dates_map = plan.get("dates") if isinstance(plan.get("dates"), dict) else {}
    date_str = date_d.isoformat()
    if date_str in dates_map:
        return as_float(dates_map[date_str], 6.0), "plan_date"
    hpw = plan.get("hours_per_weekday") if isinstance(plan.get("hours_per_weekday"), dict) else {}
    if wd in hpw:
        return as_float(hpw[wd], 6.0), "plan_weekday"
    samples = samples_by_weekday.get(wd) or []
    if samples:
        return sum(samples) / len(samples), "weekday_mean"
    if all_daily_values:
        return sum(all_daily_values) / len(all_daily_values), "overall_mean"
    return 6.0, "default"


# --------------------------------------------------------------------------
# remaining segments + Monte Carlo
# --------------------------------------------------------------------------

def build_segments(now_local, reset_local):
    tz = now_local.tzinfo
    today = now_local.date()
    reset_date = reset_local.date()
    segs = []
    if reset_date == today:
        segs.append((today, now_local, reset_local))
    else:
        next_midnight = datetime(today.year, today.month, today.day, tzinfo=tz) + timedelta(days=1)
        segs.append((today, now_local, next_midnight))
        d = today + timedelta(days=1)
        while d < reset_date:
            start = datetime(d.year, d.month, d.day, tzinfo=tz)
            segs.append((d, start, start + timedelta(days=1)))
            d += timedelta(days=1)
        r_start = datetime(reset_date.year, reset_date.month, reset_date.day, tzinfo=tz)
        segs.append((reset_date, r_start, reset_local))
    return segs


def monte_carlo(week_used, rate, rate_source, resolved_segments, samples_by_weekday, all_daily_values,
                day_ratios, now, today_obs=None, persist=None, anchor=None, n=MC_TRIALS):
    # today_obs = (date, active hours worked so far today, share of a typical day already gone)
    today_date, today_worked, today_gone = today_obs or (None, 0.0, 0.0)
    rng = random.Random(int(now // TEN_MIN))
    totals = []
    hours_sums = []
    # Rate = measured average x a per-trial factor for the error in that average
    #        x a per-day factor for how far one day's pace strays from it (measured once enough days exist)
    est_lo, est_hi = (0.6, 1.4) if rate_source in ("prior", "default") else (0.85, 1.15)
    pool = all_daily_values if len(all_daily_values) >= 3 else []
    for _ in range(n):
        trial_rate = rate * rng.uniform(est_lo, est_hi)
        total = week_used
        total_hours = 0.0
        for date_d, share, base_hours, source, wd, prior_f, wall_h in resolved_segments:
            if source in ("plan_date", "plan_weekday"):
                if base_hours == 0:
                    h = 0.0
                else:
                    h = base_hours * share * rng.uniform(0.85, 1.15)
            else:
                # A finished day of the same weekday, shrunk toward all days (scaled by the weekday prior)
                # while that weekday has few samples of its own
                samples = samples_by_weekday.get(wd) or []
                if pool and rng.random() >= len(samples) / (len(samples) + 2.0):
                    h = rng.choice(pool) * prior_f * share
                elif len(samples) >= 2 or (samples and pool):
                    h = rng.choice(samples) * share
                else:
                    h = base_hours * share * rng.uniform(0.5, 1.5)
                if date_d == today_date and today_gone >= 0.05:
                    # Today's own pace so far, trusted more the more of the day is gone
                    w = min(0.9, 2.0 * today_gone)
                    h = w * (today_worked / today_gone) * share + (1.0 - w) * h
            h = min(h, wall_h)  # never more active hours than clock hours left in the segment
            day_factor = rng.choice(day_ratios) if day_ratios else rng.uniform(0.4, 1.6)
            total += trial_rate * day_factor * h
            total_hours += h
        if persist and persist["weight"] > 0:
            # blend with the current stretch carried forward to the reset
            wp = persist["weight"]
            h_p = min(persist["horizon_h"], persist["frac"] * rng.uniform(0.75, 1.25) * persist["horizon_h"])
            gain_p = persist["rate"] * rng.uniform(0.85, 1.15) * h_p
            total = week_used + wp * gain_p + (1.0 - wp) * (total - week_used)
            total_hours = wp * h_p + (1.0 - wp) * total_hours
        if anchor:
            # same use as last week over the part of the week still ahead, weighted by that part
            a_total, wa, a_hours = anchor
            gain_a = a_total * wa * rng.uniform(0.85, 1.15)
            total = week_used + wa * gain_a + (1.0 - wa) * (total - week_used)
            total_hours = wa * a_hours * wa + (1.0 - wa) * total_hours
        totals.append(total)
        hours_sums.append(total_hours)
    totals.sort()
    p10 = round(percentile(totals, 10), 1)
    p50 = round(percentile(totals, 50), 1)
    p90 = round(percentile(totals, 90), 1)
    active_hours_left = sum(hours_sums) / len(hours_sums) if hours_sums else 0.0
    return p10, p50, p90, active_hours_left


DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]   # fixed names, never the locale's %a


def runout_time(now, reset, used, p50, day_hours, hour_share_for, persist_weight):
    """When the median forecast reaches 100%: its gain is spread over the time left the way work is
    expected to fall (learned hours of the day), blended with the current stretch going on evenly."""
    if p50 < 100.0:
        return None
    if used >= 100.0 or p50 <= used:
        return now
    steps = []
    t = float(now)
    while t < reset:
        lt = to_local(t)
        steps.append((t, day_hours.get(lt.date(), 0.0) * hour_share_for(WEEKDAYS[lt.weekday()])[lt.hour]))
        t += TEN_MIN
    if not steps:
        return None
    total_p = sum(w for _, w in steps)
    wp = persist_weight if total_p > 0 else 1.0
    need = (100.0 - used) / (p50 - used)
    cum = 0.0
    for t, w in steps:
        step = (1.0 - wp) * (w / total_p if total_p > 0 else 0.0) + wp / len(steps)
        if step > 0 and cum + step >= need:
            return min(reset, t + TEN_MIN * (need - cum) / step)
        cum += step
    return reset


def runout_fields(runout, reset, now_local):
    if runout is None:
        return {"runout_at": None, "idle_min": None, "runout_label": None, "idle_label": None}
    idle = max(0, int(round((reset - runout) / 60.0)))
    lt = to_local(runout)
    label = lt.strftime("%H:%M") if lt.date() == now_local.date() else "%s %s" % (DAYS[lt.weekday()], lt.strftime("%H:%M"))
    idle_label = "%dm" % idle if idle < 60 else ("%dh" % (idle // 60) + ("%dm" % (idle % 60) if idle % 60 else ""))
    return {"runout_at": int(runout), "idle_min": idle, "runout_label": label, "idle_label": idle_label}


# --------------------------------------------------------------------------
# mode / hysteresis
# --------------------------------------------------------------------------

def decide_mode(prev_mode, p50, p90, use_p90=True, goal=DEFAULT_TARGET_PERCENT):
    """The mode comes from the WEEKLY forecast only (rules/WORKING-RULES.md); the 5-hour window is shown separately and never enters it
    (a full 5-hour window only pauses work until it resets).
    use_p90=False (more than P90_DAYS before the reset): only the median decides; a tail that far out
    can still be corrected, since the forecast is redone every 10 minutes.
    goal = the weekly % to be at the target (target_percent, 100 = the old thresholds): every percent threshold of the forecast
    (80/85/95/100/105/110) is scaled by goal/100, so a 90% goal shifts PLENTY/NORMAL/TIGHT to 72/76.5/85.5/90/94.5/99."""
    k = goal / 100.0
    T = lambda x: round(x * k, 4)          # a scaled threshold
    g = lambda x: "%g" % T(x)              # ... as printed in a reason
    if not use_p90:
        if prev_mode == "PLENTY" and p50 < T(85):
            return "PLENTY", "PLENTY held: p50 %.1f<%s (median only this early)" % (p50, g(85))
        # this early the median is mostly last week's total: ~the goal means "on track", so TIGHT needs a clear overshoot
        if prev_mode == "TIGHT" and p50 > T(100):
            return "TIGHT", "TIGHT held: p50 %.1f>%s (median only this early)" % (p50, g(100))
        if p50 > T(105):
            return "TIGHT", "p50 %.1f>%s (median only this early)" % (p50, g(105))
        if p50 < T(80):
            return "PLENTY", "p50 %.1f<%s (median only this early)" % (p50, g(80))
        return "NORMAL", "p50 %.1f in normal range (median only this early)" % p50
    if prev_mode == "PLENTY" and (p50 < T(85) and p90 < T(105)):
        return "PLENTY", "PLENTY held: p50 %.1f<%s, p90 %.1f<%s" % (p50, g(85), p90, g(105))
    if prev_mode == "TIGHT" and (p50 > T(95) or p90 > T(105)):
        return "TIGHT", "TIGHT held: p50 %.1f>%s or p90 %.1f>%s" % (p50, g(95), p90, g(105))
    if p50 > T(100) or p90 > T(110):
        return "TIGHT", "p50 %.1f>%s or p90 %.1f>%s" % (p50, g(100), p90, g(110))
    if p50 < T(80) and p90 < T(100):
        return "PLENTY", "p50 %.1f<%s and p90 %.1f<%s" % (p50, g(80), p90, g(100))
    return "NORMAL", "p50 %.1f, p90 %.1f in normal range" % (p50, p90)


# --------------------------------------------------------------------------
# main computation
# --------------------------------------------------------------------------

def compute_forecast(base_dir, now=None):
    if now is None:
        now = time.time()

    pace = read_pace(base_dir)
    if pace is None:
        return None

    week_used = as_float(pace.get("week_used"), 0.0)
    five_hour_used = as_float(pace.get("five_hour_used"))  # None = Claude Code reports no 5-hour window; shown only, never part of the mode
    week_resets_at = int(as_float(pace.get("week_resets_at"), 0.0) or 0)

    plan = read_plan(base_dir)
    plan_used = os.path.exists(os.path.join(base_dir, "pace-plan.json"))
    target = as_float(plan.get("target"), DEFAULT_TARGET)  # legacy: the weekly % need_rate aims at (nothing else read it)
    goal, goal_set = target_percent_setting(plan)           # the weekly % to be at the target: modes, forecast and need_rate aim at it
    if goal_set:
        target = goal
    default_rate = as_float(plan.get("default_rate"), DEFAULT_RATE)
    target_hours = target_hours_setting(plan)
    target_at = int(week_resets_at - round(target_hours * 3600))  # forecast moment (reset - N hours)

    history = read_history(base_dir)
    prev_forecast = read_json_safe(os.path.join(base_dir, "pace-forecast.json")) or {}
    prev_mode = prev_forecast.get("mode")
    prev_mode_since = prev_forecast.get("mode_since")
    if prev_forecast.get("week_resets_at") != week_resets_at:
        prev_mode = prev_mode_since = None  # a new week decides afresh, no carry-over from the last one
    prev_prior_rate = prev_forecast.get("prior_rate")

    bins, local_weeks = update_activity_index(base_dir, now, history)
    peer_cfg = read_json_safe(os.path.join(base_dir, "pace-peer.json"))
    if isinstance(peer_cfg, dict) and peer_cfg.get("ssh_host"):
        try:
            sync_peer(base_dir, now, peer_cfg, bins, local_weeks)
        except Exception as e:
            log_error(base_dir, "peer sync: %r" % (e,))
    local_bin_count = len(bins)
    bins = bins | read_peer_bins(base_dir, int((now - ACTIVITY_WINDOW_DAYS * 86400) // TEN_MIN))
    activity_days = len({to_local(b * TEN_MIN).date() for b in bins})

    # last week: its final use anchors this week's forecast, its average rate is the rate prior
    last_total = last_week_total(merged_weeks(base_dir, local_weeks), week_resets_at)
    prev_reset = week_resets_at - WEEK_SECONDS
    last_hours = active_hours_between(bins, prev_reset - WEEK_SECONDS, prev_reset)
    last_rate = last_total / last_hours if (last_total and last_hours >= 10.0) else None

    # ---- rates (needed in both branches) ----
    week_start = week_resets_at - WEEK_SECONDS
    rate_week = compute_rate_week(week_used, week_start, now, bins)
    rate_recent = compute_rate_recent(history, week_resets_at, week_used, now, bins)

    if rate_recent is not None and rate_week is not None:
        rate = 0.6 * rate_recent + 0.4 * rate_week
        rate_source = "blend"
    elif rate_recent is not None:
        rate = rate_recent
        rate_source = "recent"
    elif rate_week is not None:
        rate = rate_week
        rate_source = "week"
    elif last_rate is not None:
        rate = last_rate
        rate_source = "last_week"
    elif prev_prior_rate is not None:
        rate = as_float(prev_prior_rate, default_rate)
        rate_source = "prior"
    else:
        rate = default_rate
        rate_source = "default"

    prior_rate_out = rate if rate_source in ("blend", "recent", "week", "last_week") else prev_prior_rate

    data_section = {
        "history_rows": len(history),
        "activity_days": activity_days,
        "peer_only_bins": len(bins) - local_bin_count,
        "plan_used": plan_used,
    }

    if week_resets_at <= now:
        wu1 = r(week_used, 1)
        mode = "NORMAL"
        mode_since = now if prev_mode != "NORMAL" else (prev_mode_since or now)
        return {
            "updated": int(now),
            "week_used": week_used,
            "five_hour_used": five_hour_used,
            "week_resets_at": week_resets_at,
            "target_at": target_at, "target_hours": target_hours,
            "p10": wu1, "p50": wu1, "p90": wu1,
            "rate": r(rate, 4), "rate_recent": r(rate_recent, 4), "rate_week": r(rate_week, 4),
            "rate_source": rate_source,
            "active_hours_left": 0.0,
            "need_rate": None,
            "target": target, "target_percent": goal,
            "mode": mode, "mode_since": int(mode_since), "mode_reason": "week reset, waiting for new data",
            "prior_rate": r(prior_rate_out, 4),
            "data": data_section,
        }

    if target_at <= now:
        # Past the target (the last N hours before the reset): what is left would be lost, so spend it (PLENTY);
        wu1 = r(week_used, 1)
        mode, reason = "PLENTY", "past the target (reset -%gh): unused quota is lost" % target_hours
        mode_since = prev_mode_since if (mode == prev_mode and prev_mode_since is not None) else now
        return {
            "updated": int(now),
            "week_used": week_used,
            "five_hour_used": five_hour_used,
            "week_resets_at": week_resets_at,
            "target_at": target_at, "target_hours": target_hours,
            "p10": wu1, "p50": wu1, "p90": wu1,
            "rate": r(rate, 4), "rate_recent": r(rate_recent, 4), "rate_week": r(rate_week, 4),
            "rate_source": rate_source,
            "active_hours_left": 0.0,
            "need_rate": None,
            "target": target, "target_percent": goal,
            "mode": mode, "mode_since": int(mode_since), "mode_reason": reason,
            "prior_rate": r(prior_rate_out, 4),
            "data": data_section,
        }

    # ---- daily profile + remaining segments ----
    samples_by_weekday, hour_share_for, all_daily_values, _n = build_daily_profile(bins, now)

    now_local = to_local(now)
    target_local = to_local(target_at)
    raw_segments = build_segments(now_local, target_local)

    # weekday_prior: the share of a normal day assumed for a weekday until it has days of its own
    wd_prior = plan.get("weekday_prior") if isinstance(plan.get("weekday_prior"), dict) else {}
    resolved_segments = []
    for date_d, start_dt, end_dt in raw_segments:
        wd = WEEKDAYS[date_d.weekday()]
        share = share_in_range(hour_share_for(wd), start_dt, end_dt)
        base_hours, source = daily_hours_resolved(date_d, wd, plan, samples_by_weekday, all_daily_values)
        prior_f = as_float(wd_prior.get(wd), 1.0)
        if source in ("overall_mean", "default"):
            base_hours *= prior_f
        wall_h = (end_dt - start_dt).total_seconds() / 3600.0
        resolved_segments.append((date_d, share, base_hours, source, wd, prior_f, wall_h))

    today = now_local.date()
    midnight = datetime(today.year, today.month, today.day, tzinfo=now_local.tzinfo)
    today_obs = (today, active_hours_between(bins, midnight.timestamp(), now),
                 share_in_range(hour_share_for(WEEKDAYS[today.weekday()]), midnight, now_local))

    day_ratios = daily_rate_ratios(history, bins, now)
    persist = persistence(bins, history, week_resets_at, week_used, now, rate, horizon_end=target_at)
    # early in the week the forecast leans on last week's total, fading out as the week goes by
    anchor = None
    if last_total:
        left_share = min(1.0, max(0.0, (target_at - now) / WEEK_SECONDS))
        anchor = (last_total, left_share, last_hours)
        data_section["last_week_used"] = r(last_total, 1)
        data_section["anchor_weight"] = round(left_share, 2)
    data_section["pool_days"] = len(all_daily_values)
    data_section["rate_days"] = len(day_ratios)
    p10, p50, p90, active_hours_left = monte_carlo(
        week_used, rate, rate_source, resolved_segments, samples_by_weekday, all_daily_values, day_ratios, now,
        today_obs, persist, anchor
    )
    data_section["persist_weight"] = round(persist["weight"], 2)
    data_section["persist_frac"] = round(persist["frac"], 2)
    data_section["today_worked_h"] = round(today_obs[1], 1)

    need_rate = (target - week_used) / active_hours_left if active_hours_left >= 0.25 else None

    mode, reason = decide_mode(prev_mode, p50, p90,
                               use_p90=(target_at - now) <= P90_DAYS * 86400, goal=goal)
    mode_since = prev_mode_since if (mode == prev_mode and prev_mode_since is not None) else now

    # past 100%: when the limit runs out and how long it stays empty before the reset
    day_hours = {}
    for date_d, _share, base_hours, _source, _wd, _pf, _wall in resolved_segments:
        day_hours[date_d] = base_hours
    runout = runout_time(now, target_at, week_used, p50, day_hours, hour_share_for, persist["weight"])

    return {
        "updated": int(now),
        "week_used": week_used,
        "five_hour_used": five_hour_used,
        "week_resets_at": week_resets_at,
        "target_at": target_at, "target_hours": target_hours,
        "p10": p10, "p50": p50, "p90": p90,
        "rate": r(rate, 4), "rate_recent": r(rate_recent, 4), "rate_week": r(rate_week, 4),
        "rate_source": rate_source,
        "active_hours_left": r(active_hours_left, 2),
        "need_rate": r(need_rate, 3),
        "target": target, "target_percent": goal,
        "mode": mode, "mode_since": int(mode_since), "mode_reason": reason,
        "prior_rate": r(prior_rate_out, 4),
        **runout_fields(runout, week_resets_at, now_local),
        "data": data_section,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--print", action="store_true", dest="do_print")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--rebuild", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.selftest:
        ok = run_selftest()
        sys.exit(0 if ok else 1)

    base_dir = os.path.expanduser("~/.claude")
    lock = acquire_lock(base_dir)
    if lock is None:
        sys.exit(0)

    result = None
    try:
        if args.rebuild:
            try:
                os.remove(os.path.join(base_dir, "pace-activity.json"))
            except OSError:
                pass
        result = compute_forecast(base_dir, now=time.time())
        if result is not None:
            atomic_write_json(os.path.join(base_dir, "pace-forecast.json"), result)
    except Exception:
        log_error(base_dir, "ERROR:\n" + traceback.format_exc())
    finally:
        release_lock(lock)

    if args.do_print:
        try:
            if result is not None:
                print(json.dumps(result))
            else:
                p = os.path.join(base_dir, "pace-forecast.json")
                if os.path.exists(p):
                    with open(p, encoding="utf-8-sig") as f:
                        print(f.read())
        except Exception:
            pass

    sys.exit(0)


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------

def _local_epoch(y, mo, d, h=0, mi=0, se=0):
    return time.mktime((y, mo, d, h, mi, se, 0, 0, -1))


def _write_pace(base, week_used, five_hour_used, week_resets_at, now):
    os.makedirs(base, exist_ok=True)
    atomic_write_json(os.path.join(base, "pace.json"), {
        "week_used": week_used, "five_hour_used": five_hour_used,
        "week_resets_at": int(week_resets_at), "updated": int(now),
    })


def _next_weekday_on_or_after(anchor, target_weekday):
    days_ahead = (target_weekday - anchor.weekday()) % 7
    return anchor + timedelta(days=days_ahead)


def _case_a():
    """no history and no transcripts -> default rate"""
    with tempfile.TemporaryDirectory(prefix="pace-selftest-a-") as base:
        now = time.time()
        _write_pace(base, week_used=10.0, five_hour_used=5.0, week_resets_at=now + 3 * 86400, now=now)
        res = compute_forecast(base, now=now)
        ok = (res is not None and res["rate_source"] == "default"
              and abs(res["rate"] - DEFAULT_RATE) < 1e-9
              and res["rate_recent"] is None and res["rate_week"] is None
              and res["p50"] >= res["week_used"])
        detail = "" if ok else "res=%r" % (res,)
        return ok, detail


def _case_b():
    """steady 2%/h during 10h/day for 4 days, mid-week -> p50 near arithmetic expectation"""
    with tempfile.TemporaryDirectory(prefix="pace-selftest-b-") as base:
        anchor = date(2027, 1, 1)
        wed = _next_weekday_on_or_after(anchor, 2)  # Wednesday
        now = _local_epoch(wed.year, wed.month, wed.day, 12, 0, 0)
        reset = now + 2 * 86400  # keeps week_start well before the 4 sample days
        _write_pace(base, week_used=80.0, five_hour_used=10.0, week_resets_at=reset, now=now)

        bins = []
        start_day = wed - timedelta(days=4)
        for i in range(4):
            d = start_day + timedelta(days=i)
            for hour in range(9, 19):
                for minute in (0, 10, 20, 30, 40, 50):
                    ep = _local_epoch(d.year, d.month, d.day, hour, minute, 0)
                    bins.append(int(ep) // TEN_MIN)
        atomic_write_json(os.path.join(base, "pace-activity.json"), {"files": {}, "bins": sorted(set(bins))})

        res = compute_forecast(base, now=now)
        naive = res["week_used"] + res["rate"] * res["active_hours_left"]
        tol = max(1.0, 0.25 * naive)
        ok = (res is not None and res["rate_source"] == "week"
              and abs(res["rate"] - 2.0) < 0.05
              and abs(res["p50"] - naive) <= tol)
        detail = "" if ok else "res=%r naive=%.2f" % (res, naive)
        return ok, detail


def _case_c():
    """a plan with Sunday 0 lowers p50 versus no plan"""
    anchor = date(2027, 1, 1)
    thu = _next_weekday_on_or_after(anchor, 3)  # Thursday
    now = _local_epoch(thu.year, thu.month, thu.day, 10, 0, 0)
    reset = now + 5 * 86400  # spans a full Sunday

    with tempfile.TemporaryDirectory(prefix="pace-selftest-c1-") as base_a:
        _write_pace(base_a, week_used=20.0, five_hour_used=5.0, week_resets_at=reset, now=now)
        res_a = compute_forecast(base_a, now=now)

    with tempfile.TemporaryDirectory(prefix="pace-selftest-c2-") as base_b:
        _write_pace(base_b, week_used=20.0, five_hour_used=5.0, week_resets_at=reset, now=now)
        with open(os.path.join(base_b, "pace-plan.json"), "w", encoding="utf-8") as f:
            json.dump({"hours_per_weekday": {"sun": 0}}, f)
        res_b = compute_forecast(base_b, now=now)

    with tempfile.TemporaryDirectory(prefix="pace-selftest-c3-") as base_c:
        _write_pace(base_c, week_used=20.0, five_hour_used=5.0, week_resets_at=reset, now=now)
        with open(os.path.join(base_c, "pace-plan.json"), "w", encoding="utf-8") as f:
            json.dump({"weekday_prior": {"sun": 0.3}}, f)
        res_c = compute_forecast(base_c, now=now)

    ok = (res_a is not None and res_b is not None and res_c is not None
          and res_b["p50"] < res_c["p50"] < res_a["p50"])
    detail = "" if ok else "res_a=%r res_b=%r res_c=%r" % (res_a, res_b, res_c)
    return ok, detail


def _case_g():
    """a weekday without samples draws from all finished days; the oldest (cut-short) day is left out"""
    with tempfile.TemporaryDirectory(prefix="pace-selftest-g-") as base:
        anchor = date(2027, 1, 1)
        sat = _next_weekday_on_or_after(anchor, 5)  # Saturday
        now = _local_epoch(sat.year, sat.month, sat.day, 8, 0, 0)
        reset = _local_epoch(sat.year, sat.month, sat.day, 23, 0, 0)
        _write_pace(base, week_used=30.0, five_hour_used=5.0, week_resets_at=reset, now=now)
        bins = []
        for i in range(5, 0, -1):  # Mon..Fri, 10 h each; Monday only from 15:00 (cut short)
            d = sat - timedelta(days=i)
            for hour in range(15 if i == 5 else 9, 19):
                for minute in (0, 10, 20, 30, 40, 50):
                    bins.append(int(_local_epoch(d.year, d.month, d.day, hour, minute, 0)) // TEN_MIN)
        atomic_write_json(os.path.join(base, "pace-activity.json"), {"files": {}, "bins": sorted(set(bins))})
        res = compute_forecast(base, now=now)
        naive = res["week_used"] + res["rate"] * 10.0
        ok = (res["data"]["pool_days"] == 4 and abs(res["active_hours_left"] - 10.0) < 0.5
              and abs(res["p50"] - naive) < 1.5 and res["p10"] < res["p50"] < res["p90"])
        detail = "" if ok else "res=%r naive=%.2f" % (res, naive)
        return ok, detail


def _case_i():
    """today's own pace so far overrides a low weekday prior for the rest of today"""
    anchor = date(2027, 1, 1)
    sun = _next_weekday_on_or_after(anchor, 6)
    now = _local_epoch(sun.year, sun.month, sun.day, 11, 0, 0)
    reset = _local_epoch(sun.year, sun.month, sun.day, 21, 0, 0)

    def run(worked_today):
        with tempfile.TemporaryDirectory(prefix="pace-selftest-i-") as base:
            _write_pace(base, week_used=60.0, five_hour_used=10.0, week_resets_at=reset, now=now)
            with open(os.path.join(base, "pace-plan.json"), "w", encoding="utf-8") as f:
                json.dump({"weekday_prior": {"sun": 0.3}}, f)
            bins = []
            for i in range(5, 0, -1):
                d = sun - timedelta(days=i)
                for hour in range(8, 21):
                    for minute in (0, 10, 20, 30, 40, 50):
                        bins.append(int(_local_epoch(d.year, d.month, d.day, hour, minute, 0)) // TEN_MIN)
            if worked_today:
                for hour in range(8, 11):
                    for minute in (0, 10, 20, 30, 40, 50):
                        bins.append(int(_local_epoch(sun.year, sun.month, sun.day, hour, minute, 0)) // TEN_MIN)
            atomic_write_json(os.path.join(base, "pace-activity.json"), {"files": {}, "bins": sorted(set(bins))})
            return compute_forecast(base, now=now)

    idle, busy = run(False), run(True)
    ok = idle["active_hours_left"] < 4.0 and 5.0 < busy["active_hours_left"] <= 10.0 and busy["p50"] > idle["p50"]
    return ok, "" if ok else "idle=%r busy=%r" % (idle, busy)


def _case_j():
    """near the reset the current work stretch is carried forward, and the range narrows"""
    anchor = date(2027, 1, 1)
    day = _next_weekday_on_or_after(anchor, 6)
    reset = _local_epoch(day.year, day.month, day.day, 21, 0, 0)

    def run(now_hour):
        now = _local_epoch(day.year, day.month, day.day, now_hour, 0, 0)
        with tempfile.TemporaryDirectory(prefix="pace-selftest-j-") as base:
            _write_pace(base, week_used=60.0, five_hour_used=10.0, week_resets_at=reset, now=now)
            bins = []
            for i in range(5, 0, -1):  # past days: mornings only
                d = day - timedelta(days=i)
                for hour in range(8, 12):
                    for minute in (0, 10, 20, 30, 40, 50):
                        bins.append(int(_local_epoch(d.year, d.month, d.day, hour, minute, 0)) // TEN_MIN)
            for hour in range(8, now_hour):  # today: working since 08:00 without a break
                for minute in (0, 10, 20, 30, 40, 50):
                    bins.append(int(_local_epoch(day.year, day.month, day.day, hour, minute, 0)) // TEN_MIN)
            atomic_write_json(os.path.join(base, "pace-activity.json"), {"files": {}, "bins": sorted(set(bins))})
            return compute_forecast(base, now=now)

    early, late = run(11), run(19)
    ok = (late["active_hours_left"] > 1.5 and late["data"]["persist_weight"] > 0.9
          and (late["p90"] - late["p10"]) < (early["p90"] - early["p10"]))
    return ok, "" if ok else "early=%r late=%r" % (early, late)


def _case_k():
    """past 100%: run-out time and idle minutes before the reset"""
    anchor = date(2027, 1, 1)
    day = _next_weekday_on_or_after(anchor, 6)
    now = _local_epoch(day.year, day.month, day.day, 19, 0, 0)
    reset = _local_epoch(day.year, day.month, day.day, 21, 0, 0)
    flat = lambda wd: [1.0 / 24.0] * 24
    even = runout_time(now, reset, 86.0, 102.0, {day: 10.0}, flat, 1.0)  # 14/16 of 2 h -> 20:45
    under = runout_time(now, reset, 86.0, 99.0, {day: 10.0}, flat, 1.0)
    over = runout_time(now, reset, 100.5, 104.0, {day: 10.0}, flat, 0.5)
    f = runout_fields(even, reset, to_local(now))
    tomorrow = runout_fields(now + 86400 + 3600, now + 86400 + 3600 + 5400, to_local(now))
    ok = (abs(even - (now + 105 * 60)) < 1 and under is None and over == now
          and f["runout_label"] == "20:45" and f["idle_min"] == 15 and f["idle_label"] == "15m"
          and tomorrow["runout_label"] == "Mon 20:00" and tomorrow["idle_label"] == "1h30m")
    return ok, "" if ok else "even=%r f=%r tomorrow=%r" % (even, f, tomorrow)


def _case_l():
    """new week: last week's total (from the peer too) anchors the forecast, no TIGHT carried over"""
    anchor_d = date(2027, 1, 1)
    day = _next_weekday_on_or_after(anchor_d, 6)
    reset = _local_epoch(day.year, day.month, day.day, 21, 0, 0)
    now = reset + 600
    with tempfile.TemporaryDirectory(prefix="pace-selftest-l-") as base:
        _write_pace(base, week_used=0.0, five_hour_used=5.0, week_resets_at=reset + WEEK_SECONDS, now=now)
        bins = []
        for i in range(4, 0, -1):  # four heavy days at the end of last week: 16 h each
            d = day - timedelta(days=i)
            for hour in range(6, 22):
                for minute in (0, 10, 20, 30, 40, 50):
                    bins.append(int(_local_epoch(d.year, d.month, d.day, hour, minute, 0)) // TEN_MIN)
        atomic_write_json(os.path.join(base, "pace-activity.json"), {"files": {}, "bins": sorted(set(bins))})
        atomic_write_json(os.path.join(base, "pace-activity-peer.json"), {"bins": [], "weeks": {str(int(reset)): 99.0}})
        atomic_write_json(os.path.join(base, "pace-forecast.json"), {
            "mode": "TIGHT", "mode_since": int(reset - 86400), "prior_rate": 2.5, "week_resets_at": reset})
        res = compute_forecast(base, now=now)
    ok = (res["data"].get("last_week_used") == 99.0 and res["rate_source"] == "last_week"
          and 85 <= res["p50"] <= 110 and "held" not in res["mode_reason"]
          and "median only" in res["mode_reason"]
          and decide_mode(None, 99.0, 112.0, use_p90=False)[0] == "NORMAL"
          and decide_mode(None, 104.0, 115.0, use_p90=False)[0] == "NORMAL"
          and decide_mode("TIGHT", 98.0, 110.0, use_p90=False)[0] == "NORMAL"
          and decide_mode("TIGHT", 102.0, 110.0, use_p90=False)[0] == "TIGHT"
          and decide_mode(None, 99.0, 112.0, use_p90=True)[0] == "TIGHT"
          and decide_mode(None, 78.0, 104.0, use_p90=False)[0] == "PLENTY")
    return ok, "" if ok else "res=%r" % (res,)


def _case_h():
    """day-to-day swing of the rate is measured from the history once there are enough days"""
    anchor = date(2027, 1, 1)
    mon = _next_weekday_on_or_after(anchor, 0)
    now = _local_epoch(mon.year, mon.month, mon.day, 12, 0, 0) + 7 * 86400
    reset = now + 86400
    bins, history, used = [], [], 0.0
    for i in range(6):  # six finished days, 10 active hours each, 5 / 10 / 15 % per day
        d = mon + timedelta(days=i)
        for hour in range(9, 19):
            for minute in (0, 10, 20, 30, 40, 50):
                bins.append(int(_local_epoch(d.year, d.month, d.day, hour, minute, 0)) // TEN_MIN)
        used += (5.0, 10.0, 15.0)[i % 3]
        history.append({"ts": int(_local_epoch(d.year, d.month, d.day, 20, 0, 0)), "week_used": used,
                        "five_hour_used": 0.0, "week_resets_at": int(reset), "five_hour_resets_at": None})
    ratios = daily_rate_ratios(history, set(bins), now)
    ok = len(ratios) == 5 and abs(sum(ratios) / len(ratios) - 1.0) < 1e-9 and max(ratios) > 1.3
    return ok, "" if ok else "ratios=%r" % (ratios,)


def _case_d():
    """hysteresis: p50 82 keeps a previous PLENTY, but a fresh read does not enter PLENTY"""
    now = time.time()
    reset = now + 60  # future, but negligible remaining active hours

    with tempfile.TemporaryDirectory(prefix="pace-selftest-d1-") as base1:
        _write_pace(base1, week_used=82.0, five_hour_used=50.0, week_resets_at=reset, now=now)
        atomic_write_json(os.path.join(base1, "pace-forecast.json"), {
            "mode": "PLENTY", "mode_since": int(now - 3600), "prior_rate": None, "week_resets_at": int(reset),
        })
        res1 = compute_forecast(base1, now=now)
    ok1 = res1 is not None and res1["mode"] == "PLENTY" and res1["mode_since"] == int(now - 3600)

    with tempfile.TemporaryDirectory(prefix="pace-selftest-d2-") as base2:
        _write_pace(base2, week_used=82.0, five_hour_used=50.0, week_resets_at=reset, now=now)
        res2 = compute_forecast(base2, now=now)
    ok2 = res2 is not None and res2["mode"] != "PLENTY"

    ok = ok1 and ok2
    detail = "" if ok else "res1=%r res2=%r" % (res1, res2)
    return ok, detail


def _case_e():
    """five_hour_used 85 with a weekly forecast on track -> NOT TIGHT (the 5-hour window never enters the mode)"""
    with tempfile.TemporaryDirectory(prefix="pace-selftest-e-") as base:
        now = time.time()
        _write_pace(base, week_used=50.0, five_hour_used=85.0, week_resets_at=now + 2 * 86400, now=now)
        res = compute_forecast(base, now=now)
        ok = (res is not None and res["mode"] != "TIGHT" and res["five_hour_used"] == 85.0
              and "5h" not in res["mode_reason"])
        detail = "" if ok else "res=%r" % (res,)
        return ok, detail


def _case_f_reset_passed():
    """bonus: week_resets_at already in the past -> flat p10=p50=p90=week_used, NORMAL"""
    with tempfile.TemporaryDirectory(prefix="pace-selftest-f-") as base:
        now = time.time()
        _write_pace(base, week_used=37.5, five_hour_used=1.0, week_resets_at=now - 10, now=now)
        res = compute_forecast(base, now=now)
        ok = (res is not None and res["p10"] == res["p50"] == res["p90"] == round(37.5, 1)
              and res["mode"] == "NORMAL")
        detail = "" if ok else "res=%r" % (res,)
        return ok, detail


def _case_m():
    now = _local_epoch(2026, 10, 21, 12)
    reset = _local_epoch(2026, 10, 25, 2)
    res = {}
    for n in ("0", "5"):
        os.environ["PACE_TARGET_HOURS"] = n
        with tempfile.TemporaryDirectory() as base:
            _write_pace(base, week_used=40.0, five_hour_used=10.0, week_resets_at=reset, now=now)
            res[n] = compute_forecast(base, now=now)
    a, b = res["0"], res["5"]
    ok = (a["target_at"] == reset and b["target_at"] == reset - 5 * 3600
          and b["active_hours_left"] <= a["active_hours_left"] and b["p50"] <= a["p50"] + 1.0)
    return ok, "n0 target=%s p50=%s h=%s | n5 target=%s p50=%s h=%s" % (
        a["target_at"], a["p50"], a["active_hours_left"], b["target_at"], b["p50"], b["active_hours_left"])


def _case_n():
    reset = _local_epoch(2026, 10, 25, 2)
    now = reset - 3 * 3600  # inside the last 5 hours
    os.environ["PACE_TARGET_HOURS"] = "5"
    out = []
    for fh in (10.0, 85.0):
        with tempfile.TemporaryDirectory() as base:
            _write_pace(base, week_used=95.0, five_hour_used=fh, week_resets_at=reset, now=now)
            out.append(compute_forecast(base, now=now)["mode"])
    return out == ["PLENTY", "PLENTY"], "modes=%s" % out


def _case_o():
    """five_hour_used None (no 5-hour window reported) -> shown as null, the reason names no 5h"""
    with tempfile.TemporaryDirectory(prefix="pace-selftest-o-") as base:
        now = time.time()
        _write_pace(base, week_used=50.0, five_hour_used=None, week_resets_at=now + 2 * 86400, now=now)
        res = compute_forecast(base, now=now)
        ok = (res is not None and res["five_hour_used"] is None and res["mode"] != "TIGHT"
              and "5h" not in res["mode_reason"])
        return ok, "" if ok else "res=%r" % (res,)


def _case_p():
    """target_percent scales the percent thresholds of the modes (100 = the old ones); the target hours reach 60 (Friday 09:00)"""
    d100 = [decide_mode(None, 91.0, 95.0)[0], decide_mode(None, 75.0, 85.0)[0]]
    d90 = [decide_mode(None, 91.0, 95.0, goal=90.0)[0], decide_mode(None, 75.0, 85.0, goal=90.0)[0]]
    saved = os.environ.pop("PACE_TARGET_HOURS", None)
    try:
        h60, hcap = target_hours_setting({"target_hours": 60}), target_hours_setting({"target_hours": 200, "max_target_hours": 500})
    finally:
        if saved is not None:
            os.environ["PACE_TARGET_HOURS"] = saved
    ok = d100 == ["NORMAL", "PLENTY"] and d90 == ["TIGHT", "NORMAL"] and h60 == 60.0 and hcap == ABS_MAX_TARGET_HOURS
    return ok, "goal100=%s goal90=%s hours60=%s cap=%s" % (d100, d90, h60, hcap)


def run_selftest():
    cases = [
        ("a: no data -> default rate", _case_a),
        ("b: steady 2%/h, 10h/day -> p50 near expectation", _case_b),
        ("c: plan Sunday=0 < Sunday prior 0.3 < no plan", _case_c),
        ("d: hysteresis keeps PLENTY / fresh does not enter PLENTY", _case_d),
        ("e: five_hour_used 85 -> not TIGHT (weekly only)", _case_e),
        ("f: week reset already passed -> flat/NORMAL", _case_f_reset_passed),
        ("g: weekday without data draws from all days, cut-short day left out", _case_g),
        ("h: day-to-day rate swing measured from history", _case_h),
        ("i: today's pace so far overrides a low weekday prior", _case_i),
        ("j: near the reset the current stretch carries forward, range narrows", _case_j),
        ("k: past 100% -> run-out time and idle minutes", _case_k),
        ("l: new week anchored on last week's total, no mode carried over", _case_l),
        ("m: target = reset - N h; N=0 identical to the reset forecast", _case_m),
        ("n: past the target -> PLENTY (also with a full 5h window)", _case_n),
        ("o: no 5-hour window reported -> no 5h in the reason", _case_o),
        ("p: target_percent scales the mode thresholds, target hours up to 60 (cap 96)", _case_p),
    ]
    all_ok = True
    saved = os.environ.get("PACE_TARGET_HOURS")
    os.environ["PACE_TARGET_HOURS"] = "0"  # a-l check the old at-the-reset behavior
    for name, fn in cases:
        try:
            ok, detail = fn()
        except Exception:
            ok, detail = False, traceback.format_exc()
        all_ok = all_ok and ok
        print("%s %s%s" % ("PASS" if ok else "FAIL", name, ("" if ok else " -- " + detail)))
    if saved is None:
        os.environ.pop("PACE_TARGET_HOURS", None)
    else:
        os.environ["PACE_TARGET_HOURS"] = saved
    return all_ok


if __name__ == "__main__":
    main()

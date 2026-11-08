#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Quota calibration for cost: how many API-price dollars one percent of the WEEKLY limit is worth ("gerçek $").

Claude Code reports the weekly limit as an integer percent with every response; the status line logs a row to pace-history.csv
whenever it changes (ts, week_used, five_hour_used, week_resets_at, ...). cost_core keeps per-UTC-minute API-price dollars of all
local transcripts ("min" buckets). This module joins the two, pure functions first (tested without files):

  windows:  inside one limit week (same week_resets_at) the percent only counts when SEEN rising. Every rise is a point
            (time of the response that showed it, new level). A window runs from one point to the first later point that is at
            least ADIM percentage points higher (integer rounding: one point is +-0.5 %, so a short window is noisy; with ADIM = 4
            the rounding error is at most 25 % per window and cancels across windows). Windows do not overlap.
  ratio:    per window API $ spent locally in (start, end] / percentage points gained.
  robust:   median of the per-window ratios, drop windows outside [median/3, median*3] (percent that rose with almost no local
            spend = claude.ai web/desktop or another machine; local spend with almost no percent), the result is the sum of
            dollars over the sum of points of the kept windows. Fewer than MIN_WINDOW kept windows = no result.

gerçek $ of an agent = its API $ / (API $ per 100 %) x W, with W = plan price x 12 / 52 (what one limit week costs on the plan).
Plan price: pace-plan.json "plan_usd_monthly" > CLAUDE_SHARED_PLAN / ~/.config/claude-shared/plan (100|200) > the account tier in
.credentials.json (only subscriptionType and rateLimitTier are read, nothing else) > unknown (then no "gerçek").
Result file: <config dir>/cost/oran.json (0600), refreshed in the background by cost_core.refresh_status.
"""
import csv
import json
import os
import re
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", "tools"))  # repo layout: tools/hidden.py (a --copy install ships it next to this file)
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from hidden_loader import hidden  # noqa: E402  (one hidden-launch helper)
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # python -I does not add the script directory

STEP = 4              # minimum percentage points per window
MIN_WINDOW = 4       # kept windows needed for a result
DAY = 7               # only windows that ended in the last GUN days (older weeks can sit in another regime: other machines, web use, limit changes)
FAKE_RESET = 4102444800   # placeholder reset time the status line may log (year 2100): not a real week
CLAUDE = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
DIR = os.path.join(CLAUDE, "cost")
RATIO = os.path.join(DIR, "ratio.json")
HISTORY = os.path.join(CLAUDE, "pace-history.csv")
PLAN_FILE = os.path.join(CLAUDE, "pace-plan.json")
TIER_PLAN = (("max_20x", 200), ("max_5x", 100))   # rateLimitTier substrings
TIP_PLAN = {"pro": 20}                            # subscriptionType


def points(lines):
    """satirlar: [(ts, week_used, week_resets_at)] -> {reset: [(ts, level)]}: per limit week the points where the level was seen
    to RISE (running maximum, so a stale lower value from an old session is ignored). The first row of a week is not a rise."""
    weeks = {}
    for ts, usage, reset in sorted(lines):
        if reset == FAKE_RESET or not reset:
            continue
        weeks.setdefault(reset, []).append((ts, usage))
    out = {}
    for reset, rows in weeks.items():
        peak = rows[0][1]
        pts = []
        for ts, k in rows[1:]:
            if k > peak:
                peak = k
                pts.append((ts, k))
        if pts:
            out[reset] = pts
    return out


def windows(points_week, step=STEP):
    """{reset: [(ts, level)]} -> [(t_start, t_end, points)], non-overlapping, inside each week."""
    out = []
    for pts in points_week.values():
        i = 0
        while i < len(pts):
            t0, l0 = pts[i]
            j = next((k for k in range(i + 1, len(pts)) if pts[k][1] - l0 >= step), None)
            if j is None:
                break
            out.append((t0, pts[j][0], pts[j][1] - l0))
            i = j
    return out


def usd(min_ts, min_usd, t0, t1):
    """Dollars in the minute buckets after the minute of t0 up to and including the minute of t1.
    min_ts: sorted list of minute indexes, min_usd: matching dollars."""
    import bisect
    a = bisect.bisect_right(min_ts, int(t0 // 60))
    b = bisect.bisect_right(min_ts, int(t1 // 60))
    return sum(min_usd[a:b])


def medyan(x):
    s = sorted(x)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def compute_ratio(lines, minutes, now=None, step=STEP, day=DAY, min_window=MIN_WINDOW):
    """satirlar: [(ts, week_used, week_resets_at)]; dakika: {minute index: usd}. Returns a dict
    {usd_per_pct, n, atilan, adim, medyan, window_day} or None when there is not enough data."""
    now = time.time() if now is None else now
    ks = sorted(minutes)
    ku = [minutes[k] for k in ks]
    ham = []
    for t0, t1, score in windows(points(lines), step):
        if t1 < now - day * 86400:
            continue
        ham.append((usd(ks, ku, t0, t1), score))
    if len(ham) < min_window:
        return None
    med = medyan([d / p for d, p in ham])
    if med <= 0:
        return None
    remaining = [(d, p) for d, p in ham if med / 3.0 <= d / p <= med * 3.0]
    if len(remaining) < min_window:
        return None
    return {"usd_per_pct": sum(d for d, _ in remaining) / sum(p for _, p in remaining), "n": len(remaining), "dropped": len(ham) - len(remaining),
            "step": step, "medyan": med, "window_day": day}


def _keychain_tier(config_dir=None):
    """macOS only: rateLimitTier/subscriptionType from the Keychain item "Claude Code-credentials" (the blob also holds OAuth
    tokens: it is parsed in-process and only those two strings are kept). Cached as a tier string in <config>/plan-tier.cache
    (TTL 1 day, failures cached for 10 minutes) so the status line does not spawn `security` on every refresh. Fails silently."""
    if sys.platform != "darwin":
        return ""
    cache = os.path.join(config_dir or CLAUDE, "plan-tier.cache")
    try:
        with open(cache, encoding="utf-8") as f:
            lines = f.read().split("\n")
        if time.time() - os.path.getmtime(cache) < (86400 if lines[0] == "ok" else 600):
            return lines[1] if len(lines) > 1 else ""
    except (OSError, ValueError):
        pass
    tier = ""
    try:
        import subprocess
        r = hidden.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                           capture_output=True, timeout=2, stdin=subprocess.DEVNULL)
        o = (json.loads(r.stdout.decode("utf-8", "replace") or "{}").get("claudeAiOauth") or {}) if r.returncode == 0 else {}
        tier = (str(o.get("rateLimitTier") or "") + " " + str(o.get("subscriptionType") or "")).strip().lower()
    except Exception:
        tier = ""
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            f.write(("ok\n" if tier else "yok\n") + tier)
    except OSError:
        pass
    return tier


def plan_identity(config_dir=None):
    """Monthly plan price from the account tier: max_20x -> 200, max_5x -> 100, pro -> 20; anything else (team default tier,
    enterprise, unknown, nothing readable) -> None. Sources in order, only tier strings are looked at:
    1. <config>/.credentials.json claudeAiOauth (Linux/Windows), 2. oauthAccount.userRateLimitTier in .claude.json (no secrets;
    this is where macOS has it), 3. macOS Keychain (see _keychain_tier)."""
    cfg = config_dir or CLAUDE
    sources = []
    try:
        with open(os.path.join(cfg, ".credentials.json"), encoding="utf-8") as f:
            o = json.load(f).get("claudeAiOauth") or {}
        sources.append((str(o.get("rateLimitTier") or "").lower(), str(o.get("subscriptionType") or "").lower()))
    except (OSError, ValueError, AttributeError):
        pass
    for path in (os.path.join(cfg, ".claude.json"), os.path.join(os.path.expanduser("~"), ".claude.json")):
        try:
            with open(path, encoding="utf-8") as f:
                h = json.load(f).get("oauthAccount") or {}
            sources.append((str(h.get("userRateLimitTier") or "").lower(), ""))
            break
        except (OSError, ValueError, AttributeError):
            continue
    kt = None
    while True:
        for tier, tip in sources:
            for part, price in TIER_PLAN:
                if part in tier:
                    return price
            if tip in TIP_PLAN:
                return TIP_PLAN[tip]
        if kt is not None:
            return None
        kt = _keychain_tier(cfg)
        sources = [(kt, kt.split()[-1] if kt else "")]


def plan_monthly(config_dir=None):
    """Monthly plan price in USD: pace-plan.json plan_usd_monthly, else the claude-shared plan setting, else the account tier."""
    try:
        with open(os.path.join(config_dir or CLAUDE, "pace-plan.json"), encoding="utf-8-sig") as f:
            v = json.load(f).get("plan_usd_monthly")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return float(v)
    except (OSError, ValueError, AttributeError):
        pass
    setting = os.environ.get("CLAUDE_SHARED_PLAN")
    if not setting:
        try:
            with open(os.path.join(os.path.expanduser("~"), ".config", "claude-shared", "plan"), encoding="utf-8") as f:
                setting = f.readline()
        except OSError:
            setting = ""
    m = re.search(r"\d+", setting or "")
    if m and int(m.group()) in (20, 100, 200):
        return float(m.group())
    p = plan_identity(config_dir)
    return float(p) if p else None


def week_usd(plan):
    """What one limit week costs on the plan: monthly price x 12 / 52."""
    return plan * 12.0 / 52.0


def read_history(path=None):
    out = []
    try:
        with open(path or HISTORY, encoding="utf-8") as f:
            for r in csv.reader(f):
                try:
                    out.append((float(r[0]), float(r[1]), int(float(r[3]))))
                except (ValueError, IndexError):
                    continue
    except OSError:
        pass
    return out


def minute_list(files):
    """cost_core.scan() result -> {minute: usd} summed over all transcripts."""
    d = {}
    for F in files.values():
        for k, v in (F.get("min") or {}).items():
            m = int(k)
            d[m] = d.get(m, 0.0) + v
    return d


def refresh(files, now=None):
    """Recompute and write oran.json from the scan. Keeps the previous file when there is not enough data yet."""
    now = time.time() if now is None else now
    plan = plan_monthly()
    result = compute_ratio(read_history(), minute_list(files), now)
    if result is None:
        try:   # not enough fresh windows: keep the last good result (read_coefficient ignores it once it is 3 days old)
            with open(RATIO, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("coefficient"):
                return old
        except (OSError, ValueError, AttributeError):
            pass
    if result is None or plan is None:
        o = {"t": now, "missing": "plan" if result is not None else "data"}
        if result is not None:
            o.update(result)
    else:
        w = week_usd(plan)
        o = dict(result, t=now, plan=plan, week_usd=w, coefficient=w / (result["usd_per_pct"] * 100.0))
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    tmp = "%s.tmp%d" % (RATIO, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(o, f, separators=(",", ":"))
    os.replace(tmp, RATIO)
    return o


def read_coefficient(now=None, en_old=3 * 86400):
    """gerçek $ = API $ x katsayi. None when oran.json is missing, stale or has no result."""
    now = time.time() if now is None else now
    try:
        with open(RATIO, encoding="utf-8") as f:
            o = json.load(f)
        k = o.get("coefficient")
        if isinstance(k, (int, float)) and k > 0 and now - float(o.get("t") or 0) <= en_old:
            return float(k)
    except (OSError, ValueError, AttributeError):
        pass
    return None

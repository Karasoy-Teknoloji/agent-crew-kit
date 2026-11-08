# -*- coding: utf-8 -*-
"""cost_efficiency: the weekly cost-per-finished-work side of the usage leaderboard (RANKING.md).

Plain Python, spends no model tokens. It adds NO new job: cost_share (the existing 2-hourly `cost --save --share` run) calls it.

Published per person (aggregates only), next to the daily row in the same shared clone and the same commit:
  reports/usage/<person>.week.json = {version, person, week (ISO), start, end, fx_rate, done, total, projects}
    total / projects[<repo>] = {tok: {in, out, cr, cw5, cw1h}, usd, tl_api, tl_subscription, quota_percent}
  No job names, no conversation text, no file or session paths (job names may carry customer names): only the repo name of the project.
  tl_api = API-list-price equivalent in TL; tl_subscription = the share of the weekly subscription fee the usage used (weekly quota % x weekly
  plan price), None when the quota calibration (ratio.json) is missing; quota_percent = the weekly quota percent the usage consumed.

Cost numbers come from tools/cost_report.py (one code path: transcript scan, price table, USD->TL rate, quota calibration, project
attribution), imported from the clone. A copy of this plugin without the clone's tools/ publishes nothing (logged), the daily row is unaffected.

Finished work ("done") = issues with the person as assignee whose board Status was SET to Done during the ISO week (Mon-Sun, local time).
Source: tools/gh_board.list_items (GraphQL Projects, 100 items per request, board ids from ~/.claude/gh-board-ids.json), the Status value's
own `updatedAt` (the moment the Status was last set, so moving a Done card around other fields does not count). Boards: every product board
with a Done option and the org's shared test-queue project; the boards in the config key COST_SEPARATE_BOARDS (project numbers, e.g. a management mirror or a personal scratch board) are skipped; one
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

SEPARATE_BOARDS = tuple(x for x in os.environ.get("COST_SEPARATE_BOARDS", "").replace(",", " ").split() if x)   # config key: boards never counted
CACHE_HOUR = 6
FILE_SUFFIX = ".week.json"
VERSION = 1
KEY_USD_STEP = 100       # change-only: a person's weekly dollars count as changed per this step
UNKNOWN = "unknown"
MAIN_SESSION = "main-session"    # main (coordinator) sessions that started no sub-agent job: their cost has no project to be split over
TOKENS = ("in", "out", "cr", "cw5", "cw1h")
_PLUGIN_BIN = os.path.dirname(os.path.abspath(__file__))


def week_cap(today):
    """(ISO week id, Monday, Sunday) of a date (YYYY-MM-DD or date)."""
    d = datetime.date.fromisoformat(today) if isinstance(today, str) else today
    y, w, _ = d.isocalendar()
    start = d - datetime.timedelta(days=d.weekday())
    return "%d-W%02d" % (y, w), start, start + datetime.timedelta(days=6)


def tools_dir():
    """tools/ of the clone this plugin lives in (plugins/quota-pace/bin -> repo root); None in a copy without it."""
    d = os.path.normpath(os.path.join(_PLUGIN_BIN, "..", "..", "..", "tools"))
    return d if os.path.isfile(os.path.join(d, "cost_report.py")) and os.path.isfile(os.path.join(d, "gh_board.py")) else None


def _tools_module(name):
    d = tools_dir()
    if not d:
        raise RuntimeError("no tools/ (a copy outside the clone)")
    if d not in sys.path:
        sys.path.insert(0, d)
    return importlib.import_module(name)


# ------------------------------------------------------------------ cost aggregation (pure given the cost_report result)
def _empty():
    return {"tok": {k: 0 for k in TOKENS}, "usd": 0.0, "tl_api": 0.0, "tl_subscription": 0.0, "quota_percent": 0.0,
            "_tl": False, "_sub": False, "_quota": False}


def _add(h, T):
    """Add the usage record T (a job or session of cost_report.conversion) to the bucket h."""
    for k in TOKENS:
        h["tok"][k] += int(T.get(k, 0))
    h["usd"] += T.get("usd_total", 0.0)
    for key, field, flag in (("tl_api", "tl", "_tl"), ("tl_subscription", "quota_share_tl", "_sub"), ("quota_percent", "week_percent", "_quota")):
        if T.get(field) is not None:
            h[key] += T[field]
            h[flag] = True


def _finish(h):
    out = {"tok": h["tok"], "usd": round(h["usd"], 2)}
    for key, flag in (("tl_api", "_tl"), ("tl_subscription", "_sub"), ("quota_percent", "_quota")):
        out[key] = round(h[key], 2) if h[flag] else None
    return out


def build_summary(R, person, today, done=None, fx_rate=None):
    """The publishable weekly summary from a cost_report.conversion(...) result (built with distribute_main=True, so a job's cost includes
    its share of the main session that started it). Only the repo name of each project survives: job names (R[...]['jobs'][*]['is']),
    ids, session ids and project folders are never copied. Sessions that started no job go to MAIN_SESSION."""
    wid, start, end = week_cap(today)
    projects, total, job_session = {}, _empty(), {}
    for g in R.get("projects", []):
        for j in g["jobs"]:
            job_session[j.get("session")] = job_session.get(j.get("session"), 0) + 1
    session = {S.get("session"): S for S in R.get("sessions", [])}
    for g in R.get("projects", []):
        h = projects.setdefault(g["project"] or UNKNOWN, _empty())
        for j in g["jobs"]:
            _add(h, j)
            _add(total, j)
            S = session.get(j.get("session"))
            if S and job_session.get(j.get("session")):   # main-session tokens split over the jobs like its dollars (equal share)
                share = 1.0 / job_session[j["session"]]
                for k in TOKENS:
                    h["tok"][k] += int(round(S.get(k, 0) * share))
                    total["tok"][k] += int(round(S.get(k, 0) * share))
    for sid, S in session.items():
        if job_session.get(sid):
            continue
        for h in (projects.setdefault(MAIN_SESSION, _empty()), total):
            _add(h, S)
    return {"version": VERSION, "person": person, "week": wid, "start": start.isoformat(), "end": end.isoformat(),
            "fx_rate": round(R["fx_rate"], 4) if R.get("fx_rate") else None, "done": done,
            "total": _finish(total), "projects": {p: _finish(h) for p, h in sorted(projects.items(), key=lambda kv: -kv[1]["usd"])}}


def week_summary(person, today):
    """This week's summary of the local transcripts, or None (logged by the caller) when the cost report cannot run."""
    from types import SimpleNamespace
    try:
        MR = _tools_module("cost_report")
        a = SimpleNamespace(week="", day=None, project=None, distribute_main=True, json=True, most=0)
        start, end, label, jobs, sessions = MR.install_data(a)
        return build_summary(MR.conversion(jobs, sessions, a), person, today)
    except (Exception, SystemExit) as e:
        _log("no weekly summary: %s: %s" % (type(e).__name__, e))
        return None


def _log(m):
    try:
        import cost_share
        cost_share.write_log(m)
    except Exception:
        pass


# ------------------------------------------------------------------ finished work (board Status set to Done this week)
def compute_done(items, login, today, tz=None):
    """Number of distinct issues in `items` (gh_board.list_items dicts of the Done column) assigned to `login` whose Status was set
    during the ISO week of `today`. status_at is UTC; the week is judged in `tz` (local time by default)."""
    _, start, end = week_cap(today)
    seen = set()
    for o in items:
        if login.lower() not in [x.lower() for x in o.get("assignees", [])] or not o.get("status_at"):
            continue
        try:
            t = datetime.datetime.fromisoformat(o["status_at"].replace("Z", "+00:00")).astimezone(tz).date()
        except ValueError:
            continue
        if start <= t <= end:
            seen.add(o.get("url") or "%s#%s" % (o.get("repo"), o.get("number")))
    return len(seen)


def _cache_path():
    import cost
    return os.path.join(cost.DIR, "done-cache.json")


def done_count(login, today, now=None):
    """Finished-work count of `login` for the week of `today` (None = unknown: no gh, no budget, API error). Cached CACHE_HOUR hours."""
    now = time.time() if now is None else now
    wid = week_cap(today)[0]
    key = "%s|%s" % (login.lower(), wid)
    path = _cache_path()
    try:
        with open(path, encoding="utf-8") as f:
            ob = json.load(f)
        if not isinstance(ob, dict):
            ob = {}
    except (OSError, ValueError):
        ob = {}
    e = ob.get(key)
    if isinstance(e, dict) and isinstance(e.get("n"), int) and 0 <= now - float(e.get("t", 0)) < CACHE_HOUR * 3600:
        return e["n"]
    try:
        GB = _tools_module("gh_board")
        ids = GB.load_ids()
        g, _ = GB.rates()
        if g["remaining"] < GB.MIN_GQL:
            raise RuntimeError("GraphQL budget low (%s)" % g["remaining"])
        items = []
        for k, b in sorted(ids["boards"].items(), key=lambda kv: int(kv[0])):
            if k in SEPARATE_BOARDS or not any(GB.norm_name(o) == "done" for o in b["fields"].get("Status", {}).get("options", {})):
                continue
            items += GB.list_items(ids, k, "Done")
        import cost_core as CK
        n = compute_done(items, login, today, CK.TZ)
    except (Exception, SystemExit) as ex:
        _log("no finished count: %s: %s" % (type(ex).__name__, str(ex)[:120]))
        return None
    ob = {k: v for k, v in ob.items() if k.endswith("|" + wid)}
    ob[key] = {"n": n, "t": now}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ob, f)
    except OSError:
        pass
    return n


# ------------------------------------------------------------------ the published file, change key, leaderboard text
def file_path(clone_report_dir, file_stem):
    return os.path.join(clone_report_dir, file_stem + FILE_SUFFIX)


def summary_write_text(summary):
    return json.dumps(summary, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def read_summary(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("version") == VERSION and isinstance(d.get("total"), dict) else None


def efficiency_key(summaries, today):
    """Change key of the weekly summaries: (name, finished count, weekly dollars in KEY_USD_STEP steps) of the summaries of the
    current week, sorted. Timestamps and small dollar drift never change it (change-only commits)."""
    wid = week_cap(today)[0]
    return sorted((name, o.get("done"), int((o["total"].get("usd") or 0) // KEY_USD_STEP))
                  for name, o in summaries.items() if o and o.get("week") == wid)


def tl_text(x):
    return "₺" + "{:,.0f}".format(x) if x is not None else "—"


def work_per(tl, done):
    """TL per finished item; None when nothing finished (shown as an em dash) or the TL is unknown."""
    return tl / done if tl is not None and done else None


def efficiency_rank(summaries, today):
    """[(name, TL per finished item)] lowest first, current-week summaries with at least 1 finished item and a known API-equivalent TL."""
    wid = week_cap(today)[0]
    s = [(name, work_per(o["total"].get("tl_api"), o.get("done"))) for name, o in summaries.items()
         if o and o.get("week") == wid and o.get("done")]
    return sorted(((name, v) for name, v in s if v is not None), key=lambda x: (x[1], x[0]))


def project_distribution(o, most=3):
    p = [(name, v.get("usd") or 0.0) for name, v in (o.get("projects") or {}).items()]
    top = sum(u for _, u in p)
    if top <= 0:
        return "—"
    p.sort(key=lambda x: -x[1])
    s = " · ".join("%s %d%%" % (name, round(100 * u / top)) for name, u in p[:most])
    return s + (" · +%d" % (len(p) - most) if len(p) > most else "")


def table_lines(order_names, summaries, today, medal):
    """Markdown lines (no trailing blank) of the finished-work section; order_names = people in the existing ranking order."""
    wid = week_cap(today)[0]
    L = ["### Finished work and its cost (%s)" % wid, "",
         "| # | Person | Weekly % | Finished (✅ Done) | ₺ API equivalent | ₺ subscription share | ₺ / finished item (API) | ₺ / finished item (subscription) | Project split |",
         "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for i, name in enumerate(order_names, 1):
        o = summaries.get(name)
        if not o or o.get("week") != wid:
            L.append("| %s | %s | — | — | — | — | — | — | — |" % (medal.get(i, str(i)), name))
            continue
        t, b = o["total"], o.get("done")
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            medal.get(i, str(i)), name, "%d%%" % round(t["quota_percent"]) if t.get("quota_percent") is not None else "—",
            b if b is not None else "—", tl_text(t.get("tl_api")), tl_text(t.get("tl_subscription")),
            tl_text(work_per(t.get("tl_api"), b)), tl_text(work_per(t.get("tl_subscription"), b)), project_distribution(o)))
    vs = efficiency_rank(summaries, today)
    L += ["", "**Efficiency (lowest ₺ / finished item, API equivalent; at least 1 item):** " + (
        " · ".join("%s %s %s/item" % (medal.get(i, "%d." % i), name, tl_text(v)) for i, (name, v) in enumerate(vs, 1))
        if vs else "_no finished work this week_"), "",
         "Finished = issues assigned to the person whose board Status was set to ✅ Done during the week (Mon-Sun) (product boards + the shared test queue; an issue counts once). "
         "₺ API equivalent = API list price × the TCMB exchange rate; ₺ subscription share = weekly quota % × the weekly plan fee. The ranking rule is unchanged (7-day $); "
         "efficiency is a separate line, lower is better. The project split is the dollar share; job names and conversation content are never shared."]
    return L

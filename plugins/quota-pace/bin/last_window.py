#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""last-window: use ALL leftover weekly Claude quota in the last hours before the reset (opt-in, runs from a timer).

Plain Python, stdlib only. The wrapper itself spends no tokens (the one-shot Haiku relay that delivers the assignment costs a few cents);
the work is done by a normal, visible, interactive Claude Code session. Commands:

  last-window trigger [--dry-run]   the timer entry (every 15 min). Gate: target_at <= now < week_resets_at, weekly use < 100%,
                                  no assignment already running (lock), not restarted within 15 min. Then, in the foreground:
                                  1. pick one LIVE interactive session of the owner (highest model first: Opus > Sonnet > Fable > Haiku,
                                     newer model version first, then the oldest session start; headless runs, last-window's own sessions
                                     and names in LAST_WINDOW_EXCLUDING / <state dir>/excluding.txt are skipped) and give it the assignment
                                     with a one-shot headless Haiku relay (ListAgents + SendMessage), the full brief being a file;
                                     The relay's HANDOFF binds the run to that session (no second delivery); the next candidate only
                                     on a clear MISSING or a relay that could not run; a timed-out relay ends the hand-out for this run;
                                  2. no live session took it: start the fallback `last-window-<person>`: interactive claude in a
                                     detached tmux session on last-window's own server (`tmux -L last-window`; Windows: a Windows Terminal tab);
                                  3. follow the session's registry entry and transcript (status, turns), end the run at the reset (stop
                                     message / `/exit` + kill for our own session), record the run.
                                  --dry-run: print the gate decision, the ranked sessions and the picked items, send/start nothing.
  last-window session              print the live sessions, who would get the assignment and why the others are excluded
  last-window attempt              test of the delivery/follow/cutoff path with a trivial prompt in a scratch state dir (see --help)
  last-window choice               print the picked items (a: label, b: board "AI queue", c: standing-list signals), start nothing
  last-window status [--last N]     latest runs of EVERYONE from the shared log (claude-shared reports/last-window/)
  last-window label [--dry]     create the label ai-last-window in the product repos (idempotent)
  last-window burn --on|--off|status
                                  burn mode: the same assignment, work order and never-list as the last window, but
                                  outside the window, until the weekly limit is used up, the weekly reset (auto-off) or `--off`. Only a
                                  developer (people.json `role: developer`, checked by the gh login) may switch it on or off; state in burn.json.

Modes of `trigger` (one gate, `decision(mode=...)`; the timer passes `--modes`, default `window` for timers installed before the option):
  window   the last window (above).                       burn   burn mode, while switched on (any install with the timer).
  resume   the resume alarm (install --resume-alarm): once per 5-hour reset that stopped work (pace.json five_hour_used >= 100, the reset
           passed), every live terminal session whose last assistant entry is still the 429 "session limit" stop RESUME_DELAY after the
           reset gets one "continue where you stopped" relay. Claude Code's own autoContinueAtUsageLimit resumes an open session by itself;
           this covers what it does not (Esc, three continuations used, sleep over 30 min). No new session, no new items. pace.json without
           a 5-hour reading or stale: the stops are found from the transcripts alone (the reset is at most 5 h after the stop).

Quota data: pace.json (written by the status line of any interactive session, including the one that does the work) and
~/.claude/last-window/status.json. The coordinator prompt is last-window-prompt.md (next to this file), rendered per run into
runs/<ts>-brief.md. Files, all under <config dir>/last-window/:
  <date>.md      the run journal (the coordinator appends DONE/WIP/SKIP/NOTE lines; the wrapper adds a header and a footer)
  runs/*-brief.md  the brief of each run (kept 14 days)     coordinator.lock   pid lock + the session of the run     status.json   quota, last start
Shared log: reports/last-window/<person>.md (one row per run; the Items cell starts with `session:<name>`) and LOG.md (all people,
newest first) in claude-shared main, pushed through its own clone (~/.cache/claude-shared-last-window) with the same ownership rules as
the usage ranking (the name must be listed in people.json; only your own file is written).
Env: LAST_WINDOW_CLAUDE (claude executable), LAST_WINDOW_MODEL / LAST_WINDOW_ARGS (extra `claude` args of the fallback session),
LAST_WINDOW_RC (0 = no Remote Control for the fallback session; default on), LAST_WINDOW_MAX_TURNS (default 200), LAST_WINDOW_IDLE_SEC (idle seconds that end a run, 600),
LAST_WINDOW_ACK_SEC (an assigned session idle this long without the assignment ends the run, 300), LAST_WINDOW_ROLE_SEC (relay timeout, 300),
LAST_WINDOW_RESET_ATTEMPT (relays of the "reset reached" message, 3), LAST_WINDOW_EXCLUDING (comma list of session-name patterns to skip),
PACE_TARGET_HOURS (target hours before the reset, default as the forecast), CLAUDE_CONFIG_DIR, CLAUDE_SHARED_PERSON/REMOTE,
LAST_WINDOW_CHOICE (a JSON file replacing the live pick).
Never fails loudly in `trigger`: errors go to <config dir>/last-window/last-window.log and the exit code stays 0.
"""
import argparse
import datetime
import fnmatch
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # python -I does not add the script directory
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", "tools"))  # repo layout: tools/hidden.py (a --copy install ships it next to this file)
from hidden_loader import hidden  # noqa: E402  (one hidden-launch helper)

HOME = os.path.expanduser("~")
CLAUDE = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
DIR = os.environ.get("LAST_WINDOW_DIR") or os.path.join(CLAUDE, "last-window")   # LAST_WINDOW_DIR: tests / `attempt` only
LOCK = os.path.join(DIR, "coordinator.lock")
STATUS = os.path.join(DIR, "status.json")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT = os.path.join(SCRIPT_DIR, "last-window-prompt.md")

LABEL = "ai-last-window"
APPROVAL_LABEL = "prod-approval-waiting"
LABEL_COLOR = "5319e7"
LABEL_DESCRIPTION = "Pre-approved for the AI last-window run (the last hours of the weekly quota): dev-only, no human approval needed"
DEFAULT_TARGET_HOUR = 5.0
RETRY_INTERVAL = 15 * 60       # a coordinator is restarted at most this often
FAST_ERROR_SEC = 120           # a run shorter than this that failed counts as a quick failure
FAST_ERROR_LIMIT = 3          # this many in a row: back off for ERROR_WAITING
ERROR_WAITING = 3600
RESET_LAST_MINUTES = 10 * 60    # no restart in the last 10 minutes before the reset
TARGET_MAX_HOUR = 96.0       # the longest window ever (pace_forecast.ABS_MAX_TARGET_HOURS, the hard ceiling of target hours before the reset)
DEFAULT_MAX_HOUR = 72.0  # the default cap of the target hours; "max_target_hours" in pace-plan.json moves it up to TARGET_MAX_HOUR (as in pace_forecast.py)
LOCK_MAX_AGE = (TARGET_MAX_HOUR + 1) * 3600   # a lock older than the longest window is stale even if the pid number is alive (pid reuse)
MODES = ("window", "burn", "resume")
FIVE_HOUR = 5 * 3600
RESUME_DELAY = 10 * 60       # the resume alarm waits this long after the 5-hour reset (Claude Code's own auto-continue goes first)
RESUME_MAX_AGE = 12 * 3600   # a limit stop older than this is not resumed by the transcript-only fallback (a session left open for days)
RESUME_MAX_SESSION = 5        # at most this many sessions get the resume relay per reset
BURN_IDLE_WAIT = 3600      # burn mode: a run that ended with nothing done or in progress holds the next start this long
REPO_DIR = "reports/last-window"
LOG = "LOG.md"
LINE_CAP = 200
TIME = time.time             # replaced by tests
POLL = float(os.environ.get("LAST_WINDOW_POLL") or 10)

_CHECK = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# ------------------------------------------------------------------ small helpers

def org():
    """The GitHub org (config key GH_ORG / ~/.config/claude-shared/org, the same key as the ranking); "" = not configured."""
    import cost_share as MP
    return MP.github_org()


def product_repos():
    """Product repos = the repos linked to the project boards that have a "Yapay Zeka" Status (ops and infra repos are not products).
    Config key LAST_WINDOW_REPOS / ~/.config/claude-shared/product-repos: repo names separated by commas or spaces; default none."""
    import cost_share as MP
    return tuple(r for r in re.split(r"[,\s]+", MP.setting_value("product-repos", "LAST_WINDOW_REPOS")) if r)



def status_channel():
    """(name, id) of the team's AI status channel: config keys LAST_WINDOW_CHANNEL (default #ai-status) and LAST_WINDOW_CHANNEL_ID
    (~/.config/claude-shared/channel, channel-id; no default id: the coordinator then finds the channel by name)."""
    import cost_share as MP
    name = MP.setting_value("channel", "LAST_WINDOW_CHANNEL", "#ai-status")
    return name, MP.setting_value("channel-id", "LAST_WINDOW_CHANNEL_ID") or "look it up by name"


def write_log(message):
    try:
        os.makedirs(DIR, exist_ok=True)
        path = os.path.join(DIR, "last-window.log")
        try:
            if os.path.getsize(path) > 200_000:
                with open(path, encoding="utf-8", errors="replace") as f:
                    last = f.readlines()[-300:]
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(last)
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(TIME())), _CHECK.sub(" ", str(message))[:600]))
    except Exception:
        pass


def read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = "%s.%d.tmp" % (path, os.getpid())
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(temp, path)


def local_time(t, seconds=False):
    return time.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M", time.localtime(t))


def count(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ quota: window, readings, gate
def target_hour(pace):
    """Hours before the reset where the window starts: the forecast's own setting (PACE_TARGET_HOURS env, pace-plan.json), else what
    pace.json recorded, else 5. 0 means "no window" (the old behaviour of the forecast): the mode is then off."""
    plan = read_json(os.path.join(CLAUDE, "pace-plan.json"))
    v = count(os.environ.get("PACE_TARGET_HOURS"))
    if v is None:
        v = count(plan.get("target_hours"))
    if v is None:
        v = count(pace.get("target_hours"))
    if v is None:
        v = DEFAULT_TARGET_HOUR
    max_ = count(plan.get("max_target_hours"))
    max_ = DEFAULT_MAX_HOUR if max_ is None else min(max(max_, 0.0), TARGET_MAX_HOUR)
    return min(max(v, 0.0), max_)


def quota_status(pace, status, now):
    """The best knowledge of the weekly quota now: {week_used (%, None = unknown), week_resets_at, five_hour_used,
    five_hour_resets_at, source}. The source with the newer `updated` wins (the status line writes pace.json while someone works
    interactively, the wrapper writes status.json while it runs). A week_used is only trusted when its reset is the chosen reset.
    No reset in the future anywhere: the last known one is rolled forward by whole weeks (the week length is fixed), unknown use."""
    sources = []
    for name, d in (("pace", pace), ("status", status)):
        r, u = count(d.get("week_resets_at")), count(d.get("updated"))
        if r:
            sources.append((u or 0, name, d, r))
    sources.sort(key=lambda k: k[0], reverse=True)
    future = [k for k in sources if k[3] > now]
    if future:
        _, name, d, reset = future[0]
        usage = count(d.get("week_used"))
        fresh = True
    elif sources:
        reset, name, usage, fresh = sources[0][3], "forecast", None, False
        while reset <= now:
            reset += 7 * 86400
    else:
        return {"week_used": None, "week_resets_at": None, "five_hour_used": None, "five_hour_resets_at": None, "source": "missing"}
    picks = d if future and count(d.get("five_hour_resets_at")) else status   # the status line (pace.json) carries the 5-hour window too
    five_end, five_ratio = count(picks.get("five_hour_resets_at")), count(picks.get("five_hour_used"))
    return {"week_used": usage if fresh else None, "week_resets_at": reset, "source": name,
            "five_hour_used": five_ratio if five_end and five_end > now else None,
            "five_hour_resets_at": five_end if five_end and five_end > now else None}


def decision(now, quota, target, lock_live, last_begin, fast_error=0, mode="window", wait_idle=None):
    """Pure gate: ("start" | "wait", reason code, detail). Order matters: window first, then the quota, then the process state.
    `mode`: "window" (the last window), "burn" (burn mode: no window, the caller checked it is switched on) or "resume" (the resume
    alarm: no window, no restart spacing, the caller checked a reset is due; an unknown weekly reset does not block it)."""
    reset = quota.get("week_resets_at")
    if not reset and mode != "resume":
        return "wait", "reset-unknown", "no weekly reset time known (pace.json and status.json are empty)"
    if mode == "window":
        if target <= 0:
            return "wait", "window-missing", "PACE_TARGET_HOURS is 0: the forecast has no target window, so there is no last window"
        window_head = reset - target * 3600
        if now < window_head:
            return "wait", "before-window", "window opens %s (reset %s - %gh)" % (local_time(window_head), local_time(reset), target)
    if reset and now >= reset:
        return "wait", "after-reset", "reset %s has passed" % local_time(reset)
    if mode != "resume" and reset - now <= RESET_LAST_MINUTES and last_begin:
        return "wait", "reset-near", "less than 10 min to the reset: no restart"
    usage = quota.get("week_used")
    if usage is not None and usage >= 100:
        return "wait", "quota-done", "weekly limit is used up (%g%%); nothing left until the reset" % usage
    if quota.get("five_hour_used") is not None and quota["five_hour_used"] >= 100 and quota.get("five_hour_resets_at"):
        return "wait", "five-hour-full", "5-hour window is full until %s" % local_time(quota["five_hour_resets_at"])
    if lock_live:
        return "wait", "running", "a coordinator is already running (lock)"
    if mode == "resume":    # a relay per stopped session: the coordinator's failure back-off and restart spacing do not apply
        return "start", "resume", "the 5-hour window that stopped work has reset"
    if fast_error >= FAST_ERROR_LIMIT and last_begin and now - last_begin < ERROR_WAITING:
        return "wait", "error-backoff", "%d quick failures in a row: backing off 1 h" % fast_error
    if mode == "burn" and wait_idle and now < wait_idle:
        return "wait", "idle-wait", "the last burn-mode run found nothing to do: next start after %s" % local_time(wait_idle)
    if last_begin and now - last_begin < RETRY_INTERVAL:
        return "wait", "retry-interval", "last start %s: at most one start per 15 min" % local_time(last_begin)
    if mode == "burn":
        return "start", "burn-is-open", "burn mode is on until the weekly reset %s" % local_time(reset)
    return "start", "in-window", "inside the window %s .. %s" % (local_time(window_head), local_time(reset))


def select_mode(now, quota, target, lock_live, last_begin, fast_error, modes, burn_is_open, resume_ready, wait_idle=None):
    """The gate of every mode of this tick: (chosen (mode, action, reason, detail) | None, all results). Precedence: the last window, then
    burn mode, then the resume alarm. A mode that is not installed (modes), not switched on or not due is not asked."""
    results = []
    for mode, present in (("window", "window" in modes), ("burn", burn_is_open), ("resume", resume_ready)):
        if present:
            results.append((mode,) + decision(now, quota, target, lock_live and mode != "resume", last_begin, fast_error, mode, wait_idle))
    return next((s for s in results if s[1] == "start"), None), results


# ------------------------------------------------------------------ burn mode: state and the developer check
def burn_path():
    return os.path.join(DIR, "burn.json")


def read_burn():
    return read_json(burn_path())


def burn_is_open(now, b=None):
    """Switched on and its weekly reset (stored at activation, never the rolled-forward estimate) not reached."""
    b = read_burn() if b is None else b
    return bool(b.get("active")) and (count(b.get("week_reset")) or 0) > now


def burn_duration_full(now):
    """Auto-off: a burn mode whose weekly reset passed is switched off (recorded). True when it did that now."""
    b = read_burn()
    if not b.get("active") or (count(b.get("week_reset")) or 0) > now:
        return False
    b.update(active=False, closer="automatic (weekly reset)", closing=int(now))
    write_json(burn_path(), b)
    write_log("burn mode switched off: the weekly reset %s passed" % local_time(count(b.get("week_reset")) or now))
    return True


def person_role(login):
    """(display name, role) of a GitHub login from people.json: the clone this script runs from, else last-window's own clone
    (refreshed from origin). (None, None) when the login is not listed or nothing is readable."""
    import cost_share as MP
    if not login:
        return None, None
    for clone in (os.path.normpath(os.path.join(SCRIPT_DIR, "..", "..", "..")), None):
        if clone is None:
            clone = clone_path()
            try:
                MP.prepare_clone(clone, MP.remote_address())
            except Exception as e:
                write_log("people.json clone: %s" % e)
        record = MP.people_json(clone)
        if record:
            return record.get(login.lower(), (None, None))
    return None, None


# ------------------------------------------------------------------ resume alarm: the 5-hour reset and the sessions it stopped
LIMIT_RE = re.compile(r"session limit|5-hour limit|usage limit", re.I)
LIMIT_OUTSIDE_RE = re.compile(r"spend limit|weekly limit|monthly", re.I)   # a 5-hour reset does not lift these
LIMIT_TAIL = 64_000


def iso_epoch(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def limit_halt(path):
    """The 5-hour limit stop that ended this transcript, or None: the LAST assistant entry is Claude Code's synthetic 429
    (`error: "rate_limit"`, text "You've hit your session limit · resets 6:30pm"; seen in our transcripts since 16.10.2026).
    Spend and weekly limits are not 5-hour stops. Returns {t (epoch), uuid, text}."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - LIMIT_TAIL))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    last = None
    for line in data.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("type") == "assistant":
            last = e
    if not last or last.get("error") != "rate_limit":
        return None
    content = (last.get("message") or {}).get("content") or []
    text_ = " ".join(c.get("text") or "" for c in content if isinstance(c, dict)) if isinstance(content, list) else str(content)
    t = iso_epoch(last.get("timestamp"))
    if t is None or not LIMIT_RE.search(text_) or LIMIT_OUTSIDE_RE.search(text_):
        return None
    return {"t": t, "uuid": last.get("uuid") or last.get("timestamp"), "text": text_[:160]}


def resume_path():
    return os.path.join(DIR, "resume.json")


def read_resume():
    """The resume alarm's own state {key: the last handled 5-hour reset, warned: relayed stop uuids}: its own file (and lock), so it
    works while a coordinator run holds coordinator.lock and rewrites status.json from memory."""
    return read_json(resume_path())


def get_resume_lock(now):
    """The resume alarm's own lock (relays take at most RESUME_MAX_SESSION x the relay timeout); a lock older than 1 h is stale."""
    path = os.path.join(DIR, "resume.lock")
    os.makedirs(DIR, exist_ok=True)
    for _ in range(2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return path
        except FileExistsError:
            try:
                if now - os.path.getmtime(path) < 3600:
                    return None
                os.remove(path)
            except OSError:
                return None
    return None


def resume_status(pace, res_state, now):
    """Is a resume due? {ready, key, reason, detail, r5}. ready True: pace.json shows a used-up 5-hour window (five_hour_used >= 100)
    whose reset passed RESUME_DELAY ago and is not handled yet (key = that resets_at, recorded once). ready False: that window is
    still full (or inside the grace): everything waits for it. ready None, every other case (pace.json missing or stale, or already
    showing a newer window because a session went on): the transcripts decide (limit_stopped), each stop uuid at most once."""
    u5, r5 = count(pace.get("five_hour_used")), count(pace.get("five_hour_resets_at"))
    if r5 and u5 is not None and u5 >= 100 and now < r5 + FIVE_HOUR:
        if now < r5 + RESUME_DELAY:
            return {"ready": False, "key": None, "r5": r5, "reason": "resume-waiting",
                    "detail": "5-hour window full, resets %s; resume check at %s" % (local_time(r5), local_time(r5 + RESUME_DELAY))}
        if res_state.get("key") != int(r5):
            return {"ready": True, "key": int(r5), "r5": r5, "reason": "resume", "detail": "the 5-hour window reset at %s" % local_time(r5)}
    return {"ready": None, "key": None, "r5": r5, "reason": "resume-sessions",
            "detail": "the limit stops are read from the transcripts"}


def halt_reset(t, r5):
    """When the 5-hour window of a stop at `t` resets: pace.json's r5 when the stop lies in that window; at the latest the start of a
    newer window pace.json already shows (r5 - 5 h); otherwise at most 5 h after the stop."""
    if r5 and r5 - FIVE_HOUR <= t < r5:
        return r5
    if r5 and t < r5 - FIVE_HOUR:
        return min(t + FIVE_HOUR, r5 - FIVE_HOUR)
    return t + FIVE_HOUR


def limit_stopped(sessions, r5, now, warned=(), skip=(), all_=False):
    """Live terminal sessions still stopped by a 5-hour limit whose reset (+ RESUME_DELAY) has passed: idle, last assistant entry the
    429, not relayed for that stop yet (`warned` = stop uuids), not in `skip` (names). Stops older than RESUME_MAX_AGE are left
    alone. all_=True (dry run) lists the stops whose reset has not come yet too. Reads only the registry and a 64 KB tail per idle session."""
    out, excluding = [], excluding_patterns()
    for o in sessions:
        if dropping_reason(o, excluding) or o["name"] in skip or o["status"] != "idle":
            continue
        d = limit_halt(o["path"])
        if not d or d["uuid"] in warned or now - d["t"] > RESUME_MAX_AGE:
            continue
        reset = halt_reset(d["t"], r5)
        if all_ or now >= reset + RESUME_DELAY:
            out.append(dict(o, halt=d, reset=reset))
    return sorted(out, key=lambda o: o["started"])[:RESUME_MAX_SESSION]


def resume_message(o):
    return ("[last-window on %s] resume alarm, automatic, not from a person: the 5-hour usage window that stopped this session "
            "at %s has reset (%s). Continue the work this session was doing when the limit stopped it, from where it stopped: check `git "
            "status`, your task list and your agents' hand-off notes first; commit and push WIP after every step. Only already-started work: "
            "pick nothing new. If that work is finished, stop. Do not reply to this message (the sender is a one-shot relay that has exited)." % (
                machine_name(), local_time(o["halt"]["t"]), local_time(o["reset"])))


def wake_resume(stopped, res_state):
    """Relay the resume message to every stopped session once; record each stop (uuid) in resume.json before the relay, so a crash or a
    slow relay never sends it twice. Returns [(name, relay answer)]."""
    warned = res_state.setdefault("warned", [])
    result = []
    for o in stopped:
        warned.append(o["halt"]["uuid"])
        del warned[:-50]
        write_json(resume_path(), res_state)
        answer = forward(o["name"], resume_message(o))
        write_log("resume alarm: '%s' (stopped %s) -> %s" % (o["name"], local_time(o["halt"]["t"]), answer))
        result.append((o["name"], answer))
    return result


# ------------------------------------------------------------------ process helpers (lock, kill)
class Stopped(BaseException):
    """SIGTERM/SIGINT reached the wrapper (systemctl stop, logout): stop the coordinator, still record the run and free the lock."""


def _stop_handler(signum, frame):
    raise Stopped()


def _install_handlers():
    """SIGTERM/SIGINT/SIGHUP raise Stopped, so the run still stops its own session, records itself and frees the lock."""
    for sig in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), _stop_handler)


def pid_live(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return bool(ok) and code.value == 259    # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_lock():
    return read_json(LOCK)


def lock_live(now):
    k = read_lock()
    if not k:
        return False
    return pid_live(int(k.get("pid") or 0)) and now - (count(k.get("t")) or 0) < LOCK_MAX_AGE


def get_lock(now):
    os.makedirs(DIR, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if lock_live(now):
                return False
            try:
                os.remove(LOCK)
            except OSError:
                return False
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "t": int(now)}, f)
        return True
    return False


def update_lock(**fields):
    """Add fields (the session that took the assignment) to our own lock."""
    k = read_lock()
    if k.get("pid") == os.getpid():
        k.update(fields)
        write_json(LOCK, k)


def lock_release():
    try:
        if read_lock().get("pid") == os.getpid():
            os.remove(LOCK)
    except OSError:
        pass


# ------------------------------------------------------------------ the pick (scripted: gh REST + tools/gh_board.py)
def gh(*args, timeout=120):
    """(parsed JSON | None, error text). Only `gh api` and read-only calls are made here."""
    try:
        r = hidden.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, type(e).__name__
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip().replace("\n", " ")[:200]
    try:
        return json.loads(r.stdout), ""
    except ValueError:
        return None, "bad JSON from gh"


def gh_login():
    v = os.environ.get("LAST_WINDOW_GH_LOGIN")
    if v:
        return v
    out = hidden.run(["gh", "api", "user", "-q", ".login"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    return out.stdout.strip() if out.returncode == 0 else ""


def tools_dir(clone=None):
    """Directory of the kit's tools (gh_board.py): the clone this plugin sits in (a --repodan install, or a
    run from the repo), else the job's own clone refreshed from origin."""
    candidate = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "..", "..", "tools"))
    if os.path.isfile(os.path.join(candidate, "gh_board.py")):
        return candidate
    import cost_share as MP
    clone = clone or clone_path()
    MP.prepare_clone(clone, MP.remote_address())
    return os.path.join(clone, "tools")


def suitable(it, login):
    """Open, not waiting for a production approval, unassigned or assigned to this user."""
    if it.get("state", "open") != "open" or APPROVAL_LABEL in it.get("labels", []):
        return False
    assigned = [a.lower() for a in it.get("assignees", [])]
    return not assigned or login.lower() in assigned


def pick_a(login):
    """Issues with the label ai-last-window across the org (one REST search call)."""
    if not org():
        return [], "label search: GH_ORG is not configured (README, Configuration)"
    q = "org:%s label:%s is:issue is:open" % (org(), LABEL)
    data, error = gh("api", "-X", "GET", "search/issues", "-f", "q=" + q, "-f", "per_page=100")
    if data is None:
        return [], "label search: " + error
    out = []
    for i in data.get("items", []):
        repo = i["repository_url"].rsplit("/", 1)[-1]
        it = {"repo": repo, "number": i["number"], "title": i["title"], "url": i["html_url"], "state": i["state"],
              "labels": [l["name"] for l in i.get("labels", [])], "assignees": [a["login"] for a in i.get("assignees", [])]}
        if suitable(it, login):
            out.append(it)
    return sorted(out, key=lambda x: (x["repo"], x["number"])), ""


def board_items(status_name, tools):
    try:
        r = hidden.run([sys.executable, os.path.join(tools, "gh_board.py"), "items", "--status", status_name],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [], "gh_board items: " + type(e).__name__
    if r.returncode != 0:
        return [], "gh_board items: " + (r.stderr or r.stdout).strip().replace("\n", " ")[:200]
    try:
        return json.loads(r.stdout), ""
    except ValueError:
        return [], "gh_board items: bad JSON"


def pick_b(login, items, excluded=()):
    """Board items in "AI queue": highest board priority first (rank 0 = most urgent; none = last), then oldest issue first."""
    excluded_urls = set(excluded)
    candidates = [i for i in items if suitable(i, login) and i["url"] not in excluded_urls]
    candidates.sort(key=lambda i: (i.get("rank", 99), i["number"]))
    for i in candidates:
        i["repo"] = i["repo"].split("/")[-1]
    return candidates


def pick_c():
    """Cheap signals for the standing list: dev CI red, open Dependabot PRs, per product repo (2 REST calls each)."""
    red, dependabot, errors = [], [], []
    for repo in product_repos():
        runs, h = gh("api", "repos/%s/%s/actions/runs?branch=dev&status=completed&per_page=1" % (org(), repo))
        if runs is None:
            errors.append("%s runs: %s" % (repo, h))
        elif runs.get("workflow_runs") and runs["workflow_runs"][0].get("conclusion") in ("failure", "timed_out", "startup_failure"):
            r = runs["workflow_runs"][0]
            red.append({"repo": repo, "workflow": r.get("name"), "url": r.get("html_url")})
        prs, h = gh("api", "repos/%s/%s/pulls?state=open&per_page=100" % (org(), repo))
        if prs is None:
            errors.append("%s pulls: %s" % (repo, h))
        else:
            n = sum(1 for p in prs if str((p.get("user") or {}).get("login", "")).startswith("dependabot"))
            if n:
                dependabot.append({"repo": repo, "is_open": n})
    return {"red_ci": red, "dependabot": dependabot, "errors": errors}


def choices(login=None, light=False):
    """Everything the coordinator is handed, plus errors (a failed source is reported, never fatal)."""
    if os.environ.get("LAST_WINDOW_CHOICE"):    # a JSON file {login, a, b, c, errors} replacing the live pick (offline use, tests)
        with open(os.environ["LAST_WINDOW_CHOICE"], encoding="utf-8") as f:
            return json.load(f)
    login = login or gh_login()
    errors = []
    a, h = pick_a(login)
    errors += [h] if h else []
    try:
        items, h = board_items("AI queue", tools_dir())
    except Exception as e:
        items, h = [], "board: %s: %s" % (type(e).__name__, e)
    errors += [h] if h else []
    b = pick_b(login, items, excluded={i["url"] for i in a})
    c = {"red_ci": [], "dependabot": [], "errors": []} if light else pick_c()
    errors += c.pop("errors")
    return {"login": login, "a": a, "b": b, "c": c, "errors": errors}


def item_line(i):
    return "- %s/%s#%s %s | %s%s%s" % (org(), i["repo"], i["number"], i["url"], i["title"][:100],
                                       " | priority: " + i["priority"] if i.get("priority") else "",
                                       " | assigned" if i.get("assignees") else "")


def select_text(s, upper_limit=40):
    def list_(items):
        if not items:
            return "- (none)"
        lines = [item_line(i) for i in items[:upper_limit]]
        if len(items) > upper_limit:
            lines.append("- ... and %d more (re-query)" % (len(items) - upper_limit))
        return "\n".join(lines)
    c = s["c"]
    signals = []
    for k in c["red_ci"]:
        signals.append("- CI on dev is red: %s (%s) %s" % (k["repo"], k["workflow"], k["url"]))
    for d in c["dependabot"]:
        signals.append("- %d open Dependabot PR(s) in %s" % (d["is_open"], d["repo"]))
    return list_(s["a"]), list_(s["b"]), "\n".join(signals) or "- (none found)"


def create_label(dry=False):
    """Create the label in every product repo (REST, idempotent: 422 = exists). Returns [(repo, status)]."""
    result = []
    for repo in product_repos():
        if dry:
            result.append((repo, "would create"))
            continue
        r = hidden.run(["gh", "api", "-X", "POST", "repos/%s/%s/labels" % (org(), repo), "-f", "name=" + LABEL,
                            "-f", "color=" + LABEL_COLOR, "-f", "description=" + LABEL_DESCRIPTION],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=60)
        if r.returncode == 0:
            result.append((repo, "created"))
        elif "already_exists" in (r.stdout + r.stderr) or "422" in (r.stdout + r.stderr):
            result.append((repo, "exists"))
        else:
            result.append((repo, "ERROR " + (r.stderr or r.stdout).strip().replace("\n", " ")[:120]))
    return result


# ------------------------------------------------------------------ journal
JOURNAL_RE = re.compile(r"^- \[\d\d:\d\d\] (DONE|WIP|SKIP|NOTE)(?: (.*))?$")


def journal_path(t):
    return os.path.join(DIR, time.strftime("%Y-%m-%d", time.localtime(t)) + ".md")


def add_journal(path, text_):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text_)


def journal_items(path, offset=0):
    """[(kind, ref, text)] of the DONE/WIP/SKIP/NOTE lines written after byte `offset`."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in data.splitlines():
        m = JOURNAL_RE.match(line.strip())
        if not m:
            continue
        kind, rest = m.group(1), (m.group(2) or "").strip()
        if kind == "NOTE":
            out.append((kind, "", rest))
        else:
            ref, _, text = rest.partition(" | ")
            out.append((kind, ref.strip(), text.strip()))
    return out


URL_RE = re.compile(r"^https://github\.com/([^/\s]+)/([^/\s]+)/(issues|pull)/(\d+)")


def short_ref(url):
    m = URL_RE.match(url or "")
    return "%s#%s" % (m.group(2), m.group(4)) if m else None


# ------------------------------------------------------------------ live sessions: who gets the assignment
# Claude Code registers every running session in <config dir>/sessions/<pid>.json (name, sessionId, cwd, startedAt, status, kind,
# entrypoint); its transcript is <config dir>/projects/<cwd with every non-alphanumeric char as '-'>/<sessionId>.jsonl, whose
# `assistant` entries carry the model. Verified 01.11.2026 against the live sessions on this machine.
MODEL_SCORE = {"opus": 58, "sonnet": 54, "fable": 49, "haiku": 40}   # AUTONOMY.md / crew-agents ladder (Artificial Analysis index)
TRANSCRIPT_TAIL = 400_000


def session_dir():
    return os.path.join(CLAUDE, "sessions")


def transcript_path(cwd, sid):
    return os.path.join(CLAUDE, "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd or ""), "%s.jsonl" % sid)


def last_model(path):
    """`message.model` of the last assistant entry in the transcript's tail, or None (new session, no file)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - TRANSCRIPT_TAIL))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    model = None
    for line in data.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        m = e.get("message") if isinstance(e, dict) and e.get("type") == "assistant" else None
        if isinstance(m, dict) and m.get("model") and not str(m["model"]).startswith("<"):
            model = m["model"]
    return model


def model_score(model):
    """(ladder score, version tuple) of a model id: claude-opus-5-5 -> (58, (5, 5)). Unknown -> (-1, ()), ranked last."""
    m = str(model or "").lower()
    for family, score in MODEL_SCORE.items():
        if family in m:
            version = tuple(int(x) for x in re.findall(r"(?<!\d)\d{1,2}(?!\d)", m.split(family, 1)[1])[:2])
            return score, version
    return -1, ()


def read_sessions(model=True):
    """Every registered session whose process is alive: [{pid, name, sid, cwd, started, status, kind, entry, model, path}]. model=False skips
    the transcript read (the resume check needs only the registry and its own short tail read)."""
    out = []
    try:
        names = os.listdir(session_dir())
    except OSError:
        return out
    for name in sorted(names):
        if not name.endswith(".json"):
            continue
        d = read_json(os.path.join(session_dir(), name))
        pid, sid = int(count(d.get("pid")) or 0), d.get("sessionId")
        if not pid or not sid or not record_valid(d):     # dead, or a leftover file whose pid number was reused
            continue
        path = transcript_path(d.get("cwd"), sid)
        out.append({"pid": pid, "name": d.get("name") or "", "sid": sid, "cwd": d.get("cwd") or "", "started": (count(d.get("startedAt")) or 0) / 1000.0,
                    "status": d.get("status") or "", "kind": d.get("kind"), "entry": d.get("entrypoint"), "path": path,
                    "model": last_model(path) if model else None})
    return out


def excluding_patterns():
    """Names the user keeps out of the assignment (shell patterns): LAST_WINDOW_EXCLUDING (comma list) and <last-window dir>/excluding.txt (one per line)."""
    pattern = [x.strip() for x in (os.environ.get("LAST_WINDOW_EXCLUDING") or "").split(",") if x.strip()]
    try:
        with open(os.path.join(DIR, "excluding.txt"), encoding="utf-8") as f:
            pattern += [s.strip() for s in f if s.strip() and not s.lstrip().startswith("#")]
    except OSError:
        pass
    return pattern


def dropping_reason(o, excluding, target=None):
    """Why this session cannot take the assignment, or None. Only a person's terminal session (`cli`) qualifies: headless/SDK runs
    (the old `claude -p` coordinator shows up as kind=interactive, entrypoint=sdk-cli) and last-window's own sessions never do."""
    if target:                                   # test hook: exactly this name, nothing else is considered
        return None if o["name"] == target and o["entry"] == "cli" else "not the test target"
    if o["entry"] != "cli":
        return "not a terminal session (%s)" % o["entry"]
    if not o["name"]:
        return "no name (cannot be addressed)"
    if o["name"].lower().startswith("last-window"):
        return "last-window's own session"
    if any(fnmatch.fnmatchcase(o["name"].lower(), d.lower()) for d in excluding):
        return "excluded by LAST_WINDOW_EXCLUDING / excluding.txt"
    return None


def sort_candidates(sessions, excluding=(), target=None):
    """(candidates best first, [(session, reason)] for the rest). Highest model score first, then the newer model version, then the
    oldest session start. A name shared by several live sessions cannot be addressed and is dropped."""
    candidate, dropped = [], []
    for o in sessions:
        n = dropping_reason(o, excluding, target)
        (dropped if n else candidate).append((o, n) if n else o)
    counts = {}
    for o in candidate:
        counts[o["name"]] = counts.get(o["name"], 0) + 1
    unique = [o for o in candidate if counts[o["name"]] == 1]
    dropped += [(o, "name shared by %d live sessions" % counts[o["name"]]) for o in candidate if counts[o["name"]] > 1]
    unique.sort(key=lambda o: (-model_score(o["model"])[0], tuple(-x for x in (model_score(o["model"])[1] + (0, 0, 0))[:3]), o["started"]))
    return unique, dropped


def session_table(sessions, excluding=(), target=None):
    candidate, dropped = sort_candidates(sessions, excluding, target)
    now = TIME()
    lines = []
    for i, o in enumerate(candidate):
        lines.append("  %s %-24s %-20s %-5s age %-7s" % ("-> " if i == 0 else "   ", o["name"][:24], (o["model"] or "model unknown")[:20], o["status"] or "?", duration_text(now - o["started"])))
    for o, n in dropped:
        lines.append("     %-24s %-20s %-5s excluded: %s" % (o["name"][:24] or "(no name)", (o["model"] or "-")[:20], o["status"] or "?", n))
    return candidate, lines


def duration_text(secs):
    secs = max(int(secs), 0)
    return "%dd%02dh" % (secs // 86400, secs % 86400 // 3600) if secs >= 86400 else "%dh%02dm" % (secs // 3600, secs % 3600 // 60)


# ------------------------------------------------------------------ delivery: the existing relay pattern
FORWARD_RE = re.compile(r"\b(HANDOFF|MISSING)\b")


def forward_result(output):
    """'handoff' | 'missing' | None: the LAST answer word of the relay's output, with or without markdown (`**HANDOFF**`)."""
    m = FORWARD_RE.findall(output or "")
    return {"HANDOFF": "handoff", "MISSING": "missing"}[m[-1]] if m else None


def forward(name, text_, time_overrun=None):
    """Hand `text` to the live session named `name`. A cross-session message can only be sent from inside a Claude session, so a one-shot
    headless Haiku does it with ListAgents + SendMessage (same pattern as ~/.claude/bin/takilan-watchdog.py and server-session.sh `forward`;
    no direct CLI/IPC route exists: the per-session socket in cc-socks is undocumented). Returns:
      'handoff'   the relay answered HANDOFF (sent; a busy target shows it only at the end of its current turn)
      'missing'      a clear MISSING (no such session, not sent)
      'error'     the relay could not run, or exited with an error and no answer word: nothing is known to be sent
      'unknown' the relay timed out, or exited cleanly without an answer word: it MAY have sent, so nobody else may get the same text."""
    prompt = ("You are only a message relay. Call ListAgents and find the live session whose name is exactly '%s' (there must be exactly one). "
             "If it exists, call SendMessage to it with the text inside the <message> block below, verbatim, and nothing else. Do nothing else. "
             "Finish with one single line: HANDOFF (sent) or MISSING (no such session or not sent).\n\n<message>\n%s\n</message>" % (name, text_))
    command = [claude_path(), "-p", "--model", "haiku", "--tools", "SendMessage,ListAgents", "--allowedTools", "SendMessage,ListAgents",
           "--strict-mcp-config", "--permission-mode", "bypassPermissions", "--no-session-persistence"]
    secs = time_overrun or float(os.environ.get("LAST_WINDOW_ROLE_SEC") or 300)
    try:
        r = hidden.run(command, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=secs,
                           cwd=HOME if os.path.isdir(HOME) else None,
                           env=dict(os.environ, SKIP_REQUEST_LOG="1"))
    except subprocess.TimeoutExpired:
        write_log("relay to '%s' timed out after %g s: it may have sent" % (name, secs))
        return "unknown"
    except OSError as e:
        write_log("relay to '%s' could not run: %s" % (name, type(e).__name__))
        return "error"
    result = forward_result(r.stdout)
    last = (r.stdout.strip().splitlines() or ["?"])[-1].strip()
    write_log("relay to '%s': %s%s -> %s" % (name, last[:80], "" if r.returncode == 0 else " (exit %s)" % r.returncode, result or "no answer word"))
    if result:
        return result
    return "error" if r.returncode != 0 else "unknown"


def read_transcript(path, offset):
    """(new complete lines after byte `offset`, new offset)."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    last = data.rfind(b"\n") + 1
    return [s.decode("utf-8", errors="replace") for s in data[:last].split(b"\n") if s], offset + last


# ------------------------------------------------------------------ the run
def claude_path():
    return os.environ.get("LAST_WINDOW_CLAUDE") or shutil.which("claude") or "claude"


def repos_root():
    import cost_share as MP
    return MP.root_dir() or HOME


def machine_name():
    import socket
    return socket.gethostname()


def window_name():
    """Name of the session last-window starts itself (the fallback): last-window-<person>; also the tmux session name."""
    import cost_share as MP
    return "last-window-" + MP.file_name(MP.person_name() or "person")


MODE_NAME = {"window": "last window (the last hours of the weekly quota)", "burn": "burn mode"}
ENTRY_ASSIGNMENT = ("You were chosen by `last-window` on {{MACHINE}} (plugin quota-pace; the owner's timer started it, this is not a message from a person) to run "
               "{{PERSON}}'s {{MODE_NAME}} NOW, in this session. The owner may be watching: this is a normal, visible session. "
               "Anything the owner tells you directly outranks this brief. If you are in the middle of something, bring it to a clean point (commit, push) first, "
               "then start. Nobody can approve anything for you: work only on the pre-approved items below.")
ENTRY_SESSION = ("You are the coordinator of {{PERSON}}'s {{MODE_NAME}}, started automatically by `last-window trigger` (plugin quota-pace) in the "
                "interactive session `{{SESSION}}`. The owner may be watching it live (tmux / terminal / Remote Control) and may type into it: his own "
                "instructions outrank this brief. Nobody can approve anything for you: work only on the pre-approved items below.")
MODE_BLOCK_RE = re.compile(r"<!-- mode:([a-z,]+) -->\n(.*?)<!-- /mode -->\n", re.S)


def mode_blocks(text_, mode):
    """Keep the `<!-- mode:a,b -->` ... `<!-- /mode -->` blocks of the brief that list `mode`, drop the others (one brief for every mode)."""
    return MODE_BLOCK_RE.sub(lambda m: m.group(2) if mode in m.group(1).split(",") else "", text_)
END_AT_ASSIGNMENT = ("go idle (do not exit; it is the owner's session). The wrapper sends a fresh assignment at the next tick if quota is left")
END_AT_SESSION = "exit (`/exit`); the wrapper starts a fresh coordinator at the next tick"


def build_prompt(quota, begin, journal, picks, via="session", session="", mode="window"):
    import cost_share as MP
    a, b, c = select_text(picks)
    person = MP.person_name() or "the user"
    entry = (ENTRY_ASSIGNMENT if via == "assignment" else ENTRY_SESSION).replace("{{MACHINE}}", machine_name()).replace("{{PERSON}}", person).replace(
        "{{SESSION}}", session).replace("{{MODE_NAME}}", MODE_NAME.get(mode, MODE_NAME["window"]))
    bt = read_burn() if mode == "burn" else {}
    values = {"ENTRY": entry, "END_AT": END_AT_ASSIGNMENT if via == "assignment" else END_AT_SESSION,
                "BURN_OPENER": str(bt.get("opener") or "a developer"), "BURN_OPENING": local_time(count(bt.get("opening")) or begin),
                "PERSON": person, "LOGIN": picks.get("login") or "the user", "RESET_ISO": local_time(quota["week_resets_at"]),
                "RESET_EPOCH": str(int(quota["week_resets_at"])),
                "WEEK_USED": ("%g%%" % quota["week_used"]) if quota.get("week_used") is not None else "unknown",
                "BEGIN_ISO": local_time(begin), "JOURNAL": journal, "TOOLS": picks.get("tools") or "tools",
                "LABEL": LABEL, "APPROVAL_LABEL": APPROVAL_LABEL, "CHANNEL": status_channel()[0], "CHANNEL_ID": status_channel()[1], "PICK_A": a, "PICK_B": b, "PICK_C": c}
    with open(PROMPT, encoding="utf-8") as f:
        text_ = mode_blocks(f.read(), mode)
    for k, v in values.items():
        text_ = text_.replace("{{%s}}" % k, v)
    return text_


def summary_message(marker, brief, journal, reset, person, mode="window"):
    """The message that goes to the chosen session: who sends it, the essentials, and the path of the full brief (rendered to a file so
    the relay does not have to copy 7 KB and the brief survives)."""
    closing = " (or when burn mode is switched off)" if mode == "burn" else ""
    return ("[last-window on %s] %s: assignment for the %s, automatic, not from a person. Run id %s.\n"
            "Read %s now and follow it; it is the full brief (pre-approved item order, the never-list, journal format, Slack status lines).\n"
            "Essentials: work only on the pre-approved items in the brief; post exactly ONE Slack status line per item in {{CHANNEL}} "
            "(<person> · <repo> · <area/path> · <issue url> · started, then done|handed-over as a reply in that item's thread) and nothing else in Slack; "
            "append DONE/WIP/SKIP/NOTE lines to %s after every step; commit and push WIP after every step; start nothing new in the last 10 minutes; "
            "the weekly quota resets at %s%s, when I will send \"reset reached\": then start nothing new, push WIP, report.\n"
            "I am a one-shot relay that exits right after sending: do not reply to me (SendMessage to me fails). Report through the journal and the issues." % (
                machine_name(), person, MODE_NAME.get(mode, MODE_NAME["window"]), marker, brief, journal, local_time(reset), closing)).replace(
        "{{CHANNEL}}", status_channel()[0])


def process_begin(pid):
    """Start time of process `pid` as Claude Code writes it into the registry's `procStart` (Linux: field 22 of /proc/<pid>/stat,
    clock ticks since boot; checked 01.11.2026 against every live entry), or None where it cannot be read (other systems)."""
    try:
        with open("/proc/%d/stat" % pid, encoding="ascii", errors="replace") as f:
            st = f.read()
        return st[st.rfind(")") + 2:].split()[19]
    except (OSError, IndexError, ValueError):
        return None


def record_valid(d):
    """A registry entry whose process is alive AND is the process that wrote it: a leftover file of a dead session whose pid number was
    reused has a different procStart (compared only where both are known)."""
    pid = int(count(d.get("pid")) or 0)
    if not pid or not pid_live(pid):
        return False
    written, current = d.get("procStart"), process_begin(pid)
    return written in (None, "") or current is None or str(written) == current


class Session:
    """A session the run follows: either an existing live session that received the assignment (`assignment`), or the one last-window
    starts itself (tmux on POSIX, a Windows Terminal tab on Windows)."""
    def __init__(self, name, sid=None, cwd=None, via="assignment"):
        self.name, self.sid, self.cwd, self.via, self.pid = name, sid, cwd, via, None

    @property
    def path(self):
        return transcript_path(self.cwd, self.sid)

    def record(self):
        """Registry entry of this session, or None when it is gone. Found by sessionId, else (once its pid is known) by the same pid and
        name: `/clear` gives the same process a new sessionId and transcript, which the follow loop then reads (self.sid is updated)."""
        inputs = []
        for name in os.listdir(session_dir()) if os.path.isdir(session_dir()) else ():
            if name.endswith(".json"):
                inputs.append(read_json(os.path.join(session_dir(), name)))
        for d in inputs:
            if d.get("sessionId") == self.sid and record_valid(d):
                self.pid = int(d["pid"])
                return d
        if self.pid:
            for d in inputs:
                if int(count(d.get("pid")) or 0) == self.pid and (d.get("name") or "") == self.name and d.get("sessionId") and record_valid(d):
                    write_log("'%s' (pid %d) has a new session id %s (was %s)" % (self.name, self.pid, d["sessionId"], self.sid))
                    self.sid = d["sessionId"]
                    self.cwd = d.get("cwd") or self.cwd
                    return d
        return None

    def live(self):
        return self.record() is not None

    def idle(self):
        d = self.record()
        return bool(d) and d.get("status") == "idle"

    def message(self, text_, time_overrun=None):
        """'handoff' | 'missing' | 'error' | 'unknown' (see forward)."""
        return forward(self.name, text_, time_overrun)

    def stop(self):
        """Assigned sessions are the owner's: they are told to stop (message), never killed."""


def record_sec():
    return float(os.environ.get("LAST_WINDOW_RECORD_SEC") or 120)


class TmuxSession(Session):
    def __init__(self, name, cwd):
        import uuid
        super().__init__(name, str(uuid.uuid4()), cwd, "tmux")
        self.reason = None

    def start(self, command, env):
        """Detached tmux session running the interactive claude. `env` is passed with env(1) so it also reaches a pre-existing tmux server."""
        tmux("kill-session", "-t", "=" + self.name)             # a leftover of a crashed wrapper under the same, own name
        line = "exec env " + " ".join("%s=%s" % (k, shlex.quote(v)) for k, v in env.items()) + " " + " ".join(shlex.quote(x) for x in command)
        rc, out = tmux("new-session", "-d", "-s", self.name, "-x", "220", "-y", "50", "-c", self.cwd, line)
        if rc != 0:
            raise OSError("tmux new-session: " + out.strip()[:200])

    def live(self):
        return tmux("has-session", "-t", "=" + self.name)[0] == 0

    def screen(self):
        return tmux("capture-pane", "-p", "-t", "=" + self.name + ":")[1]

    def wait_record(self, secs=None):
        """Wait until the started claude registered itself (it is then up and working on the brief). Meanwhile the one dialog the wrapper
        answers is the folder trust ("Yes, I trust this folder": a folder Claude Code has not seen interactively asks it, and the folder
        is the owner's own repos root, the cwd the coordinator always ran in; dialog text checked on Claude Code 2.1.316). Nothing else
        is ever pressed: the bypass-permissions warning is the owner's decision (accept it once interactively, see INSTALL.md). Not
        registered within LAST_WINDOW_RECORD_SEC (120 s): the pane goes to the log, the session is killed, self.reason says why."""
        end_at = TIME() + (secs or record_sec())
        bypass_seen = trust_answered = False
        while True:
            if self.record():
                return True
            if not self.live():
                self.reason = "the session ended before it registered"
                break
            screen = self.screen()
            if "I trust this folder" in screen and not trust_answered:
                if re.search(r"[❯>]\s*(1\.\s*)?Yes, I trust", screen):
                    tmux("send-keys", "-t", "=" + self.name + ":", "Enter")
                    write_log("answered the folder-trust dialog of '%s' for %s" % (self.name, self.cwd))
                    trust_answered = True
                else:
                    tmux("send-keys", "-t", "=" + self.name + ":", "Down")
            elif "bypass" in screen.lower() and not bypass_seen:
                bypass_seen = True
                write_log("'%s' shows the bypass-permissions warning: not answered (accept it once in an interactive session)" % self.name)
            if TIME() >= end_at:
                self.reason = "not registered within %d s%s" % (secs or record_sec(), " (bypass-permissions warning on screen)" if bypass_seen else "")
                break
            time.sleep(1)
        lines = [s.rstrip() for s in self.screen().splitlines() if s.strip()][-25:]
        write_log("'%s' %s; its pane:" % (self.name, self.reason))
        for s in lines:
            write_log("  | " + s)
        tmux("kill-session", "-t", "=" + self.name)             # no keys: a dialog must never be answered by the cutoff either
        return False

    def message(self, text_, time_overrun=None):
        tmux("send-keys", "-t", "=" + self.name + ":", "-l", text_.replace("\n", " "))
        time.sleep(0.5)
        return "handoff" if tmux("send-keys", "-t", "=" + self.name + ":", "Enter")[0] == 0 else "error"

    def stop(self):
        """The cutoff: interrupt the turn, `/exit`, then kill the session (this is our own session)."""
        if not self.live():
            return
        tmux("send-keys", "-t", "=" + self.name + ":", "Escape")
        time.sleep(1)
        tmux("send-keys", "-t", "=" + self.name + ":", "-l", "/exit")
        time.sleep(0.5)
        tmux("send-keys", "-t", "=" + self.name + ":", "Enter")
        for _ in range(20):
            if not self.live():
                return
            time.sleep(1)
        tmux("kill-session", "-t", "=" + self.name)


def _taskkill(pid):
    hidden.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=30)


def _sid_processes(sid):
    """Windows: pids of the processes whose command line carries our own session id (the claude of a tab that never registered).
    The id is a fresh uuid4 of this run, so this matches only our own claude; the PowerShell process itself is left out."""
    if not re.fullmatch(r"[0-9a-f-]{36}", sid or ""):
        return []
    ps = ("Get-CimInstance Win32_Process -Filter \"CommandLine like '%%%s%%'\" | Where-Object { $_.ProcessId -ne $PID } | "
          "ForEach-Object { $_.ProcessId }" % sid)
    try:
        r = hidden.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=60,
                      stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(x) for x in r.stdout.split() if x.isdigit()]


class TabSession(Session):
    """Windows: an interactive claude in a Windows Terminal tab (UNTESTED on Windows). The tab is not our child (wt.exe returns at once),
    so it is followed through the session registry once it registered, and ended by pid after it was told to stop."""
    def __init__(self, name, cwd):
        import uuid
        super().__init__(name, str(uuid.uuid4()), cwd, "tab")
        self.reason = None

    def start(self, command, env):
        os.makedirs(os.path.join(DIR, "runs"), exist_ok=True)
        cmd = os.path.join(DIR, "runs", "%s.cmd" % self.name)
        with open(cmd, "w", encoding="utf-8", newline="\r\n") as f:
            f.write("@echo off\r\ncd /d \"%s\"\r\n" % self.cwd)
            for k, v in env.items():
                f.write("set \"%s=%s\"\r\n" % (k, v.replace("%", "%%")))
            f.write(subprocess.list2cmdline(command) + "\r\n")
        wt = shutil.which("wt.exe")
        command_wt = [wt, "-w", "0", "new-tab", "--title", self.name, "cmd", "/c", cmd] if wt else ["cmd", "/c", "start", self.name, "cmd", "/c", cmd]
        # visible: a terminal tab opened on purpose for the person (they watch and answer in it): its window must stay visible
        hidden.popen(command_wt, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, visible=True)

    def wait_record(self, secs=None):
        """The tab registers only after claude started in it: wait for it (LAST_WINDOW_RECORD_SEC, 120 s) before following it, so the
        first follow pass does not mistake a starting tab for a closed one. Not registered: stop its claude (found by our session id)."""
        end_at = TIME() + (secs or record_sec())
        while not self.record():
            if TIME() >= end_at:
                self.reason = "not registered within %d s" % (secs or record_sec())
                pids = _sid_processes(self.sid)
                write_log("tab '%s' %s; stopping its claude: %s" % (self.name, self.reason, ", ".join(map(str, pids)) or "no process found"))
                for pid in pids:
                    _taskkill(pid)
                return False
            time.sleep(2)
        return True

    def stop(self):
        if not getattr(self, "_stopped", False) and self.record() and self.pid:
            self._stopped = True                    # once: the registry entry may outlive the killed process for a moment
            _taskkill(self.pid)


TMUX_SOCKET = "last-window"   # own tmux server (`tmux -L last-window`): never the user's server, and it lives and dies with the unit's cgroup


def tmux(*args, timeout=30):
    """Every tmux call of last-window goes to its own server socket: a server started here lives in the systemd unit's cgroup and is
    killed with it, and the user's own tmux sessions (default socket) are never in that cgroup or touched."""
    try:
        r = hidden.run([os.environ.get("LAST_WINDOW_TMUX") or shutil.which("tmux") or "tmux", "-L", TMUX_SOCKET, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, type(e).__name__


ENV_KEYS = ("PATH", "HOME", "SSH_AUTH_SOCK", "CLAUDE_CONFIG_DIR", "GH_CONFIG_DIR", "XDG_CONFIG_HOME", "GIT_TERMINAL_PROMPT", "GIT_CONFIG_COUNT",
                     "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "CLAUDE_SHARED_PERSON", "CLAUDE_SHARED_ROOT", "CLAUDE_SHARED_REMOTE", "PACE_TARGET_HOURS")


def session_command(name, sid, brief):
    """Interactive claude with the brief as the first prompt (the prompt goes first so an optional-value flag in LAST_WINDOW_ARGS, e.g.
    `--remote-control NAME`, cannot swallow it). bypassPermissions like the owner's own sessions."""
    command = [claude_path(), "Read %s and follow it. It is the last-window brief." % brief, "--name", name, "--session-id", sid,
           "--permission-mode", "bypassPermissions"]
    if os.environ.get("LAST_WINDOW_RC", "1") != "0":   # visible from claude.ai/code and the mobile app (verified 01.11.2026); LAST_WINDOW_RC=0 turns it off
        command += ["--remote-control", name]
    if os.environ.get("LAST_WINDOW_MODEL"):
        command += ["--model", os.environ["LAST_WINDOW_MODEL"]]
    if os.environ.get("LAST_WINDOW_ARGS"):
        command += shlex.split(os.environ["LAST_WINDOW_ARGS"], posix=(os.name != "nt"))
    return command


def time_present(reset, last_minutes):
    """True while a new hand-out may still start: before the reset minus `last_minutes` (the gate's last-10-minutes rule)."""
    return TIME() < reset - last_minutes


def hand_off(candidates, message_text, reset, last_minutes):
    """Relay the assignment to the best candidate; the next candidate ONLY on a clear 'missing' or a relay 'error' (nothing was sent).
    Returns (Session | None, status):
      'handoff'   the relay answered HANDOFF: this session has it. The run stays with it even when the message does not show up in its
                 transcript yet (a busy session receives it at the end of its turn); no other session and no fallback get it.
      'unknown' the relay timed out or gave no answer word: it may have sent, so no other session and no fallback get it this run.
      'missing'      no candidate took it: the fallback may start.
      'reset'    the reset (or its last 10 minutes) came while candidates were tried: nothing more is handed out."""
    for o in candidates[:3]:
        if not time_present(reset, last_minutes):
            write_log("reset is near (%s): no more candidates" % local_time(reset))
            return None, "reset"
        offset = os.path.getsize(o["path"]) if os.path.exists(o["path"]) else 0
        h = Session(o["name"], o["sid"], o["cwd"], "assignment")
        h.pid = o.get("pid")
        result = forward(o["name"], message_text)
        if result == "handoff":
            h.offset, h.handoff_t = offset, TIME()
            return h, "handoff"
        if result == "unknown":
            write_log("relay to '%s' gave no clear answer: it may have sent, so no other session gets the assignment this run" % o["name"])
            return None, "unknown"
        write_log("assignment not sent to '%s' (%s); trying the next candidate" % (o["name"], result))
    return None, "missing"


def _no_signal(step):
    """Run a cleanup step with SIGTERM/SIGINT/SIGHUP ignored, so a second signal (systemctl stop, Ctrl-C) cannot cut it short."""
    old = {}
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, name):
            try:
                old[name] = signal.signal(getattr(signal, name), signal.SIG_IGN)
            except (ValueError, OSError):      # not the main thread
                pass
    try:
        step()
    except Exception as e:
        write_log("cleanup failed: %s: %s" % (type(e).__name__, e))
    finally:
        for name, f in old.items():
            try:
                signal.signal(getattr(signal, name), f)
            except (ValueError, OSError, TypeError):
                pass


def run_cmd(quota, status, picks, begin, attempt=None, mode="window", skip=()):
    """One run (`mode` window or burn; `skip` = names of sessions that just got the resume relay and keep their own work): give the assignment to the best live session (or start the visible fallback session), follow it, end it at the reset.
    Returns the run record (without the shared-log part). `attempt` ({target, tmux_name, prompt}) is the test mode of `last-window attempt`.
    Whatever happens after the fallback session was started (an exception, SIGTERM), that session is stopped before this returns."""
    reset = quota["week_resets_at"]
    # the gate's last-10-minutes rule inside the run: a run that started before the last 10 minutes hands nothing out in them (several
    # candidates x relay timeouts take a while); a first start inside them, which the gate allows, is bounded by the reset itself
    last_minutes = RESET_LAST_MINUTES if reset - begin > RESET_LAST_MINUTES else 0
    journal = journal_path(begin)
    os.makedirs(os.path.join(DIR, "runs"), exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(begin))
    marker = "SP-" + stamp
    brief = os.path.join(DIR, "runs", stamp + "-brief.md")
    add_journal(journal, "\n## Run %s%s (weekly use %s at start, reset %s)\n\n" % (
        local_time(begin, True), " [burn mode]" if mode == "burn" else "", ("%g%%" % quota["week_used"]) if quota.get("week_used") is not None else "?", local_time(reset)))
    offset = os.path.getsize(journal)
    record = {"begin": begin, "week_begin": quota.get("week_used"), "journal": journal, "reset": reset, "reason": None, "cost": None, "exit": None,
             "session": None, "via": None, "turn": 0, "mode": mode}
    env = {k: os.environ[k] for k in ENV_KEYS if os.environ.get(k)}
    env.update(LAST_WINDOW="1", LAST_WINDOW_JOURNAL=journal, LAST_WINDOW_RESET=str(int(reset)))
    person = _person()
    h, started = None, False
    try:
        # 1. primary path: a live interactive session of the owner
        target = attempt["target"] if attempt else None
        # the test of the fallback (attempt --tmux) must never hand anything to a live session: skip the live-session step altogether
        candidate = [] if attempt and attempt.get("tmux_name") else sort_candidates([o for o in read_sessions() if o["name"] not in skip], excluding_patterns(), target)[0]
        handoff = None
        if candidate:
            _write(brief, attempt["prompt"] if attempt else build_prompt(quota, begin, journal, picks, "assignment", mode=mode))
            h, handoff = hand_off(candidate, summary_message(marker, brief, journal, reset, person, mode), reset, last_minutes)
        elif not attempt:
            write_log("no live interactive session can take the assignment")
        if handoff == "unknown":
            record["reason"], record["exit"] = "handoff-unknown", 1
        elif h is None and (not attempt or attempt.get("tmux_name")):
            # 2. fallback: a visible session of our own
            if handoff == "reset" or not time_present(reset, last_minutes):
                write_log("reset is near (%s): the fallback session is not started" % local_time(reset))
                record["reason"], record["exit"] = "reset-near", 0
            else:
                name = (attempt or {}).get("tmux_name") or window_name()
                _write(brief, attempt["prompt"] if attempt else build_prompt(quota, begin, journal, picks, "session", name, mode))
                h = (TabSession if os.name == "nt" else TmuxSession)(name, repos_root())
                if os.name != "nt" and not (os.environ.get("LAST_WINDOW_TMUX") or shutil.which("tmux")):
                    raise OSError("tmux is not installed (needed for the visible fallback session)")
                started = True                    # from here on the session may exist: the finally below stops it
                h.start(session_command(name, h.sid, brief), env)
                h.offset = 0
                if h.wait_record():
                    write_log("started session '%s' (%s)" % (name, h.via))
                else:                             # pane logged and session killed by wait_record; counts as a quick failure
                    record["session"], record["via"] = h.name, h.via
                    record["reason"], record["exit"], record["fast"], record["detail"] = "error", -1, True, h.reason
                    h = None
        if h is None:
            if record["reason"] is None:
                record["reason"], record["exit"] = "handoff-missing", 1
        else:
            record["session"], record["via"] = h.name, h.via
            update_lock(session=h.name, via=h.via, sid=h.sid)
            status["assignment"] = {"session": h.name, "sid": h.sid, "via": h.via, "t": int(begin)}
            write_json(STATUS, status)
            _watch(h, record, status, quota, marker, reset, begin)
            record["exit"] = 0
    except Stopped:
        write_log("stopped by a signal during the run")
        record["reason"], record["exit"] = "stopped", 0
    except Exception as e:
        write_log("run failed: %s: %s" % (type(e).__name__, e))
        record["reason"] = "error" if started or (h is not None and h.via == "assignment") else "not-started"
        record["exit"] = -1
    finally:
        if h is not None and h.via != "assignment":    # our own session never outlives the wrapper (stopping it twice is harmless)
            _no_signal(h.stop)
    end_at = TIME()
    record["end"] = end_at
    _, status_last, quota_last = now_quota(end_at)
    record["week_last"] = quota_last["week_used"] if quota_last["week_used"] is not None else quota.get("week_used")
    items = journal_items(journal, offset)
    record["finished"] = [(r, t) for k, r, t in items if k == "DONE" and r]
    record["resume"] = [(r, t) for k, r, t in items if k == "WIP" and r]
    add_journal(journal, "\n_Run ended %s: %s, session %s, %d turns, weekly use %s, %d done, %d wip_\n" % (
        local_time(end_at, True), str(record["reason"]) + (" (%s)" % record["detail"] if record.get("detail") else ""), record["session"] or "-", record["turn"],
        ("%g%%" % record["week_last"]) if record["week_last"] is not None else "?", len(record["finished"]), len(record["resume"])))
    return record


def _person():
    import cost_share as MP
    return MP.person_name() or "the user"


def _write(path, text_):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text_)


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def notify_reset(h, marker, reset, cause=None):
    """Tell an assigned session that the reset came, and make sure it got it: relay again while the relay fails, or while the session sits
    idle without the message in its transcript (an idle session shows a delivered message at once). A busy session that the relay says
    has it is not sent a second copy: it shows it at the end of its turn. Bounded: LAST_WINDOW_RESET_ATTEMPT relays (3), about
    (tries + 1) x LAST_WINDOW_STOP_SEC (90 s) in all. Returns 'seen' (in the transcript), 'handoff' (relayed, not seen yet) or 'missing'."""
    stamp = marker + "-RESET"
    text_ = ("[last-window on %s] %s (%s, %s): start nothing new, push your WIP and write the final NOTE in the journal. "
             "Do not reply to this message (the sender is a one-shot relay)." % (machine_name(), cause or "Reset reached", local_time(reset), stamp))
    attempt = int(os.environ.get("LAST_WINDOW_RESET_ATTEMPT") or 3)
    wait = float(os.environ.get("LAST_WINDOW_STOP_SEC") or 90)
    path, offset = h.path, _size(h.path)
    delivery, handoff_t, last = 0, None, None
    end_at = TIME() + wait * (attempt + 1)
    while True:
        if h.path != path:                          # the session moved to a new transcript (/clear)
            path, offset = h.path, 0
        lines, offset = read_transcript(path, offset)
        if any(stamp in s for s in lines):
            write_log("reset message reached '%s' (relay %d of %d)" % (h.name, delivery, attempt))
            return "seen"
        if not h.live():
            write_log("'%s' closed before the reset message was confirmed" % h.name)
            break
        now = TIME()
        if handoff_t is None or (now - handoff_t >= wait and h.idle()):
            if delivery >= attempt:
                break
            delivery += 1
            last = h.message(text_, time_overrun=min(120.0, float(os.environ.get("LAST_WINDOW_ROLE_SEC") or 120)))
            if last in ("handoff", "unknown"):
                handoff_t = TIME()
            else:
                write_log("reset message to '%s' not sent (%s), relay %d of %d" % (h.name, last, delivery, attempt))
        elif now >= end_at:
            break
        time.sleep(min(POLL, 5))
    write_log("reset message to '%s' NOT confirmed in its transcript after %d relay(s) (last answer: %s)" % (h.name, delivery, last))
    return "handoff" if handoff_t else "missing"


def _watch(h, record, status, quota, marker, reset, begin):
    """Follow the session until it ends: the reset (stop message / `/exit`), the weekly or 5-hour limit, the session closing, the turn cap, or
    the session idling for IDLE_SEC after it has worked (it finished; the next tick may assign again). Sets record['reason'] and record['turn'].
    An assigned session counts turns only from the assignment marker on (its earlier work is not this run's). Until the marker shows up
    the run waits with that session; it gives up (`handoff-unseen`, nobody else gets the assignment) only when the session sat idle for
    ACK_SEC without it, which means the relay's HANDOFF was wrong: an idle session shows a delivered message at once."""
    max_turn = int(os.environ.get("LAST_WINDOW_MAX_TURNS") or 200)
    idle_sec = float(os.environ.get("LAST_WINDOW_IDLE_SEC") or 600)
    ack_sec = float(os.environ.get("LAST_WINDOW_ACK_SEC") or 300)
    path, offset = h.path, getattr(h, "offset", 0)
    seen = h.via != "assignment"
    handoff_t = getattr(h, "handoff_t", None) or TIME()
    was_warned, wait_idle_t = False, None
    messages, turn_t, idle_t, limit_t = set(), None, None, None
    while True:
        if h.path != path:                          # the session moved to a new transcript (/clear): follow it there
            write_log("'%s' moved to a new transcript (%s): following it" % (h.name, h.sid))
            path, offset = h.path, 0
        lines, offset = read_transcript(path, offset)
        for s in lines:
            if not seen:
                if marker in s:
                    seen = True
                    write_log("'%s' shows the assignment, %d s after the relay" % (h.name, TIME() - handoff_t))
                continue
            try:
                e = json.loads(s)
            except ValueError:
                continue
            m = e.get("message") if isinstance(e, dict) and e.get("type") == "assistant" else None
            if isinstance(m, dict):
                messages.add(m.get("id") or s[:80])
        record["turn"] = len(messages)
        now = TIME()
        if now >= reset:
            record["reason"] = "reset"
            if h.via == "assignment":
                record["reset_notifier"] = notify_reset(h, marker, reset)
            h.stop()
            return
        if record.get("mode") == "burn" and not burn_is_open(now):     # a developer switched burn mode off (one small file read per poll)
            record["reason"] = "burn-off"
            write_log("burn mode was switched off: ending the run")
            if h.via == "assignment":
                record["reset_notifier"] = notify_reset(h, marker, now, "Burn mode was switched off")
            h.stop()
            return
        if not h.live():
            if h.via == "assignment":
                record["reason"] = "session-closed"
            else:   # our own session ended by itself: a crash right after the start is a quick failure, otherwise it simply finished
                record["reason"] = "error" if now - begin < FAST_ERROR_SEC and record["turn"] == 0 else "output"
            return
        pace, d2, k2 = now_quota(now)
        if (k2.get("week_used") or 0) >= 100:
            limit_t = limit_t or now
            if now - limit_t > 60:
                record["reason"] = "limit"
                h.stop()
                return
        elif (k2.get("five_hour_used") or 0) >= 100 and k2.get("five_hour_resets_at"):
            record["reason"] = "five-hour"
            h.stop()
            return
        if not seen:
            if now - handoff_t >= ack_sec and not was_warned:
                was_warned = True
                write_log("'%s' has not shown the assignment %d s after the relay's HANDOFF; staying with it (a busy session gets it at the end "
                        "of its turn)" % (h.name, now - handoff_t))
            wait_idle_t = (wait_idle_t or now) if h.idle() else None
            if wait_idle_t is not None and now - wait_idle_t >= ack_sec and now - handoff_t >= ack_sec:
                record["reason"] = "handoff-unseen"
                write_log("'%s' sat idle for %d s without the assignment: the relay's HANDOFF did not arrive; no other session gets it this run" % (
                    h.name, now - wait_idle_t))
                return
            time.sleep(POLL)
            continue
        if turn_t is None and len(messages) >= max_turn:
            turn_t = now
            h.message("[last-window on %s] Turn cap reached (%d): stop at a clean point, write the hand-off NOTE (done / remaining / next) and %s." % (
                machine_name(), max_turn, END_AT_ASSIGNMENT if h.via == "assignment" else END_AT_SESSION))
        if turn_t is not None and (h.idle() or now - turn_t > 600):
            record["reason"] = "turn-cap"
            h.stop()
            return
        if record["turn"] > 0 and h.idle():
            idle_t = idle_t or now
            if now - idle_t >= idle_sec:
                record["reason"] = "output"
                h.stop()
                return
        else:
            idle_t = None
        time.sleep(POLL)


def delete_old_conditions():
    cap = TIME() - 14 * 86400
    runs_dir = os.path.join(DIR, "runs")
    try:
        for name in os.listdir(runs_dir):
            path = os.path.join(runs_dir, name)
            if os.path.getmtime(path) < cap:
                os.remove(path)
    except OSError:
        pass


# ------------------------------------------------------------------ shared log (same clone/ownership rules as cost_share)
TITLE = ["Start", "End", "Week %", "Reason", "Done", "WIP", "API $", "Items"]
TABLE_TITLE = "| " + " | ".join(TITLE) + " |\n|" + "---|" * len(TITLE) + "\n"


def clone_path():
    return os.environ.get("CLAUDE_SHARED_LAST_WINDOW_CLONE") or os.path.join(HOME, ".cache", "claude-shared-last-window")


def cell(s):
    return " ".join(str(s).replace("|", "/").split())


def build_line(k):
    """One markdown row from a run record; the Start cell is the row's key."""
    def item(r):
        m = URL_RE.match(r)
        return "[%s](%s)" % (short_ref(r), r) if m else None
    linked = [item(r) for r, _ in k.get("finished", [])]
    linked = [b for b in linked if b]
    shown = linked[:8] + (["+%d more" % (len(linked) - 8)] if len(linked) > 8 else [])
    week = "%s→%s" % (("%g" % k["week_begin"]) if k.get("week_begin") is not None else "?",
                      ("%g" % k["week_last"]) if k.get("week_last") is not None else "?")
    cost = ("%.0f" % k["cost"]) if k.get("cost") else "—"
    if k.get("session"):   # which session took the run (no extra column: older rows and readers keep their 8-cell shape)
        shown = ["session:%s" % k["session"]] + shown
    reason = k.get("reason") or "running"
    if k.get("mode") in ("burn", "resume") and not reason.startswith(k["mode"] + "/"):   # the mode in the Reason cell (no extra column)
        reason = "%s/%s" % (k["mode"], reason)
    cells = [local_time(k["begin"], True), local_time(k["end"]) if k.get("end") else "running", week, reason,
                str(len(k.get("finished", []))), str(len(k.get("resume", []))), cost, " ".join(shown) or "—"]
    return "| " + " | ".join(cell(h) for h in cells) + " |"


def line_parse(line):
    p = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(p) != len(TITLE) or not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", p[0]):
        return None
    return dict(zip(TITLE, p))


def file_lines(text_):
    return [r for r in (line_parse(s) for s in (text_ or "").splitlines()) if r]


def person_file(person, text_, record):
    """The person's file with this run's row added, or replaced when a row with the same Start exists (a run is written when it
    starts and again when it ends). Rows beyond LINE_CAP are dropped from the top."""
    new = build_line(record)
    begin = local_time(record["begin"], True)
    lines = (text_ or "").splitlines()
    body = [s for s in lines if line_parse(s)]
    body = [new if s.strip("| ").startswith(begin) else s for s in body] if any(s.strip("| ").startswith(begin) for s in body) else body + [new]
    body = body[-LINE_CAP:]
    title = ("# Last-window runs — %s\n\nOne row per last-window run (the last hours before the weekly quota reset, when leftover quota is used on "
              "pre-approved work). Written by `last-window trigger`; do not edit. `Week %%` = weekly limit used at start→end. `API $` = API list-price "
              "equivalent of that run, not a bill. All people: [LOG.md](LOG.md).\n\n" % person)
    return title + TABLE_TITLE + "\n".join(body) + "\n"


def records_text(person_lines, limit=60):
    every = []
    for person, lines in person_lines.items():
        every += [(r["Start"], person, r) for r in lines]
    every.sort(reverse=True)
    title = ["Person"] + TITLE
    L = ["# Last-window log (all people)", "",
         "Latest runs of the last-window mode (last hours before each person's weekly quota reset), newest first. Generated from "
         "`reports/last-window/<person>.md`; read it with `last-window status`. Installation and rules: README of plugins/quota-pace and "
         "rules/WORKING-RULES.md (Agents and quota).", "",
         "| " + " | ".join(title) + " |", "|" + "---|" * len(title)]
    for _, person, r in every[:limit]:
        L.append("| " + " | ".join([cell(person)] + [r[b] for b in TITLE]) + " |")
    if not every:
        L.append("| (no runs yet) |" + " |" * (len(title) - 1))
    return "\n".join(L) + "\n"


def read_sharing(clone, names):
    import cost_share as MP
    out = {}
    for name in sorted(set(names)):
        try:
            with open(os.path.join(clone, *REPO_DIR.split("/"), MP.file_name(name) + ".md"), encoding="utf-8") as f:
                out[name] = file_lines(f.read())
        except OSError:
            pass
    return out


def one_attempt(clone, remote, person, record):
    import cost_share as MP
    MP.prepare_clone(clone, remote)
    lookup = MP.read_people(clone)
    if person not in lookup.values():
        raise RuntimeError("'%s' is not in people.json: nothing written" % person)
    path = os.path.join(clone, *REPO_DIR.split("/"), MP.file_name(person) + ".md")
    try:
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    except OSError:
        existing = ""
    MP.write(path, person_file(person, existing, record))
    MP.write(os.path.join(clone, *REPO_DIR.split("/"), LOG), records_text(read_sharing(clone, lookup.values())))
    MP.git(clone, "add", "--", REPO_DIR)
    rc, _ = MP.git(clone, "diff", "--cached", "--quiet")
    if rc == 0:
        return "no change"
    message = "chore(last-window): %s %s" % (person, local_time(record["begin"]))
    rc, out = MP.git(clone, "commit", "--quiet", "-m", message)
    if rc != 0 and "identity" in out.lower():
        rc, out = MP.git(clone, "-c", "user.name=" + person, "-c", "user.email=usage@claude-shared.invalid", "commit", "--quiet", "-m", message)
    if rc != 0:
        raise RuntimeError("commit: " + out.replace("\n", " ")[:200])
    rc, out = MP.git(clone, "push", "--quiet", "origin", "HEAD:main")
    if rc != 0:
        raise RuntimeError("push: " + out.replace("\n", " ")[:200])
    return "pushed"


def share(record):
    """Write/replace this run's row in the shared log. Never raises; True when pushed (or already there)."""
    import cost_share as MP
    person = MP.person_name()
    if not person:
        write_log("shared log skipped: no person name (CLAUDE_SHARED_PERSON, ~/.config/claude-shared/person, git user.name)")
        return False
    error = None
    for _ in range(2):
        try:
            status = one_attempt(clone_path(), MP.remote_address(), person, record)
            write_log("shared log: %s (%s)" % (status, build_line(record)))
            return True
        except Exception as e:
            error = str(e)
            if "people.json" in error:
                break
            time.sleep(1 + (os.getpid() % 3))
    write_log("shared log FAILED: %s" % error)
    return False


# ------------------------------------------------------------------ commands
def now_quota(now):
    pace, status = read_json(os.path.join(CLAUDE, "pace.json")), read_json(STATUS)
    return pace, status, quota_status(pace, status, now)


def modes_parse(v):
    """--modes window,resume (the timer's argument, written by the installer); burn mode needs no entry (it is switched on by hand)."""
    k = tuple(dict.fromkeys(x.strip() for x in (v or "").split(",") if x.strip()))
    wrong = [x for x in k if x not in ("window", "resume")]
    if wrong or not k:
        raise argparse.ArgumentTypeError("unknown mode(s) %s: use window and/or resume" % ",".join(wrong or ["(empty)"]))
    return k


def trigger_status(now, modes, write=True):
    """Everything this tick's gate needs, read once: pace/status/quota, burn mode (auto-off written unless `write` is False), the resume
    check (pace.json first, else the transcripts of the idle live sessions) and the gate result of every mode."""
    pace, status, quota = now_quota(now)
    if write:
        burn_duration_full(now)
    bt = read_burn()
    is_open = burn_is_open(now, bt)
    live = lock_live(now)
    res_state = read_resume()
    res = resume_status(pace, res_state, now) if ("resume" in modes or is_open) else None
    stopped = None
    if res and res["ready"] is None:
        stopped = limit_stopped(read_sessions(model=False), res["r5"], now, res_state.get("warned") or [])
        res["ready"] = bool(stopped)
        res["reason"] = "resume" if stopped else "resume-missing"
        res["detail"] = ("%d live session(s) still stopped by a 5-hour limit that has reset" % len(stopped)) if stopped else \
            "no live session is still stopped by a 5-hour limit that has reset"
    selected, results = select_mode(now, quota, target_hour(pace), live, count(status.get("last_begin")), int(status.get("fast_error") or 0),
                                modes, is_open, bool(res and res["ready"]), count(status.get("wait_idle")))
    return {"pace": pace, "status": status, "quota": quota, "burn": bt, "burn_is_open": is_open, "resume": res, "resume_status": res_state, "stopped": stopped,
            "selected": selected, "results": results}


def trigger_emit(t, modes):
    s, results = t["selected"], t["results"]
    first = s or (results[0] if results else None)
    if first:
        print("last-window: %s (%s): %s%s" % (first[1], first[2], first[3], "" if first[0] == "window" else "  [mode %s]" % first[0]))
    else:
        print("last-window: wait (no-mode): no installed mode is due now")
    for r in results:
        if r is not first:
            print("  mode %s: %s (%s): %s" % r)
    if "window" not in modes:
        print("  mode window: not installed (install --last-window)")
    bt = t["burn"]
    if t["burn_is_open"]:
        print("  burn mode: ON since %s by %s, until the weekly reset %s" % (
            local_time(count(bt.get("opening")) or 0), bt.get("opener") or "?", local_time(count(bt.get("week_reset")) or 0)))
    else:
        print("  burn mode: off")
    res = t["resume"]
    if res is None:
        print("  resume alarm: not installed (install --resume-alarm)")
    elif not res["ready"]:
        print("  resume alarm: %s: %s" % (res["reason"], res["detail"]))
    quota = t["quota"]
    if quota.get("week_used") is not None:
        print("  weekly use %g%% (source %s), reset %s" % (quota["week_used"], quota["source"], local_time(quota["week_resets_at"])))


def save_resume(now, quota, sent):
    """The resume alarm's record: a journal block (every relay and its answer) and, when at least one relay went out (or may have), one
    shared-log row (Reason resume/wake, Items session:<names>). Returns the row's Start (a run of the same tick starts after it:
    rows are keyed by their Start second)."""
    add_journal(journal_path(now), "\n## Resume alarm %s (5-hour reset)\n\n%s\n" % (local_time(now, True), "\n".join(
        "- [%s] NOTE resume relay to %s: %s" % (time.strftime("%H:%M", time.localtime(now)), name, answer) for name, answer in sent)))
    outgoing = [name for name, answer in sent if answer in ("handoff", "unknown")]
    if outgoing:
        share({"begin": now, "end": TIME(), "week_begin": quota.get("week_used"), "week_last": quota.get("week_used"), "reason": "wake",
                "mode": "resume", "finished": [], "resume": [], "cost": None, "session": ",".join(outgoing)})
        return now
    return None


def run_cmd_resume(t, now, mode):
    """The resume alarm's part of a tick, under its own lock (independent of a coordinator run): record the reset key, relay to every
    session the limit stopped (minus the session of the last assignment when a coordinator run starts now: it gets the fresh assignment),
    record. Returns ([(name, answer)], Start of the shared-log row or None)."""
    res, res_state = t["resume"], t["resume_status"]
    lock = get_resume_lock(now)
    if not lock:
        write_log("resume alarm: another tick is relaying (resume.lock)")
        return [], None
    try:
        if res["key"]:
            res_state["key"] = res["key"]                 # once per reset, recorded before any relay
            write_json(resume_path(), res_state)
        stopped = t["stopped"] if t["stopped"] is not None else limit_stopped(
            read_sessions(model=False), res["r5"], now, res_state.get("warned") or [])
        if mode in ("window", "burn"):
            stopped = [o for o in stopped if o["sid"] != (t["status"].get("assignment") or {}).get("sid")]
        sent = wake_resume(stopped, res_state)
        if not sent:
            write_log("resume alarm: no live session is stopped by the limit (%s)" % res["detail"])
            return [], None
        return sent, save_resume(now, t["quota"], sent)
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def cmd_trigger(a):
    now = TIME()
    modes = a.modes or ("window",)
    t = trigger_status(now, modes, write=not a.dry_run)
    trigger_emit(t, modes)
    status, quota, res = t["status"], t["quota"], t["resume"]
    selected = t["selected"]
    action = "start" if selected and selected[0] in ("window", "burn") else "wait"
    if a.dry_run:
        if res is not None:
            stopped = limit_stopped(read_sessions(model=False), res["r5"], now, t["resume_status"].get("warned") or [], all_=True)
            print("Sessions stopped by the 5-hour limit (each gets the resume relay once, %d min after its reset):" % (RESUME_DELAY // 60))
            print("\n".join("  %-24s stopped %s, reset %s" % (o["name"][:24], local_time(o["halt"]["t"]), local_time(o["reset"])) for o in stopped) or "  (none)")
        if "window" not in modes and not t["burn_is_open"]:
            print("DRY RUN: nothing sent, nothing written.")
            return 0
        s = choices()
        emit_choice(s)
        candidate, lines = session_table(read_sessions(), excluding_patterns())
        print("Live sessions (best first; the first one gets the assignment):")
        print("\n".join(lines) or "  (none)")
        if candidate:
            print("DRY RUN: would %s the assignment to '%s' (relay: a one-shot headless Haiku with SendMessage), brief from %s. Nothing sent, nothing written." % (
                "send" if action == "start" else "NOT send now, otherwise send", candidate[0]["name"], PROMPT))
        else:
            print("DRY RUN: no live session can take it; would %s the visible session '%s' (cwd %s). Nothing started, nothing written." % (
                "start" if action == "start" else "NOT start now, otherwise start", window_name(), repos_root()))
        return 0
    if not selected:
        for r in t["results"]:
            write_log("%s: %s: %s" % (r[0], r[2], r[3]))
        return 0
    mode = selected[0]
    sent, resume_begin = [], None
    if res and res["ready"]:                # the resume alarm first: the sessions the limit stopped get their own work back
        _install_handlers()
        sent, resume_begin = run_cmd_resume(t, now, mode)
    if action != "start":
        return 0
    if not get_lock(now):
        write_log("running: lock taken between the check and the start")
        return 0
    _install_handlers()
    try:
        begin_t = TIME()
        if resume_begin is not None and int(begin_t) <= int(resume_begin):    # own Start second: rows are keyed by it
            begin_t = int(resume_begin) + 1
        picks = choices()
        picks["tools"] = tools_dir()
        for h in picks["errors"]:
            write_log("pick: " + h)
        status["last_begin"] = int(begin_t)
        write_json(STATUS, status)
        draft = {"begin": begin_t, "week_begin": quota.get("week_used"), "reason": None, "finished": [], "resume": [], "cost": None, "mode": mode}
        share(draft)                      # visible to the team as "running" from the start
        record = run_cmd(quota, status, picks, begin_t, mode=mode, skip={name for name, c in sent if c in ("handoff", "unknown")})
        fast = record.get("fast") or (record["end"] - record["begin"] < FAST_ERROR_SEC and record["reason"] in ("error", "not-started"))
        status["fast_error"] = int(status.get("fast_error") or 0) + 1 if fast else 0
        idle_run = not record["finished"] and not record["resume"] and record["reason"] in ("output", "handoff-missing", "handoff-unseen", "handoff-unknown", "session-closed")
        status["wait_idle"] = int(TIME() + BURN_IDLE_WAIT) if mode == "burn" and idle_run else None
        write_json(STATUS, status)
        share(record)
        write_log("run ended (%s): %s, exit %s, done %d, wip %d" % (mode, record["reason"], record["exit"], len(record["finished"]), len(record["resume"])))
        delete_old_conditions()
    finally:
        lock_release()
    return 0


def setup_record():
    """The install flags saved by the installer (~/.claude/quota-pace.json "setup"), or None (a copy install keeps no record)."""
    k = read_json(os.path.join(CLAUDE, "quota-pace.json")).get("setup")
    return k if isinstance(k, list) else None


def burn_installed():
    """True when the last-window timer is installed (--last-window or --resume-alarm saved), None when unknown."""
    k = setup_record()
    return None if k is None else ("--last-window" in k or "--resume-alarm" in k)


def installed_modes():
    """The modes the timer runs, as the installer passed them (`last-window status` shows the same gate the timer sees)."""
    k = setup_record() or ["--last-window"]
    return tuple(m for m, f in (("window", "--last-window"), ("resume", "--resume-alarm")) if f in k) or ("window",)


def cmd_burn(a):
    """last-window burn --on | --off | status."""
    now = TIME()
    if a.operation == "status":
        burn_duration_full(now)
        b = read_burn()
        if burn_is_open(now, b):
            print("Burn mode ON: switched on by %s (%s). It switches itself off at the weekly reset (%s); to switch it off: last-window burn --off" % (
                b.get("opener"), local_time(count(b.get("opening")) or 0), local_time(count(b.get("week_reset")) or 0)))
        elif b:
            print("Burn mode OFF (last switched off by %s, %s)." % (b.get("closer") or "?", local_time(count(b.get("closing")) or 0)))
        else:
            print("Burn mode OFF (never switched on).")
        return 0
    # Accident guards, not a security boundary (the login override and people.json are local): a coordinator never switches the mode
    # itself. A session last-window started carries LAST_WINDOW=1; an assigned session of the owner runs while the coordinator lock is live.
    if os.environ.get("LAST_WINDOW") == "1" or (a.operation == "on" and lock_live(now)):
        print("Refused: burn mode cannot be switched on while a last-window run is going or from a last-window session; a developer switches it "
              "on from their own terminal when no run is going (switching it off works during a run too: last-window burn --off from your own terminal).")
        return 3
    login = gh_login()
    name, role = person_role(login)
    if not login:
        print("Refused: the GitHub login could not be read (gh auth login). Burn mode cannot be changed without checking the role.")
        return 3
    if role != "developer":
        print("Refused: only developers (people.json `role: developer`) may switch burn mode on or off. "
              "GitHub login '%s' %s." % (login, "is not in people.json" if not name else "has role: %s" % role))
        write_log("burn mode %s refused for %s (role %s)" % (a.operation, login, role))
        return 3
    b = read_burn()
    if a.operation == "off":
        if not burn_is_open(now, b):
            print("Burn mode is already off.")
            return 0
        b.update(active=False, closer=name, closer_login=login, closing=int(now))
        write_json(burn_path(), b)
        write_log("burn mode switched OFF by %s (%s)" % (name, login))
        print("Burn mode switched OFF (%s, %s). A running run stops at the next check." % (name, local_time(now)))
        return 0
    if burn_is_open(now, b):
        print("Burn mode is already on: switched on by %s (%s), until the weekly reset (%s)." % (
            b.get("opener"), local_time(count(b.get("opening")) or 0), local_time(count(b.get("week_reset")) or 0)))
        return 0
    _, _, quota = now_quota(now)
    reset = quota.get("week_resets_at")
    if not reset:
        print("Refused: the weekly quota reset time is unknown (no pace.json). Open a Claude Code session first; the status line writes pace.json.")
        return 2
    if quota.get("week_used") is not None and quota["week_used"] >= 100:
        print("Refused: the weekly quota is already used up (%g%%); reset %s." % (quota["week_used"], local_time(reset)))
        return 2
    write_json(burn_path(), {"active": True, "opener": name, "opener_login": login, "opening": int(now), "week_reset": int(reset),
                            "week_reset_source": quota.get("source"), "machine": machine_name()})
    write_log("burn mode switched ON by %s (%s) until %s" % (name, login, local_time(reset)))
    print("Burn mode ON: %s, %s. Pre-approved work (the last-window order and never-list) is done until the weekly quota is used up or the weekly "
          "reset (%s%s); after every 5-hour reset it goes on where it stopped. To switch it off: last-window burn --off" % (
              name, local_time(now), local_time(reset), ", estimated" if quota.get("source") == "forecast" else ""))
    if burn_installed() is False:
        print("WARNING: the last-window timer does not look installed on this machine; the mode runs only from the timer. To install it: "
              "pwsh -File plugins/quota-pace/install.ps1 -ResumeAlarm  (Linux: bash plugins/quota-pace/install.sh --resume-alarm)")
    return 0


def emit_choice(s):
    a, b, c = s["a"], s["b"], s["c"]
    print("Picks for %s (re-picked at every start):" % (s["login"] or "?"))
    print("  (a) label %s: %d open issue(s)" % (LABEL, len(a)))
    for i in a[:15]:
        print("      " + item_line(i))
    print("  (b) board 'AI queue', not %s, unassigned or mine: %d issue(s), highest priority first" % (APPROVAL_LABEL, len(b)))
    for i in b[:15]:
        print("      " + item_line(i))
    if len(b) > 15:
        print("      ... %d more" % (len(b) - 15))
    print("  (c) standing list signals: %d repo(s) with red CI on dev, Dependabot PRs in %d repo(s)" % (len(c["red_ci"]), len(c["dependabot"])))
    for k in c["red_ci"]:
        print("      red CI on dev: %s (%s) %s" % (k["repo"], k["workflow"], k["url"]))
    for d in c["dependabot"]:
        print("      %d Dependabot PR(s): %s" % (d["is_open"], d["repo"]))
    for h in s["errors"]:
        print("  [source error] " + h)


def cmd_choice(a):
    emit_choice(choices())
    return 0


def cmd_label(a):
    result = create_label(a.dry)
    for repo, d in result:
        print("%-22s %s" % (repo, d))
    return 1 if any(d.startswith("ERROR") for _, d in result) else 0


def cmd_status(a):
    import cost_share as MP
    clone = clone_path()
    notes = []
    if not a.local:
        try:
            MP.prepare_clone(clone, MP.remote_address())
        except RuntimeError as e:
            notes.append("could not refresh the shared log (%s)" % e)
    lookup = MP.read_people(clone) if os.path.isdir(os.path.join(clone, ".git")) else {}
    data = read_sharing(clone, lookup.values()) if lookup else {}
    every = sorted(((r["Start"], k, r) for k, rs in data.items() for r in rs), reverse=True)[:a.last]
    if every:
        print("%-18s %-17s %-17s %-8s %-9s %-4s %-3s %s" % ("Person", "Start", "End", "Week %", "Reason", "Done", "WIP", "Items"))
        for _, person, r in every:
            print("%-18s %-17s %-17s %-8s %-9s %-4s %-3s %s" % (person[:18], r["Start"][:16], r["End"], r["Week %"], r["Reason"], r["Done"], r["WIP"],
                                                                 re.sub(r"\]\([^)]*\)", "]", r["Items"]).replace("[", "")))
    else:
        print("No last-window runs recorded in the shared log yet.")
    for n in notes:
        print("note: " + n)
    now = TIME()
    print("This machine:")
    modes = installed_modes()
    trigger_emit(trigger_status(now, modes, write=False), modes)
    k = read_lock()
    if lock_live(now) and k.get("session"):
        print("  running now in session '%s' (%s), since %s; watch it: %s" % (
            k["session"], "assigned live session" if k.get("via") == "assignment" else "started by last-window",
            local_time(count(k.get("t")) or now), ("tmux -L %s attach -t %s" % (TMUX_SOCKET, k["session"])) if k.get("via") == "tmux" else "open that session / `claude --resume`"))
    return 0


def cmd_session(a):
    """Print the live sessions, who would get the assignment and why the others are out. Starts and sends nothing."""
    candidate, lines = session_table(read_sessions(), excluding_patterns())
    print("Live sessions on this machine (best first: model score, newer model version, oldest start):")
    print("\n".join(lines) or "  (none)")
    print("Assignment goes to: %s" % (candidate[0]["name"] if candidate else "nobody (the visible session '%s' would be started)" % window_name()))
    return 0


def cmd_attempt(a):
    """Test of the whole delivery + follow + cutoff path with a trivial prompt, in a scratch state dir (own lock, log and journal; no
    gate, no shared-log row). --send NAME: relay to the live session NAME (must start with last-window-test); --tmux: start the
    visible fallback session last-window-test. The run ends at --reset-sec seconds with the normal reset handling."""
    global DIR, LOCK, STATUS
    import tempfile
    if a.send and not a.send.startswith("last-window-test"):
        print("refused: --send only targets sessions named last-window-test*")
        return 2
    if bool(a.send) == bool(a.tmux):
        print("give exactly one of --send NAME or --tmux")
        return 2
    DIR = tempfile.mkdtemp(prefix="last-window-attempt-")
    LOCK, STATUS = os.path.join(DIR, "coordinator.lock"), os.path.join(DIR, "status.json")
    now = TIME()
    quota = {"week_used": None, "week_resets_at": now + a.reset_sec}
    attempt = {"target": a.send, "tmux_name": "last-window-test" if a.tmux else None, "prompt": a.prompt}
    get_lock(now)
    _install_handlers()
    try:
        record = run_cmd(quota, {}, {}, now, attempt)
    finally:
        lock_release()
    print("attempt: reason=%s session=%s mode=%s turns=%s state dir %s" % (record["reason"], record["session"], record["via"], record["turn"], DIR))
    shutil.rmtree(DIR, ignore_errors=True)
    return 0 if record["reason"] in ("reset", "output") else 1


def main(argv=None):
    for flow in (sys.stdout, sys.stderr):   # None under pythonw.exe (the Windows scheduled task): nothing to reconfigure
        if flow is not None and hasattr(flow, "reconfigure"):
            flow.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="last-window", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    t = sp.add_parser("trigger", help="timer entry: gate, then run one coordinator")
    t.add_argument("--dry-run", action="store_true", help="print the gate decision and the picks; start nothing, write nothing")
    t.add_argument("--modes", type=modes_parse, default=None, metavar="window,resume",
                   help="modes the timer runs (written by the installer; default window). Burn mode runs whenever it is switched on")
    t.set_defaults(f=cmd_trigger)
    b = sp.add_parser("burn", help="burn mode: --on / --off (developers only) or status")
    g = b.add_mutually_exclusive_group()
    g.add_argument("--on", dest="operation", action="store_const", const="on", help="switch burn mode on (role: developer only)")
    g.add_argument("--off", dest="operation", action="store_const", const="off", help="switch burn mode off (role: developer only)")
    b.add_argument("status", nargs="?", choices=["status"], help="show the state (the default)")
    b.set_defaults(f=cmd_burn, operation="status")
    sp.add_parser("choice", help="print the picked items").set_defaults(f=cmd_choice)
    d = sp.add_parser("status", help="latest runs of everyone (shared log)")
    d.add_argument("--last", type=int, default=15)
    d.add_argument("--local", action="store_true", help="do not refresh the shared log from GitHub")
    d.set_defaults(f=cmd_status)
    sp.add_parser("session", help="print the live sessions and who would get the assignment").set_defaults(f=cmd_session)
    x = sp.add_parser("attempt", help="test the delivery / follow / cutoff path with a trivial prompt (scratch state, no real run)")
    x.add_argument("--send", metavar="NAME", help="relay to the live test session NAME (name must start with last-window-test)")
    x.add_argument("--tmux", action="store_true", help="start the visible fallback session last-window-test")
    x.add_argument("--reset-sec", type=int, default=120, help="seconds until the simulated reset (default 120)")
    x.add_argument("--prompt", default="Reply with the single word OK and stop. Do not use any tool.", help="the trivial prompt")
    x.set_defaults(f=cmd_attempt)
    e = sp.add_parser("label", help="create the label in the product repos")
    e.add_argument("--dry", action="store_true")
    e.set_defaults(f=cmd_label)
    a = ap.parse_args(argv)
    if a.cmd == "trigger" and not a.dry_run:
        try:
            return a.f(a)
        except Stopped:
            write_log("stopped by a signal")
            return 0
        except Exception as ex:   # the timer must never see a failure it cannot act on
            write_log("ERROR %s: %s" % (type(ex).__name__, ex))
            return 0
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())

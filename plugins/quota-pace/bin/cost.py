"""Claude Code spending statistics: day / week / month / year (API-price equivalent of token usage, NOT a bill).
Stdlib only, spends no tokens, sends nothing anywhere (everything stays on this machine).

  cost                  summary: rolling windows, today, last 7 days, last 4 weeks, months, years (empty sections are hidden)
  cost --window        rolling windows: last 24 hours / 7 days / 30 days (with per-model breakdown)
  cost --day|--week|--month|--year [--last N]   one view in detail (incl. the 10 most expensive sessions)
  cost --json           all views as one JSON document (ids and numbers only, no prompt text)
  cost --save         write daily totals to daily.jsonl and hourly totals (last 45 days) to hourly.jsonl (the timer runs this)
  cost --panel [DIR]    write flat JSON tables and a static dashboard index.html (default <config dir>/cost/panel)
  cost --status          one status-line string "[last Nm ₺A (₺a) · ]last 24h ₺A (₺a) · 7d ₺B $C (₺b) · 30d ₺D (₺d) · ×N" (7d also in dollars; (₺..) = real cost, ×N = plan is N times cheaper than API prices; both only with a calibration) (reads only the cache file, never scans transcripts)
  cost --status --money U1 U2 ...   same, then one line per USD amount U with the amount in lira (the status lines format
                           their amounts here, one place); no lira rate cached yet = dollars
  cost --status --input  same, but reads the status-line JSON from stdin and prints 3 lines: the windows string, "session"
                           (cost.total_cost_usd) and the cost of the current/last action (per prompt_id, see turn_amount) as "₺A (₺B)"
  cost --refresh-status   incrementally refresh that cache (--status starts it in the background when the cache is older than 5 min)

Source: <config dir>/projects/<project>/<session>.jsonl and <session>/subagents/**/agent-*.jsonl (+ .meta.json: agentType only).
The config dir is $CLAUDE_CONFIG_DIR, else ~/.claude. Parsing and the price table live in cost_parse.py and
cost-prices.json. Advisor calls are read from usage.iterations[] and priced with their own model.
Day boundary: $COST_TZ (IANA name, or "local"); default Europe/Istanbul. Weeks are ISO (Mon-Sun).
Money is shown in lira: computation stays in USD, the displayed value is multiplied by the TCMB rate in <config dir>/cost/fx.json
(cost_fx.py fetches it in the background refresh only; no rate yet = dollars).
Files: cost.py (entry + fast --status path), cost_core.py (scan, records, reports), cost_parse.py (parser, prices).
Writes: <config dir>/cost/{daily.jsonl, hourly.jsonl, cache.json, status.cache}; all 0600, directory 0700.
No prompt, output, title or description text is written anywhere (project folder names and session ids are).
"""
import json
import os
import sys
import time

# python -I (isolated) does not put the script's directory on sys.path: add it so the sibling modules import.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", "tools"))  # repo layout: tools/hidden.py (a --copy install ships it next to this file)
from hidden_loader import hidden  # noqa: E402  (one hidden-launch helper)

HOME = os.path.expanduser("~")
CLAUDE = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
DIR = os.path.join(CLAUDE, "cost")
STATUS = os.path.join(DIR, "status.cache")
LOCK = os.path.join(DIR, "status.lock")
SCRIPT = os.path.abspath(__file__)
TZ_NAME = os.environ.get("COST_TZ") or "Europe/Istanbul"
STATUS_TTL = 300    # seconds: a status cache older than this is refreshed in the background
HOURLY_DAYS = 45   # days kept in hourly.jsonl
WINDOWS = (("last 24h", 1), ("7d", 7), ("30d", 30))  # rolling windows: (label, days)
API_LABEL = "API equivalent (not a bill)"   # closes the cost segment while there is no "real cost" calibration


# ------------------------------------------------------------------ status line (fast path, no heavy imports)
def _tz():
    if os.name == "nt":
        # no tzset, and the C runtime knows "UTC" but not IANA names: other zones use the system's local zone here
        # (only the completion of a window's start from daily totals depends on it)
        if TZ_NAME.upper() == "UTC":
            os.environ["TZ"] = "UTC"
            try:
                import ctypes
                ctypes.CDLL("ucrtbase")._tzset()  # the CRT caches the zone: re-read TZ
            except Exception:
                pass
        return
    if TZ_NAME.lower() != "local":
        os.environ["TZ"] = TZ_NAME
    try:
        time.tzset()
    except AttributeError:
        pass


def _start_refresh():
    """One detached background refresh (`--refresh-status` child); not restarted when one was started in the last 60 s.
    The child takes an exclusive non-blocking lock itself (_get_lock: fcntl on POSIX, msvcrt on Windows), so two scans never
    overlap. No external `flock`/`nice` binary is needed (macOS has neither flock nor GNU tools)."""
    try:
        if time.time() - os.stat(LOCK).st_mtime < 60:
            return
    except OSError:
        pass
    if os.name == "nt":
        return _refresh_start_windows()
    try:
        pid = os.fork()
    except (OSError, AttributeError):
        return
    if pid:
        return
    try:
        os.setsid()
        dn = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(dn, fd)
        os.makedirs(DIR, mode=0o700, exist_ok=True)
        try:
            os.close(os.open(LOCK, os.O_WRONLY | os.O_CREAT, 0o600))
            os.utime(LOCK)
        except OSError:
            pass
        try:
            os.nice(19)
        except (OSError, AttributeError):
            pass
        exe = sys.executable or "python3"
        os.execv(exe, [exe, "-I", SCRIPT, "--refresh-status"])
    finally:
        os._exit(0)


def _refresh_start_windows():
    """Windows: one detached, windowless, idle-priority `--refresh-status` child (no fork/flock here). The 60 s stamp stops a
    restart storm; the child takes an exclusive lock itself (_get_lock) so two scans never overlap."""
    try:
        os.makedirs(DIR, exist_ok=True)
        with open(LOCK, "ab"):
            pass
        os.utime(LOCK)
        exe = sys.executable or "python"
        pw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(pw):
            exe = pw
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | IDLE_PRIORITY_CLASS (+ CREATE_NO_WINDOW from hidden)
        import subprocess   # local: the hot path of this module never needs it (a missing import here was swallowed below and no refresh ever started)
        hidden.popen([exe, "-I", SCRIPT, "--refresh-status"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, creationflags=0x00000008 | 0x00000200 | 0x00000040)
    except Exception:
        pass


def _get_lock():
    """Non-blocking exclusive lock on status.lock, held until the process exits (fd kept in a global).
    Returns False when another refresh holds it. POSIX: fcntl.flock; Windows: msvcrt.locking on the first byte."""
    global _LOCK_FD
    try:
        os.makedirs(DIR, mode=0o700, exist_ok=True)
        fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FD = fd
        return True
    except OSError:
        return False
    except Exception:
        return True


def window_parts(n_days, now_h, e, hour, day_usd):
    """Rolling window = the last n_days*24 hour buckets (the current partial hour included).
    Returns [("s", hour, 1.0) | ("g", "YYYY-MM-DD", share)].
    Limit: the hourly record starts at hour e (the oldest bucket). When the window reaches back before e, those hours are
    completed from the daily totals: the part of a day's total not covered by hourly buckets (daily - that day's hourly sum)
    is spread evenly over the hours before e and the share that overlaps the window is taken.
    hour: {hour: usd}; day_usd: {day: usd}."""
    start = now_h - 24 * n_days + 1
    out = [("s", h, 1.0) for h in range(start, now_h + 1) if h in hour]
    if e is None or start >= e:
        return out
    lt = time.localtime(start * 3600)
    day_start = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    while day_start // 3600 < e:
        lt = time.localtime(day_start)
        iso = time.strftime("%Y-%m-%d", lt)
        following = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1)))
        h0 = -(-day_start // 3600)
        h_end = -(-following // 3600)
        h1 = min(h_end, e)
        cut0 = max(h0, start)
        top = day_usd.get(iso) or 0.0
        if top > 0 and h1 > h0 and h1 > cut0:
            present = sum(hour[h] for h in range(h1, h_end) if h in hour)
            missing = max(0.0, top - present)
            out.append(("g", iso, missing / top * (h1 - cut0) / (h1 - h0)))
        day_start = following
    return out


FX = os.path.join(DIR, "fx.json")


def read_fx():
    """Cached USD->TRY rate (TCMB ForexSelling), None when there is none yet."""
    try:
        with open(FX, encoding="utf-8") as f:
            k = float(json.load(f).get("rate"))
        return k if 1.0 <= k <= 1000.0 else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def money(usd, rate=None, full=False):
    """Display an USD amount in lira at the cached rate: '₺' prefix, Turkish number format (thousands '.', decimals ','),
    2 decimals under 100 and none from 100 up ('₺2,31', '₺205', '₺18.950'). Without a cached rate: dollars ('$2.31', '$205';
    full=True: whole dollars, for rounded sums)."""
    usd = float(usd)
    rate = read_fx() if rate is None else rate
    if not rate:
        return ("$%.2f" if abs(usd) < 100 and not full else "$%.0f") % usd
    v = usd * rate
    if abs(v) < 100:
        return "₺" + ("%.2f" % v).replace(".", ",")
    return "₺" + ("{:,.0f}".format(v)).replace(",", ".")


def usd_tr(usd):
    """Whole dollars in the same Turkish style as money(): '$' prefix, thousands '.', 2 decimals (',') under 100 ('$1.763', '$2,31')."""
    usd = float(usd)
    if abs(usd) < 100:
        return "$" + ("%.2f" % usd).replace(".", ",")
    return "$" + ("{:,.0f}".format(usd)).replace(",", ".")


def coefficient():
    """API $ -> 'real cost' factor of the quota calibration (cost_ratio.read_coefficient), None without a fresh calibration."""
    try:
        import cost_ratio
        return cost_ratio.read_coefficient()
    except Exception:
        return None


def cost_pair(usd, full=False):
    """Amount as in the agent rows: '₺20 (₺0,33)' = API-price amount, then in parentheses what that usage really cost on the plan
    (real cost, the quota calibration of cost_ratio.py); only '₺20' without a fresh calibration. full=True: the API amount is
    rounded to whole dollars first (rolling-window sums)."""
    k = coefficient()
    return money(round(usd) if full else usd, None, full) + (" (%s)" % money(usd * k) if k else "")


TURN = os.path.join(DIR, "turn")
TURN_OLD = 7 * 86400
LAST_MIN_SEC = 300    # floor of the "last <duration>" window of the status line (see input_amounts)
TURN_HISTORY = 40     # earlier prompt boundaries kept per session (lines 2+ of the state file)


def turn_amount(sid, pid, total, dir_=None):
    return _turn(sid, pid, total, dir_)[0]


def _turn(sid, pid, total, dir_=None, history=None):
    """(USD, epoch of the boundary) -- USD cost of the current/last action = Claude Code's cost.total_cost_usd now minus its value when the action began.
    An action is one user prompt (prompt_id). Baseline = the last total seen under the previous prompt_id (state per session in
    <config dir>/cost/turn/<session>: "prompt_id baseline last_total"); a total that dropped (/clear, new session) restarts at 0,
    so the result is never negative. The first sighting of a session has no baseline: it starts at the current total. Atomic write.
    The boundary time is when this prompt_id was first seen (4th field of the state file; 3-field files of older versions: unknown).
    Lines 2+ of the state file are the earlier prompts' "boundary baseline" pairs, newest first (older versions read line 1 only);
    when `history` is a list it receives them as (epoch, baseline USD) tuples."""
    dir_ = dir_ or TURN
    name = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(sid))[:120]
    path = os.path.join(dir_, name)
    sp = sb = sl = st = None
    old = []
    try:
        with open(path, encoding="utf-8") as f:
            part = f.readline().split()
            for r in f:
                x = r.split()
                if len(x) == 2:
                    old.append((float(x[0]), float(x[1])))
        if len(part) in (3, 4):
            sp, sb, sl = part[0], float(part[1]), float(part[2])
            st = float(part[3]) if len(part) == 4 else None
    except (OSError, ValueError):
        pass
    now = time.time()
    bts = st if sp == pid and st else now     # same prompt: keep the boundary time
    if sp is None:
        base = total
    elif sp != pid:
        base = sl if total >= sl else 0.0
    else:
        base = sb
    if total < base:
        base = 0.0
    if sp is not None and sp != pid and st:
        old.insert(0, (st, sb))     # the prompt that just ended becomes history
    old = old[:TURN_HISTORY]
    if history is not None:
        history.extend(old)
    try:
        new = sp is None
        os.makedirs(dir_, mode=0o700, exist_ok=True)
        if new:     # a new session: drop state files older than a week
            limit = time.time() - TURN_OLD
            for e in os.scandir(dir_):
                try:
                    if e.is_file() and e.stat().st_mtime < limit:
                        os.remove(e.path)
                except OSError:
                    pass
        tmp = "%s.tmp%d" % (path, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("%s %.6f %.6f %.0f\n" % (pid, base, total, bts) + "".join("%.0f %.6f\n" % e for e in old))
        os.replace(tmp, path)
    except OSError:
        pass
    return total - base, bts


def elapsed_str(sec):
    """Elapsed time: '45s' under a minute, '7m' from 60 s, '1h20m' from 60 min up (the 'last Nm' label)."""
    sec = max(0, int(sec))
    if sec < 60:
        return "%ds" % sec
    min_ = sec // 60
    return "%dh%dm" % (min_ // 60, min_ % 60) if min_ >= 60 else "%dm" % min_


def input_amounts(raw):
    """Status-line JSON text -> ["<session total USD>", "<last action USD>", "<USD since the prompt boundary incl. agents>",
    "<boundary epoch>"] (the last two only with a baseline); "-" for a value that cannot be known."""
    try:
        d = json.loads(raw)
        t = d["cost"]["total_cost_usd"]
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            return ["-", "-"]
        t = float(t)
        sid, pid = d.get("session_id"), d.get("prompt_id")
        if not sid or not pid:
            return ["%.6f" % t, "-"]
        history = []
        delta, bts = _turn(sid, str(pid), t, None, history)
        all_ = delta     # total_cost_usd delta: main session + its agents
        # "last <duration>" floor: a prompt younger than LAST_MIN_SEC (a 2 s window says nothing) is widened to the newest earlier prompt
        # boundary that is at least that old (the oldest known one when none is), with the cost since that boundary
        if time.time() - bts < LAST_MIN_SEC:
            for b, tb in history:
                if tb <= t:
                    bts, all_ = b, t - tb
                    if time.time() - b >= LAST_MIN_SEC:
                        break
        tp = d.get("transcript_path")
        if isinstance(tp, str) and tp.endswith(".jsonl") and os.path.isfile(tp):
            # the last action = the main session's own responses since its last prompt (agents have their own rows); the
            # total_cost_usd delta (which includes agents) is the fallback when the transcript cannot be read
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # python -I does not add the script directory
            try:
                import cost_incremental
                v = cost_incremental.turn_total(tp, str(sid))
            except Exception:
                v = None
            if v is not None:
                delta = max(v, 0.0)
        return ["%.6f" % t, "%.6f" % delta, "%.6f" % all_, "%.0f" % bts]
    except (ValueError, KeyError, TypeError, AttributeError):
        return ["-", "-"]


def status_line():
    _tz()
    try:
        with open(STATUS, encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = None
    if c is None or "hour" not in c or time.time() - float(c.get("t") or 0) > STATUS_TTL:
        _start_refresh()
    if not c or "hour" not in c:
        return ""
    try:
        hour = {int(k): float(v) for k, v in c["hour"].items()}
        day = c.get("days") or {}
        e = c.get("e")
        now_h = int(time.time() // 3600)
        parts = []
        rate = read_fx()
        for name, n in WINDOWS:
            t = 0.0
            for kind, k, w in window_parts(n, now_h, e, hour, day):
                t += hour[k] if kind == "s" else float(day[k]) * w
            if name == "7d" and rate:    # the weekly window also in dollars, without parentheses (they are for the real cost)
                a = cost_pair(t, True).partition(" (")
                txt = "%s %s %s%s" % (name, a[0], usd_tr(t), a[1] + a[2] if a[1] else "")
            else:
                txt = "%s %s" % (name, cost_pair(t, True))
            parts.append(txt)
    except (ValueError, TypeError, KeyError):
        return ""
    k = coefficient()
    if k:     # how many times cheaper the plan is than API list prices: 1/coefficient = (API $ per 100 % of the week) / (weekly plan $)
        parts.append("×%d" % round(1.0 / k))
    else:     # no calibration (plan unknown, e.g. on macOS the login may live in the Keychain; or too little data): the amounts of the whole
              # line are API list-price equivalents of the local transcripts, not what the subscription charges
        parts.append(API_LABEL)
    return " · ".join(parts)


def write_status(amounts=(), action=False):
    """Line 1: the rolling-window string (empty when there is no cache yet). Then one line per USD amount in `amounts`,
    in lira; with `action` each amount (session, last action) is the '₺A (₺B)' text instead, and the 3rd/4th items (USD since
    the prompt boundary incl. agents, boundary epoch) become the first window 'last 7m ₺A (₺B)' of line 1 (omitted when 0 or unknown).
    UTF-8 bytes whatever the console code page is (Windows would write the middle dot as cp1252)."""
    lines = [status_line()]
    if action and len(amounts) >= 4:
        try:
            u, bts = float(amounts[2]), float(amounts[3])
            if u > 0 and bts > 0:
                on = "last %s %s" % (elapsed_str(time.time() - bts), cost_pair(u))
                lines[0] = on + (" · " + lines[0] if lines[0] else "")
        except ValueError:
            pass
        amounts = amounts[:2]
    for i, t in enumerate(amounts):
        try:
            lines.append(cost_pair(float(t)) if action else money(float(t)))
        except ValueError:
            lines.append("")
    output = "\n".join(lines) if amounts else lines[0]
    sys.stdout.buffer.write(output.encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


def _run_cmd_scheduled(main):
    """The scheduled run (--save / --share: timer or Task Scheduler under pythonw, no stdout, no stderr): leaves a run log in
    ~/.local/state/cost.log (env COST_LOG; rolling, tools/runlog.py) with start, the sharing steps (cost_share), the exit
    code and, on a crash, the traceback. The exit code and the exception go on unchanged; a log that cannot be written is ignored."""
    lg = None
    try:
        import runlog
        lg = runlog.runlog("cost")
        lg.info("start pid=%d args=%s", os.getpid(), " ".join(sys.argv[1:]))
    except Exception:
        pass
    t0, code = time.time(), 1
    try:
        code = main() or 0
        return code
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        raise
    except BaseException:
        if lg:
            import traceback
            lg.error("crash: %s", traceback.format_exc())
        raise
    finally:
        if lg:
            lg.info("exit=%s duration=%.1fs", code, time.time() - t0)


if __name__ == "__main__":
    if "--status" in sys.argv[1:]:
        a = sys.argv[1:]
        if "--input" in a:
            amounts = input_amounts(sys.stdin.buffer.read().decode("utf-8", "replace"))
        else:
            amounts = a[a.index("--money") + 1:] if "--money" in a else ()
        sys.exit(write_status(amounts, "--input" in a))  # fast path: only this small file is compiled, the core is not loaded
    if "--refresh-status" in sys.argv[1:] and not _get_lock():
        sys.exit(0)  # another refresh is running
    import cost_core
    if any(a in sys.argv[1:] for a in ("--save", "--share", "--ranking")):
        sys.exit(_run_cmd_scheduled(cost_core.main))
    sys.exit(cost_core.main())


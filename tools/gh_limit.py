#!/usr/bin/env python3
"""gh-limit: machine-wide GitHub API budget guard in front of the real `gh` (all sessions and agents of one machine share it).

  gh ...                                   # through the shim ~/.local/bin/gh that `--install` writes (install.sh / install.ps1 do it)
  python3 tools/gh_limit.py --install          # write the shim (points at this clone; falls back to the real gh if the clone is gone)
  python3 tools/gh_limit.py --remove       # remove the shim (only if it is ours)
  python3 tools/gh_limit.py --summary [--minutes 30]   # summary of the call log: calls/min, top endpoints, 403/429s, waits

Why: ~6 sessions x 10-20 agents share one token. The hourly budget has room, but GitHub's secondary limits (concurrency, points/min,
content-creating requests/min, search 30/min) stall agents with 403 "secondary rate limit". GitHub docs ("Rate limits for the REST
API", "secondary rate limits"): <=100 concurrent requests, ~900 points/min REST, ~80 content-creating requests/min and ~500/h,
search 30/min (code search 10/min); after a limit hit wait `retry-after`, else until `x-ratelimit-reset` when remaining is 0, else at
least one minute, growing exponentially.

What it does per call (state in ~/.cache/gh-limit, shared through flock by every process of this user):
  - at most SLOTS (8) gh processes at once (one lock file per slot; a crashed process frees its slot with its file lock);
  - content-creating calls (gh api POST/PATCH/PUT/DELETE except GraphQL queries, GraphQL mutations, gh issue/pr/label/... writes):
    >=1 s apart, <=60/min, <=450/h; search: <=25/min (code search <=9/min);
  - on 403/429 rate-limit answers: a machine-wide pause (retry-after / x-ratelimit-reset / 60 s doubling, cap 300 s), then a retry
    (at most MAX_ATTEMPT retries) -- only when stdout is not a terminal (output is buffered so a retry never duplicates output);
  - `gh api` GETs get an ETag cache (If-None-Match; a 304 replays the cached output, and 304s do not count against the primary limit);
  - `gh run watch` / `gh pr checks --watch` get `--interval 30` unless an interval is given (gh polls every 3-10 s by default);
  - when fewer than 1500 calls of the hourly core/graphql budget are left (x-ratelimit-* of earlier answers), calls are paced so
    the rest lasts until the reset (shared by GH_LIMIT_MACHINE=2 machines, at most 30 s apart) instead of running dry and stalling everyone;
  - one JSON line per call in ~/.cache/gh-limit/log (class, endpoint, status, waits, attempts, remaining budget).
Everything else passes through unchanged: arguments, stdin, exit code, output. `gh auth` (the git credential helper), `gh help`,
`gh --version`, nested calls (GH_LIMIT_ACTIVE) and GH_LIMIT_CLOSED=1 go straight to the real gh.
Windows: same code path (the shared locks use msvcrt instead of flock). `--install` writes ~/.local/bin/gh (sh shim, Git Bash) and
~/.local/bin/gh.ps1 (pwsh shim), and a marked block in the pwsh all-hosts profile that puts ~/.local/bin first on PATH (the machine
PATH, which holds gh.exe, always comes before the user PATH and needs admin to change); `--remove` removes all three.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # python -I drops the script dir
from hidden_loader import hidden  # noqa: E402  (one hidden-launch helper)
import time
from pathlib import Path

try:
    import fcntl
except ImportError:          # Windows: shared locks come from msvcrt.locking
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None
WINDOWS = os.name == "nt"

DIR = Path(os.environ.get("GH_LIMIT_DIR") or (Path.home() / ".cache" / "gh-limit"))
SLOTS = int(os.environ.get("GH_LIMIT_SLOT", "8"))
MAX_ATTEMPT = int(os.environ.get("GH_LIMIT_MAX_ATTEMPT", "4"))
WAIT_UPPER = 300.0            # cap of one backoff wait (s)
PAUSE_UPPER = 900.0          # never wait longer than this for a pause (primary exhaustion -> fail fast with gh's own error)
LOG_UPPER = 5 * 1024 * 1024
PACE_THRESHOLD = int(os.environ.get("GH_LIMIT_PACE_THRESHOLD", "1500"))   # below this many calls left in the hour, pace the calls
PACE_BACKUP = 200            # calls kept back for the end of the window
MACHINE = int(os.environ.get("GH_LIMIT_MACHINE", "1"))   # config key: machines sharing one GitHub token (default 1)
PACE_UPPER = 30.0             # never space calls more than this (s): slower, but never stuck
WATCH_INTERVAL = int(os.environ.get("GH_LIMIT_WATCH_INTERVAL", "30"))   # poll interval forced on `gh run watch` / `--watch`
CACHE_UPPER = 2 * 1024 * 1024
MARKER = "# gh-limit shim"   # marker line of the shim we write; --install/--remove never touch a ~/.local/bin/gh without it
PROFILE_BEGIN, PROFILE_END = "# >>> gh-limit (tools/gh_limit.py --install) >>>", "# <<< gh-limit <<<"   # managed block of the pwsh profile

# bucket: (min gap s, max per minute, max per hour)
BUCKET = {"write": (1.0, 60, 450), "search": (2.0, 25, 10**9), "code_search": (6.5, 9, 10**9)}

TRANSITION = {"auth", "help", "version", "completion", "config", "alias", "extension", "ext", "-h", "--help", "--version", "-v"}
WATCH = {("run", "watch")}
WRITE = {
    "issue": {"create", "comment", "edit", "close", "reopen", "delete", "transfer", "pin", "unpin", "lock", "unlock", "develop"},
    "pr": {"create", "comment", "edit", "close", "reopen", "merge", "ready", "review", "lock", "unlock", "update-branch"},
    "label": {"create", "edit", "delete", "clone"}, "release": {"create", "edit", "delete", "upload", "delete-asset"},
    "run": {"rerun", "cancel", "delete"}, "workflow": {"run", "enable", "disable"}, "cache": {"delete"},
    "project": {"create", "edit", "delete", "close", "copy", "item-add", "item-create", "item-edit", "item-delete", "item-archive",
                "field-create", "field-delete", "link", "unlink", "mark-template"},
    "repo": {"create", "edit", "delete", "fork", "rename", "archive", "unarchive", "sync", "deploy-key", "autolink"},
    "gist": {"create", "edit", "delete", "rename", "clone"}, "secret": {"set", "delete"}, "variable": {"set", "delete"},
}
# gh api flags that take a value (both `-X POST` and `-XPOST` / `--method=POST` forms are handled)
API_VALUABLE = {"-X": "method", "--method": "method", "-H": "header", "--header": "header", "-f": "field", "--raw-field": "field",
               "-F": "field", "--field": "field", "--input": "input", "-q": "jq", "--jq": "jq", "-t": "template",
               "--template": "template", "--cache": "cache", "-p": "preview", "--preview": "preview", "--hostname": "hostname"}
API_FLAG = {"-i": "include", "--include": "include", "--paginate": "paginate", "--slurp": "slurp", "--silent": "silent",
              "--verbose": "verbose"}
LIMIT_RE = re.compile(r"secondary rate limit|rate limit exceeded|abuse detection|HTTP 429|too many requests", re.I)


# ---------------------------------------------------------------- argument analysis (pure)

def api_parse(args):
    """Parse the arguments after `gh api`: {"endpoint", "method", "header": [...], "field": [...], flags...}."""
    r = {"endpoint": None, "header": [], "field": [], "input": None, "jq": None, "template": None, "cache": None}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            if r["endpoint"] is None and i + 1 < len(args):
                r["endpoint"] = args[i + 1]
            break
        name, value = a, None
        if a.startswith("--") and "=" in a:
            name, value = a.split("=", 1)
        elif len(a) > 2 and a[:2] in API_VALUABLE and not a.startswith("--"):
            name, value = a[:2], a[2:]
        if name in API_VALUABLE:
            if value is None:
                i += 1
                value = args[i] if i < len(args) else ""
            k = API_VALUABLE[name]
            if k in ("header", "field"):
                r[k].append(value)
            else:
                r[k] = value
        elif a in API_FLAG:
            r[API_FLAG[a]] = True
        elif not a.startswith("-") and r["endpoint"] is None:
            r["endpoint"] = a
        i += 1
    m = r.get("method")
    r["method"] = (m or ("POST" if (r["field"] or r["input"]) else "GET")).upper()
    return r


def classify(args):
    """(class, endpoint label, api-parse or None). Classes: transition, watch, write, search, code_search, graphql, read."""
    pos = [a for a in args if not a.startswith("-")]
    if not args or args[0] in TRANSITION or (pos and pos[0] in TRANSITION) or "--help" in args or "-h" in args[:3]:
        return "transition", " ".join(args[:2]), None
    group = pos[0] if pos else args[0]
    verb = pos[1] if len(pos) > 1 else ""
    if group == "api":
        p = api_parse(args[args.index("api") + 1:])
        ep = p["endpoint"] or ""
        label = "api %s %s" % (p["method"], normalize_endpoint(ep))
        if ep == "graphql":
            text = " ".join(p["field"]) + " " + (p["input"] or "")
            return ("write" if re.search(r"\bmutation\b", text) else "graphql"), label, p
        path = ep.lstrip("/")
        if path.startswith("search/code"):
            return "code_search", label, p
        if path.startswith("search/"):
            return "search", label, p
        return ("read" if p["method"] in ("GET", "HEAD") else "write"), label, p
    label = "%s %s" % (group, verb)
    if (group, verb) in WATCH or "--watch" in args:
        return "watch", label, None
    if group == "search":
        return ("code_search" if verb == "code" else "search"), label, None
    if verb in WRITE.get(group, ()):
        return "write", label, None
    return "read", label, None


def watch_interval(args):
    """`gh run watch` / `--watch` poll every 3 s (pr checks: 10 s) by default: add `--interval WATCH_INTERVAL` unless one is given."""
    if any(a in ("-i", "--interval") or a.startswith("--interval=") for a in args):
        return args
    return args + ["--interval", str(WATCH_INTERVAL)]


def pr_short_circuit(args):
    """Branch-lookup `gh pr view` / `gh pr status` (no PR number or URL) is answered locally: the team never opens PRs in its own repos, and
    Claude Code's PR-status lookup in every session made these the top caller (778 of 3102 calls in 20 min, 04.11.2026).
    GH_LIMIT_PR=1 turns it off; an explicit PR number/URL/branch argument always goes to GitHub."""
    if os.environ.get("GH_LIMIT_PR") == "1":
        return False
    pos = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-R", "--repo", "--json", "-q", "--jq", "-t", "--template"):
            i += 2
            continue
        if not a.startswith("-"):
            pos.append(a)
        i += 1
    return pos[:2] == ["pr", "status"] or (pos[:2] == ["pr", "view"] and len(pos) == 2)


def normalize_endpoint(ep):
    """Endpoint label for the log: query string dropped, numbers -> :n, shas -> :sha (owner/repo kept, they matter for the top list)."""
    path = ep.split("?", 1)[0].lstrip("/")
    part = []
    for s in path.split("/"):
        if s.isdigit():
            s = ":n"
        elif re.fullmatch(r"[0-9a-f]{7,40}", s):
            s = ":sha"
        part.append(s)
    return "/".join(part)


# ---------------------------------------------------------------- limiter math (pure)

def reserved(times, now, interval, minutes_upper, hour_upper):
    """Earliest start time t >= now for a new call given the reserved start times `times` (sorted): t is at least `interval` after
    the last one, and the windows (t-60, t] and (t-3600, t] keep below their caps."""
    z = sorted(times)
    t = max(now, (z[-1] + interval) if z else now)
    for _ in range(10000):
        last_min = [x for x in z if x > t - 60]
        last_hour = [x for x in z if x > t - 3600]
        if len(last_min) >= minutes_upper:
            t = last_min[len(last_min) - minutes_upper] + 60.0
            continue
        if len(last_hour) >= hour_upper:
            t = last_hour[len(last_hour) - hour_upper] + 3600.0
            continue
        return t
    return t


def pace_interval(remaining, reset_at, now):
    """Seconds between calls of one machine so the hourly budget lasts until its reset: 0 while more than PACE_THRESHOLD calls are left,
    else (seconds to reset) / (calls left - PACE_BACKUP) * MACHINE, capped at PACE_UPPER."""
    if remaining is None or reset_at is None or reset_at <= now or remaining > PACE_THRESHOLD:
        return 0.0
    return min((reset_at - now) / max(remaining - PACE_BACKUP, 1) * MACHINE, PACE_UPPER)


def source(kind, args, p):
    """Rate-limit resource a call spends: graphql for GraphQL and gh issue/pr/project/repo commands, else core (search has its own)."""
    if p is not None:
        return "graphql" if p["endpoint"] == "graphql" else "core"
    group = next((a for a in args if not a.startswith("-")), "")
    return "core" if group in ("run", "workflow", "release", "cache", "secret", "variable", "gist", "label") else "graphql"


def wait_pace(res):
    """Pace this call when the shared hourly budget of `res` runs low (last seen x-ratelimit-* values). Returns seconds waited."""
    now = time.time()

    def f(st):
        b = st.get("budget", {}).get(res)
        interim = pace_interval(b and b.get("remaining"), b and b.get("reset"), now)
        if not interim:
            return now
        z = [x for x in st.get("pace_" + res, []) if x > now - 3600]
        t = reserved(z[-50:], now, interim, 10**9, 10**9)
        t = min(t, now + 60.0)   # bounded backlog: one call waits at most 60 s (agent tool timeouts), never piles up
        st["pace_" + res] = z[-50:] + [t]
        return t
    wait = max(change_status(f) - time.time(), 0.0)
    if wait:
        time.sleep(wait)
    return wait


def save_budget(hdrs):
    res, remaining, reset = hdrs.get("x-ratelimit-resource"), hdrs.get("x-ratelimit-remaining"), hdrs.get("x-ratelimit-reset")
    if res and remaining and remaining.isdigit() and reset and reset.isdigit():
        change_status(lambda st: st.setdefault("budget", {}).__setitem__(res, {"remaining": int(remaining), "reset": int(reset)}))


def waiting_duration(attempt, retry_after=None, remaining=None, reset_at=None, now=None):
    """Seconds to wait after a rate-limited answer (GitHub's documented order): retry-after; x-ratelimit-reset when remaining is 0;
    else 60 s doubling per attempt (attempt = 1, 2, ...), capped at WAIT_UPPER. Returns (seconds, primary_exhausted)."""
    now = time.time() if now is None else now
    if retry_after is not None:
        return min(max(float(retry_after), 1.0), WAIT_UPPER), False
    if remaining == 0 and reset_at:
        return max(float(reset_at) - now + 1.0, 1.0), True
    return min(60.0 * (2 ** (attempt - 1)), WAIT_UPPER), False


def split_headers(data):
    """Split `gh api -i` stdout into (status, headers{lower: value}, body bytes). Unknown shape -> (None, {}, data)."""
    if not data.startswith(b"HTTP/"):
        return None, {}, data
    for delimiter in (b"\r\n\r\n", b"\n\n"):
        k = data.find(delimiter)
        if k >= 0:
            break
    else:
        k, delimiter = len(data), b""
    lines = data[:k].decode("latin-1").splitlines()
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return None, {}, data
    headers = {}
    for s in lines[1:]:
        if ":" in s:
            a, d = s.split(":", 1)
            headers[a.strip().lower()] = d.strip()
    return status, headers, data[k + len(delimiter):]


def is_limited(status, headers, rc, stderr):
    """True for a rate-limit answer (primary or secondary), False for any other result."""
    if status in (403, 429):
        return status == 429 or "retry-after" in headers or headers.get("x-ratelimit-remaining") == "0" or bool(LIMIT_RE.search(stderr))
    return rc != 0 and status is None and bool(LIMIT_RE.search(stderr))


# ---------------------------------------------------------------- shared state (flock)

def lock(f, unblocked):
    """Exclusive lock on an open file, released when it is closed (or the process dies). OSError when it is not free and it is taken."""
    if fcntl:
        fcntl.flock(f, fcntl.LOCK_EX | (fcntl.LOCK_NB if unblocked else 0))
        return
    f.seek(0)
    while True:     # Windows: lock the first byte; LK_LOCK gives up after ~10 s, so the blocking form polls LK_NBLCK
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if unblocked:
                raise
            time.sleep(0.05)


class Lock:
    def __init__(self, path, unblocked=False):
        self.path, self.unblocked, self.f = path, unblocked, None

    def __enter__(self):
        self.f = open(self.path, "a+")
        try:
            lock(self.f, self.unblocked)
        except OSError:
            self.f.close()
            self.f = None
        return self.f

    def __exit__(self, *_):
        if self.f:
            self.f.close()


def change_status(fn):
    """Read-modify-write ~/.cache/gh-limit/bucket.json under its lock; fn(state) returns a value."""
    with Lock(DIR / "bucket.lock"):
        path = DIR / "bucket.json"
        try:
            st = json.loads(path.read_text())
        except (OSError, ValueError):
            st = {}
        result = fn(st)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        for i in range(20):     # Windows: replacing a file that another process is reading fails for a moment
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if i == 19:
                    raise
                time.sleep(0.05)
        return result


def wait_bucket(kind):
    """Reserve a start time in the class bucket; sleep until it. Returns the seconds waited."""
    if kind not in BUCKET:
        return 0.0
    interval, per_minute, per_hour = BUCKET[kind]
    now = time.time()

    def f(st):
        z = [x for x in st.get(kind, []) if x > now - 3600]
        t = reserved(z, now, interval, per_minute, per_hour)
        t = min(t, now + 60.0)   # bounded backlog: a write waits at most 60 s; past the caps GitHub's 403 + retry takes over
        z.append(t)
        st[kind] = z
        return t
    t = change_status(f)
    wait = max(t - time.time(), 0.0)
    if wait:
        time.sleep(wait)
    return wait


def wait_pause():
    """Honour a machine-wide pause set after a rate-limit answer (capped at PAUSE_UPPER). Returns the seconds waited."""
    try:
        until = json.loads((DIR / "bucket.json").read_text()).get("stop", 0)
    except (OSError, ValueError):
        return 0.0
    wait = min(max(until - time.time(), 0.0), PAUSE_UPPER)
    if wait:
        time.sleep(wait)
    return wait


def pause_put(seconds):
    until = time.time() + seconds
    change_status(lambda st: st.__setitem__("stop", max(st.get("stop", 0), until)))


def slot_al(time_overrun=600.0):
    """Take one of SLOTS lock files (non-blocking scan, 0.2 s pause between scans). After time_overrun run without a slot."""
    started = time.time()
    while True:
        for i in range(SLOTS):
            k = Lock(DIR / ("slot.%d" % i), unblocked=True)
            if k.__enter__():
                return k, time.time() - started
        if time.time() - started > time_overrun:
            return None, time.time() - started
        time.sleep(0.2)


def log_write(record):
    path = DIR / "log"
    try:
        if path.exists() and path.stat().st_size > LOG_UPPER:
            os.replace(path, path.with_name("log.1"))
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------- ETag cache

def cache_suitable(p):
    return (p is not None and p["method"] == "GET" and p["endpoint"] not in (None, "graphql") and not p.get("paginate")
            and not p.get("include") and not p.get("silent") and not p.get("cache") and not p.get("input")
            and not any(h.lower().startswith("if-none-match") for h in p["header"]))


def cache_key(args):
    identity = [args, os.environ.get("GH_HOST", ""), os.environ.get("GH_REPO", ""),
              hashlib.sha256(os.environ.get("GH_TOKEN", "").encode()).hexdigest()[:12]]
    if any("{" in a for a in args):     # {owner}/{repo} placeholders resolve from the current directory's git remote
        identity.append(os.getcwd())
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


# ---------------------------------------------------------------- main path

def real_gh():
    """The real gh: GH_LIMIT_REAL (set by the shim) or the first `gh` on PATH that is not a gh-limit shim."""
    g = os.environ.get("GH_LIMIT_REAL")
    if g and os.access(g, os.X_OK):
        return g
    for d in os.environ.get("PATH", "").split(os.pathsep):
        c = os.path.join(d, "gh.exe" if WINDOWS else "gh")
        if os.path.isfile(c) and os.access(c, os.X_OK):
            try:
                with open(c, "rb") as f:
                    if MARKER.encode() in f.read(4096):
                        continue
            except OSError:
                continue
            return c
    return r"C:\Program Files\GitHub CLI\gh.exe" if WINDOWS else "/usr/bin/gh"


def transition(gh, args):
    if WINDOWS:     # os.execv does not replace the process on Windows: the prompt would return before gh ends
        sys.exit(hidden.run([gh] + args, visible=True).returncode)   # visible: interactive pass-through, the terminal's console is inherited (Ctrl+C, prompts)
    os.execv(gh, [gh] + args)


def run_cmd(args):
    gh = real_gh()
    if (fcntl is None and msvcrt is None) or os.environ.get("GH_LIMIT_CLOSED") == "1" or os.environ.get("GH_LIMIT_ACTIVE") == "1":
        transition(gh, args)
    kind, label, p = classify(args)
    if kind == "transition":
        transition(gh, args)
    if pr_short_circuit(args):
        try:
            branch = hidden.run(["git", "branch", "--show-current"], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            branch = ""
        sys.stderr.write('no pull requests found for branch "%s"\n' % branch)
        try:
            DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            pass
        log_write({"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "h": socket.gethostname(), "pid": os.getpid(), "kind": "local",
                 "endpoint": label, "status": None, "rc": 1, "cache": True, "short_circuit": True})
        return 1
    try:
        DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        transition(gh, args)
    env = dict(os.environ, GH_LIMIT_ACTIVE="1")
    record = {"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "h": socket.gethostname(), "pid": os.getpid(), "kind": kind, "endpoint": label}
    started = time.time()
    buffer = not sys.stdout.isatty() and kind != "watch" and os.environ.get("GH_LIMIT_FLOW") != "1"

    if kind == "watch":
        args = watch_interval(args)
        sys.stderr.write("gh-limit: `gh %s` polls GitHub (default every 3-10 s; raised to %d s here); WORKING-RULES (GitHub API "
                         "budget) asks for one REST check every 4-5 min instead.\n" % (label, WATCH_INTERVAL))
    if not buffer:      # terminal or watch: stream, no retry (output cannot be taken back)
        wait = wait_pause() + wait_bucket(kind)
        slot, sw = (None, 0.0) if kind == "watch" else slot_al()
        try:
            rc = hidden.run([gh] + args, env=env, visible=True).returncode   # visible: streams to the terminal
        finally:
            if slot:
                slot.__exit__()
        record.update(rc=rc, wait=round(wait + sw, 2), duration=round(time.time() - started, 2), mod="flow")
        log_write(record)
        return rc

    stdin_data = None
    if not sys.stdin.isatty() and any(a == "-" or a.endswith("@-") for a in args):
        stdin_data = sys.stdin.buffer.read()   # buffered once so a retry can send it again
    ek, ai = [], (args.index("api") if p is not None else 0)
    inject = p is not None and not any(p.get(k) for k in ("include", "paginate", "silent", "verbose"))
    if inject:
        ek.append("--include")
    key, cache = None, None
    if cache_suitable(p):
        key = cache_key(args)
        try:
            cache = json.loads((DIR / "etag" / (key + ".json")).read_text())
            ek += ["-H", "If-None-Match: " + cache["etag"]]
        except (OSError, ValueError, KeyError):
            cache = None
    wait_total, attempt = 0.0, 0
    res = None if kind in ("search", "code_search") else source(kind, args, p)
    while True:
        attempt += 1
        wait_total += wait_pause() + wait_bucket(kind) + (wait_pace(res) if res else 0.0)
        slot, sw = slot_al()
        wait_total += sw
        try:
            r = hidden.run([gh] + args[:ai + 1] + ek + args[ai + 1:], env=env, capture_output=True, input=stdin_data)
        finally:
            if slot:
                slot.__exit__()
        out, err = r.stdout, r.stderr
        status, hdrs = None, {}
        if inject:
            status, hdrs, out = split_headers(out)
            save_budget(hdrs)
        err_text = err.decode("utf-8", "replace")
        if is_limited(status, hdrs, r.returncode, err_text) and attempt <= MAX_ATTEMPT:
            ra = hdrs.get("retry-after")
            remaining = hdrs.get("x-ratelimit-remaining")
            sec, primary = waiting_duration(attempt, float(ra) if ra and ra.replace(".", "", 1).isdigit() else None,
                                          int(remaining) if remaining and remaining.isdigit() else None, hdrs.get("x-ratelimit-reset"))
            log_write(dict(record, status=status, rc=r.returncode, attempt=attempt, limited=True, wait_after=round(sec, 1)))
            if sec > PAUSE_UPPER:      # primary budget gone for a long time: fail now with gh's own message
                break
            pause_put(sec)
            continue
        break
    rc = r.returncode
    from_cache = False
    if status == 304 and cache is not None:
        out, err, rc, from_cache = cache["out"].encode("latin-1"), b"", 0, True
    elif key and status == 200 and rc == 0 and hdrs.get("etag") and len(out) <= CACHE_UPPER:
        try:
            (DIR / "etag").mkdir(exist_ok=True, mode=0o700)
            tmp = DIR / "etag" / (key + ".tmp%d" % os.getpid())
            tmp.write_text(json.dumps({"etag": hdrs["etag"], "out": out.decode("latin-1"), "t": time.time()}))
            os.replace(tmp, DIR / "etag" / (key + ".json"))
        except OSError:
            pass
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(err)
    sys.stderr.buffer.flush()
    record.update(status=status, rc=rc, attempt=attempt, wait=round(wait_total, 2), duration=round(time.time() - started, 2))
    for k, a in (("remaining", "x-ratelimit-remaining"), ("source", "x-ratelimit-resource")):
        if a in hdrs:
            record[k] = hdrs[a]
    if from_cache:
        record["cache"] = True
    log_write(record)
    return rc


# ---------------------------------------------------------------- install / summary

def shim_text(clone_script, real, python):
    """The sh shim (Linux/macOS; Git Bash on Windows, where paths are written with forward slashes)."""
    q = lambda x: sh_q(Path(x).as_posix() if WINDOWS else x)
    return ("#!/bin/sh\n%s (written by tools/gh_limit.py --install; runs the clone's limiter, falls back to the real gh without it)\n"
            "w=%s\n"
            "GH_LIMIT_REAL=%s; export GH_LIMIT_REAL\n"
            "if [ -f \"$w\" ] && [ \"${GH_LIMIT_CLOSED:-}\" != 1 ] && [ \"${GH_LIMIT_ACTIVE:-}\" != 1 ] && [ -x %s ]; then\n"
            "  GH_LIMIT_SHIM=1 exec %s -I \"$w\" \"$@\"\nfi\nexec \"$GH_LIMIT_REAL\" \"$@\"\n") % (MARKER, q(clone_script), q(real), q(python),
                                                                                 q(python))


def shim_ps1_text(clone_script, real, python):
    """The pwsh shim (Windows): native argument passing, piped input forwarded, the session's environment restored afterwards."""
    ps = lambda x: "'" + str(x).replace("'", "''") + "'"
    return (MARKER + " (written by tools/gh_limit.py --install; runs the clone's limiter, falls back to the real gh without it)\r\n"
            "$w = %s; $py = %s; $real = %s\r\n"
            "$old = @{}; foreach ($k in 'GH_LIMIT_REAL', 'GH_LIMIT_SHIM') { $old[$k] = [Environment]::GetEnvironmentVariable($k) }\r\n"
            "try {\r\n"
            "  $env:GH_LIMIT_REAL = $real\r\n"
            "  if ((Test-Path -LiteralPath $w) -and (Test-Path -LiteralPath $py) -and $env:GH_LIMIT_CLOSED -ne '1' -and $env:GH_LIMIT_ACTIVE -ne '1') {\r\n"
            "    $env:GH_LIMIT_SHIM = '1'\r\n"
            "    if ($MyInvocation.ExpectingInput) { $input | & $py -I $w @args } else { & $py -I $w @args }\r\n"
            "  } elseif ($MyInvocation.ExpectingInput) { $input | & $real @args } else { & $real @args }\r\n"
            "  $rc = $LASTEXITCODE\r\n"
            "} finally { foreach ($k in $old.Keys) { [Environment]::SetEnvironmentVariable($k, $old[$k]) } }\r\n"
            "exit $rc\r\n") % (ps(clone_script), ps(python), ps(real))


def sh_q(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def profile_path():
    """The pwsh all-hosts profile <Documents>/PowerShell/profile.ps1 (Documents from the shell API, it may be redirected); None
    without pwsh, off Windows, or when Documents lies outside the home directory (a scratch HOME must never edit the real profile)."""
    if not (WINDOWS and shutil.which("pwsh")):
        return None
    try:
        import ctypes
        # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
        class GUID(ctypes.Structure):
            _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]
        g = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
        ptr = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(ptr)) != 0:
            return None
        docs = Path(ptr.value)
        ctypes.windll.ole32.CoTaskMemFree(ptr)
        path = docs / "PowerShell" / "profile.ps1"
        path.resolve().relative_to(Path.home().resolve())
        return path
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def profile_block(bin_dir):
    return ("%s\r\n$__ghs = '%s'; $env:Path = $__ghs + ';' + (($env:Path -split ';' | Where-Object { $_ -ne $__ghs }) -join ';')\r\n%s\r\n"
            % (PROFILE_BEGIN, str(bin_dir).replace("'", "''"), PROFILE_END))


def write_profile(path, bin_dir, remove, tag, whatif):
    """Put (or remove) the managed block in the pwsh profile. Everything outside the block is kept."""
    try:
        old = path.read_bytes().decode("utf-8-sig")   # bytes: keep the file's own line endings
    except OSError:
        old = ""
    begin = old.find(PROFILE_BEGIN)
    end = old.find(PROFILE_END, begin) if begin >= 0 else -1
    clean = old if begin < 0 or end < 0 else (old[:begin] + old[end + len(PROFILE_END):].lstrip("\r\n"))
    new = clean if remove else (clean.rstrip("\r\n") + ("\r\n" if clean.strip() else "") + profile_block(bin_dir))
    if new.strip() == "" and remove:
        new = ""
    if new == old:
        print("%sgh-limit: profile same: %s" % (tag, path))
        return
    print("%sgh-limit: profile %s: %s" % (tag, "clean" if remove else "write", path))
    if not whatif:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(new.encode("utf-8"))


def install(remove=False, target=None, whatif=False):
    """Write (or remove) the shim ~/.local/bin/gh (+ gh.ps1 and the pwsh profile block on Windows).
    Returns 0 on success, 1 when a foreign ~/.local/bin/gh is in the way."""
    with_profile = WINDOWS and target is None   # the pwsh profile only follows the default target (an explicit one is a test or a trial)
    target = Path(target or (Path.home() / ".local" / "bin" / "gh"))
    tag = "[-WhatIf] " if whatif else ""
    ps1 = target.with_name(target.name + ".ps1")
    ours = target.is_file() and MARKER in target.read_text(errors="replace")
    if target.exists() and not ours:
        print("gh-limit: WARNING: %s is another file, left untouched" % target)
        return 1
    if remove:
        for d in ([target, ps1] if WINDOWS else [target]):
            if d.is_file() and MARKER in d.read_text(errors="replace"):
                print("%sgh-limit: delete: %s" % (tag, d))
                if not whatif:
                    d.unlink()
        if with_profile and (pf := profile_path()):
            write_profile(pf, target.parent, True, tag, whatif)
        return 0
    real = real_gh()
    if not os.access(real, os.X_OK):
        print("gh-limit: WARNING: real gh not found (%s); shim not written" % real)
        return 1
    python = sys.executable if WINDOWS else (shutil.which("python3") or sys.executable)   # Windows: python3 is the Store stub
    script = Path(__file__).resolve()
    files = [(target, shim_text(script, real, python), 0o755)]
    if WINDOWS:
        files.append((ps1, shim_ps1_text(script, real, python), 0o644))
    for path, text, mode in files:
        if path.is_file() and MARKER in path.read_text(errors="replace") and path.read_text() == text.replace("\r\n", "\n"):
            print("%sgh-limit: same: %s" % (tag, path))
            continue
        if path.exists() and MARKER not in path.read_text(errors="replace"):
            print("gh-limit: WARNING: %s is another file, left untouched" % path)
            return 1
        print("%sgh-limit: write: %s (real gh: %s)" % (tag, path, real))
        if not whatif:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))   # bytes: the sh shim must keep LF on Windows
            path.chmod(mode)
    if WINDOWS:
        pf = profile_path() if with_profile else None
        if not with_profile:
            pass
        elif pf:
            write_profile(pf, target.parent, False, tag, whatif)
        else:
            print("gh-limit: note: pwsh profile not written (no pwsh, or the profile is outside the home directory); the shim only takes effect "
                  "when %s comes before gh.exe on PATH" % target.parent)
        return 0
    paths = os.environ.get("PATH", "").split(os.pathsep)
    if str(target.parent) not in paths or (os.path.dirname(real) in paths
                                           and paths.index(str(target.parent)) > paths.index(os.path.dirname(real))):
        print("gh-limit: WARNING: %s is not before %s on PATH; the shim does not take effect" % (target.parent, os.path.dirname(real)))
    return 0


def summary(minutes):
    limit = time.time() - minutes * 60
    lines = []
    for name in ("log.1", "log"):
        try:
            for s in (DIR / name).read_text(encoding="utf-8").splitlines():
                try:
                    k = json.loads(s)
                    t = time.mktime(time.strptime(k["t"][:19], "%Y-%m-%dT%H:%M:%S"))
                except (ValueError, KeyError):
                    continue
                if t >= limit:
                    k["_t"] = t
                    lines.append(k)
        except OSError:
            pass
    if not lines:
        print("gh-limit: no records in the last %d minutes (%s)" % (minutes, DIR / "log"))
        return 0
    call = [k for k in lines if not k.get("limited")]
    per_minute = {}
    for k in lines:
        per_minute[int(k["_t"] // 60)] = per_minute.get(int(k["_t"] // 60), 0) + 1
    by_endpoint, kind = {}, {}
    for k in call:
        by_endpoint[k["endpoint"]] = by_endpoint.get(k["endpoint"], 0) + 1
        kind[k["kind"]] = kind.get(k["kind"], 0) + 1
    limited = [k for k in lines if k.get("limited")]
    error = [k for k in call if k.get("status") in (403, 429)]
    wait = sorted(k.get("wait", 0) for k in call)
    print("gh-limit summary, last %d min: %d calls, %d rate-limit responses (retried), %d final 403/429, %d ETag 304" % (
        minutes, len(call), len(limited), len(error), sum(1 for k in call if k.get("cache"))))
    print("  per minute: avg %.1f, max %d" % (len(lines) / max(minutes, 1), max(per_minute.values())))
    print("  kind: " + ", ".join("%s %d" % x for x in sorted(kind.items(), key=lambda x: -x[1])))
    print("  wait (s): median %.2f, max %.2f" % (wait[len(wait) // 2], wait[-1]))
    print("  most called:")
    for u, n in sorted(by_endpoint.items(), key=lambda x: -x[1])[:12]:
        print("    %5d  %s" % (n, u))
    return 0


def main(argv):
    if os.environ.get("GH_LIMIT_SHIM") != "1" and argv[:1] in (["--install"], ["--remove"], ["--summary"]):
        if argv[0] == "--summary":
            per_minute = int(argv[argv.index("--minutes") + 1]) if "--minutes" in argv else 30
            return summary(per_minute)
        return install(remove=argv[0] == "--remove", whatif="--whatif" in argv)
    return run_cmd(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Lost-work scan: find git work that exists only on this machine or never reached the integration branch.

  python3 tools/lost_work.py                      # scan the folder next to this clone, table, exit 1 if own work is at risk
  python3 tools/lost_work.py --root ~/src --root D:\\work --depth 3
  python3 tools/lost_work.py --json | --brief | --hook | --no-fetch | --min-idle 1 | --push-unpushed | --notify | --cache-write

Per repo (every worktree of it) it reports:
  DIRTY     uncommitted or untracked files in a worktree
  UNPUSHED  commits on a local branch that exist on no remote branch (own = author email equals the repo's user.email)
  UNMERGED  local/origin branches with commits not in the integration branch (origin/dev if it exists, else the
            default branch); patch-equivalent commits (--cherry-pick) count as merged, and a branch whose merge
            into the base changes nothing counts as merged too (squash merges)
  ORPHAN-WT linked worktree whose work is already in the integration branch (or whose directory is gone)
  STASH     stash entries (shared by all worktrees of the repo)
  BEHIND    local integration branch behind its origin
  STALE     own UNMERGED branch whose first unique commit is older than STALE_DAY (3) working days (Mon-Fri, holidays ignored):
            by team rule it is merged to dev (green) or handed back to the board; shown first, with age and issue number
            parsed from the branch name ("issue yok" when it has none). Never `personal/*`.
  CLEANUP   own origin branches already merged and still present (delete candidates; never personal/*, the long-lived
            branch of a personal claude-shared variant)
Others' commits and branches are counted separately (author names only), never part of the exit code.

Exit code: 1 when own unpushed commits or dirty worktrees exist, else 0 (--hook always exits 0). STALE does not change it
(the daily job ignores it); it is `summary.stale` in --json.
`--min-idle H` ignores dirty/unpushed work touched in the last H hours (it is probably being worked on right now).
The default root (the folder holding this clone) includes the claude-shared clone itself, so the installer's session-start report
(tools/rules_watch.py shows the cached result; the daily job `--push-unpushed --cache-write` and a detached `--hook --cache-write` refresh write it) cover it,
`personal/*` branches included.
Read-only, except `--push-unpushed`, which pushes only branches whose unpushed commits are all your own (never someone else's work
under this machine's credentials). Deleting is limited to what is provably lossless and only with `--clean-lossless` (the daily job): a
clean linked worktree whose branch tip is an ancestor of origin/<base> or of an origin surum/* / release/* branch or equals
origin/<branch> (pushed; the origin branch stays), and any local branch whose tip is an ancestor of the base or a release branch
(`git worktree remove` without --force, then `git branch -D` after the check; squash merges are not proof and stay listed). Never the
main checkout, main/master/dev/test/next, surum/*, release/*, personal/*, yedek/*, someone else's branch, a dirty or locked
worktree, or anything touched in the last --clean-idle hours (default 2). Every removal is logged (repo, path, branch, tip SHA) and stored
in the result cache; everything else stays a report.

Ported from the private hourly scanner lost-work-scan.sh (same classes, same base order dev/main/master, same
`rev-list --not --remotes` test for unpushed, same idle idea), made cross-platform (no bash), with per-author
split, unmerged remote branches, squash detection and an optional push.

Run log: every run appends to a rolling log (1 MB files, 64 kept), default ~/.local/state/lost-work.log, override with
LOST_WORK_LOG. The scheduled task runs under pythonw (no stdout/stderr), so this log is the only trace of its results and
errors. --hook logs only start and summary. Never credentials; a log that cannot be written is ignored silently.

Trust model (same as rules_watch.py): branch names, author names and file names come from git and are untrusted.
They are stripped of control characters and length-capped; `--hook` prints them to the human only (`systemMessage`).
"""
import argparse
import concurrent.futures
import datetime
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # python -I drops the script dir
import notifier  # noqa: E402
from hidden_loader import hidden  # noqa: E402
import runlog  # noqa: E402
import lost_work_cache  # noqa: E402  (the result cache the session-start hook reads)

LOG = logging.getLogger("lost-work")      # tools/runlog.py: ~/.local/state/lost-work.log (env LOST_WORK_LOG), rolling 1 MB x 64
_VERBOSE = True  # --hook turns this off: only start and summary are logged
_GIT_CALLS = 0
_CACHE_RESULT = False  # --cache-write: this run owns the refresh lock and must release it however it ends
_GIT_LOCK = threading.Lock()
# Verdict cache for the expensive "is this branch really unmerged" test (cherry-pick filter + merge-tree), keyed by
# repo|ref|tip sha|base sha, so a verdict is exact and never stale. The session-start hook has an 8 s budget; without it a few
# big repos with old branches eat the budget on every start. A cache that cannot be read or written is ignored.
CACHE = {}
CACHE_SEEN = set()
CACHE_LOCK = threading.Lock()
CACHE_DIRTY = False


def cache_path():
    return Path(os.environ.get("LOST_WORK_CACHE") or Path.home() / ".local" / "state" / "lost-work-cache.json")


def cache_load():
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            CACHE.update({k: v for k, v in data.items() if isinstance(v, list)})
    except (OSError, ValueError):
        pass


def cache_save():
    """Keeps only entries seen in this run (a branch that is gone or moved drops out), plus entries of repos not scanned."""
    if not CACHE_DIRTY:
        return
    try:
        scanned = {k.split("|", 1)[0] for k in CACHE_SEEN}
        keep = {k: v for k, v in CACHE.items() if k in CACHE_SEEN or k.split("|", 1)[0] not in scanned}
        lost_work_cache.atomic_write(cache_path(), json.dumps(keep))
    except OSError:
        pass


def init_log():
    """Attach the shared rolling file handler (tools/runlog.py); any failure leaves logging off and the scan untouched."""
    runlog.runlog("lost-work")


def log(msg, *args, always=True, level=logging.INFO):
    """Write one log line; never raises. always=False lines are skipped under --hook."""
    if not (always or _VERBOSE):
        return
    try:
        LOG.log(level, msg, *args)
    except Exception:
        pass


def short_err(text, limit=200):
    return clean(runlog.mask_out(text or ""), limit)


def log_git_failure(cwd, args, why):
    log("git %s failed repo=%s: %s", args[0] if args else "?", clean(cwd, 200), why, always=False, level=logging.WARNING)


REPO_ROOT = Path(__file__).resolve().parent.parent
PROTECTED = {"dev", "test", "master", "main", "next"}
SKIP_DIRS = {"node_modules", "bin", "obj", "venv", "__pycache__"}
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_ASKPASS": "", "LC_ALL": "C"}   # never a prompt or a credential window


# A scanned repo's own config must not run code: command-line -c beats .git/config (fsmonitor, hooks, ssh, pager,
# external diff, credential helpers, ext:: transport).
SAFE_GIT = ["-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", "-c", "core.sshCommand=ssh",
            "-c", "core.pager=cat", "-c", "core.editor=true", "-c", "diff.external=", "-c", "credential.helper=",
            "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=user", "-c", "gc.auto=0",
            "-c", "maintenance.auto=false", "-c", "fetch.fsckObjects=false"]


def _user_credential_config():
    """credential.*.helper entries of the system and global config, re-added after the empty reset above in file
    order, empty values included: an empty credential.helper also clears URL-scoped helpers, and the empty
    `credential.<url>.helper=` lines that `gh auth setup-git` writes are the resets that keep generic helpers
    away from that URL. Repo-local config is never read here."""
    out = []
    for scope in ("--system", "--global"):
        r = hidden.run(["git", "config", scope, "-z", "--get-regexp", r"^credential\."], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=ENV, stdin=subprocess.DEVNULL)
        for entry in r.stdout.split("\0"):  # -z: "key\nvalue" (value may be empty, "key" alone also occurs)
            key, _, val = entry.partition("\n")
            if key.endswith(".helper") and val.strip() != "helper-selector":  # helper-selector opens a GUI dialog
                out += ["-c", f"{key}={val}"]
    return out


def run_git(argv, timeout):
    """subprocess.run replacement that kills the whole process tree on timeout (git-remote-https -> sh -> credential
    helper grandchildren would otherwise stay orphaned). Raises subprocess.TimeoutExpired like subprocess.run.
    No console window per git call (tools/hidden.py)."""
    global _GIT_CALLS
    with _GIT_LOCK:
        _GIT_CALLS += 1
    win = os.name == "nt"
    p = hidden.popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                         errors="replace", env=ENV, stdin=subprocess.DEVNULL,
                         **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                            if win else {"start_new_session": True}))
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if win:
            hidden.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, check=False)
        else:
            try:
                os.killpg(p.pid, 9)
            except OSError:
                pass
        p.kill()
        p.communicate()
        raise
    return subprocess.CompletedProcess(argv, p.returncode, out, err)


SAFE_GIT += _user_credential_config()


FETCH_SLOTS = threading.BoundedSemaphore(1)  # replaced by main() from --fetch-jobs
RAW_PATH = {}  # cleaned report path -> real path (only the real one may reach git)


def clean(text, limit=120):
    text = _CTRL.sub(" ", str(text)).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def git(cwd, *args, timeout=30, raw=False):
    """stdout of a git command, or None on failure/timeout (empty output is "")."""
    try:
        r = run_git(["git", "--no-optional-locks", *SAFE_GIT, "-C", str(cwd), *args], timeout)
    except subprocess.TimeoutExpired:
        log_git_failure(cwd, args, f"timeout after {timeout}s")
        return None
    except OSError as e:
        log_git_failure(cwd, args, short_err(e))
        return None
    if r.returncode != 0:
        # rc 1 with empty stderr is a plain "no" (rev-parse -q --verify, merge-base --is-ancestor), not a failure
        if r.returncode > 1 or r.stderr.strip():
            log_git_failure(cwd, args, f"rc={r.returncode} {short_err(r.stderr)}")
        return None
    return r.stdout if raw else r.stdout.strip("\n")


def fetch_origin(repo, timeout):
    """None when `git fetch --prune origin` succeeded, else a short cleaned reason (credentials masked)."""
    if lost_work_cache.fetch_fresh(repo):   # rules_watch or another fetch got it a moment ago
        return None
    argv = ["git", "--no-optional-locks", *SAFE_GIT, "-C", str(repo), "fetch", "--prune", "--quiet", "origin"]
    try:
        r = run_git(argv, timeout)
    except subprocess.TimeoutExpired:
        return f"timeout after {timeout}s"
    except OSError as e:
        return clean(e, 120)
    if r.returncode == 0:
        return None
    err = [x.strip() for x in r.stderr.splitlines() if x.strip()]
    fatal = [x for x in err if x.startswith(("fatal:", "error:"))]
    reason = (fatal or err or [f"exit code {r.returncode}"])[0]
    return clean(re.sub(r"://[^/\s@]+@", "://***@", reason), 120)


def lines(text):
    return [x for x in (text or "").split("\n") if x]


def day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(int(ts))) if ts else ""


STALE_DAY = 3  # an own branch lives at most this many working days (rules/WORKING-RULES.md "No lost work")
ISSUE_RE = re.compile(r"(?:^|[/#])(?:gh-|issue-)?(\d{1,6})(?=[-_/]|$)")
BOT_BRANCH = ("dependabot/", "renovate/")
KEPT_BRANCH = ("main", "master", "dev", "test", "gh-pages")
KEPT_PREFIX = ("release/", "personal/", "yedek/")


def issue_of(branch):
    """Issue number in a branch name (`duzeltme/2403-fix` -> 2403, `fix/#17` -> 17), else None. 1-6 digits, at the start
    of a path segment (a date like 20261106 or a version `v2` is no issue)."""
    m = ISSUE_RE.search(branch or "")
    return int(m.group(1)) if m else None


def working_days_since(start_ts, now_ts=None):
    """Mon-Fri days after the local date of `start_ts` up to and including the local date of `now_ts` (holidays ignored)."""
    a = datetime.date.fromtimestamp(int(start_ts))
    b = datetime.date.fromtimestamp(int(now_ts if now_ts is not None else time.time()))
    n = (b - a).days
    if n <= 0:
        return 0
    full, rest = divmod(n, 7)
    return full * 5 + sum(1 for i in range(1, rest + 1) if (a + datetime.timedelta(days=i)).weekday() < 5)


def work_stale(start_ts, now_ts=None):
    return bool(start_ts) and working_days_since(start_ts, now_ts) > STALE_DAY


def kept_branch(name):
    """Branches no job may delete or call stale: the long-lived ones (main/master/dev/test, release/*, personal/*, yedek/*)."""
    return name in KEPT_BRANCH or name.startswith(KEPT_PREFIX)


def bot_dal(name):
    return name.startswith(BOT_BRANCH)


# ---- discovery ---------------------------------------------------------------------------------

def find_git_dirs(root, depth):
    """Directories holding a `.git` (dir or file) at most `depth` levels under root; never descends into a repo."""
    found = []

    def walk(d, level):
        if (d / ".git").exists():
            found.append(d)
            return
        if level >= depth:
            return
        try:
            subs = sorted((e for e in os.scandir(d) if e.is_dir(follow_symlinks=False)), key=lambda e: e.name)
        except OSError:
            return
        for e in subs:
            if not e.name.startswith(".") and e.name not in SKIP_DIRS:
                walk(Path(e.path), level + 1)

    walk(Path(root).expanduser(), 0)
    return found


def group_repos(dirs):
    """{common git dir: first directory}; linked worktrees found next to the clone fold into their repo."""
    groups = {}
    for d in dirs:
        common = git(d, "rev-parse", "--git-common-dir")
        if not common:
            continue
        key = os.path.normcase(os.path.realpath(os.path.join(str(d), common)))
        groups.setdefault(key, d)
    return groups


def worktrees(repo):
    """Entries of `git worktree list --porcelain`: dicts with path/head/branch/detached/prunable/bare."""
    out, cur = [], None
    for ln in (git(repo, "worktree", "list", "--porcelain") or "").split("\n") + [""]:
        if not ln:
            if cur:
                out.append(cur)
            cur = None
        elif ln.startswith("worktree "):
            cur = {"path": ln[9:], "head": "", "branch": None, "detached": False, "prunable": False, "bare": False}
        elif cur is not None:
            if ln.startswith("HEAD "):
                cur["head"] = ln[5:]
            elif ln.startswith("branch refs/heads/"):
                cur["branch"] = ln[18:]
            elif ln == "detached":
                cur["detached"] = True
            elif ln.startswith("prunable"):
                cur["prunable"] = True
            elif ln == "bare":
                cur["bare"] = True
    return out


# ---- per-repo analysis -------------------------------------------------------------------------

def integration_branch(repo):
    """Name of the integration branch on origin: dev if it exists, else origin's default, else main/master."""
    def has(b):
        return git(repo, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{b}^{{commit}}")
    if has("dev"):
        return "dev"
    head = git(repo, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    if head and head.startswith("origin/") and has(head[7:]):
        return head[7:]
    return next((b for b in ("main", "master") if has(b)), None)


def commits_of(repo, spec, *extra):
    """[(email, name, ct, at)] for `git log` with the given revision args (committer and author time)."""
    out = git(repo, "log", "--format=%ae%x09%an%x09%ct%x09%at", *extra, *spec, timeout=60)
    res = []
    for ln in lines(out):
        e, n, c, a = (ln.split("\t") + ["", "", ""])[:4]
        res.append((e.lower(), clean(n, 60), int(c or 0), int(a or 0)))
    return res


def first_commit_ts(commits, email):
    """Author time of the oldest own commit (the branch's first unique commit; a rebase moves the committer time, not this)."""
    return min((c[3] or c[2] for c in commits if not email or c[0] == email), default=0)


def split_own(commits, email):
    """(own count, others count, other author names, oldest own ct, newest ct); no email set = all own."""
    own = [c for c in commits if not email or c[0] == email]
    oth = [c for c in commits if email and c[0] != email]
    return (len(own), len(oth), sorted({c[1] for c in oth}), min((c[2] for c in own), default=0),
            max((c[2] for c in commits), default=0))


def dirty_state(wt, head_ct, ignore):
    """(file count, newest mtime) of a worktree; untracked paths under an `ignore` prefix do not count."""
    out = git(wt, "status", "--porcelain", "-z", timeout=60, raw=True)
    if not out:
        return 0, 0
    ents, newest, count, i = out.split("\0"), head_ct, 0, 0
    while i < len(ents):
        e = ents[i]
        i += 1
        if len(e) < 4:
            continue
        xy, fp = e[:2], e[3:]
        if xy[0] in "RC":
            i += 1  # the rename/copy source follows as its own field
        if xy == "??" and any(fp.startswith(p) for p in ignore):
            continue
        count += 1
        try:
            newest = max(newest, int(os.stat(os.path.join(wt, fp)).st_mtime))
        except OSError:
            pass
    return count, newest


def analyse(repo, fetch, fetch_timeout, min_idle=0.0, ignore=(), deep=True, stale_only=False):
    """deep=False is the --hook scan; stale_only then still looks at own unmerged branches, but only the ones old enough to be STALE."""
    now = time.time()
    wts = [w for w in worktrees(repo) if not w["bare"]]
    if not wts:
        return None
    main_path = wts[0]["path"]
    RAW_PATH[clean(main_path, 200)] = main_path
    res = {"repo": clean(Path(main_path).name, 80), "path": clean(main_path, 200), "fetch": "skipped", "fetch_error": "",
           "email": "",
           "integration": None, "dirty": [], "stashes": 0, "unpushed": [], "unmerged": [], "cleanup": [],
           "orphan_worktrees": [], "behind": None, "others": {"unmerged": 0, "authors": [], "branches": []}}
    remotes = lines(git(repo, "remote"))
    if "origin" not in remotes:
        res["fetch"] = "no-remote" if not remotes else "no-origin"
    elif fetch:
        with FETCH_SLOTS:  # fetches run one at a time (or --fetch-jobs N), never all repos at once
            t0 = time.monotonic()
            err = fetch_origin(repo, fetch_timeout)
        log("fetch repo=%s %s %.1fs", clean(repo, 200), f"failed ({err})" if err else "ok", time.monotonic() - t0,
            always=False)
        res["fetch"] = "failed" if err else "ok"
        res["fetch_error"] = err or ""
    email = (git(repo, "config", "user.email") or "").strip().lower()
    res["email"] = email
    base = integration_branch(repo)
    res["integration"] = base
    bref = f"refs/remotes/origin/{base}" if base else None

    # dirty files per worktree (with idle time), stashes
    for w in wts:
        if w["prunable"] or not os.path.isdir(w["path"]):
            continue
        head_ct = int(git(w["path"], "log", "-1", "--format=%ct") or 0)
        files, newest = dirty_state(w["path"], head_ct, ignore)
        idle = max(0.0, (now - newest) / 3600) if newest else 0.0
        if files and idle >= min_idle:
            res["dirty"].append({"path": clean(w["path"], 200), "branch": clean(w["branch"] or "(detached)"), "files": files,
                                 "idle_hours": round(idle, 1)})
    res["stashes"] = len(lines(git(repo, "stash", "list")))

    # local branches: unpushed
    locals_ = lines(git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads"))
    def remote_has(b):
        return bool(git(repo, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{b}^{{commit}}"))
    for b in sorted(locals_):
        cs = commits_of(repo, [f"refs/heads/{b}", "--not", "--remotes"])
        if not cs:
            continue
        own, oth, names, since, last = split_own(cs, email)
        idle = max(0.0, (now - last) / 3600)
        if idle < min_idle:
            continue
        has_remote = remote_has(b)
        diverged = has_remote and git(repo, "merge-base", "--is-ancestor", f"refs/remotes/origin/{b}",
                                      f"refs/heads/{b}") is None
        res["unpushed"].append({"branch": clean(b), "own": own, "others": oth, "other_authors": names,
                                "since": day(since), "last": day(last), "idle_hours": round(idle, 1),
                                "diverged": bool(diverged), "protected": b in PROTECTED or b == base,
                                "no_remote": not has_remote})
    if base and base in locals_ and bref:
        behind = git(repo, "rev-list", "--count", f"refs/heads/{base}..{bref}")
        if behind and int(behind) > 0:
            res["behind"] = {"branch": base, "commits": int(behind)}

    if bref and (deep or stale_only):
        unm = {}  # branch name -> row; local and origin copies fold into one row
        # tips not contained in base: one cheap ref listing instead of an ancestry call per branch
        out = git(repo, "for-each-ref", "--no-merged", bref, "--format=%(refname)\t%(authoremail)\t%(objectname)",
                  "refs/heads", "refs/remotes/origin")
        tip_sha = {}
        unmerged_refs = {}
        for ln in lines(out):
            parts = ln.split("\t")
            if len(parts) >= 2:
                unmerged_refs[parts[0]] = parts[1].strip("<>").lower()
                tip_sha[parts[0]] = parts[2] if len(parts) > 2 else ""
        base_sha = base_tree = ""
        if unmerged_refs:  # base commit and tree in one call, only when there is something to judge
            base_sha, base_tree = (lines(git(repo, "rev-parse", bref, f"{bref}^{{tree}}")) + ["", ""])[:2]
        all_remote = lines(git(repo, "for-each-ref", "--format=%(refname)\t%(authoremail)", "refs/remotes/origin")) if deep else []

        def skip(b):
            return b in PROTECTED or b == base or b == "HEAD" or b.startswith("yedek/")

        def keep(b):
            return b.startswith("personal/")  # long-lived personal variant (README "Your own version"), never a delete candidate

        def check(ref, b, where, tip_email):
            """Record `ref` as unmerged when it has commits that are not patch-equivalent in base."""
            if email and tip_email != email and where == "origin":
                if not deep:
                    return
                # someone else's branch: counted by ancestry only (their commits are none of our business)
                res["others"]["unmerged"] += 1
                res["others"]["branches"].append(clean(b))
                # the tip can vanish between the ref listing and this log (parallel `fetch --prune`, timeout):
                # git() gives None, commits_of() gives [] and the author is then unknown, never an IndexError
                tip = commits_of(repo, [ref], "-1")
                if tip:
                    res["others"]["authors"] = sorted(set(res["others"]["authors"]) | {tip[0][1]})
                return
            key = f"{res['path']}|{ref}|{tip_sha.get(ref, '')}|{base_sha}" if tip_sha.get(ref) and base_sha else None
            with CACHE_LOCK:
                hit = CACHE.get(key) if key else None
                if key:
                    CACHE_SEEN.add(key)
            if hit is not None:
                cs = [tuple(c) for c in hit]
            else:
                if not deep:  # --hook: cheap age test first (cherry-pick filtering below only ever makes a branch younger)
                    if not work_stale(first_commit_ts(commits_of(repo, [f"{bref}..{ref}"], "--no-merges"), email), now):
                        return
                cs = commits_of(repo, [f"{bref}...{ref}"], "--right-only", "--cherry-pick", "--no-merges")
                if cs:
                    mt = git(repo, "merge-tree", "--write-tree", bref, ref)  # git >= 2.38; None on older git
                    if mt is not None and mt.split("\n")[0] == base_tree:
                        cs = []  # merging it changes nothing: squash-merged
                if key:
                    global CACHE_DIRTY
                    with CACHE_LOCK:
                        CACHE[key] = [list(c) for c in cs]
                        CACHE_DIRTY = True
            if not cs:
                return
            row = unm.setdefault(b, {"branch": clean(b), "where": [], "commits": []})
            row["where"].append(where)
            row["commits"] = max(row["commits"], cs, key=len)

        for ref, tip_email in unmerged_refs.items():
            if ref.startswith("refs/heads/") and not skip(ref[11:]):
                check(ref, ref[11:], "local", tip_email)
            elif ref.startswith("refs/remotes/origin/") and not skip(ref[20:]):
                check(ref, ref[20:], "origin", tip_email)
        for ln in all_remote:
            ref, _, tip_email = ln.partition("\t")
            b = ref[20:]
            if ref.startswith("refs/remotes/origin/") and ref not in unmerged_refs and not skip(b) and not keep(b) \
                    and (not email or tip_email.strip("<>").lower() == email):
                res["cleanup"].append(clean(b))  # own tip, already in base, still on origin
        for row in unm.values():
            own, oth, names, _, last = split_own(row["commits"], email)
            if own:
                first = first_commit_ts(row["commits"], email)
                wd = working_days_since(first, now) if first else 0
                res["unmerged"].append({"branch": row["branch"], "where": sorted(set(row["where"])), "own": own,
                                        "others": oth, "last": day(last), "first": day(first), "age_days": max(0, int((now - first) // 86400)) if first else 0,
                                        "working_days": wd, "issue": issue_of(row["branch"]),
                                        "stale": wd > STALE_DAY and not kept_branch(row["branch"])})
            elif deep:
                res["others"]["unmerged"] += 1
                res["others"]["authors"] = sorted(set(res["others"]["authors"]) | set(names))
                res["others"]["branches"].append(row["branch"])

    # linked worktrees whose work is already in base
    for w in (wts[1:] if deep else []):
        reason = None
        if w["prunable"] or not os.path.isdir(w["path"]):
            reason = "directory missing"
        elif bref and w["head"] and git(repo, "merge-base", "--is-ancestor", w["head"], bref) is not None:
            reason = f"merged into origin/{base}"
        if reason:
            res["orphan_worktrees"].append({"path": clean(w["path"], 200), "branch": clean(w["branch"] or "(detached)"),
                                            "reason": reason,
                                            "dirty": any(d["path"] == clean(w["path"], 200) for d in res["dirty"])})
    res["unpushed"].sort(key=lambda r: (-r["own"], r["branch"]))
    res["unmerged"].sort(key=lambda r: (-r["stale"], -r["working_days"], r["branch"]))
    return res


# ---- push --------------------------------------------------------------------------------------

def push_unpushed(repo_res, timeout=60):
    """Push own unpushed feature branches under the same name; diverged -> yedek/<host>-<branch>-<date>. Never force.
    A branch whose unpushed commits include someone else's is not pushed (reported as skipped): this machine's credentials
    must not publish another person's work."""
    repo = RAW_PATH.get(repo_res["path"], repo_res["path"])
    host = re.sub(r"[^A-Za-z0-9._-]", "-", platform.node().split(".")[0]) or "host"
    stamp = time.strftime("%Y%m%d")
    done = []
    if repo_res["fetch"] in ("no-remote", "no-origin"):
        return done
    for u in repo_res["unpushed"]:
        if u["own"] == 0 or u["protected"]:
            continue
        b = u["branch"]
        if u["others"]:
            log("push repo=%s %s skipped: %s", clean(repo, 200), clean(b), f"{u['others']} unpushed commit(s) by others",
                always=False)
            done.append({"repo": clean(repo_res["repo"], 80), "branch": clean(b), "pushed_as": "", "ok": False, "backup": False,
                         "skipped": f"{u['others']} unpushed commit(s) by others"})
            continue
        target = b
        if u["diverged"]:
            target = f"yedek/{host}-{b.replace('/', '-')}-{stamp}"
            if git(repo, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{target}"):
                target += time.strftime("-%H%M")
        # no "+" and no --force: a non-fast-forward push is refused by git itself
        ok = git(repo, "push", "--quiet", "origin", f"refs/heads/{b}:refs/heads/{target}", timeout=timeout) is not None
        log("push repo=%s %s -> %s %s", clean(repo, 200), clean(b), clean(target), "ok" if ok else "FAILED", always=False)
        done.append({"repo": clean(repo_res["repo"], 80), "branch": clean(b), "pushed_as": clean(target), "ok": ok, "backup": target != b,
                     "skipped": ""})
    return done


# ---- lossless cleanup --------------------------------------------------------------------------

CLEAN_IDLE_H = 2.0   # a worktree or branch touched within this many hours may be in use by an agent: never cleaned
RELEASE_REFS = ("refs/remotes/origin/surum", "refs/remotes/origin/release")   # origin branches that also count as "already shipped"


def lossless_protected(name):
    """Branches the cleanup never deletes: the long-lived ones (main/master/dev/test/next, release/*, surum/*, personal/*, yedek/*)."""
    return name in PROTECTED or kept_branch(name) or name.startswith("surum/")


def contained_in_base(repo, sha, bref):
    """True when commit `sha` is an ancestor of the repo's base on origin or of any origin surum/* / release/* branch (plain ancestry:
    a squash merge is not proof, so such a branch stays and is only reported)."""
    out = git(repo, "for-each-ref", "--contains", sha, "--format=%(refname)", bref, *RELEASE_REFS)
    return bool(out)


def newest_touch(wt):
    """Epoch seconds of the latest sign of life of a worktree: its folder, HEAD, index and HEAD reflog in its git dir, and the HEAD commit."""
    newest = int(git(wt, "log", "-1", "--format=%ct") or 0)
    gd = git(wt, "rev-parse", "--absolute-git-dir")
    for p in [wt] + ([os.path.join(gd, n) for n in ("HEAD", "index", os.path.join("logs", "HEAD"))] if gd else []):
        try:
            newest = max(newest, int(os.stat(p).st_mtime))
        except OSError:
            pass
    return newest


def clean_lossless(repo_res, idle_h=CLEAN_IDLE_H):
    """Remove what cannot lose work: a linked worktree with a clean tree (no changes, no untracked files; ignored files do not count) and
    then its local branch, when its tip is contained in the base or a surum/*/release/* branch on origin, OR equals origin/<branch>
    (pushed: the work is safe on origin and a worktree is only a local checkout; the origin branch stays), and any other local branch
    whose tip is contained in the base or a release branch.
    Never: the main checkout, protected branches (lossless_protected), a branch whose origin copy is someone else's (tip author is not
    this repo's user.email), anything touched within `idle_h` hours, a dirty or locked worktree (git refuses itself, no --force).
    Pushing runs first (push_unpushed), so own unpushed work is never in here. Returns [{repo, path, branch, sha, kind}]."""
    repo = RAW_PATH.get(repo_res["path"], repo_res["path"])
    base, email = repo_res["integration"], repo_res["email"]
    if repo_res["fetch"] in ("no-remote", "no-origin") or not base or not email:
        return []
    bref = f"refs/remotes/origin/{base}"
    if not git(repo, "rev-parse", "-q", "--verify", f"{bref}^{{commit}}"):
        return []
    now = time.time()
    name = clean(repo_res["repo"], 80)
    done = []

    def foreign(b):   # someone else's branch checked out here: the tip author of its origin copy is not this user
        who = (git(repo, "log", "-1", "--format=%ae", f"refs/remotes/origin/{b}") or "").strip().lower()
        return bool(who) and who != email

    def drop_branch(b, sha, kind_of):
        if git(repo, "rev-parse", "-q", "--verify", f"refs/heads/{b}^{{commit}}") != sha:   # moved since the check
            return
        if git(repo, "branch", "-D", b) is not None:   # git itself refuses a branch that is checked out somewhere
            done.append({"repo": name, "path": "", "branch": clean(b), "sha": sha, "kind": "branch"})
            log("clean repo=%s branch %s %s removed (%s)", clean(repo, 200), clean(b), sha, kind_of, always=False)

    wts = [w for w in worktrees(repo) if not w["bare"]]
    for w in wts[1:]:   # [0] is the main checkout: never touched
        b = w["branch"]
        if w["prunable"] or not os.path.isdir(w["path"]) or not w["head"] or (b and (lossless_protected(b) or foreign(b))):
            continue
        merged = contained_in_base(repo, w["head"], bref)
        pushed = bool(b) and git(repo, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{b}^{{commit}}") == w["head"]
        if not (merged or pushed) or now - newest_touch(w["path"]) < idle_h * 3600:
            continue
        status = git(w["path"], "status", "--porcelain", "--untracked-files=all", raw=True)   # None = could not tell = not clean
        if status is None or status.strip():
            continue
        if git(repo, "worktree", "remove", w["path"]) is None:   # no --force: a locked or changed worktree is refused
            continue
        why = "merged" if merged else "pushed"   # pushed: not in the base yet, but the branch tip is on origin
        done.append({"repo": name, "path": clean(w["path"], 200), "branch": clean(b or "(detached)"), "sha": w["head"], "kind": "worktree",
                     "why": why})
        log("clean repo=%s worktree %s %s %s removed (%s)", clean(repo, 200), clean(w["path"], 200), clean(b or "(detached)"), w["head"],
            "worktree kaldırıldı (dal origin'de)" if why == "pushed" else "worktree kaldırıldı (base'de)", always=False)
        if b:
            drop_branch(b, w["head"], "worktree gone")

    in_use = {w["branch"] for w in worktrees(repo) if w["branch"]}
    heads = {}
    for ln in lines(git(repo, "for-each-ref", "--format=%(refname)%09%(objectname)", "refs/heads")):
        ref, _, sha = ln.partition("\t")
        heads[ref[len("refs/heads/"):]] = sha
    for b, sha in sorted(heads.items()):
        if lossless_protected(b) or b in in_use or not sha or foreign(b) or not contained_in_base(repo, sha, bref):
            continue
        stamp = max(int(git(repo, "log", "-g", "-1", "--format=%ct", f"refs/heads/{b}") or 0),
                    int(git(repo, "log", "-1", "--format=%ct", f"refs/heads/{b}") or 0))
        if now - stamp < idle_h * 3600:
            continue
        drop_branch(b, sha, "contained in base")
    return done


# ---- reporting ---------------------------------------------------------------------------------

def summarize(repos):
    s = {"repos": len(repos), "dirty_worktrees": 0, "dirty_files": 0, "unpushed_branches": 0, "unpushed_own": 0,
         "unpushed_others": 0, "unmerged_branches": 0, "orphan_worktrees": 0, "stashes": 0, "behind": 0,
         "cleanup": 0, "others_unmerged": 0, "fetch_failed": 0, "repos_at_risk": 0, "stale": 0}
    for r in repos:
        own_unpushed = [u for u in r["unpushed"] if u["own"]]
        s["dirty_worktrees"] += len(r["dirty"])
        s["dirty_files"] += sum(d["files"] for d in r["dirty"])
        s["unpushed_branches"] += len(own_unpushed)
        s["unpushed_own"] += sum(u["own"] for u in own_unpushed)
        s["unpushed_others"] += sum(u["others"] for u in r["unpushed"])
        s["unmerged_branches"] += len(r["unmerged"])
        s["stale"] += sum(1 for m in r["unmerged"] if m["stale"])
        s["orphan_worktrees"] += len(r["orphan_worktrees"])
        s["stashes"] += r["stashes"]
        s["behind"] += 1 if r["behind"] else 0
        s["cleanup"] += len(r["cleanup"])
        s["others_unmerged"] += r["others"]["unmerged"]
        s["fetch_failed"] += 1 if r["fetch"] == "failed" else 0
        s["repos_at_risk"] += 1 if (own_unpushed or r["dirty"]) else 0
    s["at_risk"] = bool(s["unpushed_own"] or s["dirty_worktrees"])
    return s


def rows_of(r):
    """[(kind, where, detail)] for one repo."""
    rows = []
    no_remote = r["fetch"] in ("no-remote", "no-origin")
    for d in r["dirty"]:
        rows.append(("DIRTY", d["branch"], f"{d['files']} file(s), idle {d['idle_hours']}h, in {d['path']}"))
    for u in r["unpushed"]:
        if not u["own"] and not u["others"]:
            continue
        how = "no remote" if no_remote else ("diverged from origin" if u["diverged"] else
                                             ("never pushed" if u["no_remote"] else "ahead of origin"))
        if u["own"]:
            extra = f" (+{u['others']} by others)" if u["others"] else ""
            rows.append(("UNPUSHED", u["branch"], f"{u['own']} own commit(s){extra}, since {u['since']}, {how}"))
        else:
            rows.append(("unpushed-others", u["branch"], f"{u['others']} commit(s) by {', '.join(u['other_authors'][:3])}"))
    for m in r["unmerged"]:
        if m["stale"]:
            continue  # listed first, in the STALE block (stale_lines)
        rows.append(("UNMERGED", m["branch"], f"{m['own']} own commit(s), last {m['last']}, on {'+'.join(m['where'])}"))
    for o in r["orphan_worktrees"]:
        rows.append(("ORPHAN-WT", o["branch"], f"{o['reason']}{', still dirty' if o['dirty'] else ''}: {o['path']}"))
    if r["stashes"]:
        rows.append(("STASH", "-", f"{r['stashes']} entr{'y' if r['stashes'] == 1 else 'ies'}"))
    if r["behind"]:
        rows.append(("BEHIND", r["behind"]["branch"], f"local is {r['behind']['commits']} commit(s) behind origin"))
    if r["cleanup"]:
        rows.append(("CLEANUP", "-", f"{len(r['cleanup'])} own origin branch(es) already merged: "
                     + ", ".join(r["cleanup"][:3]) + (" ..." if len(r["cleanup"]) > 3 else "")))
    if r["others"]["unmerged"]:
        rows.append(("others", "-", f"{r['others']['unmerged']} unmerged origin branch(es) by "
                     + ", ".join(r["others"]["authors"][:4])))
    if r["fetch"] == "failed":
        rows.append(("FETCH-FAILED", "-", f"refs may be stale: {r['fetch_error'] or 'unknown error'}"))
    return rows


def stale_lines(repos, limit=None):
    """Stale own branches (STALE), oldest first: `repo  branch  Nd (W working days)  issue #N | issue yok`."""
    items = sorted(((r, m) for r in repos for m in r["unmerged"] if m["stale"]),
                   key=lambda x: (-x[1]["working_days"], x[0]["repo"], x[1]["branch"]))
    out = [f"  {clean(r['repo'], 40)}  {clean(m['branch'], 60)}  {m['age_days']}d ({m['working_days']} working days, first commit {m['first']})  "
           + (f"issue #{m['issue']}" if m["issue"] else "issue yok") for r, m in items]
    return out[:limit] if limit else out


def summary_line(s):
    risk = []
    if s["unpushed_own"]:
        risk.append(f"{s['unpushed_own']} own unpushed commit(s) on {s['unpushed_branches']} branch(es)")
    if s["dirty_worktrees"]:
        risk.append(f"{s['dirty_worktrees']} dirty worktree(s)")
    other = [f"{s[k]} {label}" for k, label in (
        ("stale", f"STALE branch(es) (own, unmerged for more than {STALE_DAY} working days)"),
        ("unmerged_branches", "unmerged own branch(es)"), ("orphan_worktrees", "orphan worktree(s)"),
        ("stashes", "stash(es)"), ("cleanup", "merged origin branch(es) to delete"),
        ("fetch_failed", "repo(s) with failed fetch (refs stale)")) if s[k]]
    head = "AT RISK: " + "; ".join(risk) if risk else "No unpushed or uncommitted own work"
    return f"{head}{'; also ' + ', '.join(other) if other else ''} ({s['repos']} repos scanned)"


def risky_lines(repos):
    out = []
    for r in sorted(repos, key=lambda x: -sum(u["own"] for u in x["unpushed"])):
        un = sum(u["own"] for u in r["unpushed"])
        if un or r["dirty"]:
            out.append(f"  {clean(r['repo'], 80)}: {un} unpushed, {len(r['dirty'])} dirty worktree(s)")
    return out


def hook_message(summ, repos, skipped=0, n_repos=0, budget=0):
    """The session-start report (human text): at-risk repos, STALE count and the oldest 5, and a note when the scan was cut short.
    "" when nothing is at risk, nothing is stale and nothing was skipped. The ONE place this text is built: --hook prints it,
    --cache-write stores it for tools/rules_watch.py."""
    details = (f"Details: python3 {REPO_ROOT.joinpath('tools', 'lost_work.py').as_posix()}"
               " (--push-unpushed pushes own branches, never main/master/test/dev, never force)")
    # a scan cut short by the budget is never silent: unscanned repos may hold work at risk
    partial = (f"Partial scan: {skipped} of {n_repos} repos not scanned within {budget} s; run the full scan."
               if skipped > 0 else "")
    stale = ""
    if summ["stale"]:
        stale = (f"Stale branches: {summ['stale']} own branch(es) unmerged for more than {STALE_DAY} working days"
                 " (oldest 5; merge to dev if green and still wanted, or hand back):\n" + "\n".join(stale_lines(repos, 5)))
    if summ["at_risk"] or stale:
        return "Lost-work check: " + summary_line(summ) + ("\n" + "\n".join(risky_lines(repos)[:5]) if summ["at_risk"] else "") \
               + ("\n" + stale if stale else "") + ("\n" + partial if partial else "") + "\n" + details
    if partial:
        return "Lost-work check: " + partial + "\n" + details
    return ""


def print_table(repos, summ):
    bl = stale_lines(repos)
    if bl:
        print(f"STALE: {len(bl)} own branch(es) unmerged for more than {STALE_DAY} working days "
              "(merge to dev if green and still wanted, or hand back to the board):")
        print("\n".join(bl))
    for r in sorted(repos, key=lambda x: x["repo"].lower()):
        rows = rows_of(r)
        if rows:
            print(f"\n{clean(r['repo'], 80)}  (base origin/{clean(r['integration'] or '?', 60)}, "
                  f"{clean(r['email']) or 'no user.email'})")
            for kind, where, detail in rows:
                print(f"  {kind:<15} {clean(where, 40):<40} {clean(detail, 300)}")
    print(f"\n{summary_line(summ)}")


def notify(text):
    notifier.toast("Lost work", text, "lost-work")  # shared helper: Windows toast / notify-send, never a modal box


def main():
    t0 = time.monotonic()
    init_log()
    code = 1
    try:
        code = run()
        return code
    except SystemExit as e:  # argparse errors, explicit exits
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        raise
    except BaseException:
        try:
            LOG.exception("uncaught exception")
        except Exception:
            pass
        raise
    finally:
        if _CACHE_RESULT:
            lost_work_cache.release_lock()
        log("end exit=%s git_calls=%d duration=%.1fs", code, _GIT_CALLS, time.monotonic() - t0, always=False)


def take_refresh_lock():
    """--cache-write: ONE refresh at a time across the session hook's background run and the daily job (same lock, tools/lost_work_cache.py).
    The hook's background run (rules_watch.py --refresh) holds the lock itself and says so with LOST_LOCK_OWNER=1: nothing to take
    and nothing to release here (False). Otherwise wait up to 5 minutes for a running refresh, then take the lock (a lock older than
    lost_work_cache.LOCK_MAX is taken over); True = this run owns it and releases it at the end. After the wait the scan runs anyway
    (a missed daily run costs more than two scans at once), without owning the lock."""
    if os.environ.get("LOST_LOCK_OWNER") == "1":
        return False
    end = time.time() + 300
    while not lost_work_cache.take_lock():
        if time.time() > end:
            return False
        time.sleep(5)
    return True


def run():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="repo folders or folders holding repos (same as --root)")
    ap.add_argument("--root", action="append", default=[], help="folder to scan (repeatable); default: the folder "
                    "holding this claude-shared clone")
    ap.add_argument("--depth", type=int, default=3, help="how deep below a root to look for repos (default 3)")
    ap.add_argument("--no-fetch", action="store_true", help="do not contact the remote (faster, refs may be stale)")
    ap.add_argument("--fetch-timeout", type=int, default=120, help="seconds per repo fetch (default 120)")
    ap.add_argument("--fetch-jobs", type=int, default=1, help="repos fetched in parallel, 1 or 2 (default 1: sequential)")
    ap.add_argument("--min-idle", type=float, default=0.0, help="ignore dirty/unpushed work touched in the last H hours")
    ap.add_argument("--ignore-untracked", action="append", default=[], metavar="PREFIX",
                    help="untracked paths starting with PREFIX do not make a worktree dirty (e.g. .githooks/)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--brief", action="store_true", help="one summary line plus at-risk repos only")
    ap.add_argument("--hook", action="store_true", help="SessionStart hook: no fetch, time-boxed, JSON systemMessage "
                    "only when work is at risk, always exit 0")
    ap.add_argument("--budget", type=int, default=8, help="--hook: seconds before the scan stops (default 8)")
    ap.add_argument("--push-unpushed", action="store_true", help="push own unpushed feature branches under the same "
                    "name (never dev/test/master/main, never force; diverged -> yedek/<host>-<branch>-<date>)")
    ap.add_argument("--clean-lossless", action="store_true", help="remove what cannot lose work: clean linked worktrees and local "
                    "branches whose tip is already in the base (dev/main) or an origin surum/* / release/* branch (never --force, never "
                    "protected or personal branches, never anything touched in the last --clean-idle hours; runs after --push-unpushed)")
    ap.add_argument("--clean-idle", type=float, default=CLEAN_IDLE_H, metavar="H",
                    help="--clean-lossless leaves worktrees and branches touched within H hours alone (default 2)")
    ap.add_argument("--cache-write", action="store_true", help="also store the result (summary and the session-start report text) in "
                    "the cache file tools/rules_watch.py reads instead of scanning at session start (tools/lost_work_cache.py; env LOST_WORK_LAST)")
    ap.add_argument("--notify", action="store_true", help="desktop notification when work is at risk (Windows toast / notify-send, tools/notifier.py)")
    a = ap.parse_args()
    if a.fetch_jobs not in (1, 2):
        ap.error("--fetch-jobs must be 1 or 2")
    global FETCH_SLOTS
    FETCH_SLOTS = threading.BoundedSemaphore(a.fetch_jobs)
    global _VERBOSE, _CACHE_RESULT
    _VERBOSE = not a.hook
    _CACHE_RESULT = a.cache_write and take_refresh_lock()
    mode = "hook" if a.hook else "json" if a.json else "brief" if a.brief else "table"
    log("start pid=%d mode=%s args=%s", os.getpid(), mode, " ".join(clean(x, 200) for x in sys.argv[1:]))

    cache_load()
    roots = [*a.root, *a.paths] or [str(REPO_ROOT.parent)]
    groups = group_repos([d for r in roots for d in find_git_dirs(r, a.depth)])
    deadline = time.time() + a.budget if a.hook else None
    fetch = not (a.no_fetch or a.hook)

    late = object()  # marks a repo the --hook budget left unscanned

    def one(repo, do_fetch=fetch):
        if deadline and time.time() > deadline:
            return late
        return analyse(repo, do_fetch, a.fetch_timeout, a.min_idle, a.ignore_untracked, deep=not a.hook, stale_only=a.hook)

    # --hook runs no fetch, so 4 workers are cheap and keep a machine with many repos inside the budget
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, groups.values()))
    skipped = sum(r is late for r in results)
    repos = [r for r in results if r and r is not late]
    pushed, cleaned = [], []
    if a.push_unpushed:
        for r in repos:
            pushed += push_unpushed(r)
    if a.clean_lossless:   # after the push, so own unpushed work is on origin before anything is judged
        for r in repos:
            cleaned += clean_lossless(r, a.clean_idle)
    if pushed or cleaned:  # rescan so the report and exit code show what is left
        repos = [r for r in (one(g, False) for g in groups.values()) if r and r is not late]
    cache_save()
    summ = summarize(repos)
    log("summary %s", summary_line(summ))

    msg = hook_message(summ, repos, skipped, len(groups), a.budget)
    if a.cache_write:
        lost_work_cache.write(summ, msg, cleaned if a.clean_lossless else None)
    if a.hook:
        if msg:
            print(json.dumps({"systemMessage": msg}))
        return 0
    if a.json:
        print(json.dumps({"host": platform.node(), "roots": [clean(x, 200) for x in roots], "summary": summ, "pushed": pushed, "cleaned": cleaned, "repos": repos},
                         indent=2, ensure_ascii=False))
    elif a.brief:
        print(summary_line(summ))
        extra = ([lost_work_cache.cleaned_text(cleaned)] if cleaned else []) + risky_lines(repos) + stale_lines(repos)
        print("\n".join(extra), end="\n" if extra else "")
    else:
        for p in pushed:
            if p["skipped"]:
                print(f"not pushed {clean(p['repo'], 80)} {clean(p['branch'])}: {clean(p['skipped'])}")
            else:
                print(("pushed " if p["ok"] else "PUSH FAILED ") + f"{clean(p['repo'], 80)} {clean(p['branch'])} -> origin/{clean(p['pushed_as'])}")
        for c in cleaned:
            print(f"removed {c['kind']} {clean(c['repo'], 80)} {clean(c['branch'])} {c['sha'][:9]}" + (f" ({clean(c['path'], 120)})" if c["path"] else "")
                  + (" - worktree kaldırıldı (dal origin'de)" if c.get("why") == "pushed" else ""))
        print_table(repos, summ)
    if a.notify and summ["at_risk"]:
        notify(summary_line(summ))
    return 1 if summ["at_risk"] else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cost_share: a usage row per person in the team's shared markdown files (opt-in, runs from the existing 2-hourly job).

Plain Python: it spends NO model tokens; it reads the local transcripts' totals, one `git` clone and nothing else.

  cost --share            (the 2-hourly timer run) record the totals, then compare the ranking on origin/main with the ranking
                              including this person's fresh row. Same (timestamps, rolling-window drift and the 7-day dollars on their
                              own do not count) = nothing is written, nothing committed. Different, or the first run of a date = today's
                              row is written (replaced in place when it exists), RANKING.md is regenerated from ALL files, commit
                              (the message names the rank / mode / weekly % / Slack move) and push, normal push, no force
  cost --share --dry     print the row; touch nothing
  cost --share --automatic the status line's throttled run (every share_interval_hour hours, default 2, while Claude Code is in use):
                              pushes only when the integer weekly % differs from the last pushed one, replacing today's row in place
  --ranking                  accepted for old drop-ins; has no effect (every run regenerates the ranking)

Every machine that runs it commits its own row AND the regenerated ranking, only when something changed. The job keeps its own clone in
~/.cache/claude-shared-usage (reset to origin/main before each attempt, which is a pull --rebase that cannot conflict: the person's
file has one writer and RANKING.md is regenerated, never merged; on a rejected push it re-fetches and redoes the work, up to 3 times).
A rank or mode change of the person's own row also raises a desktop notification on that machine (notify-send; bildir()).
Ownership: a row is only written to the file of the runner's own name, and that name must be a value of kisiler.json; the ranking
reads only the files of the names in kisiler.json. Who committed what stays visible in the git history.

Row (a markdown table row): date | 24h | 7d | 30d $ | weekly limit % | plan ($100/$200) | week id | week-to-date $ | per-model $ and
Mtokens (Opus, Sonnet, Haiku, Fable, other: the previous calendar day) | own commits that reached dev in the last 24 h | lines added | lines deleted
(numstat of those commits, noise excluded: NOISE_RE; old rows lack the last two columns and show "—"), then forecast median, reset time,
pace mode, projected run-out, forecast time, and this machine's Slack inbox state (slack-inbox: acik/hata/kapali/yok) with its last poll.
Dollars are the API list-price equivalent of token usage (not a bill). Only the name and these numbers leave the machine.

Person name: env CLAUDE_SHARED_PERSON, else ~/.config/claude-shared/kisi, else `git config user.name`.
Plan: env CLAUDE_SHARED_PLAN, else ~/.config/claude-shared/plan (100 or 200).
Commit count in each daily row: own commits (author email = git user.email) of the last 24 h on the integration branch
(origin/dev, else origin/main|master) of every clone under the team root (env CLAUDE_SHARED_KOK, else
~/.config/claude-shared/kok, else ~/source/repos or ~/src, whichever exists). Local
knowledge only: a machine without clones or user.email writes "—", so the row figure is NOT used for the ranking when GitHub
answers. The "Commit (7 gün)" column of RANKING.md comes from GitHub (github_commitleri): `gh api search/commits` with
q=author:<login> org:<GH_ORG> author-date:>=<today-6>, minus the automation subjects chore(kullanim) and chore(last-window), logins from kisiler.json, cached 6 h in
<config dir>/cost/github-commit-cache.json (search API: 30 requests/min; one request per person per 6 h). Falls back to
the sum of the row figures when `gh` is missing or fails. Default branch of each repo only (GitHub search limitation).
Never fails loudly: any error goes to <config dir>/cost/paylas.log and the exit code stays 0.

Config keys (env, else the first line of ~/.config/claude-shared/<file>; README, "Configuration"):
  CLAUDE_SHARED_REMOTE (uzak)                the team's shared usage repo (clone URL). No default: unset = the ranking is off.
  CLAUDE_SHARED_REPORT_DIR (report-dir)  report folder inside that repo, default raporlar/kullanim.
  CLAUDE_SHARED_PEOPLE (people-path)      the people list inside that repo, default plugins/quota-pace/kisiler.json.
  GH_ORG (org)                             GitHub org for the 7-day commit column; unset = the row figures are used.
"""
import datetime
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", "tools"))  # repo layout: tools/hidden.py (a --copy install ships it next to this file)
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from hidden_loader import hidden  # noqa: E402  (one hidden-launch helper)
import runlog  # noqa: E402  (the rolling run log)
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # python -I does not add the script directory
import cost_efficiency as V  # noqa: E402  weekly cost per finished work item (sidecar <kisi>.hafta.json, leaderboard section)

RANKING_FILE = "RANKING.md"
QUOTA_FRESHNESS = 48 * 3600      # pace.json older than this is not quoted
GIT_TIME_OVERRUN = 120
PLANS = (100, 200)
MODELS = (("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"), ("fable", "Fable"))

HOME = os.path.expanduser("~")
SETTING = os.path.join(HOME, ".config", "claude-shared")

# A scanned repo's own config must not run code: command-line -c beats .git/config (fsmonitor, hooks, ssh, pager, external
# diff, credential helpers, ext:: transport). Same list as tools/lost_work.py (SAFE_GIT); tools/ is not installed next to this file.
SAFE_GIT = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull, "-c", "core.sshCommand=ssh",
            "-c", "core.pager=cat", "-c", "core.editor=true", "-c", "diff.external=", "-c", "credential.helper=",
            "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=user", "-c", "gc.auto=0",
            "-c", "maintenance.auto=false", "-c", "fetch.fsckObjects=false"]
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "LC_ALL": "C"}
SKIP = {"node_modules", "bin", "obj", "venv", "__pycache__"}

_CHECK = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")



# ------------------------------------------------------------------ small helpers
def setting_path(name):
    return os.path.join(SETTING, name)


def read_from_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.readline().strip()
    except OSError:
        return ""


def setting_value(name, env, default=""):
    """One config key: env `env`, else the first line of ~/.config/claude-shared/<ad>, else `default` (see the module doc)."""
    return (os.environ.get(env) or read_from_file(setting_path(name)) or default).strip()


REPORT_DIR = setting_value("report-dir", "CLAUDE_SHARED_REPORT_DIR", "raporlar/kullanim").strip("/")
PEOPLE_PATH = setting_value("people-path", "CLAUDE_SHARED_PEOPLE", "plugins/quota-pace/kisiler.json").strip("/")
REMOTE_MISSING = "ortak depo ayarlanmadi (CLAUDE_SHARED_REMOTE veya ~/.config/claude-shared/uzak; README, Configuration)"


def clean_name(s, limit=30):
    """A name safe to post: no control characters, no field separator, no @mention or markup, one line, capped."""
    s = _CHECK.sub(" ", str(s or ""))
    s = re.sub(r"[·|@<>`*_\[\]\\:./!#~&;()]", " ", s)    # separators, mentions, links/autolinks, refs, entities, markup
    s = " ".join(s.split())
    return s[:limit].strip()


def person_name():
    name = os.environ.get("CLAUDE_SHARED_PERSON") or read_from_file(setting_path("person"))
    if not name:
        name = git_output(HOME, "config", "--get", "user.name") or ""
    return clean_name(name)


def git_output(cwd, *args, timeout=30):
    """stdout of a git command run with the repo-config code-execution switches off, or None on any failure."""
    try:
        r = hidden.run(["git", "--no-optional-locks", *SAFE_GIT, "-C", str(cwd), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=GIT_ENV, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as e:
        kom_error("git " + (args[0] if args else ""), type(e).__name__)
        return None
    if r.returncode != 0:
        if not (args[:1] in (("config",), ("rev-parse",)) and r.returncode == 1):      # `config --get` of an unset key and `rev-parse` outside a repo are answers, not failures
            kom_error("git " + (args[0] if args else ""), r.returncode, r.stderr)
        return None
    return r.stdout.strip()


def daily_write(level, message, *args):
    """One line in the run log of the scheduled job (tools/runlog.py "cost": ~/.local/state/cost.log, env COST_LOG). Never raises."""
    try:
        runlog.runlog("cost").log(level, message, *args)
    except Exception:
        pass


def clean(text, n=300):
    """Output of a failed command made fit for a log line: tokens and URL credentials masked, control characters and line breaks gone."""
    try:
        return _CHECK.sub(" ", runlog.mask_out(text or "")).strip()[:n]
    except Exception:
        return ""


def kom_error(name, rc, output=""):
    """Log one failed git / gh call. `name` is the program and sub-command only ("git fetch", "gh api"): never the arguments, a remote URL could carry a credential."""
    daily_write(logging.WARNING, "%s failed rc=%s: %s", name, rc, clean(output))


def _command_name(args):
    """Program name, plus the sub-command of git and gh ("git fetch", "gh api", "notify-send")."""
    name = os.path.splitext(os.path.basename(str(args[0])))[0] if args else "?"
    return name + " " + str(args[1]) if name in ("git", "gh") and len(args) > 1 and not str(args[1]).startswith("-") else name


def log_yaz(message):
    """Append to <config dir>/cost/paylas.log (kept short; also mirrored into the run log); a logging failure is swallowed too."""
    daily_write(logging.INFO, "%s", clean(message, 500))
    try:
        import cost
        os.makedirs(cost.DIR, exist_ok=True)
        path = os.path.join(cost.DIR, "share.log")
        try:
            if os.path.getsize(path) > 200_000:
                with open(path, encoding="utf-8", errors="replace") as f:
                    last = f.readlines()[-200:]
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(last)
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), _CHECK.sub(" ", str(message))[:500]))
    except Exception:
        pass


# ------------------------------------------------------------------ the numbers
def quota_info(now=None):
    """(weekly quota used %, epoch when that week resets) from pace.json (the status line's rate-limit data), or (None, None)
    when missing, stale or from a week that already reset."""
    import cost
    now = time.time() if now is None else now
    try:
        with open(os.path.join(cost.CLAUDE, "pace.json"), encoding="utf-8-sig") as f:
            p = json.load(f)
        used, upd, reset = p.get("week_used"), float(p.get("updated") or 0), float(p.get("week_resets_at") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None, None
    if not isinstance(used, (int, float)) or isinstance(used, bool) or not 0 <= used <= 100:
        return None, None
    if now - upd > QUOTA_FRESHNESS or reset <= now or reset > now + 8 * 86400:
        return None, None
    return int(round(used)), reset


def pace_ek(now=None):
    """(forecast median % at the target, local reset time 'YYYY-MM-DD HH:MM', pace mode) from pace.json; each None when unknown.
    Same freshness rule as quota_info: nothing is quoted from a stale file or a week that already reset."""
    import cost
    quota, reset = quota_info(now)
    if quota is None:
        return None, None, None
    try:
        with open(os.path.join(cost.CLAUDE, "pace.json"), encoding="utf-8-sig") as f:
            p = json.load(f)
    except (OSError, ValueError):
        p = {}
    if not isinstance(p, dict):
        p = {}
    t, mod = p.get("p50"), p.get("mode")
    t = int(round(t)) if isinstance(t, (int, float)) and not isinstance(t, bool) and 0 <= t <= 1000 else None
    mod = mod if isinstance(mod, str) and MOD_RE.match(mod) else None
    return t, time.strftime("%Y-%m-%d %H:%M", time.localtime(reset)), mod


def end_at_extra(now=None):
    """(run-out, forecast time) from pace.json as local 'YYYY-MM-DD HH:MM' strings; run-out is "enough" when the forecast says the
    limit lasts until the reset. (None, None) when unknown (stale file, no forecast, file from an older pace_forecast)."""
    import cost
    quota, _ = quota_info(now)
    if quota is None:
        return None, None
    try:
        with open(os.path.join(cost.CLAUDE, "pace.json"), encoding="utf-8-sig") as f:
            p = json.load(f)
        upd = float(p.get("updated") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None, None
    if "runout_at" not in p or not upd:
        return None, None
    ra = p.get("runout_at")
    fmt = "%Y-%m-%d %H:%M"
    if isinstance(ra, (int, float)) and not isinstance(ra, bool) and ra > 0:
        end_at = time.strftime(fmt, time.localtime(ra))
    elif ra is None and isinstance(p.get("p50"), (int, float)):
        end_at = ENOUGH
    else:
        return None, None
    return end_at, time.strftime(fmt, time.localtime(upd))


def end_at_text(b):
    """`2026-11-01 12:00` -> `01.11 12:00`; "enough" -> "sıfırlanmaya yetiyor"; "—" when unknown."""
    return "sıfırlanmaya yetiyor" if b == ENOUGH else reset_text(b)


def slack_text(status, last):
    """`açık · 04.11 02:35` (state and the last poll of the person's Slack inbox poller), `kapalı`, `kurulu değil`, "—" when unknown."""
    name = {"is_open": "açık", "error": "error", "closed": "kapalı", "missing": "kurulu değil"}.get(status)
    if name is None:
        return "—"
    time_ = reset_text(last)
    return name if status == "missing" or time_ == "—" else "%s · %s" % (name, time_)


def reset_text(s):
    """`2026-11-01 12:00` -> `01.11 12:00`; "—" when empty."""
    m = re.match(r"^\d{4}-(\d\d)-(\d\d) (\d\d:\d\d)$", s or "")
    return "%s.%s %s" % (m.group(2), m.group(1), m.group(3)) if m else "—"


def weekly_quota(now=None):
    return quota_info(now)[0]


def kok_dir():
    path = os.environ.get("CLAUDE_SHARED_KOK") or read_from_file(setting_path("kok"))
    candidates = [path] if path else [os.path.join(HOME, "source", "repos"), os.path.join(HOME, "src")]   # documented defaults
    for a in candidates:
        a = os.path.expanduser(a)
        if os.path.isdir(a):
            return a
    return None


def clones(kok, depth=2):
    """Git clones/worktrees under the root (the root's children, and theirs); a clone is not descended into."""
    found = []

    def gez(d, k):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            return
        for name in names:
            if name.startswith(".") or name in SKIP:
                continue
            path = os.path.join(d, name)
            if os.path.exists(os.path.join(path, ".git")):
                found.append(path)
            elif k < depth and os.path.isdir(path):
                gez(path, k + 1)
    if os.path.exists(os.path.join(kok, ".git")):
        found.append(kok)
    else:
        gez(kok, 1)
    return found


# Noise that is not work output: lock files, generated EF files, minified/map files, build and vendor folders (matched on the new path).
NOISE_RE = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|packages\.lock\.json|[^/]*\.lock"
    r"|[^/]*\.min\.js|[^/]*\.min\.css|[^/]*\.map|[^/]*ModelSnapshot\.cs|Migrations/[^/]*\.Designer\.cs"
    r"|[^/]*\.(pdf|cdr|bad|jsonl|svg)|[^/]*\.g\.(i\.)?cs|(bad_REHBER_|PRODUCT_A_)[^/]*)$"   # PRODUCT_A_*, bad_REHBER_*: extensionless CDR/phonebook dumps; pdf: standards/manuals; cdr, bad: CDR/phonebook data dumps; jsonl: machine logs; svg, *.g.cs: generated
    r"|(^|/)(bin|obj|node_modules|dist|build)/|(^|/)wwwroot/lib/")


def numstat_path(path):
    """New path of a numstat path field (`a => b`, `dir/{a => b}/f`)."""
    if " => " not in path:
        return path
    if "{" in path:
        path = re.sub(r"\{([^{}]*) => ([^{}]*)\}", r"\2", path)
        return path.replace("//", "/")
    return path.split(" => ", 1)[1]


def numstat_parse(text):
    """{hash: (added, deleted)} from `git log --format=%x01%H --numstat -M`. Binary files (`-`), noise paths (NOISE_RE)
    and pure renames (0/0) add nothing; a commit that only had such files still appears with (0, 0)."""
    result, hash_ = {}, None
    for line in (text or "").split("\n"):
        if line.startswith("\x01"):
            hash_ = line[1:].strip()
            if hash_:
                result.setdefault(hash_, (0, 0))
            continue
        part = line.split("\t", 2)
        if hash_ is None or len(part) != 3 or not (part[0].isdigit() and part[1].isdigit()):
            continue
        ek, delete = int(part[0]), int(part[1])
        if (ek == 0 and delete == 0) or NOISE_RE.search(numstat_path(part[2])):
            continue
        a, d = result[hash_]
        result[hash_] = (a + ek, d + delete)
    return result


def commit_ve_line(mail, kok, hour=24):
    """(commits, added lines, deleted lines) of the own commits of the last `hour` hours that are on the integration branch of
    any clone under `kok`; the same commit seen in two clones counts once. None when unknown."""
    if not mail or not kok:
        return None
    total = {}
    for clone in clones(kok):
        for ref in ("origin/dev", "origin/main", "origin/master"):
            if git_output(clone, "rev-parse", "--verify", "-q", ref + "^{commit}") is not None:
                out = git_output(clone, "-c", "core.quotepath=false", "log", ref, "--no-merges", "--numstat", "-M", "--since=%d hours ago" % hour, "-F", "-i",
                                "--author=" + mail, "--format=%x01%H", timeout=120)
                total.update(numstat_parse(out))
                break
    return len(total), sum(a for a, _ in total.values()), sum(d for _, d in total.values())


def github_org():
    """The GitHub org of the 7-day commit column (config key GH_ORG / ~/.config/claude-shared/org); "" = not configured."""
    return setting_value("org", "GH_ORG")


GITHUB_CACHE_HOUR = 6
# Subjects of the commits this plugin writes itself (one_attempt: the usage row; last_window: its journal). They are automation, not a
# person's work, so the GitHub commit column leaves them out.
AUTOMATION_TOPICS = ("chore(kullanim)", "chore(last-window)")
TR = datetime.timezone(datetime.timedelta(hours=3))


def _cache_path():
    import cost
    return os.path.join(cost.DIR, "github-commit-cache.json")


def github_commit_say(login, bas, now=None):
    """Commits by `login` in the org's repos with author date >= `bas` (YYYY-MM-DD), via gh search/commits, minus the automation
    commits (AUTOMATION_TOPICS, matched on the subject line); cached per (login, bas) for GITHUB_CACHE_HOUR hours. None when
    unknown (no gh, no network, API error)."""
    now = time.time() if now is None else now
    key = "%s|%s" % (login, bas)
    path = _cache_path()
    try:
        with open(path, encoding="utf-8") as f:
            ob = json.load(f)
        if not isinstance(ob, dict):
            ob = {}
    except (OSError, ValueError):
        ob = {}
    e = ob.get(key)
    if isinstance(e, dict) and isinstance(e.get("n"), int) and 0 <= now - float(e.get("t", 0)) < GITHUB_CACHE_HOUR * 3600:
        return e["n"]
    org = github_org()
    if not org:
        return None   # no org configured: the column falls back to the row figures
    try:
        # one subject line per commit (all pages); the automation subjects are dropped here, not in the query
        r = hidden.run(["gh", "api", "-X", "GET", "search/commits", "-f", "q=author:%s org:%s author-date:>=%s" % (login, org, bas),
                            "-f", "per_page=100", "--paginate", "--jq", '.items[].commit.message | split("\\n")[0]'],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, env=GIT_ENV,
                           stdin=subprocess.DEVNULL)
        # text mode without an explicit encoding decodes with the Windows ANSI codepage; a non-cp1252 byte in a commit subject kills
        # the reader thread and leaves stdout None (AttributeError on splitlines). Explicit utf-8 + this guard keep it a plain "unknown".
        if r.returncode == 0 and r.stdout is not None:
            n = sum(1 for topic in r.stdout.splitlines() if not topic.startswith(AUTOMATION_TOPICS))
        else:
            n = None
            kom_error("gh api", r.returncode, r.stderr)
            print("github_commit_say: gh api basarisiz (rc=%s, stdout=%s)" % (r.returncode, "missing" if r.stdout is None else "present"), file=sys.stderr)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        kom_error("gh api", type(e).__name__)
        n = None
    if n is None:
        return None
    ob[key] = {"n": n, "t": now}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ob, f)
    except OSError:
        pass
    return n


def github_commitleri(lookup, today, now=None):
    """{display name: commits in the 7-day window [today-6 .. bugun]} for every person of kisiler.json ({login: name}); people whose
    count is unknown are left out (the ranking then falls back to the sum of their row figures)."""
    bas = (datetime.date.fromisoformat(today) - datetime.timedelta(days=6)).isoformat()
    out = {}
    for login, name in lookup.items():
        n = github_commit_say(login, bas, now)
        if n is not None:
            out[name] = n
    return out


def commit_say(mail, kok, hour=24):
    """Own commits of the last `hour` hours that are on the integration branch of any clone under `kok` (None when unknown)."""
    c = commit_ve_line(mail, kok, hour)
    return None if c is None else c[0]


def window_usd(records, hour):
    """(24h, 7d, 30d) in whole dollars, from the same rolling windows the status line shows."""
    import cost_core as CK
    return tuple(int(round(P["usd"])) for P in CK.compute_window(records, hour))




# ------------------------------------------------------------------ this machine's Slack inbox (tools/slack-triage/gelen.py)
SG_UNIT = "slack-inbox"                        # systemd user unit (install.sh --slack-inbox)
SG_LABEL = "com.agentcrewkit.slack-inbox"           # launchd agent label
SG_FRESH = 20 * 60                               # no service manager: a poll this recent counts as running
SON_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d")


def command(args, timeout=10):
    """(returncode, stdout, stderr) of a short read-only command; (None, "", "") when it cannot run. Replaced by tests."""
    try:
        r = hidden.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           env=GIT_ENV, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        if _command_name(args).startswith(("git", "gh")):
            kom_error(_command_name(args), type(e).__name__)
        return None, "", ""
    if r.returncode != 0 and _command_name(args).startswith(("git", "gh")):      # service probes (systemctl is-enabled ...) answer non-zero as a normal answer
        kom_error(_command_name(args), r.returncode, r.stderr)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def slack_inbox_last():
    """(last poll 'YYYY-MM-DD HH:MM' local or None, whether that poll reported an error) from gelen.py's state file. Only the two
    `status` fields are read: the inbox file and the message text are never opened."""
    path = os.path.join(os.environ.get("SLACK_INBOX_DATA") or os.path.join(HOME, ".local", "state", "slack-inbox"), "state.json")
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f).get("status")
    except (OSError, ValueError, AttributeError):
        return None, False
    if not isinstance(st, dict):
        return None, False
    last = st.get("last_run")
    return (last[:16] if isinstance(last, str) and SON_RE.match(last) else None), bool(st.get("error"))


def slack_inbox_status(now=None):
    """(state word, last poll) of this machine's token-free Slack inbox poller, both None when it cannot be told (Windows without a
    poll on record; no service manager answers). State: acik (enabled and running), hata (running, but the last poll failed), kapali (installed,
    switched off), yok (not installed). Linux: `systemctl --user` on slack-inbox.timer; macOS: launchd agent com.agentcrewkit.slack-inbox.
    Windows (install.ps1 -SlackInbox, a Task Scheduler task): only the last poll is read, a recent one counts as running (acik/hata), an old one
    as kapali; no poll on record is unknown. Nothing but these two values leaves the machine."""
    now = time.time() if now is None else now
    last, error = slack_inbox_last()
    if sys.platform.startswith("win"):
        if last is None:
            return None, None
        try:
            loaded = now - time.mktime(time.strptime(last, "%Y-%m-%d %H:%M")) < SG_FRESH
        except (ValueError, OverflowError):
            loaded = False
        return ("error" if error else "is_open") if loaded else "closed", last
    if sys.platform == "darwin":
        if not os.path.exists(os.path.join(HOME, "Library", "LaunchAgents", SG_LABEL + ".plist")):
            return "missing", None
        rc, _, _ = command(["launchctl", "print", "gui/%d/%s" % (os.getuid(), SG_LABEL)])
        loaded = rc == 0
        installed = True
    else:
        rc, out, err = command(["systemctl", "--user", "is-enabled", SG_UNIT + ".timer"])
        if rc is None:   # no systemctl at all (cron install, or no systemd): the poll's own timestamp is all there is
            try:
                loaded = last is not None and now - time.mktime(time.strptime(last, "%Y-%m-%d %H:%M")) < SG_FRESH
            except (ValueError, OverflowError):
                loaded = False
            return ("is_open" if loaded and not error else "error" if loaded else "missing" if last is None else "closed"), last
        if rc != 0 and out != "disabled" and "bus" in err.lower():
            return None, None   # the user manager cannot be reached from here: unknown, not "not installed"
        installed = rc == 0 or out == "disabled"
        loaded = rc == 0 and command(["systemctl", "--user", "is-active", SG_UNIT + ".timer"])[1] == "active"
    if not installed:
        return "missing", None
    return ("error" if error else "is_open") if loaded else "closed", last


# ------------------------------------------------------------------ the row
COLUMNS = (["Date", "24sa $", "7g $", "30g $", "Kota %", "Plan", "Week", "Hafta $"]
            + ["%s $" % name for _, name in MODELS] + ["Diğer $"]
            + ["%s Mtok" % name for _, name in MODELS] + ["Diğer Mtok", "Commit"])
EXTRA_COLUMN = ["Satır +", "Satır −"]   # added later; rows with SUTUNLAR only (old rows) still parse and show "—"
EK2_COLUMN = ["Tahmin %", "Sıfırlanma", "Mod"]   # weekly forecast median, reset time, pace mode (from pace.json); empty ("—") when unknown
EK3_COLUMN = ["Bitiş", "Tahmin zamanı"]   # projected weekly-limit run-out (pace.json runout_at; "enough" = lasts until reset) and when the forecast was made
INTERIM_COLUMN = COLUMNS + EXTRA_COLUMN
ARA2_COLUMN = INTERIM_COLUMN + EK2_COLUMN   # 24 columns, before EK3_COLUMN
ARA3_COLUMN = ARA2_COLUMN + EK3_COLUMN   # 26 columns, before EK4_COLUMN
EK4_COLUMN = ["Slack", "Slack son"]   # this machine's token-free Slack inbox (slack-inbox): state word and the last poll; never any message content
EVERY_COLUMN = ARA3_COLUMN + EK4_COLUMN
OLD_TITLES = (COLUMNS, INTERIM_COLUMN, ARA2_COLUMN, ARA3_COLUMN)
ENOUGH = "enough"
SLACK_STATUSES = ("is_open", "error", "closed", "missing")   # running | running but the last poll failed (e.g. token expired) | installed, switched off | not installed
MOD_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DAY_NAMES = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]


def file_name(person):
    """File stem for a person: lower case, Turkish letters folded, anything else becomes '-'."""
    s = str(person).translate(str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")).lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "person"


def model_family(model):
    m = str(model).lower()
    for key, _ in MODELS:
        if key in m:
            return key
    return "other"


def model_distribution(record):
    """({family: $}, {family: Mtoken}) of one daily record (all token kinds summed; Fable runs report $ only)."""
    usd, tok = {}, {}
    for m, v in ((record or {}).get("model") or {}).items():
        f = model_family(m)
        usd[f] = usd.get(f, 0.0) + float(v.get("usd", 0.0) or 0.0)
        n = sum(int(v.get(k, 0) or 0) for k in ("in", "out", "cr", "cw5", "cw1h"))
        tok[f] = tok.get(f, 0.0) + n / 1e6
    return usd, tok


def plan_oku():
    """100 or 200 from env CLAUDE_SHARED_PLAN / ~/.config/claude-shared/plan; None when not set (the row then says '—')."""
    ham = os.environ.get("CLAUDE_SHARED_PLAN") or read_from_file(setting_path("plan"))
    m = re.search(r"\d+", ham or "")
    return int(m.group()) if m and int(m.group()) in PLANS else None


def week_usd(records, reset):
    """Dollars since the start of the current weekly-limit week (daily records from the local date of reset-7d; approximate)."""
    import cost_core as CK
    bas = CK.local_day(reset - 7 * 86400)
    return int(round(sum(r.get("usd", 0.0) for d, r in records.items() if d >= bas)))


def _h(x, nd=1):
    return "—" if x is None else ("%d" % x if isinstance(x, int) else "%.*f" % (nd, x))


def _k(n):
    """Compact Turkish number: 950, 12,3K, 1,2M."""
    if n < 1000:
        return "%d" % n
    return ("%.1f" % (n / 1000.0) + "K" if n < 1000000 else "%.1f" % (n / 1000000.0) + "M").replace(".", ",")


def line_text(add, delete):
    """`+12,3K / −4,1K`, or "—" when the rows carry no line figures."""
    return "—" if add is None or delete is None else "+%s / −%s" % (_k(add), _k(delete))


def usd_bin_line(d7, add, delete):
    """7-day spend per 1000 changed lines (`$4,12`), or "—" without line figures."""
    if add is None or delete is None or add + delete <= 0:
        return "—"
    return ("$%.2f" % (d7 * 1000.0 / (add + delete))).replace(".", ",")


def install_line(r):
    """The markdown table row of a record dict (see line_parse for the inverse)."""
    h = [r["date"], "%d" % r["d24"], "%d" % r["d7"], "%d" % r["d30"], _h(r.get("quota")),
         "$%d" % r["plan"] if r.get("plan") else "—", r.get("week") or "—", _h(r.get("week_usd"))]
    fam = [a for a, _ in MODELS] + ["other"]
    h += ["%.1f" % r["usd"].get(f, 0.0) for f in fam] + ["%.1f" % r["tok"].get(f, 0.0) for f in fam]
    h += [_h(r.get("commit")), _h(r.get("add")), _h(r.get("delete"))]
    h += [_h(r.get("forecast")), r.get("reset") or "—", r.get("mod") or "—"]
    h += [r.get("end_at") or "—", r.get("forecast_time") or "—"]
    h += [r.get("slack") or "—", r.get("slack_son") or "—"]
    return "| " + " | ".join(h) + " |"


def _count(s, tip=float):
    try:
        return tip(s)
    except ValueError:
        return None


def line_parse(line):
    """A table row back into a record dict, or None for anything else (header, separator, prose, a malformed row)."""
    if not isinstance(line, str) or not line.startswith("| 20"):
        return None
    c = [x.strip() for x in line.strip().strip("|").split("|")]
    if len(c) not in (len(COLUMNS), len(INTERIM_COLUMN), len(ARA2_COLUMN), len(ARA3_COLUMN), len(EVERY_COLUMN)) or not DATE_RE.match(c[0]):
        return None
    try:
        datetime.date.fromisoformat(c[0])
    except ValueError:
        return None
    d24, d7, d30 = (_count(c[i], int) for i in (1, 2, 3))
    if None in (d24, d7, d30):
        return None
    fam = [a for a, _ in MODELS] + ["other"]
    usd = {f: _count(c[8 + i]) or 0.0 for i, f in enumerate(fam)}
    tok = {f: _count(c[13 + i]) or 0.0 for i, f in enumerate(fam)}
    plan = _count(c[5].lstrip("$"), int)
    return {"date": c[0], "d24": d24, "d7": d7, "d30": d30, "quota": _count(c[4], int), "plan": plan if plan in PLANS else None,
            "week": c[6] if DATE_RE.match(c[6]) else None, "week_usd": _count(c[7], int), "usd": usd, "tok": tok,
            "commit": _count(c[18], int),
            "add": _count(c[19], int) if len(c) > 19 else None, "delete": _count(c[20], int) if len(c) > 20 else None,
            "forecast": _count(c[21], int) if len(c) > 21 else None,
            "reset": c[22] if len(c) > 22 and reset_text(c[22]) != "—" else None,
            "mod": c[23] if len(c) > 23 and MOD_RE.match(c[23]) else None,
            "end_at": c[24] if len(c) > 24 and (c[24] == ENOUGH or reset_text(c[24]) != "—") else None,
            "forecast_time": c[25] if len(c) > 25 and reset_text(c[25]) != "—" else None,
            "slack": c[26] if len(c) > 26 and c[26] in SLACK_STATUSES else None,
            "slack_son": c[27] if len(c) > 27 and reset_text(c[27]) != "—" else None}


def file_lines(text):
    """Records of a person's file, one per date (the first row of a date wins), oldest first."""
    seen = {}
    for s in (text or "").splitlines():
        r = line_parse(s)
        if r and r["date"] not in seen:
            seen[r["date"]] = r
    return [seen[t] for t in sorted(seen)]


def file_title(person):
    return ("# Claude kullanımı — %s\n\n"
            "Günde bir satır; `cost --share` işi ekler (yerel Python, model token'ı harcamaz). Elle düzenlemeyin. "
            "Dolar = API liste fiyatı karşılığı (fatura değil). Model ve token sütunları bir önceki takvim gününe aittir; "
            "Kota %% = haftalık limit kullanımı, Hafta $ = limit haftasının başından beri dolar. Sıralama: [RANKING.md](RANKING.md).\n\n"
            "| %s |\n|%s\n" % (person, " | ".join(EVERY_COLUMN), "---|" * len(EVERY_COLUMN)))


def add_to_file(text, person, line, date, quota_update=False, instead=False):
    """The person's file with `line` appended unless that date already has a row (so a second run the same day changes nothing).
    quota_update (the status line's automatic share): a row of that date whose weekly % differs from the new row's is replaced in
    place, so a date never gets a second row and the 7-day / week-over-week figures keep one row per day.
    yerine (the timer run that has already decided the change is real): the row of that date is replaced unconditionally, still in place."""
    if not text or not text.strip():
        text = file_title(person)
    existing = [r for r in file_lines(text) if r["date"] == date]
    if existing:
        new_r = line_parse(line) or {}
        bos = all(existing[0].get(k) is None for k in ("forecast", "reset", "mod", "end_at", "forecast_time"))
        missing = (new_r.get("commit") is not None and (existing[0].get("commit") is None or new_r["commit"] > existing[0]["commit"])) or             (not sum(existing[0]["usd"].values()) and sum(new_r.get("usd", {}).values()) > 0)   # earlier run knew no repos / saw an idle day
        end_at_bos = existing[0].get("end_at") is None and existing[0].get("forecast_time") is None and (new_r.get("end_at") or new_r.get("forecast_time"))
        quota_different = quota_update and new_r.get("quota") is not None and new_r["quota"] != existing[0].get("quota")
        if instead or (bos and any(new_r.get(k) is not None for k in ("forecast", "reset", "mod", "end_at", "forecast_time"))) or end_at_bos or missing or quota_different:   # a same-day row that this run can fill in
            sat = text.split("\n")
            i = next(i for i, x in enumerate(sat) if (line_parse(x) or {}).get("date") == date)
            sat[i] = line
            text = "\n".join(sat)
            for old_s in OLD_TITLES:
                text = text.replace("| %s |\n|%s" % (" | ".join(old_s), "---|" * len(old_s)),
                                      "| %s |\n|%s" % (" | ".join(EVERY_COLUMN), "---|" * len(EVERY_COLUMN)), 1)
        return text
    new = "| %s |\n|%s" % (" | ".join(EVERY_COLUMN), "---|" * len(EVERY_COLUMN))
    for old_s in OLD_TITLES:
        text = text.replace("| %s |\n|%s" % (" | ".join(old_s), "---|" * len(old_s)), new, 1)   # an old header grows the new columns
    return text.rstrip("\n") + "\n" + line + "\n"


def scope_head(record, previous):
    """First day this row covers: a row dated R covers R-1 (normally just yesterday), so today's row covers [latest earlier row
    date .. yesterday] and a person's first row the last 7 days. One rule for the model split and for commits/lines."""
    today = _day(record["date"])
    onc = [_day(r["date"]) for r in previous if _day(r["date"]) < today]
    return max(onc) if onc else today - datetime.timedelta(days=7)


def apply_scope(record, previous):
    """Fill the model columns and commits/lines of `record` for the days no earlier row of this person covers (nothing counted
    twice; an idle yesterday no longer zeroes a new joiner's row)."""
    today = _day(record["date"])
    bas = scope_head(record, previous)
    days = record.get("model_days")
    if days:
        usd, tok = {}, {}
        for g, (u, t) in days.items():
            if bas <= _day(g) < today:
                for f, v in u.items():
                    usd[f] = usd.get(f, 0.0) + v
                for f, v in t.items():
                    tok[f] = tok.get(f, 0.0) + v
        record["usd"], record["tok"] = usd, tok
    if record.get("mail") and record.get("kok"):
        cs = commit_ve_line(record["mail"], record["kok"], max(1, (today - bas).days) * 24)
        if cs:
            record["commit"], record["add"], record["delete"] = cs


def todays_record(person, today, now=None):
    import cost_core as CK
    records, hour = CK.daily_read(), CK.read_hourly()
    d24, d7, d30 = window_usd(records, hour)
    mail, kok = git_output(HOME, "config", "--get", "user.email"), kok_dir()
    cs = commit_ve_line(mail, kok)
    quota, reset = quota_info(now)
    forecast, reset_, mod = pace_ek(now)
    end_at, forecast_time = end_at_extra(now)
    slack, slack_son = slack_inbox_status()
    yesterday = (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()
    usd, tok = model_distribution(records.get(yesterday))
    days = {}
    for i in range(1, 8):   # per-day split of the last 7 days; one_attempt keeps only the days no earlier row of this person covered
        g = (datetime.date.fromisoformat(today) - datetime.timedelta(days=i)).isoformat()
        if g in records:
            days[g] = model_distribution(records[g])
    return {"model_days": days, "mail": mail, "kok": kok, "person": person, "date": today, "d24": d24, "d7": d7, "d30": d30, "quota": quota, "plan": plan_oku(), "forecast": forecast, "reset": reset_, "mod": mod, "end_at": end_at, "forecast_time": forecast_time, "slack": slack, "slack_son": slack_son,
            "week": CK.local_day(reset) if reset else None, "week_usd": week_usd(records, reset) if reset else None,
            "usd": usd, "tok": tok, "commit": cs[0] if cs else None,
            "add": cs[1] if cs else None, "delete": cs[2] if cs else None}


# ------------------------------------------------------------------ the ranking and the plan comparison
def _day(t):
    return datetime.date.fromisoformat(t)


def compute_ranking(person_lines, today):
    """Rank by each person's 7-day $ in their latest row of the last 7 days. person_lines: {name: [records]}.
    Trend = that 7g against the row dated 7 days earlier (±1 day, nearest). Commits = sum of the daily figures of the last 7 days."""
    t0 = _day(today)
    result = []
    for person, ls in person_lines.items():
        son7 = [k for k in ls if 0 <= (t0 - _day(k["date"])).days <= 6]
        if not son7:
            continue
        last = son7[-1]
        candidate = [(abs((_day(last["date"]) - _day(k["date"])).days - 7), k) for k in ls
                if 6 <= (_day(last["date"]) - _day(k["date"])).days <= 8]
        previous = min(candidate, key=lambda a: (a[0], a[1]["date"]))[1] if candidate else None
        commits = [k["commit"] for k in son7 if k["commit"] is not None]
        add = [k["add"] for k in son7 if k.get("add") is not None]
        delete = [k["delete"] for k in son7 if k.get("delete") is not None]
        result.append({"person": person, "d7": last["d7"], "previous_d7": previous["d7"] if previous else None, "last_date": last["date"],
                      "quota": last["quota"], "plan": last["plan"],
                      "forecast": last.get("forecast"), "reset": last.get("reset"), "mod": last.get("mod"), "end_at": last.get("end_at"), "forecast_time": last.get("forecast_time"), "week_usd": last["week_usd"],
                      "slack": last.get("slack"), "slack_son": last.get("slack_son"),
                      "commit": sum(commits) if commits else None,
                      "add": sum(add) if add else None, "delete": sum(delete) if delete else None})
    result.sort(key=lambda r: (-r["d7"], r["person"]))
    return result


MOD_ORDER = {"PLENTY": 0, "PLENTY": 0, "NORMAL": 1}   # pace modes that may take work; TIGHT/TIGHT and unknown modes never do


def work_alma_rank(person_lines, today, roles=None):
    """Who takes the next piece of work (rules/WORKING-RULES.md, "Who picks next"): (sira, disarida).
    sira = names that may take work, best first: pace mode PLENTY/PLENTY before NORMAL, then the lowest forecast % at the target
    ("Forecast"; the weekly % when there is no forecast), then the name. disarida = [(name, reason)] of the people who never take
    work: role testci or yonetici in kisiler.json (only developers take board work), mode TIGHT/TIGHT, weekly use at 100 %, or no leaderboard
    data (no row in the last 7 days, or no mode / weekly %). roller: {name: role} (default: read_roles() of this clone; a name
    without a role is a developer). The single ranking used by the leaderboard and by the triage job's owner lease
    (tools/slack-triage/pencere.py)."""
    roles = read_roles() if roles is None else roles
    order, outside = [], []
    seen = set()
    for r in compute_ranking(person_lines, today):
        seen.add(r["person"])
        mod = (r.get("mod") or "").upper()
        if roles.get(r["person"]) in IS_ALMAZ:
            outside.append((r["person"], IS_ALMAZ[roles[r["person"]]]))
        elif not mod or r.get("quota") is None:
            outside.append((r["person"], "veri yok"))
        elif mod not in MOD_ORDER:
            outside.append((r["person"], "mod %s" % mod))
        elif r["quota"] >= 100:
            outside.append((r["person"], "haftalık %100"))
        else:
            t = r.get("forecast") if r.get("forecast") is not None else r["quota"]
            order.append((MOD_ORDER[mod], t, r["person"]))
    outside += [(k, IS_ALMAZ.get(roles.get(k), "veri yok")) for k in sorted(person_lines) if k not in seen]
    return [k for _, _, k in sorted(order)], outside


def trend_text(d7, previous):
    if not previous or previous <= 0:
        return "new"
    percent = int(round((d7 - previous) * 100.0 / previous))
    if abs(percent) < 3:
        return "≈ aynı"
    return ("▲ +%d%%" if percent > 0 else "▼ %d%%") % percent


def important_key(r):
    """What makes a person's row count as changed for the timer run: weekly %, plan, pace mode, forecast %, the day the limit is
    projected to run out (never the hour: the forecast moves it every few minutes), the Slack state word. Not part of it: the forecast
    time, the Slack last-poll time, the rolling 24 h / 30 d dollars, the week-to-date dollars (all of them drift with the clock alone).
    The 7-day dollars matter through the ranking (see order_key), not on their own."""
    b = r.get("end_at")
    return (r.get("quota"), r.get("plan"), r.get("mod"), r.get("forecast"), b if b in (None, ENOUGH) else b[:10], r.get("slack"))


def order_key(person_lines, today):
    """The ranking as a comparable value: [(name, important_key of the person's latest row)] in rank order. Two states with the same
    key look the same in RANKING.md apart from timestamps and the 7-day dollar figures."""
    return [(r["person"], important_key(r)) for r in compute_ranking(person_lines, today)]


def change_summary(previous, new, person):
    """(short text for the commit message, whether to tell the person) from two order_key lists: the rank move, mode, weekly %
    and Slack state of `person`. Telling = a rank or mode change; the other parts are only logged."""
    def find(order):
        for i, (name, k) in enumerate(order, 1):
            if name == person:
                return i, k
        return None, None
    (i0, k0), (i1, k1) = find(previous), find(new)
    k0, k1 = k0 or (None,) * 6, k1 or (None,) * 6
    part, news = [], False
    if i0 != i1 and i1 is not None:
        part.append("sıra %s→%d" % (i0 if i0 else "—", i1))
        news = True
    if k0[2] != k1[2] and k1[2]:
        part.append("mod %s→%s" % (k0[2] or "—", k1[2]))
        news = True
    if k0[0] != k1[0] and k1[0] is not None:
        part.append("kota %s→%%%d" % ("%%%d" % k0[0] if k0[0] is not None else "—", k1[0]))
    if k0[5] != k1[5] and k1[5]:
        part.append("slack %s→%s" % (k0[5] or "—", k1[5]))
    return " · ".join(part) or "güncelleme", news


MIN_QUOTA = 10   # a week's $-per-% is only quoted once at least this much of the limit is used (small % are mostly rounding)


def week_ratios(person_lines):
    """[(name, week id, plan, $ per 1% of the weekly limit)]: the latest row of each person-week with kota >= MIN_QUOTA."""
    result = []
    for person, ls in person_lines.items():
        last = {}
        for k in ls:
            if k["week"] and k["plan"] and k["quota"] is not None and k["quota"] >= MIN_QUOTA and k["week_usd"] is not None:
                last[k["week"]] = k
        result += [(person, h, k["plan"], k["week_usd"] / float(k["quota"])) for h, k in last.items()]
    return result


def average(x):
    return sum(x) / len(x) if x else None


def compare_plan(person_lines):
    """{plan: {kisi, hafta, oran, kapasite, aylik, plan_head}}: per plan the mean $ per 1% of the weekly limit, the implied weekly
    capacity at 100% ($), its monthly equivalent (x30/7) and that per plan dollar. Plans without data are absent."""
    groups = {}
    for person, _h_, plan, ratio in week_ratios(person_lines):
        g = groups.setdefault(plan, {"person": set(), "ratios": []})
        g["person"].add(person)
        g["ratios"].append(ratio)
    out = {}
    for plan, g in groups.items():
        ratio = average(g["ratios"])
        out[plan] = {"person": len(g["person"]), "week": len(g["ratios"]), "ratio": ratio, "capacity": ratio * 100,
                     "monthly": ratio * 100 * 30 / 7.0, "plan_head": ratio * 100 * 30 / 7.0 / plan}
    return out


def week_table(person_lines):
    """{week id: {plan: mean $ per 1%}}."""
    t = {}
    for _k, week, plan, ratio in week_ratios(person_lines):
        t.setdefault(week, {}).setdefault(plan, []).append(ratio)
    return {h: {p: average(v) for p, v in d.items()} for h, d in t.items()}


def day_pattern(person_lines):
    """{(plan, weekday 0=Mon): $ per 1% of the weekly limit that day}: from consecutive rows of one limit week, the growth of
    week-to-date $ over the growth of the limit %. The usage day is the day before the later row's date. Days with < 3 points of
    limit growth in total are left out."""
    add_up = {}
    for ls in person_lines.values():
        for a, b in zip(ls, ls[1:]):
            if ((_day(b["date"]) - _day(a["date"])).days != 1 or not b["plan"] or a["week"] != b["week"] or not b["week"]
                    or None in (a["quota"], b["quota"], a["week_usd"], b["week_usd"])):
                continue
            min_, du = b["quota"] - a["quota"], b["week_usd"] - a["week_usd"]
            if min_ < 0 or du < 0:
                continue
            g = (_day(a["date"])).weekday()
            t = add_up.setdefault((b["plan"], g), [0, 0])
            t[0] += du
            t[1] += min_
    return {k: v[0] / float(v[1]) for k, v in add_up.items() if v[1] >= 3}


def model_summary(person_lines, today):
    """{name: ({family: $}, {family: Mtok})} over the rows of the last 7 days (each row = the previous calendar day)."""
    t0 = _day(today)
    out = {}
    for person, ls in person_lines.items():
        u, t = {}, {}
        for k in ls:
            if 0 <= (t0 - _day(k["date"])).days <= 6:
                for f, v in k["usd"].items():
                    u[f] = u.get(f, 0.0) + v
                for f, v in k["tok"].items():
                    t[f] = t.get(f, 0.0) + v
        if u:
            out[person] = (u, t)
    return out


MEDAL = {1: "🥇", 2: "🥈", 3: "🥉"}
MODEL_NAMES = [(a, name) for a, name in MODELS] + [("other", "Diğer")]


def time_text(t):
    """'dd.mm HH:MM' (Istanbul time, UTC+3 on every machine) of an epoch or ISO string; '—' when unknown."""
    try:
        if isinstance(t, str):
            t = datetime.datetime.fromisoformat(t).timestamp()
        return datetime.datetime.fromtimestamp(t, TR).strftime("%d.%m %H:%M")
    except (TypeError, ValueError, OSError):
        return "—"


MOD_RANK = {"PLENTY": 0, "NORMAL": 1, "TIGHT": 2}


def upcoming_work_lines(order, roles=None):
    """"Sıradaki işi kim alır" section (WORKING-RULES, hand-overs return work to the board: the most unused quota picks next):
    developers ordered by pace mode (PLENTY, NORMAL, TIGHT), then by lowest forecast %; no mode or forecast = last, marked "veri yok".
    Testers and managers (roles testci, yonetici; roller as in work_alma_rank) never take board work: they are listed under a
    "testçi (iş almaz)" / "yönetici (iş almaz)" note."""
    roles = read_roles() if roles is None else roles

    def key(r):
        m, t = MOD_RANK.get(r.get("mod")), r.get("forecast")
        return (m is None or t is None, m if m is not None else 9, t if t is not None else 0, r["person"])
    L = ["", "### Sıradaki işi kim alır", "",
         "Devredilen iş panoya döner; panodan yalnız yazılımcılar alır, en çok kullanılmamış kotası olan önce: önce mod (PLENTY, NORMAL, TIGHT), sonra en düşük tahmin %.", "",
         "| # | Kişi | Mod | Tahmin |", "|---|---|---|---:|"]
    for i, r in enumerate(sorted((r for r in order if roles.get(r["person"]) not in IS_ALMAZ), key=key), 1):
        data = r.get("mod") in MOD_RANK and r.get("forecast") is not None
        L.append("| %d | %s | %s | %s |" % (i, r["person"].split()[0], r["mod"] if data else "veri yok", "%%%d" % r["forecast"] if data else "—"))
    for role, not_ in IS_ALMAZ.items():
        names = [r["person"].split()[0] for r in sorted(order, key=lambda r: r["person"]) if roles.get(r["person"]) == role]
        if names:
            L += ["", "_%s: %s_" % (not_, ", ".join(names))]
    return L


def ranking_text(person_lines, today, github=None, last_time=None, week=None, roles=None):
    """Markdown of RANKING.md (Turkish). github: {name: 7-day commits from GitHub} overrides the sum of the row figures;
    last_time: {name: epoch/ISO time of the person's latest row}. hafta: {name: weekly summary of cost_efficiency} for the finished-work
    section (none = the section is left out). roller: {name: role} for "Sıradaki işi kim alır" (default: read_roles())."""
    github, last_time, week = github or {}, last_time or {}, week or {}
    L = ["# 🏆 Claude kullanımı — sıralama ve plan karşılaştırması (%s)" % today, "",
         "Bu dosya her `cost --share` çalıştırmasında `raporlar/kullanim/*.md` dosyalarından yeniden üretilir; elle düzenlemeyin. "
         "Dolar = API liste fiyatı karşılığı (fatura değil). Kullanılmayan haftalık kota sıfırlanmada yanar.", "",
         "## Last 7 gün", ""]
    order = compute_ranking(person_lines, today)
    if not order:
        L.append("_Son 7 günde satır yok. Katılmak için README'deki \"Günlük kullanım ve sıralama\" bölümü._")
    else:
        old = (_day(today) - datetime.timedelta(days=1)).isoformat()
        flag = lambda r: "" if r["last_date"] >= old else " ⏳ " + r["last_date"]
        L += ["### Kota", "",
              "| # | Kişi | Haftalık % | Tahmin | Tahmini bitiş | Sıfırlanma | Mod | Slack gelen | Last güncelleme |", "|---|---|---:|---:|---|---|---|---|---|"]
        for i, r in enumerate(order, 1):
            L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                MEDAL.get(i, str(i)), r["person"].split()[0] + flag(r),
                "%%%d" % r["quota"] if r["quota"] is not None else "—",
                "%%%d" % r["forecast"] if r.get("forecast") is not None else "—", end_at_text(r.get("end_at")), reset_text(r.get("reset")), r.get("mod") or "—",
                slack_text(r.get("slack"), r.get("slack_son")),
                time_text(last_time.get(r["person"])) if r["person"] in last_time else r["last_date"]))
        L += ["", "### Maliyet ve verim", "",
              "| # | Kişi | Plan | 7 gün | Geçen haftaya göre | Commit (7 gün) | Satır (7 gün) | $ / 1000 satır |", "|---|---|---|---:|---|---:|---:|---:|"]
        for i, r in enumerate(order, 1):
            L.append("| %s | %s | %s | $%d | %s | %s | %s | %s |" % (
                MEDAL.get(i, str(i)), r["person"] + flag(r), "$%d" % r["plan"] if r["plan"] else "—", r["d7"], trend_text(r["d7"], r["previous_d7"]),
                _h(github.get(r["person"], r["commit"])), line_text(r["add"], r["delete"]), usd_bin_line(r["d7"], r["add"], r["delete"])))
        L += ["", "Ekip toplamı: $%d · %d kişi. ⏳ = son satırı dünden eski. Tahmini bitiş = haftalık limitin tahmini bittiği an (yerel tahmin); "
                  "\"—\" = kişinin yeni sürümle ilk günlük çalışması bekleniyor. Slack gelen = kişinin token harcamayan Slack gelen kutusu "
                  "(`install.sh --slack-inbox`): açık / hata (son yoklama başarısız) / kapalı / kurulu değil ve son yoklama zamanı; mesaj içeriği paylaşılmaz." % (sum(r["d7"] for r in order), len(order))]
        L += upcoming_work_lines(order, roles)
        if week:
            L += [""] + V.table_lines([r["person"] for r in order], week, today, MEDAL)
    L += ["", "## Plan karşılaştırması: $100 mı $200 mü?", "",
          "Ölçü: haftalık limitin **%%1'i kaç $ karşılığı kullanım** ediyor (limit haftasının dolar toplamı ÷ kota %%; kota ≥ %d olan haftalar). "
          "Yüksek = aynı %% ile daha çok iş. Aylık karşılık = %%100 × 30/7." % MIN_QUOTA, ""]
    pk = compare_plan(person_lines)
    if not pk:
        L.append("_Henüz yeterli veri yok (plan, kota ve hafta bilgisi olan satırlar gerekir)._")
    else:
        L += ["| Plan | Kişi | Hafta | $ / %1 kota | Haftalık kapasite ($) | Aylık karşılık ($) | Aylık karşılık ÷ plan fiyatı |", "|---|---:|---:|---:|---:|---:|---:|"]
        for p in sorted(pk):
            v = pk[p]
            L.append("| $%d | %d | %d | %.2f | %.0f | %.0f | %.1f× |" % (p, v["person"], v["week"], v["ratio"], v["capacity"], v["monthly"], v["plan_head"]))
        if 100 in pk and 200 in pk:
            k = pk[200]["ratio"] / pk[100]["ratio"]
            L += ["", "$200 planı, $100 planına göre **%.2f×** kapasite veriyor (fiyat 2×). %s" % (
                k, "Ek $100 karşılığını veriyor." if k >= 1.8 else "Ek $100'ın karşılığı bu veride görünmüyor." if k < 1.3 else
                "Ek $100'ın karşılığı kısmen görünüyor.")]
        else:
            L += ["", "_Karşılaştırma için iki plandan da veri gerekiyor._"]
    ht = week_table(person_lines)
    if ht:
        L += ["", "## Hafta hafta: %1 kota kaç $ yaptı", "", "| Limit haftası (bitiş) | $100 | $200 |", "|---|---:|---:|"]
        for h in sorted(ht)[-8:]:
            L.append("| %s | %s | %s |" % (h, *("%.2f" % ht[h][p] if p in ht[h] else "—" for p in PLANS)))
    gd = day_pattern(person_lines)
    if gd:
        L += ["", "## Haftanın günü: %1 kota kaç $ yaptı", "", "| Gün | $100 | $200 |", "|---|---:|---:|"]
        for g in range(7):
            if any((p, g) in gd for p in PLANS):
                L.append("| %s | %s |" % (DAY_NAMES[g], " | ".join("%.2f" % gd[(p, g)] if (p, g) in gd else "—" for p in PLANS)))
    mo = model_summary(person_lines, today)
    if mo:
        L += ["", "## Modeller (son 7 gün: dolar payı · milyon token)", "",
              "| # | Kişi | " + " | ".join(name for _, name in MODEL_NAMES) + " |", "|---|---|" + "---:|" * len(MODEL_NAMES)]
        total_u, total_t = {}, {}
        ordered = [r["person"] for r in order if r["person"] in mo] + sorted(k for k in mo if k not in {r["person"] for r in order})   # same order as the tables above
        for i, person in enumerate(ordered, 1):
            u, t = mo[person]
            top = sum(u.values()) or 1.0
            L.append("| %s | %s | %s |" % (MEDAL.get(i, str(i)), person.split()[0], " | ".join("%d%% · %.0f" % (round(100 * u.get(f, 0.0) / top), t.get(f, 0.0)) for f, _ in MODEL_NAMES)))
            for f in u:
                total_u[f] = total_u.get(f, 0.0) + u[f]
            for f in t:
                total_t[f] = total_t.get(f, 0.0) + t[f]
        top = sum(total_u.values()) or 1.0
        L.append("| | **Ekip** | %s |" % " | ".join("%d%% · %.0f" % (round(100 * total_u.get(f, 0.0) / top), total_t.get(f, 0.0)) for f, _ in MODEL_NAMES))
    L += ["", "Nasıl daha az parayla daha çok iş: [HOW-SAVINGS.md](HOW-SAVINGS.md).", ""]
    return "\n".join(L)


# ------------------------------------------------------------------ git (the job's own clone)
def clone_path():
    return os.environ.get("CLAUDE_SHARED_CLONE") or os.path.join(HOME, ".cache", "claude-shared-usage")


def remote_address():
    return setting_value("remote", "CLAUDE_SHARED_REMOTE")


def git(clone, *args, timeout=GIT_TIME_OVERRUN):
    """(returncode, output) of a git command in the job's own clone (credential helpers stay on for the push; hooks are off)."""
    kom = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", "-c", "gc.auto=0", "-C", str(clone), *args]
    try:
        r = hidden.run(kom, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           env=GIT_ENV, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as e:
        kom_error("git " + (args[0] if args else ""), type(e).__name__)
        return 1, type(e).__name__
    if r.returncode != 0 and not (args[:1] == ("diff",) and r.returncode == 1):     # `diff --quiet` answers 1 when there is a difference
        kom_error("git " + (args[0] if args else ""), r.returncode, r.stdout + r.stderr)
    return r.returncode, (r.stdout + r.stderr).strip()


def prepare_clone(clone, remote):
    """The job's clone, reset to the remote main (it is the job's own: nothing in it is precious). Raises RuntimeError."""
    if not remote:
        raise RuntimeError(REMOTE_MISSING)
    if not os.path.isdir(os.path.join(clone, ".git")):
        os.makedirs(os.path.dirname(clone), exist_ok=True)
        try:
            r = hidden.run(["git", "clone", "--quiet", "--branch", "main", remote, clone], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=300, env=GIT_ENV, stdin=subprocess.DEVNULL)
        except (subprocess.TimeoutExpired, OSError) as e:
            kom_error("git clone", type(e).__name__)
            raise RuntimeError("clone: %s" % type(e).__name__)
        if r.returncode != 0:
            kom_error("git clone", r.returncode, r.stderr or r.stdout)
            raise RuntimeError("clone: " + (r.stderr or r.stdout).strip().replace("\n", " ")[:200])
    for args in (("fetch", "--quiet", "origin", "main"), ("checkout", "--quiet", "-B", "main", "origin/main"),
                 ("reset", "--quiet", "--hard", "origin/main"), ("clean", "-fdq")):
        rc, out = git(clone, *args)
        if rc != 0:
            raise RuntimeError("git %s: %s" % (args[0], out.replace("\n", " ")[:200]))


ROLES = ("developer", "tester", "manager")   # kisiler.json roles; only "developer" takes board work and owns triage
IS_ALMAZ = {"tester": "testçi (iş almaz)", "manager": "yönetici (iş almaz)"}   # roles that never take work, with their note


def person_parse(data):
    """{github login (lower case): (display name, role)} from parsed kisiler.json, the single parser of that file. An entry is either
    "Name" (old format = role yazilimci, so old clones keep working) or {"name": "Name", "role": "developer"|"tester"|"manager"}; a missing or
    unknown role is yazilimci. Invalid entries are skipped; {} when `data` is not an object."""
    if not isinstance(data, dict):
        return {}
    out = {}
    for l, v in data.items():
        name, role = (v, None) if isinstance(v, str) else ((v.get("name"), v.get("role")) if isinstance(v, dict) else (None, None))
        if isinstance(l, str) and isinstance(name, str) and clean_name(name):
            out[l.lower()] = (clean_name(name), role if role in ROLES else "developer")
    return out


def people_json(clone):
    """person_parse of kisiler.json in the clone; {} when unreadable."""
    try:
        with open(os.path.join(clone, *PEOPLE_PATH.split("/")), encoding="utf-8") as f:
            return person_parse(json.load(f))
    except (OSError, ValueError):
        return {}


def read_people(clone):
    """{github login (lower case): display name} from kisiler.json in the clone; {} when unreadable."""
    return {l: a for l, (a, _r) in people_json(clone).items()}


def read_roles(clone=None):
    """{display name: role} from kisiler.json in the clone (default: the clone this script runs from)."""
    clone = clone or os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
    return {a: r for a, r in people_json(clone).values()}


def read_files(clone, names):
    """{name: [records]} of the files of the listed names only (a stray file nobody listed is ignored)."""
    out = {}
    for name in sorted(set(names)):
        path = os.path.join(clone, *REPORT_DIR.split("/"), file_name(name) + ".md")
        try:
            with open(path, encoding="utf-8") as f:
                out[name] = file_lines(f.read())
        except OSError:
            pass
    return out


def read_week(clone, names):
    """{name: weekly summary} of the <kisi>.hafta.json sidecars of the listed names only (cost_efficiency)."""
    out = {}
    for name in sorted(set(names)):
        o = V.read_summary(V.file_path(os.path.join(clone, *REPORT_DIR.split("/")), file_name(name)))
        if o:
            out[name] = o
    return out


def last_times(clone, names, now=None):
    """{name: epoch of the person's latest row}: the commit time of their file in the clone (rows are only added by the daily
    job, which commits them at once); a file with uncommitted changes (the row being written now) counts as now."""
    now = time.time() if now is None else now
    out = {}
    for name in sorted(set(names)):
        path = REPORT_DIR + "/" + file_name(name) + ".md"
        rc, status = git(clone, "status", "--porcelain", "--", path)
        if rc == 0 and status.strip():
            out[name] = now
            continue
        rc, t = git(clone, "log", "-1", "--format=%ct", "--", path)
        if rc == 0 and t.strip().isdigit():
            out[name] = int(t.strip())
    return out


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def one_attempt(clone, remote, record, today, automatic=False, change=False):
    """Fetch, add this person's row, rebuild the ranking, commit, push. Returns 'itildi', 'degisiklik yok' or raises RuntimeError.
    otomatik (status-line trigger): only the weekly % (integer, as the table shows it) decides: same as today's pushed row = nothing
    is written, otherwise today's row is replaced in place.
    degisim (the 2-hourly timer run): the ranking as it stands on origin/main is compared with the ranking including this row (see
    order_key / important_key: timestamp-only drift never counts). Same = nothing is written, nothing committed. Different, or
    the first run of a date = today's row is replaced in place, RANKING.md is regenerated, the commit message says what moved
    (kayit["summary"], kayit["news"] carry that text and whether a rank or mode change is worth telling the person)."""
    prepare_clone(clone, remote)
    lookup = read_people(clone)
    person = record["person"]
    if person not in lookup.values():
        raise RuntimeError("'%s' kisiler.json icinde yok: satir yazilmadi" % person)
    path = os.path.join(clone, *REPORT_DIR.split("/"), file_name(person) + ".md")
    try:
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    except OSError:
        existing = ""
    apply_scope(record, file_lines(existing))
    summary = ""
    week_path = V.file_path(os.path.join(clone, *REPORT_DIR.split("/")), file_name(person))
    week_old = read_week(clone, lookup.values())
    week_new = dict(week_old)
    if record.get("week_summary"):   # this run's weekly summary; the finished count falls back to the published one of the same week
        ho = dict(record["week_summary"])
        previous_h = week_old.get(person) or {}
        if ho.get("done") is None and previous_h.get("week") == ho.get("week"):
            ho["done"] = previous_h.get("done")
        week_new[person] = ho
    efficiency_same = V.efficiency_key(week_old, today) == V.efficiency_key(week_new, today)
    if change:
        old = read_files(clone, lookup.values())
        new_r = line_parse(install_line(record))
        new = {**old, person: [r for r in old.get(person, []) if r["date"] != record["date"]] + [new_r]}
        new[person].sort(key=lambda r: r["date"])
        previous_order, new_order = order_key(old, today), order_key(new, today)
        today_present = any(r["date"] == record["date"] for r in old.get(person, []))
        if today_present and previous_order == new_order and efficiency_same:
            return "degisiklik yok"
        summary, record["news"] = change_summary(previous_order, new_order, person)
        if not efficiency_same and previous_order == new_order and today_present:   # only the finished count / weekly dollars moved: say so, no desktop notification
            b0, b1 = (week_old.get(person) or {}).get("done"), (week_new.get(person) or {}).get("done")
            summary = "biten iş %s→%s" % (b0 if b0 is not None else "—", b1 if b1 is not None else "—") if b0 != b1 else "haftalık cost güncellendi"
        record["summary"] = summary
    if automatic:
        todays = [r for r in file_lines(existing) if r["date"] == record["date"]]
        if todays and todays[0].get("quota") == record.get("quota"):
            return "degisiklik yok"
    write(path, add_to_file(existing, person, install_line(record), record["date"], quota_update=automatic, instead=change))
    if record.get("week_summary"):
        write(week_path, V.summary_write_text(week_new[person]))
    every = read_files(clone, lookup.values())
    write(os.path.join(clone, *REPORT_DIR.split("/"), RANKING_FILE),
        ranking_text(every, today, github_commitleri(lookup, today), last_times(clone, lookup.values()), read_week(clone, lookup.values()), read_roles(clone)))
    git(clone, "add", "--", REPORT_DIR)
    rc, _ = git(clone, "diff", "--cached", "--quiet")
    if rc == 0:
        return "degisiklik yok"
    message = "chore(kullanim): %s %s" % (person, today) + (" · " + summary if summary else "")
    rc, out = git(clone, "commit", "--quiet", "-m", message)
    if rc != 0 and "identity" in out.lower():   # no git identity configured on this machine: commit under the person's name
        rc, out = git(clone, "-c", "user.name=" + person, "-c", "user.email=kullanim@claude-shared.invalid", "commit", "--quiet", "-m", message)
    if rc != 0:
        raise RuntimeError("commit: " + out.replace("\n", " ")[:200])
    rc, out = git(clone, "push", "--quiet", "origin", "HEAD:main")
    if rc != 0:
        raise RuntimeError("push: " + out.replace("\n", " ")[:200])
    return "itildi"


def access_check(person=None, clone=None, remote=None):
    """(ok, reason): can this machine do what the daily job does? Clone/fetch the shared repo into the job's own clone, check
    that `person` is listed in kisiler.json on main, and `git push --dry-run` (contacts the remote and authenticates, writes
    nothing). Used by the installer's --share; `cost --share --dry` only prints the row and never reaches git."""
    clone, remote = clone or clone_path(), remote or remote_address()
    try:
        prepare_clone(clone, remote)
    except RuntimeError as e:
        return False, str(e)
    if person is not None and person not in read_people(clone).values():
        return False, "'%s' ortak deponun main dalindaki kisiler.json icinde yok (once ekleyip main'e commitleyin)" % person
    rc, out = git(clone, "push", "--dry-run", "--quiet", "origin", "HEAD:main")
    return rc == 0, "" if rc == 0 else "push: " + out.replace("\n", " ")[:200]


# ------------------------------------------------------------------ the status line's automatic share
# statusline.sh / statusline.ps1 start `cost --share --automatic` detached when the marker file share-following (its mtime = the
# next due time) is in the past. State lives in <claude dir>/cost/: share-status.json (son_kontrol, last_sharing, pushed_quota),
# share-following (the shell's one-stat gate), paylas.lock (one share at a time; older than AUTO_LOCK_OLD s = stale = free).
AUTO_INTERVAL_HOUR = 2.0
AUTO_LOCK_OLD = 30 * 60
PUSH_ATTEMPT = 3


def oto_yol(name):
    import cost
    return os.path.join(cost.CLAUDE, "cost", name)


def auto_interval_hour():
    """Hours between checks: `share_interval_hour` in <claude dir>/pace-plan.json (a positive number), else AUTO_INTERVAL_HOUR."""
    import cost
    try:
        with open(os.path.join(cost.CLAUDE, "pace-plan.json"), encoding="utf-8-sig") as f:
            v = json.load(f).get("share_interval_hour")
    except (OSError, ValueError, AttributeError):
        return AUTO_INTERVAL_HOUR
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 24 * 30 else AUTO_INTERVAL_HOUR


def read_auto_status():
    try:
        with open(oto_yol("share-status.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def auto_required(status, now, interval_hour):
    """True when the last check is at least `interval_hour` hours old (or there never was one)."""
    last = status.get("son_kontrol")
    if not isinstance(last, (int, float)) or isinstance(last, bool):
        return True
    return now - last >= interval_hour * 3600


def auto_decision(quota, status):
    """'skip' (no usable weekly %), 'degisiklik yok' (same integer % as the last push) or 'it'. Nothing else counts."""
    if quota is None:
        return "skip"
    return "degisiklik yok" if status.get("pushed_quota") == quota else "it"


def oto_yaz(status, now, interval_hour):
    """Save the state and move the marker to the next due time."""
    status["son_kontrol"] = now
    path = oto_yol("share-status.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f)
        os.replace(tmp, path)
        marker = oto_yol("share-following")
        with open(marker, "a"):
            pass
        os.utime(marker, (now, now + interval_hour * 3600))
    except OSError as e:
        log_yaz("uyari: paylas durumu yazilamadi: %s" % type(e).__name__)


def get_auto_lock(now):
    """Take paylas.lock (O_EXCL). A lock older than AUTO_LOCK_OLD seconds is stale and taken over. False = a share is running."""
    path = oto_yol("share.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for _ in (1, 2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if now - os.stat(path).st_mtime < AUTO_LOCK_OLD:
                    return False
                os.unlink(path)
            except OSError:
                return False
        except OSError:
            return False
    return False


def auto_lock_release():
    try:
        os.unlink(oto_yol("share.lock"))
    except OSError:
        pass


# ------------------------------------------------------------------ the job
def share(ranking=False, dry=False, now=None, automatic=False, prepare=None):
    """The whole daily job after the totals are recorded. Never raises; returns 0. (`ranking` is kept for old drop-ins.)
    otomatik: the status line's throttled run (see above); `prepare` records the totals, called only when something will be pushed."""
    try:
        return _auto(now, prepare) if automatic else _share(dry, now)
    except Exception as e:  # never fail loudly: the daily recording must not break because sharing did
        log_yaz("HATA %s: %s" % (type(e).__name__, e))
        if automatic:   # a crashing run must not be restarted by every status-line render: wait for the next interval
            try:
                oto_yaz(read_auto_status(), time.time(), auto_interval_hour())
            except Exception:
                pass
        return 0


def _it(record, today, automatic=False, change=False):
    """Push attempts (each one re-fetches and resets the job's clone to origin/main, then redoes the row on the fresh base, which is
    what a pull --rebase would give). Returns (result or None, error)."""
    clone, remote, error = clone_path(), remote_address(), None
    for attempt in range(PUSH_ATTEMPT):
        try:
            return one_attempt(clone, remote, record, today, automatic, change), None
        except RuntimeError as e:
            error = str(e)
            if "people.json" in error or error == REMOTE_MISSING:
                break
            if attempt < PUSH_ATTEMPT - 1:
                time.sleep(1 + random.random() * 3)
    return None, error


def _auto(now, prepare):
    now = time.time() if now is None else now
    interval = auto_interval_hour()
    if not get_auto_lock(now):
        log_yaz("otomatik: baska bir paylasim suruyor, atlandi")
        return 0
    try:
        return _oto_is(now, prepare, interval)
    finally:
        auto_lock_release()


def _oto_is(now, prepare, interval):
    import cost_core as CK
    status = read_auto_status()
    if not auto_required(status, now, interval):
        oto_yaz(status, status["son_kontrol"], interval)   # only re-aims the marker; the check time stays
        return 0
    person = person_name()
    if not person:
        oto_yaz(status, now, interval)
        log_yaz("otomatik atlandi: kisi adi yok")
        return 0
    quota, _ = quota_info(now)
    decision = auto_decision(quota, status)
    if decision != "it":
        oto_yaz(status, now, interval)
        log_yaz("otomatik: %s (kota %s)" % (decision, quota))
        return 0
    if prepare:
        prepare()
    today = CK.today_str()
    record = todays_record(person, today, now)
    result, error = _it(record, today, automatic=True)
    if result:
        status["pushed_quota"] = record["quota"]
        if result == "itildi":
            status["last_sharing"] = now
        log_yaz("otomatik %s: %s" % (result, install_line(record)))
    else:
        log_yaz("otomatik BASARISIZ: %s" % error)
    oto_yaz(status, now, interval)   # a failed push waits for the next interval too
    return 0


def notify(person, summary):
    """Desktop notification on this machine (notify-send, the same mechanism as lost_work.py and gelen.py) for a rank or mode change
    of the person's own row. Linux only, best effort, silent when notify-send is missing; CLAUDE_SHARED_NOTIFIER=0 switches it off.
    Nothing is sent to anyone else."""
    if os.environ.get("CLAUDE_SHARED_NOTIFIER") == "0" or not sys.platform.startswith("linux") or not shutil.which("notify-send"):
        return False
    rc, _, _ = command(["notify-send", "-a", "claude-usage", "Claude kullanımı: %s" % person, summary], timeout=10)
    return rc == 0


def prepare_week_summary(person, today):
    """The weekly per-project cost summary plus the finished-work count (cost_efficiency) of this person, or None when it cannot be built
    (no tools/ next to the plugin, no cost data, or CLAUDE_SHARED_EFFICIENCY=0). Never raises; the daily row does not depend on it."""
    if os.environ.get("CLAUDE_SHARED_EFFICIENCY") == "0":
        return None
    try:
        ho = V.week_summary(person, today)
        if ho is None:
            return None
        login = next((l for l, a in people_local().items() if a == person), None)
        ho["done"] = V.done_count(login, today) if login else None
        return ho
    except Exception as e:
        log_yaz("hafta ozeti hatasi: %s: %s" % (type(e).__name__, e))
        return None


def people_local():
    """{login: name} from the kisiler.json next to this plugin (the clone this script runs from); {} when unreadable."""
    return read_people(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")))


def _share(dry, now):
    import cost_core as CK
    person = person_name()
    if not person:
        log_yaz("atlandi: kisi adi yok (CLAUDE_SHARED_PERSON, ~/.config/claude-shared/kisi veya git user.name)")
        return 0
    today = CK.today_str()
    record = todays_record(person, today, now)
    if record["plan"] is None:
        log_yaz("uyari: plan bilinmiyor (CLAUDE_SHARED_PLAN veya ~/.config/claude-shared/plan: 100 ya da 200); satirda plan '—'")
    if dry:
        print(install_line(record))
        return 0
    record["week_summary"] = prepare_week_summary(person, today)
    # the timer run and the status line's automatic share use the same clone: one at a time (a busy lock = the next 2-hourly run retries)
    if not get_auto_lock(time.time()):
        log_yaz("paylas: baska bir paylasim suruyor, bu tur atlandi")
        return 0
    try:
        result, error = _it(record, today, change=True)
    finally:
        auto_lock_release()
    if result:
        log_yaz("%s: %s" % (result, install_line(record)) if result == "itildi" else "degisiklik yok (satir ve sira ayni, zaman damgasi disinda)")
        if result == "itildi":
            log_yaz("degisim: " + record.get("summary", ""))
            if record.get("news"):
                notify(person, record["summary"])
        if record.get("quota") is not None:   # the row is the new baseline for the automatic share
            status = read_auto_status()
            status["pushed_quota"] = record["quota"]
            if result == "itildi":
                status["last_sharing"] = time.time()
            oto_yaz(status, time.time(), auto_interval_hour())
        return 0
    log_yaz("BASARISIZ: %s" % error)
    return 0

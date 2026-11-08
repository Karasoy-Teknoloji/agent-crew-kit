"""The team layer of the kit: the ONE reader of config keys and the optional team migration hook.

Config keys (README, "Configuration"). value(file, env, default) returns the first non-empty of:
  1. the environment variable `env`;
  2. the first line of ~/.config/claude-shared/<file> (this person's own setting; Windows: USERPROFILE\\.config);
  3. the first line of <clone>/team-config/<file> (committed by a downstream team, so every member gets the team's values
     with a `git pull` instead of editing shared code or each machine's ~/.config). The environment variable
     CLAUDE_SHARED_TEAM_CONFIG_DIR replaces that directory (team_dir()); the kit's own tests point it at an empty temp dir, so a
     test run inside a downstream fork never reads the team's committed values;
  4. `default`.
Personal keys (person, plan, root) pass team=False: a committed team value would give every member the same name or plan.
<clone> is the repo this file sits in (tools/..). A legacy --copy install has no clone next to its copy, so it reads 1, 2 and 4 only.

Team migration hook: when <clone>/tools/team_migrate.py exists (a downstream team's own script; the kit ships none), the installer
(before it reads the saved install record) and rules_watch.py's background run (before it reads quota-pace.json) call
team_migrate(), which runs `team_migrate.py --ensure` with a timeout. It fails open: a missing script is a no-op, and a failure or a
timeout is reported in the returned lines and never stops the caller. Runs are serialized with an O_EXCL lock file
(~/.claude/team-migrate.lock, stale after the timeout + 30 s). Stdlib only.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # python -I drops the script dir
from hidden_loader import hidden  # noqa: E402  (one hidden-launch helper)

CLONE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PERSONAL_DIR = os.path.join(os.path.expanduser("~"), ".config", "claude-shared")
TEAM_DIR = os.path.join(CLONE, "team-config")
TEAM_DIR_ENV = "CLAUDE_SHARED_TEAM_CONFIG_DIR"   # replaces TEAM_DIR when set and non-empty (tools/tests/fork_support.py sets it for tests)
MIGRATE = os.path.join(CLONE, "tools", "team_migrate.py")
MIGRATE_TIMEOUT = 120   # seconds; --ensure is meant to be idempotent and quick when there is nothing to do
LOCK = os.path.join(os.path.expanduser("~"), ".claude", "team-migrate.lock")   # one --ensure at a time (installer vs rules_watch)
LOCK_GRACE = 30         # a lock older than MIGRATE_TIMEOUT + this is a crashed run's and is taken over


def first_line(path):
    """The stripped first line of a UTF-8 file; "" when it cannot be read."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.readline().strip()
    except (OSError, ValueError):
        return ""


def team_dir():
    """The directory of layer 3: env CLAUDE_SHARED_TEAM_CONFIG_DIR, else <clone>/team-config."""
    return (os.environ.get(TEAM_DIR_ENV) or "").strip() or TEAM_DIR


def value(file, env=None, default="", team=True):
    """One config key in the order of the module doc: env `env`, ~/.config/claude-shared/<file>, <team_dir()>/<file>
    (only with team=True), then `default`. Always a stripped string."""
    v = (os.environ.get(env) or "").strip() if env else ""
    if not v and file:
        v = first_line(os.path.join(PERSONAL_DIR, file))
        if not v and team:
            v = first_line(os.path.join(team_dir(), file))
    return v or str(default).strip()


def _take_lock(timeout):
    """Create LOCK with O_EXCL. True when taken (or when no lock can be made at all: fail open), False when another run holds it."""
    try:
        os.makedirs(os.path.dirname(LOCK), exist_ok=True)
        os.close(os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        try:
            if time.time() - os.stat(LOCK).st_mtime <= timeout + LOCK_GRACE:
                return False
            os.unlink(LOCK)   # stale: the run that made it is gone
            os.close(os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except OSError:
            return False   # another run took it first
    except OSError:
        return True


def team_migrate(python=None, timeout=None):
    """Run the team's migration (`<clone>/tools/team_migrate.py --ensure`) when it exists. Returns its output lines for the caller to
    relay ([] when there is no script and nothing to say). Never raises: a failure or a timeout becomes one warning line.
    Runs are serialized by the lock file LOCK: while another run holds it, one line says so and this call is skipped."""
    if not os.path.isfile(MIGRATE):
        return []
    t = timeout or MIGRATE_TIMEOUT
    if not _take_lock(t):
        return ["team migration (tools/team_migrate.py --ensure) is already running elsewhere; skipped this time"]
    try:
        return _run_migrate(python, t)
    finally:
        try:
            os.unlink(LOCK)
        except OSError:
            pass


def _run_migrate(python, timeout):
    cmd = [python or sys.executable, "-I", MIGRATE, "--ensure"]
    try:
        r = hidden.run(cmd, cwd=CLONE, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    except subprocess.TimeoutExpired:
        return ["team migration (tools/team_migrate.py --ensure): no result within %d s; continuing without it" % timeout]
    except OSError as e:
        return ["team migration (tools/team_migrate.py --ensure) could not start: %s; continuing without it" % e]
    lines = [ln.rstrip() for ln in ((r.stdout or "") + (r.stderr or "")).splitlines() if ln.strip()]
    if r.returncode != 0:
        lines.append("team migration (tools/team_migrate.py --ensure) failed with exit code %d; continuing without it" % r.returncode)
    return lines

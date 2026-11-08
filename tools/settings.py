"""The team layer of the kit: the ONE reader of config keys and the optional team migration hook.

Config keys (README, "Configuration"). value(file, env, default) returns the first non-empty of:
  1. the environment variable `env`;
  2. the first line of ~/.config/claude-shared/<file> (this person's own setting; Windows: USERPROFILE\\.config);
  3. the first line of <clone>/team-config/<file> (committed by a downstream team, so every member gets the team's values
     with a `git pull` instead of editing shared code or each machine's ~/.config);
  4. `default`.
Personal keys (person, plan, root) pass team=False: a committed team value would give every member the same name or plan.
<clone> is the repo this file sits in (tools/..). A legacy --copy install has no clone next to its copy, so it reads 1, 2 and 4 only.

Team migration hook: when <clone>/tools/team_migrate.py exists (a downstream team's own script; the kit ships none), the installer
(before it reads the saved install record) and rules_watch.py's background run (before it reads quota-pace.json) call
team_migrate(), which runs `team_migrate.py --ensure` with a timeout. It fails open: a missing script is a no-op, and a failure or a
timeout is reported in the returned lines and never stops the caller. Stdlib only.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # python -I drops the script dir
from hidden_loader import hidden  # noqa: E402  (one hidden-launch helper)

CLONE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PERSONAL_DIR = os.path.join(os.path.expanduser("~"), ".config", "claude-shared")
TEAM_DIR = os.path.join(CLONE, "team-config")
MIGRATE = os.path.join(CLONE, "tools", "team_migrate.py")
MIGRATE_TIMEOUT = 120   # seconds; --ensure is meant to be idempotent and quick when there is nothing to do


def first_line(path):
    """The stripped first line of a UTF-8 file; "" when it cannot be read."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.readline().strip()
    except (OSError, ValueError):
        return ""


def value(file, env=None, default="", team=True):
    """One config key in the order of the module doc: env `env`, ~/.config/claude-shared/<file>, <clone>/team-config/<file>
    (only with team=True), then `default`. Always a stripped string."""
    v = (os.environ.get(env) or "").strip() if env else ""
    if not v and file:
        v = first_line(os.path.join(PERSONAL_DIR, file))
        if not v and team:
            v = first_line(os.path.join(TEAM_DIR, file))
    return v or str(default).strip()


def team_migrate(python=None, timeout=None):
    """Run the team's migration (`<clone>/tools/team_migrate.py --ensure`) when it exists. Returns its output lines for the caller to
    relay ([] when there is no script and nothing to say). Never raises: a failure or a timeout becomes one warning line."""
    if not os.path.isfile(MIGRATE):
        return []
    cmd = [python or sys.executable, "-I", MIGRATE, "--ensure"]
    try:
        r = hidden.run(cmd, cwd=CLONE, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout or MIGRATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return ["team migration (tools/team_migrate.py --ensure): no result within %d s; continuing without it" % (timeout or MIGRATE_TIMEOUT)]
    except OSError as e:
        return ["team migration (tools/team_migrate.py --ensure) could not start: %s; continuing without it" % e]
    lines = [ln.rstrip() for ln in ((r.stdout or "") + (r.stderr or "")).splitlines() if ln.strip()]
    if r.returncode != 0:
        lines.append("team migration (tools/team_migrate.py --ensure) failed with exit code %d; continuing without it" % r.returncode)
    return lines

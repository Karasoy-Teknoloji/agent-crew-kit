#!/usr/bin/env python3
"""Shared installer logic for quota-pace (called by install.ps1 and install.sh).

Copies the scripts into ~/.claude, merges statusLine + subagentStatusLine + pace-hook entries into settings.json
(forward slashes only, idempotent), runs a self-check. Never creates pace-peer.json.
--direct copies nothing: settings.json, the cost command and the daily job point at the scripts in this claude-shared clone (the
plugin folder has the same layout as ~/.claude), old copies are removed, and ~/.claude/quota-pace.json records the mode so
tools/rules_watch.py keeps the clone up to date (git pull --ff-only, then this self-check, rolled back on a failure). Runtime data
stays in ~/.claude. --probe runs only the quick self-check (no install, no first scan); rules_watch.py calls it after a pull.
The cost statistics are part of the default install (bin/cost*.py, price table, dashboard template, a `cost`
command and a per-user job that records the totals every 2 hours: a systemd user timer on Linux, a launchd user agent on macOS, a Task
Scheduler task on Windows; without systemd the status-line background refresh records them). --free leaves them out (--cost
is accepted and means the default). --remove always removes them too (the recorded history under ~/.claude/cost is kept).
--share [--person "First Last"] [--plan 100|200] joins the team usage ranking: checks the name against people.json and push access to the
shared repo (nothing is written when a check fails), writes ~/.config/claude-shared/person and plan (the files cost_share.py reads),
and makes that job run `--save --share` (it commits only when the row or the ranking changed).
Every install (any mode) also sets up the team's lost-work protection from this clone, nothing copied: the SessionStart hook
tools/rules_watch.py (rule warnings + the cached lost_work.py result, refreshed in the background; silent when the clone is missing) and a daily job running
tools/daily_lost_work.py (= lost_work.py --push-unpushed --clean-lossless --cache-write; Windows task / systemd user timer 'lost-work', 17:30). --remove removes both.
Run with `python -I`. Output is English.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
STAMP = datetime.date.today().isoformat()
EVENTS = ("SessionStart", "UserPromptSubmit")
# (source file, destination relative to ~/.claude, platform: "win" / "posix" / "both"). The plugin folder has the same layout as
# ~/.claude (status line scripts at the top, everything else in bin/), so the scripts find their siblings relative to themselves.
FILES = [
    ("bin/pace_forecast.py", "bin/pace_forecast.py", "both"),
    ("bin/pace-hook.py", "bin/pace-hook.py", "both"),
    ("statusline.ps1", "statusline.ps1", "win"),
    ("bin/statusline-local.ps1", "bin/statusline-local.ps1", "win"),
    ("statusline.sh", "statusline.sh", "posix"),
    ("bin/line_wrap.py", "bin/line_wrap.py", "posix"),     # width wrap for statusline.sh (Windows wraps inside statusline-local.ps1)
    # the sub-agent rows live in the sibling plugin crew-agents (one source); the installed copy finds the cost tools in ~/.claude/bin
    ("../crew-agents/tools/subagent-statusline.py", "bin/subagent-statusline.py", "both"),
    # the one hidden-launch helper (tools/hidden.*): the bin scripts and statusline.ps1 find it at ../../tools in the repo layout, in bin/ in a copy
    ("../../tools/hidden.py", "bin/hidden.py", "both"),
    ("../../tools/hidden_loader.py", "bin/hidden_loader.py", "both"),   # the one import of hidden (falls back to plain subprocess if hidden.py is missing)
    ("../../tools/hidden.ps1", "bin/hidden.ps1", "win"),
]
# Optional cost statistics (source relative to this folder, destination relative to ~/.claude, platform)
COST_FILES = [
    ("bin/cost.py", "bin/cost.py", "both"),
    ("bin/cost_core.py", "bin/cost_core.py", "both"),
    ("bin/cost_parse.py", "bin/cost_parse.py", "both"),
    ("bin/cost_share.py", "bin/cost_share.py", "both"),
    ("../../tools/runlog.py", "bin/runlog.py", "both"),     # the rolling run log of the scheduled job (a clone install finds it in tools/)
    ("../../tools/settings.py", "bin/settings.py", "both"),   # the one config-key reader of cost_share / cost_ratio / last_window
    ("bin/cost_incremental.py", "bin/cost_incremental.py", "both"),
    ("bin/cost_ratio.py", "bin/cost_ratio.py", "both"),
    ("bin/cost_fx.py", "bin/cost_fx.py", "both"),
    ("bin/cost-prices.json", "bin/cost-prices.json", "both"),
    ("bin/cost-panel.html", "bin/cost-panel.html", "both"),
]
# Optional "last window" mode (install.sh --last-window): the last-window wrapper, its coordinator prompt; needs the cost/share modules above
LAST_WINDOW_FILES = [
    ("bin/last_window.py", "bin/last_window.py", "both"),
    ("bin/last-window-prompt.md", "bin/last-window-prompt.md", "both"),
]
UNIT = "cost-daily"
SP_UNIT = "last-window"
SP_TASK = "last-window"   # Windows Task Scheduler task name
LAUNCHD_LABEL = "com.agentcrewkit.cost-daily"   # launchd agent label; the plist is ~/Library/LaunchAgents/<label>.plist
EXIT_SHARE_OPTION = 2     # bad --share/--person/--plan (nothing written)
EXIT_SHARE_ACCESS = 3      # no push access to the shared repo (nothing written)
OUR_STATUS = ("statusline-local.ps1", "statusline.ps1", "statusline.sh")
OUR_SUBAGENT = "subagent-statusline.py"
SUB_KEY = "bin/subagent-statusline.py"
STATUS = "quota-pace.json"   # ~/.claude/<STATUS>: written by a direct-source (--direct) install, which is the default; tools/rules_watch.py reads it
# Its "setup" key holds the sharing options of the install (--share --person --plan --last-window); a bare re-run (`install.sh` with no
# options) reuses them, so repairing or moving the clone never drops a teammate out of the ranking.
# Team tools every install sets up from this clone (never copied, in every mode): the SessionStart hook tools/rules_watch.py (rule
# change warnings, direct-mode auto-update, and the cached lost-work result of tools/lost_work.py) and a daily lost-work job that pushes own
# unpushed branches and refreshes that cache (daily_lost_work.py -> lost_work.py --push-unpushed --cache-write: never main/master/test/dev, never force, never someone else's commits).
CLONE = SRC.parent.parent
sys.path.insert(0, str(CLONE / "tools"))   # tools/hidden.py: the one hidden-launch helper (python -I drops the script dir)
from hidden_loader import hidden  # noqa: E402
import settings  # noqa: E402  (tools/settings.py: the team layer, here its optional migration hook)
RULES_WATCH = CLONE / "tools" / "rules_watch.py"
HIDDEN_PS1 = CLONE / "tools" / "hidden.ps1"   # the one hidden-launch helper every Windows task snippet dot-sources
LOST = CLONE / "tools" / "lost_work.py"
LOST_ENTRY = CLONE / "tools" / "daily_lost_work.py"   # the ONE entry the daily job calls; its options live in the clone (no copy in the scheduler)
EVIDENCE = CLONE / "tools" / "evidence_hook.py"   # Stop + SubagentStop hook: hedge words, missing-access claims without a `vault_cli.py find` search, unrecorded decisions (Stop)
EVIDENCE_EVENTS = ("Stop", "SubagentStop")
TOOL_CHECK = CLONE / "tools" / "tool_check.py"   # tool check + the Windows python3 shim (install.ps1 -Tools / install.sh --tools installs)
GH_LIMIT = CLONE / "tools" / "gh_limit.py"   # gh shim ~/.local/bin/gh -> this clone's machine-wide GitHub API limiter
WATCHDOG = CLONE / "tools" / "agent_watchdog_note.py"   # PostToolUse (matcher "Agent") hook: reminds to keep the 30-min agent watchdog CronCreate
AGENT_NAME = CLONE / "plugins" / "crew-agents" / "tools" / "agent_name.py"   # PreToolUse (matcher "Agent|Task") hook: names every subagent
SG_DIR = CLONE / "tools" / "slack-triage"      # optional token-free Slack inbox poller (install.sh --slack-inbox, install.ps1 -SlackInbox), run from the clone
SG_INBOX = SG_DIR / "inbox.py"
SG_EVENTS = ("SessionStart", "UserPromptSubmit")   # hook events that print the unread count (status line shows it too)
SG_UNIT = "slack-inbox"                       # systemd user unit
SG_LABEL = "com.agentcrewkit.slack-inbox"         # launchd agent label
SG_TASK = "slack-inbox"                      # Windows Task Scheduler task name
SG_MINUTES = 3                                 # the Windows task starts at hh:03 and repeats every 5 min (:03, :08, ...), off the round minutes
# SessionStart + UserPromptSubmit hook: prints "Slack: N new" (only when unread items exist); silent when the clone is missing
SG_BACKUP = "import os,runpy,sys; p=sys.argv[1]; sys.argv=[p,'count']; os.path.isfile(p) and runpy.run_path(p, run_name='__main__')"
LOST_UNIT = "lost-work"      # systemd user unit
LOST_TASK = "lost-work"     # Windows Task Scheduler task
# The plugin plugins/crew-agents (agent tools, the proactive-concise output style) runs straight from this clone: the claude-shared
# marketplace is declared as a local `directory` source and the plugin is enabled in settings.json (merge_crew_agents), and
# `claude plugin install` registers it (claude_plugins; enabledPlugins alone does not install it: without the install record Claude
# Code does not load its output style). Claude Code then loads the plugin in place (no cache copy).
# The team agents are NOT in the plugin: Claude Code always prefixes plugin agents with the plugin name (`crew-agents:quick-task-hm`)
# and has no setting for the agents directory, so they live in <clone>/agents and ~/.claude/agents/claude-shared is a link to that
# directory (agent_link: a junction on Windows, no admin needed; a symlink elsewhere). Claude Code loads subdirectories of
# ~/.claude/agents, so a person's own agents stay beside it untouched; a link, not a copy, so a git pull updates the agents.
AGENT_SOURCE = CLONE / "agents"
AGENT_LINK = "claude-shared"                               # ~/.claude/agents/<AGENT_LINK> -> AGENT_SOURCE
MARKETPLACE = "claude-shared"                              # "name" in .claude-plugin/marketplace.json
AGENT_PLUGIN = f"crew-agents@{MARKETPLACE}"
OLD_PLUGINS = (f"team-agents@{MARKETPLACE}", f"legacy-agents@{MARKETPLACE}")   # merged into crew-agents (0.6.0); dropped from enabledPlugins
# The ONE old -> new agent name mapping (table: plugins/crew-agents/README.md "Old names"; every row has a 1:1 successor, merged sets
# map several old names to one new name; `Explore` is unchanged and not listed). Used twice: copies or symlinks of the old names in
# ~/.claude/agents are backed up and removed (the keys), and old names in the personal ~/.claude/CLAUDE.md are rewritten (the pairs).
OLD_AGENTS = {
    "haiku-worker": "quick-task-hm", "quick-task": "quick-task-hm",
    "sonnet-worker": "developer-sm", "developer": "developer-sm",
    "sonnet-deep": "hard-task-sh",
    "opus-deep": "senior-developer-oh", "senior-developer": "senior-developer-oh",
    "opus-max": "principal-engineer-omax",
    "fable-deep": "deep-analysis-fh", "deep-analysis": "deep-analysis-fh",
    "long-task": "long-task-fxh",
    "research": "research-sm", "reviewer": "reviewer-oh", "release-ops": "release-ops-oh", "security": "security-oh",
    "verifier": "verifier-hm", "operation": "operations-sm", "panel-reader": "panel-reader-sm",
}
# Old names that are also plain English words: renaming them inside prose would corrupt it ("security review"), so the CLAUDE.md
# rewrite skips them (their agent files are still removed).
OLD_AGENT_WORD = frozenset({"developer", "research", "reviewer", "security"})
# The hook runs the clone's script only when it exists: a moved or missing clone must not show a hook error at every start.
RW_BACKUP ="import os,runpy,sys; p=sys.argv[1]; os.path.isfile(p) and runpy.run_path(p, run_name='__main__')"


def add_registered_flags(a, record):
    """Add the options saved by the last install (the "setup" list `record`) to the parsed options `a` (in place) and return `a`.
    A flag given now wins over the saved value (--person, --plan) and is added on top of the others (--last-window keeps --share).
    Nothing drops a saved option except --remove (there is no per-option off switch). The one exception: an explicit --free
    cannot run with the cost modules that --share and --last-window need, so those two saved flags are left out, with a note."""
    if a.free:
        for f in ("--share", "--last-window", "--resume-alarm"):
            if f in record and not getattr(a, f[2:].replace("-", "_"), False):
                say(f"WARNING: --free does not work with {f}; the saved {f} is left out of this install.")
    else:
        a.share = a.share or "--share" in record
        a.last_window = a.last_window or "--last-window" in record
        a.resume_alarm = getattr(a, "resume_alarm", False) or "--resume-alarm" in record
    a.slack_inbox = a.slack_inbox or "--slack-inbox" in record
    a.slack_triage = a.slack_triage or "--slack-triage" in record
    a.stale = a.stale or "--stale" in record
    if a.person is None and "--person" in record and (a.share or a.last_window or getattr(a, "resume_alarm", False)):
        a.person = record[record.index("--person") + 1]
    if a.plan is None and a.share and "--plan" in record:
        a.plan = record[record.index("--plan") + 1]
    return a


def registered_setup(claude):
    """The sharing options saved by the last direct-source install (list of strings), or []."""
    try:
        d = json.loads((claude / STATUS).read_text(encoding="utf-8"))
        k = d.get("setup")
        return k if isinstance(k, list) and all(isinstance(x, str) for x in k) else []
    except (OSError, ValueError, AttributeError):
        return []


def places(claude, direct, want):
    """Where each script runs from, keyed by its destination under ~/.claude ("bin/pace-hook.py"): the copy under ~/.claude, or with
    --direct the file in this clone (same layout, see FILES). settings.json, the cost job and the self-check all take paths from here."""
    return {d: (Path(os.path.normpath(SRC / s)) if direct else claude / d) for s, d, plat in FILES + COST_FILES + LAST_WINDOW_FILES if plat in want}


def backup_name(path):
    """path.backup-<date>; if that exists already (rerun the same day), keep the older backup."""
    bak = path.with_name(path.name + f".backup-{STAMP}")
    if bak.exists():
        bak = path.with_name(path.name + f".backup-{STAMP}-{datetime.datetime.now():%H%M%S}")
    return bak


def say(msg):
    print(msg)


def posix(p):
    return str(p).replace("\\", "/")


def is_ours_hook(h):
    # Matches broken entries too: backslashes (JSON-escaped or not) or eaten separators.
    blob = json.dumps(h, ensure_ascii=False)
    return "pace-hook" in blob


def is_ours_status(s):
    blob = json.dumps(s)
    return isinstance(s, dict) and any(k in blob for k in OUR_STATUS)


def merge(cfg, python, hook, status_cmd, remove, sub_cmd=None):
    """Mutate cfg; return a list of change descriptions."""
    changes = []
    hooks = cfg.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ValueError("'hooks' is not an object")
    for ev in EVENTS:
        groups = (hooks or {}).get(ev)
        if groups is None:
            groups = []
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{ev} is not a list")
        new_entry = {"type": "command", "command": python, "args": ["-I", hook], "timeout": 10}
        placed = False
        out = []
        for g in groups:
            inner = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(inner, list):
                out.append(g)
                continue
            kept = []
            for h in inner:
                if is_ours_hook(h):
                    if not remove and not placed:
                        kept.append(new_entry)
                        placed = True
                        if h != new_entry:
                            changes.append(f"{ev}: pace-hook entry updated")
                    else:
                        changes.append(f"{ev}: pace-hook entry removed")
                else:
                    kept.append(h)
            if kept:
                out.append({**g, "hooks": kept})
        if not remove and not placed:
            out.append({"hooks": [new_entry]})
            changes.append(f"{ev}: pace-hook entry added")
        if out:
            cfg.setdefault("hooks", {})[ev] = out
        elif isinstance(cfg.get("hooks"), dict):
            cfg["hooks"].pop(ev, None)
    if remove and cfg.get("hooks") == {}:
        del cfg["hooks"]
    cur = cfg.get("statusLine")
    if remove:
        if is_ours_status(cur):
            del cfg["statusLine"]
            changes.append("statusLine removed")
    elif not (isinstance(cur, dict) and cur.get("type") == "command" and cur.get("command") == status_cmd):
        base = {k: v for k, v in cur.items() if k not in ("args",)} if isinstance(cur, dict) else {}
        cfg["statusLine"] = {**base, "type": "command", "command": status_cmd}
        changes.append("statusLine set")
    # sub-agent rows: only a command that is ours is replaced or removed; somebody else's subagentStatusLine is left alone
    cur = cfg.get("subagentStatusLine")
    ours = isinstance(cur, dict) and OUR_SUBAGENT in json.dumps(cur)
    if remove:
        if ours:
            del cfg["subagentStatusLine"]
            changes.append("subagentStatusLine removed")
    elif sub_cmd:
        if cur is not None and not ours:
            changes.append("subagentStatusLine is another command: left alone")
        elif not (isinstance(cur, dict) and cur.get("type") == "command" and cur.get("command") == sub_cmd):
            cfg["subagentStatusLine"] = {"type": "command", "command": sub_cmd}
            changes.append("subagentStatusLine set")
    return changes


# (event, matcher, extra argument) of the tools/rules_watch.py entries: the session start report and the cheap per-prompt delta ("--request")
RW_EVENTS = (("SessionStart", "startup", None), ("UserPromptSubmit", None, "--request"))


def merge_rules_watch(cfg, python, remove):
    """Mutate cfg: one entry per event of RW_EVENTS for tools/rules_watch.py of this clone, replacing any earlier rules_watch entry
    (e.g. one added by hand from the README); removed with --remove. Returns change descriptions."""
    hooks = cfg.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ValueError("'hooks' is not an object")
    changes = []
    for event, matcher, extra_arg in RW_EVENTS:
        groups = (hooks or {}).get(event) or []
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{event} is not a list")
        new_entry = {"type": "command", "command": python, "args": ["-I", "-c", RW_BACKUP, posix(RULES_WATCH)] + ([extra_arg] if extra_arg else []), "timeout": 120}
        out, placed = [], False
        for g in groups:
            inner = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(inner, list):
                out.append(g)
                continue
            kept = []
            for h in inner:
                if "rules_watch" not in json.dumps(h, ensure_ascii=False):
                    kept.append(h)
                elif not remove and not placed:
                    kept.append(new_entry)
                    placed = True
                    if h != new_entry:
                        changes.append(f"{event}: rules_watch entry updated")
                else:
                    changes.append(f"{event}: rules_watch entry removed")
            if kept:
                out.append({**g, "hooks": kept})
        if not remove and not placed:
            out.append({**({"matcher": matcher} if matcher else {}), "hooks": [new_entry]})
            changes.append(f"{event}: rules_watch entry added (rule warnings + lost-work report)")
        if out:
            cfg.setdefault("hooks", {})[event] = out
        elif isinstance(cfg.get("hooks"), dict):
            cfg["hooks"].pop(event, None)
            if not cfg["hooks"]:
                del cfg["hooks"]
    return changes


EXIT_SETTING_INVALID = 3


def apply_rules_watch(settings, python):
    """--setting (run by tools/rules_watch.py's background run after a pull): bring ONLY the rules_watch hook entries of settings.json up to
    date with merge_rules_watch, the same function the install uses, so a new RW_EVENTS event arrives with git pull and needs no re-run.
    Touches nothing else; no write when nothing changes; one backup (.backup-<date>) before the first write; the write is atomic (temp file
    + replace). A missing file is left alone (no install yet); invalid JSON is not touched (EXIT_SETTING_INVALID). Returns the exit code."""
    if not settings.is_file():
        say("no settings.json: nothing touched")
        return 0
    try:
        cfg = json.loads(settings.read_text(encoding="utf-8-sig"))
        if not isinstance(cfg, dict):
            raise ValueError("the root is not an object")
        changes = merge_rules_watch(cfg, python, False)
    except (ValueError, OSError) as e:
        say(f"WARNING: settings.json could not be read/parsed ({e}); rules_watch entries not updated, the file is untouched.")
        return EXIT_SETTING_INVALID
    if not changes:
        say("settings.json: rules_watch entries up to date, no change")
        return 0
    bak = backup_name(settings)
    shutil.copy2(settings, bak)
    say(f"backup: {posix(bak)}")
    tmp = settings.with_name(settings.name + ".temp")
    tmp.write_bytes((json.dumps(cfg, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    os.replace(tmp, settings)
    for c in changes:
        say(f"settings.json: {c}")
    return 0


def merge_evidence_hook(cfg, python, remove):
    """Mutate cfg: one entry per event in EVIDENCE_EVENTS (Stop for the main session, SubagentStop for subagents) running
    tools/evidence_hook.py of this clone, replacing any earlier evidence_hook entry (e.g. the `test -f ... ||true` line added by hand
    from the README); removed with --remove. Returns change descriptions."""
    hooks = cfg.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ValueError("'hooks' is not an object")
    new_entry = {"type": "command", "command": python, "args": ["-I", "-c", RW_BACKUP, posix(EVIDENCE)], "timeout": 10}
    changes = []
    for ev in EVIDENCE_EVENTS:
        groups = (hooks or {}).get(ev) or []
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{ev} is not a list")
        out, placed = [], False
        for g in groups:
            inner = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(inner, list):
                out.append(g)
                continue
            kept = []
            for h in inner:
                if "evidence_hook" not in json.dumps(h, ensure_ascii=False):
                    kept.append(h)
                elif not remove and not placed:
                    kept.append(new_entry)
                    placed = True
                    if h != new_entry:
                        changes.append(f"{ev}: evidence_hook entry updated")
                else:
                    changes.append(f"{ev}: evidence_hook entry removed")
            if kept:
                out.append({**g, "hooks": kept})
        if not remove and not placed:
            out.append({"hooks": [new_entry]})
            changes.append(f"{ev}: evidence_hook entry added (hedge words, 'no access' without a vault_cli.py find, unrecorded decision)")
        if out:
            cfg.setdefault("hooks", {})[ev] = out
        elif isinstance(cfg.get("hooks"), dict):
            cfg["hooks"].pop(ev, None)
            if not cfg["hooks"]:
                del cfg["hooks"]
    return changes


def _merge_agent_hook(cfg, python, remove, event, matcher, script_, description):
    """Mutate cfg: one `event` group with `matcher` running `script` of this clone (an Agent-tool hook); an existing entry for the same
    script is updated in place, other hooks of the event are kept; removed with --remove. Returns change descriptions."""
    hooks = cfg.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ValueError("'hooks' is not an object")
    groups = (hooks or {}).get(event) or []
    if not isinstance(groups, list):
        raise ValueError(f"hooks.{event} is not a list")
    marker = script_.stem
    new_entry = {"type": "command", "command": python, "args": ["-I", "-c", RW_BACKUP, posix(script_)], "timeout": 10}
    out, changes, placed = [], [], False
    for g in groups:
        inner = g.get("hooks") if isinstance(g, dict) else None
        if not isinstance(inner, list):
            out.append(g)
            continue
        kept = []
        for h in inner:
            if marker not in json.dumps(h, ensure_ascii=False):
                kept.append(h)
            elif not remove and not placed and g.get("matcher") == matcher:
                kept.append(new_entry)
                placed = True
                if h != new_entry:
                    changes.append(f"{event}: {marker} entry updated")
            else:
                changes.append(f"{event}: {marker} entry removed")
        if kept:
            out.append({**g, "hooks": kept})
    if not remove and not placed:
        out.append({"matcher": matcher, "hooks": [new_entry]})
        changes.append(f"{event}: {marker} entry added ({description})")
    if out:
        cfg.setdefault("hooks", {})[event] = out
    elif isinstance(cfg.get("hooks"), dict):
        cfg["hooks"].pop(event, None)
        if not cfg["hooks"]:
            del cfg["hooks"]
    return changes


def merge_agent_watchdog(cfg, python, remove):
    """PostToolUse (matcher "Agent"): tools/agent_watchdog_note.py, the 30-minute agent watchdog reminder."""
    return _merge_agent_hook(cfg, python, remove, "PostToolUse", "Agent", WATCHDOG, "30-min watchdog reminder when a background agent starts")


def merge_agent_name(cfg, python, remove):
    """PreToolUse (matcher "Agent|Task"): plugins/crew-agents/tools/agent_name.py, a random name for every spawned subagent."""
    return _merge_agent_hook(cfg, python, remove, "PreToolUse", "Agent|Task", AGENT_NAME, "a random name for every subagent")


def merge_crew_agents(cfg, remove):
    """Mutate cfg: extraKnownMarketplaces.claude-shared -> this clone as a `directory` source (other keys of the entry, e.g. autoUpdate,
    are kept) and enabledPlugins[crew-agents@claude-shared] = true; the merged-away team-agents/legacy-agents entries are dropped.
    --remove drops the enabledPlugins entry, and the marketplace entry only when it points at this clone and no other claude-shared
    plugin is enabled. Returns change descriptions."""
    markets, plugins = cfg.get("extraKnownMarketplaces"), cfg.get("enabledPlugins")
    if markets is not None and not isinstance(markets, dict):
        raise ValueError("'extraKnownMarketplaces' is not an object")
    if plugins is not None and not isinstance(plugins, dict):
        raise ValueError("'enabledPlugins' is not an object")
    markets, plugins = dict(markets or {}), dict(plugins or {})
    source = {"source": "directory", "path": posix(CLONE)}
    changes = []
    for old in OLD_PLUGINS:
        if old in plugins:
            del plugins[old]
            changes.append(f"enabledPlugins: {old} removed (merged into crew-agents)")
    entry = markets.get(MARKETPLACE)
    ours_ = isinstance(entry, dict) and entry.get("source") == source
    if remove:
        if plugins.pop(AGENT_PLUGIN, None) is not None:
            changes.append(f"enabledPlugins: {AGENT_PLUGIN} removed")
        if ours_ and not any(k.endswith("@" + MARKETPLACE) and v for k, v in plugins.items()):
            del markets[MARKETPLACE]
            changes.append(f"extraKnownMarketplaces: {MARKETPLACE} (this clone) removed")
    else:
        if not ours_:
            markets[MARKETPLACE] = {**(entry if isinstance(entry, dict) else {}), "source": source}
            changes.append(f"extraKnownMarketplaces: {MARKETPLACE} -> this clone ({posix(CLONE)}), plugins load in place")
        if plugins.get(AGENT_PLUGIN) is not True:
            plugins[AGENT_PLUGIN] = True
            changes.append(f"enabledPlugins: {AGENT_PLUGIN} enabled (team agents from the clone)")
    for key, value in (("extraKnownMarketplaces", markets), ("enabledPlugins", plugins)):
        if value:
            cfg[key] = value
        else:
            cfg.pop(key, None)
    return changes


def claude_plugin(exe, args, timeout=180):
    """`claude plugin <args>` -> (ok, last output line). Never raises; stdin is closed so nothing can prompt."""
    try:
        r = hidden.run([exe, "plugin", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=timeout)
    except Exception as e:
        return False, str(e)
    output = (r.stdout.strip() or r.stderr.strip()).splitlines()
    return r.returncode == 0, (output[-1][:200].encode("ascii", "replace").decode() if output else "")   # ASCII: the console may be cp1254


def claude_plugins(cfg, a, tag):
    """Register the claude-shared plugins that settings.json enables (--remove: crew-agents) with the `claude plugin` CLI, user
    scope. `install` is idempotent ("already installed") and the plugin loads in place from this clone, so there is no update step.
    A marketplace Claude Code has not registered yet (a fresh machine) is added from this clone and the install retried. Skips (True)
    when `claude` is not on PATH or HOME is not the real home (tests); False only when a CLI call fails."""
    requested = [AGENT_PLUGIN] if a.remove else sorted(k for k, v in (cfg.get("enabledPlugins") or {}).items()
                                                      if v is True and k.endswith("@" + MARKETPLACE))
    if not requested:
        return True
    exe = shutil.which("claude")
    own = " && ".join(f"claude plugin {'uninstall' if a.remove else 'install'} {k} --scope user" for k in requested)
    if not exe:
        say(f"{tag}claude command not on PATH: plugins not registered (once claude is installed, run the installer again or: {own})")
        return True
    real = real_home()
    if real is None or Path.home().resolve() != real.resolve():
        say(f"{tag}HOME is not the real home folder: claude plugin not run. In a real install: {own}")
        return True
    if a.whatif:
        say(f"{tag}claude plugin: {own}")
        return True
    ok = True
    for k in requested:
        if a.remove:
            status, message = claude_plugin(exe, ["uninstall", k, "--scope", "user"])
            status = status or "not installed" in message.lower() or "not found" in message.lower()   # nothing to remove is fine
        else:
            status, message = claude_plugin(exe, ["install", k, "--scope", "user"])
            if not status:   # marketplace not registered with Claude Code yet: add this clone, retry once
                added, _ = claude_plugin(exe, ["marketplace", "add", posix(CLONE), "--scope", "user"])
                if added:
                    status, message = claude_plugin(exe, ["install", k, "--scope", "user"])
        say(f"  [{'OK' if status else 'ERROR'}] claude plugin {'uninstall' if a.remove else 'install'} {k}: {message}")
        ok &= status
    return ok


def clean_old_agents(claude, tag, whatif):
    """Back up and remove ~/.claude/agents/<old name>.md (a copy or a symlink of either old agent set, dangling ones included), so
    only the merged team set loads. A symlink is only removed (its target lives in git); a file is copied to .backup-<date>
    first (the backup does not end in .md, so Claude Code does not load it)."""
    folder = claude / "agents"
    for name in OLD_AGENTS:
        p = folder / f"{name}.md"
        if not (p.is_symlink() or p.exists()):
            continue
        if not p.is_symlink():
            bak = backup_name(p)
            say(f"{tag}backup: {posix(bak)}")
            if not whatif:
                shutil.copy2(p, bak)
        say(f"{tag}delete: {posix(p)} (old agent name; replaced by ~/.claude/agents/{AGENT_LINK}, see plugins/crew-agents/README.md)")
        if not whatif:
            p.unlink()


def _link_exists(p):
    """True when p is a symlink or a Windows junction (dangling ones included); never follows it."""
    if p.is_symlink():
        return True
    isj = getattr(os.path, "isjunction", None)   # Python 3.12+
    if isj is not None:
        return isj(p)
    try:
        import stat
        return getattr(os.lstat(p), "st_reparse_tag", 0) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)
    except OSError:
        return False


def _link_delete(p):
    """Remove the link itself, never what it points at: a junction or a Windows directory symlink is a directory entry (rmdir, which
    removes only the reparse point), a POSIX symlink is a file entry (unlink)."""
    if os.name == "nt":
        try:
            os.rmdir(p)
            return
        except OSError:
            pass
    os.unlink(p)


def _link_create(target, p):
    """Create p -> target: a directory junction on Windows (no admin or developer mode needed, unlike a symlink), a symlink elsewhere."""
    if os.name == "nt":
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(p))
            return
        except (ImportError, AttributeError):
            pass
    os.symlink(target, p, target_is_directory=True)


def agent_link(claude, tag, remove, target=None):
    """~/.claude/agents/claude-shared -> <clone>/agents (see AGENT_SOURCE). Idempotent: a link to the right place is left alone; a link
    that points elsewhere or nowhere (moved or deleted clone) is replaced. Only that one entry is ever touched: a real directory or file
    with the same name is not ours and stays (warning, False). A missing source (no clone, or a clone without agents/) is a no-op.
    --remove takes the link away (only a link, never a directory). Returns True when the link is in place or nothing was to be done."""
    target = Path(target or AGENT_SOURCE)
    p = claude / "agents" / AGENT_LINK
    linked = _link_exists(p)
    if remove:
        if linked:
            say(f"{tag}delete: {posix(p)} (agent link)")
            if not tag:
                _link_delete(p)
        return True
    if not target.is_dir():
        say(f"{tag}agent source missing, link not created: {posix(target)}")
        return True
    if linked:
        try:
            same = os.path.isdir(p) and os.path.samefile(p, target)
        except OSError:
            same = False
        if same:
            say(f"{tag}same: {posix(p)} -> {posix(target)}")
            return True
    elif p.exists():
        say(f"{tag}WARNING: {posix(p)} is not a link; left alone, the team agents do not load on this machine. Move it elsewhere and run "
            f"the installer again.")
        return False
    say(f"{tag}agent link: {posix(p)} -> {posix(target)}" + (" (old/broken link replaced)" if linked else ""))
    if tag:
        return True
    try:
        if linked:
            _link_delete(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        _link_create(target, p)
    except OSError as e:
        say(f"  [ERROR] agent link could not be created: {e}")
        return False
    return True


def update_agent_names(claude, tag, whatif):
    """Rewrite whole-word old agent names (OLD_AGENTS) in the personal ~/.claude/CLAUDE.md to the new names, so sessions stop asking
    for agents that no longer exist. Only that file (never repos, rules files or memory). The file is backed up (.backup-<date>) first;
    nothing is written with whatif; a second run finds nothing. A line that talks about the old names themselves ("old names ...",
    or the Turkish "eski ad ...") is history text and is left alone. `quick-task-hm` is not matched by `quick-task` (no word or hyphen neighbours)."""
    p = claude / "CLAUDE.md"
    try:
        raw = p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return
    names = [k for k in OLD_AGENTS if k not in OLD_AGENT_WORD]
    pattern = re.compile(r"(?<![\w-])(" + "|".join(re.escape(k) for k in sorted(names, key=len, reverse=True)) + r")(?![\w-])")
    date = re.compile(r"\bold names?\b|\beski (?:ad|isim)", re.IGNORECASE)   # "eski ad/isim" = Turkish "old name" (input text)
    done = {}

    def change(m):
        done[m.group(1)] = OLD_AGENTS[m.group(1)]
        return OLD_AGENTS[m.group(1)]

    lines = [l if date.search(l) else pattern.sub(change, l) for l in raw.splitlines(keepends=True)]
    if not done:
        return
    bak = backup_name(p)
    say(f"{tag}backup: {posix(bak)}")
    say(f"{tag}agent names updated: {posix(p)} (" + ", ".join(f"{k} -> {v}" for k, v in done.items()) + ")")
    if not whatif:
        shutil.copy2(p, bak)
        p.write_bytes("".join(lines).encode("utf-8"))


def merge_slack_inbox(cfg, python, install):
    """Mutate cfg: one entry per event in SG_EVENTS (SessionStart, UserPromptSubmit) running `inbox.py count` of this clone ("Slack: N new";
    prints nothing when the inbox is empty, so a running session sees new items on its next prompt), replacing any earlier inbox.py entry
    (e.g. one added by hand); install=False removes them. Returns change descriptions."""
    hooks = cfg.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ValueError("'hooks' is not an object")
    new_entry = {"type": "command", "command": python, "args": ["-I", "-c", SG_BACKUP, posix(SG_INBOX)], "timeout": 10}
    changes = []
    for ev in SG_EVENTS:
        groups = (cfg.get("hooks") or {}).get(ev) or []
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{ev} is not a list")
        out, placed = [], False
        for g in groups:
            inner = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(inner, list):
                out.append(g)
                continue
            kept = []
            for h in inner:
                hs = json.dumps(h, ensure_ascii=False)
                if not ("inbox.py" in hs and "slack-triage" in hs):
                    kept.append(h)
                elif install and not placed:
                    kept.append(new_entry)
                    placed = True
                    if h != new_entry:
                        changes.append(f"{ev}: slack-inbox entry updated")
                else:
                    changes.append(f"{ev}: slack-inbox entry removed")
            if kept:
                out.append({**g, "hooks": kept})
        if install and not placed:
            out.append({"hooks": [new_entry]})
            changes.append(f"{ev}: slack-inbox entry added (\"Slack: N new\")")
        if out:
            cfg.setdefault("hooks", {})[ev] = out
        elif isinstance(cfg.get("hooks"), dict):
            cfg["hooks"].pop(ev, None)
            if not cfg["hooks"]:
                del cfg["hooks"]
    return changes


def run_check(cmd, stdin, label, timeout=60):
    try:
        r = hidden.run(cmd, input=stdin, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as e:
        say(f"  [ERROR] {label}: {e}")
        return False
    ok = r.returncode == 0
    say(f"  [{'OK' if ok else 'ERROR'}] {label} (exit {r.returncode})")
    if not ok and r.stderr.strip():
        say("    " + r.stderr.strip().splitlines()[-1][:200])
    return ok


def real_home():
    """The account's real home directory from the user database (Windows: the profile folder from the shell API, not
    from USERPROFILE, so a scratch USERPROFILE is never taken for the real one); None when it cannot be determined."""
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            # FOLDERID_Profile {5E6C858F-0E22-4760-9AFE-EA3317B67173}
            class GUID(ctypes.Structure):
                _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]
            g = GUID(0x5E6C858F, 0x0E22, 0x4760, (ctypes.c_ubyte * 8)(0x9A, 0xFE, 0xEA, 0x33, 0x17, 0xB6, 0x71, 0x73))
            ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(ptr)) == 0:
                val = ptr.value
                ctypes.windll.ole32.CoTaskMemFree(ptr)
                return Path(val)
        except Exception:
            pass
    return None


TASK = "cost-daily"   # Windows Task Scheduler task name (the systemd unit is named UNIT)

TASK_RECORD_PS = r"""
$ErrorActionPreference = 'Stop'
. ([scriptblock]::Create((Get-Content -Raw -LiteralPath $env:INSTALL_HIDDEN_PS1)))   # tools/hidden.ps1: the one hidden-task registration (read as text: works under any execution policy)
$t = New-ScheduledTaskTrigger -Once -At ((Get-Date).Date.AddMinutes(23)) -RepetitionInterval (New-TimeSpan -Hours 2) -RepetitionDuration (New-TimeSpan -Days 3650)
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$s.Priority = 8
Register-HiddenTask -Name $env:INSTALL_TASK_NAME -Exe $env:INSTALL_TASK_EXE -Argument ($env:INSTALL_TASK_ON + ' "' + $env:INSTALL_TASK_SCRIPT + '" --save' + $env:INSTALL_TASK_EXTRA) -Trigger $t -Settings $s -Description 'Claude Code cost record, every 2 hours (quota-pace, cost --save [--share]; commits only on a change)'
"""
LOST_TASK_RECORD_PS = r"""
$ErrorActionPreference = 'Stop'
. ([scriptblock]::Create((Get-Content -Raw -LiteralPath $env:INSTALL_HIDDEN_PS1)))   # tools/hidden.ps1: the one hidden-task registration (read as text: works under any execution policy)
$t = New-ScheduledTaskTrigger -Daily -At '17:30'
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-HiddenTask -Name $env:INSTALL_TASK_NAME -Exe $env:INSTALL_TASK_EXE -Argument ($env:INSTALL_TASK_ON + ' "' + $env:INSTALL_TASK_SCRIPT + '"') -Trigger $t -Settings $s -Description 'Lost work: pushes your own unpushed branches, cleans worktrees and local branches that lose nothing (lost_work.py --push-unpushed --clean-lossless; never main/master/test/dev, never force)'
"""
TASK_DELETE_PS = r"""
$ErrorActionPreference = 'Stop'
if (Get-ScheduledTask -TaskName $env:INSTALL_TASK_NAME -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName $env:INSTALL_TASK_NAME -Confirm:$false }
"""

TASK_SHARES_PS = r"""
$t = Get-ScheduledTask -TaskName $env:INSTALL_TASK_NAME -ErrorAction SilentlyContinue
if ($t -and (($t.Actions | ForEach-Object { $_.Arguments }) -join ' ') -match '--share') { exit 7 }
exit 0
"""


def task_shares(shell):
    """True when the registered Task Scheduler task of the current user already runs with --share (exit code 7 of the snippet). Replaced by tests."""
    try:
        r = hidden.run([shell, "-NoProfile", "-NonInteractive", "-Command", TASK_SHARES_PS], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=dict(os.environ, INSTALL_TASK_NAME=TASK), timeout=60)
    except Exception:
        return False
    return r.returncode == 7


def task_env(python):
    """Env entries for every Windows task snippet of this installer (one code path). The hidden launch itself (`conhost.exe --headless <python.exe>
    -I <script>`, why, and the pythonw fallback) lives in ONE place, tools/hidden.ps1 (New-HiddenAction / Register-HiddenTask); the snippets dot-source
    it from INSTALL_HIDDEN_PS1 and hand over the plain interpreter (INSTALL_TASK_EXE) and its leading arguments (INSTALL_TASK_ON)."""
    return {"INSTALL_TASK_EXE": str(python), "INSTALL_TASK_ON": "-I", "INSTALL_HIDDEN_PS1": str(HIDDEN_PS1)}


def run_cmd_task(shell, ps_script, python, script, share=False, name=TASK, extra_env=None):
    """Run a Task Scheduler PowerShell snippet for the CURRENT user only (no admin, no UAC), for the task `name` (the daily cost
    task by default, LOST_TASK for the lost-work job). Replaced by tests."""
    env = dict(os.environ, INSTALL_TASK_NAME=name, **task_env(python), INSTALL_TASK_SCRIPT=str(script),
               INSTALL_TASK_EXTRA=" --share" if share else "", INSTALL_SG_MINUTES=str(SG_MINUTES), **(extra_env or {}))
    try:
        r = hidden.run([shell, "-NoProfile", "-NonInteractive", "-Command", ps_script], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=120)
    except Exception as e:
        say(f"  [ERROR] task scheduler: {e}")
        return False
    if r.returncode != 0:
        say("  [ERROR] task scheduler (exit %d)" % r.returncode)
        if (r.stderr or r.stdout).strip():
            say("    " + (r.stderr or r.stdout).strip().splitlines()[-1][:200])
    return r.returncode == 0


def generate_write(dst, text, tag, executable=False):
    """Write a generated file; a different existing file is kept as a backup first. Returns True when something changed."""
    data = text.encode("utf-8")
    if dst.exists():
        if dst.read_bytes() == data:
            say(f"{tag}same: {posix(dst)}")
            return False
        bak = backup_name(dst)
        say(f"{tag}backup: {posix(bak)}")
        if not tag:
            shutil.copy2(dst, bak)
    say(f"{tag}write: {posix(dst)}")
    if not tag:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        if executable:
            dst.chmod(0o755)
    return True


def delete_file(p, tag):
    if p.exists():
        say(f"{tag}delete: {posix(p)}")
        if not tag:
            p.unlink()
        return True
    return False


def unit_q(p):
    """systemd ExecStart token: '%' doubled, quoted when it holds a space."""
    p = str(p).replace("%", "%%")
    return f'"{p}"' if " " in p else p


def clean_old_dropin(dropin_dir, claude, home, tag):
    """An old manual drop-in (`cost-daily.service.d/*.conf`) whose ExecStart runs a script under
    ~/.claude/bin/ breaks once the direct-source install removes that copy. Back it up (.backup-<date>), remove it, and say so in one
    line. Drop-ins that run anything else are left alone. Returns True when one was found (the caller does the daemon-reload)."""
    kok = os.path.normpath(str(claude / "bin"))
    found = False
    for conf in sorted(dropin_dir.glob("*.conf")) if dropin_dir.is_dir() else []:
        try:
            lines = conf.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        paths = [t.strip('"\'') for l in lines if l.strip().startswith("ExecStart") and "=" in l for t in l.split("=", 1)[1].split()]
        paths = [os.path.normpath(t.replace("%h", str(home)).replace("${HOME}", str(home)).replace("$HOME", str(home))
                                   .replace("~", str(home), 1) if t.startswith(("~", "%h", "$")) else t) for t in paths]
        if not any(y.startswith(kok + os.sep) for y in paths):
            continue
        bak = backup_name(conf)
        say(f"{tag}old leftover setting (drop-in) removed, the copy in ~/.claude/bin goes away anyway: {posix(conf)} -> backup {posix(bak)}")
        if not tag:
            shutil.copy2(conf, bak)
            conf.unlink()
        found = True
    return found


def clean_old_scheduler_dropin(dropin_dir, tag):
    """An old manual timer drop-in (`cost-daily.timer.d/*.conf`) that only re-sets OnCalendar= keeps the old daily time alive
    over the 2-hourly schedule of the shipped timer (a drop-in wins). Back it up (.backup-<date>), remove it, say so in one line.
    A drop-in with anything else in it is left alone. Returns True when one was removed (the caller does the daemon-reload)."""
    found = False
    for conf in sorted(dropin_dir.glob("*.conf")) if dropin_dir.is_dir() else []:
        try:
            lines = [l.strip() for l in conf.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip() and not l.strip().startswith(("#", ";"))]
        except OSError:
            continue
        if not any(l.startswith("OnCalendar") for l in lines) or any(l != "[Timer]" and not l.startswith("OnCalendar") for l in lines):
            continue
        bak = backup_name(conf)
        say(f"{tag}old timer setting (drop-in) removed, it overrode the 2-hourly schedule: {posix(conf)} -> backup {posix(bak)}")
        if not tag:
            shutil.copy2(conf, bak)
            conf.unlink()
        found = True
    return found


def systemctl(args):
    try:
        r = hidden.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)
    except Exception as e:
        say(f"  [ERROR] systemctl --user {' '.join(args)}: {e}")
        return False
    say(f"  [{'OK' if r.returncode == 0 else 'ERROR'}] systemctl --user {' '.join(args)}")
    if r.returncode != 0 and r.stderr.strip():
        say("    " + r.stderr.strip().splitlines()[-1][:200])
    return r.returncode == 0


# ---------------------------------------------------------------- joining the usage ranking (--share)
def _share_module():
    """cost_share.py from this plugin (the one source of the name/plan rules); python -I does not add its folder."""
    if str(SRC / "bin") not in sys.path:
        sys.path.insert(0, str(SRC / "bin"))
    import cost_share
    return cost_share


def share_name(person_arg):
    """(name, None) or (None, error): the exact people.json value (the same comparison the job makes), from --person, else
    the way the job resolves it (CLAUDE_SHARED_PERSON, the person file, git user.name). Shared by --share and --last-window."""
    mp = _share_module()
    in_list = sorted({mp.clean_name(v) for v in mp.read_people(SRC.parent.parent).values()})
    if not in_list:   # this clone carries no people list (the kit): read it from the team's shared repo (CLAUDE_SHARED_REMOTE)
        try:
            mp.prepare_clone(mp.clone_path(), mp.remote_address())
            in_list = sorted({mp.clean_name(v) for v in mp.read_people(mp.clone_path()).values()})
        except RuntimeError as e:
            return None, "ERROR: people.json could not be read: %s" % e
    if not in_list:
        return None, "ERROR: people.json could not be read or is empty (%s)." % posix(SRC / "people.json")
    raw = person_arg if person_arg else mp.person_name()
    name = mp.clean_name(raw)
    if name not in in_list:
        source = "given with --person" if person_arg else "found (git user.name or the settings file)"
        return None, (f"ERROR: the name '{raw}' {source} is not in the people.json list. Valid names: {', '.join(in_list)}.\n"
                      "If your name is different, give the listed one: --person \"First Last\" (Windows: -Person \"First Last\"). "
                      "If you are not listed, first add yourself to plugins/quota-pace/people.json and commit it to main.")
    return name, None


def share_setting(person_arg, plan_arg):
    """(name, plan, None) or (None, None, error). The name comes from share_name. The plan is
    --plan, else what the job would read today (CLAUDE_SHARED_PLAN, the plan file); 100 or 200."""
    mp = _share_module()
    name, error = share_name(person_arg)
    if error:
        return None, None, error
    if plan_arg is None:
        plan = mp.read_plan()
        if plan is None:
            return None, None, "ERROR: no plan set. Give --plan 100 or --plan 200 (Windows: -Plan 100)."
    else:
        try:
            plan = int(str(plan_arg).strip().lstrip("$"))
        except ValueError:
            plan = None
        if plan not in mp.PLANS:
            return None, None, f"ERROR: invalid plan '{plan_arg}': give --plan {' or --plan '.join(str(x) for x in mp.PLANS)}."
    return name, plan, None


def share_precheck(python, person, plan):
    """Exit code (0 ok) after the two checks, before anything is installed: the existing dry run (`cost --share --dry`, from
    the plugin's own copy; it only builds and prints the row) and a real access probe (clone + `git push --dry-run`), because the
    dry run never reaches git."""
    env = dict(os.environ, CLAUDE_SHARED_PERSON=person, CLAUDE_SHARED_PLAN=str(plan))
    say("Sharing pre-check (cost --share --dry; the first scan can take a few minutes):")
    try:
        r = hidden.run([python, "-I", str(SRC / "bin" / "cost.py"), "--share", "--dry"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=1800)
    except Exception as e:
        say(f"ERROR: cost --share --dry could not run: {e}")
        return EXIT_SHARE_OPTION
    if r.returncode != 0:
        say(f"ERROR: cost --share --dry failed (exit {r.returncode}). {(r.stderr.strip().splitlines() or [''])[-1][:200]}")
        return EXIT_SHARE_OPTION
    for line in r.stdout.strip().splitlines()[-1:]:
        say("  row: " + line[:200])
    say("Checking access to the shared repo (git push --dry-run; nothing is written):")
    ok, reason = _share_module().access_check(person)
    if not ok:
        missing_ = "" if shutil.which("gh") else " (gh is not installed: https://cli.github.com)"
        say(f"ERROR: the shared repo is not set (CLAUDE_SHARED_REMOTE or ~/.config/claude-shared/remote), there is no push access, or you are not logged in to GitHub{missing_}.\n"
            f"  Detail: {reason}\n"
            "  First run: gh auth login  and  gh auth setup-git   (the account needs write access to the shared repo),\n"
            "  then run this install command again with the same options. Nothing was installed.")
        return EXIT_SHARE_ACCESS
    say("  [OK] name is in people.json, push access present")
    project_scope_warning()
    return 0


def project_scope_warning():
    """Non-fatal: the weekly 'finished work' column reads the GitHub Projects boards (GraphQL), which needs the gh token scope
    `project` (read:project is enough). Without it the share still works and the column shows a dash. No new scheduled step is needed: the
    existing 2-hourly job runs the weekly summary (cost_efficiency) from the clone, so the update arrives with the clone pull."""
    try:
        r = hidden.run(["gh", "auth", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        text_ = (r.stdout or "") + (r.stderr or "")
    except Exception:
        return
    scope = [x for x in text_.splitlines() if "scopes" in x.lower()]
    if scope and not any("project" in x for x in scope):
        say("  WARNING: the gh token has no 'project' scope; the 'finished work' column of the ranking stays '-'. To fix: gh auth refresh -s read:project")
    elif scope:
        say("  [OK] the gh token has the 'project' scope (for the weekly finished-work count)")


def write_share_setting(person, plan, tag):
    """~/.config/claude-shared/person and plan: exactly the files cost_share.py reads (its settings folder; Windows: USERPROFILE\\.config)."""
    dir_ = Path(_share_module().SETTING)
    generate_write(dir_ / "person", person + "\n", tag)
    generate_write(dir_ / "plan", f"{plan}\n", tag)


def old_sharing_warning(*files_):
    """Re-installing without --share turns the job back into plain `--save`; say so when the job on disk shared."""
    for d in files_:
        try:
            if "--share" in d.read_text(encoding="utf-8", errors="replace"):
                say("WARNING: the previous install's runlog job was sharing; without --share it now only records. "
                    "To stay in the ranking, run the install again with --share.")
                return
        except OSError:
            pass


def macos():
    """True on macOS (replaced by tests)."""
    return sys.platform == "darwin"


def generate_plist(python, script, claude, share, env=None):
    """launchd user agent for the record: same times and command as the systemd timer / Task Scheduler task (every 2 hours at :23,
    `cost.py --save [--share]`, low priority). Deterministic bytes, so a re-install is a no-op."""
    import plistlib
    env_ = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}   # launchd's default PATH has no Homebrew
    env_.update(env or {})
    d = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [str(python), "-I", posix(script), "--save"] + (["--share"] if share else []),
        "StartCalendarInterval": [{"Hour": h, "Minute": 23} for h in range(0, 24, 2)],   # a Mac asleep at one of them runs it on wake
        "Nice": 19,
        "LowPriorityIO": True,
        "ProcessType": "Background",
        "EnvironmentVariables": env_,
        "StandardOutPath": posix(claude / "cost" / "launchd.log"),
        "StandardErrorPath": posix(claude / "cost" / "launchd.log"),
    }
    return plistlib.dumps(d).decode("utf-8")


def launchctl(args, quiet=False):
    """launchctl <args>; returns True on exit 0. `quiet`: a failure is normal (booting out an agent that is not loaded)."""
    try:
        r = hidden.run(["launchctl", *args], capture_output=True, text=True, timeout=60)
    except Exception as e:
        say(f"  [ERROR] launchctl {' '.join(args)}: {e}")
        return False
    if not quiet:
        say(f"  [{'OK' if r.returncode == 0 else 'ERROR'}] launchctl {' '.join(args)}")
        if r.returncode != 0 and (r.stderr or r.stdout).strip():
            say("    " + (r.stderr or r.stdout).strip().splitlines()[-1][:200])
    return r.returncode == 0


def macos_task(a, python, claude, script, home, tag):
    """The macOS daily job: a launchd user agent. Written under any HOME; launchctl is only called for the real home."""
    real = real_home()
    real_ = real is not None and home.resolve() == real.resolve()
    live = real_ and not a.whatif
    plist = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    if a.remove:
        if live and plist.exists():
            launchctl(["bootout", f"{domain}/{LAUNCHD_LABEL}"], quiet=True)
        delete_file(plist, tag)
        say(f"{tag}The recorded history is kept: {posix(claude / 'cost')} (delete it yourself if you want it gone)")
        return True
    env = {k: os.environ[k] for k in ("CLAUDE_CONFIG_DIR", "COST_TZ", "COST_PRICES") if os.environ.get(k)}
    if not a.share:
        old_sharing_warning(plist)
    generate_write(plist, generate_plist(python, script, claude, a.share, env), tag)
    if a.whatif:
        return True
    if not live:
        say("  HOME is not the real home folder: launchctl not run. In a real install: "
            f"launchctl bootout {domain}/{LAUNCHD_LABEL}; launchctl bootstrap {domain} {posix(plist)}")
        return True
    # idempotent: drop a loaded copy first (not loaded is normal), then load the current file
    launchctl(["bootout", f"{domain}/{LAUNCHD_LABEL}"], quiet=True)
    return launchctl(["bootstrap", domain, str(plist)])


def cost_extra(a, python, claude, tag, script):
    """Wrapper command + (Linux/systemd) user timer for the daily record, or their removal. Returns False on a failure.
    `script` is the cost.py they run: the copy in ~/.claude/bin, or the clone's one with --direct."""
    home = Path.home()
    ok = True
    if a.windows:
        wrapper = claude / "bin" / "cost.cmd"   # in ~/.claude/bin in both modes: statusline.ps1 reads the python path from it
        target = f'"{posix(script)}"' if a.direct else '"%~dp0cost.py"'
        body = f'@"{python}" {target} %*\r\n'
    else:
        wrapper = home / ".local" / "bin" / "cost"
        body = f'#!/bin/sh\n# Claude Code spending statistics (API-price equivalent, not a bill). Help: cost --help\nexec "{python}" "{posix(script)}" "$@"\n'
    if a.remove:
        delete_file(wrapper, tag)
        for pyc in sorted((claude / "bin" / "__pycache__").glob("cost*.pyc")):   # byte code Python left next to our scripts
            delete_file(pyc, tag)
    else:
        generate_write(wrapper, body, tag, executable=True)

    if a.windows:
        # per-user task, runs without any window (headless conhost, see tools/hidden.ps1), no admin rights; only for the account's REAL profile
        # (a scratch HOME never touches the real Task Scheduler, same rule as the systemd branch)
        real = real_home()
        real_ = real is not None and home.resolve() == real.resolve()
        exe = str(python)   # tools/hidden.ps1 (New-HiddenAction) wraps it in a headless conhost
        wscript = str(script).replace("/", "\\")
        shell = a.shell or "powershell"
        if a.remove:
            if not real_:
                say(f"{tag}HOME is not the real profile folder: task scheduler task '{TASK}' left alone.")
            elif a.whatif:
                say(f"{tag}task scheduler: task '{TASK}' is deleted if it exists")
            else:
                ok &= run_cmd_task(shell, TASK_DELETE_PS, exe, wscript)
                say(f"  task scheduler: task '{TASK}' absent/deleted")
            say(f"{tag}The recorded history is kept: {posix(claude / 'cost')} (delete it yourself if you want it gone)")
            return ok
        if not real_:
            say(f"{tag}HOME is not the real profile folder: task scheduler not run (in a real install task '{TASK}' records every 2 hours at :23).")
        elif a.whatif:
            say(f"{tag}task scheduler: '{TASK}' (user task, every 2 hours at :23, low priority, no admin rights needed): {exe} -I {wscript} --save{' --share' if a.share else ''}")
        else:
            if not a.share and task_shares(shell):
                say("WARNING: the previous install's runlog job was sharing; without --share it now only records. "
                    "To stay in the ranking, run the install again with --share.")
            if run_cmd_task(shell, TASK_RECORD_PS, exe, wscript, share=a.share):
                say(f"  [OK] task scheduler: '{TASK}' (every 2 hours at :23, at startup when the computer was off, low priority"
                    f"{', --save --share' if a.share else ''})")
            else:
                ok = False
        return ok
    if macos():
        return ok & macos_task(a, python, claude, script, home, tag)
    if not (sys.platform.startswith("linux") and shutil.which("systemctl")):
        if not a.remove:
            # no timer on Linux without systemd: the status-line background refresh (cost --refresh-status, at most every
            # 5 minutes while Claude Code is in use) writes the same daily and hourly records, so nothing else is needed
            say(f"{tag}no timer (Linux without systemd): the status line's background refresh writes the runlog record "
                f"(at most every 5 minutes while Claude Code is in use). Optional crontab line: 23 */2 * * * \"{python}\" \"{posix(script)}\" --save{' --share' if a.share else ''}")
        return ok

    real = real_home()
    real_ = real is not None and home.resolve() == real.resolve()
    # XDG_CONFIG_HOME is honoured only for the real home: a test run with a scratch HOME must never reach the real unit directory
    base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") if real_ else home / ".config"
    udir = base / "systemd" / "user"
    svc, tmr = udir / f"{UNIT}.service", udir / f"{UNIT}.timer"
    live = real_ and not a.whatif
    if a.remove:
        if live and (svc.exists() or tmr.exists()):
            systemctl(["disable", "--now", f"{UNIT}.timer"])
        delete_file(svc, tag)
        delete_file(tmr, tag)
        if live:
            systemctl(["daemon-reload"])
        say(f"{tag}The recorded history is kept: {posix(claude / 'cost')} (delete it yourself if you want it gone)")
        return ok
    old_a = clean_old_dropin(udir / f"{UNIT}.service.d", claude, home, tag)
    old_b = clean_old_scheduler_dropin(udir / f"{UNIT}.timer.d", tag)
    if (old_a or old_b) and live:
        systemctl(["daemon-reload"])
    env = "".join(f'Environment="{k}={os.environ[k]}"\n' for k in ("CLAUDE_CONFIG_DIR", "COST_TZ", "COST_PRICES") if os.environ.get(k))
    tpl = (SRC / "systemd" / f"{UNIT}.service.in").read_text(encoding="utf-8")
    svc_text = tpl.replace("@EXEC@", f"{unit_q(python)} {unit_q(posix(script))}").replace("@ENV@", env).replace("@PAYLAS@", " --share" if a.share else "")
    if not a.share:
        old_sharing_warning(svc)
    generate_write(svc, svc_text, tag)
    generate_write(tmr, (SRC / "systemd" / f"{UNIT}.timer").read_text(encoding="utf-8"), tag)
    if a.whatif:
        return ok
    if live:
        ok &= systemctl(["daemon-reload"])
        ok &= systemctl(["enable", "--now", f"{UNIT}.timer"])
    else:
        say("  HOME is not the real home folder: systemctl not run. In a real install: "
            f"systemctl --user daemon-reload && systemctl --user enable --now {UNIT}.timer")
    return ok

# ---------------------------------------------------------------- "last window" (install.sh --last-window)
SP_TASK_RECORD_PS = r"""
$ErrorActionPreference = 'Stop'
. ([scriptblock]::Create((Get-Content -Raw -LiteralPath $env:INSTALL_HIDDEN_PS1)))   # tools/hidden.ps1: the one hidden-task registration (read as text: works under any execution policy)
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 8)
Register-HiddenTask -Name $env:INSTALL_TASK_NAME -Exe $env:INSTALL_TASK_EXE -Argument ($env:INSTALL_TASK_ON + ' "' + $env:INSTALL_TASK_SCRIPT + '" ' + $env:INSTALL_SP_TRIGGER) -Trigger $t -Settings $s -Description 'Claude Code last-window (quota-pace, last-window trigger, every 5 minutes): last window, 5-hour resume alarm, burn mode'
"""


def sp_trigger(a):
    """The arguments of `last-window trigger` the timer runs: the modes this install opted into (--last-window = window, --resume-alarm =
    resume). Burn mode needs no entry: it runs whenever a developer switches it on and the timer exists."""
    modes = (["window"] if a.last_window else []) + (["resume"] if getattr(a, "resume_alarm", False) else [])
    if modes in ([], ["window"]):   # the last window alone stays a bare `trigger`: a rolled-back script still understands it
        return "trigger"
    return "trigger --modes " + ",".join(modes)


def run_cmd_sp_task(shell, ps_script, python, script, trigger="trigger"):
    """Run a Task Scheduler snippet for the CURRENT user only, for the task SP_TASK (no admin). Replaced by tests."""
    env = dict(os.environ, INSTALL_TASK_NAME=SP_TASK, **task_env(python), INSTALL_TASK_SCRIPT=str(script), INSTALL_SP_TRIGGER=trigger)
    try:
        r = hidden.run([shell, "-NoProfile", "-NonInteractive", "-Command", ps_script], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=120)
    except Exception as e:
        say(f"  [ERROR] task scheduler: {e}")
        return False
    if r.returncode != 0:
        say("  [ERROR] task scheduler (exit %d)" % r.returncode)
        if (r.stderr or r.stdout).strip():
            say("    " + (r.stderr or r.stdout).strip().splitlines()[-1][:200])
    return r.returncode == 0


def sp_wrapper(a, claude):
    """The `last-window` command file: ~/.local/bin/last-window (POSIX) or ~/.claude/bin/last-window.cmd (Windows)."""
    return claude / "bin" / "last-window.cmd" if a.windows else Path.home() / ".local" / "bin" / "last-window"


def env_lines(key):
    """systemd `Environment=` lines for the variables in `key` that the installing shell has set."""
    return "".join('Environment="%s=%s"\n' % (k, os.environ[k].replace("%", "%%").replace('"', '\\"')) for k in key if os.environ.get(k))


def sp_env():
    """Environment lines for the systemd unit, taken from the installing shell: a user timer does not see the login session's PATH or
    SSH agent. Only what the coordinator needs (claude, gh; git goes over https with the gh credential helper, see the template), plus the
    last-window options when set."""
    key = ("PATH", "SSH_AUTH_SOCK", "CLAUDE_CONFIG_DIR", "GH_CONFIG_DIR", "XDG_CONFIG_HOME", "PACE_TARGET_HOURS", "LAST_WINDOW_MODEL",
               "LAST_WINDOW_ARGS", "LAST_WINDOW_MAX_TURNS", "CLAUDE_SHARED_PERSON", "CLAUDE_SHARED_ROOT")
    lines = [env_lines(key)]
    exe = shutil.which("claude")
    if exe:
        lines.append('Environment="LAST_WINDOW_CLAUDE=%s"\n' % exe.replace("%", "%%"))
    return "".join(lines)


def last_window_precheck(person):
    """Exit code (0 ok): claude and gh exist, and the shared log is writable by this person (clone + git push --dry-run) -- the same
    probe --share uses, against the last-window clone."""
    missing = [x for x in ("claude", "gh", "git") if not shutil.which(x)]
    if missing:
        say(f"ERROR: not found: {', '.join(missing)}. last-window uses `claude`, `gh` and `git`. Nothing was installed.")
        return EXIT_SHARE_OPTION
    if os.name != "nt" and not shutil.which("tmux"):
        say("WARNING: tmux not found. A live session gets the work when there is one; otherwise the visible fallback session (tmux) cannot start. Installing it is recommended (pacman -S tmux / apt install tmux).")
    say("Checking access to the shared record (git push --dry-run; nothing is written):")
    ok, reason = _share_module().access_check(person, clone=os.path.join(str(Path.home()), ".cache", "claude-shared-last-window"))
    if not ok:
        say(f"ERROR: no push access to the shared repo claude-shared, or you are not logged in to GitHub.\n  Detail: {reason}\n"
            "  First run: gh auth login  and  gh auth setup-git, then repeat this command. Nothing was installed.")
        return EXIT_SHARE_ACCESS
    say("  [OK] name is in people.json, push access present")
    return 0


def last_window_extra(a, python, claude, tag, script):
    """The `last-window` command, the 15-minute timer (Linux/systemd user timer, Windows per-user task) or their removal; macOS gets
    the command only. `script` is the last_window.py they run (the copy in ~/.claude/bin, or the clone's with --direct)."""
    home = Path.home()
    ok = True
    wrapper = sp_wrapper(a, claude)
    if a.windows:
        target = f'"{posix(script)}"' if a.direct else '"%~dp0last_window.py"'
        body = f'@"{python}" -I {target} %*\r\n'
    else:
        body = f'#!/bin/sh\n# Claude Code "last window" (last quota window). Help: last-window --help\nexec "{python}" -I "{posix(script)}" "$@"\n'
    if a.remove:
        delete_file(wrapper, tag)
        for pyc in sorted((claude / "bin" / "__pycache__").glob("last_window*.pyc")):
            delete_file(pyc, tag)
    else:
        generate_write(wrapper, body, tag, executable=True)

    real = real_home()
    real_ = real is not None and home.resolve() == real.resolve()
    if a.windows:
        exe = str(python)   # tools/hidden.ps1 (New-HiddenAction) wraps it in a headless conhost
        wscript = str(script).replace("/", "\\")
        shell = a.shell or "powershell"
        if a.remove:
            if not real_:
                say(f"{tag}HOME is not the real profile folder: task '{SP_TASK}' left alone.")
            elif a.whatif:
                say(f"{tag}task scheduler: task '{SP_TASK}' is deleted if it exists")
            else:
                ok &= run_cmd_sp_task(shell, TASK_DELETE_PS, exe, wscript)
                say(f"  task scheduler: task '{SP_TASK}' absent/deleted")
        elif not real_:
            say(f"{tag}HOME is not the real profile folder: task scheduler not run (in a real install task '{SP_TASK}' checks the gate every 5 minutes).")
        elif a.whatif:
            say(f"{tag}task scheduler: '{SP_TASK}' (user task, every 5 minutes, no admin rights needed): {exe} -I {wscript} {sp_trigger(a)}")
        elif run_cmd_sp_task(shell, SP_TASK_RECORD_PS, exe, wscript, sp_trigger(a)):
            say(f"  [OK] task scheduler: '{SP_TASK}' (every 5 minutes; {sp_trigger(a)}: starts work only in a mode whose gate is open)")
        else:
            ok = False
        return ok
    if macos():
        say(f"{tag}macOS: no timer installed (only the `last-window` command). By hand: launchd or cron running `last-window {sp_trigger(a)}` every 5 minutes.")
        return ok
    if not (sys.platform.startswith("linux") and shutil.which("systemctl")):
        if not a.remove:
            say(f"{tag}no timer (Linux without systemd). Optional crontab line: */5 * * * * \"{python}\" -I \"{posix(script)}\" {sp_trigger(a)}")
        return ok
    base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") if real_ else home / ".config"
    udir = base / "systemd" / "user"
    svc, tmr = udir / f"{SP_UNIT}.service", udir / f"{SP_UNIT}.timer"
    live = real_ and not a.whatif
    if a.remove:
        if live and (svc.exists() or tmr.exists()):
            systemctl(["disable", "--now", f"{SP_UNIT}.timer"])
        delete_file(svc, tag)
        delete_file(tmr, tag)
        if live:
            systemctl(["daemon-reload"])
        say(f"{tag}The shared record (reports/last-window/) and the local runlog {posix(claude / 'last-window')} are kept.")
        return ok
    tpl = (SRC / "systemd" / f"{SP_UNIT}.service.in").read_text(encoding="utf-8")
    svc_text = tpl.replace("@EXEC@", f"{unit_q(python)} -I {unit_q(posix(script))}").replace("@TRIGGER@", sp_trigger(a)).replace("@ENV@", sp_env()).replace("@GITORG@", sp_git_org())
    generate_write(svc, svc_text, tag)
    generate_write(tmr, (SRC / "systemd" / f"{SP_UNIT}.timer").read_text(encoding="utf-8"), tag)
    if a.whatif:
        return ok
    if live:
        ok &= systemctl(["daemon-reload"])
        ok &= systemctl(["enable", "--now", f"{SP_UNIT}.timer"])
    else:
        say("  HOME is not the real home folder: systemctl not run. In a real install: "
            f"systemctl --user daemon-reload && systemctl --user enable --now {SP_UNIT}.timer")
    return ok


SG_TASK_RECORD_PS = r"""
$ErrorActionPreference = 'Stop'
. ([scriptblock]::Create((Get-Content -Raw -LiteralPath $env:INSTALL_HIDDEN_PS1)))   # tools/hidden.ps1: the one hidden-task registration (read as text: works under any execution policy)
$t = New-ScheduledTaskTrigger -Once -At ((Get-Date).Date.AddMinutes([int]$env:INSTALL_SG_MINUTES)) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$s.Priority = 8
Register-HiddenTask -Name $env:INSTALL_TASK_NAME -Exe $env:INSTALL_TASK_EXE -Argument ($env:INSTALL_TASK_ON + ' "' + $env:INSTALL_TASK_SCRIPT + '" poll') -Trigger $t -Settings $s -Description 'Slack inbox (claude-shared slack-inbox: token-free, every 5 minutes, no model call)'
"""


def sg_windows(a, python, claude, tag, home, real_):
    """Windows part of the Slack inbox poller: the `slack-inbox.cmd` read command in ~/.claude/bin (a wrapper, no symlinks without admin)
    and a per-user Task Scheduler task running the clone's inbox.py poll every 5 minutes without any window (headless conhost, see tools/hidden.ps1), or their
    removal. A scratch HOME never reaches the real Task Scheduler."""
    install = a.slack_inbox and not a.remove
    cmd = claude / "bin" / "slack-inbox.cmd"
    if install:
        generate_write(cmd, f'@"{python}" -I "{posix(SG_DIR / "inbox.py")}" read %*\r\n', tag)
    else:
        delete_file(cmd, tag)
    exe = str(python)   # tools/hidden.ps1 (New-HiddenAction) wraps it in a headless conhost
    wscript = str(SG_INBOX).replace("/", "\\")
    shell = a.shell or "powershell"
    if not real_:
        say(f"{tag}HOME is not the real profile folder: task '{SG_TASK}' left alone.")
        return True
    if not install:
        if a.whatif:
            say(f"{tag}task scheduler: task '{SG_TASK}' is deleted if it exists")
            return True
        ok = run_cmd_task(shell, TASK_DELETE_PS, exe, wscript, name=SG_TASK)
        say(f"  task scheduler: task '{SG_TASK}' absent/deleted")
        return ok
    if a.whatif:
        say(f"{tag}task scheduler: '{SG_TASK}' (user task, every 5 minutes, starts at :{SG_MINUTES:02d}, no admin rights needed): {exe} -I {wscript} poll")
        return True
    if run_cmd_task(shell, SG_TASK_RECORD_PS, exe, wscript, name=SG_TASK):
        say(f"  [OK] task scheduler: '{SG_TASK}' (every 5 minutes, starts at :{SG_MINUTES:02d}, at startup when the computer was off)")
        return True
    return False


def sg_link(claude, tag, remove):
    """~/.claude/bin/slack-inbox -> the clone's slack-inbox (a link, never a copy)."""
    dst, src = claude / "bin" / "slack-inbox", SG_DIR / "slack-inbox"
    if remove:
        if dst.is_symlink():
            delete_file(dst, tag)
        return
    if dst.is_symlink() and Path(os.readlink(dst)) == src:
        say(f"{tag}same: {posix(dst)}")
        return
    say(f"{tag}link: {posix(dst)} -> {posix(src)}")
    if not tag:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        dst.symlink_to(src)


def sg_plist(python):
    import plistlib
    d = {"Label": SG_LABEL, "ProgramArguments": [str(python), "-I", posix(SG_INBOX), "poll"], "StartInterval": 300,
         "RunAtLoad": True, "Nice": 10, "ProcessType": "Background",
         "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
         "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}
    return plistlib.dumps(d).decode("utf-8")


def sg_unit_write(path, text, tag):
    """Write a unit file; a symlink left by `systemctl link` (into the clone) is replaced, never written through."""
    if path.is_symlink() and not tag:
        path.unlink()
    return generate_write(path, text, tag)


def slack_inbox_extra(a, python, claude, tag):
    """Optional token-free Slack inbox poller (install.sh --slack-inbox / install.ps1 -SlackInbox): a 5-minute systemd user timer (Linux), launchd
    agent (macOS) or Task Scheduler task (Windows) that runs the clone's inbox.py poll, plus the ~/.claude/bin/slack-inbox command. Removal with --remove. Same real-home
    rule as the other jobs: a scratch HOME never reaches the real scheduler."""
    home = Path.home()
    ok = True
    install = a.slack_inbox and not a.remove
    real = real_home()
    real_ = real is not None and home.resolve() == real.resolve()
    live = real_ and not a.whatif
    if install and not (SG_INBOX.is_file() and (SG_DIR / "systemd" / f"{SG_UNIT}.service").is_file()):
        say(f"{tag}ERROR: the slack-inbox files are not in this clone: {posix(SG_DIR)} (agent-crew-kit does not ship these tools: tools/slack-triage/ is the team's own add-on; README, Optional team add-ons)")
        return False
    if a.windows:
        return sg_windows(a, python, claude, tag, home, real_)
    sg_link(claude, tag, not install)
    if macos():
        plist = home / "Library" / "LaunchAgents" / f"{SG_LABEL}.plist"
        domain = f"gui/{os.getuid()}"
        if not install:
            if live and plist.exists():
                launchctl(["bootout", f"{domain}/{SG_LABEL}"], quiet=True)
            delete_file(plist, tag)
            return ok
        generate_write(plist, sg_plist(python), tag)
        if a.whatif:
            return ok
        if not live:
            say(f"  HOME is not the real home folder: launchctl not run. In a real install: launchctl bootstrap {domain} {posix(plist)}")
            return ok
        launchctl(["bootout", f"{domain}/{SG_LABEL}"], quiet=True)
        return launchctl(["bootstrap", domain, str(plist)])
    if not (sys.platform.startswith("linux") and shutil.which("systemctl")):
        if install:
            say(f"{tag}slack-inbox: no timer installed. Optional crontab line: */5 * * * * \"{python}\" -I \"{posix(SG_INBOX)}\" poll")
        return ok
    base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") if real_ else home / ".config"
    udir = base / "systemd" / "user"
    svc, tmr = udir / f"{SG_UNIT}.service", udir / f"{SG_UNIT}.timer"
    if not install:
        if live and (svc.exists() or svc.is_symlink() or tmr.exists() or tmr.is_symlink()):
            systemctl(["disable", "--now", f"{SG_UNIT}.timer"])
        for f in (svc, tmr):
            if f.is_symlink() and not tag:
                f.unlink()
            else:
                delete_file(f, tag)
        if live:
            systemctl(["daemon-reload"])
        return ok
    tpl = (SG_DIR / "systemd" / f"{SG_UNIT}.service").read_text(encoding="utf-8")
    svc_text = re.sub(r"(?m)^ExecStart=.*$", lambda m: f"ExecStart={unit_q(python)} -I {unit_q(posix(SG_INBOX))} poll", tpl)
    sg_unit_write(svc, svc_text, tag)
    sg_unit_write(tmr, (SG_DIR / "systemd" / f"{SG_UNIT}.timer").read_text(encoding="utf-8"), tag)
    if a.whatif:
        return ok
    if live:
        ok &= systemctl(["daemon-reload"])
        ok &= systemctl(["enable", "--now", f"{SG_UNIT}.timer"])
    else:
        say("  HOME is not the real home folder: systemctl not run. In a real install: "
            f"systemctl --user daemon-reload && systemctl --user enable --now {SG_UNIT}.timer")
    return ok


def tool_extra(a, python):
    """The tool check (tools/tool_check.py, the one list of the command-line tools agents need): every install prints the report and, on
    Windows, writes the python3 shim (~/.local/bin/python3[.cmd]) when no real python3 is on PATH; --tools is the consent flag that also
    installs the missing tools (winget / apt-get / brew, pip --user). --remove removes the shim. A missing optional tool never fails the install."""
    if str(TOOL_CHECK.parent) not in sys.path:
        sys.path.insert(0, str(TOOL_CHECK.parent))
    import tool_check
    if a.remove:
        return tool_check.python3_shim(a.python, remove=True, whatif=a.whatif, write=say)
    tool_check.python3_shim(a.python, whatif=a.whatif, write=say)   # a skipped / foreign-file shim is a warning, not a failed install
    return tool_check.execute(a.python, install=a.tools, whatif=a.whatif, write=say) == 0


def gh_limit_extra(a):
    """The `gh` shim (~/.local/bin/gh, on Windows also gh.ps1 and a PATH line in the pwsh profile) in front of the real gh:
    tools/gh_limit.py of this clone limits concurrency, spaces writes and retries secondary-limit answers machine-wide; the shim
    falls back to the real gh when the clone is gone. A scratch HOME (tests, --whatif trials elsewhere) never touches the pwsh
    profile: gh_limit.install only follows the default target."""
    if str(GH_LIMIT.parent) not in sys.path:
        sys.path.insert(0, str(GH_LIMIT.parent))
    import gh_limit
    return gh_limit.install(remove=a.remove, whatif=a.whatif) == 0


ST_TASK = "slack-triage"                      # Windows Task Scheduler task name (install.ps1 -SlackTriage)
ST_UNIT = "slack-triage"                       # systemd user unit (install.sh --slack-triage)
RUN_CMD_ST = SG_DIR / "run_cmd.sh"
ST_TASK_PY = SG_DIR / "task.py"

ST_TASK_RECORD_PS = r"""
$ErrorActionPreference = 'Stop'
. ([scriptblock]::Create((Get-Content -Raw -LiteralPath $env:INSTALL_HIDDEN_PS1)))   # tools/hidden.ps1: the one hidden-task registration (read as text: works under any execution policy)
$t = New-ScheduledTaskTrigger -Once -At ((Get-Date).Date) -RepetitionInterval (New-TimeSpan -Minutes ([int]$env:INSTALL_ST_MINUTES)) -RepetitionDuration (New-TimeSpan -Days 3650)
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 50)
$s.Priority = 8
Register-HiddenTask -Name $env:INSTALL_TASK_NAME -Exe $env:INSTALL_TASK_EXE -Argument ($env:INSTALL_TASK_ON + ' "' + $env:INSTALL_TASK_SCRIPT + '"') -Trigger $t -Settings $s -Description 'Slack/mail request triage (claude-shared slack-triage: a cheap window check every few minutes, the real work only when it is this machine''s turn)'
"""


def st_setting(conf):
    """INTERVAL_MIN / INTERVAL_MAX (minutes) from the triage config; defaults 2 and 4."""
    d = {"INTERVAL_MIN": 2, "INTERVAL_MAX": 4}
    try:
        for l in conf.read_text(encoding="utf-8-sig").splitlines():
            m = re.match(r"^\s*(INTERVAL_MIN|INTERVAL_MAX)=([0-9]+)", l)
            if m:
                d[m.group(1)] = max(1, int(m.group(2)))
    except OSError:
        pass
    d["INTERVAL_MAX"] = max(d["INTERVAL_MAX"], d["INTERVAL_MIN"])
    return d


def slack_triage_extra(a, python, tag):
    """Optional Slack/mail triage runner (install.sh --slack-triage / install.ps1 -SlackTriage). Every developer's machine may run it; one
    owns triage until a request/ack handover (owner lease, tools/slack-triage/window.py) and runs one tick per window. Installs, pointing at the clone:
    a per-user Task Scheduler task running task.py every INTERVAL_MIN minutes (it adds the random spread and starts Git Bash) on
    Windows, a systemd user timer with RandomizedDelaySec on Linux. The config ~/.config/slack-triage/config is COPIED from
    config.example when missing, because it holds per-machine settings (MAIL_ENABLED etc.); an existing one is never touched.
    --remove removes the task/timer and keeps the config and the state."""
    home = Path.home()
    install = a.slack_triage and not a.remove
    real = real_home()
    real_ = real is not None and home.resolve() == real.resolve()
    live = real_ and not a.whatif
    if install and not (RUN_CMD_ST.is_file() and ST_TASK_PY.is_file()):
        say(f"{tag}ERROR: the slack-triage files are not in this clone: {posix(SG_DIR)} (agent-crew-kit does not ship these tools: tools/slack-triage/ is the team's own add-on; README, Optional team add-ons)")
        return False
    conf = Path(os.environ.get("SLACK_TRIAGE_CONFIG") or home / ".config" / "slack-triage" / "config")
    if install and not conf.exists():
        say(f"{tag}copy (machine-specific settings): {posix(conf)} <- config.example")
        if not a.whatif:
            conf.parent.mkdir(parents=True, exist_ok=True)
            conf.write_bytes((SG_DIR / "config.example").read_bytes())
    cfg = st_setting(conf)
    if a.windows:
        exe = str(python)   # tools/hidden.ps1 (New-HiddenAction) wraps it in a headless conhost
        wscript = str(ST_TASK_PY).replace("/", "\\")
        shell = a.shell or "powershell"
        if not real_:
            say(f"{tag}HOME is not the real profile folder: task '{ST_TASK}' left alone.")
            return True
        if not install:
            if a.whatif:
                say(f"{tag}task scheduler: task '{ST_TASK}' is deleted if it exists")
                return True
            ok = run_cmd_task(shell, TASK_DELETE_PS, exe, wscript, name=ST_TASK)
            say(f"  task scheduler: task '{ST_TASK}' absent/deleted (settings and state files are kept)")
            return ok
        text_ = f"'{ST_TASK}' (every {cfg['INTERVAL_MIN']} minutes, task.py waits a random delay of up to {cfg['INTERVAL_MAX'] - cfg['INTERVAL_MIN']} minutes): {exe} -I {wscript}"
        if a.whatif:
            say(f"{tag}task scheduler: {text_}")
            return True
        if run_cmd_task(shell, ST_TASK_RECORD_PS, exe, wscript, name=ST_TASK, extra_env={"INSTALL_ST_MINUTES": str(cfg["INTERVAL_MIN"])}):
            say(f"  [OK] task scheduler: {text_}")
            return True
        return False
    if not (sys.platform.startswith("linux") and shutil.which("systemctl")):
        if install:
            say(f"{tag}slack-triage: no timer installed. Optional crontab line: */{cfg['INTERVAL_MIN']} * * * * bash \"{posix(RUN_CMD_ST)}\"")
        return True
    base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") if real_ else home / ".config"
    udir = base / "systemd" / "user"
    svc, tmr = udir / f"{ST_UNIT}.service", udir / f"{ST_UNIT}.timer"
    if not install:
        if live and (svc.exists() or svc.is_symlink() or tmr.exists() or tmr.is_symlink()):
            systemctl(["disable", "--now", f"{ST_UNIT}.timer"])
        for f in (svc, tmr):
            if f.is_symlink() and not tag:
                f.unlink()
            else:
                delete_file(f, tag)
        if live:
            systemctl(["daemon-reload"])
        return True
    svc_text = re.sub(r"(?m)^ExecStart=.*$", lambda m: f"ExecStart=/bin/bash {unit_q(posix(RUN_CMD_ST))}",
                      (SG_DIR / "systemd" / f"{ST_UNIT}.service").read_text(encoding="utf-8"))
    tmr_text = (SG_DIR / "systemd" / f"{ST_UNIT}.timer").read_text(encoding="utf-8")
    tmr_text = re.sub(r"(?m)^OnUnitActiveSec=.*$", f"OnUnitActiveSec={cfg['INTERVAL_MIN']}min", tmr_text)
    tmr_text = re.sub(r"(?m)^RandomizedDelaySec=.*$", f"RandomizedDelaySec={cfg['INTERVAL_MAX'] - cfg['INTERVAL_MIN']}min", tmr_text)
    sg_unit_write(svc, svc_text, tag)
    sg_unit_write(tmr, tmr_text, tag)
    if a.whatif:
        return True
    if live:
        return systemctl(["daemon-reload"]) & systemctl(["enable", "--now", f"{ST_UNIT}.timer"])
    say("  HOME is not the real home folder: systemctl not run. In a real install: "
        f"systemctl --user daemon-reload && systemctl --user enable --now {ST_UNIT}.timer")
    return True


STALE_GH_VAULT = os.environ.get("STALE_GH_VAULT", "")   # config key: Vault path of the GitHub token for the stale-branch job, e.g. kv/<team>/github#token
STALE_UNIT = "stale"                            # systemd user unit (install.sh --stale): the daily stale-branch job, always-on host only


def sp_git_org():
    """The org-scoped ssh->https rewrite of the last-window unit (config key GH_ORG); empty when no org is configured."""
    org = _share_module().github_org()
    if not org:
        return ""
    return (f'Environment="GIT_CONFIG_COUNT=1"\nEnvironment="GIT_CONFIG_KEY_0=url.https://github.com/{org}/.insteadOf"\n'
            f'Environment="GIT_CONFIG_VALUE_0=git@github.com:{org}/"\n')


def stale_extra(a, python, tag):
    """Daily stale-branch cleanup + report (tools/slack-triage/stale.py) as a systemd user timer on an always-on Linux host (agent-host),
    Mon-Fri 09:30 Europe/Istanbul, Persistent so a missed run catches up. The units live in tools/slack-triage/systemd/; ExecStart is
    rewritten to this clone's stale.py (use the source, no copy). --remove removes them. Linux only (no Windows/macOS scheduler)."""
    home = Path.home()
    install = a.stale and not a.remove
    real = real_home()
    real_ = real is not None and home.resolve() == real.resolve()
    live = real_ and not a.whatif
    if not (sys.platform.startswith("linux") and shutil.which("systemctl")):
        if install:
            say(f"{tag}stale: Linux systemd (agent-host) only; not installed on this machine.")
        return True
    udir = (Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") if real_ else home / ".config") / "systemd" / "user"
    svc, tmr = udir / f"{STALE_UNIT}.service", udir / f"{STALE_UNIT}.timer"
    if not install:
        if live and (svc.exists() or svc.is_symlink() or tmr.exists() or tmr.is_symlink()):
            systemctl(["disable", "--now", f"{STALE_UNIT}.timer"])
        for f in (svc, tmr):
            delete_file(f, tag)
        if live and (svc.exists() or tmr.exists()):
            systemctl(["daemon-reload"])
        return True
    source = SG_DIR / "systemd"
    if not (source / f"{STALE_UNIT}.service").is_file():
        say(f"{tag}ERROR: the stale units are not in this clone: {posix(source)} (agent-crew-kit does not ship these tools: tools/slack-triage/ is the team's own add-on; README, Optional team add-ons)")
        return False
    stale_py = SG_DIR / "stale.py"
    vault_py = CLONE / "tools" / "vault_cli.py"
    # GitHub identity: explicit Vault token (GH_TOKEN via vault_cli.py run, never printed), not the host's ambient `gh` login.
    svc_text = re.sub(r"(?m)^ExecStart=.*$", lambda m: f"ExecStart={unit_q(str(python))} -X utf8 -I {unit_q(posix(vault_py))} run --env GH_TOKEN={STALE_GH_VAULT} -- {unit_q(str(python))} -X utf8 -I {unit_q(posix(stale_py))} runlog %h/.local/state/slack-triage %h/.local/state/slack-triage.log",
                      (source / f"{STALE_UNIT}.service").read_text(encoding="utf-8"))
    sg_unit_write(svc, svc_text, tag)
    sg_unit_write(tmr, (source / f"{STALE_UNIT}.timer").read_text(encoding="utf-8"), tag)
    if a.whatif:
        return True
    if live:
        return systemctl(["daemon-reload"]) & systemctl(["enable", "--now", f"{STALE_UNIT}.timer"])
    say(f"  HOME is not the real home folder: systemctl not run. In a real install: systemctl --user daemon-reload && systemctl --user enable --now {STALE_UNIT}.timer")
    return True


def lost_extra(a, python, tag):
    """The daily lost-work job (lost_work.py --push-unpushed from this clone): a Windows per-user task, a systemd user timer on Linux, a
    crontab hint elsewhere; or its removal. Same real-home rule as the cost job: a scratch HOME never reaches the real scheduler."""
    home = Path.home()
    ok = True
    real = real_home()
    real_ = real is not None and home.resolve() == real.resolve()
    if a.windows:
        exe = str(python)   # tools/hidden.ps1 (New-HiddenAction) wraps it in a headless conhost
        wscript = str(LOST_ENTRY).replace("/", "\\")
        shell = a.shell or "powershell"
        if not real_:
            say(f"{tag}HOME is not the real profile folder: task '{LOST_TASK}' left alone.")
        elif a.whatif:
            say(f"{tag}task scheduler: '{LOST_TASK}' " + ("is deleted if it exists" if a.remove else
                f"(user task, daily 17:30, at startup when the computer was off): {exe} -I {wscript}"))
        elif a.remove:
            ok &= run_cmd_task(shell, TASK_DELETE_PS, exe, wscript, name=LOST_TASK)
            say(f"  task scheduler: task '{LOST_TASK}' absent/deleted")
        elif run_cmd_task(shell, LOST_TASK_RECORD_PS, exe, wscript, name=LOST_TASK):
            say(f"  [OK] task scheduler: '{LOST_TASK}' (daily 17:30; pushes your own unpushed branches, cleans worktrees and local branches that lose nothing, never force)")
        else:
            ok = False
        return ok
    if macos() or not (sys.platform.startswith("linux") and shutil.which("systemctl")):
        if not a.remove:
            say(f"{tag}lost work: no timer installed. Optional crontab line: 30 17 * * * \"{python}\" -I \"{posix(LOST_ENTRY)}\"")
        return ok
    base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") if real_ else home / ".config"
    udir = base / "systemd" / "user"
    svc, tmr = udir / f"{LOST_UNIT}.service", udir / f"{LOST_UNIT}.timer"
    live = real_ and not a.whatif
    if a.remove:
        if live and (svc.exists() or tmr.exists()):
            systemctl(["disable", "--now", f"{LOST_UNIT}.timer"])
        delete_file(svc, tag)
        delete_file(tmr, tag)
        if live:
            systemctl(["daemon-reload"])
        return ok
    tpl = (SRC / "systemd" / f"{LOST_UNIT}.service.in").read_text(encoding="utf-8")
    svc_text = tpl.replace("@EXEC@", f"{unit_q(python)} -I {unit_q(posix(LOST_ENTRY))}") \
        .replace("@ENV@", env_lines(("PATH", "SSH_AUTH_SOCK", "GH_CONFIG_DIR", "XDG_CONFIG_HOME")))
    generate_write(svc, svc_text, tag)
    generate_write(tmr, (SRC / "systemd" / f"{LOST_UNIT}.timer").read_text(encoding="utf-8"), tag)
    if a.whatif:
        return ok
    if live:
        ok &= systemctl(["daemon-reload"])
        ok &= systemctl(["enable", "--now", f"{LOST_UNIT}.timer"])
    else:
        say("  HOME is not the real home folder: systemctl not run. In a real install: "
            f"systemctl --user daemon-reload && systemctl --user enable --now {LOST_UNIT}.timer")
    return ok


def probe(a, python, place, save):
    """The self-check, run exactly as settings.json runs things: the hook for both events, `cost --help` (with `save` also the first
    scan), the status line with a sample whose resets lie in 2100 (not a real reset: it writes no pace.json / history and starts no
    forecast) and the sub-agent rows. True when all pass. The installer runs it after every install; `--probe` runs it alone (quick, no
    first scan), which is what tools/rules_watch.py does after pulling the clone in --direct mode."""
    say("Self-check:")
    ok = True
    hook = posix(place["bin/pace-hook.py"])
    for ev, extra in (("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": "test"})):
        payload = json.dumps({"hook_event_name": ev, "session_id": "install-probe", **extra})
        ok &= run_check([python, "-I", hook], payload, f"hook {ev}")
    # cost statistics first: the first scan writes the status-line cache, so the status-line check below starts no background refresh
    if a.cost:
        cost_py = place["bin/cost.py"]
        ok &= run_check([python, "-I", str(cost_py), "--help"], None, "cost --help")
        if save:
            say("First scan (cost --save; a long history can take a few minutes):")
            ok &= run_check([python, "-I", str(cost_py), "--save"], None, "cost --save", timeout=1800)
    if LOST_ENTRY.is_file():   # the daily job's entry (it runs lost_work.py)
        ok &= run_check([python, "-I", str(LOST_ENTRY), "--help"], None, "daily_lost_work --help")
    if EVIDENCE.is_file():   # both events, exactly as settings.json runs it; the hook fails open, so prove it runs: a hedge must be blocked
        for ev in EVIDENCE_EVENTS:
            payload = json.dumps({"hook_event_name": ev, "session_id": "install-probe", "last_assistant_message": "This is probably wrong."})
            try:
                r = hidden.run([python, "-I", "-c", RW_BACKUP, posix(EVIDENCE)], input=payload, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=60)
                good = r.returncode == 0 and '"decision": "block"' in r.stdout
            except Exception:
                good = False
            say(f"  [{'OK' if good else 'ERROR'}] evidence_hook {ev}")
            ok &= good
    if WATCHDOG.is_file():   # fails open, so prove it runs: a background Agent call must produce the nudge (own state file, nothing written to ~/.cache)
        try:
            with tempfile.TemporaryDirectory() as td:
                r = hidden.run([python, "-I", "-c", RW_BACKUP, posix(WATCHDOG)], capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=60, env={**os.environ, "AGENT_WATCHDOG_STATE": str(Path(td) / "s.json")},
                                   input=json.dumps({"session_id": "install-probe", "tool_name": "Agent", "tool_input": {"run_in_background": True}}))
                good = r.returncode == 0 and "additionalContext" in r.stdout
        except Exception:
            good = False
        say(f"  [{'OK' if good else 'ERROR'}] agent_watchdog_note PostToolUse")
        ok &= good
    if AGENT_NAME.is_file():   # fails open, so prove it runs: an Agent call must come back named (own state folder, nothing left behind)
        try:
            with tempfile.TemporaryDirectory() as td:
                r = hidden.run([python, "-I", "-c", RW_BACKUP, posix(AGENT_NAME)], capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=60, env={**os.environ, "CREW_AGENT_NAME_DIR": td},
                                   input=json.dumps({"session_id": "install-probe", "tool_name": "Agent", "tool_input": {"prompt": "x", "description": "y"}}))
                good = r.returncode == 0 and "updatedInput" in r.stdout
        except Exception:
            good = False
        say(f"  [{'OK' if good else 'ERROR'}] agent_name PreToolUse")
        ok &= good
    if a.slack_inbox:
        ok &= run_check([python, "-I", str(SG_INBOX), "count"], None, "slack-inbox count")
    if a.slack_triage:   # imports the window/turn module of the clone exactly as the tick does (no GitHub call, no claude)
        ok &= run_check([python, "-X", "utf8", "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import window; window.setting(); print(window.person()[1])",
                         str(SG_DIR)], None, "slack-triage window")
    if a.last_window or a.resume_alarm or (place["bin/last_window.py"].is_file() and sp_wrapper(a, Path.home() / ".claude").is_file()):
        ok &= run_check([python, "-I", str(place["bin/last_window.py"]), "--help"], None, "last-window --help")
    sample = json.dumps({"session_id": "install-probe", "model": {"display_name": "Test"},
                         "workspace": {"current_dir": str(Path.home())},
                         "rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": 4102444800},
                                         "seven_day": {"used_percentage": 20, "resets_at": 4102444800}}})
    if a.windows:
        cmd = [a.shell, "-NoProfile", "-File", str(place["bin/statusline-local.ps1"])]
    else:
        cmd = ["bash", str(place["statusline.sh"])]
    ok &= run_check(cmd, sample, "status line")
    if (SRC / FILES[-1][0]).is_file():   # exactly the command written to settings.json (python path, -I, script path)
        task = {"id": "install-probe", "agentType": "x", "description": "install probe", "tokenCount": 1234}
        sub = [python, "-I", posix(place[SUB_KEY])]
        try:
            r = hidden.run(sub, input=json.dumps({"tasks": [task]}), capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60)
            good = r.returncode == 0 and '"id": "install-probe"' in r.stdout and "install probe" in r.stdout
        except Exception:
            good = False
        say(f"  [{'OK' if good else 'ERROR'}] agent row (subagentStatusLine)")
        ok &= good
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", required=True)
    ap.add_argument("--shell", help="pwsh/powershell path (Windows only)")
    ap.add_argument("--windows", action="store_true")
    ap.add_argument("--whatif", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--cost", action="store_true", help="install the cost statistics (the default; kept for old command lines)")
    ap.add_argument("--free", action="store_true", help="leave the cost statistics out")
    ap.add_argument("--share", action="store_true", help="join the usage ranking: write person/plan, check access, daily job runs --save --share")
    ap.add_argument("--person", help='with --share: the name as listed in people.json (default: the name the job would resolve)')
    ap.add_argument("--plan", help="with --share: 100 or 200 (monthly USD of the subscription)")
    ap.add_argument("--last-window", dest="last_window", action="store_true",
                    help="opt in to the last-window mode: leftover weekly quota is used on pre-approved work before the reset; runs are logged in the shared repo")
    ap.add_argument("--resume-alarm", dest="resume_alarm", action="store_true",
                    help="opt in to the 5-hour resume alarm: after a 5-hour limit reset the sessions it stopped get one 'continue' relay "
                         "(same last-window command and timer; also what burn mode `last-window burn --on` runs on)")
    ap.add_argument("--slack-inbox", dest="slack_inbox", action="store_true",
                    help="opt in to the token-free Slack inbox poller (Linux/macOS/Windows): 5-minute timer (systemd, launchd or Task Scheduler), slack-inbox command, SessionStart 'Slack: N new' line")
    ap.add_argument("--slack-triage", dest="slack_triage", action="store_true",
                    help="opt in to running the Slack/mail request triage on this machine (every developer's machine may; one owns triage until a request/ack handover): scheduler task/timer, config copied when missing")
    ap.add_argument("--stale", action="store_true",
                    help="opt in to the daily stale-branch cleanup + report on this (always-on Linux) host: systemd user timer Mon-Fri 09:30 Europe/Istanbul, Persistent; it replaces the triage tick call")
    ap.add_argument("--tools", action="store_true",
                    help="install the missing command-line tools (jq, poppler, tesseract, shellcheck, ...; winget / apt-get / brew, pip --user); without it the tool check only reports")
    ap.add_argument("--direct", action="store_true", help="run the scripts from this clone instead of copies in ~/.claude (updates come with git pull); this is the default")
    ap.add_argument("--copy", action="store_true", help="legacy: copy the scripts into ~/.claude/bin (needs a re-run for every update); the default runs them from this clone")
    ap.add_argument("--setting", action="store_true", help="only bring the rules_watch hook entries of settings.json and the ~/.claude/agents/claude-shared link up to date (no install; rules_watch.py runs it after a pull, --direct installs only)")
    ap.add_argument("--probe", action="store_true", help="only run the quick self-check for these options (no install, no first scan)")
    a = ap.parse_args()
    a.cost = not a.free
    if a.direct and a.copy:
        say("ERROR: --direct and --copy cannot be given together.")
        return EXIT_SHARE_OPTION
    # every run starts from the sharing options saved by the last install and adds the flags given now, so `install.sh --last-window`
    # keeps the ranking and the Slack jobs (a bare run is the same with no new flag). --remove and --probe use only the flags given.
    # --whatif previews the same result, writing nothing.
    if not (a.remove or a.probe or a.setting):
        # a downstream team's own migration (tools/team_migrate.py --ensure, absent in the kit) runs before the saved record is read,
        # so it can bring old state, settings and names up to date first; it fails open (settings.team_migrate)
        if a.whatif:
            if Path(settings.MIGRATE).is_file():
                say("whatif: would run the team migration (tools/team_migrate.py --ensure) first")
        else:
            for line in settings.team_migrate():
                say(line)
        record = registered_setup(Path.home() / ".claude")
        if record:
            add_registered_flags(a, record)
            say("Keeping the saved install flags: " + " ".join(record))
    if a.plan is not None and not a.share:
        say("ERROR: --plan is only given together with --share.")
        return EXIT_SHARE_OPTION
    sp = a.last_window or a.resume_alarm   # both install the last-window command and its timer
    if a.person is not None and not (a.share or sp):
        say("ERROR: --person is only given together with --share (or --last-window / --resume-alarm).")
        return EXIT_SHARE_OPTION
    if sp and a.free:
        say("ERROR: --last-window / --resume-alarm need the cost/share modules; they cannot be given together with --free.")
        return EXIT_SHARE_OPTION
    if a.share and a.free:
        say("ERROR: --share needs the cost statistics; it cannot be given together with --free.")
        return EXIT_SHARE_OPTION
    if a.direct and a.free and not a.remove:
        # the status line turns the cost segments on when bin/cost.py sits next to it, which is always true in the clone
        say("ERROR: --direct runs with the cost statistics; it cannot be given together with --free "
            "(to turn the cost segments off, create the file ~/.claude/cost/off).")
        return EXIT_SHARE_OPTION
    # direct source is the default: every consumer points at the clone's files (git pull updates them, no re-run); --copy or
    # --free (which the clone's layout cannot honour) keep the old copy mode. --remove undoes either mode the same way.
    a.direct = (a.direct or not (a.copy or a.free)) and not a.remove

    claude = Path.home() / ".claude"
    settings = claude / "settings.json"
    python = posix(a.python)
    want = ("win", "both") if a.windows else ("posix", "both")
    place = places(claude, a.direct, want)
    if a.setting:
        if not a.direct:
            say("rules_watch entries not updated automatically: the install is not direct (--direct)")
            return 0
        agent_link(claude, "", False)   # the background run after every pull: creates or repairs the agents link (no-op when in place)
        return apply_rules_watch(settings, python)
    if a.probe:
        return 0 if probe(a, python, place, save=False) else 1

    share_person = share_plan = None
    if a.share and not a.remove:   # --remove ignores the sharing options: it removes the job, the person/plan files stay
        share_person, share_plan, error = share_setting(a.person, a.plan)
        if error:
            say(error)
            return EXIT_SHARE_OPTION
        if not a.whatif:
            rc = share_precheck(a.python, share_person, share_plan)
            if rc:
                return rc
    a.share = a.share and not a.remove
    sp_person = None
    if sp and not a.remove:
        sp_person, error = share_name(a.person)
        if error:
            say(error)
            return EXIT_SHARE_OPTION
        if not a.whatif:
            rc = last_window_precheck(sp_person)
            if rc:
                return rc

    hook = posix(place["bin/pace-hook.py"])
    files = [(s, claude / d) for s, d, plat in FILES if plat in want]
    if a.cost or a.remove:
        files += [(s, claude / d) for s, d, plat in COST_FILES if plat in want]
    if sp or a.remove:
        files += [(s, claude / d) for s, d, plat in LAST_WINDOW_FILES if plat in want]

    def q(p):
        return f'"{p}"' if " " in p else p
    if a.windows:
        status_cmd = f"{q(posix(a.shell))} -NoProfile -File {q(posix(place['bin/statusline-local.ps1']))}"
    else:
        status_cmd = f"bash {q(posix(place['statusline.sh']))}"
    if a.direct:   # direct source: a missing clone or a failing script must never show an error in the status line
        sp = posix(place["bin/statusline-local.ps1"] if a.windows else place["statusline.sh"])
        if a.windows:
            status_cmd = (f"{q(posix(a.shell))} -NoProfile -Command \"if (Test-Path '{sp}') {{ & '{sp}' }} else {{ 'Claude' }}\"")
        else:
            status_cmd = (f"bash -c 'f=\"$1\"; o=$([ -f \"$f\" ] && bash \"$f\" 2>/dev/null); "
                          f"if [ -n \"$o\" ]; then printf %s \"$o\"; else printf Claude; fi' _ {q(sp)}")
    # sub-agent rows: the same interpreter as the hook (absolute path: python3 does not exist on Windows), forward slashes only
    sub_cmd = f"{q(python)} -I {q(posix(place[SUB_KEY]))}" if (SRC / FILES[-1][0]).is_file() else None

    tag = "[-WhatIf] " if a.whatif else ""
    say(f"{tag}Target folder: {posix(claude)}")
    if a.direct:
        say(f"{tag}Mode: direct ({posix(SRC.parent.parent)}); scripts are not copied, updates arrive with git pull")

    # 1. settings.json: parse before touching anything; abort without writing on failure.
    cfg, existed = {}, settings.exists()
    try:
        if existed:
            cfg = json.loads(settings.read_text(encoding="utf-8-sig"))
            if not isinstance(cfg, dict):
                raise ValueError("the root is not an object")
        changes = merge(cfg, python, hook, status_cmd, a.remove, sub_cmd)
        changes += merge_rules_watch(cfg, python, a.remove)
        changes += merge_evidence_hook(cfg, python, a.remove)
        changes += merge_agent_watchdog(cfg, python, a.remove)
        changes += merge_agent_name(cfg, python, a.remove)
        changes += merge_crew_agents(cfg, a.remove)
        if a.slack_inbox or a.remove:
            changes += merge_slack_inbox(cfg, python, a.slack_inbox and not a.remove)
    except (ValueError, OSError) as e:
        say(f"ERROR: settings.json could not be read/parsed ({e}). Nothing was written.")
        return 2
    if not existed and not a.remove:
        changes.insert(0, "settings.json olusturuldu")

    if a.share:
        say(f"{tag}Joining the ranking: name '{share_person}', plan ${share_plan}")
        write_share_setting(share_person, share_plan, tag)
    if sp_person:
        say(f"{tag}Last window: name '{sp_person}' (shared record: reports/last-window/)")
        if not (a.share and share_person == sp_person):
            generate_write(Path(_share_module().SETTING) / "person", sp_person + "\n", tag)

    # 2. files (--direct: copies left by an earlier install are removed; the clone's files run instead)
    for src, dst in files:
        if a.remove or a.direct:
            if dst.exists():
                if a.direct:   # migration from a copy install: keep a backup of what was copied, then drop it
                    bak = backup_name(dst)
                    say(f"{tag}backup: {posix(bak)}")
                    if not a.whatif:
                        shutil.copy2(dst, bak)
                say(f"{tag}delete: {posix(dst)}")
                if not a.whatif:
                    dst.unlink()
            continue
        if not (SRC / src).is_file():
            say(f"{tag}WARNING: source missing, skipped: {src}")
            continue
        data = (SRC / src).read_bytes()
        if dst.exists():
            if dst.read_bytes() == data:
                say(f"{tag}same: {posix(dst)}")
                continue
            bak = backup_name(dst)
            say(f"{tag}backup: {posix(bak)}")
            if not a.whatif:
                shutil.copy2(dst, bak)
        say(f"{tag}kopyala: {posix(dst)}")
        if not a.whatif:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)

    if not a.remove:   # every install: the old agent names give way to the team agents linked from this clone
        clean_old_agents(claude, tag, a.whatif)
        update_agent_names(claude, tag, a.whatif)
    agent_link(claude, tag, a.remove)   # every install and --remove: ~/.claude/agents/claude-shared -> <clone>/agents

    # the mode record tools/rules_watch.py reads; only a --direct install has one (a copy install or --remove removes it).
    # argv: the options the self-check after an automatic pull runs with (`install_common.py <argv> --probe`).
    if a.direct:
        argv = ["--python", python] + (["--windows"] if a.windows else []) + (["--shell", posix(a.shell)] if a.shell else []) + ["--direct"]
        setup = (["--share"] if a.share else []) + (["--person", share_person or sp_person] if (share_person or sp_person) else []) \
            + (["--plan", str(share_plan)] if a.share and share_plan else []) + (["--last-window"] if a.last_window else []) + (["--resume-alarm"] if a.resume_alarm else []) \
            + (["--slack-inbox"] if a.slack_inbox else []) + (["--slack-triage"] if a.slack_triage else []) + (["--stale"] if a.stale else [])
        status = {"mode": "direct", "repo": posix(SRC.parent.parent), "python": python, "argv": argv, "setup": setup}
        generate_write(claude / STATUS, json.dumps(status, indent=2, ensure_ascii=False) + "\n", tag)
    else:
        delete_file(claude / STATUS, tag)

    ok_cost = True
    if a.cost or a.remove:
        ok_cost = cost_extra(a, python, claude, tag, place["bin/cost.py"])

    ok_sp = True
    if sp or a.remove:
        ok_sp = last_window_extra(a, python, claude, tag, place["bin/last_window.py"])

    ok_lost = lost_extra(a, python, tag)   # every install: the daily lost-work push from this clone
    ok_gh = gh_limit_extra(a)   # every install: the gh shim -> tools/gh_limit.py (GitHub API budget guard)
    ok_tool = tool_extra(a, a.python)   # every install: tool report + python3 shim (Windows); --tools installs the missing tools
    ok_sg = True
    if a.slack_inbox or a.remove:
        ok_sg = slack_inbox_extra(a, python, claude, tag)
    if a.slack_triage or a.remove:
        ok_sg &= slack_triage_extra(a, python, tag)
    if a.stale or a.remove:
        ok_sg &= stale_extra(a, python, tag)

    # 3. settings.json write (UTF-8 without BOM, 2-space indent)
    for c in changes:
        say(f"{tag}settings.json: {c}")
    if not changes:
        say(f"{tag}settings.json: no change")
    elif not a.whatif:
        if existed:
            bak = backup_name(settings)
            shutil.copy2(settings, bak)
            say(f"backup: {posix(bak)}")
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_bytes((json.dumps(cfg, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

    ok_plugin = claude_plugins(cfg, a, tag)   # every install: register the enabled claude-shared plugins (after settings.json is written)

    if a.whatif:
        say(f"{tag}Nothing was written.")
        return 0
    if a.remove:
        say("Removal complete. The ranking settings (~/.config/claude-shared/person, plan) stay; delete them if you do not want them.")
        return 0

    # 4. self-check: run everything exactly as configured
    ok = probe(a, python, place, save=True)
    if a.cost:
        ok &= ok_cost
    ok &= ok_sp & ok_lost & ok_sg & ok_gh & ok_tool & ok_plugin
    say("Install complete. Restart Claude Code." if ok
        else "Installed, but the self-check failed; see the error above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

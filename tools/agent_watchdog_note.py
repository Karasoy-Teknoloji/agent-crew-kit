#!/usr/bin/env python3
"""PostToolUse hook (matcher "Agent"): nudge the coordinator to keep the agent watchdog.

Team rule: AUTONOMY.md "Watch your agents". When the Agent tool call has `run_in_background: true`, print one line as
`additionalContext` so the session checks (CronList) that the ~30-min fallback CronCreate exists. At most once per session per
30 minutes; state: ~/.cache/agent-watchdog-note.json {session_id: last_emit_epoch}.

Contract (https://code.claude.com/docs/en/hooks, PostToolUse): stdin JSON has `session_id`, `tool_name`, `tool_input`;
stdout {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "..."}} with exit 0.
Never fails the tool call: any error exits 0 with no output. Python 3 standard library only.
"""
import json
import os
import sys
import time
from pathlib import Path

NOTE = ("Background agent started: make sure the 30-min agent watchdog CronCreate exists (CronList); create it if not. "
        "Each watchdog run also calls TaskStop on every completed agent whose result was already relayed, then CronDeletes "
        "the check only after that cleanup, when no agents are running.")
INTERVAL_S = 30 * 60
KEEP_S = 24 * 3600
STATE = Path(os.environ.get("AGENT_WATCHDOG_STATE") or Path.home() / ".cache" / "agent-watchdog-note.json")


def main():
    d = json.load(sys.stdin)
    ti = d.get("tool_input")
    if d.get("tool_name") not in ("Agent", "Task") or not isinstance(ti, dict) or ti.get("run_in_background") is not True:
        return
    sid = str(d.get("session_id") or "")
    if not sid:
        return
    now = time.time()
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}
    last = state.get(sid)
    if isinstance(last, (int, float)) and 0 <= now - last < INTERVAL_S:
        return
    state = {k: v for k, v in state.items() if isinstance(v, (int, float)) and now - v < KEEP_S}
    state[sid] = now
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": NOTE}}))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)

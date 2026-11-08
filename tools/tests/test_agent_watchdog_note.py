"""Tests for tools/agent_watchdog_note.py (PostToolUse hook, matcher "Agent": watchdog nudge for background agents).

Run: python -m unittest discover -s tools/tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "agent_watchdog_note.py"


class Hook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "sub" / "state.json"
        self.addCleanup(self.tmp.cleanup)

    def run_hook(self, payload=None, raw=None, state=None):
        env = {**os.environ, "AGENT_WATCHDOG_STATE": str(state or self.state)}
        p = subprocess.run([sys.executable, "-I", str(SCRIPT)], input=raw if raw is not None else json.dumps(payload),
                           capture_output=True, text=True, timeout=10, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def bg(self, sid="s1", **ti):
        return {"hook_event_name": "PostToolUse", "session_id": sid, "tool_name": "Agent",
                "tool_input": {"run_in_background": True, **ti}}

    def test_background_agent_emits_additional_context(self):
        out = json.loads(self.run_hook(self.bg()))["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        self.assertIn("CronList", out["additionalContext"])
        self.assertIn("30-min agent watchdog", out["additionalContext"])
        self.assertIn("TaskStop", out["additionalContext"])

    def test_foreground_or_other_tool_is_silent(self):
        self.assertEqual(self.run_hook({"session_id": "s", "tool_name": "Agent", "tool_input": {"prompt": "x"}}), "")
        self.assertEqual(self.run_hook({"session_id": "s", "tool_name": "Agent", "tool_input": {"run_in_background": False}}), "")
        self.assertEqual(self.run_hook({"session_id": "s", "tool_name": "Bash", "tool_input": {"run_in_background": True}}), "")

    def test_once_per_session_per_30_minutes(self):
        self.assertNotEqual(self.run_hook(self.bg("a")), "")
        self.assertEqual(self.run_hook(self.bg("a")), "")
        self.assertNotEqual(self.run_hook(self.bg("b")), "")  # other session
        st = json.loads(self.state.read_text())
        st["a"] -= 31 * 60
        self.state.write_text(json.dumps(st))
        self.assertNotEqual(self.run_hook(self.bg("a")), "")  # interval elapsed

    def test_errors_exit_zero_silently(self):
        for raw in ["", "not json", "[]", "null", json.dumps({"tool_name": "Agent", "tool_input": "x"})]:
            self.assertEqual(self.run_hook(raw=raw), "", raw)
        self.state.parent.mkdir(parents=True)
        self.state.write_text("garbage")  # corrupt state is treated as empty
        self.assertNotEqual(self.run_hook(self.bg()), "")
        # unwritable state path: still emits, never fails
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("x")
        self.assertNotEqual(self.run_hook(self.bg("z"), state=blocker / "x.json"), "")


if __name__ == "__main__":
    unittest.main()

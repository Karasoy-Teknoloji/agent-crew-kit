"""Tests for the sub-agent row's last-activity part ("<age> sessiz" from 10 min, "boşta").
Stdlib only, synthetic transcripts only, nothing reads or writes the real ~/.claude.

Run:  python3 -I -m unittest discover -s plugins/quota-pace/tests     (from the repo root)
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "..", "crew-agents", "tools", "subagent-statusline.py"))
_ROOT = tempfile.mkdtemp(prefix="agent-last-test-")
os.environ["HOME"] = os.environ["USERPROFILE"] = _ROOT
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(_ROOT, ".claude")
os.makedirs(os.environ["CLAUDE_CONFIG_DIR"])
_spec = importlib.util.spec_from_file_location("subagent_statusline", SCRIPT)
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

T0 = 1_780_000_000.0   # fixed "now" for age maths


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


def assistant(epoch, stop="end_turn", tool=False):
    content = [{"type": "text", "text": "x"}] + ([{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}] if tool else [])
    return json.dumps({"type": "assistant", "timestamp": iso(epoch), "message": {"role": "assistant", "stop_reason": stop, "content": content}})


def tool_result(epoch):
    return json.dumps({"type": "user", "timestamp": iso(epoch), "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})


def user_text(epoch):
    return json.dumps({"type": "user", "timestamp": iso(epoch), "message": {"role": "user", "content": "go on"}})


NOISE = json.dumps({"type": "api-request", "timestamp": iso(T0 + 999), "params": {}})


class Fmt(unittest.TestCase):
    def test_humanize(self):
        h = S.humanize_secs
        self.assertEqual(h(12), "12sn")
        self.assertEqual(h(59), "59sn")
        self.assertEqual(h(60), "1dk")
        self.assertEqual(h(4 * 60 + 40), "4dk 40sn")
        self.assertEqual(h(5 * 3600 + 32 * 60), "5sa 32dk")
        self.assertEqual(h(2 * 86400 + 3 * 3600), "2g 3sa")


class QueueParse(unittest.TestCase):
    def test_final_answer_is_ended(self):
        ts, ended = S.parse_tail([assistant(T0 - 100, tool=True), tool_result(T0 - 90), assistant(T0 - 80), NOISE])
        self.assertEqual(int(ts), int(T0 - 80))
        self.assertTrue(ended)

    def test_pending_tool_call_is_not_ended(self):
        ts, ended = S.parse_tail([assistant(T0 - 50, stop="tool_use", tool=True)])
        self.assertEqual(int(ts), int(T0 - 50))
        self.assertFalse(ended)

    def test_tool_result_after_answer_is_not_ended(self):
        self.assertFalse(S.parse_tail([assistant(T0 - 80), tool_result(T0 - 10)])[1])

    def test_new_user_message_after_answer_is_not_ended(self):
        ts, ended = S.parse_tail([assistant(T0 - 80), user_text(T0 - 5)])
        self.assertEqual(int(ts), int(T0 - 80))
        self.assertFalse(ended)

    def test_streaming_partial_without_stop_reason_is_not_ended(self):
        self.assertFalse(S.parse_tail([assistant(T0 - 80, stop=None)])[1])

    def test_garbage_and_cut_first_line(self):
        ts, ended = S.parse_tail(['{"type": "assist', "not json", assistant(T0 - 20)])
        self.assertEqual(int(ts), int(T0 - 20))
        self.assertTrue(ended)

    def test_empty(self):
        self.assertEqual(S.parse_tail([]), (None, False))


class LinePart(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="agent-last-")
        self.session = os.path.join(self.dir, "sess.jsonl")
        os.makedirs(os.path.join(self.dir, "sess", "subagents"))
        self.cache = os.path.join(self.dir, "cache.json")
        self.tools = S.load_cost_tools()
        self.assertIsNotNone(self.tools)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, lines, mtime=None, aid="aX"):
        p = os.path.join(self.dir, "sess", "subagents", "agent-%s.jsonl" % aid)
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def parts(self, task=None, now=T0):
        task = task or {"id": "aX", "status": "running"}
        return S.activity_parts(task, self.session, now, self.tools, self.cache)

    def test_seconds(self):
        self.write([assistant(T0 - 12, tool=True)])
        self.assertEqual(self.parts(), [])                                   # below 10 min: no age shown

    def test_hours_and_idle(self):
        self.write([assistant(T0 - (5 * 3600 + 32 * 60))])
        self.assertEqual(self.parts(), ["5sa 32dk sessiz", "boşta"])

    def test_minutes(self):
        self.write([assistant(T0 - 300, tool=True)])
        self.assertEqual(self.parts(), [])
        self.write([assistant(T0 - 599, tool=True)], aid="aB")
        self.assertEqual(self.parts({"id": "aB"}), [])
        self.write([assistant(T0 - 720, tool=True)], aid="aC")
        self.assertEqual(self.parts({"id": "aC"}), ["12dk sessiz"])
        self.assertEqual(S.SILENT_AFTER, 600)

    def test_days_show_date(self):
        ep = T0 - (2 * 86400 + 3 * 3600)
        self.write([assistant(ep, tool=True)])
        self.assertEqual(self.parts(), ["2g 3sa sessiz"])

    def test_idle_needs_running_status_and_grace(self):
        self.write([assistant(T0 - 10)])
        self.assertEqual(self.parts(), [])                                   # younger than the grace: normal hand-over
        self.write([assistant(T0 - 600)])
        self.assertNotIn("boşta", self.parts({"id": "aX", "status": "completed"}))

    def test_missing_file_gives_nothing(self):
        self.assertEqual(S.activity_parts({"id": "nope"}, self.session, T0, self.tools, self.cache), [])
        self.assertEqual(S.activity_parts({"id": "aX"}, None, T0, self.tools, self.cache), [])
        self.assertEqual(S.last_activity(os.path.join(self.dir, "missing.jsonl"), self.cache), (None, False))

    def test_no_tools_gives_nothing(self):
        self.write([assistant(T0 - 100)])
        self.assertEqual(S.activity_parts({"id": "aX"}, self.session, T0, None, self.cache), [])

    def test_mtime_fallback_when_no_timestamps(self):
        p = self.write([NOISE], mtime=T0 - 90)
        ts, ended = S.last_activity(p, self.cache)
        self.assertEqual(int(ts), int(T0 - 90))
        self.assertFalse(ended)

    def test_cache_hit_skips_the_read(self):
        p = self.write([assistant(T0 - 100)])
        S.last_activity(p, self.cache)
        orig = S.read_tail_lines
        S.read_tail_lines = lambda _p: self.fail("the tail was read again for an unchanged file")
        try:
            self.assertTrue(S.last_activity(p, self.cache)[1])
        finally:
            S.read_tail_lines = orig

    def test_corrupt_cache_is_ignored(self):
        p = self.write([assistant(T0 - 100)])
        with open(self.cache, "w") as f:
            f.write("{nope")
        self.assertTrue(S.last_activity(p, self.cache)[1])

    def test_row_body_appends_activity_last(self):
        row = S.row_body({"id": "a", "description": "d", "agentType": "t", "tokenCount": 5000}, T0, (), ["12dk sessiz", "boşta"])
        self.assertTrue(row.endswith("· 12dk sessiz · boşta"))


if __name__ == "__main__":
    unittest.main()

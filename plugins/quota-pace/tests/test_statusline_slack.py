"""Slack inbox notice: the status line segment (statusline.ps1 / statusline.sh) and the UserPromptSubmit + SessionStart hook entries.

Run:  python3 -I -m unittest discover -s plugins/quota-pace/tests     (from the repo root)
Scratch HOME/USERPROFILE and a scratch inbox (SLACK_INBOX_INBOX); the real inbox is never touched.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUG = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, PLUG)
import install_common  # noqa: E402

PWSH = shutil.which("pwsh") or shutil.which("powershell")
STDIN = json.dumps({"model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"}, "workspace": {"current_dir": "."}})
ITEMS = [json.dumps({"ts": "1", "kind": "dm", "sender": "a", "issue": "", "text": "x%d" % i}) + "\n" for i in range(3)]


class Scratch(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sl-slack-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = os.path.join(self.root, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.inbox = os.path.join(self.root, "slack-inbox.jsonl")

    def env(self):
        e = dict(os.environ, HOME=self.home, USERPROFILE=self.home, SLACK_INBOX_INBOX=self.inbox, COST_STATUS="0")
        e.pop("CLAUDE_CONFIG_DIR", None)
        return e

    def write_inbox(self, lines, offset=None):
        with open(self.inbox, "w", encoding="utf-8", newline="") as f:
            f.write("".join(lines))
        if offset is not None:
            with open(self.inbox + ".seen", "w") as f:
                f.write(str(offset))

    def check(self, render):
        self.assertNotIn("Slack:", render())                                  # no inbox file
        self.write_inbox([])
        self.assertNotIn("Slack:", render())                                  # empty file
        self.write_inbox(ITEMS[:2])
        self.assertIn(" | Slack: 2 new", render())                           # two unread
        self.write_inbox(ITEMS, offset=len(ITEMS[0].encode()) + len(ITEMS[1].encode()))
        self.assertIn(" | Slack: 1 new", render())                           # offset skips the read ones
        self.write_inbox(ITEMS, offset=sum(len(i.encode()) for i in ITEMS))
        self.assertNotIn("Slack:", render())                                  # all read
        self.write_inbox(ITEMS, offset=10**9)
        self.assertNotIn("Slack:", render())                                  # stale offset beyond the end
        with open(self.inbox + ".seen", "w") as f:
            f.write("garbage")
        self.assertIn(" | Slack: 3 new", render())                           # unreadable offset = from the start


@unittest.skipUnless(PWSH, "needs PowerShell")
class StatusLinePs1(Scratch):
    def test_segment(self):
        def render():
            p = subprocess.run([PWSH, "-NoProfile", "-File", os.path.join(PLUG, "statusline.ps1")], input=STDIN, capture_output=True,
                               text=True, encoding="utf-8", env=self.env(), timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            return p.stdout
        self.check(render)


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq") and os.name == "posix", "needs bash and jq")
class StatusLineSh(Scratch):
    def test_segment(self):
        def render():
            p = subprocess.run(["bash", os.path.join(PLUG, "statusline.sh")], input=STDIN, capture_output=True, text=True,
                               env=self.env(), timeout=60)
            return p.stdout
        self.check(render)


class HookEntries(Scratch):
    def test_both_events_idempotent_and_removable(self):
        pace = {"type": "command", "command": "py", "args": ["-I", "/x/pace-hook.py"]}
        cfg = {"hooks": {"UserPromptSubmit": [{"hooks": [pace]}]}}
        ch = install_common.merge_slack_inbox(cfg, "py", True)
        self.assertEqual(len(ch), 2, ch)
        for ev in install_common.SG_EVENTS:
            found = [h for g in cfg["hooks"][ev] for h in g["hooks"] if "inbox.py" in json.dumps(h)]
            self.assertEqual(len(found), 1, ev)
        self.assertIn(pace, [h for g in cfg["hooks"]["UserPromptSubmit"] for h in g["hooks"]])   # pace-hook untouched
        self.assertEqual(install_common.merge_slack_inbox(cfg, "py", True), [])                       # idempotent
        install_common.merge_slack_inbox(cfg, "py", False)
        self.assertNotIn("SessionStart", cfg["hooks"])
        self.assertEqual(cfg["hooks"]["UserPromptSubmit"], [{"hooks": [pace]}])

    def test_hook_command_is_silent_without_the_add_on(self):
        """agent-crew-kit does not ship tools/slack-triage/inbox.py: the hook must exit 0 and print nothing, even with unread items."""
        cfg = {}
        install_common.merge_slack_inbox(cfg, sys.executable, True)
        h = cfg["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        run = lambda: subprocess.run([h["command"], *h["args"]], capture_output=True, text=True, encoding="utf-8", env=self.env(), timeout=30)
        self.write_inbox(ITEMS[:2])
        r = run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((r.stdout, r.stderr), ("", ""))


if __name__ == "__main__":
    unittest.main()

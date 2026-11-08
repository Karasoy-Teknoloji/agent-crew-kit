"""Burn mode in the status line: " | BURN" (statusline.ps1 / statusline.sh) while ~/.claude/last-window/burn.json says it is on and its
stored weekly reset has not passed.

Run:  python3 -I -m unittest discover -s plugins/quota-pace/tests     (from the repo root)
Scratch HOME/USERPROFILE; the real profile is never touched.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUG = os.path.normpath(os.path.join(HERE, ".."))
PWSH = shutil.which("pwsh") or shutil.which("powershell")
STDIN = json.dumps({"model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"}, "workspace": {"current_dir": "."}})


class Scratch(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sl-burn-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = os.path.join(self.root, "home")
        os.makedirs(os.path.join(self.home, ".claude", "last-window"))
        self.file = os.path.join(self.home, ".claude", "last-window", "burn.json")

    def env(self):
        e = dict(os.environ, HOME=self.home, USERPROFILE=self.home, COST_STATUS="0", SLACK_INBOX_INBOX=os.path.join(self.root, "missing.jsonl"))
        e.pop("CLAUDE_CONFIG_DIR", None)
        return e

    def write(self, **d):
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump(d, f)

    def check(self, render):
        self.assertNotIn("BURN", render())                                                  # never switched on
        self.write(active=True, opener="Onur Demir", week_reset=int(time.time()) + 3600)
        self.assertIn(" | BURN", render())
        self.write(active=False, opener="Onur Demir", week_reset=int(time.time()) + 3600)
        self.assertNotIn("BURN", render())                                                  # switched off
        self.write(active=True, opener="Onur Demir", week_reset=int(time.time()) - 1)
        self.assertNotIn("BURN", render())                                                  # its weekly reset passed (auto-off)
        with open(self.file, "w") as f:
            f.write("{bozuk")
        self.assertNotIn("BURN", render())                                                  # unreadable: nothing, no error


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
                               encoding="utf-8", env=self.env(), timeout=60)
            return p.stdout
        self.check(render)


if __name__ == "__main__":
    unittest.main()

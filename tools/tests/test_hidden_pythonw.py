"""End to end, per scheduled entry point: run it under pythonw.exe (no console, like Task Scheduler) and check that every child
process it starts carries CREATE_NO_WINDOW (one visible console window per child, hundreds per run), that the run
left a log, and that nothing crashed.

Entry points: lost_work (daily task), cost --save --share (every 2 h).
Windows only; the network and GitHub are never touched (temp HOME, temp bare remote, no token).

Run: python -m unittest discover -s tools/tests
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
REPO = TOOLS.parent
RUNNER = Path(__file__).resolve().parent / "pythonw_record.py"
PYTHONW = Path(sys.executable).with_name("pythonw.exe")
NO_WINDOW = 0x08000000


@unittest.skipUnless(os.name == "nt" and PYTHONW.exists(), "pythonw.exe (Windows) needed")
class PythonwTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pw-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.log = self.tmp / "run.log"
        self.env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home), "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        for k in ("SLACK_INBOX_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            self.env.pop(k, None)

    def git(self, cwd, *args):
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def pythonw(self, script, *args, env=None):
        rec = self.tmp / "record.jsonl"
        rec.unlink(missing_ok=True)
        subprocess.run([str(PYTHONW), "-I", str(RUNNER), str(rec), str(script), *args], env={**self.env, **(env or {})}, timeout=240)
        rows = [json.loads(l) for l in rec.read_text(encoding="utf-8").splitlines()] if rec.exists() else []
        kids = [r for r in rows if "argv" in r]
        end = [r for r in rows if "exit" in r or "crash" in r]
        self.assertTrue(end, "the run left no end marker: %s" % rows)
        self.assertNotIn("crash", end[-1], end[-1].get("crash"))
        self.assertGreaterEqual(len(kids), 1, "the entry point started no child, so the check proved nothing: %s" % rows)
        bad = [k for k in kids if not k["flags"] & NO_WINDOW]
        self.assertEqual(bad, [], "children started without CREATE_NO_WINDOW")
        return kids, end[-1]["exit"]

    def log_text(self):
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def test_lost_work(self):
        work = self.tmp / "work"
        repo = work / "r"
        repo.mkdir(parents=True)
        self.git(repo, "init", "-q", "-b", "dev")
        self.git(repo, "config", "user.email", "me@example.com")
        self.git(repo, "config", "user.name", "me")
        (repo / "a.txt").write_text("x\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "a")
        kids, rc = self.pythonw(TOOLS / "lost_work.py", "--root", str(work), "--no-fetch", "--brief", env={"LOST_WORK_LOG": str(self.log)})
        self.assertGreaterEqual(len(kids), 3)
        t = self.log_text()
        self.assertIn("start", t.lower())
        self.assertRegex(t, r"(?i)summary|done|scanned|repos")

    def test_cost_save_share(self):
        """The 2-hourly job: totals recorded, then the row pushed to a (temp) remote through git; every git child windowless."""
        seed, remote, clone = self.tmp / "seed", self.tmp / "remote.git", self.tmp / "clone"
        seed.mkdir()
        self.git(seed, "init", "-q", "-b", "main")
        (seed / "plugins" / "quota-pace").mkdir(parents=True)
        (seed / "plugins" / "quota-pace" / "people.json").write_text(json.dumps({"test_person": {"name": "Test Person", "role": "developer"}}), encoding="utf-8")
        self.git(seed, "add", "-A")
        self.git(seed, "commit", "-q", "-m", "seed")
        self.git(self.tmp, "clone", "-q", "--bare", str(seed), str(remote))
        env = {"CLAUDE_SHARED_PERSON": "Test Person", "CLAUDE_SHARED_PLAN": "100", "CLAUDE_SHARED_REMOTE": str(remote),
               "CLAUDE_SHARED_CLONE": str(clone), "CLAUDE_SHARED_EFFICIENCY": "0", "COST_LOG": str(self.log)}
        kids, rc = self.pythonw(REPO / "plugins" / "quota-pace" / "bin" / "cost.py", "--save", "--share", env=env)
        t = self.log_text()
        self.assertIn("start", t)
        self.assertRegex(t, r"exit=\d+ duration=")
        self.assertEqual(rc, 0, t)


"""The status line's throttled automatic share (cost --share --automatic): throttle, lock, change detection (weekly % only),
the in-place row update, and the shell gate in statusline.sh. Stdlib only, synthetic data, no network (local bare remote).

Run: python3 -I -m unittest discover -s plugins/quota-pace/tests -p 'test_share_automatic.py'
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from bash_bul import BASH  # Git Bash on Windows, never the WSL launcher in System32
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cost as TM  # noqa: E402  (sets the scratch HOME / CLAUDE_CONFIG_DIR before the modules are imported)
import cost  # noqa: E402
import cost_share as P  # noqa: E402
from test_share import record, day  # noqa: E402

PLUG = os.path.normpath(os.path.join(HERE, ".."))


def setUpModule():
    os.makedirs(TM._CFG, exist_ok=True)


def tearDownModule():
    shutil.rmtree(TM._ROOT, ignore_errors=True)


def clean():
    shutil.rmtree(os.path.join(cost.CLAUDE, "cost"), ignore_errors=True)
    try:
        os.unlink(os.path.join(cost.CLAUDE, "pace-plan.json"))
    except OSError:
        pass


class Throttle(unittest.TestCase):
    def setUp(self):
        clean()

    def test_due_only_after_the_interval(self):
        self.assertTrue(P.auto_required({}, 1000, 2))                                   # never checked
        self.assertFalse(P.auto_required({"last_check": 1000}, 1000 + 2 * 3600 - 1, 2))
        self.assertTrue(P.auto_required({"last_check": 1000}, 1000 + 2 * 3600, 2))
        self.assertFalse(P.auto_required({"last_check": 1000}, 1000 + 23 * 3600, 24))

    def test_interval_default_and_config(self):
        self.assertEqual(P.auto_interval_hour(), 2.0)
        for v, want in ((6, 6.0), (0.5, 0.5), (0, 2.0), (-1, 2.0), ("x", 2.0), (True, 2.0), (99999, 2.0)):
            with open(os.path.join(cost.CLAUDE, "pace-plan.json"), "w") as f:
                json.dump({"share_interval_hour": v}, f)
            self.assertEqual(P.auto_interval_hour(), want, v)

    def test_state_file_moves_the_marker_to_the_next_due_time(self):
        P.write_auto_status({"pushed_quota": 40}, 5000.0, 2)
        self.assertEqual(P.read_auto_status(), {"pushed_quota": 40, "last_check": 5000.0})
        self.assertEqual(int(os.stat(P.auto_path("share-following")).st_mtime), 5000 + 7200)   # what statusline.sh stats


class Lock(unittest.TestCase):
    def setUp(self):
        clean()

    def test_one_share_at_a_time_and_stale_lock_is_free(self):
        now = time.time()
        self.assertTrue(P.get_auto_lock(now))
        self.assertFalse(P.get_auto_lock(now + 60))                                    # running
        self.assertFalse(P.get_auto_lock(now + 29 * 60))
        self.assertTrue(P.get_auto_lock(now + 31 * 60))                                # older than 30 min: taken over
        P.auto_lock_release()
        self.assertTrue(P.get_auto_lock(now + 32 * 60))
        P.auto_lock_release()
        self.assertFalse(os.path.exists(P.auto_path("share.lock")))

    def test_a_run_that_finds_the_lock_taken_does_nothing_and_keeps_it(self):
        self.assertTrue(P.get_auto_lock(time.time()))
        with mock.patch.object(P, "person_name", side_effect=AssertionError("must not run")):
            P.share(automatic=True)
        self.assertTrue(os.path.exists(P.auto_path("share.lock")))                    # not released by the loser
        P.auto_lock_release()


class Decision(unittest.TestCase):
    def test_only_the_integer_weekly_percent_decides(self):
        self.assertEqual(P.auto_decision(None, {}), "skip")
        self.assertEqual(P.auto_decision(40, {}), "push")
        self.assertEqual(P.auto_decision(40, {"pushed_quota": 40}), "no change")
        self.assertEqual(P.auto_decision(41, {"pushed_quota": 40}), "push")


@unittest.skipUnless(shutil.which("git"), "git not installed")
class Flow(unittest.TestCase):
    """one_attempt(automatic=True) and the whole _auto flow against a local bare remote."""

    def setUp(self):
        clean()
        self.d = tempfile.mkdtemp(prefix="auto-", dir=TM._ROOT)
        self.remote = os.path.join(self.d, "remote.git")
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        self.sh("git", "init", "-q", "--bare", "-b", "main", self.remote)
        seed = os.path.join(self.d, "seed")
        self.sh("git", "clone", "-q", self.remote, seed)
        path = os.path.join(seed, *P.PEOPLE_PATH.split("/"))
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            json.dump({"gh-ali": "Ali"}, f)
        self.sh("git", "-C", seed, "add", "-A")
        self.sh("git", "-C", seed, "commit", "-q", "-m", "seed")
        self.sh("git", "-C", seed, "push", "-q", "origin", "HEAD:main")
        self.seed = seed
        self.clone = os.path.join(self.d, "clone")
        for p in (mock.patch.dict(os.environ, dict(self.env, CLAUDE_SHARED_REMOTE=self.remote, CLAUDE_SHARED_CLONE=self.clone)),
                  mock.patch.object(P, "GIT_ENV", dict(self.env, GIT_TERMINAL_PROMPT="0")),
                  mock.patch.object(P, "person_name", return_value="Ali"),
                  mock.patch.object(P.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)

    def sh(self, *a):
        r = subprocess.run(a, capture_output=True, text=True, encoding="utf-8", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def head(self):
        return self.sh("git", "--git-dir", self.remote, "rev-parse", "main")

    def person_file(self):
        self.sh("git", "-C", self.seed, "pull", "-q", "origin", "main")
        return TM.rd(os.path.join(self.seed, "reports", "usage", "ali.md"))

    def attempt(self, r):
        return P.one_attempt(self.clone, self.remote, r, r["date"], automatic=True)

    def test_same_percent_writes_and_commits_nothing_even_when_other_figures_moved(self):
        self.assertEqual(self.attempt(record(day(), quota=40, d7=100, commit=1)), "pushed")
        head = self.head()
        r = record(day(), quota=40, d7=999, commit=9, week_usd=777)                    # $ and commits moved, % did not
        r["mode"], r["forecast"] = "TIGHT", 120
        self.assertEqual(self.attempt(r), "no change")
        self.assertEqual(self.head(), head)

    def test_changed_percent_replaces_todays_row_in_place_and_pushes(self):
        self.attempt(record(day(1), quota=30, d7=100))
        self.attempt(record(day(), quota=40, d7=100))
        self.assertEqual(self.attempt(record(day(), quota=47, d7=150, commit=2)), "pushed")
        rows = P.file_lines(self.person_file())
        self.assertEqual([(x["date"], x["quota"], x["d7"]) for x in rows], [(day(1), 30, 100), (day(), 47, 150)])   # one row per date
        self.assertEqual(len(P.line_parse(P.build_line(record(day(), quota=47)))["date"]), 10)
        self.assertEqual(sum(l.startswith("| %s |" % day()) for l in self.person_file().splitlines()), 1)   # rows that BEGIN with today (the fixture's week-reset cell may equal today)

    def test_daily_job_still_never_replaces_a_row_of_the_same_day(self):
        self.attempt(record(day(), quota=40))
        m = self.person_file()
        self.assertEqual(P.add_to_file(m, "Ali", P.build_line(record(day(), quota=55)), day()), m)

    def test_whole_flow_throttle_unchanged_changed_and_retry(self):
        t0 = 1_800_000_000.0
        status = {"quota": 40}
        calls = []

        def bk(person, today, now=None):
            calls.append(1)
            return record(today, quota=status["quota"])
        with mock.patch.object(P, "todays_record", side_effect=bk), \
                mock.patch.object(P, "quota_info", side_effect=lambda s=None: (status["quota"], t0 + 86400)):
            P.share(automatic=True, now=t0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(P.read_auto_status()["pushed_quota"], 40)
            head = self.head()
            P.share(automatic=True, now=t0 + 3600)                                  # inside the 2 h interval: nothing at all
            self.assertEqual(len(calls), 1)
            P.share(automatic=True, now=t0 + 3 * 3600)                              # due, % unchanged: no work, no commit
            self.assertEqual((len(calls), self.head()), (1, head))
            self.assertEqual(P.read_auto_status()["last_check"], t0 + 3 * 3600)         # only the check time moved
            self.assertEqual(P.read_auto_status()["last_sharing"], t0)
            status["quota"] = 41
            real = P.one_attempt
            n = {"i": 0}

            def flaky(*a, **k):
                n["i"] += 1
                if n["i"] < 3:
                    raise RuntimeError("push: rejected")                              # lost the race twice
                return real(*a, **k)
            with mock.patch.object(P, "one_attempt", side_effect=flaky):
                P.share(automatic=True, now=t0 + 6 * 3600)
            self.assertEqual(n["i"], 3)
            self.assertNotEqual(self.head(), head)
            self.assertEqual(P.read_auto_status()["pushed_quota"], 41)
        self.assertFalse(os.path.exists(P.auto_path("share.lock")))

    def test_failed_push_keeps_the_old_baseline_and_waits_for_the_next_interval(self):
        t0 = 1_800_000_000.0
        with mock.patch.object(P, "todays_record", return_value=record(day(), quota=40)), \
                mock.patch.object(P, "quota_info", return_value=(40, t0 + 86400)), \
                mock.patch.object(P, "one_attempt", side_effect=RuntimeError("push: down")):
            P.share(automatic=True, now=t0)
        d = P.read_auto_status()
        self.assertNotIn("pushed_quota", d)
        self.assertEqual(d["last_check"], t0)


STUB = """#!/usr/bin/env python3
import sys
if "--automatic" in sys.argv:
    open(sys.argv[0] + ".call", "a").write("x\\n")
"""


@unittest.skipUnless(BASH and shutil.which("jq"), "bash and jq required")
class ShellGate(unittest.TestCase):
    """statusline.sh starts the share detached only when due; renders inside the interval start nothing."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="auto-sh-")
        self.claude = os.path.join(self.home, ".claude")
        os.makedirs(os.path.join(self.claude, "cost"))
        os.makedirs(os.path.join(self.home, ".config", "claude-shared"))
        open(os.path.join(self.home, ".config", "claude-shared", "plan"), "w").write("100\n")
        self.plug = os.path.join(self.home, "plug")
        shutil.copytree(PLUG, self.plug, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.stub = os.path.join(self.plug, "bin", "cost.py")
        open(self.stub, "w").write(STUB)
        self.call = self.stub + ".call"
        self.marker = os.path.join(self.claude, "cost", "share-following")
        self.lock = os.path.join(self.claude, "cost", "share.lock")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def render(self):
        now = int(time.time())
        data = json.dumps({"model": {"display_name": "X", "id": "x"}, "workspace": {"current_dir": "/"}, "rate_limits": {
            "five_hour": {"used_percentage": 10, "resets_at": now + 3 * 3600},
            "seven_day": {"used_percentage": 50, "resets_at": now + 3 * 86400}}})
        t0 = time.time()
        r = subprocess.run([BASH, os.path.join(self.plug, "statusline.sh")], input=data, text=True, capture_output=True, timeout=60,
                           env=dict(os.environ, HOME=self.home, USERPROFILE=self.home, QUOTA_SHARE_AUTO="1"))
        self.assertIn("50", r.stdout)                                                 # the weekly figure: the status line rendered
        time.sleep(0.6)                                                               # the detached stub is quick
        return time.time() - t0

    def call_count(self):
        return len(open(self.call).read().split()) if os.path.exists(self.call) else 0

    def test_gate(self):
        self.render()
        self.assertEqual(self.call_count(), 1)                                         # never shared: starts one
        future = time.time() + 7200                                                   # what the job writes after a check
        os.utime(self.marker, (future, future)) if os.path.exists(self.marker) else (open(self.marker, "w").close(), os.utime(self.marker, (future, future)))
        self.render()
        self.render()
        self.assertEqual(self.call_count(), 1)                                         # two renders inside the interval start nothing
        past = time.time() - 3 * 3600                                                 # forced back by more than 2 h
        os.utime(self.marker, (past, past))
        self.render()
        self.assertEqual(self.call_count(), 2)                                         # exactly one more start

    def test_running_share_and_stale_lock(self):
        open(self.lock, "w").close()                                                 # fresh lock: a share is running
        self.render()
        self.assertEqual(self.call_count(), 0)
        old = time.time() - 31 * 60
        os.utime(self.lock, (old, old))                                              # stale lock counts as free
        self.render()
        self.assertEqual(self.call_count(), 1)

    def test_not_joined_or_switched_off_starts_nothing(self):
        os.unlink(os.path.join(self.home, ".config", "claude-shared", "plan"))
        self.render()
        self.assertEqual(self.call_count(), 0)
        open(os.path.join(self.home, ".config", "claude-shared", "plan"), "w").write("100\n")
        open(os.path.join(self.claude, "cost", "off"), "w").close()
        self.render()
        self.assertEqual(self.call_count(), 0)


if __name__ == "__main__":
    unittest.main()

"""The 2-hourly timer run of the usage share: it commits and pushes only when the person's row or the ranking really changed
(timestamp-only drift never counts), says what moved in the commit message, regenerates RANKING.md safely and never touches other
people's files; plus the Slack inbox state column. Stdlib only, synthetic data, no network (local bare remote), no desktop notification.

Run: python3 -I -m unittest discover -s plugins/quota-pace/tests -p 'test_share_change.py'
"""
import builtins
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cost as TM  # noqa: E402  (sets the scratch HOME / CLAUDE_CONFIG_DIR and CLAUDE_SHARED_NOTIFIER=0 before the modules are imported)
import cost  # noqa: E402
import cost_share as P  # noqa: E402
from test_share import record, day  # noqa: E402


def setUpModule():
    os.makedirs(TM._CFG, exist_ok=True)


def tearDownModule():
    shutil.rmtree(TM._ROOT, ignore_errors=True)


def clean():
    shutil.rmtree(os.path.join(cost.CLAUDE, "cost"), ignore_errors=True)


def line(date=None, **kw):
    """A record of the timer run: the fixture row plus the pace and Slack fields."""
    r = record(date or day(), **kw.pop("fixture", {}))
    r.update({"mode": "NORMAL", "forecast": 85, "end_at": P.ENOUGH, "forecast_time": "2026-11-04 10:00", "slack": "open", "slack_last": "2026-11-04 10:00"})
    r.update(kw)
    return r


class Flow(unittest.TestCase):
    def setUp(self):
        clean()
        self.d = tempfile.mkdtemp(prefix="change-", dir=TM._ROOT)
        self.remote = os.path.join(self.d, "remote.git")
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        self.sh("git", "init", "-q", "--bare", "-b", "main", self.remote)
        self.seed = os.path.join(self.d, "seed")
        self.sh("git", "clone", "-q", self.remote, self.seed)
        path = os.path.join(self.seed, *P.PEOPLE_PATH.split("/"))
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            json.dump({"gh-ali": "Ali", "gh-veli": "Veli"}, f)
        self.veli(line(fixture=dict(person="Veli", d7=150, quota=30)))
        self.clone = os.path.join(self.d, "clone")
        for p in (mock.patch.dict(os.environ, dict(self.env, CLAUDE_SHARED_REMOTE=self.remote, CLAUDE_SHARED_CLONE=self.clone)),
                  mock.patch.object(P, "GIT_ENV", dict(self.env, GIT_TERMINAL_PROMPT="0")),
                  mock.patch.object(P, "person_name", return_value="Ali"),
                  mock.patch.object(P, "github_commit_count", return_value=None),
                  mock.patch.object(P.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)

    def sh(self, *a):
        r = subprocess.run(a, capture_output=True, text=True, encoding="utf-8", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def veli(self, r):
        """Seed Veli's file (another person) on the remote."""
        path = os.path.join(self.seed, "reports", "usage", "veli.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(P.add_to_file("", "Veli", P.build_line(r), r["date"]))
        self.sh("git", "-C", self.seed, "add", "-A")
        self.sh("git", "-C", self.seed, "commit", "-q", "-m", "seed veli")
        self.sh("git", "-C", self.seed, "push", "-q", "origin", "HEAD:main")

    def head(self):
        return self.sh("git", "--git-dir", self.remote, "rev-parse", "main")

    def message(self):
        return self.sh("git", "--git-dir", self.remote, "log", "-1", "--format=%s", "main")

    def files(self):
        return self.sh("git", "--git-dir", self.remote, "show", "--name-only", "--format=", "main").split()

    def run_cmd(self, r):
        return P.one_attempt(self.clone, self.remote, r, r["date"], change=True)

    def ali(self):
        self.sh("git", "-C", self.seed, "pull", "-q", "origin", "main")
        return TM.rd(os.path.join(self.seed, "reports", "usage", "ali.md"))

    # ------------------------------------------------------------ change-only
    def test_first_run_of_a_date_writes_row_and_ranking_and_names_the_person(self):
        r = line(fixture=dict(d7=100, quota=40))
        self.assertEqual(self.run_cmd(r), "pushed")
        self.assertEqual(sorted(self.files()), ["reports/usage/RANKING.md", "reports/usage/ali.md"])   # nobody else's file
        self.assertTrue(self.message().startswith("chore(usage): Ali %s" % day()), self.message())
        self.assertIn("rank", r["summary"])

    def test_timestamp_and_rolling_window_drift_commit_nothing(self):
        self.run_cmd(line(fixture=dict(d7=100, quota=40)))
        head = self.head()
        r = line(fixture=dict(d7=100, quota=40), forecast_time="2026-11-04 12:00", slack_last="2026-11-04 12:00",
                  end_at=P.ENOUGH)
        r["d24"], r["d30"], r["week_usd"] = 77, 555, 999                       # rolling windows moved with the clock
        self.assertEqual(self.run_cmd(r), "no change")
        self.assertEqual(self.head(), head)

    def test_run_out_hour_jitter_is_not_a_change_but_another_day_is(self):
        self.run_cmd(line(fixture=dict(d7=100, quota=95), end_at="2026-11-06 14:00"))
        head = self.head()
        self.assertEqual(self.run_cmd(line(fixture=dict(d7=100, quota=95), end_at="2026-11-06 17:40")), "no change")
        self.assertEqual(self.head(), head)
        self.assertEqual(self.run_cmd(line(fixture=dict(d7=100, quota=95), end_at="2026-11-07 09:00")), "pushed")

    def test_weekly_percent_change_replaces_todays_row_in_place(self):
        self.run_cmd(line(fixture=dict(d7=100, quota=40)))
        r = line(fixture=dict(d7=110, quota=41))
        self.assertEqual(self.run_cmd(r), "pushed")
        self.assertIn("quota 40%→41%", self.message())
        rows = P.file_lines(self.ali())
        self.assertEqual([(x["date"], x["quota"], x["d7"]) for x in rows], [(day(), 41, 110)])        # one row per date, fresh figures

    def test_mode_change_commits_and_is_worth_telling(self):
        self.run_cmd(line(fixture=dict(d7=100, quota=40), mode="NORMAL"))
        r = line(fixture=dict(d7=100, quota=40), mode="TIGHT")
        self.assertEqual(self.run_cmd(r), "pushed")
        self.assertIn("mode NORMAL→TIGHT", self.message())
        self.assertTrue(r["news"])

    def test_slack_state_change_commits_but_is_not_a_notification(self):
        self.run_cmd(line(fixture=dict(d7=100, quota=40), slack="open"))
        r = line(fixture=dict(d7=100, quota=40), slack="error")
        self.assertEqual(self.run_cmd(r), "pushed")
        self.assertIn("slack open→error", self.message())
        self.assertFalse(r["news"])
        self.assertEqual(P.file_lines(self.ali())[-1]["slack"], "error")

    def test_rank_change_alone_commits_and_is_worth_telling(self):
        self.run_cmd(line(fixture=dict(d7=100, quota=40)))                       # Veli (150) is first, Ali second
        r = line(fixture=dict(d7=300, quota=40))                                  # only the 7-day dollars moved, past Veli
        self.assertEqual(self.run_cmd(r), "pushed")
        self.assertIn("rank 2→1", self.message())
        self.assertTrue(r["news"])
        md = self.sh("git", "--git-dir", self.remote, "show", "main:reports/usage/RANKING.md")
        self.assertRegex(md, r"\| 🥇 \| Ali \|")

    def test_dollar_move_that_keeps_the_ranking_commits_nothing(self):
        self.run_cmd(line(fixture=dict(d7=100, quota=40)))
        head = self.head()
        self.assertEqual(self.run_cmd(line(fixture=dict(d7=130, quota=40))), "no change")
        self.assertEqual(self.head(), head)

    def test_a_new_day_always_writes_its_row(self):
        self.run_cmd(line(day(1), fixture=dict(d7=100, quota=40)))
        self.assertEqual(self.run_cmd(line(day(), fixture=dict(d7=100, quota=40))), "pushed")
        self.assertEqual([x["date"] for x in P.file_lines(self.ali())], [day(1), day()])

    def test_a_push_that_lost_the_race_is_redone_on_the_fresh_base_without_force(self):
        calls = {"n": 0}
        real = P.one_attempt

        def racy(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                self.veli(line(fixture=dict(person="Veli", d7=170, quota=31)))      # another machine pushed meanwhile
                raise RuntimeError("push: rejected")
            return real(*a, **k)
        with mock.patch.object(P, "one_attempt", side_effect=racy):
            result, error = P._push(line(fixture=dict(d7=100, quota=40)), day(), change=True)
        self.assertEqual((result, error, calls["n"]), ("pushed", None, 2))
        md = self.sh("git", "--git-dir", self.remote, "show", "main:reports/usage/RANKING.md")
        self.assertIn("31%", md)                                                  # Veli's newer row is in the regenerated ranking
        self.assertEqual(self.sh("git", "--git-dir", self.remote, "rev-list", "--count", "main"), "3")   # seed+veli, veli again, ali: linear history

    # ------------------------------------------------------------ the whole job
    def test_job_takes_the_lock_notifies_only_on_rank_or_mode_and_skips_when_busy(self):
        r = line(fixture=dict(d7=300, quota=40))
        with mock.patch.object(P, "todays_record", return_value=r), mock.patch.object(P, "notify") as notify, \
                mock.patch("cost_core.today_str", return_value=day()):
            self.assertTrue(P.get_auto_lock(time.time()))                           # another share is running
            P._share(False, None)
            self.assertFalse(notify.called)
            self.assertEqual(self.sh("git", "--git-dir", self.remote, "rev-list", "--count", "main"), "1")   # nothing pushed
            P.auto_lock_release()
            P._share(False, None)
            notify.assert_called_once()
            self.assertEqual(notify.call_args[0][0], "Ali")
            self.assertFalse(os.path.exists(P.auto_path("share.lock")))
            self.assertEqual(P.read_auto_status()["pushed_quota"], 40)
            notify.reset_mock()
            P._share(False, None)                                                 # nothing changed: no commit, no notification
            self.assertFalse(notify.called)
            r2 = line(fixture=dict(d7=300, quota=41))
            with mock.patch.object(P, "todays_record", return_value=r2):
                P._share(False, None)                                             # % moved, rank and mode did not
            self.assertFalse(notify.called)

    def test_notification_is_off_in_tests_and_when_switched_off(self):
        with mock.patch.object(P, "command", return_value=(0, "", "")) as k, mock.patch.dict(os.environ, {"CLAUDE_SHARED_NOTIFIER": "0"}):
            self.assertFalse(P.notify("Ali", "x"))
            self.assertFalse(k.called)


class ChangeKey(unittest.TestCase):
    def test_key_ignores_what_drifts_with_the_clock(self):
        a, b = line(), line()
        b.update(forecast_time="2030-01-01 00:00", slack_last="2030-01-01 00:00", d24=1, d30=2, week_usd=3, d7=999)
        self.assertEqual(P.important_key(a), P.important_key(b))
        for k, v in (("quota", 41), ("mode", "TIGHT"), ("forecast", 99), ("plan", 100), ("slack", "closed"), ("end_at", "2031-01-01 00:00")):
            c = line()
            c[k] = v
            self.assertNotEqual(P.important_key(a), P.important_key(c), k)


class SlackBox(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="inbox-", dir=TM._ROOT)
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        for p in (mock.patch.dict(os.environ, {"SLACK_INBOX_DATA": self.d}), mock.patch.object(sys, "platform", "linux")):
            p.start()
            self.addCleanup(p.stop)

    def status(self, last="2026-11-04 02:35:57", error=None):
        with open(os.path.join(self.d, "state.json"), "w") as f:
            json.dump({"ch": {"C1": "1.0"}, "status": {"last_run": last, "error": error}}, f)

    def cmd(self, enabled=(0, "enabled", ""), active=(0, "active", "")):
        def f(args, timeout=10):
            return enabled if "is-enabled" in args else active
        return mock.patch.object(P, "command", side_effect=f)

    def test_running_failing_switched_off_and_not_installed(self):
        self.status()
        with self.cmd():
            self.assertEqual(P.slack_inbox_status(), ("open", "2026-11-04 02:35"))
        self.status(error="token expired")
        with self.cmd():
            self.assertEqual(P.slack_inbox_status()[0], "error")
        self.status()
        with self.cmd(enabled=(1, "disabled", "")):
            self.assertEqual(P.slack_inbox_status(), ("closed", "2026-11-04 02:35"))
        with self.cmd(active=(3, "inactive", "")):
            self.assertEqual(P.slack_inbox_status()[0], "closed")                  # enabled but stopped: it will not poll
        with self.cmd(enabled=(1, "", "Failed to get unit file state for slack-inbox.timer: No such file or directory")):
            self.assertEqual(P.slack_inbox_status(), ("missing", None))

    def test_unreachable_user_manager_is_unknown_not_missing(self):
        with self.cmd(enabled=(1, "", "Failed to connect to bus: No medium found")):
            self.assertEqual(P.slack_inbox_status(), (None, None))

    def test_no_systemctl_falls_back_to_the_last_poll(self):
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 120))
        self.status(last=t)
        with mock.patch.object(P, "command", return_value=(None, "", "")):
            self.assertEqual(P.slack_inbox_status()[0], "open")
        self.status(last="2020-01-01 00:00:00")
        with mock.patch.object(P, "command", return_value=(None, "", "")):
            self.assertEqual(P.slack_inbox_status()[0], "closed")
        os.unlink(os.path.join(self.d, "state.json"))
        with mock.patch.object(P, "command", return_value=(None, "", "")):
            self.assertEqual(P.slack_inbox_status(), ("missing", None))

    def test_windows_reads_the_last_poll_and_garbage_state_is_ignored(self):
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(P.slack_inbox_status(), (None, None))   # no poll on record: unknown
            self.status(last=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 120)))
            self.assertEqual(P.slack_inbox_status()[0], "open")
            self.status(last=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 120)), error="token expired")
            self.assertEqual(P.slack_inbox_status()[0], "error")
            self.status(last="2020-01-01 00:00:00")
            self.assertEqual(P.slack_inbox_status(), ("closed", "2020-01-01 00:00"))
            os.unlink(os.path.join(self.d, "state.json"))
        with open(os.path.join(self.d, "state.json"), "w") as f:
            f.write("{not json")
        with self.cmd():
            self.assertEqual(P.slack_inbox_status(), ("open", None))

    def test_only_status_is_read_never_the_inbox_content(self):
        self.status()
        with open(os.path.join(self.d, "slack-inbox.jsonl"), "w") as f:
            f.write(json.dumps({"text": "HIDDEN-CONTENT", "sender": "x"}) + "\n")
        opened = []
        real = builtins.open

        def watch(path, *a, **k):
            opened.append(str(path))
            return real(path, *a, **k)
        with self.cmd(), mock.patch.object(builtins, "open", watch):
            status = P.slack_inbox_status()
        self.assertEqual(status, ("open", "2026-11-04 02:35"))
        self.assertEqual([p for p in opened if p.startswith(self.d)], [os.path.join(self.d, "state.json")])
        self.assertNotIn("HIDDEN", P.build_line(dict(line(), slack=status[0], slack_last=status[1])))


class LineAndTable(unittest.TestCase):
    def test_row_roundtrip_and_older_rows(self):
        r = line(slack="error", slack_last="2026-11-04 02:35")
        p = P.line_parse(P.build_line(r))
        self.assertEqual((p["slack"], p["slack_last"]), ("error", "2026-11-04 02:35"))
        self.assertEqual(len(P.EVERY_COLUMN), 28)
        old = "| " + " | ".join(P.build_line(r).strip("| ").split(" | ")[:26]) + " |"           # a row from before the Slack columns
        e = P.line_parse(old)
        self.assertEqual((e["slack"], e["slack_last"], e["mode"]), (None, None, "NORMAL"))
        self.assertIsNone(P.line_parse(P.build_line(r).replace("| error |", "| <script> |"))["slack"])    # only the known words survive

    def test_old_header_grows_the_columns_and_the_ranking_shows_the_cell(self):
        old_title = "| %s |\n|%s\n" % (" | ".join(P.INTERIM3_COLUMN), "---|" * len(P.INTERIM3_COLUMN))
        old_line = "| " + " | ".join(P.build_line(line(day(1))).strip("| ").split(" | ")[:26]) + " |"
        m = P.add_to_file("# x\n\n" + old_title + old_line + "\n", "Ali", P.build_line(line(day())), day())
        self.assertIn("| Slack | Slack last |", m)
        data = {"Ali": P.file_lines(m), "Veli": [line(day(), fixture=dict(person="Veli", d7=10), slack="closed", slack_last=None),
                                                       ][:1], "Cem": [line(day(), fixture=dict(person="Cem", d7=5), slack=None, slack_last=None)]}
        data["Ali"] = [dict(r, slack="open", slack_last="2026-11-04 02:35") if r["date"] == day() else r for r in data["Ali"]]
        md = P.ranking_text(data, day())
        self.assertIn("| Mode | Slack inbox | Last update |", md)
        self.assertRegex(md, r"\| Ali \|[^\n]*\| open · 04\.11 02:35 \|")
        self.assertRegex(md, r"\| Veli \|[^\n]*\| closed \|")
        self.assertRegex(md, r"\| Cem \|[^\n]*\| — \|")
        self.assertEqual((P.slack_text("missing", None), P.slack_text("error", "2026-11-04 02:35"), P.slack_text("open", None)),
                         ("not installed", "error · 04.11 02:35", "open"))


if __name__ == "__main__":
    unittest.main()

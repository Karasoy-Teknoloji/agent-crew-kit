"""Tests for cost_share (daily row in reports/usage/<person>.md + regenerated RANKING.md). Stdlib only, synthetic data only.

Run:  python3 -I -m unittest discover -s plugins/quota-pace/tests     (from the repo root)

No network: the remote is a local bare repository (CLAUDE_SHARED_REMOTE). The cost windows come from a synthetic transcript
run through the real CLI (`--share --dry` prints the row and writes nothing).
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cost as TM  # noqa: E402  (sets the scratch HOME / CLAUDE_CONFIG_DIR before the modules are imported; helpers only)
import cost  # noqa: E402
import cost_share as P  # noqa: E402


def setUpModule():
    # test_cost's tearDownModule removes the shared scratch tree when its tests are done: make it again for this module
    os.makedirs(TM._CFG, exist_ok=True)


def tearDownModule():
    shutil.rmtree(TM._ROOT, ignore_errors=True)


def day(n=0):
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


def record(date, quota=None, plan=200, week="2026-11-01", week_usd=None, d7=100, commit=None, opus=1.0, sonnet=2.0, person="Ali", add=None, delete=None):
    return {"person": person, "date": date, "d24": 10, "d7": d7, "d30": 300, "quota": quota, "plan": plan, "week": week,
            "week_usd": week_usd, "usd": {"opus": opus, "sonnet": sonnet, "haiku": 0.0, "fable": 0.0, "other": 0.0},
            "tok": {"opus": 5.0, "sonnet": 20.0, "haiku": 0.0, "fable": 0.0, "other": 0.0}, "commit": commit, "add": add, "delete": delete}


class RowFormat(unittest.TestCase):
    def test_row_roundtrip_and_unknowns(self):
        r = record("2026-10-28", quota=43, week_usd=210, commit=3)
        p = P.line_parse(P.build_line(r))
        self.assertEqual({k: p[k] for k in ("date", "d24", "d7", "d30", "quota", "plan", "week", "week_usd", "commit")},
                         {k: r[k] for k in ("date", "d24", "d7", "d30", "quota", "plan", "week", "week_usd", "commit")})
        self.assertEqual(p["usd"]["opus"], 1.0)
        self.assertEqual(p["tok"]["sonnet"], 20.0)
        e = P.line_parse(P.build_line(record("2026-10-28", plan=None, week=None)))
        self.assertEqual((e["quota"], e["plan"], e["week"], e["week_usd"], e["commit"]), (None, None, None, None, None))

    def test_noise_is_not_a_row(self):
        for s in ("", "| Date | 24h $ |", "|---|---|", "| 2026-13-45 | 1 | 2 |", "text", None, "| 2026-10-28 | x | 1 | 1 |" + " 1 |" * 15):
            self.assertIsNone(P.line_parse(s), s)

    def test_file_append_is_once_per_date_and_keeps_the_header(self):
        m = P.add_to_file("", "Ali", P.build_line(record("2026-10-27")), "2026-10-27")
        self.assertIn("# Claude usage — Ali", m)
        m2 = P.add_to_file(m, "Ali", P.build_line(record("2026-10-28")), "2026-10-28")
        self.assertEqual([r["date"] for r in P.file_lines(m2)], ["2026-10-27", "2026-10-28"])
        self.assertEqual(P.add_to_file(m2, "Ali", P.build_line(record("2026-10-28", d7=999)), "2026-10-28"), m2)

    def test_names_and_file_stems(self):
        # Turkish locale data: a Turkish name with Turkish letters is the input being folded.
        self.assertEqual(P.file_name("Çağrı Şen"), "cagri-sen")
        self.assertEqual(P.file_name("../x"), "x")
        self.assertEqual(P.model_family("claude-opus-5-5"), "opus")
        self.assertEqual(P.model_family("claude-fable-5-1"), "fable")
        self.assertEqual(P.model_family("gpt"), "other")

    def test_model_split_sums_all_token_kinds(self):
        u, t = P.model_distribution({"model": {"claude-opus-5-5": {"usd": 3.0, "in": 1_000_000, "out": 1_000_000, "cr": 2_000_000, "cw5": 0, "cw1h": 0},
                                          "claude-fable-5-1": {"usd": 1.5}}})
        self.assertEqual((u["opus"], t["opus"], u["fable"], t["fable"]), (3.0, 4.0, 1.5, 0.0))


class Ranking(unittest.TestCase):
    def test_line_columns_roundtrip_and_old_rows_show_dash(self):
        p = P.line_parse(P.build_line(record("2026-10-28", commit=2, add=1234, delete=56)))
        self.assertEqual((p["commit"], p["add"], p["delete"]), (2, 1234, 56))
        old = P.build_line(record("2026-10-27", commit=3)).rsplit(" | — | — | — | — | — | — | — | — | — |", 1)[0] + " |"      # a 19-column row from before the change
        self.assertEqual(len(old.strip("|").split("|")), len(P.COLUMNS))
        e = P.line_parse(old)
        self.assertEqual((e["commit"], e["add"], e["delete"]), (3, None, None))
        self.assertEqual(P.line_text(e["add"], e["delete"]), "—")
        m = P.add_to_file("# x\n\n| %s |\n|%s\n%s\n" % (" | ".join(P.COLUMNS), "---|" * len(P.COLUMNS), old), "Ali",
                           P.build_line(record("2026-10-28", add=1, delete=2)), "2026-10-28")
        self.assertEqual([(r["add"], r["delete"]) for r in P.file_lines(m)], [(None, None), (1, 2)])
        self.assertIn("Lines +", m)

    def test_weekly_quota_columns_present_missing_and_old_rows(self):
        r = dict(record("2026-10-28", quota=78), forecast=107, reset="2026-11-01 12:00", mode="TIGHT")
        p = P.line_parse(P.build_line(r))
        self.assertEqual((p["quota"], p["forecast"], p["reset"], p["mode"]), (78, 107, "2026-11-01 12:00", "TIGHT"))
        e = P.line_parse(P.build_line(record("2026-10-28")))      # missing data: empty, not an error
        self.assertEqual((e["forecast"], e["reset"], e["mode"]), (None, None, None))
        for n in (len(P.COLUMNS), len(P.INTERIM_COLUMN)):               # old 19- and 21-column rows still parse
            old = "|".join(P.build_line(record("2026-10-27", commit=3, add=5, delete=6)).split("|")[:n + 1]) + " |"
            o = P.line_parse(old)
            self.assertEqual((o["commit"], o["forecast"], o["reset"], o["mode"]), (3, None, None, None))
            m = P.add_to_file("# x\n\n| %s |\n|%s\n%s\n" % (" | ".join(P.EVERY_COLUMN[:n]), "---|" * n, old), "Ali", P.build_line(r), "2026-10-28")
            self.assertIn("| Forecast % | Reset | Mode |", m)
            self.assertEqual([x["mode"] for x in P.file_lines(m)], [None, "TIGHT"])
        empty = P.add_to_file("", "Ali", P.build_line(record("2026-10-28", quota=78)), "2026-10-28")
        full = P.add_to_file(empty, "Ali", P.build_line(r), "2026-10-28")          # same-day row without the fields is filled in once
        self.assertEqual([(x["forecast"], x["mode"]) for x in P.file_lines(full)], [(107, "TIGHT")])
        self.assertEqual(P.add_to_file(full, "Ali", P.build_line(record("2026-10-28")), "2026-10-28"), full)   # filled rows are never touched
        data = {"Ali": [r], "Veli": [record("2026-10-28", d7=50, quota=10)]}
        t = P.ranking_text(data, "2026-10-28")
        self.assertIn("| # | Person | Weekly % | Forecast | Projected run-out | Reset | Mode | Slack inbox | Last update |", t)
        self.assertIn("| # | Person | Plan | 7 days | vs last week | Commits (7 days) | Lines (7 days) | $ / 1000 lines |", t)
        self.assertIn("| 78% | 107% | — | 01.11 12:00 | TIGHT |", t)
        self.assertIn("| 10% | — | — | — | — |", t)
        r2 = dict(r, end_at="2026-10-31 21:00", forecast_time="2026-10-31 10:00")
        self.assertIn("| 107% | 31.10 21:00 | 01.11 12:00 |", P.ranking_text({"Ali": [r2]}, "2026-10-28"))
        self.assertIn("| 107% | lasts until the reset |", P.ranking_text({"Ali": [dict(r, end_at="enough")]}, "2026-10-28"))
        q = P.line_parse(P.build_line(r2))
        self.assertEqual((q["end_at"], q["forecast_time"]), ("2026-10-31 21:00", "2026-10-31 10:00"))
        self.assertEqual(P.line_parse(P.build_line(dict(r, end_at="enough")))["end_at"], "enough")
        old24 = "|".join(P.build_line(r).split("|")[:len(P.INTERIM2_COLUMN) + 1]) + " |"   # 24-column row from before the run-out columns
        self.assertEqual((P.line_parse(old24)["mode"], P.line_parse(old24)["end_at"]), ("TIGHT", None))
        m = P.add_to_file("# x\n\n| %s |\n|%s\n%s\n" % (" | ".join(P.INTERIM2_COLUMN), "---|" * len(P.INTERIM2_COLUMN), old24), "Ali", P.build_line(r2), "2026-10-28")
        self.assertEqual([x["end_at"] for x in P.file_lines(m)], ["2026-10-31 21:00"])   # same-day row gets the run-out filled in
        self.assertIn("| Run-out | Forecast time |", m)

    def test_ranking_uses_github_commits_and_shows_last_update(self):
        data = {"Ali": [record("2026-10-28", d7=200, commit=None)], "Veli": [record("2026-10-28", d7=100, commit=5)]}
        t0 = datetime.datetime(2026, 10, 28, 10, 5, tzinfo=datetime.timezone.utc).timestamp()
        m = P.ranking_text(data, "2026-10-28", {"Ali": 42}, {"Ali": t0})
        self.assertIn("| Mode | Slack inbox | Last update |", m)
        self.assertRegex(m, r"\| Ali \|.*\| 28\.10 13:05 \|")      # Istanbul time = UTC+3
        self.assertRegex(m, r"\| Ali \|[^\n]*\| 42 \| ")             # GitHub commit figure in the cost table
        self.assertRegex(m, r"\| Veli \|.*\| 2026-10-28 \|")           # no GitHub figure: row date
        self.assertRegex(m, r"\| Veli \|[^\n]*\| 5 \| ")                # row sum
        self.assertEqual(P.github_commit_count.__name__, "github_commit_count")

    def test_github_commit_count_leaves_out_chore_usage_commits(self):
        # the leaderboard's own automatic commits (chore(usage), and chore(last-window) from the same plugin) must not inflate the column
        topics = ["chore(usage): Ali 2026-11-04 · quota 47%→50%", "feat(x): real work", "chore(usage): Ali 2026-11-03",
                   "chore(last-window): Ali 2026-11-02 10:00", "fix(y): more work", "chore(deps): not one of ours"]
        call = subprocess.CompletedProcess([], 0, stdout="\n".join(topics) + "\n", stderr="")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(P, "_cache_path", return_value=os.path.join(d, "cache.json")), \
                mock.patch.object(P.subprocess, "run", return_value=call) as go, mock.patch.dict(os.environ, {"GH_ORG": "example-org"}):
            n = P.github_commit_count("ali", "2026-10-29", now=1000.0)
        self.assertEqual(n, 3)                                   # feat, fix, chore(deps): the two automation subjects are not counted
        self.assertIn("--paginate", go.call_args[0][0])

    def test_ranking_shows_7_day_line_sum_in_one_column_after_commits(self):
        data = {"Ali": [record("2026-10-27", d7=150, commit=2, add=10000, delete=3000), record("2026-10-28", d7=200, commit=3, add=2300, delete=1100)],
                "Veli": [record("2026-10-28", d7=300, plan=100, commit=1)],
                "Cem": [record("2026-10-28", d7=50, commit=1, add=950, delete=0)]}
        s = {r["person"]: r for r in P.compute_ranking(data, "2026-10-28")}
        self.assertEqual((s["Ali"]["add"], s["Ali"]["delete"], s["Veli"]["add"]), (12300, 4100, None))
        m = P.ranking_text(data, "2026-10-28")
        self.assertIn("| Commits (7 days) | Lines (7 days) |", m)
        self.assertIn("| 5 | +12.3K / −4.1K | $12.20 |", m)      # 200 / (16400 / 1000)
        self.assertIn("| 1 | — | — |", m)
        self.assertIn("| 1 | +950 / −0 | $52.63 |", m)         # 50 / 0.95
        self.assertIn("$ / 1000 lines", m)
        self.assertEqual(P.usd_bin_line(100, 0, 0), "—")
        self.assertLess(m.index("| Veli |"), m.index("| Ali |"))      # order is still by 7-day spend

    def test_rank_trend_commits_and_stale_marker(self):
        data = {"Ali": [record("2026-10-21", d7=100), record("2026-10-27", d7=150, commit=2), record("2026-10-28", d7=200, commit=3, quota=40)],
                "Veli": [record("2026-10-28", d7=300, plan=100)],
                "Ece": [record("2026-10-10", d7=999)]}
        s = P.compute_ranking(data, "2026-10-28")
        self.assertEqual([r["person"] for r in s], ["Veli", "Ali"])          # Ece has no row in the last 7 days
        ali = s[1]
        self.assertEqual((ali["d7"], ali["previous_d7"], ali["commit"], ali["quota"]), (200, 100, 5, 40))
        self.assertEqual(P.trend_text(200, 100), "▲ +100%")
        self.assertEqual(P.trend_text(100, None), "new")
        self.assertEqual(P.trend_text(101, 100), "≈ same")

    def test_empty_board_text(self):
        self.assertIn("No rows", P.ranking_text({}, "2026-10-28"))


class PlanComparison(unittest.TestCase):
    def data(self):
        # $200 person: 300 $ for 30 % -> 10 $/%; $100 person: 100 $ for 20 % -> 5 $/%
        return {"Ali": [record("2026-10-27", quota=10, week_usd=100, plan=200), record("2026-10-28", quota=30, week_usd=300, plan=200)],
                "Veli": [record("2026-10-27", quota=5, week_usd=25, plan=100, person="Veli"),
                         record("2026-10-28", quota=20, week_usd=100, plan=100, person="Veli")]}

    def test_per_plan_dollars_per_percent_uses_the_latest_row_of_each_week(self):
        pk = P.compare_plan(self.data())
        self.assertAlmostEqual(pk[200]["ratio"], 10.0)
        self.assertAlmostEqual(pk[100]["ratio"], 5.0)
        self.assertAlmostEqual(pk[200]["capacity"], 1000.0)
        self.assertAlmostEqual(pk[200]["monthly"], 1000 * 30 / 7.0)
        self.assertAlmostEqual(pk[100]["plan_head"], 500 * 30 / 7.0 / 100)
        self.assertEqual((pk[200]["person"], pk[200]["week"]), (1, 1))

    def test_small_quota_and_missing_plan_are_left_out(self):
        v = {"Ali": [record("2026-10-28", quota=9, week_usd=300), record("2026-10-27", quota=50, week_usd=300, plan=None)]}
        self.assertEqual(P.compare_plan(v), {})
        self.assertIn("Not enough data yet", P.ranking_text(v, "2026-10-28"))

    def test_text_states_the_ratio_when_both_plans_exist(self):
        m = P.ranking_text(self.data(), "2026-10-28")
        self.assertIn("**2.00×** the capacity", m)
        self.assertIn("The extra $100 pays off", m)
        self.assertIn("| $200 | 1 | 1 | 10.00 |", m)

    def test_only_one_plan_says_so(self):
        m = P.ranking_text({"Ali": self.data()["Ali"]}, "2026-10-28")
        self.assertIn("needs data from both plans", m)

    def test_weekday_pattern_uses_consecutive_rows_of_one_week(self):
        v = {"Ali": [record("2026-10-26", quota=10, week_usd=100), record("2026-10-27", quota=20, week_usd=250),       # usage day Mon 26
                     record("2026-10-28", quota=30, week_usd=300),                                                    # usage day Tue 27
                     record("2026-10-30", quota=50, week_usd=400),                                                    # gap: not consecutive
                     record("2026-10-31", quota=60, week_usd=100, week="2026-11-08")]}                               # other week: skipped
        g = P.day_pattern(v)
        self.assertAlmostEqual(g[(200, 0)], 15.0)       # Monday: +150 $ / +10 %
        self.assertAlmostEqual(g[(200, 1)], 5.0)        # Tuesday: +50 $ / +10 %
        self.assertEqual(sorted(g), [(200, 0), (200, 1)])   # the gap and the other week add nothing
        self.assertIn("| Mon | — | 15.00 |", P.ranking_text(v, "2026-10-31"))

    def test_week_table_and_model_summary(self):
        v = self.data()
        self.assertEqual(P.week_table(v), {"2026-11-01": {200: 10.0, 100: 5.0}})
        mo = P.model_summary(v, "2026-10-28")
        self.assertEqual(mo["Ali"][0]["sonnet"], 4.0)
        self.assertIn("**Team**", P.ranking_text(v, "2026-10-28"))

    def test_model_table_is_ranked_like_the_main_table_with_first_names(self):
        v = {"Zeynep Yılmaz": [record("2026-10-28", d7=500)], "Ali Veli": [record("2026-10-28", d7=900)], "Onur Demir": [record("2026-10-28", d7=700)]}
        m = P.ranking_text(v, "2026-10-28")
        table = m.split("## Models")[1].splitlines()
        lines = [l for l in table if l.startswith("| ")][1:]
        self.assertEqual([l.split("|")[1].strip() for l in lines[:3]], ["🥇", "🥈", "🥉"])
        self.assertEqual([l.split("|")[2].strip() for l in lines], ["Ali", "Onur", "Zeynep", "**Team**"])


class Quota(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="quota-", dir=TM._ROOT)

    def pace(self, **kw):
        p = {"week_used": 34, "updated": 1_800_000_000, "week_resets_at": 1_800_000_000 + 3 * 86400}
        p.update(kw)
        with open(os.path.join(self.d, "pace.json"), "w") as f:
            json.dump(p, f)

    def quota(self, now=1_800_000_000 + 3600):
        with mock.patch.object(cost, "CLAUDE", self.d):
            return P.weekly_quota(now)

    def test_fresh_value_is_used_and_bad_data_is_omitted(self):
        self.pace()
        self.assertEqual(self.quota(), 34)
        self.pace(updated=1_800_000_000 - 100 * 3600)
        self.assertIsNone(self.quota(), "stale")
        self.pace(week_resets_at=1_800_000_000)
        self.assertIsNone(self.quota(), "week already reset")
        self.pace(week_used=140)
        self.assertIsNone(self.quota())
        self.pace(week_used=True)
        self.assertIsNone(self.quota())
        os.remove(os.path.join(self.d, "pace.json"))
        self.assertIsNone(self.quota(), "missing file")

    def test_forecast_reset_and_mode_come_from_pace_json(self):
        now = 1_800_000_000 + 3600
        self.pace(p50=106.6, mode="TIGHT")
        with mock.patch.object(cost, "CLAUDE", self.d):
            t, reset, mode = P.pace_extra(now)
            self.assertEqual((t, mode), (107, "TIGHT"))
            self.assertRegex(reset, r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
            self.pace(mode="bad mode!", p50="x")
            self.assertEqual(P.pace_extra(now)[0::2], (None, None))
            self.pace(updated=1_800_000_000 - 100 * 3600, p50=90, mode="RELAXED")
            self.assertEqual(P.pace_extra(now), (None, None, None), "stale")
            os.remove(os.path.join(self.d, "pace.json"))
            self.assertEqual(P.pace_extra(now), (None, None, None), "missing file")

    def test_runout_from_pace_json_incl_bom_and_lasts_until_reset(self):
        now = 1_800_000_000 + 3600
        with mock.patch.object(cost, "CLAUDE", self.d):
            self.pace(p50=106.6, runout_at=1_800_000_000 + 7200)
            b, z = P.end_at_extra(now)
            self.assertRegex(b, r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
            self.assertEqual(b, time.strftime("%Y-%m-%d %H:%M", time.localtime(1_800_000_000 + 7200)))
            self.assertRegex(z, r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
            self.pace(p50=80.0, runout_at=None)
            self.assertEqual(P.end_at_extra(now)[0], "enough")
            self.pace(p50=80.0)                                      # file from an older pace_forecast: no runout_at key
            self.assertEqual(P.end_at_extra(now), (None, None))
            self.pace(updated=1_800_000_000 - 100 * 3600, p50=90, runout_at=None)
            self.assertEqual(P.end_at_extra(now), (None, None), "stale")
            with open(os.path.join(self.d, "pace.json"), "w", encoding="utf-8-sig") as f:   # Windows file with a BOM
                json.dump({"week_used": 34, "updated": 1_800_000_000, "week_resets_at": 1_800_000_000 + 3 * 86400,
                           "p50": 90.0, "runout_at": 1_800_000_000 + 7200}, f)
            self.assertIsNotNone(P.end_at_extra(now)[0])
            os.remove(os.path.join(self.d, "pace.json"))
            self.assertEqual(P.end_at_extra(now), (None, None), "missing file")


@unittest.skipUnless(shutil.which("git"), "git not installed")
class Commits(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="root-", dir=TM._ROOT)

    def git(self, repo, *a, env=None):
        e = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, **(env or {}))
        r = subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def repo(self, name, base="main"):
        path = os.path.join(self.root, name)
        os.makedirs(path)
        self.git(path, "init", "-q", "-b", base)
        return path

    def commit(self, repo, mail, hour_once, name="f"):
        t = int(time.time() - hour_once * 3600)
        d = "%d +0000" % t
        with open(os.path.join(repo, name + str(t) + mail), "w") as f:
            f.write(mail)
        self.git(repo, "add", "-A")
        self.git(repo, "-c", "user.name=x", "-c", "user.email=" + mail, "commit", "-q", "-m", "c",
                 env={"GIT_AUTHOR_DATE": d, "GIT_COMMITTER_DATE": d})

    def remote_ref(self, repo, name):
        self.git(repo, "update-ref", "refs/remotes/origin/" + name, "HEAD")

    def test_counts_own_recent_commits_on_the_integration_branch_across_clones(self):
        a = self.repo("a")                              # oldest first, like a real history
        self.commit(a, "me@example.com", 30)            # older than 24 h
        self.commit(a, "other@example.com", 3)
        self.commit(a, "ME@example.com", 2)             # e-mail case does not matter
        self.commit(a, "me@example.com", 1)
        self.remote_ref(a, "main")
        self.commit(a, "me@example.com", 0.5)           # local only, never reached origin: not "reached dev"
        b = self.repo(os.path.join("group", "b"), base="dev")
        self.commit(b, "me@example.com", 5)
        self.remote_ref(b, "dev")
        c = self.repo("c")                              # no origin ref at all: skipped
        self.commit(c, "me@example.com", 1)
        self.assertEqual(P.commit_count("me@example.com", self.root), 3)
        self.assertEqual(P.commit_count("other@example.com", self.root), 1)
        self.assertEqual(P.commit_count("nobody@example.com", self.root), 0)

    def test_unknown_when_no_email_or_root(self):
        self.assertIsNone(P.commit_count("", self.root))
        self.assertIsNone(P.commit_count("me@example.com", None))

    def test_numstat_parse_excludes_noise(self):
        t = lambda *x: "\t".join(x)
        out = "\n".join(["\x01h1", "", t("10", "2", "src/a.cs"), t("-", "-", "img/logo.png"), t("500", "5", "package-lock.json"),
                         t("9", "9", "web/yarn.lock"), t("9", "9", "Cargo.lock"), t("9", "9", "pnpm-lock.yaml"), t("9", "9", "x/packages.lock.json"),
                         t("99", "0", "Data/Migrations/20260101_A.Designer.cs"), t("99", "0", "Data/AppDbContextModelSnapshot.cs"),
                         t("7", "0", "Data/Migrations/20260101_A.cs"), t("9", "9", "w/app.min.js"), t("9", "9", "w/app.min.css"), t("9", "9", "w/app.js.map"),
                         t("9", "9", "bin/x.dll"), t("9", "9", "a/obj/y.cs"), t("9", "9", "node_modules/p/i.js"), t("9", "9", "ui/dist/m.js"),
                         t("9", "9", "ui/build/m.js"), t("9", "9", "web/wwwroot/lib/jq.js"), t("0", "0", "old.cs => new.cs"),
                         t("3", "1", "src/{a => b}/c.cs"), "\x01h2", t("0", "0", "{x => y}/z.cs"), "\x01h3", t("1", "1", "builder/a.cs")])
        self.assertEqual(P.numstat_parse(out), {"h1": (10 + 7 + 3, 2 + 0 + 1), "h2": (0, 0), "h3": (1, 1)})
        self.assertEqual(P.numstat_parse(""), {})

    def test_generated_and_data_files_are_excluded(self):
        t = lambda *x: "\t".join(x)
        # Turkish locale data: Turkish folder names (Yardımcı, Doğru, Hata, Rehber) are real-world path samples.
        out = "\n".join(["\x01h1", t("9000", "0", "iso/standartlar/TS_EN_ISO_27002.pdf"), t("900", "0", "Yardımcı/Doğru/PRODUCT_A_SMS_CDR_1.cdr"),
                         t("90", "0", "Hata/bad_REHBER_2022.bad"), t("90", "0", "logs/x.jsonl"), t("90", "0", "img/a.svg"),
                         t("90", "0", "Api/Client.g.cs"), t("90", "0", "Api/Client.g.i.cs"), t("90", "0", "Hata/22-10-20/bad_REHBER_202210180930"), t("90", "0", "Rehber/PRODUCT_A_312_20221002"), t("5", "1", "src/Real.cs"), t("4", "0", "docs/a.md")])
        self.assertEqual(P.numstat_parse(out), {"h1": (9, 1)})

    def test_non_ascii_paths_are_not_quoted_by_git(self):
        a = self.repo("a")
        t = int(time.time() - 3600)
        d = "%d +0000" % t
        os.makedirs(os.path.join(a, "Yardımcı"))
        with open(os.path.join(a, "Yardımcı", "x.cdr"), "w") as f:
            f.write("1\n" * 40)
        with open(os.path.join(a, "k.py"), "w") as f:
            f.write("1\n")
        self.git(a, "add", "-A")
        self.git(a, "-c", "user.name=x", "-c", "user.email=me@example.com", "commit", "-q", "-m", "c",
                 env={"GIT_AUTHOR_DATE": d, "GIT_COMMITTER_DATE": d})
        self.remote_ref(a, "main")
        self.assertEqual(P.commits_and_lines("me@example.com", self.root), (1, 1, 0))

    def test_line_sums_of_the_same_commits_and_dedup_across_clones(self):
        a = self.repo("a")
        t = int(time.time() - 3600)
        d = "%d +0000" % t
        with open(os.path.join(a, "k.py"), "w") as f:
            f.write("1\n2\n3\n")
        with open(os.path.join(a, "package-lock.json"), "w") as f:
            f.write("x\n" * 50)
        self.git(a, "add", "-A")
        self.git(a, "-c", "user.name=x", "-c", "user.email=me@example.com", "commit", "-q", "-m", "c",
                 env={"GIT_AUTHOR_DATE": d, "GIT_COMMITTER_DATE": d})
        self.remote_ref(a, "main")
        b = os.path.join(self.root, "b")
        subprocess.run(["git", "clone", "-q", a, b], check=True, capture_output=True)
        self.assertEqual(P.commits_and_lines("me@example.com", self.root), (1, 3, 0))
        self.assertEqual(P.commits_and_lines("other@example.com", self.root), (0, 0, 0))
        self.assertIsNone(P.commits_and_lines("", self.root))

    def test_same_commit_in_two_clones_counts_once(self):
        a = self.repo("a")
        self.commit(a, "me@example.com", 1)
        self.remote_ref(a, "main")
        b = os.path.join(self.root, "b")
        subprocess.run(["git", "clone", "-q", a, b], check=True, capture_output=True)
        self.assertEqual(P.commit_count("me@example.com", self.root), 1)

    def test_root_that_is_not_there_is_skipped(self):
        with mock.patch.dict(os.environ, {"CLAUDE_SHARED_ROOT": os.path.join(self.root, "missing")}):
            self.assertIsNone(P.root_dir())



    def test_default_layout_is_found_without_a_setting(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        root = os.path.join(home, "src")
        os.makedirs(root)
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(P, "HOME", home),                 mock.patch.object(P.settings, "first_line", return_value=""):
            os.environ.pop("CLAUDE_SHARED_ROOT", None)
            self.assertEqual(P.root_dir(), root)


class SameDayFill(unittest.TestCase):
    def test_same_day_row_with_unknown_commits_and_zero_split_is_refilled(self):
        old = record("2026-10-30", opus=0.0, sonnet=0.0)
        new = record("2026-10-30", commit=4, add=10, delete=2)
        text = P.add_to_file("", "Ali", P.build_line(old), "2026-10-30")
        text = P.add_to_file(text, "Ali", P.build_line(new), "2026-10-30")
        r = P.file_lines(text)
        self.assertEqual((len(r), r[0]["commit"], r[0]["usd"]["sonnet"]), (1, 4, 2.0))
        self.assertEqual(P.add_to_file(text, "Ali", P.build_line(new), "2026-10-30"), text)


class ModelCoverage(unittest.TestCase):
    DAY = {"2026-10-26": ({"opus": 5.0}, {"opus": 1.0}), "2026-10-27": ({"opus": 2.0, "sonnet": 1.0}, {"opus": 1.0}),
           "2026-10-28": ({"sonnet": 4.0}, {"sonnet": 2.0})}

    def rec(self, previous):
        k = record("2026-10-30")
        k["model_days"] = dict(self.DAY)
        P.apply_scope(k, [record(t) for t in previous])
        return k

    def test_first_row_covers_last_seven_days_even_if_yesterday_was_idle(self):
        k = self.rec([])   # 2026-10-29 has no record: used to give an all-zero split
        self.assertEqual(k["usd"], {"opus": 7.0, "sonnet": 5.0})

    def test_daily_row_covers_only_uncovered_days(self):
        self.assertEqual(self.rec(["2026-10-28"])["usd"], {"sonnet": 4.0})   # row 28th covered the 27th; 28th and 29th are new
        self.assertEqual(self.rec(["2026-10-29"])["usd"], {})   # normal day: only yesterday (idle)

    def test_commit_window_follows_the_same_days(self):
        for previous, hour in (([], 168), (["2026-10-28"], 48), (["2026-10-29"], 24)):
            k = record("2026-10-30")
            k["mail"], k["root"] = "me@x", "root"
            with mock.patch.object(P, "commits_and_lines", return_value=(7, 70, 7)) as m:
                P.apply_scope(k, [record(t) for t in previous])
            m.assert_called_once_with("me@x", "root", hour)
            self.assertEqual((k["commit"], k["add"], k["delete"]), (7, 70, 7))

    def test_unknown_root_keeps_dashes(self):
        k = record("2026-10-30")
        k["mail"], k["root"] = "me@x", None
        P.apply_scope(k, [])
        self.assertIsNone(k["commit"])

    def test_no_double_count_across_rows(self):
        a = self.rec([])
        self.assertEqual(sum(a["usd"].values()), 12.0)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class SharedFiles(unittest.TestCase):
    """The git flow against a local bare repository: own row, regenerated ranking, ownership, retry after a lost race."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="remote-", dir=TM._ROOT)
        self.remote = os.path.join(self.d, "remote.git")
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        self.sh("git", "init", "-q", "--bare", "-b", "main", self.remote)
        seed = os.path.join(self.d, "seed")
        self.sh("git", "clone", "-q", self.remote, seed)
        path = os.path.join(seed, *P.PEOPLE_PATH.split("/"))
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            json.dump({"gh-ali": "Ali", "gh-veli": "Veli"}, f)
        self.sh("git", "-C", seed, "add", "-A")
        self.sh("git", "-C", seed, "commit", "-q", "-m", "seed")
        self.sh("git", "-C", seed, "push", "-q", "origin", "HEAD:main")
        self.seed = seed
        self._env = mock.patch.dict(os.environ, self.env)
        self._env.start()
        self._genv = mock.patch.object(P, "GIT_ENV", dict(self.env, GIT_TERMINAL_PROMPT="0"))
        self._genv.start()

    def tearDown(self):
        self._genv.stop()
        self._env.stop()

    def sh(self, *a):
        r = subprocess.run(a, capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def run_cmd(self, clone, r):
        return P.one_attempt(clone, self.remote, r, r["date"])

    def test_row_and_ranking_are_committed_and_pushed_and_a_second_run_changes_nothing(self):
        k = os.path.join(self.d, "clone1")
        self.assertEqual(self.run_cmd(k, record("2026-10-28", quota=40, week_usd=300)), "pushed")
        self.assertEqual(self.run_cmd(k, record("2026-10-28", quota=40, week_usd=300)), "no change")
        self.sh("git", "-C", self.seed, "pull", "-q", "origin", "main")
        ali = TM.rd(os.path.join(self.seed, "reports", "usage", "ali.md"))
        self.assertEqual(len(P.file_lines(ali)), 1)
        self.assertIn("Ali", TM.rd(os.path.join(self.seed, "reports", "usage", "RANKING.md")))
        self.assertIn("chore(usage): Ali 2026-10-28", self.sh("git", "-C", self.seed, "log", "-1", "--format=%s"))

    def test_second_person_regenerates_the_ranking_from_all_files(self):
        self.run_cmd(os.path.join(self.d, "k1"), record("2026-10-28", d7=100))
        self.run_cmd(os.path.join(self.d, "k2"), record("2026-10-28", d7=500, plan=100, person="Veli"))
        self.sh("git", "-C", self.seed, "pull", "-q", "origin", "main")
        s = TM.rd(os.path.join(self.seed, "reports", "usage", "RANKING.md"))
        self.assertLess(s.index("Veli"), s.index("Ali"))
        self.assertEqual(self.sh("git", "-C", self.seed, "rev-list", "--count", "HEAD"), "3")

    def test_unlisted_name_writes_nothing_and_a_stray_file_is_not_ranked(self):
        with self.assertRaises(RuntimeError) as c:
            self.run_cmd(os.path.join(self.d, "k3"), record("2026-10-28", person="Mallory"))
        self.assertIn("people.json", str(c.exception))
        path = os.path.join(self.seed, "reports", "usage")
        os.makedirs(path)
        with open(os.path.join(path, "mallory.md"), "w", encoding="utf-8") as f:
            f.write(P.file_title("Mallory") + P.build_line(record("2026-10-28", d7=9999, person="Mallory")) + "\n")
        self.sh("git", "-C", self.seed, "add", "-A")
        self.sh("git", "-C", self.seed, "commit", "-q", "-m", "stray")
        self.sh("git", "-C", self.seed, "push", "-q", "origin", "HEAD:main")
        self.run_cmd(os.path.join(self.d, "k4"), record("2026-10-28", d7=10))
        self.sh("git", "-C", self.seed, "pull", "-q", "origin", "main")
        self.assertNotIn("Mallory", TM.rd(os.path.join(path, "RANKING.md")))

    def test_lost_race_is_redone_once_on_a_fresh_fetch(self):
        k = os.path.join(self.d, "k5")
        real = P.prepare_clone
        event = {"n": 0}

        def racy(clone, remote):
            real(clone, remote)
            event["n"] += 1
            if event["n"] == 1:      # somebody else pushes right after our fetch
                self.sh("git", "-C", self.seed, "pull", "-q", "origin", "main")
                with open(os.path.join(self.seed, "x.txt"), "w") as f:
                    f.write("x")
                self.sh("git", "-C", self.seed, "add", "-A")
                self.sh("git", "-C", self.seed, "commit", "-q", "-m", "other")
                self.sh("git", "-C", self.seed, "push", "-q", "origin", "HEAD:main")
        with mock.patch.object(P, "prepare_clone", racy), mock.patch.object(P.time, "sleep"), \
                mock.patch.object(P, "todays_record", return_value=record("2026-10-28")), \
                mock.patch.object(P, "person_name", return_value="Ali"), \
                mock.patch.dict(os.environ, {"CLAUDE_SHARED_CLONE": k, "CLAUDE_SHARED_REMOTE": self.remote}):
            self.assertEqual(P.share(), 0)
        self.assertEqual(event["n"], 2)
        self.sh("git", "-C", self.seed, "pull", "-q", "origin", "main")
        self.assertTrue(os.path.isfile(os.path.join(self.seed, "reports", "usage", "ali.md")))


class CostWindowsAndCli(unittest.TestCase):
    """The numbers come from the real CLI on a synthetic transcript; --dry prints the row and writes nothing."""

    def setUp(self):
        self.cfg = TM.Cfg()
        self.setting = tempfile.mkdtemp(prefix="home-", dir=TM._ROOT)

    def run_cli(self, *args, person="Test Kisi"):
        env = {"HOME": self.setting, "USERPROFILE": self.setting, "CLAUDE_SHARED_PERSON": person, "CLAUDE_SHARED_PLAN": "200",
               "CLAUDE_SHARED_ROOT": os.path.join(self.setting, "missing")}
        return self.cfg.run(*args, env=env)

    def test_row_has_windows_quota_plan_week_and_models(self):
        now = time.time()
        self.cfg.transcript("p", "s1", [TM.asst(now - 600, "m1", model="claude-opus-5-5", out=20000),
                                        TM.asst(now - 30 * 3600, "m2", model="claude-sonnet-5-5", out=40000)])
        with open(os.path.join(self.cfg.dir, "pace.json"), "w") as f:
            json.dump({"week_used": 41, "updated": now - 3600, "week_resets_at": now + 86400}, f)
        r = self.run_cli("--share", "--dry")
        self.assertEqual(r.returncode, 0, r.stderr)
        line = [l for l in r.stdout.splitlines() if l.startswith("| 20")]
        self.assertEqual(len(line), 1, r.stdout)
        p = P.line_parse(line[0])
        self.assertEqual((p["d24"], p["quota"], p["plan"], p["commit"]), (p["d24"], 41, 200, None))
        self.assertIsNotNone(p["week"])
        self.assertIsNotNone(p["week_usd"])
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.dir, "cost", "daily.jsonl")))   # --share includes --save
        self.assertNotIn("projects", line[0])

    def test_sharing_failure_keeps_exit_code_zero_and_logs(self):
        self.cfg.transcript("p", "s1", [TM.asst(time.time() - 600, "m1", out=20000)])
        r = self.cfg.run("--share", env={"HOME": self.setting, "USERPROFILE": self.setting, "CLAUDE_SHARED_PERSON": "Test Kisi",
                                           "CLAUDE_SHARED_REMOTE": os.path.join(self.setting, "missing.git"),
                                           "CLAUDE_SHARED_CLONE": os.path.join(self.setting, "clone")})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("FAILED", TM.rd(os.path.join(self.cfg.dir, "cost", "share.log")))
        self.assertNotIn("Traceback", r.stderr)

    def test_without_a_name_nothing_is_sent(self):
        with open(os.path.join(self.setting, ".gitconfig"), "w") as f:
            f.write("")
        env = {"HOME": self.setting, "USERPROFILE": self.setting, "CLAUDE_SHARED_PERSON": "", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        r = self.cfg.run("--share", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no person name", TM.rd(os.path.join(self.cfg.dir, "cost", "share.log")))


class ShippedFiles(unittest.TestCase):
    def test_module_has_no_personal_or_machine_strings(self):
        txt = TM.rd(os.path.join(TM.BIN, "cost_share.py")).lower()
        # "slack" itself is allowed since the Slack inbox column (the module reads the local poller's state); what stays forbidden is
        # anything that talks to Slack or carries a credential
        for b in ("/home/", "example-user", "example-host", "hooks.slack.com", "api.slack.com", "chat.postmessage", "xoxb", "xoxp", "xapp-",
                  "slack-inbox.jsonl", "okundu"):
            self.assertNotIn(b, txt)



class UpcomingWorkWhoTakes(unittest.TestCase):
    def test_order(self):
        order = [{"person": "Ali X", "mode": "TIGHT", "forecast": 10}, {"person": "Cem X", "mode": None, "forecast": None},
                {"person": "Bora X", "mode": "PLENTY", "forecast": 80}, {"person": "Deniz X", "mode": "PLENTY", "forecast": 40},
                {"person": "Ece X", "mode": "NORMAL", "forecast": 5}]
        m = "\n".join(P.upcoming_work_lines(order))
        self.assertIn("### Who picks next", m)
        names = re.findall(r"^\| \d \| (\w+) \|", m, re.M)
        self.assertEqual(names, ["Deniz", "Bora", "Ece", "Ali", "Cem"])
        self.assertIn("| 5 | Cem | no data | — |", m)
        self.assertIn("| 1 | Deniz | PLENTY | 40% |", m)

    def test_testers_and_managers_take_no_work(self):
        order = [{"person": "Ali X", "mode": "PLENTY", "forecast": 5}, {"person": "Ece X", "mode": "NORMAL", "forecast": 50},
                {"person": "Veli X", "mode": "PLENTY", "forecast": 1}]
        m = "\n".join(P.upcoming_work_lines(order, {"Ali X": "tester", "Veli X": "manager"}))
        self.assertEqual(re.findall(r"^\| \d \| (\w+) \|", m, re.M), ["Ece"])
        self.assertIn("_tester (takes no work): Ali_", m)
        self.assertIn("_manager (takes no work): Veli_", m)
        self.assertNotIn("tester (takes no work)", "\n".join(P.upcoming_work_lines(order, {})))

    def test_ranking_in_text(self):
        self.assertNotIn("Who picks next", P.ranking_text({}, "2026-10-28"))


class GithubCommitCountWithoutStdout(unittest.TestCase):
    def _call(self, run):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(P, "_cache_path", return_value=os.path.join(d, "ob.json")), \
                mock.patch.object(P.subprocess, "run", run):
            with mock.patch.dict(os.environ, {"GH_ORG": "example-org"}):
                return P.github_commit_count("x", "2026-11-01", now=1.0)

    def test_no_org_configured_is_unknown_without_a_gh_call(self):
        go = mock.Mock()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(P, "_cache_path", return_value=os.path.join(d, "ob.json")), \
                mock.patch.object(P.subprocess, "run", go), mock.patch.object(P.settings, "first_line", return_value=""), \
                mock.patch.dict(os.environ, {"GH_ORG": ""}):
            self.assertIsNone(P.github_commit_count("x", "2026-11-01", now=1.0))
        go.assert_not_called()

    def test_stdout_none_unknown(self):
        self.assertIsNone(self._call(lambda *a, **k: mock.Mock(returncode=0, stdout=None)))

    def test_utf8_and_automation_kept_apart(self):
        kw = {}
        def run(*a, **k):
            kw.update(k)
            # Turkish locale data: Turkish letters in a commit subject must survive decoding.
            return mock.Mock(returncode=0, stdout="feat: ğüş\nchore(usage): x\n")
        self.assertEqual(self._call(run), 1)
        self.assertEqual(kw.get("encoding"), "utf-8")


if __name__ == "__main__":
    unittest.main()

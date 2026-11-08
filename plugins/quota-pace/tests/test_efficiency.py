"""Tests for cost_efficiency: weekly per-project summary, finished-work count, cost per finished item, the leaderboard section and the
change-only commit. Stdlib only, synthetic data, no network (the board items are plain dicts, the remote is a local bare repository).

Run: python3 -I -m unittest discover -s plugins/quota-pace/tests -p 'test_efficiency.py'
"""
import datetime
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cost as TM  # noqa: E402  (scratch HOME, CLAUDE_SHARED_EFFICIENCY=0)
import cost_share as P  # noqa: E402
import cost_efficiency as V  # noqa: E402
import test_share_change as D  # noqa: E402
from test_share import day  # noqa: E402

def setUpModule():
    os.makedirs(TM._CFG, exist_ok=True)   # test_cost's tearDownModule (or another module's) may have removed the scratch tree


UTC = datetime.timezone.utc
TODAY = "2026-11-04"   # Wednesday of 2026-W45 (Mon 2026-11-02 .. Sun 2026-11-08)


def job(session, project, usd, tl, quota_tl, **tok):
    d = {"session": session, "project": project, "is": "Musteri ACME fatura isi", "id": "abc123", "usd_total": usd, "tl": tl, "quota_share_tl": quota_tl,
         "week_percent": usd / 10.0, "in": 10, "out": 20, "cr": 300, "cw5": 4, "cw1h": 5}
    d.update(tok)
    return d


def report():
    """A cost_report.conversion()-shaped result: two jobs of session s1 (project ops), one job of s2 (product-c), session s3 without jobs."""
    j1, j2, j3 = job("s1", "ops", 10.0, 500.0, 5.0), job("s1", "ops", 6.0, 300.0, 3.0), job("s2", "product-c", 4.0, 200.0, 2.0)
    session = lambda sid, usd: {"session": sid, "project_folder": "/home/ali/musteri-acme", "usd": usd, "usd_total": usd, "tl": usd * 50, "quota_share_tl": usd / 2,
                               "week_percent": usd / 10.0, "in": 100, "out": 200, "cr": 3000, "cw5": 40, "cw1h": 50}
    return {"fx_rate": 50.0, "projects": [{"project": "ops", "jobs": [j1, j2]}, {"project": "product-c", "jobs": [j3]}],
            "sessions": [session("s1", 2.0), session("s2", 1.0), session("s3", 3.0)]}


class Total(unittest.TestCase):
    def setUp(self):
        self.o = V.build_summary(report(), "Ali", TODAY, done=3)

    def test_week_and_person(self):
        self.assertEqual((self.o["week"], self.o["start"], self.o["end"], self.o["person"], self.o["done"]), ("2026-W45", "2026-11-02", "2026-11-08", "Ali", 3))

    def test_per_project_totals_and_unallocated_session_bucket(self):
        p = self.o["projects"]
        self.assertEqual(sorted(p), ["main-session", "ops", "product-c"])
        self.assertEqual((p["ops"]["usd"], p["ops"]["tl_api"], p["ops"]["tl_subscription"]), (16.0, 800.0, 8.0))
        self.assertEqual((p["main-session"]["usd"], p["main-session"]["tl_api"]), (3.0, 150.0))     # s3 started no job
        t = self.o["total"]
        self.assertEqual(t["usd"], 23.0)
        self.assertEqual(t["usd"], round(sum(x["usd"] for x in p.values()), 2))                 # the total is the sum of the projects
        self.assertEqual(t["tl_api"], round(sum(x["tl_api"] for x in p.values()), 2))

    def test_tokens_by_type_include_the_session_share(self):
        t = self.o["projects"]["ops"]["tok"]
        self.assertEqual(t["in"], 10 + 10 + 100)      # two jobs + the whole of session s1 (its jobs are both in ops)
        self.assertEqual(t["cr"], 300 + 300 + 3000)
        self.assertEqual(set(t), {"in", "out", "cr", "cw5", "cw1h"})

    def test_nothing_identifying_is_published(self):
        txt = V.summary_write_text(self.o)
        for forbidden in ("ACME", "fatura", "abc123", "/home", "musteri", "s1", "project_folder", "\"is\""):
            self.assertNotIn(forbidden, txt)
        self.assertEqual(sorted(self.o), ["done", "end", "fx_rate", "person", "projects", "start", "total", "version", "week"])

    def test_missing_rate_and_calibration_give_none_not_zero(self):
        R = report()
        for g in R["projects"]:
            for j in g["jobs"]:
                j.update(tl=None, quota_share_tl=None, week_percent=None)
        o = V.build_summary(R, "Ali", TODAY)
        self.assertIsNone(o["projects"]["ops"]["tl_api"])
        self.assertIsNone(o["projects"]["ops"]["tl_subscription"])
        self.assertEqual(o["projects"]["ops"]["usd"], 16.0)


class Done(unittest.TestCase):
    def item(self, url, who, time, repo="o/r", n=1):
        return {"url": url, "repo": repo, "number": n, "assignees": who, "status_at": time}

    def test_counts_only_this_week_this_person_distinct(self):
        o = [self.item("u1", ["ali"], "2026-11-02T00:30:00Z"),            # Monday, inside
             self.item("u2", ["Ali", "veli"], "2026-11-04T10:00:00Z"),      # inside, login case ignored, two assignees
             self.item("u2", ["ali"], "2026-11-04T10:00:00Z"),            # same issue on a second board: once
             self.item("u3", ["ali"], "2026-10-31T22:00:00Z"),            # last week
             self.item("u4", ["ali"], "2026-11-09T00:10:00Z"),            # next week
             self.item("u5", ["veli"], "2026-11-03T10:00:00Z"),             # someone else
             self.item("u6", [], "2026-11-03T10:00:00Z"),                 # unassigned
             self.item("u7", ["ali"], None)]                              # no timestamp
        self.assertEqual(V.compute_done(o, "ali", TODAY, UTC), 2)

    def test_week_boundaries_use_the_local_zone(self):
        tz = datetime.timezone(datetime.timedelta(hours=3))
        o = [self.item("u1", ["ali"], "2026-11-01T22:00:00Z"),            # Mon 01:00 local (+3): inside although Sunday in UTC
             self.item("u2", ["ali"], "2026-11-08T21:30:00Z")]            # Mon 00:30 local next week: outside
        self.assertEqual(V.compute_done(o, "ali", TODAY, tz), 1)
        self.assertEqual(V.compute_done(o, "ali", TODAY, UTC), 1)       # UTC: first is Sunday (out), second is Sunday 21:30 (in)

    def test_cache_hit_makes_no_board_call(self):
        path = V._cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"ali|2026-W45": {"n": 7, "t": 1000.0}}, f)
        with mock.patch.object(V, "_tools_module", side_effect=AssertionError("network")):
            self.assertEqual(V.done_count("Ali", TODAY, now=1000.0 + 3600), 7)
            self.assertIsNone(V.done_count("Ali", TODAY, now=1000.0 + 7 * 3600))   # stale cache + no tools -> unknown, not a crash


class Efficiency(unittest.TestCase):
    def summary(self, name, tl, done, week="2026-W45", usd=100.0, tl_ab=None):
        return {"version": 1, "person": name, "week": week, "done": done,
                "total": {"usd": usd, "tl_api": tl, "tl_subscription": tl_ab, "quota_percent": 40.0, "tok": {}},
                "projects": {"ops": {"usd": 60.0}, "product-c": {"usd": 30.0}, "x": {"usd": 5.0}, "y": {"usd": 5.0}}}

    def test_cost_per_item_and_dash_for_zero(self):
        self.assertEqual(V.work_per(1000.0, 4), 250.0)
        self.assertIsNone(V.work_per(1000.0, 0))
        self.assertIsNone(V.work_per(1000.0, None))
        self.assertIsNone(V.work_per(None, 3))

    def test_efficiency_ranking_lowest_first_and_needs_one_item(self):
        summaries = {"Ali": self.summary("Ali", 9000.0, 3), "Veli": self.summary("Veli", 2000.0, 4), "Cem": self.summary("Cem", 100.0, 0),
              "Ece": self.summary("Ece", 10.0, 5, week="2026-W44")}
        self.assertEqual(V.efficiency_rank(summaries, TODAY), [("Veli", 500.0), ("Ali", 3000.0)])      # Cem has 0 items, Ece is last week

    def test_section_rows(self):
        summaries = {"Ali": self.summary("Ali", 9000.0, 3, tl_ab=300.0), "Cem": self.summary("Cem", 100.0, 0)}
        md = "\n".join(V.table_lines(["Ali", "Cem", "Deniz"], summaries, TODAY, P.MEDAL))
        self.assertIn("| 🥇 | Ali | 40% | 3 | ₺9,000 | ₺300 | ₺3,000 | ₺100 | ops 60% · product-c 30% · x 5% · +1 |", md)
        self.assertIn("| 🥈 | Cem | 40% | 0 | ₺100 | — | — | — | ops 60% · product-c 30% · x 5% · +1 |", md)                            # zero finished -> dashes
        self.assertIn("| 🥉 | Deniz | — | — | — | — | — | — | — |", md)                              # no summary yet
        self.assertIn("🥇 Ali ₺3,000/item", md)
        self.assertNotIn("Cem ₺", md.split("Efficiency")[1])

    def test_change_key_ignores_small_dollar_drift_but_sees_a_finished_item(self):
        a = {"Ali": self.summary("Ali", 1.0, 3, usd=120.0)}
        self.assertEqual(V.efficiency_key(a, TODAY), V.efficiency_key({"Ali": self.summary("Ali", 1.0, 3, usd=150.0)}, TODAY))
        self.assertNotEqual(V.efficiency_key(a, TODAY), V.efficiency_key({"Ali": self.summary("Ali", 1.0, 4, usd=120.0)}, TODAY))
        self.assertNotEqual(V.efficiency_key(a, TODAY), V.efficiency_key({"Ali": self.summary("Ali", 1.0, 3, usd=210.0)}, TODAY))


class Flow(D.Flow):
    """The timer run with a weekly summary: published in the same commit, change-only, shown in RANKING.md."""

    def week_id(self):
        return V.week_cap(day())[0]

    def r(self, done, usd=100.0, tl=5000.0, **kw):
        k = D.line(fixture=dict(d7=100, quota=40), **kw)
        k["week_summary"] = {"version": 1, "person": "Ali", "week": self.week_id(), "start": "x", "end": "y", "fx_rate": 50.0, "done": done,
                           "total": {"usd": usd, "tl_api": tl, "tl_subscription": 400.0, "quota_percent": 40.0, "tok": {"in": 1}},
                           "projects": {"ops": {"usd": usd, "tl_api": tl, "tl_subscription": 400.0, "quota_percent": 40.0, "tok": {"in": 1}}}}
        return k

    def json(self):
        self.sh("git", "-C", self.seed, "pull", "-q", "origin", "main")
        return json.loads(TM.rd(os.path.join(self.seed, "reports", "usage", "ali.week.json")))

    def test_published_in_the_same_commit_and_shown(self):
        self.assertEqual(self.run_cmd(self.r(3)), "pushed")
        self.assertIn("reports/usage/ali.week.json", self.files())
        self.assertNotIn("reports/usage/veli.week.json", self.files())                   # nobody else's file
        self.assertEqual(self.json()["done"], 3)
        md = self.sh("git", "--git-dir", self.remote, "show", "main:reports/usage/RANKING.md")
        self.assertIn("### Finished work and its cost (%s)" % self.week_id(), md)
        self.assertIn("₺1,667", md)                                                               # 5000 / 3 per finished item
        self.assertIn("**Efficiency", md)

    def test_unchanged_summary_commits_nothing_a_new_finished_item_does(self):
        self.run_cmd(self.r(3))
        head = self.head()
        self.assertEqual(self.run_cmd(self.r(3, usd=130.0, tl=6500.0)), "no change")        # dollar drift inside one step
        self.assertEqual(self.head(), head)
        self.assertEqual(self.run_cmd(self.r(4)), "pushed")
        self.assertIn("finished 3→4", self.message())
        self.assertEqual(self.json()["done"], 4)

    def test_unknown_finished_count_keeps_the_published_one_of_the_same_week(self):
        self.run_cmd(self.r(3))
        self.assertEqual(self.run_cmd(self.r(None, usd=260.0)), "pushed")                        # dollars crossed a step
        self.assertEqual(self.json()["done"], 3)

    def test_run_without_summary_behaves_as_before(self):
        r = D.line(fixture=dict(d7=100, quota=40))
        self.assertEqual(self.run_cmd(r), "pushed")
        self.assertNotIn("reports/usage/ali.week.json", self.files())
        self.assertNotIn("Finished work", self.sh("git", "--git-dir", self.remote, "show", "main:reports/usage/RANKING.md"))


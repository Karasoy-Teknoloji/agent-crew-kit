"""Pace target: hours cap (max_target_hours, ceiling 96) and goal percent (target_percent) for the modes, the forecast and the hook.
Friday 09:00 with a 90% goal = target_hours 60 + target_percent 90 in ~/.claude/pace-plan.json (reset Sunday 21:00).
Run: python3 -I -m unittest discover -s plugins/quota-pace/tests -p 'test_pace_target_percent.py'"""
import json, os, shutil, subprocess, sys, tempfile, time, unittest
from unittest import mock

PLUG = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(PLUG, "bin"))
import pace_forecast as PF  # noqa: E402


def epoch(y, mo, d, h=0, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


class Hour(unittest.TestCase):
    def setUp(self):
        for k in ("PACE_TARGET_HOURS", "PACE_TARGET_PERCENT"):
            p = mock.patch.dict(os.environ)
            p.start()
            self.addCleanup(p.stop)
            os.environ.pop(k, None)

    def test_friday_morning_is_allowed_by_default(self):
        self.assertEqual(PF.target_hours_setting({"target_hours": 60}), 60.0)           # was capped to 48

    def test_default_cap_is_72_and_the_old_default_target_is_unchanged(self):
        self.assertEqual(PF.target_hours_setting({"target_hours": 200}), 72.0)
        self.assertEqual(PF.target_hours_setting({}), PF.DEFAULT_TARGET_HOURS)
        self.assertEqual(PF.target_hours_setting({"target_hours": -3}), 0.0)

    def test_max_target_hours_moves_the_cap_but_never_past_96(self):
        self.assertEqual(PF.target_hours_setting({"target_hours": 90, "max_target_hours": 96}), 90.0)
        self.assertEqual(PF.target_hours_setting({"target_hours": 500, "max_target_hours": 500}), 96.0)   # hard ceiling
        self.assertEqual(PF.target_hours_setting({"target_hours": 60, "max_target_hours": 24}), 24.0)     # lowering works too
        self.assertEqual(PF.target_hours_setting({"target_hours": 60, "max_target_hours": "x"}), 60.0)   # junk = the default cap

    def test_env_variable_is_capped_like_the_plan_value(self):
        os.environ["PACE_TARGET_HOURS"] = "80"
        self.assertEqual(PF.target_hours_setting({}), 72.0)
        self.assertEqual(PF.target_hours_setting({"max_target_hours": 96}), 80.0)


class Percent(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("PACE_TARGET_PERCENT", None)

    def test_default_is_100_and_not_explicit(self):
        self.assertEqual(PF.target_percent_setting({}), (100.0, False))
        self.assertEqual(PF.target_percent_setting({"target": 90}), (100.0, False))      # the legacy key does not move the modes

    def test_value_clamped_and_junk_ignored(self):
        self.assertEqual(PF.target_percent_setting({"target_percent": 90}), (90.0, True))
        self.assertEqual(PF.target_percent_setting({"target_percent": 150}), (100.0, True))
        self.assertEqual(PF.target_percent_setting({"target_percent": 5}), (50.0, True))
        self.assertEqual(PF.target_percent_setting({"target_percent": "abc"}), (100.0, False))
        os.environ["PACE_TARGET_PERCENT"] = "85"
        self.assertEqual(PF.target_percent_setting({"target_percent": 90}), (85.0, True))


class Mod(unittest.TestCase):
    def test_goal_100_keeps_the_old_decisions_and_reasons(self):
        self.assertEqual(PF.decide_mode(None, 101.0, 105.0), ("TIGHT", "p50 101.0>100 or p90 105.0>110"))
        self.assertEqual(PF.decide_mode(None, 75.0, 85.0), ("PLENTY", "p50 75.0<80 and p90 85.0<100"))
        self.assertEqual(PF.decide_mode(None, 91.0, 95.0)[0], "NORMAL")
        self.assertEqual(PF.decide_mode("PLENTY", 84.0, 100.0, use_p90=False)[1], "PLENTY held: p50 84.0<85 (median only this early)")
        self.assertEqual(PF.decide_mode("TIGHT", 99.0, 104.0)[1], "TIGHT held: p50 99.0>95 or p90 104.0>105")

    def test_goal_90_shifts_every_percent_threshold_by_0_9(self):
        g = dict(goal=90.0)
        self.assertEqual(PF.decide_mode(None, 91.0, 95.0, **g), ("TIGHT", "p50 91.0>90 or p90 95.0>99"))         # 100 -> 90
        self.assertEqual(PF.decide_mode(None, 85.0, 100.0, **g)[0], "TIGHT")                                      # p90 > 99
        self.assertEqual(PF.decide_mode(None, 71.0, 85.0, **g), ("PLENTY", "p50 71.0<72 and p90 85.0<90"))         # 80 -> 72, 100 -> 90
        self.assertEqual(PF.decide_mode(None, 75.0, 85.0, **g)[0], "NORMAL")                                     # PLENTY under goal 100, not under 90
        self.assertEqual(PF.decide_mode("PLENTY", 76.0, 92.0, **g)[1], "PLENTY held: p50 76.0<76.5, p90 92.0<94.5")   # 85 -> 76.5, 105 -> 94.5
        self.assertEqual(PF.decide_mode("TIGHT", 86.0, 92.0, **g)[1], "TIGHT held: p50 86.0>85.5 or p90 92.0>94.5")  # 95 -> 85.5
        self.assertEqual(PF.decide_mode(None, 95.0, 99.0, use_p90=False, **g), ("TIGHT", "p50 95.0>94.5 (median only this early)"))
        self.assertEqual(PF.decide_mode(None, 71.0, 99.0, use_p90=False, **g)[0], "PLENTY")

    def test_decide_mode_has_no_5h_input(self):
        import inspect
        self.assertEqual(list(inspect.signature(PF.decide_mode).parameters), ["prev_mode", "p50", "p90", "use_p90", "goal"])


class Forecast(unittest.TestCase):
    """compute_forecast against a scratch ~/.claude: the Friday-09:00 / 90% plan of this machine."""
    def setUp(self):
        # computed per test, not at import: other test modules switch the process time zone (COST_TZ) while the suite runs
        self.RESET = epoch(2026, 11, 8, 21)            # Sunday 21:00 local
        self.FRIDAY = epoch(2026, 11, 6, 9)            # Friday 09:00 local
        self.d = tempfile.mkdtemp(prefix="pace-percent-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        p = mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        for k in ("PACE_TARGET_HOURS", "PACE_TARGET_PERCENT"):
            os.environ.pop(k, None)

    def forecast(self, plan, week_used, now):
        PF._write_pace(self.d, week_used=week_used, five_hour_used=10.0, week_resets_at=self.RESET, now=now)
        with open(os.path.join(self.d, "pace-plan.json"), "w") as f:
            json.dump(plan, f)
        return PF.compute_forecast(self.d, now=now)

    def test_friday_0900_target_and_90_goal_reach_the_output(self):
        r = self.forecast({"target": 90, "target_hours": 60, "target_percent": 90}, 60.0, epoch(2026, 11, 5, 9))
        self.assertEqual(r["target_at"], int(self.FRIDAY))
        self.assertEqual((r["target_hours"], r["target_percent"], r["target"]), (60.0, 90.0, 90.0))

    def test_without_the_new_keys_nothing_changes(self):
        r = self.forecast({"target": 90, "target_hours": 5}, 60.0, epoch(2026, 11, 5, 9))
        self.assertEqual((r["target_hours"], r["target_percent"], r["target"]), (5.0, 100.0, 90.0))   # legacy target stays the need_rate goal
        self.assertEqual(r["target_at"], int(self.RESET - 5 * 3600))

    def test_goal_decides_the_mode(self):
        now = epoch(2026, 11, 6, 6)                       # 3 h before Friday 09:00, 95% used: at least 95% at the target
        a = self.forecast({"target_hours": 60}, 95.0, now)
        b = self.forecast({"target_hours": 60, "target_percent": 90}, 95.0, now)
        self.assertGreaterEqual(b["p50"], 95.0)
        self.assertEqual(b["mode"], "TIGHT")
        self.assertIn(">90 or", b["mode_reason"])          # the reason names the scaled threshold
        self.assertNotIn(">90 or", a["mode_reason"])

    def test_full_5h_window_does_not_make_the_mode_tight(self):
        # weekly forecast on track (p50 ~54 in the field), 5h window at 86%: weekly forecast alone decides
        PF._write_pace(self.d, week_used=50.0, five_hour_used=86.0, week_resets_at=int(time.time()) + 2 * 86400, now=time.time())
        r = PF.compute_forecast(self.d, now=time.time())
        self.assertLess(r["p50"], 80.0)
        self.assertNotEqual(r["mode"], "TIGHT")
        self.assertNotIn("5h", r["mode_reason"])
        self.assertEqual(r["five_hour_used"], 86.0)        # still there as information
        self.assertNotIn("five_hour_warning", r)              # no behaviour-changing 5h flag

    def test_weekly_p50_above_the_threshold_work_tight_whatever_the_5h_window_says(self):
        for five in (None, 0.0, 86.0):
            PF._write_pace(self.d, week_used=95.0, five_hour_used=five, week_resets_at=int(time.time()) + 2 * 86400, now=time.time())
            r = PF.compute_forecast(self.d, now=time.time())
            self.assertGreater(r["p50"], 100.0)
            self.assertEqual(r["mode"], "TIGHT")

    def test_decide_mode_weekly_thresholds_and_hysteresis(self):
        self.assertEqual(PF.decide_mode(None, 54.2, 78.4)[0], "PLENTY")
        self.assertEqual(PF.decide_mode(None, 101.0, 105.0)[0], "TIGHT")
        self.assertEqual(PF.decide_mode("TIGHT", 54.2, 78.4)[0], "PLENTY")          # nothing holds TIGHT any more but the weekly forecast
        self.assertEqual(PF.decide_mode("TIGHT", 96.0, 100.0)[0], "TIGHT")        # weekly hysteresis kept

    def test_past_friday_morning_unused_quota_is_spent(self):
        r = self.forecast({"target_hours": 60, "target_percent": 90}, 70.0, epoch(2026, 11, 6, 10))
        self.assertEqual(r["mode"], "PLENTY")
        self.assertIn("past the target (reset -60h)", r["mode_reason"])

    def test_need_rate_aims_at_the_goal_when_set_else_at_the_legacy_target(self):
        now = epoch(2026, 11, 5, 9)
        a = self.forecast({"target": 90, "target_hours": 60}, 50.0, now)
        b = self.forecast({"target": 90, "target_hours": 60, "target_percent": 80}, 50.0, now)
        self.assertAlmostEqual(a["need_rate"] * a["active_hours_left"], 40.0, delta=0.1)   # (90 - 50)
        self.assertAlmostEqual(b["need_rate"] * b["active_hours_left"], 30.0, delta=0.1)   # (80 - 50)


class Hook(unittest.TestCase):
    """pace-hook.py: the goal shows in the forecast line; the 95% hand-over stays absolute (independent of the goal)."""

    def run_hook(self, **pace):
        home = tempfile.mkdtemp(prefix="pace-hook-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        os.makedirs(os.path.join(home, ".claude"))
        now = time.time()
        p = {"week_used": 50, "week_resets_at": now + 3 * 86400, "updated": now, "mode": "NORMAL", "p10": 80, "p50": 88, "p90": 95,
             "rate": 1.0, "active_hours_left": 20, "five_hour_used": 10, "target_at": now + 2 * 86400, "target_hours": 24,
             "target_percent": 100}
        p.update(pace)
        with open(os.path.join(home, ".claude", "pace.json"), "w") as f:
            json.dump(p, f)
        env = dict(os.environ, HOME=home, USERPROFILE=home)
        return subprocess.run([sys.executable, "-I", os.path.join(PLUG, "bin", "pace-hook.py")], input='{"session_id":"t","hook_event_name":"SessionStart"}',
                              text=True, env=env, capture_output=True, timeout=60, encoding="utf-8").stdout

    def test_goal_is_shown_only_below_100(self):
        self.assertIn(", goal 90%)", self.run_hook(target_percent=90))
        self.assertNotIn("goal", self.run_hook(target_percent=100))
        self.assertNotIn("goal", self.run_hook())             # a pace.json written before the key existed
        self.assertNotIn("goal", self.run_hook(target_percent=None))

    def test_5h_window_is_shown_with_its_reset_and_restricts_nothing(self):
        o = self.run_hook(five_hour_used=86, five_hour_resets_at=time.time() + 3600)
        self.assertIn("5-hour window 86% used, resets ", o)
        self.assertIn("restricts no work", o)
        self.assertNotIn("nearly full", self.run_hook(target_at=time.time() - 3600, five_hour_used=86))
        self.assertNotIn("5-hour", self.run_hook(five_hour_used=None))

    def test_handover_at_95_is_absolute_with_a_90_goal(self):
        o92 = self.run_hook(week_used=92, target_percent=90)
        self.assertIn("[Quota >= 90%]", o92)
        self.assertNotIn("Hand over all work", o92)         # past the goal, still under the 95% buffer: keep working
        o96 = self.run_hook(week_used=96, target_percent=90)
        self.assertIn("Hand over all work", o96)
        past = self.run_hook(week_used=96, target_percent=90, target_at=time.time() - 3600)
        self.assertNotIn("Hand over all work", past)        # inside the target window the rule is off (spend what is left)


class Status(unittest.TestCase):
    """The goal travels: forecast -> statusline.sh -> pace.json; and last-window reads the same target hours and cap."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="pace-percent-home-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        os.makedirs(os.path.join(self.home, ".claude"))

    @unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq required")
    def test_statusline_passes_target_percent_into_pace_json(self):
        now = int(time.time())
        reset = now + 3 * 86400
        fc = {"week_resets_at": reset, "updated": now, "p10": 80.0, "p50": 88.0, "p90": 95.0, "rate": 0.8, "active_hours_left": 5.0,
              "mode_reason": "t", "target_at": reset - 60 * 3600, "target_hours": 60.0, "target_percent": 90.0, "mode": "NORMAL"}
        with open(os.path.join(self.home, ".claude", "pace-forecast.json"), "w") as f:
            json.dump(fc, f)
        data = json.dumps({"model": {"display_name": "X", "id": "x"}, "workspace": {"current_dir": "/"}, "rate_limits": {
            "five_hour": {"used_percentage": 10, "resets_at": now + 3600}, "seven_day": {"used_percentage": 50, "resets_at": reset}}})
        subprocess.run(["bash", os.path.join(PLUG, "statusline.sh")], input=data, text=True, env=dict(os.environ, HOME=self.home, USERPROFILE=self.home),
                       capture_output=True, timeout=60)
        with open(os.path.join(self.home, ".claude", "pace.json"), encoding="utf-8") as f:
            p = json.load(f)
        self.assertEqual((p["target_hours"], p["target_percent"]), (60.0, 90.0))

    def target_hour(self, plan):
        with open(os.path.join(self.home, ".claude", "pace-plan.json"), "w") as f:
            json.dump(plan, f)
        env = {k: v for k, v in os.environ.items() if k not in ("PACE_TARGET_HOURS", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "LAST_WINDOW_DIR")}
        env.update(HOME=self.home, USERPROFILE=self.home)
        code = "import sys; sys.path.insert(0, %r); import last_window as S; print(S.target_hour({}))" % os.path.join(PLUG, "bin")
        return float(subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True, timeout=60).stdout.strip())

    def test_last_window_follows_the_same_cap(self):
        self.assertEqual(self.target_hour({"target_hours": 60}), 60.0)
        self.assertEqual(self.target_hour({"target_hours": 90}), 72.0)
        self.assertEqual(self.target_hour({"target_hours": 90, "max_target_hours": 96}), 90.0)
        self.assertEqual(self.target_hour({"target_hours": 120, "max_target_hours": 200}), 96.0)


if __name__ == "__main__":
    unittest.main()

"""A weekly limit without a real reset time must not change the pace mode (statusline.sh + pace-hook.py).
Payload A: overall limit with a valid reset.  Payload B: the bogus shape seen in practice (resets_at 4102444800 = year 2100, 20%/10%),
plus a model-specific weekly limit next to the overall one. Run: python3 -I -m unittest discover -s plugins/quota-pace/tests"""
import json, os, shutil, subprocess, tempfile, time, unittest

PLUG = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
HAVE = shutil.which("bash") and shutil.which("jq")


def payload(now, seven, five, extra=None):
    rl = {"seven_day": seven}
    if five is not None:  # five=None: Claude Code reports no 5-hour window
        rl["five_hour"] = five
    rl.update(extra or {})
    return json.dumps({"model": {"display_name": "X", "id": "x"}, "workspace": {"current_dir": "/"}, "rate_limits": rl})


@unittest.skipUnless(HAVE, "bash and jq required")
class InvalidReset(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="pace-test-")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.pj = os.path.join(self.home, ".claude", "pace.json")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def status(self, data):
        env = dict(os.environ, HOME=self.home)
        return subprocess.run(["bash", os.path.join(PLUG, "statusline.sh")], input=data, text=True, env=env, capture_output=True, timeout=60).stdout

    def hook(self):
        env = dict(os.environ, HOME=self.home)
        return subprocess.run(["python3", "-I", os.path.join(PLUG, "bin", "pace-hook.py")], input='{"session_id":"t","hook_event_name":"SessionStart"}',
                              text=True, env=env, capture_output=True, timeout=60).stdout

    def test_valid_reset_writes_mode(self):
        now = int(time.time())
        self.status(payload(now, {"used_percentage": 36, "resets_at": now + 3 * 86400}, {"used_percentage": 15, "resets_at": now + 3600}))
        p = json.load(open(self.pj))
        self.assertEqual(p["week_used"], 36)
        self.assertIn("[Quota pace:", self.hook())

    def test_placeholder_reset_is_unknown(self):
        now = int(time.time())
        good = payload(now, {"used_percentage": 36, "resets_at": now + 3 * 86400}, {"used_percentage": 15, "resets_at": now + 3600})
        self.status(good)
        before = open(self.pj).read()
        bad = payload(now, {"used_percentage": 20, "resets_at": 4102444800}, {"used_percentage": 10, "resets_at": 4102444800})
        self.status(bad)
        self.assertEqual(open(self.pj).read(), before)          # not overwritten
        for seven in ({"used_percentage": 20}, {"used_percentage": 20, "resets_at": 0}):
            self.status(payload(now, seven, {"used_percentage": 10}))
            self.assertEqual(open(self.pj).read(), before)

    def test_model_specific_limit_ignored(self):
        now = int(time.time())
        self.status(payload(now, {"used_percentage": 36, "resets_at": now + 3 * 86400}, {"used_percentage": 15, "resets_at": now + 3600},
                            {"seven_day_opus": {"used_percentage": 90, "resets_at": 4102444800}}))
        self.assertEqual(json.load(open(self.pj))["week_used"], 36)

    def test_no_five_hour_window_is_not_zero(self):
        """rate_limits with seven_day only: the 5-hour window is unknown (null), not 0% - no 5h segment, no 5-hour sentence, no 5h mode rule."""
        now = int(time.time())
        seven = {"used_percentage": 36, "resets_at": now + 3 * 86400}
        line = self.status(payload(now, seven, None))
        p = json.load(open(self.pj))
        self.assertIsNone(p["five_hour_used"])
        self.assertEqual(p["five_hour_resets_at"], 0)
        self.assertNotIn("5h ", line)
        self.assertRegex(line, r"\| week 36%")
        out = self.hook()
        self.assertIn("[Quota pace:", out)
        self.assertNotIn("5-hour", out)
        # history keeps the weekly row, the 5-hour column is empty (not the string "null")
        row = open(os.path.join(self.home, ".claude", "pace-history.csv")).read().strip().splitlines()[-1].split(",")
        self.assertEqual(row[2], "")
        self.assertEqual(row[1], "36")

    def test_five_hour_window_shows_again_when_reported(self):
        now = int(time.time())
        seven = {"used_percentage": 36, "resets_at": now + 3 * 86400}
        self.status(payload(now, seven, None))
        line = self.status(payload(now, seven, {"used_percentage": 15, "resets_at": now + 3600}))
        self.assertIn("5h 15%", line)
        self.assertEqual(json.load(open(self.pj))["five_hour_used"], 15)
        self.assertIn("5-hour window 15% used", self.hook())

    def test_five_hour_window_never_makes_tight(self):
        now = int(time.time())
        seven = {"used_percentage": 36, "resets_at": now + 3 * 86400}
        self.status(payload(now, seven, {"used_percentage": 85, "resets_at": now + 3600}))
        p = json.load(open(self.pj))
        self.assertNotEqual(p["mode"], "TIGHT")             # the mode is weekly only
        self.assertEqual(p["five_hour_used"], 85)          # the 5-hour window is still recorded and shown
        os.remove(self.pj)
        self.status(payload(now, seven, None))
        self.assertNotEqual(json.load(open(self.pj))["mode"], "TIGHT")

    def _seed(self, week_used, week_resets_at, five_resets_at=0, five_used=None):
        now = int(time.time())
        json.dump({"week_used": week_used, "week_elapsed": 99, "five_hour_used": five_used, "week_resets_at": week_resets_at,
                   "five_hour_resets_at": five_resets_at, "updated": now - 30, "week_projected": 100, "mode": "NORMAL", "logged_at": now - 30},
                  open(self.pj, "w"))

    def test_weekly_reset_without_five_hour_window(self):
        """No five_hour in the input: a new week (later seven_day.resets_at, lower used_percentage) replaces the stored value;
        the stale-high previous week must not be carried over."""
        now = int(time.time())
        self._seed(90, now - 60)  # last reading belongs to the week that has just ended
        self.status(payload(now, {"used_percentage": 3, "resets_at": now + 7 * 86400}, None))
        p = json.load(open(self.pj))
        self.assertEqual(p["week_used"], 3)
        self.assertEqual(p["week_resets_at"], now + 7 * 86400)
        self.assertIsNone(p["five_hour_used"])
        # a reset a little earlier than the stored one but still ahead: also a different week -> taken as reported
        self._seed(90, now + 6 * 86400)
        self.status(payload(now, {"used_percentage": 3, "resets_at": now + 7 * 86400}, None))
        self.assertEqual(json.load(open(self.pj))["week_used"], 3)

    def test_same_week_keeps_highest_value_without_five_hour_window(self):
        """Same seven_day.resets_at, lower value (an idle session with an old reading): the higher stored value stays."""
        now = int(time.time())
        r7 = now + 3 * 86400
        self._seed(50, r7)
        self.status(payload(now, {"used_percentage": 40, "resets_at": r7}, None))
        self.assertEqual(json.load(open(self.pj))["week_used"], 50)

    def test_manual_reset_taken_with_five_hour_window(self):
        """Old behaviour kept: a newer 5-hour window with a lower weekly value (manual limit reset, same weekly resets_at) is current."""
        now = int(time.time())
        r7 = now + 3 * 86400
        self._seed(50, r7, five_resets_at=now + 600, five_used=20)
        self.status(payload(now, {"used_percentage": 5, "resets_at": r7}, {"used_percentage": 1, "resets_at": now + 3600}))
        self.assertEqual(json.load(open(self.pj))["week_used"], 5)

    def test_hook_silent_on_bogus_pace_json(self):
        now = int(time.time())
        json.dump({"week_used": 20, "week_elapsed": 5, "five_hour_used": 10, "week_resets_at": 4102444800, "five_hour_resets_at": 4102444800,
                   "updated": now, "week_projected": 400, "mode": "TIGHT"}, open(self.pj, "w"))
        self.assertEqual(self.hook(), "")


if __name__ == "__main__":
    unittest.main()

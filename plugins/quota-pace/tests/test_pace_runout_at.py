"""statusline.sh must pass the forecast's runout_at into pace.json: cost_share.end_at_extra() reads it for the forecast run-out column
(without it the column stays "—" for everyone). A null runout_at (no run-out before the reset) must stay null, not vanish.
Run: python3 -I -m unittest discover -s plugins/quota-pace/tests -p 'test_pace_runout_at.py'"""
import json, os, shutil, subprocess, tempfile, time, unittest
from bash_bul import BASH  # Git Bash on Windows, never the WSL launcher in System32

PLUG = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
HAVE = BASH and shutil.which("jq")


@unittest.skipUnless(HAVE, "bash and jq required")
class RunoutAt(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="runout-test-")
        self.claude = os.path.join(self.home, ".claude")
        os.makedirs(self.claude)
        self.now = int(time.time())
        self.reset = self.now + 3 * 86400

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_status(self, runout_at, runout_label):
        fc = {"week_resets_at": self.reset, "updated": self.now, "p10": 100.0, "p50": 104.0, "p90": 108.0, "rate": 0.8,
              "active_hours_left": 5.0, "mode_reason": "test", "target_at": self.reset - 5 * 3600, "target_hours": 5.0,
              "runout_at": runout_at, "runout_label": runout_label, "idle_label": "15m" if runout_label else None, "mode": "TIGHT"}
        with open(os.path.join(self.claude, "pace-forecast.json"), "w", encoding="utf-8") as f:
            json.dump(fc, f)
        data = json.dumps({"model": {"display_name": "X", "id": "x"}, "workspace": {"current_dir": "/"}, "rate_limits": {
            "five_hour": {"used_percentage": 10, "resets_at": self.now + 3 * 3600},
            "seven_day": {"used_percentage": 94, "resets_at": self.reset}}})
        env = dict(os.environ, HOME=self.home, USERPROFILE=self.home)
        subprocess.run([BASH, os.path.join(PLUG, "statusline.sh")], input=data, text=True, env=env, capture_output=True, timeout=60)
        with open(os.path.join(self.claude, "pace.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_runout_at_passed_through(self):
        p = self.run_status(self.now + 7200, "20:45")
        self.assertIn("runout_at", p)
        self.assertEqual(p["runout_at"], self.now + 7200)

    def test_null_runout_at_kept(self):
        p = self.run_status(None, None)
        self.assertIn("runout_at", p)
        self.assertIsNone(p["runout_at"])


if __name__ == "__main__":
    unittest.main()

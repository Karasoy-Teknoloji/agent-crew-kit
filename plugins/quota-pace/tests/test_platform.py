"""Cross-platform behaviour of the status line tools (Linux, macOS, Windows). Run: python3 -I -m unittest discover -s plugins/quota-pace/tests"""
import json, os, re, shutil, subprocess, sys, tempfile, time, unittest

PLUG = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PY = sys.executable
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class FixedDayNames(unittest.TestCase):
    """The day name must never come from the locale (%a gives "Mon" on macOS/Windows)."""

    def test_no_locale_day_format_in_status_line_code(self):
        for name in ("bin/pace-hook.py", "bin/pace_forecast.py", "statusline.sh", "statusline.ps1", "bin/statusline-local.ps1"):
            with open(os.path.join(PLUG, name), encoding="utf-8-sig") as f:
                code = f.read()
            self.assertIsNone(re.search(r"strftime\([^)]*%[aA]|date\s+[^\n]*%[aA]|-Format\s+'[^']*ddd", code), name)

    def test_hook_message_uses_the_fixed_day_names(self):
        home = tempfile.mkdtemp(prefix="day-test-")
        try:
            os.makedirs(os.path.join(home, ".claude"))
            now = int(time.time())
            reset = now + 3 * 86400
            pace = {"mode": "NORMAL", "week_used": 40, "five_hour_used": 10, "week_resets_at": reset, "five_hour_resets_at": now + 3600,
                    "updated": now, "week_projected": 70, "p10": 60, "p50": 70, "p90": 80, "rate": 1.5, "active_hours_left": 20, "target_at": reset - 5 * 3600, "target_hours": 5}
            with open(os.path.join(home, ".claude", "pace.json"), "w", encoding="utf-8") as f:
                json.dump(pace, f)
            env = dict(os.environ, HOME=home, USERPROFILE=home, LC_ALL="C", LANG="C")
            r = subprocess.run([PY, "-I", os.path.join(PLUG, "bin", "pace-hook.py")], input=b'{"session_id":"t","hook_event_name":"SessionStart"}',
                               capture_output=True, env=env, timeout=60)
            out = r.stdout.decode("utf-8")
            exp = DAYS[time.localtime(reset - 5 * 3600).tm_wday]   # the forecast target is 5 h before the reset
            self.assertRegex(out, r"\((%s) \d\d\.\d\d \d\d:\d\d, reset -5h\)" % "|".join(DAYS))
            self.assertIn("(%s " % exp, out)
        finally:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

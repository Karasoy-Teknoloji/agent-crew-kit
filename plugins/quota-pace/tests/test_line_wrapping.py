"""Width-based wrapping of the Windows status line (statusline-local.ps1): one line when everything fits, otherwise a break at segment
boundaries (" | ") only. Run with the other plugin tests:  python3 -I -m unittest discover -s plugins/quota-pace/tests

The wrapper is run with a stub status line script (-StatusLine) and no personal extension (-Ek), so the text is fully controlled;
the width comes from COLUMNS / QUOTA_LINE_WIDTH exactly as Claude Code supplies it. One test runs the real statusline.ps1
under a scratch USERPROFILE to prove the one-line output is byte-identical to the unwrapped script."""
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUG = os.path.normpath(os.path.join(HERE, ".."))
WRAP = os.path.join(PLUG, "bin", "statusline-local.ps1")
PWSH = shutil.which("pwsh") or shutil.which("powershell")

SEGS = ["Opus 4.7 · high", "%12 (120K/1M)", "01:20", "5sa %30→%55 (3sa10dk) · hafta %40→%70±5 \U0001F7E2",
        "[main] ~/Kaynak/example-org/claude-shared", "önb %93 (2 kaçış)", "oturum $1.23", "son 24sa $10.00 · 7g $50.00 · 30g $200.00"]
LINE = " | ".join(SEGS)
ESC = chr(27)
DROP = ("COLUMNS", "QUOTA_LINE_WIDTH", "QUOTA_LINE_PAY")


def cells(s):
    """Reference cell width for the test strings: emoji above U+1F300 take 2 cells."""
    return sum(2 if ord(c) >= 0x1F300 else 1 for c in s)


def pieces(text):
    """The unbreakable pieces of a status line: ' | ' segments and, inside them, ' · ' parts."""
    return re.split(r" \| | · ", text)


def assert_pieces(tc, lines, original=LINE):
    """Nothing lost, order kept, no piece cut, no row starts or ends with a separator."""
    tc.assertEqual([p for l in lines for p in pieces(l)], pieces(original))
    for l in lines:
        tc.assertFalse(l.startswith(("|", "·", " ")) or l.endswith(("|", "·", " ")), l)


@unittest.skipUnless(PWSH, "PowerShell only")
class Wrapping(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wrapping-test-")
        self.stub = os.path.join(self.root, "stub.ps1")
        self.nul = os.path.join(self.root, "yok-ek.ps1")        # no personal extension

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def render(self, text, ek=None, **env):
        with open(self.stub, "w", encoding="utf-8-sig") as f:
            f.write("[Console]::OutputEncoding = [Text.Encoding]::UTF8\n@'\n%s\n'@\n" % text)
        e = {k: v for k, v in os.environ.items() if k not in DROP}
        e.update(env)
        r = subprocess.run([PWSH, "-NoProfile", "-File", WRAP, "-ClaudeDir", self.root, "-StatusLine", self.stub, "-Ek", ek or self.nul],
                           input="", capture_output=True, text=True, encoding="utf-8", env=e, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout.rstrip("\r\n").replace("\r\n", "\n").split("\n")

    def assert_lossless(self, lines, original=LINE):
        assert_pieces(self, lines, original)

    def test_fits_is_one_unchanged_line(self):
        self.assertEqual(self.render(LINE, COLUMNS="300"), [LINE])

    def test_does_not_fit_makes_two_lines_within_width(self):
        out = self.render(LINE, COLUMNS="204")                           # width Claude Code reported on a developer's machine; the line is 216 cells
        self.assertEqual(len(out), 2, out)
        self.assert_lossless(out)
        self.assertTrue(all(cells(l) <= 204 - 4 for l in out), out)
        self.assertTrue(out[1].endswith("30g $200.00"), out)             # cost segments stay visible, whole

    def test_three_lines_when_two_are_not_enough_never_truncates(self):
        out = self.render(LINE, COLUMNS="70")
        self.assertGreaterEqual(len(out), 3, out)
        self.assert_lossless(out)
        self.assertTrue(all(cells(l) <= 66 for l in out), out)

    def test_extreme_narrow_keeps_every_segment_whole(self):
        out = self.render(LINE, COLUMNS="25")                            # a long segment may exceed the width alone, but is never cut
        self.assert_lossless(out)
        self.assertGreater(len(out), 4)

    def test_ansi_sequences_are_not_counted(self):
        parts = ["a" * 30, "b" * 30, "c" * 30]
        plain = " | ".join(parts)                                        # 96 cells
        colored = " | ".join("%s[31;1m%s%s[0m" % (ESC, s, ESC) for s in parts)   # 96 visible, much more raw
        self.assertEqual(self.render(colored, COLUMNS="100"), [colored])
        self.assertEqual(self.render(plain, COLUMNS="100"), [plain])
        self.assertEqual(len(self.render(plain, COLUMNS="99")), 2)      # 96 > 95 (99 - 4): the same boundary, colored or not
        self.assertEqual(len(self.render(colored, COLUMNS="99")), 2)

    def test_default_margin_is_the_measured_4_and_settings_padding_adds_two_per_unit(self):
        # Measured on the real Claude Code screen (test_real_screen.py): a status line row shows at most width - 4 cells; padding counts on both sides
        line = " | ".join(["a" * 30] * 3)                                 # 96 cells
        self.assertEqual(len(self.render(line, COLUMNS="100")), 1)
        self.assertEqual(len(self.render(line, COLUMNS="99")), 2)
        with open(os.path.join(self.root, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"statusLine": {"type": "command", "command": "x", "padding": 2}}, f)
        self.assertEqual(len(self.render(line, COLUMNS="104")), 1)          # margin 4 + 2 x 2 = 8
        self.assertEqual(len(self.render(line, COLUMNS="103")), 2)
        self.assertEqual(len(self.render(line, COLUMNS="100", QUOTA_LINE_PAY="4")), 1)   # an explicit QUOTA_LINE_PAY wins over the padding

    def test_emoji_counts_as_two_cells(self):
        fits = "a" * 35 + " | \U0001F7E2"                                # 35 + 3 + 2 = 40 cells (len() says 39)
        over = "a" * 36 + " | \U0001F7E2"                                # 41 cells (len() says 40)
        env = dict(QUOTA_LINE_WIDTH="40", QUOTA_LINE_PAY="0")
        self.assertEqual(self.render(fits, **env), [fits])
        self.assertEqual(self.render(over, **env), ["a" * 36, "\U0001F7E2"])

    def test_width_unknown_falls_back_to_120_and_override_wins(self):
        self.assertEqual(len(self.render(LINE)), 3)                      # no COLUMNS: 120 - 4
        self.assertEqual(len(self.render(LINE, COLUMNS="abc")), 3)       # garbage ignored
        self.assertEqual(len(self.render(LINE, COLUMNS="0")), 3)
        self.assertEqual(self.render(LINE, QUOTA_LINE_WIDTH="300"), [LINE])
        self.assertEqual(len(self.render(LINE, COLUMNS="300", QUOTA_LINE_WIDTH="120")), 3)   # explicit setting beats COLUMNS
        self.assertEqual(self.render(LINE, COLUMNS="226", QUOTA_LINE_PAY="0"), [LINE])          # margin is configurable

    def test_selim_two_line_example_still_works_and_extension_output_is_wrapped_too(self):
        ek = os.path.join(PLUG, "example", "statusline-extra-two-line.ps1")
        out = self.render(LINE, ek=ek, COLUMNS="300")                    # his static split is kept when asked for
        self.assertEqual(len(out), 2, out)
        self.assertTrue(out[1].startswith("[main]"), out)
        self.assert_lossless(out)
        out = self.render(LINE, ek=ek, COLUMNS="100")                    # and each of its lines still wraps if too long
        self.assert_lossless(out)
        self.assertGreater(len(out), 2)

    def test_real_statusline_one_line_is_byte_identical_to_unwrapped_script(self):
        home = os.path.join(self.root, "home")
        os.makedirs(os.path.join(home, ".claude", "cost"))
        with open(os.path.join(home, ".claude", "cost", "install.json"), "w") as f:     # fixed fresh rate: no network refresh may change the output between runs
            f.write(json.dumps({"install": 40.0, "date": "2026-01-01", "t": time.time() + 86400, "tried": time.time() + 86400}))
        payload = json.dumps({"model": {"id": "claude-x", "display_name": "X"}, "workspace": {"current_dir": home.replace("\\", "/")},
                              "prompt_cache": {"hit_ratio": 0.93, "misses": 2}, "cost": {"total_cost_usd": 1.2345}})
        env = {k: v for k, v in os.environ.items() if k not in DROP}
        env.update(HOME=home, USERPROFILE=home, COLUMNS="300")

        def run(cmd):
            return subprocess.run(cmd, input=payload, capture_output=True, text=True, encoding="utf-8", env=env, timeout=120).stdout.strip("\r\n")
        sl = os.path.join(PLUG, "statusline.ps1")
        wrap = [PWSH, "-NoProfile", "-File", WRAP, "-ClaudeDir", os.path.join(home, ".claude"), "-StatusLine", sl, "-Ek", self.nul]
        raw = run([PWSH, "-NoProfile", "-File", sl])
        self.assertTrue(raw)
        same = run(wrap)
        if same != raw:                                                  # the clock segment may have turned a minute between the runs
            raw = run([PWSH, "-NoProfile", "-File", sl])
            same = run(wrap)
        self.assertEqual(same, raw)                                      # fits: byte-identical, one line
        env["COLUMNS"] = "22"
        narrow = run(wrap).splitlines()
        raw = run([PWSH, "-NoProfile", "-File", sl])
        self.assertGreater(len(narrow), 1)
        self.assertEqual([p for l in narrow for p in pieces(l)], pieces(raw))


HELPER = os.path.join(PLUG, "bin", "line_wrap.py")


class WrappingPython(unittest.TestCase):
    """line_wrap.py (the wrap statusline.sh uses on Linux/macOS) follows the same rule as statusline-local.ps1."""

    def run_helper(self, text, **env):
        home = tempfile.mkdtemp(prefix="wrapping-py-")
        self.addCleanup(shutil.rmtree, home, True)
        e = {k: v for k, v in os.environ.items() if k not in DROP}
        e.update(env)
        r = subprocess.run([sys.executable, "-I", HELPER, home], input=text.encode("utf-8"), capture_output=True, env=e, timeout=60,
                           start_new_session=True)      # no controlling terminal: only the environment can give a width
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.decode("utf-8")

    def test_no_known_width_means_no_wrapping(self):
        self.assertEqual(self.run_helper(LINE), LINE)

    def test_fits_unchanged_and_narrow_wraps_at_segments(self):
        self.assertEqual(self.run_helper(LINE, COLUMNS="300"), LINE)
        out = self.run_helper(LINE, COLUMNS="204").split("\n")
        self.assertEqual(len(out), 2)
        assert_pieces(self, out)
        self.assertTrue(all(cells(l) <= 200 for l in out))
        self.assertTrue(out[1].endswith("30g $200.00"))

    def test_a_segment_that_does_not_fit_is_split_at_its_dots_and_filled_greedily(self):
        cost = "son 24sa A · 7g " + "x" * 20 + " $1 (B) · 30g C · ×61"
        line = "a" * 50 + " | " + "b" * 50 + " | " + cost
        out = self.run_helper(line, COLUMNS="140").split("\n")          # usable 136: row 1 takes the first cost pieces that fit
        assert_pieces(self, out, line)
        self.assertEqual(len(out), 2, out)
        self.assertTrue(out[0].endswith(" | son 24sa A"), out)
        self.assertTrue(out[1].startswith("7g "), out)
        self.assertTrue(all(cells(l) <= 136 for l in out), out)

    def test_manual_width_and_margin_override(self):
        parts = " | ".join(["a" * 30] * 3)                               # 96 cells
        self.assertEqual(len(self.run_helper(parts, COLUMNS="100").split("\n")), 1)
        self.assertEqual(len(self.run_helper(parts, COLUMNS="99").split("\n")), 2)
        self.assertEqual(len(self.run_helper(parts, COLUMNS="99", QUOTA_LINE_PAY="0").split("\n")), 1)
        self.assertEqual(len(self.run_helper(parts, COLUMNS="300", QUOTA_LINE_WIDTH="50").split("\n")), 3)

    @unittest.skipUnless(PWSH, "PowerShell only")
    def test_same_output_as_the_powershell_wrapper(self):
        for cols in ("204", "120", "70", "40", "25"):
            ps = Wrapping("render")
            ps.setUp()
            try:
                expected = ps.render(LINE, COLUMNS=cols)
            finally:
                ps.tearDown()
            self.assertEqual(self.run_helper(LINE, COLUMNS=cols).split("\n"), expected, cols)


if __name__ == "__main__":
    unittest.main()

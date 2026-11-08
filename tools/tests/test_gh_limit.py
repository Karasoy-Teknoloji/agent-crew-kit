"""Tests for tools/gh_limit.py: classification, limiter math, header parsing, and the retry / ETag paths against a fake gh."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "gh_limit.py"
sys.path.insert(0, str(TOOL.parent))
import gh_limit as g  # noqa: E402

FAKE_GH = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys
    sys.stdout.reconfigure(newline="\n")   # Windows text mode would turn LF into CRLF
    sys.stderr.reconfigure(newline="\n")
    d = os.environ["FAKE_DIR"]
    n_yol = os.path.join(d, "n")
    n = int(open(n_yol).read()) + 1 if os.path.exists(n_yol) else 1
    open(n_yol, "w").write(str(n))
    open(os.path.join(d, "argv%d" % n), "w").write(json.dumps(sys.argv[1:]))
    mod = os.environ.get("FAKE_MOD", "")
    inc = "--include" in sys.argv
    if mod == "secondary" and n == 1:
        if inc:
            sys.stdout.write("HTTP/2.0 403 Forbidden\r\nRetry-After: 1\r\nX-Ratelimit-Remaining: 4000\r\n\r\n")
        sys.stdout.write('{"message":"You have exceeded a secondary rate limit"}')
        sys.stderr.write("gh: You have exceeded a secondary rate limit (HTTP 403)\n")
        sys.exit(1)
    if mod == "etag" and any(a.startswith("If-None-Match") for a in sys.argv):
        sys.stdout.write("HTTP/2.0 304 Not Modified\r\nEtag: W/\"e1\"\r\nX-Ratelimit-Remaining: 4999\r\n\r\n")
        sys.stderr.write("gh: HTTP 304\n")
        sys.exit(1)
    if inc:
        sys.stdout.write("HTTP/2.0 200 OK\r\nEtag: W/\"e1\"\r\nX-Ratelimit-Remaining: 4998\r\nX-Ratelimit-Resource: core\r\n\r\n")
    sys.stdout.write("govde-%s\n" % sys.stdin.read() if "-" in sys.argv else "govde\n")
''').lstrip()


class ClassifyTest(unittest.TestCase):
    def test_kinds(self):
        c = lambda *a: g.classify(list(a))[0]
        self.assertEqual(c("auth", "git-credential", "get"), "transition")
        self.assertEqual(c("--version"), "transition")
        self.assertEqual(c("run", "watch", "--help"), "transition")
        self.assertEqual(c("api", "repos/o/r/actions/runs/5"), "okuma")
        self.assertEqual(c("api", "repos/o/r/issues/1/comments", "-f", "body=x"), "write")
        self.assertEqual(c("api", "-XPATCH", "repos/o/r/issues/1"), "write")
        self.assertEqual(c("api", "--method=DELETE", "repos/o/r/git/refs/heads/x"), "write")
        self.assertEqual(c("api", "-X", "GET", "search/issues", "-f", "q=x"), "search")
        self.assertEqual(c("api", "search/code?q=x"), "code_search")
        self.assertEqual(c("api", "graphql", "-f", "query=query { viewer { login } }"), "graphql")
        self.assertEqual(c("api", "graphql", "-f", "query=mutation { addComment(input:{}) { clientMutationId } }"), "write")
        self.assertEqual(c("issue", "comment", "5", "--body", "x"), "write")
        self.assertEqual(c("issue", "list", "-R", "o/r"), "okuma")
        self.assertEqual(c("run", "watch", "123"), "watch")
        self.assertEqual(c("pr", "checks", "4", "--watch"), "watch")
        self.assertEqual(c("search", "code", "foo"), "code_search")
        self.assertEqual(c("search", "issues", "foo"), "search")
        self.assertEqual(c("run", "list", "-L", "5"), "okuma")

    def test_watch_interval(self):
        self.assertEqual(g.watch_interval(["run", "watch", "5"]), ["run", "watch", "5", "--interval", str(g.WATCH_INTERVAL)])
        self.assertEqual(g.watch_interval(["run", "watch", "5", "-i", "60"]), ["run", "watch", "5", "-i", "60"])
        self.assertEqual(g.watch_interval(["pr", "checks", "--watch", "--interval=90"]), ["pr", "checks", "--watch", "--interval=90"])

    def test_uc_normal(self):
        self.assertEqual(g.uc_normal("/repos/o/r/actions/runs/123456/jobs?per_page=5"), "repos/o/r/actions/runs/:n/jobs")
        self.assertEqual(g.uc_normal("repos/o/r/commits/abcdef1234567"), "repos/o/r/commits/:sha")


class LimitTest(unittest.TestCase):
    def test_interval(self):
        self.assertEqual(g.reserved([], 100.0, 1.0, 60, 450), 100.0)
        self.assertEqual(g.reserved([100.0], 100.2, 1.0, 60, 450), 101.0)
        self.assertEqual(g.reserved([90.0], 100.0, 1.0, 60, 450), 100.0)

    def test_minutes_ceiling(self):
        z = [100.0 + i for i in range(60)]           # 60 starts in [100, 159]
        t = g.reserved(z, 159.5, 1.0, 60, 450)
        self.assertGreaterEqual(t, 160.0)
        self.assertLess(len([x for x in z if x > t - 60]), 60)

    def test_hour_ceiling(self):
        z = [i * 7.0 for i in range(450)]             # 450 starts in ~52 min
        t = g.reserved(z, z[-1] + 1, 1.0, 60, 450)
        self.assertEqual(t, 3600.0)                    # first start leaves the hour window

    def test_tempo(self):
        self.assertEqual(g.pace_interval(4000, 2000, 1000), 0.0)
        self.assertEqual(g.pace_interval(None, None, 1000), 0.0)
        self.assertAlmostEqual(g.pace_interval(1200, 2000, 1000), 1000 / 1000 * g.MACHINE)
        self.assertEqual(g.pace_interval(100, 4600, 1000), g.PACE_UPPER)
        self.assertEqual(g.pace_interval(100, 900, 1000), 0.0)          # window already reset
        self.assertEqual(g.source("okuma", ["pr", "view", "1"], None), "graphql")
        self.assertEqual(g.source("okuma", ["run", "view", "1"], None), "core")

    def test_waiting(self):
        self.assertEqual(g.waiting_duration(1, retry_after=30), (30.0, False))
        self.assertEqual(g.waiting_duration(1, retry_after=5000), (g.WAIT_UPPER, False))
        self.assertEqual(g.waiting_duration(1, remaining=0, reset_at=1100, now=1000), (101.0, True))
        self.assertEqual(g.waiting_duration(1)[0], 60.0)
        self.assertEqual(g.waiting_duration(2)[0], 120.0)
        self.assertEqual(g.waiting_duration(5)[0], g.WAIT_UPPER)

    def test_title_ve_limit(self):
        d, b, body = g.split_title(b"HTTP/2.0 403 Forbidden\r\nRetry-After: 7\r\n\r\n{\"m\":1}")
        self.assertEqual((d, b["retry-after"], body), (403, "7", b'{"m":1}'))
        self.assertEqual(g.split_title(b"plain"), (None, {}, b"plain"))
        self.assertTrue(g.limited_mi(d, b, 1, ""))
        self.assertFalse(g.limited_mi(403, {"x-ratelimit-remaining": "10"}, 1, "Resource not accessible by integration"))
        self.assertTrue(g.limited_mi(None, {}, 1, "gh: You have exceeded a secondary rate limit (HTTP 403)"))
        self.assertFalse(g.limited_mi(404, {}, 1, "Not Found"))


class FakeGhTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "s").mkdir()
        if os.name == "nt":     # no shebangs on Windows: a .cmd runs the fake gh script
            (self.tmp / "gh-real.py").write_text(FAKE_GH.split("\n", 1)[1])
            fake = self.tmp / "gh-real.cmd"
            fake.write_text('@"%s" "%s" %%*\r\n' % (sys.executable, self.tmp / "gh-real.py"))
        else:
            fake = self.tmp / "gh-real"
            fake.write_text("#!%s\n" % sys.executable + FAKE_GH.split("\n", 1)[1])
            fake.chmod(0o755)
        self.env = dict(os.environ, GH_LIMIT_REAL=str(fake), GH_LIMIT_DIR=str(self.tmp / "d"), FAKE_DIR=str(self.tmp / "s"))
        self.env.pop("GH_LIMIT_ACTIVE", None)
        self.env.pop("GH_LIMIT_CLOSED", None)

    def gh(self, *args, mod="", input_=None):
        r = subprocess.run([sys.executable, "-I", str(TOOL), *args], env=dict(self.env, FAKE_MOD=mod), capture_output=True,
                           input=input_, timeout=60)
        return r

    def log(self):
        return [json.loads(s) for s in (self.tmp / "d" / "log").read_text().splitlines()]

    def test_transition_ve_flagless_output(self):
        r = self.gh("run", "list")
        self.assertEqual((r.returncode, r.stdout), (0, b"govde\n"))
        self.assertEqual(json.loads((self.tmp / "s" / "argv1").read_text()), ["run", "list"])
        self.assertEqual(self.log()[-1]["kind"], "okuma")

    def test_secondary_at_limit_again_tries(self):
        t = time.time()
        r = self.gh("api", "repos/o/r/issues/1/comments", "-f", "body=x", mod="secondary")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, b"govde\n")                 # headers stripped, no duplicate output
        self.assertGreaterEqual(time.time() - t, 1.0)           # honoured Retry-After: 1
        record = self.log()
        self.assertTrue(record[0]["limited"])
        self.assertEqual((record[-1]["attempt"], record[-1]["status"], record[-1]["kind"]), (2, 200, "write"))

    def test_etag_304_from_cache(self):
        r1 = self.gh("api", "repos/o/r", "--jq", ".x", mod="etag")
        r2 = self.gh("api", "repos/o/r", "--jq", ".x", mod="etag")
        self.assertEqual((r1.stdout, r2.stdout, r2.returncode, r2.stderr), (b"govde\n", b"govde\n", 0, b""))
        self.assertIn("If-None-Match: W/\"e1\"", json.loads((self.tmp / "s" / "argv2").read_text()))
        self.assertTrue(self.log()[-1].get("cache"))

    def test_pr_view_local(self):
        r = self.gh("pr", "view", "--json", "number,url")
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"no pull requests found for branch", r.stderr)
        self.assertFalse((self.tmp / "s" / "n").exists())              # the real gh was never called
        self.assertTrue(self.log()[-1]["short_devre"])
        self.assertTrue(g.pr_short_devre(["pr", "status"]))
        self.assertFalse(g.pr_short_devre(["pr", "view", "12"]))         # explicit PR number goes to GitHub

    def test_stdin_buffered(self):
        r = self.gh("api", "graphql", "--input", "-", input_=b"{}")
        self.assertEqual(r.stdout, b"govde-{}\n")

    def test_inner_ice_call_direct_passes(self):
        r = subprocess.run([sys.executable, "-I", str(TOOL), "run", "list"], env=dict(self.env, GH_LIMIT_ACTIVE="1"),
                           capture_output=True, timeout=30)
        self.assertEqual(r.stdout, b"govde\n")
        self.assertFalse((self.tmp / "d" / "log").exists())

    def test_kur_shim(self):
        target = self.tmp / "bin" / "gh"
        old = os.environ.get("GH_LIMIT_REAL")
        os.environ["GH_LIMIT_REAL"] = self.env["GH_LIMIT_REAL"]
        try:
            self.assertEqual(g.install(target=target), 0)
            if os.name == "nt":     # the sh shim needs Git Bash; the pwsh shim is what pwsh runs
                self.assertEqual(target.read_bytes().count(b"\r"), 0)       # LF endings for sh
                ps = shutil.which("pwsh")
                if ps:
                    r = subprocess.run([ps, "-NoProfile", "-Command", "& '%s.ps1' run list" % target], env=self.env,
                                       capture_output=True, timeout=60)
                    self.assertEqual(r.stdout.replace(b"\r\n", b"\n"), b"govde\n", r.stderr)
            else:
                r = subprocess.run([str(target), "run", "list"], env=self.env, capture_output=True, timeout=30)
                self.assertEqual(r.stdout, b"govde\n")
            self.assertEqual(g.install(remove=True, target=target), 0)
            self.assertFalse(target.exists())
            self.assertFalse(Path(str(target) + ".ps1").exists())
            target.write_text("#!/bin/sh\necho foreign\n")
            self.assertEqual(g.install(target=target), 1)            # a foreign file is never overwritten
        finally:
            if old is None:
                os.environ.pop("GH_LIMIT_REAL", None)
            else:
                os.environ["GH_LIMIT_REAL"] = old


class ShQTest(unittest.TestCase):
    def test_single_quote_kacar(self):
        self.assertEqual(g.sh_q("/a'b"), "'/a'\\''b'")     # ' -> '\'' (close, escaped quote, reopen)
        sh = shutil.which("sh")
        if sh:
            r = subprocess.run([sh, "-c", "printf %s " + g.sh_q("/tmp/a'b c")], capture_output=True, text=True, timeout=30)
            self.assertEqual(r.stdout, "/tmp/a'b c", r.stderr)


class LockVeProfileTest(unittest.TestCase):
    def test_lock_second_times_unavailable(self):
        d = Path(tempfile.mkdtemp())
        a = g.Lock(d / "slot.0", unblocked=True)
        self.assertTrue(a.__enter__())
        b = g.Lock(d / "slot.0", unblocked=True)
        self.assertFalse(b.__enter__())          # taken: non-blocking acquire fails (flock on POSIX, msvcrt on Windows)
        a.__exit__()
        c = g.Lock(d / "slot.0", unblocked=True)
        self.assertTrue(c.__enter__())           # released with the file
        c.__exit__()

    def test_profile_block_idempotent_ve_back_taken(self):
        path = Path(tempfile.mkdtemp()) / "PowerShell" / "profile.ps1"
        g.write_profile(path, Path("C:/Users/x/.local/bin"), False, "", False)
        once = path.read_text(encoding="utf-8")
        self.assertIn(g.PROFILE_BAS, once)
        self.assertIn(str(Path("C:/Users/x/.local/bin")), once)
        g.write_profile(path, Path("C:/Users/x/.local/bin"), False, "", False)
        self.assertEqual(path.read_text(encoding="utf-8"), once)                  # second run changes nothing
        path.write_bytes(b"Set-Alias k kubectl\r\n" + path.read_bytes())
        g.write_profile(path, Path("C:/Users/x/.local/bin"), True, "", False)
        self.assertEqual(path.read_bytes(), b"Set-Alias k kubectl\r\n")   # only our block is removed

if __name__ == "__main__":
    unittest.main()

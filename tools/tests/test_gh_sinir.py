"""Tests for tools/gh_sinir.py: classification, limiter math, header parsing, and the retry / ETag paths against a fake gh."""
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

ARAC = Path(__file__).resolve().parents[1] / "gh_sinir.py"
sys.path.insert(0, str(ARAC.parent))
import gh_sinir as g  # noqa: E402

SAHTE_GH = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys
    sys.stdout.reconfigure(newline="\n")   # Windows text mode would turn LF into CRLF
    sys.stderr.reconfigure(newline="\n")
    d = os.environ["SAHTE_DIZIN"]
    n_yol = os.path.join(d, "n")
    n = int(open(n_yol).read()) + 1 if os.path.exists(n_yol) else 1
    open(n_yol, "w").write(str(n))
    open(os.path.join(d, "argv%d" % n), "w").write(json.dumps(sys.argv[1:]))
    mod = os.environ.get("SAHTE_MOD", "")
    inc = "--include" in sys.argv
    if mod == "ikincil" and n == 1:
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


class SiniflandirTest(unittest.TestCase):
    def test_siniflar(self):
        c = lambda *a: g.siniflandir(list(a))[0]
        self.assertEqual(c("auth", "git-credential", "get"), "gecis")
        self.assertEqual(c("--version"), "gecis")
        self.assertEqual(c("run", "watch", "--help"), "gecis")
        self.assertEqual(c("api", "repos/o/r/actions/runs/5"), "okuma")
        self.assertEqual(c("api", "repos/o/r/issues/1/comments", "-f", "body=x"), "yazma")
        self.assertEqual(c("api", "-XPATCH", "repos/o/r/issues/1"), "yazma")
        self.assertEqual(c("api", "--method=DELETE", "repos/o/r/git/refs/heads/x"), "yazma")
        self.assertEqual(c("api", "-X", "GET", "search/issues", "-f", "q=x"), "arama")
        self.assertEqual(c("api", "search/code?q=x"), "kod_arama")
        self.assertEqual(c("api", "graphql", "-f", "query=query { viewer { login } }"), "graphql")
        self.assertEqual(c("api", "graphql", "-f", "query=mutation { addComment(input:{}) { clientMutationId } }"), "yazma")
        self.assertEqual(c("issue", "comment", "5", "--body", "x"), "yazma")
        self.assertEqual(c("issue", "list", "-R", "o/r"), "okuma")
        self.assertEqual(c("run", "watch", "123"), "izle")
        self.assertEqual(c("pr", "checks", "4", "--watch"), "izle")
        self.assertEqual(c("search", "code", "foo"), "kod_arama")
        self.assertEqual(c("search", "issues", "foo"), "arama")
        self.assertEqual(c("run", "list", "-L", "5"), "okuma")

    def test_izle_araligi(self):
        self.assertEqual(g.izle_araligi(["run", "watch", "5"]), ["run", "watch", "5", "--interval", str(g.IZLE_ARALIK)])
        self.assertEqual(g.izle_araligi(["run", "watch", "5", "-i", "60"]), ["run", "watch", "5", "-i", "60"])
        self.assertEqual(g.izle_araligi(["pr", "checks", "--watch", "--interval=90"]), ["pr", "checks", "--watch", "--interval=90"])

    def test_uc_normal(self):
        self.assertEqual(g.uc_normal("/repos/o/r/actions/runs/123456/jobs?per_page=5"), "repos/o/r/actions/runs/:n/jobs")
        self.assertEqual(g.uc_normal("repos/o/r/commits/abcdef1234567"), "repos/o/r/commits/:sha")


class LimitTest(unittest.TestCase):
    def test_aralik(self):
        self.assertEqual(g.rezerve([], 100.0, 1.0, 60, 450), 100.0)
        self.assertEqual(g.rezerve([100.0], 100.2, 1.0, 60, 450), 101.0)
        self.assertEqual(g.rezerve([90.0], 100.0, 1.0, 60, 450), 100.0)

    def test_dakika_tavani(self):
        z = [100.0 + i for i in range(60)]           # 60 starts in [100, 159]
        t = g.rezerve(z, 159.5, 1.0, 60, 450)
        self.assertGreaterEqual(t, 160.0)
        self.assertLess(len([x for x in z if x > t - 60]), 60)

    def test_saat_tavani(self):
        z = [i * 7.0 for i in range(450)]             # 450 starts in ~52 min
        t = g.rezerve(z, z[-1] + 1, 1.0, 60, 450)
        self.assertEqual(t, 3600.0)                    # first start leaves the hour window

    def test_tempo(self):
        self.assertEqual(g.tempo_araligi(4000, 2000, 1000), 0.0)
        self.assertEqual(g.tempo_araligi(None, None, 1000), 0.0)
        self.assertAlmostEqual(g.tempo_araligi(1200, 2000, 1000), 1000 / 1000 * g.MAKINE)
        self.assertEqual(g.tempo_araligi(100, 4600, 1000), g.TEMPO_UST)
        self.assertEqual(g.tempo_araligi(100, 900, 1000), 0.0)          # window already reset
        self.assertEqual(g.kaynak("okuma", ["pr", "view", "1"], None), "graphql")
        self.assertEqual(g.kaynak("okuma", ["run", "view", "1"], None), "core")

    def test_bekleme(self):
        self.assertEqual(g.bekleme_suresi(1, retry_after=30), (30.0, False))
        self.assertEqual(g.bekleme_suresi(1, retry_after=5000), (g.BEKLE_UST, False))
        self.assertEqual(g.bekleme_suresi(1, kalan=0, sifirlanma=1100, simdi=1000), (101.0, True))
        self.assertEqual(g.bekleme_suresi(1)[0], 60.0)
        self.assertEqual(g.bekleme_suresi(2)[0], 120.0)
        self.assertEqual(g.bekleme_suresi(5)[0], g.BEKLE_UST)

    def test_baslik_ve_sinir(self):
        d, b, govde = g.baslik_ayir(b"HTTP/2.0 403 Forbidden\r\nRetry-After: 7\r\n\r\n{\"m\":1}")
        self.assertEqual((d, b["retry-after"], govde), (403, "7", b'{"m":1}'))
        self.assertEqual(g.baslik_ayir(b"plain"), (None, {}, b"plain"))
        self.assertTrue(g.sinirli_mi(d, b, 1, ""))
        self.assertFalse(g.sinirli_mi(403, {"x-ratelimit-remaining": "10"}, 1, "Resource not accessible by integration"))
        self.assertTrue(g.sinirli_mi(None, {}, 1, "gh: You have exceeded a secondary rate limit (HTTP 403)"))
        self.assertFalse(g.sinirli_mi(404, {}, 1, "Not Found"))


class SahteGhTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "s").mkdir()
        if os.name == "nt":     # no shebangs on Windows: a .cmd runs the fake gh script
            (self.tmp / "gh-gercek.py").write_text(SAHTE_GH.split("\n", 1)[1])
            sahte = self.tmp / "gh-gercek.cmd"
            sahte.write_text('@"%s" "%s" %%*\r\n' % (sys.executable, self.tmp / "gh-gercek.py"))
        else:
            sahte = self.tmp / "gh-gercek"
            sahte.write_text("#!%s\n" % sys.executable + SAHTE_GH.split("\n", 1)[1])
            sahte.chmod(0o755)
        self.env = dict(os.environ, GH_SINIR_GERCEK=str(sahte), GH_SINIR_DIZIN=str(self.tmp / "d"), SAHTE_DIZIN=str(self.tmp / "s"))
        self.env.pop("GH_SINIR_AKTIF", None)
        self.env.pop("GH_SINIR_KAPALI", None)

    def gh(self, *args, mod="", girdi=None):
        r = subprocess.run([sys.executable, "-I", str(ARAC), *args], env=dict(self.env, SAHTE_MOD=mod), capture_output=True,
                           input=girdi, timeout=60)
        return r

    def log(self):
        return [json.loads(s) for s in (self.tmp / "d" / "log").read_text().splitlines()]

    def test_gecis_ve_bayraksiz_cikti(self):
        r = self.gh("run", "list")
        self.assertEqual((r.returncode, r.stdout), (0, b"govde\n"))
        self.assertEqual(json.loads((self.tmp / "s" / "argv1").read_text()), ["run", "list"])
        self.assertEqual(self.log()[-1]["sinif"], "okuma")

    def test_ikincil_sinirda_yeniden_dener(self):
        t = time.time()
        r = self.gh("api", "repos/o/r/issues/1/comments", "-f", "body=x", mod="ikincil")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, b"govde\n")                 # headers stripped, no duplicate output
        self.assertGreaterEqual(time.time() - t, 1.0)           # honoured Retry-After: 1
        kayit = self.log()
        self.assertTrue(kayit[0]["sinirli"])
        self.assertEqual((kayit[-1]["deneme"], kayit[-1]["durum"], kayit[-1]["sinif"]), (2, 200, "yazma"))

    def test_etag_304_onbellekten(self):
        r1 = self.gh("api", "repos/o/r", "--jq", ".x", mod="etag")
        r2 = self.gh("api", "repos/o/r", "--jq", ".x", mod="etag")
        self.assertEqual((r1.stdout, r2.stdout, r2.returncode, r2.stderr), (b"govde\n", b"govde\n", 0, b""))
        self.assertIn("If-None-Match: W/\"e1\"", json.loads((self.tmp / "s" / "argv2").read_text()))
        self.assertTrue(self.log()[-1].get("onbellek"))

    def test_pr_view_yerel(self):
        r = self.gh("pr", "view", "--json", "number,url")
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"no pull requests found for branch", r.stderr)
        self.assertFalse((self.tmp / "s" / "n").exists())              # the real gh was never called
        self.assertTrue(self.log()[-1]["kisa_devre"])
        self.assertTrue(g.pr_kisa_devre(["pr", "status"]))
        self.assertFalse(g.pr_kisa_devre(["pr", "view", "12"]))         # explicit PR number goes to GitHub

    def test_stdin_tamponlanir(self):
        r = self.gh("api", "graphql", "--input", "-", girdi=b"{}")
        self.assertEqual(r.stdout, b"govde-{}\n")

    def test_ic_ice_cagri_dogrudan_gecer(self):
        r = subprocess.run([sys.executable, "-I", str(ARAC), "run", "list"], env=dict(self.env, GH_SINIR_AKTIF="1"),
                           capture_output=True, timeout=30)
        self.assertEqual(r.stdout, b"govde\n")
        self.assertFalse((self.tmp / "d" / "log").exists())

    def test_kur_shim(self):
        hedef = self.tmp / "bin" / "gh"
        eski = os.environ.get("GH_SINIR_GERCEK")
        os.environ["GH_SINIR_GERCEK"] = self.env["GH_SINIR_GERCEK"]
        try:
            self.assertEqual(g.kur(hedef=hedef), 0)
            if os.name == "nt":     # the sh shim needs Git Bash; the pwsh shim is what pwsh runs
                self.assertEqual(hedef.read_bytes().count(b"\r"), 0)       # LF endings for sh
                ps = shutil.which("pwsh")
                if ps:
                    r = subprocess.run([ps, "-NoProfile", "-Command", "& '%s.ps1' run list" % hedef], env=self.env,
                                       capture_output=True, timeout=60)
                    self.assertEqual(r.stdout.replace(b"\r\n", b"\n"), b"govde\n", r.stderr)
            else:
                r = subprocess.run([str(hedef), "run", "list"], env=self.env, capture_output=True, timeout=30)
                self.assertEqual(r.stdout, b"govde\n")
            self.assertEqual(g.kur(kaldir=True, hedef=hedef), 0)
            self.assertFalse(hedef.exists())
            self.assertFalse(Path(str(hedef) + ".ps1").exists())
            hedef.write_text("#!/bin/sh\necho foreign\n")
            self.assertEqual(g.kur(hedef=hedef), 1)            # a foreign file is never overwritten
        finally:
            if eski is None:
                os.environ.pop("GH_SINIR_GERCEK", None)
            else:
                os.environ["GH_SINIR_GERCEK"] = eski


class ShQTest(unittest.TestCase):
    def test_tek_tirnak_kacar(self):
        self.assertEqual(g.sh_q("/a'b"), "'/a'\\''b'")     # ' -> '\'' (close, escaped quote, reopen)
        sh = shutil.which("sh")
        if sh:
            r = subprocess.run([sh, "-c", "printf %s " + g.sh_q("/tmp/a'b c")], capture_output=True, text=True, timeout=30)
            self.assertEqual(r.stdout, "/tmp/a'b c", r.stderr)


class KilitVeProfilTest(unittest.TestCase):
    def test_kilit_ikinci_kez_alinamaz(self):
        d = Path(tempfile.mkdtemp())
        a = g.Kilit(d / "slot.0", engelsiz=True)
        self.assertTrue(a.__enter__())
        b = g.Kilit(d / "slot.0", engelsiz=True)
        self.assertFalse(b.__enter__())          # taken: non-blocking acquire fails (flock on POSIX, msvcrt on Windows)
        a.__exit__()
        c = g.Kilit(d / "slot.0", engelsiz=True)
        self.assertTrue(c.__enter__())           # released with the file
        c.__exit__()

    def test_profil_blogu_idempotent_ve_geri_alinir(self):
        yol = Path(tempfile.mkdtemp()) / "PowerShell" / "profile.ps1"
        g.profil_yaz(yol, Path("C:/Users/x/.local/bin"), False, "", False)
        once = yol.read_text(encoding="utf-8")
        self.assertIn(g.PROFIL_BAS, once)
        self.assertIn(str(Path("C:/Users/x/.local/bin")), once)
        g.profil_yaz(yol, Path("C:/Users/x/.local/bin"), False, "", False)
        self.assertEqual(yol.read_text(encoding="utf-8"), once)                  # second run changes nothing
        yol.write_bytes(b"Set-Alias k kubectl\r\n" + yol.read_bytes())
        g.profil_yaz(yol, Path("C:/Users/x/.local/bin"), True, "", False)
        self.assertEqual(yol.read_bytes(), b"Set-Alias k kubectl\r\n")   # only our block is removed

if __name__ == "__main__":
    unittest.main()

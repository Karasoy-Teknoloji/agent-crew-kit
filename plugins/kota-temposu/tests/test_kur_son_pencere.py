"""Tests for the installer's opt-in `--son-pencere` (kur_ortak.py): command, timer, files, name/access checks, removal.

Run:  python3 -I -m unittest discover -s plugins/kota-temposu/tests     (from the repo root)

Scratch HOME; the shared repo is a local bare repository (CLAUDE_SHARED_UZAK); systemctl/claude/gh are fakes on PATH, so nothing
real is installed or started. POSIX only (the Windows task registration is covered by a snippet check).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def kopya(args):
    """The installer runs from the clone by default; these tests exercise the legacy copy mode unless they ask for --repodan."""
    a = list(args)
    return a if {"--repodan", "--kopya", "--remove", "--sinama"} & set(a) else ["--kopya", *a]
PLUG = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from test_kur_maliyet import KUR, clean_env, rd  # noqa: E402
sys.path.insert(0, PLUG)
import kur_ortak  # noqa: E402

POSIX = os.name != "nt"


def git(*args, cwd=None):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


@unittest.skipUnless(POSIX and shutil.which("git"), "needs POSIX and git")
class Kurulum(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="kur-sp-")
        cls.uzak = os.path.join(cls.root, "uzak.git")
        git("init", "-q", "--bare", "-b", "main", cls.uzak)
        t = os.path.join(cls.root, "tohum")
        git("clone", "-q", cls.uzak, t)
        os.makedirs(os.path.join(t, "plugins", "kota-temposu"))
        shutil.copy(os.path.join(PLUG, "tests", "fixtures", "kisiler-ornek.json"), os.path.join(t, "plugins", "kota-temposu", "kisiler.json"))
        git("add", "-A", cwd=t)
        git("commit", "-q", "-m", "seed", cwd=t)
        git("push", "-q", "origin", "HEAD:main", cwd=t)
        cls.bos = os.path.join(cls.root, "bos.git")
        git("init", "-q", "--bare", "-b", "main", cls.bos)
        t2 = os.path.join(cls.root, "tohum2")
        git("clone", "-q", cls.bos, t2)
        os.makedirs(os.path.join(t2, "plugins", "kota-temposu"))
        with open(os.path.join(t2, "plugins", "kota-temposu", "kisiler.json"), "w") as f:
            json.dump({"baskasi": "Baska Kisi"}, f)
        git("add", "-A", cwd=t2)
        git("commit", "-q", "-m", "seed", cwd=t2)
        git("push", "-q", "origin", "HEAD:main", cwd=t2)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="home-", dir=self.root)
        self.fakebin = os.path.join(self.root, "fakebin-" + os.path.basename(self.home))
        os.makedirs(self.fakebin)
        for ad in ("systemctl", "claude", "gh"):
            p = os.path.join(self.fakebin, ad)
            with open(p, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(p, 0o755)
        self.claude = os.path.join(self.home, ".claude")
        self.udir = os.path.join(self.home, ".config", "systemd", "user")
        self.gitconfig = os.path.join(self.home, "gitconfig-bos")
        open(self.gitconfig, "w").close()
        self.sarmal = os.path.join(self.home, ".local", "bin", "son-pencere")

    def env(self, uzak=None):
        e = clean_env(HOME=self.home, USERPROFILE=self.home, PATH=self.fakebin + os.pathsep + os.environ.get("PATH", ""),
                      CLAUDE_SHARED_UZAK=uzak or self.uzak, GIT_CONFIG_GLOBAL=self.gitconfig, GIT_CONFIG_SYSTEM=os.devnull)
        for k in ("CLAUDE_SHARED_KISI", "CLAUDE_SHARED_PLAN", "CLAUDE_SHARED_KLON", "CLAUDE_SHARED_KOK", "SSH_AUTH_SOCK"):
            e.pop(k, None)
        return e

    def kur(self, *args, uzak=None):
        return subprocess.run([sys.executable, "-I", KUR, "--python", sys.executable, *kopya(args)], capture_output=True, text=True,
                              encoding="utf-8", env=self.env(uzak), timeout=600)

    def test_default_install_does_not_enrol_the_machine(self):
        self.assertEqual(self.kur().returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.claude, "bin", "son_pencere.py")))
        self.assertFalse(os.path.exists(self.sarmal))
        self.assertFalse(os.path.exists(os.path.join(self.udir, "son-pencere.timer")))

    def test_opt_in_installs_command_files_and_the_5_minute_timer(self):
        r = self.kur("--son-pencere", "--kisi", "Emre")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for f in ("son_pencere.py", "son-pencere-istem.md", "maliyet_paylas.py"):
            self.assertTrue(os.path.isfile(os.path.join(self.claude, "bin", f)), f)
        self.assertTrue(os.access(self.sarmal, os.X_OK))
        svc, tmr = rd(os.path.join(self.udir, "son-pencere.service")), rd(os.path.join(self.udir, "son-pencere.timer"))
        self.assertIn("ExecStart=%s -I %s tetik\n" % (sys.executable, os.path.join(self.claude, "bin", "son_pencere.py")), svc)
        self.assertIn("SON_PENCERE_CLAUDE=" + os.path.join(self.fakebin, "claude"), svc)    # a user timer does not see the login PATH
        self.assertIn("Type=oneshot", svc)
        self.assertIn("OnCalendar=*:0/5", tmr)
        self.assertEqual(rd(os.path.join(self.home, ".config", "claude-shared", "kisi")), "Emre\n")
        self.assertIn("[OK] ad kisiler.json'da, push yetkisi var", r.stdout)
        h = subprocess.run([self.sarmal, "--help"], capture_output=True, text=True, encoding="utf-8", env=self.env(), timeout=60)
        self.assertEqual(h.returncode, 0, h.stderr)
        self.assertIn("tetik", h.stdout)

    def test_rerun_is_idempotent_and_remove_deletes_only_ours(self):
        self.assertEqual(self.kur("--son-pencere", "--kisi", "Emre").returncode, 0)
        again = self.kur("--son-pencere", "--kisi", "Emre")
        self.assertEqual(again.returncode, 0)
        self.assertNotIn("yedek:", again.stdout)
        self.assertIn("ayni: " + self.sarmal, again.stdout)
        k = self.kur("--remove")
        self.assertEqual(k.returncode, 0, k.stdout)
        for p in (self.sarmal, os.path.join(self.udir, "son-pencere.timer"), os.path.join(self.udir, "son-pencere.service"),
                  os.path.join(self.claude, "bin", "son_pencere.py"), os.path.join(self.claude, "bin", "son-pencere-istem.md")):
            self.assertFalse(os.path.exists(p), p)

    def test_repodan_runs_from_the_clone(self):
        self.assertEqual(self.kur("--son-pencere", "--kisi", "Emre", "--repodan").returncode, 0)
        self.assertIn(os.path.join(PLUG, "bin", "son_pencere.py"), rd(os.path.join(self.udir, "son-pencere.service")))
        self.assertFalse(os.path.exists(os.path.join(self.claude, "bin", "son_pencere.py")))

    def test_bad_name_missing_tools_and_no_access_install_nothing(self):
        r = self.kur("--son-pencere", "--kisi", "Yok Boyle Biri")
        self.assertEqual(r.returncode, 2)
        self.assertIn("kisiler.json listesinde yok", r.stdout)
        r = self.kur("--son-pencere", "--kisi", "Emre", uzak=self.bos)
        self.assertEqual(r.returncode, 3, r.stdout)
        r = self.kur("--son-pencere", "--maliyetsiz", "--kisi", "Emre")
        self.assertEqual(r.returncode, 2)
        self.assertFalse(os.path.exists(self.sarmal))
        self.assertFalse(os.path.exists(os.path.join(self.udir, "son-pencere.timer")))

    def test_missing_claude_or_gh_stops_before_anything_is_installed(self):
        os.remove(os.path.join(self.fakebin, "gh"))
        gitonly = os.path.join(self.root, "gitonly-" + os.path.basename(self.home))   # PATH = the fakes + a directory holding only git
        os.makedirs(gitonly)
        os.symlink(shutil.which("git"), os.path.join(gitonly, "git"))
        e = dict(self.env(), PATH=self.fakebin + os.pathsep + gitonly)
        r = subprocess.run([sys.executable, "-I", KUR, "--python", sys.executable, "--kopya", "--son-pencere", "--kisi", "Emre"], capture_output=True,
                           text=True, encoding="utf-8", env=e, timeout=600)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("bulunamadi: gh", r.stdout)
        self.assertFalse(os.path.exists(self.sarmal))

    def test_whatif_writes_nothing(self):
        r = self.kur("--son-pencere", "--kisi", "Emre", "--whatif")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(os.path.exists(self.sarmal))
        self.assertIn("yaz: " + os.path.join(self.udir, "son-pencere.timer"), r.stdout)

    def test_resume_alarm_installs_the_same_timer_with_its_mode_and_is_remembered(self):
        r = self.kur("--devam-alarmi", "--kisi", "Emre", "--repodan")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        svc = rd(os.path.join(self.udir, "son-pencere.service"))
        self.assertIn(" tetik --kipler devam\n", svc)                 # resume alarm only: the last window stays off
        self.assertIn("OnCalendar=*:0/5", rd(os.path.join(self.udir, "son-pencere.timer")))
        kayit = json.loads(rd(os.path.join(self.claude, "kota-temposu.json")))["kurulum"]
        self.assertIn("--devam-alarmi", kayit)
        self.assertNotIn("--son-pencere", kayit)
        r = self.kur("--son-pencere", "--repodan")                    # a later flag adds to the saved one
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(" tetik --kipler pencere,devam\n", rd(os.path.join(self.udir, "son-pencere.service")))

    def test_mode_arguments_of_the_timer(self):
        class A:
            son_pencere, devam_alarmi = False, False
        a = A()
        self.assertEqual(kur_ortak.sp_tetik(a), "tetik")     # the last window alone: a bare tetik (a rolled-back script understands it)
        a.devam_alarmi = True
        self.assertEqual(kur_ortak.sp_tetik(a), "tetik --kipler devam")
        a.son_pencere = True
        self.assertEqual(kur_ortak.sp_tetik(a), "tetik --kipler pencere,devam")

    def test_windows_task_snippet_repeats_every_5_minutes_for_the_current_user_only(self):
        ps = kur_ortak.SP_GOREV_KAYIT_PS
        self.assertIn("-RepetitionInterval (New-TimeSpan -Minutes 5)", ps)
        self.assertIn("-MultipleInstances IgnoreNew", ps)
        self.assertIn("Register-GizliGorev", ps)                     # user-level principal lives in tools/gizli.ps1 (test_kur_paylas)
        self.assertIn("$env:KUR_SP_TETIK", ps)                        # `tetik --kipler ...` from sp_tetik
        self.assertEqual(kur_ortak.SP_GOREV, "son-pencere")


if __name__ == "__main__":
    unittest.main()

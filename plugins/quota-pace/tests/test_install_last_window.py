"""Tests for the installer's opt-in `--last-window` (install_common.py): command, timer, files, name/access checks, removal.

Run:  python3 -I -m unittest discover -s plugins/quota-pace/tests     (from the repo root)

Scratch HOME; the shared repo is a local bare repository (CLAUDE_SHARED_REMOTE); systemctl/claude/gh are fakes on PATH, so nothing
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


def copy(args):
    """The installer runs from the clone by default; these tests exercise the legacy copy mode unless they ask for --direct."""
    a = list(args)
    return a if {"--direct", "--copy", "--remove", "--probe"} & set(a) else ["--copy", *a]
PLUG = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from test_install_cost import INSTALL, clean_env, rd  # noqa: E402
sys.path.insert(0, PLUG)
import install_common  # noqa: E402

POSIX = os.name != "nt"


def git(*args, cwd=None):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


@unittest.skipUnless(POSIX and shutil.which("git"), "needs POSIX and git")
class Setup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="install-lw-")
        cls.remote = os.path.join(cls.root, "remote.git")
        git("init", "-q", "--bare", "-b", "main", cls.remote)
        t = os.path.join(cls.root, "seed")
        git("clone", "-q", cls.remote, t)
        os.makedirs(os.path.join(t, "plugins", "quota-pace"))
        shutil.copy(os.path.join(PLUG, "tests", "fixtures", "people-example.json"), os.path.join(t, "plugins", "quota-pace", "people.json"))
        git("add", "-A", cwd=t)
        git("commit", "-q", "-m", "seed", cwd=t)
        git("push", "-q", "origin", "HEAD:main", cwd=t)
        cls.bos = os.path.join(cls.root, "bos.git")
        git("init", "-q", "--bare", "-b", "main", cls.bos)
        t2 = os.path.join(cls.root, "seed2")
        git("clone", "-q", cls.bos, t2)
        os.makedirs(os.path.join(t2, "plugins", "quota-pace"))
        with open(os.path.join(t2, "plugins", "quota-pace", "people.json"), "w") as f:
            json.dump({"someone_else": "Someone Else"}, f)
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
        for name in ("systemctl", "claude", "gh"):
            p = os.path.join(self.fakebin, name)
            with open(p, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(p, 0o755)
        self.claude = os.path.join(self.home, ".claude")
        self.udir = os.path.join(self.home, ".config", "systemd", "user")
        self.gitconfig = os.path.join(self.home, "gitconfig-bos")
        open(self.gitconfig, "w").close()
        self.wrapper = os.path.join(self.home, ".local", "bin", "last-window")

    def env(self, remote=None):
        e = clean_env(HOME=self.home, USERPROFILE=self.home, PATH=self.fakebin + os.pathsep + os.environ.get("PATH", ""),
                      CLAUDE_SHARED_REMOTE=remote or self.remote, GIT_CONFIG_GLOBAL=self.gitconfig, GIT_CONFIG_SYSTEM=os.devnull)
        for k in ("CLAUDE_SHARED_PERSON", "CLAUDE_SHARED_PLAN", "CLAUDE_SHARED_CLONE", "CLAUDE_SHARED_ROOT", "SSH_AUTH_SOCK"):
            e.pop(k, None)
        return e

    def install(self, *args, remote=None):
        return subprocess.run([sys.executable, "-I", INSTALL, "--python", sys.executable, *copy(args)], capture_output=True, text=True,
                              encoding="utf-8", env=self.env(remote), timeout=600)

    def test_default_install_does_not_enrol_the_machine(self):
        self.assertEqual(self.install().returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.claude, "bin", "last_window.py")))
        self.assertFalse(os.path.exists(self.wrapper))
        self.assertFalse(os.path.exists(os.path.join(self.udir, "last-window.timer")))

    def test_opt_in_installs_command_files_and_the_5_minute_timer(self):
        r = self.install("--last-window", "--person", "Emre")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for f in ("last_window.py", "last-window-prompt.md", "cost_share.py"):
            self.assertTrue(os.path.isfile(os.path.join(self.claude, "bin", f)), f)
        self.assertTrue(os.access(self.wrapper, os.X_OK))
        svc, tmr = rd(os.path.join(self.udir, "last-window.service")), rd(os.path.join(self.udir, "last-window.timer"))
        self.assertIn("ExecStart=%s -I %s trigger\n" % (sys.executable, os.path.join(self.claude, "bin", "last_window.py")), svc)
        self.assertIn("LAST_WINDOW_CLAUDE=" + os.path.join(self.fakebin, "claude"), svc)    # a user timer does not see the login PATH
        self.assertIn("Type=oneshot", svc)
        self.assertIn("OnCalendar=*:0/5", tmr)
        self.assertEqual(rd(os.path.join(self.home, ".config", "claude-shared", "person")), "Emre\n")
        self.assertIn("[OK] name is in people.json, push access present", r.stdout)
        h = subprocess.run([self.wrapper, "--help"], capture_output=True, text=True, encoding="utf-8", env=self.env(), timeout=60)
        self.assertEqual(h.returncode, 0, h.stderr)
        self.assertIn("trigger", h.stdout)

    def test_rerun_is_idempotent_and_remove_deletes_only_ours(self):
        self.assertEqual(self.install("--last-window", "--person", "Emre").returncode, 0)
        again = self.install("--last-window", "--person", "Emre")
        self.assertEqual(again.returncode, 0)
        self.assertNotIn("backup:", again.stdout)
        self.assertIn("same: " + self.wrapper, again.stdout)
        k = self.install("--remove")
        self.assertEqual(k.returncode, 0, k.stdout)
        for p in (self.wrapper, os.path.join(self.udir, "last-window.timer"), os.path.join(self.udir, "last-window.service"),
                  os.path.join(self.claude, "bin", "last_window.py"), os.path.join(self.claude, "bin", "last-window-prompt.md")):
            self.assertFalse(os.path.exists(p), p)

    def test_direct_runs_from_the_clone(self):
        self.assertEqual(self.install("--last-window", "--person", "Emre", "--direct").returncode, 0)
        self.assertIn(os.path.join(PLUG, "bin", "last_window.py"), rd(os.path.join(self.udir, "last-window.service")))
        self.assertFalse(os.path.exists(os.path.join(self.claude, "bin", "last_window.py")))

    def test_bad_name_missing_tools_and_no_access_install_nothing(self):
        r = self.install("--last-window", "--person", "No Such Person")
        self.assertEqual(r.returncode, 2)
        self.assertIn("is not in the people.json list", r.stdout)
        r = self.install("--last-window", "--person", "Emre", remote=self.bos)
        self.assertEqual(r.returncode, 3, r.stdout)
        r = self.install("--last-window", "--free", "--person", "Emre")
        self.assertEqual(r.returncode, 2)
        self.assertFalse(os.path.exists(self.wrapper))
        self.assertFalse(os.path.exists(os.path.join(self.udir, "last-window.timer")))

    def test_missing_claude_or_gh_stops_before_anything_is_installed(self):
        os.remove(os.path.join(self.fakebin, "gh"))
        gitonly = os.path.join(self.root, "gitonly-" + os.path.basename(self.home))   # PATH = the fakes + a directory holding only git
        os.makedirs(gitonly)
        os.symlink(shutil.which("git"), os.path.join(gitonly, "git"))
        e = dict(self.env(), PATH=self.fakebin + os.pathsep + gitonly)
        r = subprocess.run([sys.executable, "-I", INSTALL, "--python", sys.executable, "--copy", "--last-window", "--person", "Emre"], capture_output=True,
                           text=True, encoding="utf-8", env=e, timeout=600)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("not found: gh", r.stdout)
        self.assertFalse(os.path.exists(self.wrapper))

    def test_whatif_writes_nothing(self):
        r = self.install("--last-window", "--person", "Emre", "--whatif")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(os.path.exists(self.wrapper))
        self.assertIn("write: " + os.path.join(self.udir, "last-window.timer"), r.stdout)

    def test_resume_alarm_installs_the_same_timer_with_its_mode_and_is_remembered(self):
        r = self.install("--resume-alarm", "--person", "Emre", "--direct")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        svc = rd(os.path.join(self.udir, "last-window.service"))
        self.assertIn(" trigger --modes resume\n", svc)                 # resume alarm only: the last window stays off
        self.assertIn("OnCalendar=*:0/5", rd(os.path.join(self.udir, "last-window.timer")))
        record = json.loads(rd(os.path.join(self.claude, "quota-pace.json")))["setup"]
        self.assertIn("--resume-alarm", record)
        self.assertNotIn("--last-window", record)
        r = self.install("--last-window", "--direct")                    # a later flag adds to the saved one
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(" trigger --modes window,resume\n", rd(os.path.join(self.udir, "last-window.service")))

    def test_mode_arguments_of_the_timer(self):
        class A:
            last_window, resume_alarm = False, False
        a = A()
        self.assertEqual(install_common.sp_trigger(a), "trigger")     # the last window alone: a bare trigger (a rolled-back script understands it)
        a.resume_alarm = True
        self.assertEqual(install_common.sp_trigger(a), "trigger --modes resume")
        a.last_window = True
        self.assertEqual(install_common.sp_trigger(a), "trigger --modes window,resume")

    def test_windows_task_snippet_repeats_every_5_minutes_for_the_current_user_only(self):
        ps = install_common.SP_TASK_RECORD_PS
        self.assertIn("-RepetitionInterval (New-TimeSpan -Minutes 5)", ps)
        self.assertIn("-MultipleInstances IgnoreNew", ps)
        self.assertIn("Register-HiddenTask", ps)                     # user-level principal lives in tools/hidden.ps1 (test_install_share)
        self.assertIn("$env:INSTALL_SP_TRIGGER", ps)                        # `trigger --modes ...` from sp_trigger
        self.assertEqual(install_common.SP_TASK, "last-window")


if __name__ == "__main__":
    unittest.main()

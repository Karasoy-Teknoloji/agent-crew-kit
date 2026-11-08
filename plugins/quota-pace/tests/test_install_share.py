"""Tests for the installer's `--share` (join the usage ranking in one command) and the macOS launchd agent (install_common.py).

Run:  python3 -I -m unittest discover -s plugins/quota-pace/tests     (from the repo root)

Everything runs under a scratch HOME. The "shared repo" is a local bare repository (CLAUDE_SHARED_REMOTE), so the access check is
real git but never touches the network; systemctl and launchctl are never the real ones (fake on PATH / replaced in a runner).
"""
import atexit
import json
import os
import plistlib
from pathlib import Path
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
sys.path.insert(0, HERE)
from test_install_cost import PWSH, clean_env, rd  # noqa: E402

PEOPLE = os.path.join(HERE, "fixtures", "people-example.json")   # example people list of the shared repo
# The installer checks the name against the people list of its own clone first. The kit ships none (a team's fork carries it), so
# these tests run the installer from a scratch copy of the kit that holds the example list; the source tree is never written.
_COPY = tempfile.mkdtemp(prefix="kit-clone-")
atexit.register(shutil.rmtree, _COPY, True)
shutil.copytree(os.path.normpath(os.path.join(HERE, "..", "..", "..")), os.path.join(_COPY, "kit"),
                ignore=shutil.ignore_patterns(".git", "__pycache__"))
PLUG = os.path.join(_COPY, "kit", "plugins", "quota-pace")
shutil.copy(PEOPLE, os.path.join(PLUG, "people.json"))
INSTALL = os.path.join(PLUG, "install_common.py")

POSIX = os.name != "nt"
LABEL = "com.agentcrewkit.cost-daily"


def git(*args, cwd=None):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class Join(unittest.TestCase):
    """install_common.py --share end to end on a POSIX-style install (Linux branch: systemd unit files under a scratch HOME)."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="install-share-")
        cls.remote = os.path.join(cls.root, "remote.git")        # the "shared repo": main holds the real people.json
        git("init", "-q", "--bare", "-b", "main", cls.remote)
        seed = os.path.join(cls.root, "seed")
        git("clone", "-q", cls.remote, seed)
        os.makedirs(os.path.join(seed, "plugins", "quota-pace"))
        shutil.copy(PEOPLE, os.path.join(seed, "plugins", "quota-pace", "people.json"))
        git("add", "-A", cwd=seed)
        git("commit", "-q", "-m", "seed", cwd=seed)
        git("push", "-q", "origin", "HEAD:main", cwd=seed)
        cls.bos = os.path.join(cls.root, "bos.git")           # a repo whose main does not list anybody we use
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
        self.fakebin = os.path.join(self.home, "..", "fakebin-" + os.path.basename(self.home))
        os.makedirs(self.fakebin)
        self.calls = os.path.join(self.fakebin, "systemctl-calls")
        fake = os.path.join(self.fakebin, "systemctl")
        with open(fake, "w") as f:
            f.write('#!/bin/sh\necho "$@" >> "%s"\nexit 0\n' % self.calls)
        os.chmod(fake, 0o755)
        self.claude = os.path.join(self.home, ".claude")
        self.setting = os.path.join(self.home, ".config", "claude-shared")
        self.udir = os.path.join(self.home, ".config", "systemd", "user")
        self.gitconfig = os.path.join(self.home, "gitconfig-bos")
        open(self.gitconfig, "w").close()

    def env(self, **extra):
        e = clean_env(HOME=self.home, USERPROFILE=self.home, PATH=self.fakebin + os.pathsep + os.environ.get("PATH", ""),
                      CLAUDE_SHARED_REMOTE=self.remote, GIT_CONFIG_GLOBAL=self.gitconfig, GIT_CONFIG_SYSTEM=os.devnull)
        for k in ("CLAUDE_SHARED_PERSON", "CLAUDE_SHARED_PLAN", "CLAUDE_SHARED_CLONE", "CLAUDE_SHARED_ROOT"):
            e.pop(k, None)
        e.update(extra)
        return e

    def install(self, *args, **extra):
        return self.install_raw(*copy(args), **extra)

    def install_raw(self, *args, **extra):
        win = ["--windows", "--shell", PWSH] if os.name == "nt" and PWSH else []
        return subprocess.run([sys.executable, "-I", INSTALL, *win, "--python", sys.executable, *args], capture_output=True, text=True,
                              encoding="utf-8", env=self.env(**extra), timeout=600)

    def service(self):
        return rd(os.path.join(self.udir, "cost-daily.service"))

    def daily_shares(self, *args):
        """Whether the daily job shares after an install with ARGS. POSIX: the systemd unit's command line. Windows: Task Scheduler
        is never reached from a scratch profile, so the install runs with real_home() patched to the scratch HOME and
        run_cmd_task() replaced by a recorder that keeps the `share` flag the real task registration would get (in a state file,
        which also answers task_shares(), the "was the registered task sharing" query). self.last = the installer output."""
        if POSIX:
            self.assertEqual(self.install(*args).returncode, 0)
            return "--save --share\n" in self.service()
        code = ("import sys, json; from pathlib import Path; sys.path.insert(0, %r); import install_common; calls = []; "
                "install_common.real_home = lambda: Path(%r); install_common.claude_plugin = lambda *a, **k: (True, 'ok'); "
                "install_common.run_cmd_sp_task = lambda *a, **k: True; "
                "dur = Path(%r); install_common.task_shares = lambda shell: dur.exists() and dur.read_text() == '1'; "
                "install_common.run_cmd_task = lambda shell, ps, exe, script, **kw: kw.get('name') == 'lost-work' or (calls.append(bool(kw.get('share'))), "
                "dur.write_text('1' if kw.get('share') else '0'), True)[-1]; "
                "sys.argv = ['install_common', '--windows', '--python', %r, '--shell', %r] + %r; rc = install_common.main(); "
                "print('CALLS' + json.dumps(calls)); sys.exit(rc)" % (PLUG, self.home, os.path.join(self.root, "task-status-" + os.path.basename(self.home)), sys.executable, PWSH, copy(args)))
        r = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code], capture_output=True, text=True, encoding="utf-8",
                           env=self.env(), timeout=600)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.last = r.stdout
        calls = [json.loads(l[5:]) for l in r.stdout.splitlines() if l.startswith("CALLS")]
        self.assertEqual(len(calls), 1, r.stdout)
        self.assertEqual(len(calls[0]), 1, r.stdout)                     # exactly one task registration
        return calls[0][0]

    def test_good_name_and_plan_write_the_files_the_job_reads_and_the_daily_job_shares(self):
        r = self.install("--share", "--person", "Gül Ersoy", "--plan", "100")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rd(os.path.join(self.setting, "person")), "Gül Ersoy\n")
        self.assertEqual(rd(os.path.join(self.setting, "plan")), "100\n")
        with open(os.path.join(self.setting, "person"), "rb") as f:
            self.assertFalse(f.read().startswith(b"\xef\xbb\xbf"))      # no BOM: read_from_file would keep it in the name
        # the job's own readers, started under the same HOME/USERPROFILE, find the name and the plan in exactly these files
        code = ("import sys; sys.path.insert(0, %r); import cost_share as P; print(P.SETTING); print(P.person_name()); print(P.read_plan())"
                % os.path.join(PLUG, "bin"))
        o = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code], capture_output=True, text=True, encoding="utf-8",
                           env=self.env(), timeout=60)   # Windows: the child prints the non-ASCII name; its default code page is not UTF-8 (-I ignores PYTHONUTF8)
        self.assertEqual(o.stdout.split("\n")[:3], [self.setting, "Gül Ersoy", "100"], o.stdout + o.stderr)
        if POSIX:
            self.assertIn("ExecStart=%s %s --save --share\n" % (sys.executable, os.path.join(self.claude, "bin", "cost.py")), self.service())
        self.assertTrue(self.daily_shares("--share", "--person", "Gül Ersoy", "--plan", "100"))
        self.assertIn("[OK] name is in people.json, push access present", r.stdout)
        self.assertIn("row:", r.stdout)                                # the existing dry run printed the row

    def test_name_defaults_to_git_user_name_and_plan_is_required(self):
        with open(self.gitconfig, "w") as f:
            f.write("[user]\n\tname = Emre\n")
        r = self.install("--share")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("no plan set", r.stdout)
        self.assertFalse(os.path.exists(self.setting))
        r = self.install("--share", "--plan", "200")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rd(os.path.join(self.setting, "person")), "Emre\n")
        self.assertEqual(rd(os.path.join(self.setting, "plan")), "200\n")

    def test_name_not_in_the_list_fails_in_turkish_with_the_valid_names_and_writes_nothing(self):
        with open(self.gitconfig, "w") as f:
            f.write("[user]\n\tname = example-tester-1\n")
        r = self.install("--share", "--plan", "100")                         # git name differs from the list: the Gül case
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("is not in the people.json list", r.stdout)
        self.assertIn("Gül Ersoy", r.stdout)
        self.assertIn("--person", r.stdout)
        r = self.install("--share", "--person", "Nobody Listed", "--plan", "100")
        self.assertEqual(r.returncode, 2, r.stdout)
        for name in ("Emre", "Kerem Ali Koç", "Selim Akçay"):
            self.assertIn(name, r.stdout)
        self.assertEqual(os.listdir(self.home), ["gitconfig-bos"])         # nothing written: no ~/.claude, no ~/.config

    def test_bad_plan_and_misplaced_options_are_rejected(self):
        for plan in ("150", "abc", ""):
            r = self.install("--share", "--person", "Emre", "--plan", plan)
            self.assertEqual(r.returncode, 2, (plan, r.stdout))
            self.assertIn("invalid plan", r.stdout)
        r = self.install("--person", "Emre")
        self.assertEqual(r.returncode, 2)
        self.assertIn("only given together with --share", r.stdout)
        r = self.install("--share", "--free", "--person", "Emre", "--plan", "100")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(os.listdir(self.home), ["gitconfig-bos"])

    def test_dollar_sign_plan_is_accepted(self):
        r = self.install("--share", "--person", "Emre", "--plan", "$200")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rd(os.path.join(self.setting, "plan")), "200\n")

    def test_no_push_access_stops_before_anything_is_installed(self):
        r = self.install("--share", "--person", "Emre", "--plan", "100", CLAUDE_SHARED_REMOTE=os.path.join(self.root, "missing.git"))
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("gh auth login", r.stdout)
        self.assertIn("gh auth setup-git", r.stdout)
        self.assertIn("Nothing was installed", r.stdout)
        self.assertFalse(os.path.exists(self.setting))
        self.assertFalse(os.path.exists(os.path.join(self.claude, "bin", "cost.py")))
        self.assertFalse(os.path.exists(os.path.join(self.claude, "settings.json")))
        self.assertFalse(os.path.exists(os.path.join(self.udir, "cost-daily.service")))

    def test_name_missing_from_main_of_the_shared_repo_stops_too(self):
        r = self.install("--share", "--person", "Emre", "--plan", "100", CLAUDE_SHARED_REMOTE=self.bos)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("is not in people.json on the main branch of the shared repo", r.stdout)
        self.assertFalse(os.path.exists(self.setting))

    def test_without_share_nothing_changes_and_reinstall_warns_when_the_job_used_to_share(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.setting))
        self.assertFalse(self.daily_shares())
        self.assertTrue(self.daily_shares("--share", "--person", "Emre", "--plan", "100"))
        if POSIX:
            r = self.install()                                                # plain re-install after joining
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("WARNING: the previous install's runlog job was sharing", r.stdout)
        else:
            self.assertFalse(self.daily_shares())                    # plain re-install after joining
            self.assertIn("WARNING: the previous install's runlog job was sharing", self.last)
            self.assertFalse(self.daily_shares())
            self.assertNotIn("WARNING", self.last)                           # the task no longer shares: nothing to warn about
        self.assertFalse(self.daily_shares())

    def test_rerun_is_idempotent(self):
        a = ("--share", "--person", "Emre", "--plan", "100")
        self.assertEqual(self.install(*a).returncode, 0)
        before = self.service() if POSIX else None
        r = self.install(*a)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        if POSIX:
            self.assertEqual(self.service(), before)
        self.assertIn("same: %s" % os.path.join(self.setting, "person").replace("\\", "/"), r.stdout)
        self.assertEqual(sorted(os.listdir(self.setting)), ["person", "plan"])  # no backup copies of identical files

    def test_bare_rerun_reuses_the_saved_sharing_flags(self):
        self.assertEqual(self.install_raw("--share", "--person", "Emre", "--plan", "100").returncode, 0)
        saved = json.loads(rd(os.path.join(self.home, ".claude", "quota-pace.json")))["setup"]
        self.assertEqual(saved, ["--share", "--person", "Emre", "--plan", "100"])
        r = self.install_raw()                                                # nothing given: the saved options apply
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Keeping the saved install flags", r.stdout)
        self.assertEqual(rd(os.path.join(self.setting, "person")).strip(), "Emre")
        self.assertEqual(json.loads(rd(os.path.join(self.home, ".claude", "quota-pace.json")))["setup"], saved)

    def test_whatif_validates_but_writes_and_checks_nothing(self):
        r = self.install("--whatif", "--share", "--person", "Nobody Listed", "--plan", "100")
        self.assertEqual(r.returncode, 2, r.stdout)
        r = self.install("--whatif", "--share", "--person", "Emre", "--plan", "100", CLAUDE_SHARED_REMOTE=os.path.join(self.root, "missing.git"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)             # no access probe under --whatif
        self.assertIn("Joining the ranking: name 'Emre', plan $100", r.stdout)
        self.assertEqual(os.listdir(self.home), ["gitconfig-bos"])

    def test_remove_keeps_the_ranking_settings(self):
        self.assertEqual(self.install("--share", "--person", "Emre", "--plan", "100").returncode, 0)
        r = self.install("--remove")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.udir, "cost-daily.service")))
        self.assertEqual(rd(os.path.join(self.setting, "person")), "Emre\n")
        self.assertIn("The ranking settings", r.stdout)

    @unittest.skipUnless(POSIX and shutil.which("bash"), "bash only")
    def test_install_sh_parses_the_new_options(self):
        sh = os.path.join(PLUG, "install.sh")
        run = lambda *a: subprocess.run(["bash", sh, *a], capture_output=True, text=True, encoding="utf-8", env=self.env(), timeout=300)
        r = run("--whatif", "--share", "--person", "Gül Ersoy", "--plan", "100")   # --whatif needs no jq
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Joining the ranking: name 'Gül Ersoy', plan $100", r.stdout)   # the quoted name arrives as ONE argument
        r = run("--whatif", "--share", "--person")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--person needs a value", r.stdout)
        r = run("--whatif", "--bilinmeyen")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--share", r.stdout)


@unittest.skipUnless(POSIX, "launchd agent emulation: the POSIX installer branch")
class Launchd(unittest.TestCase):
    """The macOS daily job. Linux has no launchctl, so the runner makes install_common think it is on macOS (`macos()`), points
    real_home() at the scratch HOME for the 'real profile' cases and records every launchctl call instead of running one."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="install-launchd-")
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self.claude = os.path.join(self.home, ".claude")
        self.plist = os.path.join(self.home, "Library", "LaunchAgents", LABEL + ".plist")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_mac(self, *args, real=True, env=None):
        code = ("import sys, json; from pathlib import Path; sys.path.insert(0, %r); import install_common; calls = []; "
                "install_common.macos = lambda: True; %s"
                "install_common.launchctl = lambda a, quiet=False: calls.append(a) or True; "
                "sys.argv = ['install_common', '--python', %r] + %r; rc = install_common.main(); print('CALLS' + json.dumps(calls)); sys.exit(rc)"
                % (PLUG, "install_common.real_home = lambda: Path(%r); install_common.claude_plugin = lambda *a, **k: (True, 'ok'); " % self.home if real else "", sys.executable, copy(args)))
        e = clean_env(HOME=self.home, USERPROFILE=self.home)
        e.update(env or {})
        r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, encoding="utf-8", env=e, timeout=600)
        calls = [json.loads(l[5:]) for l in r.stdout.splitlines() if l.startswith("CALLS")]
        return r, (calls[0] if calls else None)

    def agent(self):
        with open(self.plist, "rb") as f:
            return plistlib.load(f)

    def test_plist_content_matches_the_linux_schedule_and_command(self):
        sys.path.insert(0, PLUG)
        import install_common
        from pathlib import Path
        claude = Path("/Users/x/.claude")
        script = claude / "bin" / "cost.py"
        d = plistlib.loads(install_common.generate_plist("/usr/bin/python3", script, claude, False).encode("utf-8"))
        self.assertEqual(d["Label"], LABEL)
        self.assertEqual(d["ProgramArguments"], ["/usr/bin/python3", "-I", "/Users/x/.claude/bin/cost.py", "--save"])
        self.assertEqual(d["StartCalendarInterval"], [{"Hour": h, "Minute": 23} for h in range(0, 24, 2)])   # same 2-hourly :23 as cost-daily.timer
        self.assertEqual((d["Nice"], d["LowPriorityIO"], d["ProcessType"]), (19, True, "Background"))
        self.assertIn("/opt/homebrew/bin", d["EnvironmentVariables"]["PATH"])
        self.assertNotIn("RunAtLoad", d)                                              # not on every login: only at its times
        d = plistlib.loads(install_common.generate_plist("/usr/bin/python3", script, claude, True, {"COST_TZ": "Europe/Istanbul"}).encode("utf-8"))
        self.assertEqual(d["ProgramArguments"][-2:], ["--save", "--share"])
        self.assertEqual(d["EnvironmentVariables"]["COST_TZ"], "Europe/Istanbul")
        self.assertEqual(install_common.generate_plist("/p", script, claude, True), install_common.generate_plist("/p", script, claude, True))   # deterministic

    def test_real_profile_writes_the_plist_and_loads_it_idempotently(self):
        domain = "gui/%d" % os.getuid()
        r, calls = self.run_mac("--cost")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = self.agent()
        self.assertEqual(d["ProgramArguments"], [sys.executable, "-I", os.path.join(self.claude, "bin", "cost.py"), "--save"])
        self.assertEqual(calls, [["bootout", domain + "/" + LABEL], ["bootstrap", domain, self.plist]])
        self.assertNotIn("no timer (Linux", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "systemd")))
        r, calls = self.run_mac("--cost")                                          # second install: same file, reloaded once
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("same: %s" % self.plist, r.stdout)
        self.assertEqual(calls, [["bootout", domain + "/" + LABEL], ["bootstrap", domain, self.plist]])
        self.assertEqual(os.listdir(os.path.dirname(self.plist)), [LABEL + ".plist"])   # no backups, no second agent

    def test_share_reinstall_changes_the_agent_and_plain_reinstall_warns(self):
        self.assertEqual(self.run_mac("--cost", real=False)[0].returncode, 0)
        self.assertNotIn("--share", self.agent()["ProgramArguments"])
        # sharing needs the access probe: point it at a throwaway local bare repo that lists the name
        remote = os.path.join(self.root, "remote.git")
        git("init", "-q", "--bare", "-b", "main", remote)
        t = os.path.join(self.root, "seed")
        git("clone", "-q", remote, t)
        os.makedirs(os.path.join(t, "plugins", "quota-pace"))
        shutil.copy(PEOPLE, os.path.join(t, "plugins", "quota-pace", "people.json"))
        git("add", "-A", cwd=t)
        git("commit", "-q", "-m", "seed", cwd=t)
        git("push", "-q", "origin", "HEAD:main", cwd=t)
        env = {"CLAUDE_SHARED_REMOTE": remote, "GIT_CONFIG_GLOBAL": os.devnull}
        r, _ = self.run_mac("--share", "--person", "Emre", "--plan", "100", real=False, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.agent()["ProgramArguments"][-2:], ["--save", "--share"])
        self.assertEqual(rd(os.path.join(self.home, ".config", "claude-shared", "person")), "Emre\n")
        r, _ = self.run_mac("--cost", real=False)
        self.assertIn("WARNING: the previous install's runlog job was sharing", r.stdout)
        self.assertNotIn("--share", self.agent()["ProgramArguments"])

    def test_scratch_home_never_reaches_launchctl(self):
        r, calls = self.run_mac("--cost", real=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(calls, [])
        self.assertIn("launchctl not run", r.stdout)
        self.assertTrue(os.path.isfile(self.plist))                                   # the file is still rendered

    def test_whatif_writes_no_plist_and_calls_nothing(self):
        r, calls = self.run_mac("--cost", "--whatif")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(calls, [])
        self.assertIn("[-WhatIf] write: %s" % self.plist, r.stdout)
        self.assertEqual(os.listdir(self.home), [])

    def test_remove_boots_out_and_deletes_the_plist_but_keeps_ranking_settings(self):
        domain = "gui/%d" % os.getuid()
        setting = os.path.join(self.home, ".config", "claude-shared")
        os.makedirs(setting)
        with open(os.path.join(setting, "person"), "w") as f:
            f.write("Emre\n")
        self.assertEqual(self.run_mac("--cost")[0].returncode, 0)
        r, calls = self.run_mac("--remove")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(calls, [["bootout", domain + "/" + LABEL]])
        self.assertFalse(os.path.exists(self.plist))
        self.assertTrue(os.path.exists(os.path.join(setting, "person")))


class WindowsTaskArguments(unittest.TestCase):
    """The Task Scheduler snippet takes the sharing flag from an environment value (no PowerShell needed to prove the plumbing)."""

    def test_snippet_appends_the_extra_argument_and_task_run_cmd_sets_it(self):
        sys.path.insert(0, PLUG)
        import install_common
        from unittest import mock
        self.assertIn("'\" --save' + $env:INSTALL_TASK_EXTRA", install_common.TASK_RECORD_PS)
        seen = []

        def fake_run(cmd, **kw):
            seen.append(kw["env"].get("INSTALL_TASK_EXTRA"))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(install_common.subprocess, "run", fake_run):
            install_common.run_cmd_task("pwsh", install_common.TASK_RECORD_PS, "pythonw.exe", "C:\\x\\cost.py")
            install_common.run_cmd_task("pwsh", install_common.TASK_RECORD_PS, "pythonw.exe", "C:\\x\\cost.py", share=True)
        self.assertEqual(seen, ["", " --share"])

    def test_every_task_snippet_runs_through_the_headless_conhost_prefix(self):
        """One code path: all five registration snippets register through Register-HiddenTask (tools/hidden.ps1), take the interpreter args from INSTALL_TASK_ON (task_env), none hardcodes '-I' or builds its own action/principal."""
        sys.path.insert(0, PLUG)
        import install_common
        for name in ("TASK_RECORD_PS", "LOST_TASK_RECORD_PS", "SP_TASK_RECORD_PS", "SG_TASK_RECORD_PS", "ST_TASK_RECORD_PS"):
            ps = getattr(install_common, name)
            self.assertIn("-Argument ($env:INSTALL_TASK_ON + ' \"'", ps, name)
            self.assertNotIn("-Argument ('-I", ps, name)
            self.assertIn("Register-HiddenTask", ps, name)
            for own in ("New-ScheduledTaskAction", "New-ScheduledTaskPrincipal", "Register-ScheduledTask", "conhost"):
                self.assertNotIn(own, ps, name)

    def test_hidden_ps1_work_the_one_hidden_task_launcher(self):
        """tools/hidden.ps1 owns the headless conhost action, the pythonw fallback and the per-user principal; task_env only names it."""
        sys.path.insert(0, PLUG)
        import install_common
        with open(install_common.HIDDEN_PS1, encoding="utf-8") as f:
            ps = f.read()
        for need in ("function New-HiddenAction", "function Register-HiddenTask", "--headless", "conhost.exe", "pythonw.exe",
                     "-RunLevel Limited", "-LogonType Interactive", "Register-ScheduledTask"):
            self.assertIn(need, ps)
        for bad in ("Highest", "SYSTEM", "-Password", "schtasks"):
            self.assertNotIn(bad, ps)
        e = install_common.task_env(r"C:\Py\python.exe")
        self.assertEqual((e["INSTALL_TASK_EXE"], e["INSTALL_TASK_ON"]), (r"C:\Py\python.exe", "-I"))
        self.assertTrue(os.path.isfile(e["INSTALL_HIDDEN_PS1"]), e)

    def test_ps1_wrapper_passes_the_options_through(self):
        with open(os.path.join(PLUG, "install.ps1"), encoding="ascii") as f:        # ASCII only: Windows PowerShell 5.1 reads it without a BOM
            ps = f.read()
        for need in ("[switch]$Share", "[string]$Person", "[string]$Plan", "'--share'", "'--person'", "'--plan'"):
            self.assertIn(need, ps)


if __name__ == "__main__":
    unittest.main()

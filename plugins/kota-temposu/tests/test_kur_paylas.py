"""Tests for the installer's `--paylas` (join the usage ranking in one command) and the macOS launchd agent (kur_ortak.py).

Run:  python3 -I -m unittest discover -s plugins/kota-temposu/tests     (from the repo root)

Everything runs under a scratch HOME. The "shared repo" is a local bare repository (CLAUDE_SHARED_UZAK), so the access check is
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


def kopya(args):
    """The installer runs from the clone by default; these tests exercise the legacy copy mode unless they ask for --repodan."""
    a = list(args)
    return a if {"--repodan", "--kopya", "--remove", "--sinama"} & set(a) else ["--kopya", *a]
sys.path.insert(0, HERE)
from test_kur_maliyet import PWSH, clean_env, rd  # noqa: E402

KISILER = os.path.join(HERE, "fixtures", "kisiler-ornek.json")   # example people list of the shared repo
# The installer checks the name against the people list of its own clone first. The kit ships none (a team's fork carries it), so
# these tests run the installer from a scratch copy of the kit that holds the example list; the source tree is never written.
_KOPYA = tempfile.mkdtemp(prefix="kit-klon-")
atexit.register(shutil.rmtree, _KOPYA, True)
shutil.copytree(os.path.normpath(os.path.join(HERE, "..", "..", "..")), os.path.join(_KOPYA, "kit"),
                ignore=shutil.ignore_patterns(".git", "__pycache__"))
PLUG = os.path.join(_KOPYA, "kit", "plugins", "kota-temposu")
shutil.copy(KISILER, os.path.join(PLUG, "kisiler.json"))
KUR = os.path.join(PLUG, "kur_ortak.py")

POSIX = os.name != "nt"
ETIKET = "com.agentcrewkit.maliyet-gunluk"


def git(*args, cwd=None):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class Katilim(unittest.TestCase):
    """kur_ortak.py --paylas end to end on a POSIX-style install (Linux branch: systemd unit files under a scratch HOME)."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="kur-paylas-")
        cls.uzak = os.path.join(cls.root, "uzak.git")        # the "shared repo": main holds the real kisiler.json
        git("init", "-q", "--bare", "-b", "main", cls.uzak)
        tohum = os.path.join(cls.root, "tohum")
        git("clone", "-q", cls.uzak, tohum)
        os.makedirs(os.path.join(tohum, "plugins", "kota-temposu"))
        shutil.copy(KISILER, os.path.join(tohum, "plugins", "kota-temposu", "kisiler.json"))
        git("add", "-A", cwd=tohum)
        git("commit", "-q", "-m", "seed", cwd=tohum)
        git("push", "-q", "origin", "HEAD:main", cwd=tohum)
        cls.bos = os.path.join(cls.root, "bos.git")           # a repo whose main does not list anybody we use
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
        self.fakebin = os.path.join(self.home, "..", "fakebin-" + os.path.basename(self.home))
        os.makedirs(self.fakebin)
        self.calls = os.path.join(self.fakebin, "systemctl-calls")
        fake = os.path.join(self.fakebin, "systemctl")
        with open(fake, "w") as f:
            f.write('#!/bin/sh\necho "$@" >> "%s"\nexit 0\n' % self.calls)
        os.chmod(fake, 0o755)
        self.claude = os.path.join(self.home, ".claude")
        self.ayar = os.path.join(self.home, ".config", "claude-shared")
        self.udir = os.path.join(self.home, ".config", "systemd", "user")
        self.gitconfig = os.path.join(self.home, "gitconfig-bos")
        open(self.gitconfig, "w").close()

    def env(self, **extra):
        e = clean_env(HOME=self.home, USERPROFILE=self.home, PATH=self.fakebin + os.pathsep + os.environ.get("PATH", ""),
                      CLAUDE_SHARED_UZAK=self.uzak, GIT_CONFIG_GLOBAL=self.gitconfig, GIT_CONFIG_SYSTEM=os.devnull)
        for k in ("CLAUDE_SHARED_KISI", "CLAUDE_SHARED_PLAN", "CLAUDE_SHARED_KLON", "CLAUDE_SHARED_KOK"):
            e.pop(k, None)
        e.update(extra)
        return e

    def kur(self, *args, **extra):
        return self.kur_ham(*kopya(args), **extra)

    def kur_ham(self, *args, **extra):
        win = ["--windows", "--shell", PWSH] if os.name == "nt" and PWSH else []
        return subprocess.run([sys.executable, "-I", KUR, *win, "--python", sys.executable, *args], capture_output=True, text=True,
                              encoding="utf-8", env=self.env(**extra), timeout=600)

    def servis(self):
        return rd(os.path.join(self.udir, "maliyet-gunluk.service"))

    def gunluk_paylasiyor(self, *args):
        """Whether the daily job shares after an install with ARGS. POSIX: the systemd unit's command line. Windows: Task Scheduler
        is never reached from a scratch profile, so the install runs with gercek_ev() patched to the scratch HOME and
        gorev_calistir() replaced by a recorder that keeps the `paylas` flag the real task registration would get (in a state file,
        which also answers gorev_paylasiyor(), the "was the registered task sharing" query). self.son = the installer output."""
        if POSIX:
            self.assertEqual(self.kur(*args).returncode, 0)
            return "--kaydet --paylas\n" in self.servis()
        code = ("import sys, json; from pathlib import Path; sys.path.insert(0, %r); import kur_ortak; calls = []; "
                "kur_ortak.gercek_ev = lambda: Path(%r); kur_ortak.claude_plugin = lambda *a, **k: (True, 'ok'); "
                "dur = Path(%r); kur_ortak.gorev_paylasiyor = lambda shell: dur.exists() and dur.read_text() == '1'; "
                "kur_ortak.gorev_calistir = lambda shell, ps, exe, script, **kw: kw.get('ad') == 'kayip-is' or (calls.append(bool(kw.get('paylas'))), "
                "dur.write_text('1' if kw.get('paylas') else '0'), True)[-1]; "
                "sys.argv = ['kur_ortak', '--windows', '--python', %r, '--shell', %r] + %r; rc = kur_ortak.main(); "
                "print('CALLS' + json.dumps(calls)); sys.exit(rc)" % (PLUG, self.home, os.path.join(self.root, "gorev-durum-" + os.path.basename(self.home)), sys.executable, PWSH, kopya(args)))
        r = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code], capture_output=True, text=True, encoding="utf-8",
                           env=self.env(), timeout=600)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.son = r.stdout
        calls = [json.loads(l[5:]) for l in r.stdout.splitlines() if l.startswith("CALLS")]
        self.assertEqual(len(calls), 1, r.stdout)
        self.assertEqual(len(calls[0]), 1, r.stdout)                     # exactly one task registration
        return calls[0][0]

    def test_good_name_and_plan_write_the_files_the_job_reads_and_the_daily_job_shares(self):
        r = self.kur("--paylas", "--kisi", "Gül Ersoy", "--plan", "100")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rd(os.path.join(self.ayar, "kisi")), "Gül Ersoy\n")
        self.assertEqual(rd(os.path.join(self.ayar, "plan")), "100\n")
        with open(os.path.join(self.ayar, "kisi"), "rb") as f:
            self.assertFalse(f.read().startswith(b"\xef\xbb\xbf"))      # no BOM: dosyadan_oku would keep it in the name
        # the job's own readers, started under the same HOME/USERPROFILE, find the name and the plan in exactly these files
        code = ("import sys; sys.path.insert(0, %r); import maliyet_paylas as P; print(P.AYAR); print(P.kisi_adi()); print(P.plan_oku())"
                % os.path.join(PLUG, "bin"))
        o = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code], capture_output=True, text=True, encoding="utf-8",
                           env=self.env(), timeout=60)   # Windows: the child prints the non-ASCII name; its default code page is not UTF-8 (-I ignores PYTHONUTF8)
        self.assertEqual(o.stdout.split("\n")[:3], [self.ayar, "Gül Ersoy", "100"], o.stdout + o.stderr)
        if POSIX:
            self.assertIn("ExecStart=%s %s --kaydet --paylas\n" % (sys.executable, os.path.join(self.claude, "bin", "maliyet.py")), self.servis())
        self.assertTrue(self.gunluk_paylasiyor("--paylas", "--kisi", "Gül Ersoy", "--plan", "100"))
        self.assertIn("[OK] ad kisiler.json'da, push yetkisi var", r.stdout)
        self.assertIn("satir:", r.stdout)                                # the existing dry run printed the row

    def test_name_defaults_to_git_user_name_and_plan_is_required(self):
        with open(self.gitconfig, "w") as f:
            f.write("[user]\n\tname = Emre\n")
        r = self.kur("--paylas")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("plan belli degil", r.stdout)
        self.assertFalse(os.path.exists(self.ayar))
        r = self.kur("--paylas", "--plan", "200")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rd(os.path.join(self.ayar, "kisi")), "Emre\n")
        self.assertEqual(rd(os.path.join(self.ayar, "plan")), "200\n")

    def test_name_not_in_the_list_fails_in_turkish_with_the_valid_names_and_writes_nothing(self):
        with open(self.gitconfig, "w") as f:
            f.write("[user]\n\tname = example-tester-1\n")
        r = self.kur("--paylas", "--plan", "100")                         # git name differs from the list: the Gül case
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("kisiler.json listesinde yok", r.stdout)
        self.assertIn("Gül Ersoy", r.stdout)
        self.assertIn("--kisi", r.stdout)
        r = self.kur("--paylas", "--kisi", "Yok Kisi", "--plan", "100")
        self.assertEqual(r.returncode, 2, r.stdout)
        for ad in ("Emre", "Kerem Ali Koç", "Selim Akçay"):
            self.assertIn(ad, r.stdout)
        self.assertEqual(os.listdir(self.home), ["gitconfig-bos"])         # nothing written: no ~/.claude, no ~/.config

    def test_bad_plan_and_misplaced_options_are_rejected(self):
        for plan in ("150", "abc", ""):
            r = self.kur("--paylas", "--kisi", "Emre", "--plan", plan)
            self.assertEqual(r.returncode, 2, (plan, r.stdout))
            self.assertIn("gecersiz plan", r.stdout)
        r = self.kur("--kisi", "Emre")
        self.assertEqual(r.returncode, 2)
        self.assertIn("yalniz --paylas ile", r.stdout)
        r = self.kur("--paylas", "--maliyetsiz", "--kisi", "Emre", "--plan", "100")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(os.listdir(self.home), ["gitconfig-bos"])

    def test_dollar_sign_plan_is_accepted(self):
        r = self.kur("--paylas", "--kisi", "Emre", "--plan", "$200")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rd(os.path.join(self.ayar, "plan")), "200\n")

    def test_no_push_access_stops_before_anything_is_installed(self):
        r = self.kur("--paylas", "--kisi", "Emre", "--plan", "100", CLAUDE_SHARED_UZAK=os.path.join(self.root, "yok.git"))
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("gh auth login", r.stdout)
        self.assertIn("gh auth setup-git", r.stdout)
        self.assertIn("Hicbir sey kurulmadi", r.stdout)
        self.assertFalse(os.path.exists(self.ayar))
        self.assertFalse(os.path.exists(os.path.join(self.claude, "bin", "maliyet.py")))
        self.assertFalse(os.path.exists(os.path.join(self.claude, "settings.json")))
        self.assertFalse(os.path.exists(os.path.join(self.udir, "maliyet-gunluk.service")))

    def test_name_missing_from_main_of_the_shared_repo_stops_too(self):
        r = self.kur("--paylas", "--kisi", "Emre", "--plan", "100", CLAUDE_SHARED_UZAK=self.bos)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("main dalindaki kisiler.json icinde yok", r.stdout)
        self.assertFalse(os.path.exists(self.ayar))

    def test_without_paylas_nothing_changes_and_reinstall_warns_when_the_job_used_to_share(self):
        r = self.kur()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.ayar))
        self.assertFalse(self.gunluk_paylasiyor())
        self.assertTrue(self.gunluk_paylasiyor("--paylas", "--kisi", "Emre", "--plan", "100"))
        if POSIX:
            r = self.kur()                                                # plain re-install after joining
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("UYARI: onceki kurulumda gunluk is paylasiyordu", r.stdout)
        else:
            self.assertFalse(self.gunluk_paylasiyor())                    # plain re-install after joining
            self.assertIn("UYARI: onceki kurulumda gunluk is paylasiyordu", self.son)
            self.assertFalse(self.gunluk_paylasiyor())
            self.assertNotIn("UYARI", self.son)                           # the task no longer shares: nothing to warn about
        self.assertFalse(self.gunluk_paylasiyor())

    def test_rerun_is_idempotent(self):
        a = ("--paylas", "--kisi", "Emre", "--plan", "100")
        self.assertEqual(self.kur(*a).returncode, 0)
        before = self.servis() if POSIX else None
        r = self.kur(*a)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        if POSIX:
            self.assertEqual(self.servis(), before)
        self.assertIn("ayni: %s" % os.path.join(self.ayar, "kisi").replace("\\", "/"), r.stdout)
        self.assertEqual(sorted(os.listdir(self.ayar)), ["kisi", "plan"])  # no backup copies of identical files

    def test_bare_rerun_reuses_the_saved_sharing_flags(self):
        self.assertEqual(self.kur_ham("--paylas", "--kisi", "Emre", "--plan", "100").returncode, 0)
        saved = json.loads(rd(os.path.join(self.home, ".claude", "kota-temposu.json")))["kurulum"]
        self.assertEqual(saved, ["--paylas", "--kisi", "Emre", "--plan", "100"])
        r = self.kur_ham()                                                # nothing given: the saved options apply
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Kayitli kurulum bayraklari", r.stdout)
        self.assertEqual(rd(os.path.join(self.ayar, "kisi")).strip(), "Emre")
        self.assertEqual(json.loads(rd(os.path.join(self.home, ".claude", "kota-temposu.json")))["kurulum"], saved)

    def test_whatif_validates_but_writes_and_checks_nothing(self):
        r = self.kur("--whatif", "--paylas", "--kisi", "Yok Kisi", "--plan", "100")
        self.assertEqual(r.returncode, 2, r.stdout)
        r = self.kur("--whatif", "--paylas", "--kisi", "Emre", "--plan", "100", CLAUDE_SHARED_UZAK=os.path.join(self.root, "yok.git"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)             # no access probe under --whatif
        self.assertIn("Siralamaya katilim: ad 'Emre', plan $100", r.stdout)
        self.assertEqual(os.listdir(self.home), ["gitconfig-bos"])

    def test_remove_keeps_the_ranking_settings(self):
        self.assertEqual(self.kur("--paylas", "--kisi", "Emre", "--plan", "100").returncode, 0)
        r = self.kur("--remove")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.udir, "maliyet-gunluk.service")))
        self.assertEqual(rd(os.path.join(self.ayar, "kisi")), "Emre\n")
        self.assertIn("Siralama ayarlari", r.stdout)

    @unittest.skipUnless(POSIX and shutil.which("bash"), "bash only")
    def test_kur_sh_parses_the_new_options(self):
        sh = os.path.join(PLUG, "kur.sh")
        run = lambda *a: subprocess.run(["bash", sh, *a], capture_output=True, text=True, encoding="utf-8", env=self.env(), timeout=300)
        r = run("--whatif", "--paylas", "--kisi", "Gül Ersoy", "--plan", "100")   # --whatif needs no jq
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Siralamaya katilim: ad 'Gül Ersoy', plan $100", r.stdout)   # the quoted name arrives as ONE argument
        r = run("--whatif", "--paylas", "--kisi")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--kisi bir deger ister", r.stdout)
        r = run("--whatif", "--bilinmeyen")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--paylas", r.stdout)


@unittest.skipUnless(POSIX, "launchd agent emulation: the POSIX installer branch")
class Launchd(unittest.TestCase):
    """The macOS daily job. Linux has no launchctl, so the runner makes kur_ortak think it is on macOS (`macos()`), points
    gercek_ev() at the scratch HOME for the 'real profile' cases and records every launchctl call instead of running one."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="kur-launchd-")
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self.claude = os.path.join(self.home, ".claude")
        self.plist = os.path.join(self.home, "Library", "LaunchAgents", ETIKET + ".plist")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_mac(self, *args, real=True, env=None):
        code = ("import sys, json; from pathlib import Path; sys.path.insert(0, %r); import kur_ortak; calls = []; "
                "kur_ortak.macos = lambda: True; %s"
                "kur_ortak.launchctl = lambda a, sessiz=False: calls.append(a) or True; "
                "sys.argv = ['kur_ortak', '--python', %r] + %r; rc = kur_ortak.main(); print('CALLS' + json.dumps(calls)); sys.exit(rc)"
                % (PLUG, "kur_ortak.gercek_ev = lambda: Path(%r); kur_ortak.claude_plugin = lambda *a, **k: (True, 'ok'); " % self.home if real else "", sys.executable, kopya(args)))
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
        import kur_ortak
        from pathlib import Path
        claude = Path("/Users/x/.claude")
        script = claude / "bin" / "maliyet.py"
        d = plistlib.loads(kur_ortak.plist_uret("/usr/bin/python3", script, claude, False).encode("utf-8"))
        self.assertEqual(d["Label"], ETIKET)
        self.assertEqual(d["ProgramArguments"], ["/usr/bin/python3", "-I", "/Users/x/.claude/bin/maliyet.py", "--kaydet"])
        self.assertEqual(d["StartCalendarInterval"], [{"Hour": h, "Minute": 23} for h in range(0, 24, 2)])   # same 2-hourly :23 as maliyet-gunluk.timer
        self.assertEqual((d["Nice"], d["LowPriorityIO"], d["ProcessType"]), (19, True, "Background"))
        self.assertIn("/opt/homebrew/bin", d["EnvironmentVariables"]["PATH"])
        self.assertNotIn("RunAtLoad", d)                                              # not on every login: only at its times
        d = plistlib.loads(kur_ortak.plist_uret("/usr/bin/python3", script, claude, True, {"MALIYET_TZ": "Europe/Istanbul"}).encode("utf-8"))
        self.assertEqual(d["ProgramArguments"][-2:], ["--kaydet", "--paylas"])
        self.assertEqual(d["EnvironmentVariables"]["MALIYET_TZ"], "Europe/Istanbul")
        self.assertEqual(kur_ortak.plist_uret("/p", script, claude, True), kur_ortak.plist_uret("/p", script, claude, True))   # deterministic

    def test_real_profile_writes_the_plist_and_loads_it_idempotently(self):
        domain = "gui/%d" % os.getuid()
        r, calls = self.run_mac("--maliyet")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = self.agent()
        self.assertEqual(d["ProgramArguments"], [sys.executable, "-I", os.path.join(self.claude, "bin", "maliyet.py"), "--kaydet"])
        self.assertEqual(calls, [["bootout", domain + "/" + ETIKET], ["bootstrap", domain, self.plist]])
        self.assertNotIn("zamanlayici yok", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "systemd")))
        r, calls = self.run_mac("--maliyet")                                          # second install: same file, reloaded once
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ayni: %s" % self.plist, r.stdout)
        self.assertEqual(calls, [["bootout", domain + "/" + ETIKET], ["bootstrap", domain, self.plist]])
        self.assertEqual(os.listdir(os.path.dirname(self.plist)), [ETIKET + ".plist"])   # no backups, no second agent

    def test_paylas_reinstall_changes_the_agent_and_plain_reinstall_warns(self):
        self.assertEqual(self.run_mac("--maliyet", real=False)[0].returncode, 0)
        self.assertNotIn("--paylas", self.agent()["ProgramArguments"])
        # sharing needs the access probe: point it at a throwaway local bare repo that lists the name
        uzak = os.path.join(self.root, "uzak.git")
        git("init", "-q", "--bare", "-b", "main", uzak)
        t = os.path.join(self.root, "tohum")
        git("clone", "-q", uzak, t)
        os.makedirs(os.path.join(t, "plugins", "kota-temposu"))
        shutil.copy(KISILER, os.path.join(t, "plugins", "kota-temposu", "kisiler.json"))
        git("add", "-A", cwd=t)
        git("commit", "-q", "-m", "seed", cwd=t)
        git("push", "-q", "origin", "HEAD:main", cwd=t)
        env = {"CLAUDE_SHARED_UZAK": uzak, "GIT_CONFIG_GLOBAL": os.devnull}
        r, _ = self.run_mac("--paylas", "--kisi", "Emre", "--plan", "100", real=False, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.agent()["ProgramArguments"][-2:], ["--kaydet", "--paylas"])
        self.assertEqual(rd(os.path.join(self.home, ".config", "claude-shared", "kisi")), "Emre\n")
        r, _ = self.run_mac("--maliyet", real=False)
        self.assertIn("UYARI: onceki kurulumda gunluk is paylasiyordu", r.stdout)
        self.assertNotIn("--paylas", self.agent()["ProgramArguments"])

    def test_scratch_home_never_reaches_launchctl(self):
        r, calls = self.run_mac("--maliyet", real=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(calls, [])
        self.assertIn("launchctl calistirilmadi", r.stdout)
        self.assertTrue(os.path.isfile(self.plist))                                   # the file is still rendered

    def test_whatif_writes_no_plist_and_calls_nothing(self):
        r, calls = self.run_mac("--maliyet", "--whatif")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(calls, [])
        self.assertIn("[-WhatIf] yaz: %s" % self.plist, r.stdout)
        self.assertEqual(os.listdir(self.home), [])

    def test_remove_boots_out_and_deletes_the_plist_but_keeps_ranking_settings(self):
        domain = "gui/%d" % os.getuid()
        ayar = os.path.join(self.home, ".config", "claude-shared")
        os.makedirs(ayar)
        with open(os.path.join(ayar, "kisi"), "w") as f:
            f.write("Emre\n")
        self.assertEqual(self.run_mac("--maliyet")[0].returncode, 0)
        r, calls = self.run_mac("--remove")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(calls, [["bootout", domain + "/" + ETIKET]])
        self.assertFalse(os.path.exists(self.plist))
        self.assertTrue(os.path.exists(os.path.join(ayar, "kisi")))


class WindowsTaskArguments(unittest.TestCase):
    """The Task Scheduler snippet takes the sharing flag from an environment value (no PowerShell needed to prove the plumbing)."""

    def test_snippet_appends_the_extra_argument_and_gorev_calistir_sets_it(self):
        sys.path.insert(0, PLUG)
        import kur_ortak
        from unittest import mock
        self.assertIn("'\" --kaydet' + $env:KUR_GOREV_EK", kur_ortak.GOREV_KAYIT_PS)
        seen = []

        def fake_run(cmd, **kw):
            seen.append(kw["env"].get("KUR_GOREV_EK"))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(kur_ortak.subprocess, "run", fake_run):
            kur_ortak.gorev_calistir("pwsh", kur_ortak.GOREV_KAYIT_PS, "pythonw.exe", "C:\\x\\maliyet.py")
            kur_ortak.gorev_calistir("pwsh", kur_ortak.GOREV_KAYIT_PS, "pythonw.exe", "C:\\x\\maliyet.py", paylas=True)
        self.assertEqual(seen, ["", " --paylas"])

    def test_every_task_snippet_runs_through_the_headless_conhost_prefix(self):
        """One code path: all five registration snippets register through Register-GizliGorev (tools/gizli.ps1), take the interpreter args from KUR_GOREV_ON (gorev_env), none hardcodes '-I' or builds its own action/principal."""
        sys.path.insert(0, PLUG)
        import kur_ortak
        for ad in ("GOREV_KAYIT_PS", "KAYIP_GOREV_KAYIT_PS", "SP_GOREV_KAYIT_PS", "SG_GOREV_KAYIT_PS", "ST_GOREV_KAYIT_PS"):
            ps = getattr(kur_ortak, ad)
            self.assertIn("-Argument ($env:KUR_GOREV_ON + ' \"'", ps, ad)
            self.assertNotIn("-Argument ('-I", ps, ad)
            self.assertIn("Register-GizliGorev", ps, ad)
            for own in ("New-ScheduledTaskAction", "New-ScheduledTaskPrincipal", "Register-ScheduledTask", "conhost"):
                self.assertNotIn(own, ps, ad)

    def test_gizli_ps1_is_the_one_hidden_task_launcher(self):
        """tools/gizli.ps1 owns the headless conhost action, the pythonw fallback and the per-user principal; gorev_env only names it."""
        sys.path.insert(0, PLUG)
        import kur_ortak
        with open(kur_ortak.GIZLI_PS1, encoding="utf-8") as f:
            ps = f.read()
        for need in ("function New-GizliEylem", "function Register-GizliGorev", "--headless", "conhost.exe", "pythonw.exe",
                     "-RunLevel Limited", "-LogonType Interactive", "Register-ScheduledTask"):
            self.assertIn(need, ps)
        for bad in ("Highest", "SYSTEM", "-Password", "schtasks"):
            self.assertNotIn(bad, ps)
        e = kur_ortak.gorev_env(r"C:\Py\python.exe")
        self.assertEqual((e["KUR_GOREV_EXE"], e["KUR_GOREV_ON"]), (r"C:\Py\python.exe", "-I"))
        self.assertTrue(os.path.isfile(e["KUR_GIZLI_PS1"]), e)

    def test_ps1_wrapper_passes_the_options_through(self):
        with open(os.path.join(PLUG, "kur.ps1"), encoding="ascii") as f:        # ASCII only: Windows PowerShell 5.1 reads it without a BOM
            ps = f.read()
        for need in ("[switch]$Paylas", "[string]$Kisi", "[string]$Plan", "'--paylas'", "'--kisi'", "'--plan'"):
            self.assertIn(need, ps)


if __name__ == "__main__":
    unittest.main()

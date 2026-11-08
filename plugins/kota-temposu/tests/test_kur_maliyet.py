"""Tests for the optional `--maliyet` part of the kota-temposu installer (kur_ortak.py), always under a scratch HOME.

Run:  python3 -I -m unittest discover -s plugins/kota-temposu/tests     (from the repo root)

The installer must never touch the real ~/.claude or the real systemd user manager: every run here uses HOME=<temp>,
a fake `systemctl` first on PATH records any call (there must be none), and XDG_CONFIG_HOME / CLAUDE_CONFIG_DIR are unset.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))


def kopya(args):
    """The installer runs from the clone by default; these tests exercise the legacy copy mode unless they ask for --repodan."""
    a = list(args)
    return a if {"--repodan", "--kopya", "--remove", "--sinama"} & set(a) else ["--kopya", *a]
PLUG = os.path.normpath(os.path.join(HERE, ".."))
KUR = os.path.join(PLUG, "kur_ortak.py")
PWSH = shutil.which("pwsh") or shutil.which("powershell")


def clean_env(**extra):
    """The process environment minus everything that could redirect the tools to another config dir (test_maliyet.py
    sets some of these at import time when both files run in one process)."""
    env = {k: v for k, v in os.environ.items() if k not in ("XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "MALIYET_TZ", "MALIYET_FIYATLAR", "MALIYET_DURUM")}
    env.update(extra)
    return env


def rd(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


class Installer(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="kur-test-")
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self.fakebin = os.path.join(self.root, "fakebin")
        os.makedirs(self.fakebin)
        self.calls = os.path.join(self.root, "systemctl-calls")
        fake = os.path.join(self.fakebin, "systemctl")
        with open(fake, "w") as f:
            f.write('#!/bin/sh\necho "$@" >> "%s"\nexit 0\n' % self.calls)
        os.chmod(fake, 0o755)
        self.claude = os.path.join(self.home, ".claude")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def kur(self, *args):
        return self.kur_ham(*kopya(args))

    def kur_ham(self, *args):
        env = clean_env(HOME=self.home, USERPROFILE=self.home, PATH=self.fakebin + os.pathsep + os.environ.get("PATH", ""))
        win = ["--windows", "--shell", PWSH] if os.name == "nt" and PWSH else []   # Windows has no bash/jq guarantee: use the Windows branch
        return subprocess.run([sys.executable, "-I", KUR, *win, "--python", sys.executable, *args],
                              capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)

    def installed(self, rel):
        return os.path.exists(os.path.join(self.claude, rel))

    def test_default_install_includes_cost_statistics_and_the_agent_rows(self):
        r = self.kur()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for f in ("bin/maliyet.py", "bin/maliyet_oran.py", "bin/maliyet_artimli.py", "bin/subagent-statusline.py"):
            self.assertTrue(self.installed(f), f)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        sub = cfg["subagentStatusLine"]["command"]
        self.assertEqual(sub, "%s -I %s" % (sys.executable.replace("\\", "/"), os.path.join(self.claude, "bin", "subagent-statusline.py").replace("\\", "/")))
        self.assertIn("[OK] ajan satiri", r.stdout)                       # the self-check ran the written command
        self.assertEqual(self.kur().returncode, 0)                        # idempotent: one entry, no change message the second time
        self.assertEqual(json.loads(rd(os.path.join(self.claude, "settings.json")))["subagentStatusLine"]["command"], sub)

    def test_somebody_elses_subagent_status_line_is_left_alone_and_ours_is_removed(self):
        os.makedirs(self.claude)
        with open(os.path.join(self.claude, "settings.json"), "w") as f:
            json.dump({"subagentStatusLine": {"type": "command", "command": "my-own-thing"}}, f)
        r = self.kur()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(rd(os.path.join(self.claude, "settings.json")))["subagentStatusLine"]["command"], "my-own-thing")
        self.assertIn("subagentStatusLine baska bir komut", r.stdout)
        os.remove(os.path.join(self.claude, "settings.json"))
        self.kur()
        self.assertEqual(self.kur("--remove").returncode, 0)
        self.assertNotIn("subagentStatusLine", json.loads(rd(os.path.join(self.claude, "settings.json"))))
        self.assertFalse(self.installed("bin/subagent-statusline.py"))

    def test_maliyetsiz_install_leaves_out_cost_statistics(self):
        r = self.kur("--maliyetsiz")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.installed("bin/pace_forecast.py"))
        for f in ("bin/maliyet.py", "bin/maliyet_cekirdek.py", "bin/maliyet_ayristir.py", "bin/maliyet_paylas.py", "bin/maliyet-fiyatlar.json"):
            self.assertFalse(self.installed(f), f)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "bin", "maliyet")))
        udir = os.path.join(self.home, ".config", "systemd", "user")   # only the lost-work job, which every install sets up
        self.assertEqual(sorted(os.listdir(udir)) if os.path.isdir(udir) else [], ["kayip-is.service", "kayip-is.timer"] if sys.platform.startswith("linux") else [])

    @unittest.skipIf(os.name == "nt", "POSIX installer branch (wrapper in ~/.local/bin, systemd); see WindowsInstaller")
    def test_opt_in_install_files_wrapper_and_units(self):
        r = self.kur("--maliyet")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for f in ("bin/maliyet.py", "bin/maliyet_cekirdek.py", "bin/maliyet_ayristir.py", "bin/maliyet_paylas.py", "bin/maliyet-fiyatlar.json", "bin/maliyet-panel.html"):
            self.assertTrue(self.installed(f), f)
        wrapper = os.path.join(self.home, ".local", "bin", "maliyet")
        self.assertTrue(os.stat(wrapper).st_mode & stat.S_IXUSR)
        h = subprocess.run([wrapper, "--help"], capture_output=True, text=True, env=clean_env(HOME=self.home), timeout=60)
        self.assertEqual(h.returncode, 0, h.stderr)
        self.assertIn("--pencere", h.stdout)
        self.assertIn("[OK] maliyet --kaydet", r.stdout)                 # first scan ran, against the scratch HOME only
        self.assertTrue(os.path.isfile(os.path.join(self.claude, "maliyet", "durum.cache")))
        udir = os.path.join(self.home, ".config", "systemd", "user")
        svc = rd(os.path.join(udir, "maliyet-gunluk.service"))
        self.assertIn("ExecStart=%s %s --kaydet" % (sys.executable, os.path.join(self.claude, "bin", "maliyet.py")), svc)
        self.assertNotIn("@", svc)                                        # every placeholder was filled
        self.assertTrue(svc.startswith("# Installed by"), svc[:60])
        self.assertIn("Description=Claude Code daily cost totals", svc)
        tmr = rd(os.path.join(udir, "maliyet-gunluk.timer"))
        self.assertIn("OnCalendar=*-*-* 00/2:23:00", tmr)               # every 2 hours at an off-minute
        self.assertIn("Persistent=true", tmr)
        self.assertIn("RandomizedDelaySec=", tmr)
        # a scratch HOME must never drive the real systemd user manager
        self.assertIn("systemctl calistirilmadi", r.stdout)
        self.assertFalse(os.path.exists(self.calls), "systemctl was called under a scratch HOME")

    def test_rerun_is_idempotent_and_whatif_writes_nothing(self):
        self.assertEqual(self.kur("--maliyet").returncode, 0)
        r = self.kur("--maliyet")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("ayni: %s" % os.path.join(self.claude, "bin", "maliyet.py").replace("\\", "/"), r.stdout)
        self.assertNotIn("yedek:", r.stdout.split("settings.json")[0])
        shutil.rmtree(self.root + "/home")
        os.makedirs(self.home)
        w = self.kur("--maliyet", "--whatif")
        self.assertEqual(w.returncode, 0, w.stdout)
        self.assertEqual(os.listdir(self.home), [])

    def test_remove_takes_everything_but_the_history(self):
        self.assertEqual(self.kur("--maliyet").returncode, 0)
        hist = os.path.join(self.claude, "maliyet", "gunluk.jsonl")
        with open(hist, "w") as f:
            f.write('{"gun":"2026-01-01","usd":1.0}\n')
        r = self.kur("--remove")                      # no --maliyet needed: removal always covers the optional part
        self.assertEqual(r.returncode, 0, r.stdout)
        for f in ("bin/maliyet.py", "bin/maliyet_cekirdek.py", "bin/maliyet_ayristir.py", "bin/maliyet_paylas.py", "bin/maliyet-fiyatlar.json", "bin/maliyet-panel.html", "bin/pace_forecast.py"):
            self.assertFalse(self.installed(f), f)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "bin", "maliyet")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "systemd", "user", "maliyet-gunluk.timer")))
        pyc = os.path.join(self.claude, "bin", "__pycache__")
        self.assertEqual([n for n in (os.listdir(pyc) if os.path.isdir(pyc) else []) if n.startswith("maliyet")], [])   # no stray byte code
        self.assertTrue(os.path.isfile(hist))         # recorded history is the user's data: kept
        self.assertFalse(os.path.exists(self.calls))

    def run_as_real_home(self, *args):
        """Run kur_ortak.main() with gercek_ev() patched to the scratch HOME, so the branch that drives `systemctl --user`
        (taken on a teammate's real home) executes against the FAKE systemctl first on PATH."""
        code = ("import sys; from pathlib import Path; sys.path.insert(0, %r); import kur_ortak; "
                "kur_ortak.gercek_ev = lambda: Path(%r); kur_ortak.claude_plugin = lambda *a, **k: (True, 'ok'); sys.argv = ['kur_ortak', '--python', %r] + %r; sys.exit(kur_ortak.main())"
                % (PLUG, self.home, sys.executable, kopya(args)))
        env = clean_env(HOME=self.home, USERPROFILE=self.home, PATH=self.fakebin + os.pathsep + os.environ.get("PATH", ""))
        self.assertEqual(shutil.which("systemctl", path=env["PATH"]), os.path.join(self.fakebin, "systemctl"))   # never the real one
        return subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)

    @unittest.skipIf(os.name == "nt", "POSIX installer branch (wrapper in ~/.local/bin, systemd); see WindowsInstaller")
    def test_real_home_branch_enables_and_disables_the_timer(self):
        r = self.run_as_real_home("--maliyet")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rd(self.calls).split("\n")[:2], ["--user daemon-reload", "--user enable --now maliyet-gunluk.timer"])
        self.assertNotIn("calistirilmadi", r.stdout)
        os.remove(self.calls)
        r = self.run_as_real_home("--remove")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rd(self.calls).split("\n")[:2], ["--user disable --now maliyet-gunluk.timer", "--user daemon-reload"])
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "systemd", "user", "maliyet-gunluk.service")))

    @unittest.skipIf(os.name == "nt", "POSIX installer branch (systemd drop-ins)")
    def test_old_drop_in_pointing_at_the_managed_copy_is_backed_up_and_removed(self):
        d = os.path.join(self.home, ".config", "systemd", "user", "maliyet-gunluk.service.d")
        os.makedirs(d)
        eski = "[Service]\nExecStart=\nExecStart=/usr/bin/python3 %s/.claude/bin/maliyet.py --kaydet --paylas\n" % self.home
        for ad in ("paylas.conf", "baska-ad.conf"):
            with open(os.path.join(d, ad), "w") as f:
                f.write(eski)
        ilgisiz = "[Service]\nEnvironment=FOO=1\nExecStart=\nExecStart=/usr/local/bin/benim-isim\n"
        with open(os.path.join(d, "benim.conf"), "w") as f:
            f.write(ilgisiz)
        r = self.run_as_real_home("--maliyet")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        kalan = sorted(os.listdir(d))
        self.assertEqual([k for k in kalan if k.endswith(".conf")], ["benim.conf"])
        self.assertEqual(len([k for k in kalan if ".conf.yedek-" in k]), 2)
        self.assertEqual(rd(os.path.join(d, "benim.conf")), ilgisiz)
        bak = [k for k in kalan if k.startswith("paylas.conf.yedek-")][0]
        self.assertEqual(rd(os.path.join(d, bak)), eski)
        self.assertEqual(len([l for l in r.stdout.splitlines() if "drop-in" in l and "paylas.conf" in l]), 1)
        self.assertEqual(rd(self.calls).split("\n")[:2], ["--user daemon-reload", "--user daemon-reload"])   # one after the removal, one before enable

    @unittest.skipIf(os.name == "nt", "POSIX installer branch (systemd drop-ins)")
    def test_old_timer_drop_in_that_pins_a_daily_time_is_backed_up_and_removed(self):
        d = os.path.join(self.home, ".config", "systemd", "user", "maliyet-gunluk.timer.d")
        os.makedirs(d)
        eski = "[Timer]\nOnCalendar=\nOnCalendar=*-*-* 03:40:00\n"
        with open(os.path.join(d, "siralama.conf"), "w") as f:
            f.write(eski)
        baska = "[Timer]\nOnCalendar=\nOnCalendar=*-*-* 05:00:00\nRandomizedDelaySec=1h\n"      # more than the schedule: not ours to remove
        with open(os.path.join(d, "benim.conf"), "w") as f:
            f.write(baska)
        r = self.run_as_real_home("--maliyet")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        kalan = sorted(os.listdir(d))
        self.assertEqual([k for k in kalan if k.endswith(".conf")], ["benim.conf"])
        bak = [k for k in kalan if k.startswith("siralama.conf.yedek-")]
        self.assertEqual(len(bak), 1)
        self.assertEqual(rd(os.path.join(d, bak[0])), eski)
        self.assertEqual(rd(os.path.join(d, "benim.conf")), baska)
        self.assertEqual(len([l for l in r.stdout.splitlines() if "zamanlayici ayari" in l and "siralama.conf" in l]), 1)
        self.assertIn("OnCalendar=*-*-* 00/2:23:00", rd(os.path.join(self.home, ".config", "systemd", "user", "maliyet-gunluk.timer")))
        self.assertEqual(rd(self.calls).split("\n")[:2], ["--user daemon-reload", "--user daemon-reload"])
        r = self.run_as_real_home("--maliyet", "--whatif")                    # nothing left to remove, and a dry run removes nothing
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_settings_json_is_merged_not_replaced(self):
        os.makedirs(self.claude)
        with open(os.path.join(self.claude, "settings.json"), "w") as f:
            json.dump({"theme": "dark", "permissions": {"allow": ["Bash(ls:*)"]}}, f)
        self.assertEqual(self.kur("--maliyet").returncode, 0)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        self.assertEqual(cfg["theme"], "dark")
        self.assertEqual(cfg["permissions"], {"allow": ["Bash(ls:*)"]})
        self.assertIn("statusLine", cfg)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not available")
    def test_rendered_units_pass_systemd_analyze_verify(self):
        self.assertEqual(self.kur("--maliyet").returncode, 0)
        udir = os.path.join(self.home, ".config", "systemd", "user")
        r = subprocess.run(["systemd-analyze", "--user", "verify", os.path.join(udir, "maliyet-gunluk.service"), os.path.join(udir, "maliyet-gunluk.timer")],
                           capture_output=True, text=True, env=clean_env(HOME=self.home), timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("maliyet-gunluk", r.stderr.replace(udir, ""))   # no complaint about our units

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "jq/bash not available")
    def test_status_line_segments_follow_the_opt_out(self):
        payload = json.dumps({"model": {"id": "claude-x", "display_name": "X"}, "workspace": {"current_dir": self.home},
                              "prompt_cache": {"hit_ratio": 0.93, "misses": 2}, "cost": {"total_cost_usd": 1.2345}})

        def line(**env):
            e = clean_env(HOME=self.home, **env)
            return subprocess.run(["bash", os.path.join(self.claude, "statusline.sh")], input=payload, capture_output=True, text=True, env=e, timeout=60).stdout
        self.assertEqual(self.kur("--maliyetsiz").returncode, 0)
        self.assertNotIn("önb", line())                                   # opted out: nothing added
        self.assertNotIn("oturum $", line())
        self.assertEqual(self.kur().returncode, 0)                        # the default install has the cost segments
        on = line()
        self.assertIn("önb %93 (2 kaçış)", on)
        self.assertIn("oturum $1.23", on)
        self.assertIn("son 24sa $", on)
        self.assertNotIn("önb", line(MALIYET_DURUM="0"))                  # env switch
        open(os.path.join(self.claude, "maliyet", "kapali"), "w").close()
        self.assertNotIn("önb", line())                                   # marker file switch
        self.assertNotIn("oturum $", line())

    # ---- --repodan: run from the clone (no copies), state file for rules_watch.py, --sinama, --kaldir
    COPIES = ("statusline.ps1", "statusline.sh", "bin/statusline-yerel.ps1", "bin/pace-hook.py", "bin/pace_forecast.py", "bin/satir_sar.py",
              "bin/subagent-statusline.py", "bin/maliyet.py", "bin/maliyet_cekirdek.py", "bin/maliyet-fiyatlar.json", "bin/maliyet-panel.html")

    def plugin_snapshot(self):
        """Content of every file the installer would copy, from the clone: an install must never write into the clone."""
        out = {}
        for d in ("", "bin"):
            for n in sorted(os.listdir(os.path.join(PLUG, d) if d else PLUG)):
                p = os.path.join(PLUG, d, n)
                if os.path.isfile(p):
                    with open(p, "rb") as f:
                        out[os.path.join(d, n)] = f.read()
        return out

    def test_repodan_points_everything_into_the_clone_and_removes_old_copies(self):
        self.assertEqual(self.kur().returncode, 0)                        # an earlier copy install
        self.assertTrue(self.installed("bin/pace-hook.py"))
        before = self.plugin_snapshot()
        r = self.kur("--repodan")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.plugin_snapshot(), before)
        for f in self.COPIES:
            self.assertFalse(self.installed(f), f)                        # the clone's files run instead
        plug = PLUG.replace("\\", "/")
        repo = os.path.dirname(os.path.dirname(PLUG)).replace("\\", "/")
        raw = rd(os.path.join(self.claude, "settings.json"))
        cfg = json.loads(raw)
        self.assertNotIn("\\\\", raw)                                     # forward slashes only (a backslash would be JSON-escaped)
        for ev in ("SessionStart", "UserPromptSubmit"):
            self.assertEqual(cfg["hooks"][ev][0]["hooks"][0]["args"], ["-I", plug + "/bin/pace-hook.py"])
        status = cfg["statusLine"]["command"]
        if os.name == "nt" and PWSH:   # wrapped in a Test-Path fallback: a missing clone shows 'Claude', never an error
            sp = plug + "/bin/statusline-yerel.ps1"
            self.assertIn("\"if (Test-Path '%s') { & '%s' } else { 'Claude' }\"" % (sp, sp), status)
        else:
            self.assertTrue(status.endswith(plug + "/statusline.sh"), status)
        self.assertTrue(cfg["subagentStatusLine"]["command"].endswith(repo + "/plugins/ekip-ajanlari/tools/subagent-statusline.py"))
        durum = json.loads(rd(os.path.join(self.claude, "kota-temposu.json")))
        self.assertEqual((durum["kip"], durum["depo"], durum["python"]), ("repodan", repo, sys.executable.replace("\\", "/")))
        self.assertEqual(durum["argv"][:2], ["--python", sys.executable.replace("\\", "/")])
        self.assertEqual(durum["argv"][-1], "--repodan")
        if os.name == "nt" and PWSH:
            self.assertEqual(rd(os.path.join(self.claude, "bin", "maliyet.cmd")),
                             '@"%s" "%s/bin/maliyet.py" %%*\n' % (sys.executable.replace("\\", "/"), plug))
        else:
            self.assertIn('"%s/bin/maliyet.py"' % plug, rd(os.path.join(self.home, ".local", "bin", "maliyet")))
        for ok in ("[OK] hook SessionStart", "[OK] maliyet --kaydet", "[OK] durum cubugu", "[OK] ajan satiri"):
            self.assertIn(ok, r.stdout)
        again = self.kur("--repodan")                                     # idempotent
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertIn("settings.json: degisiklik yok", again.stdout)
        self.assertIn("ayni: %s/kota-temposu.json" % self.claude.replace("\\", "/"), again.stdout)

    def test_direct_source_is_the_default_with_backups_a_fallback_and_saved_flags(self):
        self.assertEqual(self.kur("--kopya").returncode, 0)
        self.assertTrue(self.installed("bin/maliyet.py"))                 # an older copy install
        old = rd(os.path.join(self.claude, "bin", "pace-hook.py"))
        r = self.kur_ham()                                                   # no mode flag: direct source
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for f in self.COPIES:
            self.assertFalse(self.installed(f), f)                        # the copies are gone ...
        baks = [n for n in os.listdir(os.path.join(self.claude, "bin")) if n.startswith("pace-hook.py.yedek-")]
        self.assertEqual(len(baks), 1)                                    # ... with a backup
        self.assertEqual(rd(os.path.join(self.claude, "bin", baks[0])), old)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        plug = PLUG.replace("\\", "/")
        self.assertIn(plug, cfg["statusLine"]["command"])
        self.assertIn("ekip-ajanlari/tools/subagent-statusline.py", cfg["subagentStatusLine"]["command"])
        self.assertNotIn(os.path.join(self.claude, "bin"), json.dumps(cfg))
        if not (os.name == "nt" and PWSH):
            # a missing clone / failing script prints the minimal fallback line instead of an error
            sh = cfg["statusLine"]["command"].replace(plug + "/statusline.sh", plug + "/yok/statusline.sh")
            o = subprocess.run(sh, shell=True, input="{}", capture_output=True, text=True)
            self.assertEqual((o.stdout, o.stderr), ("Claude", ""))
        self.assertEqual(json.loads(rd(os.path.join(self.claude, "kota-temposu.json")))["kurulum"], [])
        again = self.kur_ham()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(json.loads(rd(os.path.join(self.claude, "settings.json"))), cfg)   # idempotent

    def test_repodan_sinama_runs_the_quick_check_only(self):
        self.assertEqual(self.kur("--repodan").returncode, 0)
        before = rd(os.path.join(self.claude, "settings.json"))
        r = self.kur("--repodan", "--sinama")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for ok in ("[OK] hook SessionStart", "[OK] hook UserPromptSubmit", "[OK] maliyet --help", "[OK] durum cubugu", "[OK] ajan satiri"):
            self.assertIn(ok, r.stdout)
        self.assertNotIn("--kaydet", r.stdout)                            # no first scan: it must stay fast
        self.assertNotIn("settings.json", r.stdout)                       # installs nothing
        self.assertEqual(rd(os.path.join(self.claude, "settings.json")), before)

    def test_kaldir_undoes_repodan_and_copy_mode_drops_the_state_file(self):
        self.assertEqual(self.kur("--repodan").returncode, 0)
        r = self.kur()                                                    # back to the copied install
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.installed("kota-temposu.json"))
        self.assertTrue(self.installed("bin/pace-hook.py"))
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        self.assertEqual(cfg["hooks"]["SessionStart"][0]["hooks"][0]["args"][1], os.path.join(self.claude, "bin", "pace-hook.py").replace("\\", "/"))
        self.assertEqual(self.kur("--repodan").returncode, 0)
        r = self.kur("--remove")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        for k in ("hooks", "statusLine", "subagentStatusLine"):
            self.assertNotIn(k, cfg)
        self.assertFalse(self.installed("kota-temposu.json"))
        self.assertFalse(self.installed("bin/maliyet.cmd"))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "bin", "maliyet")))
        self.assertTrue(os.path.isfile(os.path.join(PLUG, "bin", "pace-hook.py")))   # the clone is never touched

    def test_repodan_needs_the_cost_statistics(self):
        r = self.kur("--repodan", "--maliyetsiz")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("--maliyetsiz ile birlikte verilemez", r.stdout)
        self.assertEqual(os.listdir(self.home), [])                       # nothing written

    @unittest.skipIf(os.name == "nt", "POSIX installer branch (systemd)")
    def test_repodan_systemd_unit_runs_the_clone(self):
        r = self.run_as_real_home("--repodan")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        svc = rd(os.path.join(self.home, ".config", "systemd", "user", "maliyet-gunluk.service"))
        self.assertIn("ExecStart=%s %s --kaydet" % (sys.executable, os.path.join(PLUG, "bin", "maliyet.py")), svc)



    def test_default_install_adds_the_rules_watch_hook_from_the_clone(self):
        r = self.kur()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        rw = [h for g in cfg["hooks"]["SessionStart"] for h in g["hooks"] if "rules_watch" in json.dumps(h)]
        self.assertEqual(len(rw), 1)
        self.assertEqual(rw[0]["args"][-1], os.path.join(os.path.dirname(os.path.dirname(PLUG)), "tools", "rules_watch.py").replace("\\", "/"))
        self.assertEqual(self.kur().returncode, 0)                 # a re-run adds no second entry
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        self.assertEqual(sum("rules_watch" in json.dumps(h) for g in cfg["hooks"]["SessionStart"] for h in g["hooks"]), 1)
        self.assertEqual(self.kur("--remove").returncode, 0)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json"))) if os.path.exists(os.path.join(self.claude, "settings.json")) else {}
        self.assertNotIn("rules_watch", json.dumps(cfg))

    def test_default_install_adds_ajan_bekci_notu_and_remove_takes_it_out(self):
        r = self.kur()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ajan_bekci_notu PostToolUse", r.stdout)
        self.assertIn("[OK] ajan_adi PreToolUse", r.stdout)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        self.assertEqual(sum("ajan_bekci_notu" in json.dumps(h) for g in cfg["hooks"]["PostToolUse"] for h in g["hooks"]), 1)
        self.assertEqual(self.kur("--remove").returncode, 0)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json"))) if os.path.exists(os.path.join(self.claude, "settings.json")) else {}
        self.assertNotIn("ajan_bekci_notu", json.dumps(cfg))

    def test_default_install_adds_stop_and_subagentstop_kanit_kancasi_and_remove_takes_them_out(self):
        r = self.kur()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("kanit_kancasi SubagentStop", r.stdout)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        for ev in ("Stop", "SubagentStop"):
            self.assertEqual(sum("kanit_kancasi" in json.dumps(h) for g in cfg["hooks"][ev] for h in g["hooks"]), 1, ev)
        self.assertEqual(self.kur().returncode, 0)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        self.assertEqual(sum("kanit_kancasi" in json.dumps(h) for g in cfg["hooks"]["Stop"] for h in g["hooks"]), 1)
        self.assertEqual(self.kur("--remove").returncode, 0)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json"))) if os.path.exists(os.path.join(self.claude, "settings.json")) else {}
        self.assertNotIn("kanit_kancasi", json.dumps(cfg))

    @unittest.skipIf(os.name == "nt", "POSIX installer branch (systemd); see WindowsInstaller")
    def test_real_home_installs_the_daily_lost_work_timer_from_the_clone(self):
        r = self.run_as_real_home()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("--user enable --now kayip-is.timer", rd(self.calls).split("\n"))
        svc = rd(os.path.join(self.home, ".config", "systemd", "user", "kayip-is.service"))
        kayip = os.path.join(os.path.dirname(os.path.dirname(PLUG)), "tools", "gunluk_kayip.py")
        self.assertIn("ExecStart=%s -I %s\n" % (sys.executable, kayip), svc)
        self.assertNotIn("--force", svc)
        os.remove(self.calls)
        r = self.run_as_real_home("--remove")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("--user disable --now kayip-is.timer", rd(self.calls).split("\n"))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "systemd", "user", "kayip-is.timer")))


@unittest.skipUnless(os.name == "nt" and PWSH, "Windows + PowerShell only")
class WindowsInstaller(unittest.TestCase):
    """kur_ortak.py --windows under a scratch USERPROFILE: files, maliyet.cmd, the Task Scheduler branch (never the real one)
    and the PowerShell status line with the cost segments."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="kurwin-test-")
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self.claude = os.path.join(self.home, ".claude")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def env(self, **extra):
        return clean_env(HOME=self.home, USERPROFILE=self.home, **extra)

    def kur(self, *args):
        return subprocess.run([sys.executable, "-I", KUR, "--windows", "--python", sys.executable, "--shell", PWSH, *kopya(args)],
                              capture_output=True, text=True, encoding="utf-8", env=self.env(), timeout=600)

    def as_real_home(self, *args):
        """kur_ortak.main() with gercek_ev() patched to the scratch HOME and gorev_calistir() replaced by a recorder
        (the real Task Scheduler is never reached). Returns (process, list of recorded [snippet first line, exe, script])."""
        code = ("import sys, json; from pathlib import Path; sys.path.insert(0, %r); import kur_ortak; calls = []; kcalls = []; sgcalls = []; "
                "kur_ortak.gercek_ev = lambda: Path(%r); kur_ortak.claude_plugin = lambda *a, **k: (True, 'ok'); "
                "kur_ortak.gorev_calistir = lambda shell, ps, exe, script, **kw: (kcalls if kw.get('ad') == 'kayip-is' else sgcalls if kw.get('ad') in ('slack-gelen', 'slack-triage') else calls).append([ps.strip().splitlines()[-1][:60], exe, script]) or True; "
                "sys.argv = ['kur_ortak', '--windows', '--python', %r, '--shell', %r] + %r; rc = kur_ortak.main(); "
                "print('CALLS' + json.dumps(calls)); print('KCALLS' + json.dumps(kcalls)); sys.exit(rc)"
                % (PLUG, self.home, sys.executable, PWSH, kopya(args)))
        r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, encoding="utf-8", env=self.env(), timeout=600)
        calls = [json.loads(l[5:]) for l in r.stdout.splitlines() if l.startswith("CALLS")]
        self.kcalls = ([json.loads(l[6:]) for l in r.stdout.splitlines() if l.startswith("KCALLS")] or [None])[0]   # the lost-work task
        return r, (calls[0] if calls else None)

    def line(self, payload, **env):
        e = self.env(**env)
        return subprocess.run([PWSH, "-NoProfile", "-File", os.path.join(self.claude, "bin", "statusline-yerel.ps1")], input=payload,
                              capture_output=True, text=True, encoding="utf-8", env=e, timeout=120).stdout

    def test_install_writes_files_cmd_and_leaves_task_scheduler_alone_under_a_scratch_profile(self):
        r = self.kur("--maliyet")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for f in ("statusline.ps1", "bin/statusline-yerel.ps1", "bin/maliyet.py", "bin/maliyet_cekirdek.py", "bin/maliyet-panel.html"):
            self.assertTrue(os.path.exists(os.path.join(self.claude, f)), f)
        cmd = rd(os.path.join(self.claude, "bin", "maliyet.cmd"))
        self.assertTrue(cmd.startswith('@"%s"' % sys.executable.replace("\\", "/")), cmd)
        self.assertIn("gorev zamanlayici calistirilmadi", r.stdout)
        self.assertIn("[OK] maliyet --kaydet", r.stdout)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        self.assertIn("statusline-yerel.ps1", cfg["statusLine"]["command"])
        self.assertNotIn("\\", cfg["statusLine"]["command"])                 # forward slashes only
        self.assertFalse(os.path.exists(os.path.join(self.claude, "pace-peer.json")))

    def test_real_profile_registers_and_removes_the_user_task(self):
        r, calls = self.as_real_home("--maliyet")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNotNone(calls)
        self.assertEqual(len(calls), 1)
        _, exe, script = calls[0]
        self.assertTrue(exe.lower().endswith(("pythonw.exe", "python.exe")), exe)
        self.assertTrue(script.endswith("maliyet.py") and "\\" in script, script)
        self.assertIn("[OK] gorev zamanlayici: 'maliyet-gunluk'", r.stdout)
        r, calls = self.as_real_home("--remove")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(calls), 1)                            # the removal snippet was run once
        r, calls = self.as_real_home("--maliyet", "--whatif")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(calls, [])                                # -WhatIf registers nothing
        self.assertIn("[-WhatIf] gorev zamanlayici", r.stdout)

    def test_repodan_task_runs_the_clone_and_the_status_line_finds_its_siblings_there(self):
        r, calls = self.as_real_home("--repodan")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(calls[0][2], os.path.join(PLUG, "bin", "maliyet.py").replace("/", "\\"))   # the task's script is the clone's
        # the configured command is the clone's wrapper; with no copies in the profile, the cost segments come from the clone's bin/
        self.assertFalse(os.path.exists(os.path.join(self.claude, "bin", "maliyet.py")))
        payload = json.dumps({"model": {"id": "claude-x", "display_name": "X"}, "workspace": {"current_dir": self.home.replace("\\", "/")},
                              "prompt_cache": {"hit_ratio": 0.93, "misses": 2}, "cost": {"total_cost_usd": 1.2345}})
        out = subprocess.run([PWSH, "-NoProfile", "-File", os.path.join(PLUG, "bin", "statusline-yerel.ps1")], input=payload,
                             capture_output=True, text=True, encoding="utf-8", env=self.env(), timeout=120).stdout
        self.assertIn("önb %93 (2 kaçış)", out)
        self.assertIn("oturum $1.23", out)

    def test_task_snippets_are_user_level_only(self):
        sys.path.insert(0, PLUG)
        import kur_ortak
        k = kur_ortak.GOREV_KAYIT_PS
        for need in ("Register-GizliGorev", "-Once -At", "AddMinutes(23)", "-RepetitionInterval (New-TimeSpan -Hours 2)", "-StartWhenAvailable", "--kaydet", "Priority"):
            self.assertIn(need, k)
        for bad in ("Highest", "SYSTEM", "-Password", "schtasks"):
            self.assertNotIn(bad, k)
        self.assertIn("Unregister-ScheduledTask", kur_ortak.GOREV_SIL_PS)

    def test_status_line_cost_segments_opt_out_and_local_extension(self):
        payload = json.dumps({"model": {"id": "claude-x", "display_name": "X"}, "workspace": {"current_dir": self.home.replace("\\", "/")},
                              "prompt_cache": {"hit_ratio": 0.93, "misses": 2}, "cost": {"total_cost_usd": 1.2345}})
        self.assertEqual(self.kur("--maliyetsiz").returncode, 0)
        base = self.line(payload)
        self.assertNotIn("önb", base)                       # opted out: nothing added
        self.assertNotIn("oturum $", base)
        self.assertEqual(self.kur().returncode, 0)
        on = self.line(payload)
        self.assertIn("önb %93 (2 kaçış)", on)
        self.assertIn("oturum $1.23", on)
        self.assertIn("son 24sa $", on)
        self.assertNotIn("önb", self.line(payload, MALIYET_DURUM="0"))
        open(os.path.join(self.claude, "maliyet", "kapali"), "w").close()
        off = self.line(payload)
        self.assertNotIn("önb", off)
        self.assertNotIn("oturum $", off)
        os.remove(os.path.join(self.claude, "maliyet", "kapali"))
        with open(os.path.join(self.claude, "bin", "statusline-ek.ps1"), "w", encoding="utf-8") as f:
            f.write("param([string]$Satir)\n$Satir + ' | EK-TEST'\n")
        self.assertTrue(self.line(payload).strip().endswith("EK-TEST"))

    def test_status_line_still_writes_pace_json(self):
        self.assertEqual(self.kur().returncode, 0)
        now = int(time.time())          # a real reset time: the placeholder 4102444800 (year 2100) is deliberately ignored by the status line
        payload = json.dumps({"model": {"id": "claude-x", "display_name": "X"}, "workspace": {"current_dir": self.home.replace("\\", "/")},
                              "rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": now + 7200},
                                              "seven_day": {"used_percentage": 20, "resets_at": now + 3 * 86400}}})
        out = self.line(payload)
        self.assertIn("hafta %20", out)
        pace = json.loads(rd(os.path.join(self.claude, "pace.json")))
        self.assertIn(pace["mode"], ("BOL", "NORMAL", "SIKI"))
        self.assertEqual(pace["week_used"], 20)


    def test_real_profile_registers_and_removes_the_lost_work_task(self):
        r, _ = self.as_real_home("--repodan")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.kcalls), 1)
        _, exe, script = self.kcalls[0]
        kayip = os.path.join(os.path.dirname(os.path.dirname(PLUG)), "tools", "gunluk_kayip.py")
        self.assertEqual(script, kayip.replace("/", "\\"))       # the clone's script, never a copy
        self.assertIn("[OK] gorev zamanlayici: 'kayip-is'", r.stdout)
        self.assertIn("[OK] gunluk_kayip --help", r.stdout)
        r, _ = self.as_real_home("--remove")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.kcalls), 1)                       # the removal snippet
        self.assertIn("Unregister-ScheduledTask", kur_ortak_modul().GOREV_SIL_PS)

    def test_lost_work_task_snippet_is_user_level_and_never_forces(self):
        k = kur_ortak_modul()
        for need in ("Register-GizliGorev", "-Daily -At '17:30'", "-StartWhenAvailable"):
            self.assertIn(need, k.KAYIP_GOREV_KAYIT_PS)
        for bad in ("Highest", "SYSTEM", "-Password", "schtasks"):
            self.assertNotIn(bad, k.KAYIP_GOREV_KAYIT_PS)
        self.assertNotIn("KAYIP_ARGS", k.KAYIP_GOREV_KAYIT_PS)   # the options live in tools/gunluk_kayip.py, not in the scheduler
        import importlib.util
        spec = importlib.util.spec_from_file_location("gunluk_kayip", str(k.KAYIP_GIRIS))
        g = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(g)
        self.assertIn("--push-unpushed", g.ARGS)
        self.assertIn("--cache-yaz", g.ARGS)
        self.assertNotIn("--force", g.ARGS)


def kur_ortak_modul():
    sys.path.insert(0, PLUG)
    import kur_ortak
    return kur_ortak


class RulesWatchHookMerge(unittest.TestCase):
    """merge_rules_watch(): one rules_watch entry pointing into the clone, a hand-written one is replaced, others are kept."""

    def test_hand_written_entry_is_replaced_others_kept_and_remove_takes_only_ours(self):
        k = kur_ortak_modul()
        other = {"type": "command", "command": "echo hi"}
        cfg = {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [
            {"type": "command", "command": "python ~/source/repos/claudeshared/tools/rules_watch.py", "timeout": 120}, other]}]}}
        self.assertEqual(k.merge_rules_watch(cfg, "/usr/bin/python3", False),
                         ["SessionStart: rules_watch girisi guncellendi",
                          "UserPromptSubmit: rules_watch girisi eklendi (kural uyarilari + kayip is raporu)"])
        hooks = cfg["hooks"]["SessionStart"][0]["hooks"]
        self.assertEqual(hooks[1], other)
        self.assertEqual(hooks[0]["args"][:2], ["-I", "-c"])
        self.assertEqual(hooks[0]["args"][-1], k.RULES_WATCH.as_posix())
        prompt = cfg["hooks"]["UserPromptSubmit"][0]["hooks"][0]          # the cheap per-prompt delta of the background run
        self.assertEqual(prompt["args"][-2:], [k.RULES_WATCH.as_posix(), "--istek"])
        self.assertEqual(k.merge_rules_watch(cfg, "/usr/bin/python3", False), [])       # idempotent
        k.merge_rules_watch(cfg, "/usr/bin/python3", True)
        self.assertEqual(cfg["hooks"]["SessionStart"][0]["hooks"], [other])
        self.assertNotIn("UserPromptSubmit", cfg["hooks"])
        empty = {}
        self.assertEqual(k.merge_rules_watch(empty, "py", False),
                         ["SessionStart: rules_watch girisi eklendi (kural uyarilari + kayip is raporu)",
                          "UserPromptSubmit: rules_watch girisi eklendi (kural uyarilari + kayip is raporu)"])
        self.assertEqual(empty["hooks"]["SessionStart"][0]["matcher"], "startup")
        k.merge_rules_watch(empty, "py", True)
        self.assertEqual(empty, {})

    def test_kanit_kancasi_stop_and_subagentstop_replace_hand_entry_keep_others_and_remove(self):
        k = kur_ortak_modul()
        other = {"type": "command", "command": "echo hi"}
        hand = {"type": "command", "timeout": 10,
                "command": "test -f ~/x/claude-shared/tools/kanit_kancasi.py && python3 -I ~/x/claude-shared/tools/kanit_kancasi.py || true"}
        cfg = {"hooks": {"Stop": [{"hooks": [hand, other]}]}}
        ch = k.merge_kanit_kancasi(cfg, "/usr/bin/python3", False)
        self.assertEqual(sorted(c.split(":")[0] for c in ch), ["Stop", "SubagentStop"])
        self.assertIn("guncellendi", [c for c in ch if c.startswith("Stop:")][0])
        self.assertIn("eklendi", [c for c in ch if c.startswith("SubagentStop:")][0])
        for ev in ("Stop", "SubagentStop"):
            ours = [h for g in cfg["hooks"][ev] for h in g["hooks"] if "kanit_kancasi" in json.dumps(h)]
            self.assertEqual(len(ours), 1, ev)
            self.assertEqual(ours[0]["args"][:2], ["-I", "-c"])
            self.assertEqual(ours[0]["args"][-1], k.KANIT.as_posix())
        self.assertEqual(cfg["hooks"]["Stop"][0]["hooks"][1], other)
        self.assertEqual(k.merge_kanit_kancasi(cfg, "/usr/bin/python3", False), [])      # idempotent
        k.merge_kanit_kancasi(cfg, "/usr/bin/python3", True)
        self.assertEqual(cfg, {"hooks": {"Stop": [{"hooks": [other]}]}})
        empty = {}
        k.merge_kanit_kancasi(empty, "py", False)
        k.merge_kanit_kancasi(empty, "py", True)
        self.assertEqual(empty, {})

    def test_ajan_bekci_notu_posttooluse_agent_matcher_install_idempotent_keeps_others_and_remove(self):
        k = kur_ortak_modul()
        other = {"type": "command", "command": "echo hi"}
        cfg = {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [other]}]}}
        self.assertEqual(len(k.merge_ajan_bekci(cfg, sys.executable, False)), 1)
        grp = [g for g in cfg["hooks"]["PostToolUse"] if g.get("matcher") == "Agent"]
        self.assertEqual(len(grp), 1)
        self.assertEqual(grp[0]["hooks"][0]["args"][-1], k.BEKCI.as_posix())
        self.assertEqual(k.merge_ajan_bekci(cfg, sys.executable, False), [])      # idempotent
        h = grp[0]["hooks"][0]
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run([h["command"], *h["args"]], capture_output=True, text=True, timeout=60,
                               env={**os.environ, "AJAN_BEKCI_STATE": os.path.join(td, "s.json")},
                               input=json.dumps({"session_id": "t", "tool_name": "Agent", "tool_input": {"run_in_background": True}}))
        self.assertIn("additionalContext", r.stdout)
        k.merge_ajan_bekci(cfg, sys.executable, True)
        self.assertEqual(cfg, {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [other]}]}})
        empty = {}
        k.merge_ajan_bekci(empty, "py", False)
        k.merge_ajan_bekci(empty, "py", True)
        self.assertEqual(empty, {})

    def test_ajan_adi_pretooluse_install_idempotent_names_an_agent_call_and_remove(self):
        k = kur_ortak_modul()
        other = {"type": "command", "command": "echo hi"}
        cfg = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [other]}]}}
        self.assertEqual(len(k.merge_ajan_adi(cfg, sys.executable, False)), 1)
        grp = [g for g in cfg["hooks"]["PreToolUse"] if g.get("matcher") == "Agent|Task"]
        self.assertEqual(len(grp), 1)
        self.assertEqual(grp[0]["hooks"][0]["args"][-1], k.AJAN_ADI.as_posix())
        self.assertEqual(k.merge_ajan_adi(cfg, sys.executable, False), [])        # idempotent
        h = grp[0]["hooks"][0]
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run([h["command"], *h["args"]], capture_output=True, text=True, encoding="utf-8", timeout=60,
                               env={**os.environ, "EKIP_AJAN_ADI_DIR": td},
                               input=json.dumps({"session_id": "t", "tool_name": "Agent", "tool_input": {"prompt": "p", "description": "d"}}))
        yeni = json.loads(r.stdout)["hookSpecificOutput"]["updatedInput"]
        self.assertTrue(yeni["description"].endswith(" · d"))
        k.merge_ajan_adi(cfg, sys.executable, True)
        self.assertEqual(cfg, {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [other]}]}})
        empty = {}
        k.merge_ajan_adi(empty, "py", False)
        k.merge_ajan_adi(empty, "py", True)
        self.assertEqual(empty, {})

    def test_kanit_kancasi_command_runs_the_clone_script_and_blocks_a_hedge(self):
        k = kur_ortak_modul()
        cfg = {}
        k.merge_kanit_kancasi(cfg, sys.executable, False)
        h = cfg["hooks"]["SubagentStop"][0]["hooks"][0]
        r = subprocess.run([h["command"], *h["args"]], input=json.dumps({"hook_event_name": "SubagentStop", "last_assistant_message": "Bu probably yanlis."}),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["decision"], "block")

    def test_hook_command_is_silent_when_the_clone_is_missing_and_runs_the_script_when_present(self):
        k = kur_ortak_modul()
        tmp = tempfile.mkdtemp(prefix="rw-yedek-")
        self.addCleanup(shutil.rmtree, tmp, True)
        missing = subprocess.run([sys.executable, "-I", "-c", k.RW_YEDEK, os.path.join(tmp, "yok", "rules_watch.py")],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual((missing.returncode, missing.stdout, missing.stderr), (0, "", ""))
        script = os.path.join(tmp, "rules_watch.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import sys\nprint('ran', __name__)\nsys.exit(0)\n")
        present = subprocess.run([sys.executable, "-I", "-c", k.RW_YEDEK, script], capture_output=True, text=True, timeout=60)
        self.assertEqual((present.returncode, present.stdout.strip()), (0, "ran __main__"))


class ClaudeEklentileri(unittest.TestCase):
    """claude_eklentileri(): `claude plugin install` for every enabled claude-shared plugin, idempotent, a marketplace unknown to
    Claude Code is added and the install retried, skipped without `claude` or outside the real HOME, --remove uninstalls."""

    def run_it(self, enabled, remove=False, whatif=False, claude="claude", real_home=True, fail_first_install=False):
        k = kur_ortak_modul()
        calls = []

        def fake(exe, args, timeout=180):
            calls.append(args)
            if fail_first_install and args[0] == "install" and sum(1 for c in calls if c[0] == "install") == 1:
                return False, "marketplace claude-shared not found"
            return True, "ok"
        eski = (k.claude_plugin, k.shutil.which, k.gercek_ev)
        k.claude_plugin = fake
        k.shutil.which = lambda n: claude if n == "claude" else eski[1](n)
        k.gercek_ev = lambda: Path.home() if real_home else Path(tempfile.gettempdir()) / "baska"
        try:
            a = type("A", (), {"remove": remove, "whatif": whatif})()
            ok = k.claude_eklentileri({"enabledPlugins": enabled}, a, "")
        finally:
            k.claude_plugin, k.shutil.which, k.gercek_ev = eski
        return ok, calls

    def test_install_only_claude_shared_enabled(self):
        ok, calls = self.run_it({"ekip-ajanlari@claude-shared": True, "x@y": True, "old@claude-shared": False})
        self.assertEqual((ok, calls), (True, [["install", "ekip-ajanlari@claude-shared", "--scope", "user"]]))

    def test_unknown_marketplace_is_added_then_install_retried(self):
        ok, calls = self.run_it({"ekip-ajanlari@claude-shared": True}, fail_first_install=True)
        self.assertTrue(ok)
        self.assertEqual([c[0] for c in calls], ["install", "marketplace", "install"])
        self.assertEqual(calls[1][:2], ["marketplace", "add"])

    def test_skips(self):
        self.assertEqual(self.run_it({"ekip-ajanlari@claude-shared": True}, claude=None), (True, []))   # claude not on PATH
        self.assertEqual(self.run_it({"ekip-ajanlari@claude-shared": True}, real_home=False), (True, []))   # scratch HOME
        self.assertEqual(self.run_it({"ekip-ajanlari@claude-shared": True}, whatif=True), (True, []))
        self.assertEqual(self.run_it({"x@y": True}), (True, []))

    def test_remove_uninstalls(self):
        ok, calls = self.run_it({}, remove=True)
        self.assertEqual((ok, calls), (True, [["uninstall", "ekip-ajanlari@claude-shared", "--scope", "user"]]))


class EkipAjanlariMerge(unittest.TestCase):
    """merge_ekip_ajanlari(): the claude-shared marketplace points at this clone (directory source, loaded in place), the merged
    agent plugin is enabled, the merged-away sets are dropped, and --remove takes back only what is ours."""

    def test_install_idempotent_keeps_others_and_remove(self):
        k = kur_ortak_modul()
        kaynak = {"source": "directory", "path": k.KLON.as_posix()}
        cfg = {"enabledPlugins": {"team-agents@claude-shared": True, "x@y": True},
               "extraKnownMarketplaces": {"claude-shared": {"source": {"source": "github", "repo": "example-org/claude-shared"}, "autoUpdate": True},
                                          "other": {"source": {"source": "github", "repo": "a/b"}}}}
        self.assertEqual(len(k.merge_ekip_ajanlari(cfg, False)), 3)
        self.assertEqual(cfg["enabledPlugins"], {"x@y": True, "ekip-ajanlari@claude-shared": True})
        self.assertEqual(cfg["extraKnownMarketplaces"]["claude-shared"], {"source": kaynak, "autoUpdate": True})
        self.assertIn("other", cfg["extraKnownMarketplaces"])
        self.assertEqual(k.merge_ekip_ajanlari(cfg, False), [])      # idempotent
        k.merge_ekip_ajanlari(cfg, True)
        self.assertEqual(cfg["enabledPlugins"], {"x@y": True})
        self.assertNotIn("claude-shared", cfg["extraKnownMarketplaces"])
        empty = {}
        k.merge_ekip_ajanlari(empty, False)
        k.merge_ekip_ajanlari(empty, True)
        self.assertEqual(empty, {})

    def test_remove_keeps_the_marketplace_while_another_claude_shared_plugin_is_enabled(self):
        k = kur_ortak_modul()
        cfg = {"enabledPlugins": {"kota-temposu@claude-shared": True}}
        k.merge_ekip_ajanlari(cfg, False)
        k.merge_ekip_ajanlari(cfg, True)
        self.assertEqual(cfg["enabledPlugins"], {"kota-temposu@claude-shared": True})
        self.assertIn("claude-shared", cfg["extraKnownMarketplaces"])

    def test_the_marketplace_source_resolves_to_the_plugin_in_this_clone(self):
        k = kur_ortak_modul()
        cfg = {}
        k.merge_ekip_ajanlari(cfg, False)
        kok = cfg["extraKnownMarketplaces"]["claude-shared"]["source"]["path"]
        with open(os.path.join(kok, ".claude-plugin", "marketplace.json"), encoding="utf-8") as f:
            pazar = json.load(f)
        self.assertEqual(pazar["name"], k.PAZAR)
        giris = [p for p in pazar["plugins"] if p["name"] == k.AJAN_EKLENTI.split("@")[0]]
        self.assertEqual(len(giris), 1)
        self.assertTrue(giris[0]["source"].startswith("./"))     # relative path: Claude Code loads it in place from the clone
        self.assertTrue(os.path.isdir(os.path.join(kok, giris[0]["source"], "output-styles")))
        self.assertFalse(os.path.exists(os.path.join(kok, giris[0]["source"], "agents")))   # agents are linked, not plugin-loaded (#34)
        self.assertFalse([p for p in pazar["plugins"] if p["name"] + "@" + k.PAZAR in k.ESKI_EKLENTILER])


class EkipAjanlariInstall(unittest.TestCase):
    """End to end under a scratch HOME: the install enables the plugin from the clone, links the agents and clears old-name agent files."""
    setUp, tearDown, kur, kur_ham = Installer.setUp, Installer.tearDown, Installer.kur, Installer.kur_ham   # same scratch HOME, not the Installer tests

    def test_install_enables_the_plugin_and_backs_up_old_agent_files(self):
        ajanlar = os.path.join(self.claude, "agents")
        os.makedirs(ajanlar)
        for ad in ("quick-task", "haiku-isci", "kendi-ajanim"):
            with open(os.path.join(ajanlar, ad + ".md"), "w", encoding="utf-8") as f:
                f.write("---\nname: %s\n---\n" % ad)
        r = self.kur_ham("--repodan")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        self.assertIs(cfg["enabledPlugins"]["ekip-ajanlari@claude-shared"], True)
        self.assertEqual(cfg["extraKnownMarketplaces"]["claude-shared"]["source"]["source"], "directory")
        kalan = sorted(os.listdir(ajanlar))
        self.assertIn("kendi-ajanim.md", kalan)                       # not ours: left alone
        self.assertIn("claude-shared", kalan)                          # the agents link -> <clone>/agents
        self.assertTrue(os.path.isfile(os.path.join(ajanlar, "claude-shared", "quick-task-hm.md")))
        self.assertNotIn("quick-task.md", kalan)
        self.assertNotIn("haiku-isci.md", kalan)
        self.assertEqual(len([f for f in kalan if f.startswith(("quick-task.md.yedek-", "haiku-isci.md.yedek-"))]), 2)
        self.assertEqual(self.kur("--remove").returncode, 0)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json"))) if os.path.exists(os.path.join(self.claude, "settings.json")) else {}
        self.assertNotIn("ekip-ajanlari@claude-shared", json.dumps(cfg))
        self.assertNotIn("claude-shared", os.listdir(ajanlar))        # --kaldir takes the link away
        self.assertIn("kendi-ajanim.md", os.listdir(ajanlar))


if __name__ == "__main__":
    unittest.main()

"""Tests for the optional `--cost` part of the quota-pace installer (install_common.py), always under a scratch HOME.

Run:  python3 -I -m unittest discover -s plugins/quota-pace/tests     (from the repo root)

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


def copy(args):
    """The installer runs from the clone by default; these tests exercise the legacy copy mode unless they ask for --direct."""
    a = list(args)
    return a if {"--direct", "--copy", "--remove", "--probe"} & set(a) else ["--copy", *a]
PLUG = os.path.normpath(os.path.join(HERE, ".."))
INSTALL = os.path.join(PLUG, "install_common.py")
PWSH = shutil.which("pwsh") or shutil.which("powershell")


def clean_env(**extra):
    """The process environment minus everything that could redirect the tools to another config dir (test_cost.py
    sets some of these at import time when both files run in one process)."""
    env = {k: v for k, v in os.environ.items() if k not in ("XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "COST_TZ", "COST_PRICES", "COST_STATUS")}
    env.update(extra)
    return env


def rd(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


class Installer(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="install-test-")
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

    def install(self, *args):
        return self.install_raw(*copy(args))

    def install_raw(self, *args):
        env = clean_env(HOME=self.home, USERPROFILE=self.home, PATH=self.fakebin + os.pathsep + os.environ.get("PATH", ""))
        win = ["--windows", "--shell", PWSH] if os.name == "nt" and PWSH else []   # Windows has no bash/jq guarantee: use the Windows branch
        return subprocess.run([sys.executable, "-I", INSTALL, *win, "--python", sys.executable, *args],
                              capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)

    def installed(self, rel):
        return os.path.exists(os.path.join(self.claude, rel))

    def test_default_install_includes_cost_statistics_and_the_agent_rows(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for f in ("bin/cost.py", "bin/cost_ratio.py", "bin/cost_incremental.py", "bin/subagent-statusline.py"):
            self.assertTrue(self.installed(f), f)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        sub = cfg["subagentStatusLine"]["command"]
        self.assertEqual(sub, "%s -I %s" % (sys.executable.replace("\\", "/"), os.path.join(self.claude, "bin", "subagent-statusline.py").replace("\\", "/")))
        self.assertIn("[OK] agent row", r.stdout)                       # the self-check ran the written command
        self.assertEqual(self.install().returncode, 0)                        # idempotent: one entry, no change message the second time
        self.assertEqual(json.loads(rd(os.path.join(self.claude, "settings.json")))["subagentStatusLine"]["command"], sub)

    def test_somebody_elses_subagent_status_line_is_left_alone_and_ours_is_removed(self):
        os.makedirs(self.claude)
        with open(os.path.join(self.claude, "settings.json"), "w") as f:
            json.dump({"subagentStatusLine": {"type": "command", "command": "my-own-thing"}}, f)
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(rd(os.path.join(self.claude, "settings.json")))["subagentStatusLine"]["command"], "my-own-thing")
        self.assertIn("subagentStatusLine is another command", r.stdout)
        os.remove(os.path.join(self.claude, "settings.json"))
        self.install()
        self.assertEqual(self.install("--remove").returncode, 0)
        self.assertNotIn("subagentStatusLine", json.loads(rd(os.path.join(self.claude, "settings.json"))))
        self.assertFalse(self.installed("bin/subagent-statusline.py"))

    def test_free_install_leaves_out_cost_statistics(self):
        r = self.install("--free")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.installed("bin/pace_forecast.py"))
        for f in ("bin/cost.py", "bin/cost_core.py", "bin/cost_parse.py", "bin/cost_share.py", "bin/cost-prices.json"):
            self.assertFalse(self.installed(f), f)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "bin", "cost")))
        udir = os.path.join(self.home, ".config", "systemd", "user")   # only the lost-work job, which every install sets up
        self.assertEqual(sorted(os.listdir(udir)) if os.path.isdir(udir) else [], ["lost-work.service", "lost-work.timer"] if sys.platform.startswith("linux") else [])

    @unittest.skipIf(os.name == "nt", "POSIX installer branch (wrapper in ~/.local/bin, systemd); see WindowsInstaller")
    def test_opt_in_install_files_wrapper_and_units(self):
        r = self.install("--cost")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for f in ("bin/cost.py", "bin/cost_core.py", "bin/cost_parse.py", "bin/cost_share.py", "bin/cost-prices.json", "bin/cost-panel.html"):
            self.assertTrue(self.installed(f), f)
        wrapper = os.path.join(self.home, ".local", "bin", "cost")
        self.assertTrue(os.stat(wrapper).st_mode & stat.S_IXUSR)
        h = subprocess.run([wrapper, "--help"], capture_output=True, text=True, env=clean_env(HOME=self.home), timeout=60)
        self.assertEqual(h.returncode, 0, h.stderr)
        self.assertIn("--window", h.stdout)
        self.assertIn("[OK] cost --save", r.stdout)                 # first scan ran, against the scratch HOME only
        self.assertTrue(os.path.isfile(os.path.join(self.claude, "cost", "status.cache")))
        udir = os.path.join(self.home, ".config", "systemd", "user")
        svc = rd(os.path.join(udir, "cost-daily.service"))
        self.assertIn("ExecStart=%s %s --save" % (sys.executable, os.path.join(self.claude, "bin", "cost.py")), svc)
        self.assertNotIn("@", svc)                                        # every placeholder was filled
        self.assertTrue(svc.startswith("# Installed by"), svc[:60])
        self.assertIn("Description=Claude Code daily cost totals", svc)
        tmr = rd(os.path.join(udir, "cost-daily.timer"))
        self.assertIn("OnCalendar=*-*-* 00/2:23:00", tmr)               # every 2 hours at an off-minute
        self.assertIn("Persistent=true", tmr)
        self.assertIn("RandomizedDelaySec=", tmr)
        # a scratch HOME must never drive the real systemd user manager
        self.assertIn("systemctl not run", r.stdout)
        self.assertFalse(os.path.exists(self.calls), "systemctl was called under a scratch HOME")

    def test_rerun_is_idempotent_and_whatif_writes_nothing(self):
        self.assertEqual(self.install("--cost").returncode, 0)
        r = self.install("--cost")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("same: %s" % os.path.join(self.claude, "bin", "cost.py").replace("\\", "/"), r.stdout)
        self.assertNotIn("backup:", r.stdout.split("settings.json")[0])
        shutil.rmtree(self.root + "/home")
        os.makedirs(self.home)
        w = self.install("--cost", "--whatif")
        self.assertEqual(w.returncode, 0, w.stdout)
        self.assertEqual(os.listdir(self.home), [])

    def test_remove_takes_everything_but_the_history(self):
        self.assertEqual(self.install("--cost").returncode, 0)
        hist = os.path.join(self.claude, "cost", "daily.jsonl")
        with open(hist, "w") as f:
            f.write('{"day":"2026-01-01","usd":1.0}\n')
        r = self.install("--remove")                      # no --cost needed: removal always covers the optional part
        self.assertEqual(r.returncode, 0, r.stdout)
        for f in ("bin/cost.py", "bin/cost_core.py", "bin/cost_parse.py", "bin/cost_share.py", "bin/cost-prices.json", "bin/cost-panel.html", "bin/pace_forecast.py"):
            self.assertFalse(self.installed(f), f)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "bin", "cost")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "systemd", "user", "cost-daily.timer")))
        pyc = os.path.join(self.claude, "bin", "__pycache__")
        self.assertEqual([n for n in (os.listdir(pyc) if os.path.isdir(pyc) else []) if n.startswith("cost")], [])   # no stray byte code
        self.assertTrue(os.path.isfile(hist))         # recorded history is the user's data: kept
        self.assertFalse(os.path.exists(self.calls))

    def run_as_real_home(self, *args):
        """Run install_common.main() with real_home() patched to the scratch HOME, so the branch that drives `systemctl --user`
        (taken on a teammate's real home) executes against the FAKE systemctl first on PATH."""
        code = ("import sys; from pathlib import Path; sys.path.insert(0, %r); import install_common; "
                "install_common.real_home = lambda: Path(%r); install_common.claude_plugin = lambda *a, **k: (True, 'ok'); sys.argv = ['install_common', '--python', %r] + %r; sys.exit(install_common.main())"
                % (PLUG, self.home, sys.executable, copy(args)))
        env = clean_env(HOME=self.home, USERPROFILE=self.home, PATH=self.fakebin + os.pathsep + os.environ.get("PATH", ""))
        self.assertEqual(shutil.which("systemctl", path=env["PATH"]), os.path.join(self.fakebin, "systemctl"))   # never the real one
        return subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)

    @unittest.skipIf(os.name == "nt", "POSIX installer branch (wrapper in ~/.local/bin, systemd); see WindowsInstaller")
    def test_real_home_branch_enables_and_disables_the_timer(self):
        r = self.run_as_real_home("--cost")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rd(self.calls).split("\n")[:2], ["--user daemon-reload", "--user enable --now cost-daily.timer"])
        self.assertNotIn("systemctl not run", r.stdout)
        os.remove(self.calls)
        r = self.run_as_real_home("--remove")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rd(self.calls).split("\n")[:2], ["--user disable --now cost-daily.timer", "--user daemon-reload"])
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "systemd", "user", "cost-daily.service")))

    @unittest.skipIf(os.name == "nt", "POSIX installer branch (systemd drop-ins)")
    def test_old_drop_in_pointing_at_the_managed_copy_is_backed_up_and_removed(self):
        d = os.path.join(self.home, ".config", "systemd", "user", "cost-daily.service.d")
        os.makedirs(d)
        old_ = "[Service]\nExecStart=\nExecStart=/usr/bin/python3 %s/.claude/bin/cost.py --save --share\n" % self.home
        for name in ("share.conf", "other-name.conf"):
            with open(os.path.join(d, name), "w") as f:
                f.write(old_)
        unrelated = "[Service]\nEnvironment=FOO=1\nExecStart=\nExecStart=/usr/local/bin/benim-isim\n"
        with open(os.path.join(d, "benim.conf"), "w") as f:
            f.write(unrelated)
        r = self.run_as_real_home("--cost")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        remaining = sorted(os.listdir(d))
        self.assertEqual([k for k in remaining if k.endswith(".conf")], ["benim.conf"])
        self.assertEqual(len([k for k in remaining if ".conf.backup-" in k]), 2)
        self.assertEqual(rd(os.path.join(d, "benim.conf")), unrelated)
        bak = [k for k in remaining if k.startswith("share.conf.backup-")][0]
        self.assertEqual(rd(os.path.join(d, bak)), old_)
        self.assertEqual(len([l for l in r.stdout.splitlines() if "drop-in" in l and "share.conf" in l]), 1)
        self.assertEqual(rd(self.calls).split("\n")[:2], ["--user daemon-reload", "--user daemon-reload"])   # one after the removal, one before enable

    @unittest.skipIf(os.name == "nt", "POSIX installer branch (systemd drop-ins)")
    def test_old_timer_drop_in_that_pins_a_daily_time_is_backed_up_and_removed(self):
        d = os.path.join(self.home, ".config", "systemd", "user", "cost-daily.timer.d")
        os.makedirs(d)
        old_ = "[Timer]\nOnCalendar=\nOnCalendar=*-*-* 03:40:00\n"
        with open(os.path.join(d, "ranking.conf"), "w") as f:
            f.write(old_)
        other_ = "[Timer]\nOnCalendar=\nOnCalendar=*-*-* 05:00:00\nRandomizedDelaySec=1h\n"      # more than the schedule: not ours to remove
        with open(os.path.join(d, "benim.conf"), "w") as f:
            f.write(other_)
        r = self.run_as_real_home("--cost")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        remaining = sorted(os.listdir(d))
        self.assertEqual([k for k in remaining if k.endswith(".conf")], ["benim.conf"])
        bak = [k for k in remaining if k.startswith("ranking.conf.backup-")]
        self.assertEqual(len(bak), 1)
        self.assertEqual(rd(os.path.join(d, bak[0])), old_)
        self.assertEqual(rd(os.path.join(d, "benim.conf")), other_)
        self.assertEqual(len([l for l in r.stdout.splitlines() if "timer setting" in l and "ranking.conf" in l]), 1)
        self.assertIn("OnCalendar=*-*-* 00/2:23:00", rd(os.path.join(self.home, ".config", "systemd", "user", "cost-daily.timer")))
        self.assertEqual(rd(self.calls).split("\n")[:2], ["--user daemon-reload", "--user daemon-reload"])
        r = self.run_as_real_home("--cost", "--whatif")                    # nothing left to remove, and a dry run removes nothing
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_settings_json_is_merged_not_replaced(self):
        os.makedirs(self.claude)
        with open(os.path.join(self.claude, "settings.json"), "w") as f:
            json.dump({"theme": "dark", "permissions": {"allow": ["Bash(ls:*)"]}}, f)
        self.assertEqual(self.install("--cost").returncode, 0)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        self.assertEqual(cfg["theme"], "dark")
        self.assertEqual(cfg["permissions"], {"allow": ["Bash(ls:*)"]})
        self.assertIn("statusLine", cfg)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not available")
    def test_rendered_units_pass_systemd_analyze_verify(self):
        self.assertEqual(self.install("--cost").returncode, 0)
        udir = os.path.join(self.home, ".config", "systemd", "user")
        r = subprocess.run(["systemd-analyze", "--user", "verify", os.path.join(udir, "cost-daily.service"), os.path.join(udir, "cost-daily.timer")],
                           capture_output=True, text=True, env=clean_env(HOME=self.home), timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("cost-daily", r.stderr.replace(udir, ""))   # no complaint about our units

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "jq/bash not available")
    def test_status_line_segments_follow_the_opt_out(self):
        payload = json.dumps({"model": {"id": "claude-x", "display_name": "X"}, "workspace": {"current_dir": self.home},
                              "prompt_cache": {"hit_ratio": 0.93, "misses": 2}, "cost": {"total_cost_usd": 1.2345}})

        def line(**env):
            e = clean_env(HOME=self.home, **env)
            return subprocess.run(["bash", os.path.join(self.claude, "statusline.sh")], input=payload, capture_output=True, text=True, env=e, timeout=60).stdout
        self.assertEqual(self.install("--free").returncode, 0)
        self.assertNotIn("cache ", line())                                   # opted out: nothing added
        self.assertNotIn("session $", line())
        self.assertEqual(self.install().returncode, 0)                        # the default install has the cost segments
        on = line()
        self.assertIn("cache 93% (2 misses)", on)
        self.assertIn("session $1.23", on)
        self.assertIn("last 24h $", on)
        self.assertNotIn("cache ", line(COST_STATUS="0"))                  # env switch
        open(os.path.join(self.claude, "cost", "off"), "w").close()
        self.assertNotIn("cache ", line())                                   # marker file switch
        self.assertNotIn("session $", line())

    # ---- --direct: run from the clone (no copies), state file for rules_watch.py, --probe, --remove
    COPIES = ("statusline.ps1", "statusline.sh", "bin/statusline-local.ps1", "bin/pace-hook.py", "bin/pace_forecast.py", "bin/line_wrap.py",
              "bin/subagent-statusline.py", "bin/cost.py", "bin/cost_core.py", "bin/cost-prices.json", "bin/cost-panel.html")

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

    def test_direct_points_everything_into_the_clone_and_removes_old_copies(self):
        self.assertEqual(self.install().returncode, 0)                        # an earlier copy install
        self.assertTrue(self.installed("bin/pace-hook.py"))
        before = self.plugin_snapshot()
        r = self.install("--direct")
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
            sp = plug + "/bin/statusline-local.ps1"
            self.assertIn("\"if (Test-Path '%s') { & '%s' } else { 'Claude' }\"" % (sp, sp), status)
        else:
            self.assertTrue(status.endswith(plug + "/statusline.sh"), status)
        self.assertTrue(cfg["subagentStatusLine"]["command"].endswith(repo + "/plugins/crew-agents/tools/subagent-statusline.py"))
        status_ = json.loads(rd(os.path.join(self.claude, "quota-pace.json")))
        self.assertEqual((status_["mode"], status_["repo"], status_["python"]), ("direct", repo, sys.executable.replace("\\", "/")))
        self.assertEqual(status_["argv"][:2], ["--python", sys.executable.replace("\\", "/")])
        self.assertEqual(status_["argv"][-1], "--direct")
        if os.name == "nt" and PWSH:
            self.assertEqual(rd(os.path.join(self.claude, "bin", "cost.cmd")),
                             '@"%s" "%s/bin/cost.py" %%*\n' % (sys.executable.replace("\\", "/"), plug))
        else:
            self.assertIn('"%s/bin/cost.py"' % plug, rd(os.path.join(self.home, ".local", "bin", "cost")))
        for ok in ("[OK] hook SessionStart", "[OK] cost --save", "[OK] status line", "[OK] agent row"):
            self.assertIn(ok, r.stdout)
        again = self.install("--direct")                                     # idempotent
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertIn("settings.json: no change", again.stdout)
        self.assertIn("same: %s/quota-pace.json" % self.claude.replace("\\", "/"), again.stdout)

    def test_direct_source_is_the_default_with_backups_a_fallback_and_saved_flags(self):
        self.assertEqual(self.install("--copy").returncode, 0)
        self.assertTrue(self.installed("bin/cost.py"))                 # an older copy install
        old = rd(os.path.join(self.claude, "bin", "pace-hook.py"))
        r = self.install_raw()                                                   # no mode flag: direct source
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for f in self.COPIES:
            self.assertFalse(self.installed(f), f)                        # the copies are gone ...
        baks = [n for n in os.listdir(os.path.join(self.claude, "bin")) if n.startswith("pace-hook.py.backup-")]
        self.assertEqual(len(baks), 1)                                    # ... with a backup
        self.assertEqual(rd(os.path.join(self.claude, "bin", baks[0])), old)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        plug = PLUG.replace("\\", "/")
        self.assertIn(plug, cfg["statusLine"]["command"])
        self.assertIn("crew-agents/tools/subagent-statusline.py", cfg["subagentStatusLine"]["command"])
        self.assertNotIn(os.path.join(self.claude, "bin"), json.dumps(cfg))
        if not (os.name == "nt" and PWSH):
            # a missing clone / failing script prints the minimal fallback line instead of an error
            sh = cfg["statusLine"]["command"].replace(plug + "/statusline.sh", plug + "/missing/statusline.sh")
            o = subprocess.run(sh, shell=True, input="{}", capture_output=True, text=True)
            self.assertEqual((o.stdout, o.stderr), ("Claude", ""))
        self.assertEqual(json.loads(rd(os.path.join(self.claude, "quota-pace.json")))["setup"], [])
        again = self.install_raw()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(json.loads(rd(os.path.join(self.claude, "settings.json"))), cfg)   # idempotent

    def test_direct_probe_runs_the_quick_check_only(self):
        self.assertEqual(self.install("--direct").returncode, 0)
        before = rd(os.path.join(self.claude, "settings.json"))
        r = self.install("--direct", "--probe")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for ok in ("[OK] hook SessionStart", "[OK] hook UserPromptSubmit", "[OK] cost --help", "[OK] status line", "[OK] agent row"):
            self.assertIn(ok, r.stdout)
        self.assertNotIn("--save", r.stdout)                            # no first scan: it must stay fast
        self.assertNotIn("settings.json", r.stdout)                       # installs nothing
        self.assertEqual(rd(os.path.join(self.claude, "settings.json")), before)

    def test_remove_undoes_direct_and_copy_mode_drops_the_state_file(self):
        self.assertEqual(self.install("--direct").returncode, 0)
        r = self.install()                                                    # back to the copied install
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.installed("quota-pace.json"))
        self.assertTrue(self.installed("bin/pace-hook.py"))
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        self.assertEqual(cfg["hooks"]["SessionStart"][0]["hooks"][0]["args"][1], os.path.join(self.claude, "bin", "pace-hook.py").replace("\\", "/"))
        self.assertEqual(self.install("--direct").returncode, 0)
        r = self.install("--remove")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        for k in ("hooks", "statusLine", "subagentStatusLine"):
            self.assertNotIn(k, cfg)
        self.assertFalse(self.installed("quota-pace.json"))
        self.assertFalse(self.installed("bin/cost.cmd"))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "bin", "cost")))
        self.assertTrue(os.path.isfile(os.path.join(PLUG, "bin", "pace-hook.py")))   # the clone is never touched

    def test_direct_needs_the_cost_statistics(self):
        r = self.install("--direct", "--free")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("cannot be given together with --free", r.stdout)
        self.assertEqual(os.listdir(self.home), [])                       # nothing written

    @unittest.skipIf(os.name == "nt", "POSIX installer branch (systemd)")
    def test_direct_systemd_unit_runs_the_clone(self):
        r = self.run_as_real_home("--direct")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        svc = rd(os.path.join(self.home, ".config", "systemd", "user", "cost-daily.service"))
        self.assertIn("ExecStart=%s %s --save" % (sys.executable, os.path.join(PLUG, "bin", "cost.py")), svc)



    def test_default_install_adds_the_rules_watch_hook_from_the_clone(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        rw = [h for g in cfg["hooks"]["SessionStart"] for h in g["hooks"] if "rules_watch" in json.dumps(h)]
        self.assertEqual(len(rw), 1)
        self.assertEqual(rw[0]["args"][-1], os.path.join(os.path.dirname(os.path.dirname(PLUG)), "tools", "rules_watch.py").replace("\\", "/"))
        self.assertEqual(self.install().returncode, 0)                 # a re-run adds no second entry
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        self.assertEqual(sum("rules_watch" in json.dumps(h) for g in cfg["hooks"]["SessionStart"] for h in g["hooks"]), 1)
        self.assertEqual(self.install("--remove").returncode, 0)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json"))) if os.path.exists(os.path.join(self.claude, "settings.json")) else {}
        self.assertNotIn("rules_watch", json.dumps(cfg))

    def test_default_install_adds_agent_watchdog_note_and_remove_takes_it_out(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("agent_watchdog_note PostToolUse", r.stdout)
        self.assertIn("[OK] agent_name PreToolUse", r.stdout)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        self.assertEqual(sum("agent_watchdog_note" in json.dumps(h) for g in cfg["hooks"]["PostToolUse"] for h in g["hooks"]), 1)
        self.assertEqual(self.install("--remove").returncode, 0)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json"))) if os.path.exists(os.path.join(self.claude, "settings.json")) else {}
        self.assertNotIn("agent_watchdog_note", json.dumps(cfg))

    def test_default_install_adds_stop_and_subagentstop_evidence_hook_and_remove_takes_them_out(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("evidence_hook SubagentStop", r.stdout)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        for ev in ("Stop", "SubagentStop"):
            self.assertEqual(sum("evidence_hook" in json.dumps(h) for g in cfg["hooks"][ev] for h in g["hooks"]), 1, ev)
        self.assertEqual(self.install().returncode, 0)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        self.assertEqual(sum("evidence_hook" in json.dumps(h) for g in cfg["hooks"]["Stop"] for h in g["hooks"]), 1)
        self.assertEqual(self.install("--remove").returncode, 0)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json"))) if os.path.exists(os.path.join(self.claude, "settings.json")) else {}
        self.assertNotIn("evidence_hook", json.dumps(cfg))

    @unittest.skipIf(os.name == "nt", "POSIX installer branch (systemd); see WindowsInstaller")
    def test_real_home_installs_the_daily_lost_work_timer_from_the_clone(self):
        r = self.run_as_real_home()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("--user enable --now lost-work.timer", rd(self.calls).split("\n"))
        svc = rd(os.path.join(self.home, ".config", "systemd", "user", "lost-work.service"))
        lost = os.path.join(os.path.dirname(os.path.dirname(PLUG)), "tools", "daily_lost_work.py")
        self.assertIn("ExecStart=%s -I %s\n" % (sys.executable, lost), svc)
        self.assertNotIn("--force", svc)
        os.remove(self.calls)
        r = self.run_as_real_home("--remove")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("--user disable --now lost-work.timer", rd(self.calls).split("\n"))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "systemd", "user", "lost-work.timer")))


@unittest.skipUnless(os.name == "nt" and PWSH, "Windows + PowerShell only")
class WindowsInstaller(unittest.TestCase):
    """install_common.py --windows under a scratch USERPROFILE: files, cost.cmd, the Task Scheduler branch (never the real one)
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

    def install(self, *args):
        return subprocess.run([sys.executable, "-I", INSTALL, "--windows", "--python", sys.executable, "--shell", PWSH, *copy(args)],
                              capture_output=True, text=True, encoding="utf-8", env=self.env(), timeout=600)

    def as_real_home(self, *args):
        """install_common.main() with real_home() patched to the scratch HOME and run_cmd_task() replaced by a recorder
        (the real Task Scheduler is never reached). Returns (process, list of recorded [snippet first line, exe, script])."""
        code = ("import sys, json; from pathlib import Path; sys.path.insert(0, %r); import install_common; calls = []; kcalls = []; sgcalls = []; "
                "install_common.real_home = lambda: Path(%r); install_common.claude_plugin = lambda *a, **k: (True, 'ok'); "
                "install_common.run_cmd_sp_task = lambda *a, **k: True; "
                "install_common.run_cmd_task = lambda shell, ps, exe, script, **kw: (kcalls if kw.get('name') == 'lost-work' else sgcalls if kw.get('name') in ('slack-inbox', 'slack-triage') else calls).append([ps.strip().splitlines()[-1][:60], exe, script]) or True; "
                "sys.argv = ['install_common', '--windows', '--python', %r, '--shell', %r] + %r; rc = install_common.main(); "
                "print('CALLS' + json.dumps(calls)); print('KCALLS' + json.dumps(kcalls)); sys.exit(rc)"
                % (PLUG, self.home, sys.executable, PWSH, copy(args)))
        r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, encoding="utf-8", env=self.env(), timeout=600)
        calls = [json.loads(l[5:]) for l in r.stdout.splitlines() if l.startswith("CALLS")]
        self.kcalls = ([json.loads(l[6:]) for l in r.stdout.splitlines() if l.startswith("KCALLS")] or [None])[0]   # the lost-work task
        return r, (calls[0] if calls else None)

    def line(self, payload, **env):
        e = self.env(**env)
        return subprocess.run([PWSH, "-NoProfile", "-File", os.path.join(self.claude, "bin", "statusline-local.ps1")], input=payload,
                              capture_output=True, text=True, encoding="utf-8", env=e, timeout=120).stdout

    def test_install_writes_files_cmd_and_leaves_task_scheduler_alone_under_a_scratch_profile(self):
        r = self.install("--cost")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for f in ("statusline.ps1", "bin/statusline-local.ps1", "bin/cost.py", "bin/cost_core.py", "bin/cost-panel.html"):
            self.assertTrue(os.path.exists(os.path.join(self.claude, f)), f)
        cmd = rd(os.path.join(self.claude, "bin", "cost.cmd"))
        self.assertTrue(cmd.startswith('@"%s"' % sys.executable.replace("\\", "/")), cmd)
        self.assertIn("task scheduler not run", r.stdout)
        self.assertIn("[OK] cost --save", r.stdout)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        self.assertIn("statusline-local.ps1", cfg["statusLine"]["command"])
        self.assertNotIn("\\", cfg["statusLine"]["command"])                 # forward slashes only
        self.assertFalse(os.path.exists(os.path.join(self.claude, "pace-peer.json")))

    def test_real_profile_registers_and_removes_the_user_task(self):
        r, calls = self.as_real_home("--cost")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNotNone(calls)
        self.assertEqual(len(calls), 1)
        _, exe, script = calls[0]
        self.assertTrue(exe.lower().endswith(("pythonw.exe", "python.exe")), exe)
        self.assertTrue(script.endswith("cost.py") and "\\" in script, script)
        self.assertIn("[OK] task scheduler: 'cost-daily'", r.stdout)
        r, calls = self.as_real_home("--remove")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(calls), 1)                            # the removal snippet was run once
        r, calls = self.as_real_home("--cost", "--whatif")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(calls, [])                                # -WhatIf registers nothing
        self.assertIn("[-WhatIf] task scheduler", r.stdout)

    def test_direct_task_runs_the_clone_and_the_status_line_finds_its_siblings_there(self):
        r, calls = self.as_real_home("--direct")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(calls[0][2], os.path.join(PLUG, "bin", "cost.py").replace("/", "\\"))   # the task's script is the clone's
        # the configured command is the clone's wrapper; with no copies in the profile, the cost segments come from the clone's bin/
        self.assertFalse(os.path.exists(os.path.join(self.claude, "bin", "cost.py")))
        payload = json.dumps({"model": {"id": "claude-x", "display_name": "X"}, "workspace": {"current_dir": self.home.replace("\\", "/")},
                              "prompt_cache": {"hit_ratio": 0.93, "misses": 2}, "cost": {"total_cost_usd": 1.2345}})
        out = subprocess.run([PWSH, "-NoProfile", "-File", os.path.join(PLUG, "bin", "statusline-local.ps1")], input=payload,
                             capture_output=True, text=True, encoding="utf-8", env=self.env(), timeout=120).stdout
        self.assertIn("cache 93% (2 misses)", out)
        self.assertIn("session $1.23", out)

    def test_task_snippets_are_user_level_only(self):
        sys.path.insert(0, PLUG)
        import install_common
        k = install_common.TASK_RECORD_PS
        for need in ("Register-HiddenTask", "-Once -At", "AddMinutes(23)", "-RepetitionInterval (New-TimeSpan -Hours 2)", "-StartWhenAvailable", "--save", "Priority"):
            self.assertIn(need, k)
        for bad in ("Highest", "SYSTEM", "-Password", "schtasks"):
            self.assertNotIn(bad, k)
        self.assertIn("Unregister-ScheduledTask", install_common.TASK_DELETE_PS)

    def test_status_line_cost_segments_opt_out_and_local_extension(self):
        payload = json.dumps({"model": {"id": "claude-x", "display_name": "X"}, "workspace": {"current_dir": self.home.replace("\\", "/")},
                              "prompt_cache": {"hit_ratio": 0.93, "misses": 2}, "cost": {"total_cost_usd": 1.2345}})
        self.assertEqual(self.install("--free").returncode, 0)
        base = self.line(payload)
        self.assertNotIn("cache ", base)                       # opted out: nothing added
        self.assertNotIn("session $", base)
        self.assertEqual(self.install().returncode, 0)
        on = self.line(payload)
        self.assertIn("cache 93% (2 misses)", on)
        self.assertIn("session $1.23", on)
        self.assertIn("last 24h $", on)
        self.assertNotIn("cache ", self.line(payload, COST_STATUS="0"))
        open(os.path.join(self.claude, "cost", "off"), "w").close()
        off = self.line(payload)
        self.assertNotIn("cache ", off)
        self.assertNotIn("session $", off)
        os.remove(os.path.join(self.claude, "cost", "off"))
        with open(os.path.join(self.claude, "bin", "statusline-extra.ps1"), "w", encoding="utf-8") as f:
            f.write("param([string]$Line)\n$Line + ' | EK-TEST'\n")
        self.assertTrue(self.line(payload).strip().endswith("EK-TEST"))

    def test_status_line_still_writes_pace_json(self):
        self.assertEqual(self.install().returncode, 0)
        now = int(time.time())          # a real reset time: the placeholder 4102444800 (year 2100) is deliberately ignored by the status line
        payload = json.dumps({"model": {"id": "claude-x", "display_name": "X"}, "workspace": {"current_dir": self.home.replace("\\", "/")},
                              "rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": now + 7200},
                                              "seven_day": {"used_percentage": 20, "resets_at": now + 3 * 86400}}})
        out = self.line(payload)
        self.assertIn("week 20%", out)
        pace = json.loads(rd(os.path.join(self.claude, "pace.json")))
        self.assertIn(pace["mode"], ("PLENTY", "NORMAL", "TIGHT"))
        self.assertEqual(pace["week_used"], 20)


    def test_real_profile_registers_and_removes_the_lost_work_task(self):
        r, _ = self.as_real_home("--direct")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.kcalls), 1)
        _, exe, script = self.kcalls[0]
        lost = os.path.join(os.path.dirname(os.path.dirname(PLUG)), "tools", "daily_lost_work.py")
        self.assertEqual(script, lost.replace("/", "\\"))       # the clone's script, never a copy
        self.assertIn("[OK] task scheduler: 'lost-work'", r.stdout)
        self.assertIn("[OK] daily_lost_work --help", r.stdout)
        r, _ = self.as_real_home("--remove")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.kcalls), 1)                       # the removal snippet
        self.assertIn("Unregister-ScheduledTask", install_common_module().TASK_DELETE_PS)

    def test_lost_work_task_snippet_is_user_level_and_never_forces(self):
        k = install_common_module()
        for need in ("Register-HiddenTask", "-Daily -At '17:30'", "-StartWhenAvailable"):
            self.assertIn(need, k.LOST_TASK_RECORD_PS)
        for bad in ("Highest", "SYSTEM", "-Password", "schtasks"):
            self.assertNotIn(bad, k.LOST_TASK_RECORD_PS)
        self.assertNotIn("LOST_ARGS", k.LOST_TASK_RECORD_PS)   # the options live in tools/daily_lost_work.py, not in the scheduler
        import importlib.util
        spec = importlib.util.spec_from_file_location("daily_lost_work", str(k.LOST_ENTRY))
        g = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(g)
        self.assertIn("--push-unpushed", g.ARGS)
        self.assertIn("--cache-write", g.ARGS)
        self.assertNotIn("--force", g.ARGS)


def install_common_module():
    sys.path.insert(0, PLUG)
    import install_common
    return install_common


class RulesWatchHookMerge(unittest.TestCase):
    """merge_rules_watch(): one rules_watch entry pointing into the clone, a hand-written one is replaced, others are kept."""

    def test_hand_written_entry_is_replaced_others_kept_and_remove_takes_only_ours(self):
        k = install_common_module()
        other = {"type": "command", "command": "echo hi"}
        cfg = {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [
            {"type": "command", "command": "python ~/source/repos/claudeshared/tools/rules_watch.py", "timeout": 120}, other]}]}}
        self.assertEqual(k.merge_rules_watch(cfg, "/usr/bin/python3", False),
                         ["SessionStart: rules_watch entry updated",
                          "UserPromptSubmit: rules_watch entry added (rule warnings + lost-work report)"])
        hooks = cfg["hooks"]["SessionStart"][0]["hooks"]
        self.assertEqual(hooks[1], other)
        self.assertEqual(hooks[0]["args"][:2], ["-I", "-c"])
        self.assertEqual(hooks[0]["args"][-1], k.RULES_WATCH.as_posix())
        prompt = cfg["hooks"]["UserPromptSubmit"][0]["hooks"][0]          # the cheap per-prompt delta of the background run
        self.assertEqual(prompt["args"][-2:], [k.RULES_WATCH.as_posix(), "--request"])
        self.assertEqual(k.merge_rules_watch(cfg, "/usr/bin/python3", False), [])       # idempotent
        k.merge_rules_watch(cfg, "/usr/bin/python3", True)
        self.assertEqual(cfg["hooks"]["SessionStart"][0]["hooks"], [other])
        self.assertNotIn("UserPromptSubmit", cfg["hooks"])
        empty = {}
        self.assertEqual(k.merge_rules_watch(empty, "py", False),
                         ["SessionStart: rules_watch entry added (rule warnings + lost-work report)",
                          "UserPromptSubmit: rules_watch entry added (rule warnings + lost-work report)"])
        self.assertEqual(empty["hooks"]["SessionStart"][0]["matcher"], "startup")
        k.merge_rules_watch(empty, "py", True)
        self.assertEqual(empty, {})

    def test_evidence_hook_stop_and_subagentstop_replace_hand_entry_keep_others_and_remove(self):
        k = install_common_module()
        other = {"type": "command", "command": "echo hi"}
        hand = {"type": "command", "timeout": 10,
                "command": "test -f ~/x/claude-shared/tools/evidence_hook.py && python3 -I ~/x/claude-shared/tools/evidence_hook.py || true"}
        cfg = {"hooks": {"Stop": [{"hooks": [hand, other]}]}}
        ch = k.merge_evidence_hook(cfg, "/usr/bin/python3", False)
        self.assertEqual(sorted(c.split(":")[0] for c in ch), ["Stop", "SubagentStop"])
        self.assertIn("updated", [c for c in ch if c.startswith("Stop:")][0])
        self.assertIn("added", [c for c in ch if c.startswith("SubagentStop:")][0])
        for ev in ("Stop", "SubagentStop"):
            ours = [h for g in cfg["hooks"][ev] for h in g["hooks"] if "evidence_hook" in json.dumps(h)]
            self.assertEqual(len(ours), 1, ev)
            self.assertEqual(ours[0]["args"][:2], ["-I", "-c"])
            self.assertEqual(ours[0]["args"][-1], k.EVIDENCE.as_posix())
        self.assertEqual(cfg["hooks"]["Stop"][0]["hooks"][1], other)
        self.assertEqual(k.merge_evidence_hook(cfg, "/usr/bin/python3", False), [])      # idempotent
        k.merge_evidence_hook(cfg, "/usr/bin/python3", True)
        self.assertEqual(cfg, {"hooks": {"Stop": [{"hooks": [other]}]}})
        empty = {}
        k.merge_evidence_hook(empty, "py", False)
        k.merge_evidence_hook(empty, "py", True)
        self.assertEqual(empty, {})

    def test_agent_watchdog_note_posttooluse_agent_matcher_install_idempotent_keeps_others_and_remove(self):
        k = install_common_module()
        other = {"type": "command", "command": "echo hi"}
        cfg = {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [other]}]}}
        self.assertEqual(len(k.merge_agent_watchdog(cfg, sys.executable, False)), 1)
        grp = [g for g in cfg["hooks"]["PostToolUse"] if g.get("matcher") == "Agent"]
        self.assertEqual(len(grp), 1)
        self.assertEqual(grp[0]["hooks"][0]["args"][-1], k.WATCHDOG.as_posix())
        self.assertEqual(k.merge_agent_watchdog(cfg, sys.executable, False), [])      # idempotent
        h = grp[0]["hooks"][0]
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run([h["command"], *h["args"]], capture_output=True, text=True, timeout=60,
                               env={**os.environ, "AGENT_WATCHDOG_STATE": os.path.join(td, "s.json")},
                               input=json.dumps({"session_id": "t", "tool_name": "Agent", "tool_input": {"run_in_background": True}}))
        self.assertIn("additionalContext", r.stdout)
        k.merge_agent_watchdog(cfg, sys.executable, True)
        self.assertEqual(cfg, {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [other]}]}})
        empty = {}
        k.merge_agent_watchdog(empty, "py", False)
        k.merge_agent_watchdog(empty, "py", True)
        self.assertEqual(empty, {})

    def test_agent_name_pretooluse_install_idempotent_names_an_agent_call_and_remove(self):
        k = install_common_module()
        other = {"type": "command", "command": "echo hi"}
        cfg = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [other]}]}}
        self.assertEqual(len(k.merge_agent_name(cfg, sys.executable, False)), 1)
        grp = [g for g in cfg["hooks"]["PreToolUse"] if g.get("matcher") == "Agent|Task"]
        self.assertEqual(len(grp), 1)
        self.assertEqual(grp[0]["hooks"][0]["args"][-1], k.AGENT_NAME.as_posix())
        self.assertEqual(k.merge_agent_name(cfg, sys.executable, False), [])        # idempotent
        h = grp[0]["hooks"][0]
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run([h["command"], *h["args"]], capture_output=True, text=True, encoding="utf-8", timeout=60,
                               env={**os.environ, "CREW_AGENT_NAME_DIR": td},
                               input=json.dumps({"session_id": "t", "tool_name": "Agent", "tool_input": {"prompt": "p", "description": "d"}}))
        new = json.loads(r.stdout)["hookSpecificOutput"]["updatedInput"]
        self.assertTrue(new["description"].endswith(" · d"))
        k.merge_agent_name(cfg, sys.executable, True)
        self.assertEqual(cfg, {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [other]}]}})
        empty = {}
        k.merge_agent_name(empty, "py", False)
        k.merge_agent_name(empty, "py", True)
        self.assertEqual(empty, {})

    def test_evidence_hook_command_runs_the_clone_script_and_blocks_a_hedge(self):
        k = install_common_module()
        cfg = {}
        k.merge_evidence_hook(cfg, sys.executable, False)
        h = cfg["hooks"]["SubagentStop"][0]["hooks"][0]
        r = subprocess.run([h["command"], *h["args"]], input=json.dumps({"hook_event_name": "SubagentStop", "last_assistant_message": "This is probably wrong."}),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["decision"], "block")

    def test_hook_command_is_silent_when_the_clone_is_missing_and_runs_the_script_when_present(self):
        k = install_common_module()
        tmp = tempfile.mkdtemp(prefix="rw-backup-")
        self.addCleanup(shutil.rmtree, tmp, True)
        missing = subprocess.run([sys.executable, "-I", "-c", k.RW_BACKUP, os.path.join(tmp, "missing", "rules_watch.py")],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual((missing.returncode, missing.stdout, missing.stderr), (0, "", ""))
        script = os.path.join(tmp, "rules_watch.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import sys\nprint('ran', __name__)\nsys.exit(0)\n")
        present = subprocess.run([sys.executable, "-I", "-c", k.RW_BACKUP, script], capture_output=True, text=True, timeout=60)
        self.assertEqual((present.returncode, present.stdout.strip()), (0, "ran __main__"))


class ClaudePlugins(unittest.TestCase):
    """claude_plugins(): `claude plugin install` for every enabled claude-shared plugin, idempotent, a marketplace unknown to
    Claude Code is added and the install retried, skipped without `claude` or outside the real HOME, --remove uninstalls."""

    def run_it(self, enabled, remove=False, whatif=False, claude="claude", real_home=True, fail_first_install=False):
        k = install_common_module()
        calls = []

        def fake(exe, args, timeout=180):
            calls.append(args)
            if fail_first_install and args[0] == "install" and sum(1 for c in calls if c[0] == "install") == 1:
                return False, "marketplace claude-shared not found"
            return True, "ok"
        old_ = (k.claude_plugin, k.shutil.which, k.real_home)
        k.claude_plugin = fake
        k.shutil.which = lambda n: claude if n == "claude" else old_[1](n)
        k.real_home = lambda: Path.home() if real_home else Path(tempfile.gettempdir()) / "other"
        try:
            a = type("A", (), {"remove": remove, "whatif": whatif})()
            ok = k.claude_plugins({"enabledPlugins": enabled}, a, "")
        finally:
            k.claude_plugin, k.shutil.which, k.real_home = old_
        return ok, calls

    def test_install_only_claude_shared_enabled(self):
        ok, calls = self.run_it({"crew-agents@claude-shared": True, "x@y": True, "old@claude-shared": False})
        self.assertEqual((ok, calls), (True, [["install", "crew-agents@claude-shared", "--scope", "user"]]))

    def test_unknown_marketplace_is_added_then_install_retried(self):
        ok, calls = self.run_it({"crew-agents@claude-shared": True}, fail_first_install=True)
        self.assertTrue(ok)
        self.assertEqual([c[0] for c in calls], ["install", "marketplace", "install"])
        self.assertEqual(calls[1][:2], ["marketplace", "add"])

    def test_skips(self):
        self.assertEqual(self.run_it({"crew-agents@claude-shared": True}, claude=None), (True, []))   # claude not on PATH
        self.assertEqual(self.run_it({"crew-agents@claude-shared": True}, real_home=False), (True, []))   # scratch HOME
        self.assertEqual(self.run_it({"crew-agents@claude-shared": True}, whatif=True), (True, []))
        self.assertEqual(self.run_it({"x@y": True}), (True, []))

    def test_remove_uninstalls(self):
        ok, calls = self.run_it({}, remove=True)
        self.assertEqual((ok, calls), (True, [["uninstall", "crew-agents@claude-shared", "--scope", "user"]]))


class CrewAgentsMerge(unittest.TestCase):
    """merge_crew_agents(): the claude-shared marketplace points at this clone (directory source, loaded in place), the merged
    agent plugin is enabled, the merged-away sets are dropped, and --remove takes back only what is ours."""

    def test_install_idempotent_keeps_others_and_remove(self):
        k = install_common_module()
        source = {"source": "directory", "path": k.CLONE.as_posix()}
        cfg = {"enabledPlugins": {"team-agents@claude-shared": True, "x@y": True},
               "extraKnownMarketplaces": {"claude-shared": {"source": {"source": "github", "repo": "example-org/claude-shared"}, "autoUpdate": True},
                                          "other": {"source": {"source": "github", "repo": "a/b"}}}}
        self.assertEqual(len(k.merge_crew_agents(cfg, False)), 3)
        self.assertEqual(cfg["enabledPlugins"], {"x@y": True, "crew-agents@claude-shared": True})
        self.assertEqual(cfg["extraKnownMarketplaces"]["claude-shared"], {"source": source, "autoUpdate": True})
        self.assertIn("other", cfg["extraKnownMarketplaces"])
        self.assertEqual(k.merge_crew_agents(cfg, False), [])      # idempotent
        k.merge_crew_agents(cfg, True)
        self.assertEqual(cfg["enabledPlugins"], {"x@y": True})
        self.assertNotIn("claude-shared", cfg["extraKnownMarketplaces"])
        empty = {}
        k.merge_crew_agents(empty, False)
        k.merge_crew_agents(empty, True)
        self.assertEqual(empty, {})

    def test_remove_keeps_the_marketplace_while_another_claude_shared_plugin_is_enabled(self):
        k = install_common_module()
        cfg = {"enabledPlugins": {"quota-pace@claude-shared": True}}
        k.merge_crew_agents(cfg, False)
        k.merge_crew_agents(cfg, True)
        self.assertEqual(cfg["enabledPlugins"], {"quota-pace@claude-shared": True})
        self.assertIn("claude-shared", cfg["extraKnownMarketplaces"])

    def test_the_marketplace_source_resolves_to_the_plugin_in_this_clone(self):
        k = install_common_module()
        cfg = {}
        k.merge_crew_agents(cfg, False)
        kok = cfg["extraKnownMarketplaces"]["claude-shared"]["source"]["path"]
        with open(os.path.join(kok, ".claude-plugin", "marketplace.json"), encoding="utf-8") as f:
            sunday = json.load(f)
        self.assertEqual(sunday["name"], k.MARKETPLACE)
        entry = [p for p in sunday["plugins"] if p["name"] == k.AGENT_PLUGIN.split("@")[0]]
        self.assertEqual(len(entry), 1)
        self.assertTrue(entry[0]["source"].startswith("./"))     # relative path: Claude Code loads it in place from the clone
        self.assertTrue(os.path.isdir(os.path.join(kok, entry[0]["source"], "output-styles")))
        self.assertFalse(os.path.exists(os.path.join(kok, entry[0]["source"], "agents")))   # agents are linked, not plugin-loaded (#34)
        self.assertFalse([p for p in sunday["plugins"] if p["name"] + "@" + k.MARKETPLACE in k.OLD_PLUGINS])


class CrewAgentsInstall(unittest.TestCase):
    """End to end under a scratch HOME: the install enables the plugin from the clone, links the agents and clears old-name agent files."""
    setUp, tearDown, install, install_raw = Installer.setUp, Installer.tearDown, Installer.install, Installer.install_raw   # same scratch HOME, not the Installer tests

    def test_install_enables_the_plugin_and_backs_up_old_agent_files(self):
        agents = os.path.join(self.claude, "agents")
        os.makedirs(agents)
        for name in ("quick-task", "haiku-worker", "own-ajanim"):
            with open(os.path.join(agents, name + ".md"), "w", encoding="utf-8") as f:
                f.write("---\nname: %s\n---\n" % name)
        r = self.install_raw("--direct")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json")))
        self.assertIs(cfg["enabledPlugins"]["crew-agents@claude-shared"], True)
        self.assertEqual(cfg["extraKnownMarketplaces"]["claude-shared"]["source"]["source"], "directory")
        remaining = sorted(os.listdir(agents))
        self.assertIn("own-ajanim.md", remaining)                       # not ours: left alone
        self.assertIn("claude-shared", remaining)                          # the agents link -> <clone>/agents
        self.assertTrue(os.path.isfile(os.path.join(agents, "claude-shared", "quick-task-hm.md")))
        self.assertNotIn("quick-task.md", remaining)
        self.assertNotIn("haiku-worker.md", remaining)
        self.assertEqual(len([f for f in remaining if f.startswith(("quick-task.md.backup-", "haiku-worker.md.backup-"))]), 2)
        self.assertEqual(self.install("--remove").returncode, 0)
        cfg = json.loads(rd(os.path.join(self.claude, "settings.json"))) if os.path.exists(os.path.join(self.claude, "settings.json")) else {}
        self.assertNotIn("crew-agents@claude-shared", json.dumps(cfg))
        self.assertNotIn("claude-shared", os.listdir(agents))        # --remove takes the link away
        self.assertIn("own-ajanim.md", os.listdir(agents))


if __name__ == "__main__":
    unittest.main()

"""Tests for tools/settings.py: the order of the config layers and the optional team migration hook.
Run: python -m unittest discover -s tools/tests"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))
import settings  # noqa: E402


class Layers(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.personal = os.path.join(self.d, "personal")
        self.team = os.path.join(self.d, "team-config")
        os.makedirs(self.personal)
        os.makedirs(self.team)
        for p in (mock.patch.object(settings, "PERSONAL_DIR", self.personal), mock.patch.object(settings, "TEAM_DIR", self.team),
                  mock.patch.dict(os.environ, {"KIT_TEST_KEY": ""})):
            p.start()
            self.addCleanup(p.stop)

    def put(self, folder, name, text):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_default_when_nothing_is_set(self):
        self.assertEqual(settings.value("org", "KIT_TEST_KEY", "fallback"), "fallback")
        self.assertEqual(settings.value("org", "KIT_TEST_KEY"), "")

    def test_team_config_beats_the_default(self):
        self.put(self.team, "org", "team-org\nsecond line ignored\n")
        self.assertEqual(settings.value("org", "KIT_TEST_KEY", "fallback"), "team-org")

    def test_personal_file_beats_team_config(self):
        self.put(self.team, "org", "team-org\n")
        self.put(self.personal, "org", "  my-org  \n")
        self.assertEqual(settings.value("org", "KIT_TEST_KEY"), "my-org")

    def test_env_beats_every_file(self):
        self.put(self.team, "org", "team-org\n")
        self.put(self.personal, "org", "my-org\n")
        with mock.patch.dict(os.environ, {"KIT_TEST_KEY": " env-org "}):
            self.assertEqual(settings.value("org", "KIT_TEST_KEY"), "env-org")

    def test_empty_layers_fall_through(self):
        self.put(self.personal, "org", "\n")
        self.put(self.team, "org", "team-org\n")
        self.assertEqual(settings.value("org", "KIT_TEST_KEY"), "team-org")

    def test_personal_keys_never_read_team_config(self):
        self.put(self.team, "person", "Everyone\n")
        self.assertEqual(settings.value("person", "KIT_TEST_KEY", team=False), "")
        self.put(self.personal, "person", "Me\n")
        self.assertEqual(settings.value("person", "KIT_TEST_KEY", team=False), "Me")

    def test_bom_is_not_part_of_the_value(self):
        with open(os.path.join(self.team, "org"), "wb") as f:
            f.write(b"\xef\xbb\xbfteam-org\r\n")
        self.assertEqual(settings.value("org", "KIT_TEST_KEY"), "team-org")

    def test_callers_use_the_layer(self):
        """gh_board, vault_cli, cost_share and last_window read their team keys through settings.value."""
        self.put(self.team, "vault-find-excluding", "secret/archive/, kv/offline\n")
        import vault_cli
        self.assertEqual(vault_cli.find_exclude(), ("secret/archive/", "kv/offline/"))


class TeamMigrate(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.script = os.path.join(self.d, "team_migrate.py")
        self.lock = os.path.join(self.d, "claude", "team-migrate.lock")
        for p in (mock.patch.object(settings, "MIGRATE", self.script), mock.patch.object(settings, "LOCK", self.lock)):
            p.start()
            self.addCleanup(p.stop)

    def write(self, body):
        with open(self.script, "w", encoding="utf-8") as f:
            f.write(body)

    def test_no_script_is_a_no_op(self):
        with mock.patch.object(settings.hidden, "run") as run:
            self.assertEqual(settings.team_migrate(), [])
        run.assert_not_called()

    def test_runs_ensure_and_relays_its_output(self):
        self.write("import sys\nprint('migrated:', ' '.join(sys.argv[1:]))\n")
        self.assertEqual(settings.team_migrate(), ["migrated: --ensure"])

    def test_silent_script_says_nothing(self):
        self.write("pass\n")
        self.assertEqual(settings.team_migrate(), [])

    def test_failure_fails_open_with_a_warning(self):
        self.write("import sys\nprint('half done')\nsys.exit(3)\n")
        lines = settings.team_migrate()
        self.assertEqual(lines[0], "half done")
        self.assertIn("failed with exit code 3", lines[-1])

    def test_timeout_fails_open_with_a_warning(self):
        self.write("import time\ntime.sleep(30)\n")
        lines = settings.team_migrate(timeout=1)
        self.assertEqual(len(lines), 1)
        self.assertIn("no result within 1 s", lines[0])

    def hold(self, age=0):
        os.makedirs(os.path.dirname(self.lock), exist_ok=True)
        with open(self.lock, "w") as f:
            f.write("x")
        if age:
            t = os.stat(self.lock).st_mtime - age
            os.utime(self.lock, (t, t))

    def test_held_lock_skips_the_run(self):
        self.write("print('ran')\n")
        self.hold()
        lines = settings.team_migrate()
        self.assertEqual(len(lines), 1)
        self.assertIn("already running elsewhere", lines[0])
        self.assertTrue(os.path.exists(self.lock))   # not ours: left alone

    def test_stale_lock_is_taken_over(self):
        self.write("print('ran')\n")
        self.hold(age=settings.MIGRATE_TIMEOUT + settings.LOCK_GRACE + 5)
        self.assertEqual(settings.team_migrate(), ["ran"])
        self.assertFalse(os.path.exists(self.lock))

    def test_lock_age_follows_the_given_timeout(self):
        self.write("print('ran')\n")
        self.hold(age=settings.LOCK_GRACE + 5)   # fresh for the default timeout, stale for timeout=1
        self.assertIn("already running", settings.team_migrate()[0])
        self.assertEqual(settings.team_migrate(timeout=1), ["ran"])

    def test_lock_released_after_success_failure_and_timeout(self):
        for body, kw in (("pass\n", {}), ("import sys\nsys.exit(3)\n", {}), ("import time\ntime.sleep(30)\n", {"timeout": 1})):
            self.write(body)
            settings.team_migrate(**kw)
            self.assertFalse(os.path.exists(self.lock), body)
        self.write("print('again')\n")
        self.assertEqual(settings.team_migrate(), ["again"])

    def test_lock_released_when_the_launch_fails(self):
        self.write("pass\n")
        with mock.patch.object(settings.hidden, "run", side_effect=OSError("boom")):
            self.assertIn("could not start", settings.team_migrate()[0])
        self.assertFalse(os.path.exists(self.lock))


class Wiring(unittest.TestCase):
    """The installer and the rules_watch background run call the hook before they read their install record."""
    ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))

    def src(self, *parts):
        with open(os.path.join(self.ROOT, *parts), encoding="utf-8") as f:
            return f.read()

    def test_installer_runs_the_hook_before_reading_the_saved_record(self):
        s = self.src("plugins", "quota-pace", "install_common.py")
        main = s[s.index("\ndef main("):]
        self.assertLess(main.index("team_layer.team_migrate()"), main.index("registered_setup(Path.home()"))

    def test_rules_watch_runs_the_hook_before_reading_quota_pace_json(self):
        s = self.src("tools", "rules_watch.py")
        body = s[s.index("\ndef refresh_rules("):]
        self.assertLess(body.index("settings.team_migrate()"), body.index("direct_mode()"))

    def test_rules_watch_migrates_again_after_the_pull_and_before_quota_setting(self):
        """A migration that arrives with the pull runs in the same cycle, not one cycle late."""
        s = self.src("tools", "rules_watch.py")
        body = s[s.index("\ndef refresh_rules("):]
        pull = body.index("quota_update(mode, state)")
        again = body.index("migrate()", pull)
        self.assertLess(pull, again)
        self.assertLess(again, body.index("quota_setting(mode)"))
        self.assertLess(body.index("head_before"), pull)   # only when HEAD moved


if __name__ == "__main__":
    unittest.main()

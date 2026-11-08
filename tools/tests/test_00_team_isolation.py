"""Isolation of the kit's tests from a downstream team's files (tools/tests/fork_support.py; README, "Team fork").
The module name sorts first on purpose: importing it sets CLAUDE_SHARED_TEAM_CONFIG_DIR before any other test module is imported.
Run: python -m unittest discover -s tools/tests"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.realpath(os.path.join(HERE, "..")))
import fork_support  # noqa: E402
import settings  # noqa: E402

EMPTY = fork_support.isolate_team_config()   # the one place that isolates this suite


class TeamConfigIsolation(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.fork = os.path.join(self.d, "team-config")   # what a downstream fork commits in its clone
        os.makedirs(self.fork)
        (Path(self.fork) / "org").write_text("fork-org\n", encoding="utf-8")
        for p in (mock.patch.object(settings, "TEAM_DIR", self.fork), mock.patch.object(settings, "PERSONAL_DIR", os.path.join(self.d, "none")),
                  mock.patch.dict(os.environ, {"KIT_TEST_KEY": ""})):
            p.start()
            self.addCleanup(p.stop)

    def test_the_suite_runs_with_an_empty_team_config_dir(self):
        self.assertEqual(os.environ[fork_support.ENV], EMPTY)
        self.assertEqual(os.listdir(EMPTY), [])
        self.assertEqual(fork_support.isolate_team_config(), EMPTY)   # idempotent: one directory per process

    def test_a_forks_committed_team_config_is_not_read(self):
        self.assertEqual(settings.value("org", "KIT_TEST_KEY", "kit-default"), "kit-default")

    def test_without_the_override_the_forks_value_is_read(self):
        with mock.patch.dict(os.environ, {fork_support.ENV: ""}):
            self.assertEqual(settings.value("org", "KIT_TEST_KEY", "kit-default"), "fork-org")

    def test_the_override_directory_is_the_team_layer(self):
        (Path(EMPTY) / "org").write_text("override-org\n", encoding="utf-8")
        self.addCleanup(os.unlink, os.path.join(EMPTY, "org"))
        self.assertEqual(settings.value("org", "KIT_TEST_KEY"), "override-org")
        self.assertEqual(settings.value("person", "KIT_TEST_KEY", team=False), "")   # personal keys never read the team layer


class TeamOwnedAttribute(unittest.TestCase):
    """drop_team_owned: the scan tests skip the paths a fork marks `team-owned` in .gitattributes."""
    FILES = ["tools/a.py", "tools/lab/x.py", "tools/lab/deep/y.py", "plugins/karasoy-z/z.py", "plugins/quota-pace/bin/ok.py", "top.py"]

    def tree(self, attributes):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for rel in self.FILES:
            p = Path(d, rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x = 1\n", encoding="utf-8")
        if attributes is not None:
            Path(d, ".gitattributes").write_text(attributes, encoding="utf-8")
        return d

    def test_marked_paths_are_dropped_and_the_rest_keeps_its_order(self):
        d = self.tree("# a comment\ntools/lab/** team-owned\nplugins/karasoy-*/** merge=ours team-owned\ntop.py -team-owned\n")
        self.assertEqual(fork_support.drop_team_owned(d, self.FILES), ["tools/a.py", "plugins/quota-pace/bin/ok.py", "top.py"])

    def test_backslash_paths_are_matched_and_returned_as_given(self):
        d = self.tree("tools/lab/** team-owned\n")
        got = fork_support.drop_team_owned(d, ["tools" + chr(92) + "lab" + chr(92) + "x.py", "tools" + chr(92) + "a.py"])
        self.assertEqual(got, ["tools" + chr(92) + "a.py"])

    def test_other_attributes_do_not_count(self):
        d = self.tree("tools/lab/** merge=ours kit-held\n")
        self.assertEqual(fork_support.drop_team_owned(d, self.FILES), self.FILES)

    def test_no_gitattributes_means_everything_is_scanned(self):
        d = self.tree(None)
        self.assertEqual(fork_support.drop_team_owned(d, self.FILES), self.FILES)

    def test_an_export_without_a_git_dir_works_the_same(self):
        d = self.tree("tools/lab/** team-owned\n")
        self.assertFalse(os.path.exists(os.path.join(d, ".git")))
        self.assertEqual(fork_support.team_owned(d, self.FILES), {"tools/lab/x.py", "tools/lab/deep/y.py"})


if __name__ == "__main__":
    unittest.main()

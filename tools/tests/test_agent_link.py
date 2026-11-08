"""Tests for the team agents link ~/.claude/agents/claude-shared -> <clone>/agents.

The link is made by plugins/quota-pace/install_common.py (agent_link) at every install and by the background run of
tools/rules_watch.py after every pull (install_common.py --setting, quota_setting). These tests use real links in a scratch directory:
a directory junction on Windows (no admin needed), a symlink elsewhere.

Run: python -m unittest discover -s tools/tests -p "test_agent_link.py"
"""
import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

CLONE = Path(__file__).resolve().parent.parent.parent
INSTALL = CLONE / "plugins" / "quota-pace" / "install_common.py"
RW = CLONE / "tools" / "rules_watch.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class AgentLink(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.k = load("install_common_bag", INSTALL)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self.clean)
        self.claude = self.tmp / "ev" / ".claude"
        self.source = self.tmp / "klon ç" / "agents"          # a space and a non-ASCII letter: Windows path handling
        self.source.mkdir(parents=True)
        (self.source / "quick-task-hm.md").write_text("---\nname: quick-task-hm\n---\n", encoding="utf-8")
        self.bag = self.claude / "agents" / "claude-shared"

    def clean(self):
        # links first (rmtree must never walk through a junction into its target), then the tree
        for p in (self.bag, self.claude / "agents" / "x"):
            if self.k._bag_mi(p):
                self.k._bag_sil(p)
        shutil.rmtree(self.tmp, True)

    def run_cmd(self, tag="", remove=False, target=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = self.k.agent_link(self.claude, tag, remove, target=target or self.source)
        return ok, out.getvalue()

    def test_creates_the_os_link_and_the_agents_resolve_through_it(self):
        ok, out = self.run_cmd()
        self.assertTrue(ok, out)
        self.assertIn("ajan baglantisi:", out)
        self.assertTrue(self.k._bag_mi(self.bag))
        if os.name == "nt":   # a junction, not a symlink (symlinks need admin or developer mode)
            self.assertFalse(self.bag.is_symlink())
        else:
            self.assertTrue(self.bag.is_symlink())
        self.assertTrue((self.bag / "quick-task-hm.md").is_file())
        self.assertTrue(os.path.samefile(self.bag, self.source))

    def test_second_run_is_a_no_op(self):
        self.run_cmd()
        ok, out = self.run_cmd()
        self.assertTrue(ok)
        self.assertIn("ayni:", out)
        self.assertNotIn("ajan baglantisi:", out)

    def test_a_new_file_in_the_clone_appears_without_a_rerun(self):
        self.run_cmd()
        (self.source / "new-agent-sm.md").write_text("x", encoding="utf-8")   # what a git pull does
        self.assertTrue((self.bag / "new-agent-sm.md").is_file())

    def test_a_link_to_another_place_is_replaced(self):
        other = self.tmp / "old-clone" / "agents"
        other.mkdir(parents=True)
        self.run_cmd(target=other)
        ok, out = self.run_cmd()
        self.assertTrue(ok, out)
        self.assertIn("yenilendi", out)
        self.assertTrue(os.path.samefile(self.bag, self.source))
        self.assertTrue(other.is_dir())   # the old target itself is never touched

    def test_a_dangling_link_of_a_moved_clone_is_repaired(self):
        old = self.tmp / "tasindi" / "agents"
        old.mkdir(parents=True)
        self.run_cmd(target=old)
        shutil.rmtree(old.parent)
        self.assertTrue(self.k._bag_mi(self.bag))
        ok, out = self.run_cmd()
        self.assertTrue(ok, out)
        self.assertTrue(os.path.samefile(self.bag, self.source))

    def test_own_agents_beside_the_link_are_never_touched(self):
        agents = self.claude / "agents"
        agents.mkdir(parents=True)
        (agents / "own-ajanim.md").write_text("mine", encoding="utf-8")
        (agents / "alt").mkdir()
        (agents / "alt" / "x.md").write_text("mine", encoding="utf-8")
        self.run_cmd()
        self.run_cmd(remove=True)
        self.assertEqual(sorted(p.name for p in agents.iterdir()), ["alt", "own-ajanim.md"])
        self.assertEqual((agents / "own-ajanim.md").read_text(encoding="utf-8"), "mine")

    def test_a_real_directory_with_the_name_is_left_alone(self):
        self.bag.mkdir(parents=True)
        (self.bag / "manual.md").write_text("mine", encoding="utf-8")
        ok, out = self.run_cmd()
        self.assertFalse(ok)
        self.assertIn("WARNING", out)
        self.assertTrue((self.bag / "manual.md").is_file())
        ok, _ = self.run_cmd(remove=True)   # --remove removes only a link
        self.assertTrue((self.bag / "manual.md").is_file())

    def test_remove_takes_only_the_link_and_keeps_the_clone_files(self):
        self.run_cmd()
        ok, out = self.run_cmd(remove=True)
        self.assertTrue(ok)
        self.assertFalse(self.k._bag_mi(self.bag) or self.bag.exists())
        self.assertTrue((self.source / "quick-task-hm.md").is_file())
        self.assertTrue(self.run_cmd(remove=True)[0])   # nothing left to remove: still fine

    def test_missing_clone_is_a_quiet_no_op(self):
        ok, out = self.run_cmd(target=self.tmp / "missing" / "agents")
        self.assertTrue(ok)
        self.assertFalse(self.bag.exists() or self.k._bag_mi(self.bag))
        self.assertIn("kaynagi yok", out)

    def test_whatif_writes_nothing(self):
        ok, out = self.run_cmd(tag="[-WhatIf] ")
        self.assertTrue(ok)
        self.assertIn("ajan baglantisi:", out)
        self.assertFalse(self.bag.exists() or self.k._bag_mi(self.bag))

    def test_the_default_source_is_the_agents_dir_of_this_clone(self):
        self.assertEqual(self.k.AGENT_SOURCE, CLONE / "agents")
        self.assertTrue(any(CLONE.joinpath("agents").glob("*.md")))


class QuotaSettingNote(unittest.TestCase):
    """rules_watch.quota_setting tells the human once when the background run created or repaired the link."""

    @classmethod
    def setUpClass(cls):
        cls.rw = load("rules_watch_bag", RW)

    def setting(self, output, rc=0):
        mode = {"repo": str(CLONE), "python": sys.executable, "argv": ["--direct"]}
        with unittest.mock.patch.object(self.rw, "run", return_value=(rc, output)):
            return self.rw.quota_setting(mode)

    def test_new_link_gives_a_note(self):
        status, note = self.setting("ajan baglantisi: C:/u/.claude/agents/claude-shared -> C:/k/agents\nsettings.json: rules_watch girisleri guncel, degisiklik yok\n")
        self.assertEqual(status, "ok")
        self.assertIn("claude-shared", note)

    def test_link_in_place_is_silent(self):
        self.assertEqual(self.setting("ayni: C:/u/.claude/agents/claude-shared -> C:/k/agents\nsettings.json: rules_watch girisleri guncel\n"),
                         ("ok", None))

    def test_both_changes_give_both_notes(self):
        status, note = self.setting("ajan baglantisi: a -> b\nrules_watch girisi eklendi\n")
        self.assertIn("claude-shared", note)
        self.assertIn("rules_watch", note)


if __name__ == "__main__":
    unittest.main()

"""Tests for the team agents link ~/.claude/agents/claude-shared -> <clone>/agents.

The link is made by plugins/kota-temposu/kur_ortak.py (ajan_baglantisi) at every install and by the background run of
tools/rules_watch.py after every pull (kur_ortak.py --ayar, kota_ayar). These tests use real links in a scratch directory:
a directory junction on Windows (no admin needed), a symlink elsewhere.

Run: python -m unittest discover -s tools/tests -p "test_ajan_baglantisi.py"
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

KLON = Path(__file__).resolve().parent.parent.parent
KUR = KLON / "plugins" / "kota-temposu" / "kur_ortak.py"
RW = KLON / "tools" / "rules_watch.py"


def yukle(ad, yol):
    spec = importlib.util.spec_from_file_location(ad, yol)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class AjanBaglantisi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.k = yukle("kur_ortak_bag", KUR)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self.temizle)
        self.claude = self.tmp / "ev" / ".claude"
        self.kaynak = self.tmp / "klon ç" / "agents"          # a space and a non-ASCII letter: Windows path handling
        self.kaynak.mkdir(parents=True)
        (self.kaynak / "quick-task-hm.md").write_text("---\nname: quick-task-hm\n---\n", encoding="utf-8")
        self.bag = self.claude / "agents" / "claude-shared"

    def temizle(self):
        # links first (rmtree must never walk through a junction into its target), then the tree
        for p in (self.bag, self.claude / "agents" / "x"):
            if self.k._bag_mi(p):
                self.k._bag_sil(p)
        shutil.rmtree(self.tmp, True)

    def calistir(self, tag="", remove=False, hedef=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = self.k.ajan_baglantisi(self.claude, tag, remove, hedef=hedef or self.kaynak)
        return ok, out.getvalue()

    def test_creates_the_os_link_and_the_agents_resolve_through_it(self):
        ok, out = self.calistir()
        self.assertTrue(ok, out)
        self.assertIn("ajan baglantisi:", out)
        self.assertTrue(self.k._bag_mi(self.bag))
        if os.name == "nt":   # a junction, not a symlink (symlinks need admin or developer mode)
            self.assertFalse(self.bag.is_symlink())
        else:
            self.assertTrue(self.bag.is_symlink())
        self.assertTrue((self.bag / "quick-task-hm.md").is_file())
        self.assertTrue(os.path.samefile(self.bag, self.kaynak))

    def test_second_run_is_a_no_op(self):
        self.calistir()
        ok, out = self.calistir()
        self.assertTrue(ok)
        self.assertIn("ayni:", out)
        self.assertNotIn("ajan baglantisi:", out)

    def test_a_new_file_in_the_clone_appears_without_a_rerun(self):
        self.calistir()
        (self.kaynak / "new-agent-sm.md").write_text("x", encoding="utf-8")   # what a git pull does
        self.assertTrue((self.bag / "new-agent-sm.md").is_file())

    def test_a_link_to_another_place_is_replaced(self):
        baska = self.tmp / "eski-klon" / "agents"
        baska.mkdir(parents=True)
        self.calistir(hedef=baska)
        ok, out = self.calistir()
        self.assertTrue(ok, out)
        self.assertIn("yenilendi", out)
        self.assertTrue(os.path.samefile(self.bag, self.kaynak))
        self.assertTrue(baska.is_dir())   # the old target itself is never touched

    def test_a_dangling_link_of_a_moved_clone_is_repaired(self):
        eski = self.tmp / "tasindi" / "agents"
        eski.mkdir(parents=True)
        self.calistir(hedef=eski)
        shutil.rmtree(eski.parent)
        self.assertTrue(self.k._bag_mi(self.bag))
        ok, out = self.calistir()
        self.assertTrue(ok, out)
        self.assertTrue(os.path.samefile(self.bag, self.kaynak))

    def test_own_agents_beside_the_link_are_never_touched(self):
        ajanlar = self.claude / "agents"
        ajanlar.mkdir(parents=True)
        (ajanlar / "kendi-ajanim.md").write_text("mine", encoding="utf-8")
        (ajanlar / "alt").mkdir()
        (ajanlar / "alt" / "x.md").write_text("mine", encoding="utf-8")
        self.calistir()
        self.calistir(remove=True)
        self.assertEqual(sorted(p.name for p in ajanlar.iterdir()), ["alt", "kendi-ajanim.md"])
        self.assertEqual((ajanlar / "kendi-ajanim.md").read_text(encoding="utf-8"), "mine")

    def test_a_real_directory_with_the_name_is_left_alone(self):
        self.bag.mkdir(parents=True)
        (self.bag / "elle.md").write_text("mine", encoding="utf-8")
        ok, out = self.calistir()
        self.assertFalse(ok)
        self.assertIn("UYARI", out)
        self.assertTrue((self.bag / "elle.md").is_file())
        ok, _ = self.calistir(remove=True)   # --kaldir removes only a link
        self.assertTrue((self.bag / "elle.md").is_file())

    def test_remove_takes_only_the_link_and_keeps_the_clone_files(self):
        self.calistir()
        ok, out = self.calistir(remove=True)
        self.assertTrue(ok)
        self.assertFalse(self.k._bag_mi(self.bag) or self.bag.exists())
        self.assertTrue((self.kaynak / "quick-task-hm.md").is_file())
        self.assertTrue(self.calistir(remove=True)[0])   # nothing left to remove: still fine

    def test_missing_clone_is_a_quiet_no_op(self):
        ok, out = self.calistir(hedef=self.tmp / "yok" / "agents")
        self.assertTrue(ok)
        self.assertFalse(self.bag.exists() or self.k._bag_mi(self.bag))
        self.assertIn("kaynagi yok", out)

    def test_whatif_writes_nothing(self):
        ok, out = self.calistir(tag="[-WhatIf] ")
        self.assertTrue(ok)
        self.assertIn("ajan baglantisi:", out)
        self.assertFalse(self.bag.exists() or self.k._bag_mi(self.bag))

    def test_the_default_source_is_the_agents_dir_of_this_clone(self):
        self.assertEqual(self.k.AJAN_KAYNAK, KLON / "agents")
        self.assertTrue(any(KLON.joinpath("agents").glob("*.md")))


class KotaAyarNotu(unittest.TestCase):
    """rules_watch.kota_ayar tells the human once when the background run created or repaired the link."""

    @classmethod
    def setUpClass(cls):
        cls.rw = yukle("rules_watch_bag", RW)

    def ayar(self, cikti, rc=0):
        kip = {"depo": str(KLON), "python": sys.executable, "argv": ["--repodan"]}
        with unittest.mock.patch.object(self.rw, "run", return_value=(rc, cikti)):
            return self.rw.kota_ayar(kip)

    def test_new_link_gives_a_note(self):
        durum, note = self.ayar("ajan baglantisi: C:/u/.claude/agents/claude-shared -> C:/k/agents\nsettings.json: rules_watch girisleri guncel, degisiklik yok\n")
        self.assertEqual(durum, "tamam")
        self.assertIn("claude-shared", note)

    def test_link_in_place_is_silent(self):
        self.assertEqual(self.ayar("ayni: C:/u/.claude/agents/claude-shared -> C:/k/agents\nsettings.json: rules_watch girisleri guncel\n"),
                         ("tamam", None))

    def test_both_changes_give_both_notes(self):
        durum, note = self.ayar("ajan baglantisi: a -> b\nrules_watch girisi eklendi\n")
        self.assertIn("claude-shared", note)
        self.assertIn("rules_watch", note)


if __name__ == "__main__":
    unittest.main()

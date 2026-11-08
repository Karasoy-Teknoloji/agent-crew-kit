"""Tests for the saved installer options (kur_ortak.kayitli_bayraklari_ekle): a run with one new flag keeps the options the last
install saved (--paylas/--kisi/--plan, --son-pencere, the Slack jobs); an explicit flag wins; only --kaldir and --sinama start clean;
an explicit --maliyetsiz leaves out the saved sharing flags it cannot run with. Pure functions, no files, no scheduler.

Run:  python3 -I -m unittest discover -s plugins/kota-temposu/tests     (from the repo root)
"""
import argparse
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("kur_ortak_kayitli", os.path.join(HERE, "..", "kur_ortak.py"))
K = importlib.util.module_from_spec(spec)
spec.loader.exec_module(K)

# the "kurulum" list the install of 2026-11-05 saved (paylas, kisi, plan, son-pencere, both Slack jobs)
KAYIT = ["--paylas", "--kisi", "Selim Akçay", "--plan", "100", "--son-pencere", "--slack-gelen", "--slack-triage"]


def secenek(**kw):
    """The parsed options of a run with only the given flags (the same defaults kur_ortak's argparse gives)."""
    d = dict(paylas=False, kisi=None, plan=None, son_pencere=False, slack_gelen=False, slack_triage=False, bayat=False, maliyetsiz=False)
    d.update(kw)
    return argparse.Namespace(**d)


class KayitliBayraklar(unittest.TestCase):
    def test_bare_run_takes_everything_saved(self):
        a = K.kayitli_bayraklari_ekle(secenek(), KAYIT)
        self.assertTrue(a.paylas)
        self.assertTrue(a.son_pencere)
        self.assertTrue(a.slack_gelen)
        self.assertTrue(a.slack_triage)
        self.assertEqual((a.kisi, a.plan), ("Selim Akçay", "100"))

    def test_one_new_flag_keeps_the_saved_ones(self):
        # the bug: `kur.ps1 -SonPencere` used to drop --paylas, --plan and the Slack jobs
        a = K.kayitli_bayraklari_ekle(secenek(son_pencere=True), KAYIT)
        self.assertTrue(a.paylas)
        self.assertTrue(a.son_pencere)
        self.assertTrue(a.slack_gelen and a.slack_triage)
        self.assertEqual((a.kisi, a.plan), ("Selim Akçay", "100"))

    def test_explicit_value_wins(self):
        a = K.kayitli_bayraklari_ekle(secenek(paylas=True, plan="200", kisi="Ahmet Yılmaz"), KAYIT)
        self.assertEqual((a.plan, a.kisi), ("200", "Ahmet Yılmaz"))

    def test_nothing_saved_changes_nothing(self):
        a = K.kayitli_bayraklari_ekle(secenek(slack_gelen=True), [])
        self.assertFalse(a.paylas or a.son_pencere)
        self.assertTrue(a.slack_gelen)
        self.assertIsNone(a.plan)

    def test_kisi_is_not_restored_without_a_sharing_option(self):
        # --kisi alone is an error in the installer; a saved --kisi must not turn a Slack-only run into that error
        kayit = ["--kisi", "Selim Akçay", "--slack-gelen"]
        a = K.kayitli_bayraklari_ekle(secenek(), kayit)
        self.assertIsNone(a.kisi)
        self.assertTrue(a.slack_gelen)

    def test_maliyetsiz_leaves_out_saved_sharing_but_keeps_slack(self):
        a = K.kayitli_bayraklari_ekle(secenek(maliyetsiz=True), KAYIT)
        self.assertFalse(a.paylas)
        self.assertFalse(a.son_pencere)
        self.assertIsNone(a.plan)
        self.assertIsNone(a.kisi)
        self.assertTrue(a.slack_gelen and a.slack_triage)


if __name__ == "__main__":
    unittest.main()

"""Tests for the saved installer options (install_common.add_registered_flags): a run with one new flag keeps the options the last
install saved (--share/--person/--plan, --last-window, the Slack jobs); an explicit flag wins; only --remove and --probe start clean;
an explicit --free leaves out the saved sharing flags it cannot run with. Pure functions, no files, no scheduler.

Run:  python3 -I -m unittest discover -s plugins/quota-pace/tests     (from the repo root)
"""
import argparse
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("install_common_registered", os.path.join(HERE, "..", "install_common.py"))
K = importlib.util.module_from_spec(spec)
spec.loader.exec_module(K)

# the "setup" list the install of 2026-11-05 saved (share, person, plan, last-window, both Slack jobs)
RECORD = ["--share", "--person", "Selim Akçay", "--plan", "100", "--last-window", "--slack-inbox", "--slack-triage"]


def option(**kw):
    """The parsed options of a run with only the given flags (the same defaults install_common's argparse gives)."""
    d = dict(share=False, person=None, plan=None, last_window=False, slack_inbox=False, slack_triage=False, stale=False, free=False)
    d.update(kw)
    return argparse.Namespace(**d)


class RegisteredFlags(unittest.TestCase):
    def test_bare_run_takes_everything_saved(self):
        a = K.add_registered_flags(option(), RECORD)
        self.assertTrue(a.share)
        self.assertTrue(a.last_window)
        self.assertTrue(a.slack_inbox)
        self.assertTrue(a.slack_triage)
        self.assertEqual((a.person, a.plan), ("Selim Akçay", "100"))

    def test_one_new_flag_keeps_the_saved_ones(self):
        # the bug: `install.ps1 -LastWindow` used to drop --share, --plan and the Slack jobs
        a = K.add_registered_flags(option(last_window=True), RECORD)
        self.assertTrue(a.share)
        self.assertTrue(a.last_window)
        self.assertTrue(a.slack_inbox and a.slack_triage)
        self.assertEqual((a.person, a.plan), ("Selim Akçay", "100"))

    def test_explicit_value_wins(self):
        a = K.add_registered_flags(option(share=True, plan="200", person="Ahmet Yılmaz"), RECORD)
        self.assertEqual((a.plan, a.person), ("200", "Ahmet Yılmaz"))

    def test_nothing_saved_changes_nothing(self):
        a = K.add_registered_flags(option(slack_inbox=True), [])
        self.assertFalse(a.share or a.last_window)
        self.assertTrue(a.slack_inbox)
        self.assertIsNone(a.plan)

    def test_person_work_not_restored_without_a_sharing_option(self):
        # --person alone is an error in the installer; a saved --person must not turn a Slack-only run into that error
        record = ["--person", "Selim Akçay", "--slack-inbox"]
        a = K.add_registered_flags(option(), record)
        self.assertIsNone(a.person)
        self.assertTrue(a.slack_inbox)

    def test_free_leaves_out_saved_sharing_but_keeps_slack(self):
        a = K.add_registered_flags(option(free=True), RECORD)
        self.assertFalse(a.share)
        self.assertFalse(a.last_window)
        self.assertIsNone(a.plan)
        self.assertIsNone(a.person)
        self.assertTrue(a.slack_inbox and a.slack_triage)


if __name__ == "__main__":
    unittest.main()

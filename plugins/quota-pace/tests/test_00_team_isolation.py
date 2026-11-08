"""Isolation of this suite from a downstream team's configuration (the helper lives in tools/tests/fork_support.py; README, "Team fork").
The module name sorts first on purpose: importing it sets CLAUDE_SHARED_TEAM_CONFIG_DIR before any other test module is imported
(last_window.py, for one, reads a team key at import).
Run: python -m unittest discover -s plugins/quota-pace/tests"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "..", "..", "tools", "tests")))
import fork_support  # noqa: E402

EMPTY = fork_support.isolate_team_config()   # the one place that isolates this suite


class TeamConfigIsolation(unittest.TestCase):
    def test_the_suite_runs_with_an_empty_team_config_dir(self):
        self.assertEqual(os.environ[fork_support.ENV], EMPTY)
        self.assertEqual(os.listdir(EMPTY), [])


if __name__ == "__main__":
    unittest.main()

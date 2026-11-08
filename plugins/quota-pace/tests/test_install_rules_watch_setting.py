"""Tests for install_common.apply_rules_watch (--setting): the rules_watch hook entries reach an existing settings.json after a pull, once, and
nothing else in the file changes.

Run:  python3 -I -m unittest discover -s plugins/quota-pace/tests     (from the repo root)
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("install_common_setting", os.path.join(HERE, "..", "install_common.py"))
K = importlib.util.module_from_spec(spec)
spec.loader.exec_module(K)

OTHER = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo other"}]}


def rw_commands(cfg):
    return [(ev, h) for ev, gs in cfg.get("hooks", {}).items() for g in gs for h in g["hooks"] if "rules_watch" in json.dumps(h)]


class Setting(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.f = Path(self.d.name) / "settings.json"
        K.say = lambda m: None

    def write(self, cfg):
        self.f.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def backups(self):
        return [p for p in Path(self.d.name).iterdir() if ".backup-" in p.name]

    def test_adds_entries_once_and_keeps_the_rest(self):
        # the state before this change: only the SessionStart entry, plus unrelated hooks, keys and permissions
        old = {"model": "x", "permissions": {"allow": ["Bash(ls)"]},
                "hooks": {"PreToolUse": [OTHER], "UserPromptSubmit": [OTHER],
                          "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "py", "args": ["rules_watch.py"]}]}]}}
        self.write(old)
        self.assertEqual(K.apply_rules_watch(self.f, "py"), 0)
        cfg = json.loads(self.f.read_text(encoding="utf-8"))
        events = sorted((ev, h["args"][-1].endswith("--request")) for ev, h in rw_commands(cfg))
        self.assertEqual(events, [("SessionStart", False), ("UserPromptSubmit", True)])
        self.assertEqual(cfg["model"], "x")
        self.assertEqual(cfg["permissions"], old["permissions"])
        self.assertEqual(cfg["hooks"]["PreToolUse"], [OTHER])
        self.assertIn(OTHER, cfg["hooks"]["UserPromptSubmit"])
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(json.loads(self.backups()[0].read_text(encoding="utf-8")), old)

    def test_second_run_changes_nothing(self):
        self.write({"hooks": {"PreToolUse": [OTHER]}})
        K.apply_rules_watch(self.f, "py")
        once = self.f.read_bytes()
        self.assertEqual(K.apply_rules_watch(self.f, "py"), 0)
        self.assertEqual(self.f.read_bytes(), once)
        self.assertEqual(len(self.backups()), 1)   # no second backup
        self.assertEqual(len(rw_commands(json.loads(once))), 2)

    def test_invalid_json_is_left_alone(self):
        self.f.write_text("{ bozuk", encoding="utf-8")
        self.assertEqual(K.apply_rules_watch(self.f, "py"), K.EXIT_SETTING_INVALID)
        self.assertEqual(self.f.read_text(encoding="utf-8"), "{ bozuk")
        self.assertEqual(self.backups(), [])

    def test_missing_file_is_not_created(self):
        self.assertEqual(K.apply_rules_watch(self.f, "py"), 0)
        self.assertFalse(self.f.exists())


if __name__ == "__main__":
    unittest.main()

"""Tests for kur_ortak.rules_watch_uygula (--ayar): the rules_watch hook entries reach an existing settings.json after a pull, once, and
nothing else in the file changes.

Run:  python3 -I -m unittest discover -s plugins/kota-temposu/tests     (from the repo root)
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("kur_ortak_ayar", os.path.join(HERE, "..", "kur_ortak.py"))
K = importlib.util.module_from_spec(spec)
spec.loader.exec_module(K)

DIGER = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo baska"}]}


def rw_komutlari(cfg):
    return [(ev, h) for ev, gs in cfg.get("hooks", {}).items() for g in gs for h in g["hooks"] if "rules_watch" in json.dumps(h)]


class Ayar(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.f = Path(self.d.name) / "settings.json"
        K.say = lambda m: None

    def yaz(self, cfg):
        self.f.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def yedekler(self):
        return [p for p in Path(self.d.name).iterdir() if ".yedek-" in p.name]

    def test_adds_entries_once_and_keeps_the_rest(self):
        # the state before this change: only the SessionStart entry, plus unrelated hooks, keys and permissions
        eski = {"model": "x", "permissions": {"allow": ["Bash(ls)"]},
                "hooks": {"PreToolUse": [DIGER], "UserPromptSubmit": [DIGER],
                          "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "py", "args": ["rules_watch.py"]}]}]}}
        self.yaz(eski)
        self.assertEqual(K.rules_watch_uygula(self.f, "py"), 0)
        cfg = json.loads(self.f.read_text(encoding="utf-8"))
        olaylar = sorted((ev, h["args"][-1].endswith("--istek")) for ev, h in rw_komutlari(cfg))
        self.assertEqual(olaylar, [("SessionStart", False), ("UserPromptSubmit", True)])
        self.assertEqual(cfg["model"], "x")
        self.assertEqual(cfg["permissions"], eski["permissions"])
        self.assertEqual(cfg["hooks"]["PreToolUse"], [DIGER])
        self.assertIn(DIGER, cfg["hooks"]["UserPromptSubmit"])
        self.assertEqual(len(self.yedekler()), 1)
        self.assertEqual(json.loads(self.yedekler()[0].read_text(encoding="utf-8")), eski)

    def test_second_run_changes_nothing(self):
        self.yaz({"hooks": {"PreToolUse": [DIGER]}})
        K.rules_watch_uygula(self.f, "py")
        once = self.f.read_bytes()
        self.assertEqual(K.rules_watch_uygula(self.f, "py"), 0)
        self.assertEqual(self.f.read_bytes(), once)
        self.assertEqual(len(self.yedekler()), 1)   # no second backup
        self.assertEqual(len(rw_komutlari(json.loads(once))), 2)

    def test_invalid_json_is_left_alone(self):
        self.f.write_text("{ bozuk", encoding="utf-8")
        self.assertEqual(K.rules_watch_uygula(self.f, "py"), K.EXIT_AYAR_GECERSIZ)
        self.assertEqual(self.f.read_text(encoding="utf-8"), "{ bozuk")
        self.assertEqual(self.yedekler(), [])

    def test_missing_file_is_not_created(self):
        self.assertEqual(K.rules_watch_uygula(self.f, "py"), 0)
        self.assertFalse(self.f.exists())


if __name__ == "__main__":
    unittest.main()

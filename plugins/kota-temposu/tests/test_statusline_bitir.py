"""Burn mode in the status line: " | BİTİR" (statusline.ps1 / statusline.sh) while ~/.claude/son-pencere/bitir.json says it is on and its
stored weekly reset has not passed.

Run:  python3 -I -m unittest discover -s plugins/kota-temposu/tests     (from the repo root)
Scratch HOME/USERPROFILE; the real profile is never touched.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUG = os.path.normpath(os.path.join(HERE, ".."))
PWSH = shutil.which("pwsh") or shutil.which("powershell")
STDIN = json.dumps({"model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"}, "workspace": {"current_dir": "."}})


class Scratch(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sl-bitir-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = os.path.join(self.root, "home")
        os.makedirs(os.path.join(self.home, ".claude", "son-pencere"))
        self.dosya = os.path.join(self.home, ".claude", "son-pencere", "bitir.json")

    def env(self):
        e = dict(os.environ, HOME=self.home, USERPROFILE=self.home, MALIYET_DURUM="0", SLACK_GELEN_INBOX=os.path.join(self.root, "yok.jsonl"))
        e.pop("CLAUDE_CONFIG_DIR", None)
        return e

    def yaz(self, **d):
        with open(self.dosya, "w", encoding="utf-8") as f:
            json.dump(d, f)

    def check(self, render):
        self.assertNotIn("BİTİR", render())                                                  # never switched on
        self.yaz(aktif=True, acan="Onur Demir", hafta_reset=int(time.time()) + 3600)
        self.assertIn(" | BİTİR", render())
        self.yaz(aktif=False, acan="Onur Demir", hafta_reset=int(time.time()) + 3600)
        self.assertNotIn("BİTİR", render())                                                  # switched off
        self.yaz(aktif=True, acan="Onur Demir", hafta_reset=int(time.time()) - 1)
        self.assertNotIn("BİTİR", render())                                                  # its weekly reset passed (auto-off)
        with open(self.dosya, "w") as f:
            f.write("{bozuk")
        self.assertNotIn("BİTİR", render())                                                  # unreadable: nothing, no error


@unittest.skipUnless(PWSH, "needs PowerShell")
class StatusLinePs1(Scratch):
    def test_segment(self):
        def render():
            p = subprocess.run([PWSH, "-NoProfile", "-File", os.path.join(PLUG, "statusline.ps1")], input=STDIN, capture_output=True,
                               text=True, encoding="utf-8", env=self.env(), timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            return p.stdout
        self.check(render)


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq") and os.name == "posix", "needs bash and jq")
class StatusLineSh(Scratch):
    def test_segment(self):
        def render():
            p = subprocess.run(["bash", os.path.join(PLUG, "statusline.sh")], input=STDIN, capture_output=True, text=True,
                               encoding="utf-8", env=self.env(), timeout=60)
            return p.stdout
        self.check(render)


if __name__ == "__main__":
    unittest.main()

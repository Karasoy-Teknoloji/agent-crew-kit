"""Tests for tools/ajan_adi.py, the PreToolUse hook that names every spawned subagent.

Run:  python3 -I -m unittest discover -s plugins/ekip-ajanlari/tests     (from the repo root)
Every run uses its own state folder (EKIP_AJAN_ADI_DIR), never the real temp folder.
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BETIK = os.path.normpath(os.path.join(HERE, "..", "tools", "ajan_adi.py"))


def calistir(olay, klasor, ham=None):
    girdi = ham if ham is not None else json.dumps(olay)
    return subprocess.run([sys.executable, "-I", BETIK], input=girdi, capture_output=True, text=True, encoding="utf-8",
                          env={**os.environ, "EKIP_AJAN_ADI_DIR": klasor}, timeout=60)


def olay(session="s1", tool="Agent", **girdi):
    ti = {"description": "Fix the bug", "prompt": "Do the work.", "subagent_type": "developer-sm", "run_in_background": True}
    ti.update(girdi)
    return {"session_id": session, "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": ti}


class AjanAdi(unittest.TestCase):
    def setUp(self):
        self.klasor = tempfile.mkdtemp(prefix="ajan-adi-")
        self.home = tempfile.mkdtemp(prefix="ajan-adi-home-")
        self.eski_env = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = os.environ["USERPROFILE"] = self.home      # never the real home

    def tearDown(self):
        shutil.rmtree(self.klasor, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)
        for k, v in self.eski_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def kisisel(self, icerik):
        klasor = os.path.join(self.home, ".config", "claude-shared")
        os.makedirs(klasor, exist_ok=True)
        with open(os.path.join(klasor, "ajan-adlari"), "w", encoding="utf-8") as f:
            f.write(icerik)

    def test_personal_list_is_used_and_invalid_lines_skipped(self):
        satirlar = ["# header", "", "Orman", "Yaprak", "yaprak", "Orman", "Çam", "İnci", "Ab1", "ÇokUzunBirAd", "İki Kelime",
                    "Deniz", "Derya", "Dalga", "Mercan", "Lagün", "Körfez", "Ada"]
        self.kisisel("\n".join(satirlar) + "\n")
        self.assertEqual(self.modul().ad_listesi(), ("Orman", "Yaprak", "Çam", "İnci", "Deniz", "Derya", "Dalga", "Mercan", "Lagün", "Körfez", "Ada"))

    def test_too_few_valid_names_falls_back_to_team_list(self):
        self.kisisel("# x" + "\n" + "Orman" + "\n" + "Yaprak" + "\n" + "bad1" + "\n")
        k = self.modul()
        self.assertEqual(k.ad_listesi(), k.AD_LISTESI)

    def test_missing_file_falls_back_to_team_list(self):
        k = self.modul()
        self.assertEqual(k.ad_listesi(), k.AD_LISTESI)

    def test_no_repeats_with_personal_list_in_one_session(self):
        adlar_ = ["Orman", "Yaprak", "Filiz", "Fidan", "Koru", "Çam", "Köknar", "Ladin", "Kayın", "Meşe", "Çınar", "Sedir"]
        self.kisisel("\n".join(adlar_))
        k = self.modul()
        adlar = [k.ad_sec("oturum-p", random.Random(i)) for i in range(len(adlar_) + 3)]
        self.assertEqual(len(set(adlar)), len(adlar))
        self.assertEqual(set(adlar[:len(adlar_)]), set(adlar_))
        self.assertTrue(all(a.endswith("-2") for a in adlar[len(adlar_):]))

    def test_hook_uses_personal_list(self):
        adlar_ = ["Orman", "Yaprak", "Filiz", "Fidan", "Koru", "Çam", "Köknar", "Ladin", "Kayın", "Meşe"]
        self.kisisel("\n".join(adlar_))
        r = calistir(olay(), self.klasor)
        ad = json.loads(r.stdout)["hookSpecificOutput"]["updatedInput"]["description"].split(" · ", 1)[0]
        self.assertIn(ad, adlar_)

    def modul(self):
        os.environ["EKIP_AJAN_ADI_DIR"] = self.klasor
        sys.path.insert(0, os.path.dirname(BETIK))
        import ajan_adi
        ajan_adi.DURUM_KLASORU = self.klasor
        return ajan_adi

    def test_names_description_and_prompt_and_keeps_every_other_field(self):
        r = calistir(olay(), self.klasor)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        yeni = out["updatedInput"]
        ad = yeni["description"].split(" · ", 1)[0]
        self.assertIn(ad, self.modul().AD_LISTESI)
        self.assertEqual(yeni["description"], ad + " · Fix the bug")
        self.assertEqual(yeni["prompt"], f"Your name is {ad}. Start every status message and your final report with '{ad}:'.\n\nDo the work.")
        self.assertEqual((yeni["subagent_type"], yeni["run_in_background"]), ("developer-sm", True))   # updatedInput replaces the whole input

    def test_old_tool_name_task_is_named_too(self):
        self.assertIn("updatedInput", calistir(olay(tool="Task"), self.klasor).stdout)

    def test_no_repeats_in_one_session_and_numbered_after_the_list(self):
        k = self.modul()
        adlar = [k.ad_sec("oturum-a", random.Random(i)) for i in range(len(k.AD_LISTESI) + 5)]
        self.assertEqual(len(set(adlar)), len(adlar))
        self.assertEqual(set(adlar[:len(k.AD_LISTESI)]), set(k.AD_LISTESI))
        self.assertTrue(all(a.endswith("-2") for a in adlar[len(k.AD_LISTESI):]))
        self.assertIn(k.ad_sec("oturum-b"), k.AD_LISTESI)          # another session starts fresh

    def test_passes_through_unchanged(self):
        for o in (olay(tool="Bash"), olay(prompt=""), olay(prompt=None), olay(description="Poyraz · Fix", prompt="x"),
                  olay(prompt="Your name is Lodos. ...")):
            with self.subTest(o=o["tool_input"]):
                r = calistir(o, self.klasor)
                self.assertEqual((r.returncode, r.stdout), (0, ""))
        for ham in ("", "not json", "[1, 2]", "null"):
            with self.subTest(ham=ham):
                r = calistir(None, self.klasor, ham=ham)
                self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_fails_open_when_the_state_folder_cannot_be_written(self):
        dosya = os.path.join(self.klasor, "dosya")
        with open(dosya, "w") as f:
            f.write("x")
        r = calistir(olay(), os.path.join(dosya, "alt"))          # a folder under a file cannot be created
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_name_list_is_short_unique_and_large_enough(self):
        k = self.modul()
        self.assertGreaterEqual(len(k.AD_LISTESI), 55)
        self.assertEqual(len(set(k.AD_LISTESI)), len(k.AD_LISTESI))
        for ad in k.AD_LISTESI:
            with self.subTest(ad=ad):
                self.assertTrue(ad[0].isupper() and ad.isalpha() and len(ad) <= 10)


if __name__ == "__main__":
    unittest.main()

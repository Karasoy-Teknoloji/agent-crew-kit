"""Last action of the status line = the MAIN session's own responses since its last real prompt (agents excluded), and the first
window 'son Ndk' = the total_cost_usd delta since the prompt boundary (main + agents).
Run with the other plugin tests:  python3 -I -m unittest discover -s plugins/kota-temposu/tests"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.normpath(os.path.join(HERE, "..", "bin"))
MODEL = "claude-opus-5-5"       # $4 per 1M input tokens in maliyet-fiyatlar.json (see platform_render.py)


def asst(mid, tokens, side=False):
    return {"type": "assistant", "isSidechain": side, "timestamp": "2026-10-29T10:00:00.000Z", "requestId": "r" + mid,
            "message": {"id": mid, "model": MODEL, "role": "assistant", "content": [{"type": "text", "text": "x"}],
                        "usage": {"input_tokens": tokens, "output_tokens": 0}}}


def user(text):
    return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": text}}


def tool_result():
    return {"type": "user", "isSidechain": False, "toolUseResult": {},
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}}


class TurAna(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="turana-")
        self.addCleanup(shutil.rmtree, self.home, True)
        c = os.path.join(self.home, ".claude")
        os.makedirs(os.path.join(c, "maliyet"))
        os.makedirs(os.path.join(c, "bin"))
        for f in os.listdir(BIN):
            if f.startswith("maliyet"):
                shutil.copy(os.path.join(BIN, f), os.path.join(c, "bin", f))
        for f in ("gizli.py", "gizli_yukle.py", "gunluk.py"):      # a copy install ships these next to the scripts (kur_ortak FILES)
            shutil.copy(os.path.join(BIN, "..", "..", "..", "tools", f), os.path.join(c, "bin", f))
        with open(os.path.join(c, "maliyet", "kur.json"), "w") as f:
            json.dump({"kur": 50.0}, f)
        with open(os.path.join(c, "maliyet", "oran.json"), "w") as f:
            json.dump({"t": 4102444800, "katsayi": 0.1}, f)          # far future "t": always fresh
        self.tr = os.path.join(c, "sess.jsonl")

    def write(self, *objs, mode="w"):
        with open(self.tr, mode, encoding="utf-8") as f:
            for o in objs:
                f.write(json.dumps(o) + "\n")

    def render(self, pid, total):
        d = {"session_id": "s1", "prompt_id": pid, "transcript_path": self.tr, "cost": {"total_cost_usd": total}}
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        env["HOME"] = env["USERPROFILE"] = self.home
        p = subprocess.run([sys.executable, "-I", os.path.join(self.home, ".claude", "bin", "maliyet.py"), "--durum", "--girdi"],
                           input=json.dumps(d).encode(), capture_output=True, env=env, timeout=60)
        return p.stdout.decode("utf-8").split("\n")

    def test_last_action_counts_only_main_responses_since_the_last_prompt(self):
        self.write(user("a"), asst("m1", 1_000_000), asst("m1", 1_000_000), tool_result(), asst("m2", 500_000),     # turn A: $4 + $2
                   asst("s1", 1_000_000, side=True))                                                                 # sidechain: skipped
        self.assertEqual(self.render("p1", 5.0)[2], "₺300 (₺30,00)")         # hand sum: 4 + 2 = $6 x 50 TL; duplicate line counted once
        self.write(user("b"), asst("m3", 250_000), asst("m3", 250_000), asst("s2", 1_000_000, side=True), mode="a")
        self.assertEqual(self.render("p2", 9.0)[2], "₺50,00 (₺5,00)")       # hand sum: $1 x 50 TL (incremental read, new turn only)
        self.write(asst("m4", 250_000), mode="a")                             # a second response of the same turn adds up
        self.assertEqual(self.render("p2", 9.5)[2], "₺100 (₺10,00)")

    def test_oturum_stays_the_total_cost_usd(self):
        self.write(user("a"), asst("m1", 1_000_000))
        self.assertEqual(self.render("p1", 5.0)[1], "₺250 (₺25,00)")

    def test_son_dk_window_is_the_total_delta_since_the_boundary_and_hidden_without_one(self):
        self.write(user("a"), asst("m1", 1_000_000))
        self.assertNotIn("son", self.render("p1", 5.0)[0])                   # first sighting: no baseline
        self.assertNotIn("son", self.render("p1", 5.0)[0])                   # nothing spent since the boundary
        self.assertRegex(self.render("p2", 8.0)[0].split(" · ")[0], r"^son \d+sn ₺150 \(₺15,00\)$")  # +$3 incl. agents, whatever the transcript says; seconds in the first minute (the rolling windows follow after ' · ')
        self.assertEqual(self.render("p2", 8.0)[2], "₺200 (₺20,00)")          # the clock amount is main-only: the transcript's last turn is $4

    def test_son_dk_label_style(self):
        sys.path.insert(0, BIN)
        import maliyet
        self.assertEqual([maliyet.sure_tr(s) for s in (0, 45, 59, 60, 7 * 60 + 5, 3599, 3600, 80 * 60, 4 * 3600 + 23 * 60)],
                         ["0sn", "45sn", "59sn", "1dk", "7dk", "59dk", "1sa0dk", "1sa20dk", "4sa23dk"])


if __name__ == "__main__":
    unittest.main()

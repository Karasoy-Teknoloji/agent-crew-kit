"""Tests for tools/agent_name.py, the PreToolUse hook that names every spawned subagent.

Run:  python3 -I -m unittest discover -s plugins/crew-agents/tests     (from the repo root)
Every run uses its own state folder (CREW_AGENT_NAME_DIR), never the real temp folder.
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
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "tools", "agent_name.py"))


def run_cmd(event, folder, ham=None):
    input_ = ham if ham is not None else json.dumps(event)
    return subprocess.run([sys.executable, "-I", SCRIPT], input=input_, capture_output=True, text=True, encoding="utf-8",
                          env={**os.environ, "CREW_AGENT_NAME_DIR": folder}, timeout=60)


def event(session="s1", tool="Agent", **input_):
    ti = {"description": "Fix the bug", "prompt": "Do the work.", "subagent_type": "developer-sm", "run_in_background": True}
    ti.update(input_)
    return {"session_id": session, "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": ti}


class AgentName(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="agent-name-")
        self.home = tempfile.mkdtemp(prefix="agent-name-home-")
        self.old_env = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = os.environ["USERPROFILE"] = self.home      # never the real home

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def personal(self, content):
        folder = os.path.join(self.home, ".config", "claude-shared")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "agent-names"), "w", encoding="utf-8") as f:
            f.write(content)

    def test_personal_list_is_used_and_invalid_lines_skipped(self):
        lines = ["# header", "", "Orman", "Leaf", "leaf", "Orman", "Çam", "İnci", "Ab1", "ÇokUzunBirAd", "İki Kelime",
                    "Deniz", "Derya", "Dalga", "Mercan", "Lagün", "Körfez", "Ada"]
        self.personal("\n".join(lines) + "\n")
        self.assertEqual(self.module().name_list(), ("Orman", "Leaf", "Çam", "İnci", "Deniz", "Derya", "Dalga", "Mercan", "Lagün", "Körfez", "Ada"))

    def test_too_few_valid_names_falls_back_to_team_list(self):
        self.personal("# x" + "\n" + "Orman" + "\n" + "Leaf" + "\n" + "bad1" + "\n")
        k = self.module()
        self.assertEqual(k.name_list(), k.NAME_LIST)

    def test_missing_file_falls_back_to_team_list(self):
        k = self.module()
        self.assertEqual(k.name_list(), k.NAME_LIST)

    def test_no_repeats_with_personal_list_in_one_session(self):
        names_ = ["Orman", "Leaf", "Filiz", "Fidan", "Keep", "Çam", "Köknar", "Ladin", "Kayın", "Meşe", "Çınar", "Sedir"]
        self.personal("\n".join(names_))
        k = self.module()
        names = [k.ad_sec("session-p", random.Random(i)) for i in range(len(names_) + 3)]
        self.assertEqual(len(set(names)), len(names))
        self.assertEqual(set(names[:len(names_)]), set(names_))
        self.assertTrue(all(a.endswith("-2") for a in names[len(names_):]))

    def test_hook_uses_personal_list(self):
        names_ = ["Orman", "Leaf", "Filiz", "Fidan", "Keep", "Çam", "Köknar", "Ladin", "Kayın", "Meşe"]
        self.personal("\n".join(names_))
        r = run_cmd(event(), self.folder)
        name = json.loads(r.stdout)["hookSpecificOutput"]["updatedInput"]["description"].split(" · ", 1)[0]
        self.assertIn(name, names_)

    def module(self):
        os.environ["CREW_AGENT_NAME_DIR"] = self.folder
        sys.path.insert(0, os.path.dirname(SCRIPT))
        import agent_name
        agent_name.STATUS_FOLDER = self.folder
        return agent_name

    def test_names_description_and_prompt_and_keeps_every_other_field(self):
        r = run_cmd(event(), self.folder)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        new = out["updatedInput"]
        name = new["description"].split(" · ", 1)[0]
        self.assertIn(name, self.module().NAME_LIST)
        self.assertEqual(new["description"], name + " · Fix the bug")
        self.assertEqual(new["prompt"], f"Your name is {name}. Start every status message and your final report with '{name}:'.\n\nDo the work.")
        self.assertEqual((new["subagent_type"], new["run_in_background"]), ("developer-sm", True))   # updatedInput replaces the whole input

    def test_old_tool_name_task_is_named_too(self):
        self.assertIn("updatedInput", run_cmd(event(tool="Task"), self.folder).stdout)

    def test_no_repeats_in_one_session_and_numbered_after_the_list(self):
        k = self.module()
        names = [k.ad_sec("session-a", random.Random(i)) for i in range(len(k.NAME_LIST) + 5)]
        self.assertEqual(len(set(names)), len(names))
        self.assertEqual(set(names[:len(k.NAME_LIST)]), set(k.NAME_LIST))
        self.assertTrue(all(a.endswith("-2") for a in names[len(k.NAME_LIST):]))
        self.assertIn(k.ad_sec("session-b"), k.NAME_LIST)          # another session starts fresh

    def test_passes_through_unchanged(self):
        for o in (event(tool="Bash"), event(prompt=""), event(prompt=None), event(description="Poyraz · Fix", prompt="x"),
                  event(prompt="Your name is Lodos. ...")):
            with self.subTest(o=o["tool_input"]):
                r = run_cmd(o, self.folder)
                self.assertEqual((r.returncode, r.stdout), (0, ""))
        for ham in ("", "not json", "[1, 2]", "null"):
            with self.subTest(ham=ham):
                r = run_cmd(None, self.folder, ham=ham)
                self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_fails_open_when_the_state_folder_cannot_be_written(self):
        file = os.path.join(self.folder, "file")
        with open(file, "w") as f:
            f.write("x")
        r = run_cmd(event(), os.path.join(file, "alt"))          # a folder under a file cannot be created
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_name_list_is_short_unique_and_large_enough(self):
        k = self.module()
        self.assertGreaterEqual(len(k.NAME_LIST), 55)
        self.assertEqual(len(set(k.NAME_LIST)), len(k.NAME_LIST))
        for name in k.NAME_LIST:
            with self.subTest(name=name):
                self.assertTrue(name[0].isupper() and name.isalpha() and len(name) <= 10)


if __name__ == "__main__":
    unittest.main()

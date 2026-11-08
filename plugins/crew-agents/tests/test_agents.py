"""Checks the team agent files (<clone>/agents, linked to ~/.claude/agents/claude-shared by the installer): valid frontmatter, the
<role>-<model><effort> naming scheme, the README tables of the crew-agents plugin, and that the plugin ships no agents.

Run:  python3 -I -m unittest discover -s plugins/crew-agents/tests     (from the repo root)
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUG = os.path.normpath(os.path.join(HERE, ".."))
AGENTS = os.path.normpath(os.path.join(PLUG, "..", "..", "agents"))   # repo root: plugin agents would carry the `crew-agents:` prefix
MODEL = {"h": "haiku", "s": "sonnet", "o": "opus", "f": "fable"}
EFFORT = {"m": "medium", "h": "high", "xh": "xhigh", "max": "max"}
NAME = re.compile(r"^(?P<role>[a-z][a-z0-9]*(?:-[a-z0-9]+)*)-(?P<model>[hsof])(?P<effort>m|h|xh|max)$")
BUILTIN = {"Explore"}   # keeps the built-in agent's name on purpose, so it replaces the built-in instead of sitting next to it


def frontmatter(path):
    """Top-level `key: value` pairs of the YAML frontmatter (nested blocks are skipped); raises on a malformed block or value."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    if lines[0].rstrip("\r") != "---":
        raise ValueError("no frontmatter start")
    last = next(i for i, s in enumerate(lines[1:], 1) if s.rstrip("\r") == "---")
    fields = {}
    for s in lines[1:last]:
        s = s.rstrip("\r")
        if not s or s.startswith(" "):
            continue
        key, delimiter, value = s.partition(":")
        if not delimiter or not re.fullmatch(r"[A-Za-z]+", key):
            raise ValueError("bad line: " + s)
        value = value.strip()
        if value.startswith('"'):
            if not (len(value) >= 2 and value.endswith('"')) or re.search(r'(?<!\\)"', value[1:-1]):
                raise ValueError("bad double-quoted value: " + key)
            value = value[1:-1].replace('\\"', '"')
        elif ": " in value or value.startswith(("'", "[", "{", "&", "*", "!", "|", ">")):
            raise ValueError("plain value needs quoting: " + key)
        fields[key] = value
    return fields


def agent_files():
    return sorted(f for f in os.listdir(AGENTS) if f.endswith(".md"))


class AgentFiles(unittest.TestCase):
    def test_the_plugin_ships_no_agents_and_the_agents_dir_holds_only_agents(self):
        # Claude Code auto-loads a plugin's agents/ folder with the plugin prefix; every file in <clone>/agents loads as an agent
        self.assertFalse(os.path.exists(os.path.join(PLUG, "agents")))
        self.assertEqual([f for f in os.listdir(AGENTS) if not f.endswith(".md")], [])

    def test_every_agent_has_valid_frontmatter_and_follows_the_naming_scheme(self):
        files = agent_files()
        self.assertGreater(len(files), 10)
        names = set()
        for file in files:
            with self.subTest(file=file):
                fm = frontmatter(os.path.join(AGENTS, file))
                name = fm["name"]
                names.add(name)
                self.assertEqual(name.lower(), file[:-3].lower())
                self.assertTrue(fm.get("description"), "description")
                self.assertIn(fm.get("model"), MODEL.values())
                if name in BUILTIN:
                    continue
                m = NAME.match(name)
                self.assertIsNotNone(m, "name is not <role>-<model><effort>: " + name)
                self.assertEqual(fm["model"], MODEL[m["model"]], "model letter")
                self.assertEqual(fm.get("effort"), EFFORT[m["effort"]], "effort code")
        self.assertEqual(len(names), len(files))

    def test_no_agent_uses_low_effort(self):
        for file in agent_files():
            with self.subTest(file=file):
                self.assertNotEqual(frontmatter(os.path.join(AGENTS, file)).get("effort"), "low")

    def test_agent_names_in_texts_exist(self):
        """Every `<role>-<model><effort>` name an agent prompt points to is a real agent of this set."""
        names = {frontmatter(os.path.join(AGENTS, d))["name"] for d in agent_files()}
        for file in agent_files():
            with open(os.path.join(AGENTS, file), encoding="utf-8") as f:
                text = f.read()
            for name in re.findall(r"\b[a-z]+(?:-[a-z]+)*-(?:[hsof](?:m|h|xh|max))\b", text):
                with self.subTest(file=file, name=name):
                    self.assertIn(name, names)

    def test_readme_lists_every_agent_and_maps_old_names_to_existing_ones(self):
        with open(os.path.join(PLUG, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        names = {frontmatter(os.path.join(AGENTS, d))["name"] for d in agent_files()}
        for name in names:
            with self.subTest(name=name):
                self.assertIn("`%s`" % name, readme)
        table = readme.split("## Old names", 1)[1].split("\n## ", 1)[0]
        lines = [s for s in table.splitlines() if s.startswith("| `")]
        self.assertGreater(len(lines), 10)
        for s in lines:
            cell = [h.strip().strip("`") for h in s.strip("|").split("|")]
            with self.subTest(old=cell[0]):
                self.assertIn(cell[2], names)


if __name__ == "__main__":
    unittest.main()

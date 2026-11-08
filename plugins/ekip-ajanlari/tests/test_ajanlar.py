"""Checks the team agent files (<clone>/agents, linked to ~/.claude/agents/claude-shared by the installer): valid frontmatter, the
<role>-<model><effort> naming scheme, the README tables of the ekip-ajanlari plugin, and that the plugin ships no agents.

Run:  python3 -I -m unittest discover -s plugins/ekip-ajanlari/tests     (from the repo root)
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUG = os.path.normpath(os.path.join(HERE, ".."))
AJANLAR = os.path.normpath(os.path.join(PLUG, "..", "..", "agents"))   # repo root: plugin agents would carry the `ekip-ajanlari:` prefix
MODEL = {"h": "haiku", "s": "sonnet", "o": "opus", "f": "fable"}
EFOR = {"m": "medium", "h": "high", "xh": "xhigh", "max": "max"}
AD = re.compile(r"^(?P<rol>[a-z][a-z0-9]*(?:-[a-z0-9]+)*)-(?P<model>[hsof])(?P<efor>m|h|xh|max)$")
YERLESIK = {"Explore"}   # keeps the built-in agent's name on purpose, so it replaces the built-in instead of sitting next to it


def frontmatter(yol):
    """Top-level `key: value` pairs of the YAML frontmatter (nested blocks are skipped); raises on a malformed block or value."""
    with open(yol, encoding="utf-8") as f:
        satirlar = f.read().split("\n")
    if satirlar[0].rstrip("\r") != "---":
        raise ValueError("no frontmatter start")
    son = next(i for i, s in enumerate(satirlar[1:], 1) if s.rstrip("\r") == "---")
    alanlar = {}
    for s in satirlar[1:son]:
        s = s.rstrip("\r")
        if not s or s.startswith(" "):
            continue
        anahtar, ayrac, deger = s.partition(":")
        if not ayrac or not re.fullmatch(r"[A-Za-z]+", anahtar):
            raise ValueError("bad line: " + s)
        deger = deger.strip()
        if deger.startswith('"'):
            if not (len(deger) >= 2 and deger.endswith('"')) or re.search(r'(?<!\\)"', deger[1:-1]):
                raise ValueError("bad double-quoted value: " + anahtar)
            deger = deger[1:-1].replace('\\"', '"')
        elif ": " in deger or deger.startswith(("'", "[", "{", "&", "*", "!", "|", ">")):
            raise ValueError("plain value needs quoting: " + anahtar)
        alanlar[anahtar] = deger
    return alanlar


def ajan_dosyalari():
    return sorted(f for f in os.listdir(AJANLAR) if f.endswith(".md"))


class AgentFiles(unittest.TestCase):
    def test_the_plugin_ships_no_agents_and_the_agents_dir_holds_only_agents(self):
        # Claude Code auto-loads a plugin's agents/ folder with the plugin prefix; every file in <clone>/agents loads as an agent
        self.assertFalse(os.path.exists(os.path.join(PLUG, "agents")))
        self.assertEqual([f for f in os.listdir(AJANLAR) if not f.endswith(".md")], [])

    def test_every_agent_has_valid_frontmatter_and_follows_the_naming_scheme(self):
        dosyalar = ajan_dosyalari()
        self.assertGreater(len(dosyalar), 10)
        adlar = set()
        for dosya in dosyalar:
            with self.subTest(dosya=dosya):
                fm = frontmatter(os.path.join(AJANLAR, dosya))
                ad = fm["name"]
                adlar.add(ad)
                self.assertEqual(ad.lower(), dosya[:-3].lower())
                self.assertTrue(fm.get("description"), "description")
                self.assertIn(fm.get("model"), MODEL.values())
                if ad in YERLESIK:
                    continue
                m = AD.match(ad)
                self.assertIsNotNone(m, "name is not <role>-<model><effort>: " + ad)
                self.assertEqual(fm["model"], MODEL[m["model"]], "model letter")
                self.assertEqual(fm.get("effort"), EFOR[m["efor"]], "effort code")
        self.assertEqual(len(adlar), len(dosyalar))

    def test_no_agent_uses_low_effort(self):
        for dosya in ajan_dosyalari():
            with self.subTest(dosya=dosya):
                self.assertNotEqual(frontmatter(os.path.join(AJANLAR, dosya)).get("effort"), "low")

    def test_agent_names_in_texts_exist(self):
        """Every `<role>-<model><effort>` name an agent prompt points to is a real agent of this set."""
        adlar = {frontmatter(os.path.join(AJANLAR, d))["name"] for d in ajan_dosyalari()}
        for dosya in ajan_dosyalari():
            with open(os.path.join(AJANLAR, dosya), encoding="utf-8") as f:
                metin = f.read()
            for ad in re.findall(r"\b[a-z]+(?:-[a-z]+)*-(?:[hsof](?:m|h|xh|max))\b", metin):
                with self.subTest(dosya=dosya, ad=ad):
                    self.assertIn(ad, adlar)

    def test_readme_lists_every_agent_and_maps_old_names_to_existing_ones(self):
        with open(os.path.join(PLUG, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        adlar = {frontmatter(os.path.join(AJANLAR, d))["name"] for d in ajan_dosyalari()}
        for ad in adlar:
            with self.subTest(ad=ad):
                self.assertIn("`%s`" % ad, readme)
        tablo = readme.split("## Old names", 1)[1].split("\n## ", 1)[0]
        satirlar = [s for s in tablo.splitlines() if s.startswith("| `")]
        self.assertGreater(len(satirlar), 10)
        for s in satirlar:
            hucre = [h.strip().strip("`") for h in s.strip("|").split("|")]
            with self.subTest(eski=hucre[0]):
                self.assertIn(hucre[2], adlar)


if __name__ == "__main__":
    unittest.main()

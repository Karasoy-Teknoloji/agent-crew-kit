"""Tests for tools/gizli_yukle.py, the one import of gizli: symlink install, missing gizli.py (fallback), script shipped over stdin.
Run: python -m unittest discover -s tools/tests"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
TOOL = "gh_board.py"     # stdlib only, imports gizli at the top (a missing gizli.py once raised ModuleNotFoundError: gizli)

LOAD = ("import importlib.util, sys; s = importlib.util.spec_from_file_location('t', sys.argv[1]); m = importlib.util.module_from_spec(s); "
        "s.loader.exec_module(m); g = m.gizli; r = g.run([sys.executable, '-c', 'print(7)'], capture_output=True, text=True, gorunur=True); "
        "print(g.__name__, g.NO_WINDOW if hasattr(g, 'NO_WINDOW') else '?', r.stdout.strip(), g.flags(4))")


def py(*args, **kw):
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, timeout=120, **kw)


class GizliYukle(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_fallback_when_gizli_py_is_missing(self):
        shutil.copy(os.path.join(TOOLS, TOOL), self.d)
        shutil.copy(os.path.join(TOOLS, "gizli_yukle.py"), self.d)      # no gizli.py next to it
        r = py("-I", "-c", LOAD, os.path.join(self.d, TOOL), cwd=self.d)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["gizli", "0", "7", "4"])      # plain subprocess stand-in: same API, no creation flag
        self.assertEqual(r.stderr.count("gizli.py not found"), 1, r.stderr)   # exactly one warning line

    def test_real_gizli_is_used_when_present_and_prints_no_warning(self):
        for f in (TOOL, "gizli_yukle.py", "gizli.py"):
            shutil.copy(os.path.join(TOOLS, f), self.d)
        r = py("-I", "-c", LOAD, os.path.join(self.d, TOOL), cwd=self.d)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("not found", r.stderr)

    def test_symlink_install_resolves_the_real_dir(self):
        """~/.claude/bin/<tool>.py -> clone/tools/<tool>.py: the siblings are next to the link TARGET, not next to the link."""
        link_dir = os.path.join(self.d, "bin")
        os.mkdir(link_dir)
        link = os.path.join(link_dir, TOOL)
        try:
            os.symlink(os.path.join(TOOLS, TOOL), link)
        except (OSError, NotImplementedError) as e:
            self.skipTest("symlinks not available here: %s" % e)
        r = py("-I", link, "--help", cwd=self.d)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("not found", r.stderr)      # real gizli.py was found through the link, not the fallback
        self.assertNotIn("ModuleNotFoundError", r.stderr)

    def test_every_tool_imports_gizli_only_through_the_helper(self):
        root = os.path.dirname(TOOLS)
        bad = []
        for base in ("tools", "plugins"):
            for d, _, files in os.walk(os.path.join(root, base)):
                for f in files:
                    p = os.path.join(d, f)
                    if not f.endswith(".py") or "tests" in p.split(os.sep) or f in ("gizli.py", "gizli_yukle.py"):
                        continue
                    with open(p, encoding="utf-8-sig") as fh:
                        for n, line in enumerate(fh, 1):
                            if line.startswith(("import gizli", "from gizli import")):
                                bad.append("%s:%d" % (os.path.relpath(p, root), n))
        self.assertEqual(bad, [], "use `from gizli_yukle import gizli` (falls back when gizli.py is missing)")


if __name__ == "__main__":
    unittest.main()

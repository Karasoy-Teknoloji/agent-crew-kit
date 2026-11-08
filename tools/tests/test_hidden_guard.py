"""Guard: no Python file of this repo starts a child process except through tools/hidden.py (hidden.run / hidden.popen).

Why: under pythonw (every scheduled task, many hooks) there is no console, so a child started without CREATE_NO_WINDOW
opens a visible console window; a scan with hundreds of git calls opened hundreds of windows. tools/hidden.py
adds the flag in ONE place (a child that must stay visible or own the terminal says so with hidden.run(..., gorunur=True)); this test fails when a new call site bypasses it, and lists file:line.

Run: python -m unittest discover -s tools/tests
"""
import ast
import os
import unittest

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
HELPERS = {os.path.join("tools", "hidden.py"), os.path.join("tools", "hidden_loader.py")}   # hidden_loader: the plain-subprocess stand-in when hidden.py is missing
SKIP_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv"}
SUBPROCESS_BANNED = {"run", "Popen", "call", "check_output", "check_call", "getoutput", "getstatusoutput"}
OS_BANNED = {"system", "popen", "startfile", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe"}


def is_test_file(rel):
    parts = rel.replace("\\", "/").split("/")
    return "tests" in parts[:-1] or parts[-1].startswith("test_") or parts[-1] == "conftest.py"


def python_files():
    for d, dirs, files in os.walk(ROOT):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                rel = os.path.relpath(os.path.join(d, f), ROOT)
                if rel not in HELPERS and not is_test_file(rel):
                    yield rel


def offenders(path):
    """[(line, text)] of direct process starts in one file (AST: strings and comments never count)."""
    with open(os.path.join(ROOT, path), encoding="utf-8-sig") as f:
        tree = ast.parse(f.read(), path)
    mods = {"subprocess": set(), "os": set()}    # local names bound to the modules (import subprocess as sp)
    banned_names = set()                          # from subprocess import run [as r]
    found = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name in mods:
                    mods[a.name].add(a.asname or a.name)
        elif isinstance(n, ast.ImportFrom) and n.module in mods:
            banned = SUBPROCESS_BANNED if n.module == "subprocess" else OS_BANNED
            for a in n.names:
                if a.name in banned:
                    banned_names.add(a.asname or a.name)
                    found.append((n.lineno, "from %s import %s" % (n.module, a.name)))
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            if f.value.id in mods["subprocess"] and f.attr in SUBPROCESS_BANNED:
                found.append((n.lineno, "subprocess.%s(" % f.attr))
            elif f.value.id in mods["os"] and f.attr in OS_BANNED:
                found.append((n.lineno, "os.%s(" % f.attr))
        elif isinstance(f, ast.Name) and f.id in banned_names:
            found.append((n.lineno, "%s(" % f.id))
    return sorted(found)


class ProcessGuardTest(unittest.TestCase):
    def test_no_direct_process_start_outside_the_helper(self):
        bad = ["%s:%d  %s" % (p.replace("\\", "/"), ln, what) for p in python_files() for ln, what in offenders(p)]
        self.assertFalse(bad, "start children with tools/hidden.py (run/popen), never directly:\n  " + "\n  ".join(bad))

    def test_guard_sees_the_bypass_forms(self):
        """The scanner itself: aliases, from-imports and function-local imports are all caught, comments and strings are not."""
        import tempfile
        src = ("import subprocess as sp\nfrom subprocess import run as r\nimport os\n"
               "def f():\n    import subprocess\n    subprocess.Popen(['x'])\n    sp.call(['x'])\n    r(['x'])\n    os.system('x')\n"
               "# subprocess.run(['x'])\nS = 'subprocess.run(x)'\n")
        with tempfile.TemporaryDirectory() as d:
            global ROOT
            old, ROOT = ROOT, d
            try:
                with open(os.path.join(d, "x.py"), "w", encoding="utf-8") as f:
                    f.write(src)
                got = offenders("x.py")
            finally:
                ROOT = old
        self.assertEqual([ln for ln, _ in got], [2, 6, 7, 8, 9])


if __name__ == "__main__":
    unittest.main()

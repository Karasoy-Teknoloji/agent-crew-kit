"""The ONE place that imports hidden (tools/hidden.py, the hidden-launch helper) for every tool and falls back when it is missing.

Use:  sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))   # the real dir: a symlink install (~/.claude/bin/x.py -> clone) still finds its siblings
      from hidden_loader import hidden

If hidden.py cannot be found (a copy without it, a script shipped over stdin without its siblings), one stderr line is printed and `hidden`
is a plain-subprocess stand-in with the same API (run, popen, flags, NO_WINDOW), so a lock, a lab run or a hook never dies on the import.
"""
import subprocess
import sys
import types


def _plain():
    m = types.ModuleType("hidden")
    m.NO_WINDOW = 0
    m.flags = lambda extra=0: extra
    m.run = lambda *a, visible=False, **kw: subprocess.run(*a, **kw)
    m.popen = lambda *a, visible=False, **kw: subprocess.Popen(*a, **kw)
    return m


try:
    import hidden
except ImportError:
    sys.stderr.write("warning: hidden.py not found next to the tool; using plain subprocess (no hidden launch on Windows)\n")
    hidden = _plain()

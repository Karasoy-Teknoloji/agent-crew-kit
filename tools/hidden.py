"""The ONE way claude-shared starts child processes without a console window on Windows (Python side; PowerShell side: tools/hidden.ps1).

Why: a console child (git, gh, powershell, claude) of a process that has no console (pythonw, a scheduled task, a hook) gets a NEW console,
and with Windows Terminal as the default terminal that is a visible window that flashes up and closes again. CREATE_NO_WINDOW stops that.
Use hidden.run / hidden.popen instead of subprocess.run / subprocess.Popen. Children that must talk to the user's terminal
(an interactive pass-through such as the gh shim, a terminal window opened on purpose) use gorunur=True, with a comment; tools/tests/test_hidden_guard.py
fails on any direct subprocess call.
On Linux/macOS everything here is a plain subprocess call.
"""
import os
import subprocess

NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW


def flags(extra=0):
    """creationflags value for a raw subprocess call that needs more flags (e.g. DETACHED_PROCESS): `extra | CREATE_NO_WINDOW`."""
    return extra | NO_WINDOW


def _kw(kw, visible=False):
    if NO_WINDOW and not visible:
        kw["creationflags"] = kw.get("creationflags", 0) | NO_WINDOW
    return kw


def run(*args, visible=False, **kw):
    """subprocess.run without a console window. gorunur=True leaves the flag out on purpose (the child owns the terminal or its window
    must be seen: an interactive pass-through, a tab opened for a person): say why in a comment at that call."""
    return subprocess.run(*args, **_kw(kw, visible))


def popen(*args, visible=False, **kw):
    """subprocess.Popen without a console window (gorunur=True: leave the flag out on purpose, see run)."""
    return subprocess.Popen(*args, **_kw(kw, visible))

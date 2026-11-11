"""The bash the tests run statusline.sh with: BASH is its path, or None when the machine has none.

On Windows `bash` on PATH can be System32 (bash.exe), the WSL launcher ("no installed distributions"), which answers every call with
UTF-16 text and exit 1. A real bash there is Git Bash (next to git.exe, or the MSYS2 one on PATH), so the WSL launcher is skipped.
"""
import atexit
import os
import shutil
import sys
import tempfile


def bul():
    if os.name != "nt":
        return shutil.which("bash")
    windir = os.environ.get("SystemRoot", r"C:\Windows").lower()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and not os.path.normcase(os.path.abspath(d)).startswith(os.path.normcase(windir)):
            p = shutil.which("bash", path=d)
            if p:
                return p
    git = shutil.which("git")
    if git:
        root = os.path.dirname(os.path.dirname(git))
        for rel in ("bin", os.path.join("usr", "bin")):
            p = os.path.join(root, rel, "bash.exe")
            if os.path.isfile(p):
                return p
    return None


BASH = bul()


def python3_bereitstellen():
    """statusline.sh calls `python3`; a Windows Python has only python.exe, so a one-line `python3` shim for Git Bash goes on this
    process's PATH (the test children inherit it). Nothing happens where python3 already exists."""
    if os.name != "nt" or shutil.which("python3"):
        return
    d = tempfile.mkdtemp(prefix="python3-shim-")
    atexit.register(shutil.rmtree, d, True)
    with open(os.path.join(d, "python3"), "w", newline=chr(10)) as f:
        f.write("#!/bin/sh" + chr(10) + 'exec "%s" "$@"' % sys.executable.replace(chr(92), "/") + chr(10))
    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


python3_bereitstellen()

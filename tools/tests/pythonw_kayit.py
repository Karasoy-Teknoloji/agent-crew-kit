"""Runner for test_gizli_pythonw.py (not a test): runs one script of this repo under pythonw.exe as __main__ and records every
child process it starts.

    pythonw.exe -I pythonw_kayit.py <record.jsonl> <script.py> [script args...]

Every subprocess.Popen start is appended to <record.jsonl> as {"argv": ..., "flags": creationflags} (subprocess.run, call and
check_output all end in Popen), then the real child is started. The last line is {"exit": code} or {"crash": traceback}.
Under pythonw sys.stdout/sys.stderr are None, exactly as in a scheduled task.
"""
import json
import os
import runpy
import subprocess
import sys
import traceback

REC, ENTRY, ARGS = sys.argv[1], sys.argv[2], sys.argv[3:]


def kayit(satir):
    with open(REC, "a", encoding="utf-8") as f:
        f.write(json.dumps(satir) + "\n")


_init = subprocess.Popen.__init__


def _kayitli(self, args, *a, **kw):
    kayit({"argv": [str(x) for x in args] if isinstance(args, (list, tuple)) else str(args), "flags": kw.get("creationflags", 0)})
    _init(self, args, *a, **kw)


subprocess.Popen.__init__ = _kayitli
sys.argv = [ENTRY] + ARGS
try:
    runpy.run_path(ENTRY, run_name="__main__")
    kayit({"exit": 0})
except SystemExit as e:
    kayit({"exit": e.code if isinstance(e.code, int) else (0 if e.code is None else 1)})
except BaseException:
    kayit({"crash": traceback.format_exc()})

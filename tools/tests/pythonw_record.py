"""Runner for test_hidden_pythonw.py (not a test): runs one script of this repo under pythonw.exe as __main__ and records every
child process it starts.

    pythonw.exe -I pythonw_record.py <record.jsonl> <script.py> [script args...]

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


def record(line):
    with open(REC, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


_init = subprocess.Popen.__init__


def _registered(self, args, *a, **kw):
    record({"argv": [str(x) for x in args] if isinstance(args, (list, tuple)) else str(args), "flags": kw.get("creationflags", 0)})
    _init(self, args, *a, **kw)


subprocess.Popen.__init__ = _registered
sys.argv = [ENTRY] + ARGS
try:
    runpy.run_path(ENTRY, run_name="__main__")
    record({"exit": 0})
except SystemExit as e:
    record({"exit": e.code if isinstance(e.code, int) else (0 if e.code is None else 1)})
except BaseException:
    record({"crash": traceback.format_exc()})

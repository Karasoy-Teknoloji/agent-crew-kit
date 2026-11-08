"""Tests for tools/gizli.py, the one hidden-launch helper (Python side)."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gizli  # noqa: E402

NT = os.name == "nt"


class Gizli(unittest.TestCase):
    def test_flags_are_create_no_window_on_windows_only(self):
        self.assertEqual(gizli.NO_WINDOW, 0x08000000 if NT else 0)
        self.assertEqual(gizli.flags(0x200), 0x200 | gizli.NO_WINDOW)

    def test_run_and_popen_add_the_flag_and_keep_the_callers_flags(self):
        with mock.patch.object(subprocess, "run") as run:
            gizli.run(["x"], creationflags=0x200, timeout=5)
        kw = run.call_args.kwargs
        self.assertEqual(kw["creationflags"], 0x200 | gizli.NO_WINDOW if NT else kw.get("creationflags", 0x200))
        self.assertEqual(kw["timeout"], 5)
        with mock.patch.object(subprocess, "Popen") as po:
            gizli.popen(["x"])
        self.assertEqual(po.call_args.kwargs.get("creationflags", 0), gizli.NO_WINDOW)

    def test_gorunur_leaves_the_flag_out_but_keeps_the_callers_own(self):
        with mock.patch.object(subprocess, "run") as run:
            gizli.run(["x"], gorunur=True)
            self.assertNotIn("creationflags", run.call_args.kwargs)
            gizli.run(["x"], gorunur=True, creationflags=0x200)
            self.assertEqual(run.call_args.kwargs["creationflags"], 0x200)
        with mock.patch.object(subprocess, "Popen") as po:
            gizli.popen(["x"], gorunur=True)
        self.assertNotIn("creationflags", po.call_args.kwargs)

    def test_a_real_child_still_runs_and_returns_output(self):
        r = gizli.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok"))

    def test_no_other_script_hardcodes_the_flag_or_builds_its_own_task_action(self):
        root = Path(__file__).resolve().parents[2]
        skip = {Path(__file__).resolve(), (root / "tools" / "gizli.py").resolve(), (root / "tools" / "gizli_yukle.py").resolve(), (root / "tools" / "gizli.ps1").resolve()}
        bad = []
        for pat in ("tools/**/*.py", "plugins/**/*.py", "tools/**/*.ps1", "plugins/**/*.ps1"):
            for p in root.glob(pat):
                if p.resolve() in skip or "tests" in p.parts or "lab" in p.parts:   # tools/lab: lab VM tasks (another machine, another user)
                    continue
                t = p.read_text(encoding="utf-8", errors="replace")
                if "0x08000000" in t or "New-ScheduledTaskAction" in t or "Register-ScheduledTask" in t.replace("Unregister-ScheduledTask", ""):
                    bad.append(str(p.relative_to(root)))
        self.assertEqual(sorted(set(bad)), [])

    def test_no_script_launches_with_a_raw_start_process_window_style_hidden(self):
        """Start-Process ... -WindowStyle Hidden still opens a console window for console exes; Start-GizliSurec (tools/gizli.ps1) is the one path."""
        import re
        root = Path(__file__).resolve().parents[2]
        rx = re.compile(r"Start-Process.*-WindowStyle\s+Hidden", re.I)
        bad = []
        for pat in ("tools/**/*.ps1", "plugins/**/*.ps1"):
            for p in root.glob(pat):
                if p.name == "gizli.ps1" or "tests" in p.parts or "lab" in p.parts:   # lab: runs on the lab VMs
                    continue
                for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if not line.lstrip().startswith("#") and rx.search(line):
                        bad.append("%s:%d" % (p.relative_to(root).as_posix(), n))
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()

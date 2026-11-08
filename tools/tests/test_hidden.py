"""Tests for tools/hidden.py, the one hidden-launch helper (Python side)."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hidden  # noqa: E402
import fork_support  # noqa: E402  (a downstream fork's team-owned paths are not scanned)

NT = os.name == "nt"


class Hidden(unittest.TestCase):
    def test_flags_are_create_no_window_on_windows_only(self):
        self.assertEqual(hidden.NO_WINDOW, 0x08000000 if NT else 0)
        self.assertEqual(hidden.flags(0x200), 0x200 | hidden.NO_WINDOW)

    def test_run_and_popen_add_the_flag_and_keep_the_callers_flags(self):
        with mock.patch.object(subprocess, "run") as run:
            hidden.run(["x"], creationflags=0x200, timeout=5)
        kw = run.call_args.kwargs
        self.assertEqual(kw["creationflags"], 0x200 | hidden.NO_WINDOW if NT else kw.get("creationflags", 0x200))
        self.assertEqual(kw["timeout"], 5)
        with mock.patch.object(subprocess, "Popen") as po:
            hidden.popen(["x"])
        self.assertEqual(po.call_args.kwargs.get("creationflags", 0), hidden.NO_WINDOW)

    def test_visible_leaves_the_flag_out_but_keeps_the_callers_own(self):
        with mock.patch.object(subprocess, "run") as run:
            hidden.run(["x"], visible=True)
            self.assertNotIn("creationflags", run.call_args.kwargs)
            hidden.run(["x"], visible=True, creationflags=0x200)
            self.assertEqual(run.call_args.kwargs["creationflags"], 0x200)
        with mock.patch.object(subprocess, "Popen") as po:
            hidden.popen(["x"], visible=True)
        self.assertNotIn("creationflags", po.call_args.kwargs)

    def test_a_real_child_still_runs_and_returns_output(self):
        r = hidden.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok"))

    def test_no_other_script_hardcodes_the_flag_or_builds_its_own_task_action(self):
        root = Path(__file__).resolve().parents[2]
        skip = {Path(__file__).resolve(), (root / "tools" / "hidden.py").resolve(), (root / "tools" / "hidden_loader.py").resolve(), (root / "tools" / "hidden.ps1").resolve()}
        bad = []
        for pat in ("tools/**/*.py", "plugins/**/*.py", "tools/**/*.ps1", "plugins/**/*.ps1"):
            for p in root.glob(pat):
                if p.resolve() in skip or "tests" in p.parts or "lab" in p.parts:   # tools/lab: lab VM tasks (another machine, another user)
                    continue
                t = p.read_text(encoding="utf-8", errors="replace")
                if "0x08000000" in t or "New-ScheduledTaskAction" in t or "Register-ScheduledTask" in t.replace("Unregister-ScheduledTask", ""):
                    bad.append(str(p.relative_to(root)))
        bad = fork_support.drop_team_owned(root, bad)
        self.assertEqual(sorted(set(bad)), [])

    def test_no_script_launches_with_a_raw_start_process_window_style_hidden(self):
        """Start-Process ... -WindowStyle Hidden still opens a console window for console exes; Start-HiddenProcess (tools/hidden.ps1) is the one path."""
        import re
        root = Path(__file__).resolve().parents[2]
        rx = re.compile(r"Start-Process.*-WindowStyle\s+Hidden", re.I)
        bad = []
        for pat in ("tools/**/*.ps1", "plugins/**/*.ps1"):
            for p in root.glob(pat):
                if p.name == "hidden.ps1" or "tests" in p.parts or "lab" in p.parts:   # lab: runs on the lab VMs
                    continue
                for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if not line.lstrip().startswith("#") and rx.search(line):
                        bad.append("%s:%d" % (p.relative_to(root).as_posix(), n))
        owned = fork_support.team_owned(root, {b.rsplit(":", 1)[0] for b in bad})
        self.assertEqual([b for b in bad if b.rsplit(":", 1)[0] not in owned], [])


if __name__ == "__main__":
    unittest.main()

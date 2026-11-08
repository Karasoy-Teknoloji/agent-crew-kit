#!/usr/bin/env python3
"""The ONE entry of the daily lost-work job (Windows task / systemd user unit 'lost-work'): runs tools/lost_work.py with the daily options.

Why a wrapper: the scheduler keeps whatever arguments were written at install time, so a changed option would reach other people's
machines only after they re-ran install.sh / install.ps1. The task and the unit now call THIS script with no arguments; the options live
here, in the clone, and a change arrives with the normal pull ("use the source, don't copy"). tools/rules_watch.py rewrites an
older install (task or unit that still embeds lost_work.py arguments) to call this script.

Extra command line arguments are appended to ARGS (e.g. --root, --no-fetch for a manual run). Never --force, never main/master/test/dev.
"""
import os
import runpy
import sys

TOOLS = os.path.dirname(os.path.realpath(__file__))
TARGET = os.path.join(TOOLS, "lost_work.py")
# push own unpushed branches, remove lossless worktrees/branches, short report, notify, refresh the session-start cache
ARGS = ["--push-unpushed", "--clean-lossless", "--brief", "--notify", "--min-idle", "1", "--cache-yaz"]


def main():
    sys.path.insert(0, TOOLS)   # python -I drops the script directory
    sys.argv = [TARGET, *ARGS, *sys.argv[1:]]
    runpy.run_path(TARGET, run_name="__main__")   # lost_work.py ends with sys.exit(main())


if __name__ == "__main__":
    main()

"""Test support that lets a downstream fork of this kit run the kit's own suites unchanged (README, "Team fork").

Two helpers, one module, so every suite uses the same mechanism:

1. isolate_team_config(): points CLAUDE_SHARED_TEAM_CONFIG_DIR (tools/settings.py, layer 3 of the config keys) at an empty temp
   directory. Without it a test that goes through settings.value() reads the fork's committed `team-config/` (its report folder, org,
   remote ...) and writes into the team's real report folders. Every suite that can reach settings.py calls it once, from its
   `test_00_team_isolation.py` (the 00 sorts first, so it runs before any module that reads a config key at import time).

2. drop_team_owned(root, rel_paths): the scan tests (process-start guard, hidden-loader import scan, personal-data scan) check every
   file of the repo. A fork marks files that are its own, and not the kit's, with the git attribute `team-owned` in its
   `.gitattributes`:

       tools/lab/**    team-owned

   The scans skip those paths. The attribute is read with `git check-attr`, so the pattern syntax is git's own. The lookup uses a
   throwaway git dir with `--work-tree=<root>`, so it works the same in a clone, a worktree and a `git archive` export (no .git).
   Without a `.gitattributes`, or without git, nothing is skipped and the kit scans everything.

Tests of this module: tools/tests/test_00_team_isolation.py. Stdlib only.
"""
import atexit
import os
import shutil
import subprocess
import tempfile

ENV = "CLAUDE_SHARED_TEAM_CONFIG_DIR"
ATTRIBUTE = "team-owned"
_empty_dir = None


def isolate_team_config():
    """Set ENV to an empty temp directory (the same one on every call in this process) and return its path. The directory is
    removed at exit. A value already in ENV is replaced: the kit's tests never read a team's configuration."""
    global _empty_dir
    if _empty_dir is None:
        _empty_dir = tempfile.mkdtemp(prefix="kit-team-config-")
        atexit.register(shutil.rmtree, _empty_dir, True)
    os.environ[ENV] = _empty_dir
    return _empty_dir


def team_owned(root, rel_paths):
    """The subset of rel_paths (relative to root, any slash style) that root's .gitattributes marks `team-owned`."""
    rels = [p.replace("\\", "/") for p in rel_paths]
    if not rels or not os.path.isfile(os.path.join(root, ".gitattributes")):
        return set()
    git_dir = tempfile.mkdtemp(prefix="kit-attr-")
    try:
        base = ["git", "--git-dir=" + git_dir, "--work-tree=" + root]
        subprocess.run(base[:1] + ["init", "-q", "--bare", git_dir], check=True, capture_output=True, timeout=60)
        r = subprocess.run(base + ["check-attr", "-z", "--stdin", ATTRIBUTE], input="\0".join(rels).encode("utf-8") + b"\0",
                           capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return set()   # no git: the kit's own tree has no team-owned paths, so scanning everything is the safe default
    finally:
        shutil.rmtree(git_dir, True)
    if r.returncode != 0:
        return set()
    fields = r.stdout.decode("utf-8", "replace").split("\0")   # path, attribute, value, path, attribute, value, ...
    return {fields[i] for i in range(0, len(fields) - 2, 3) if fields[i + 2] == "set"}


def drop_team_owned(root, rel_paths):
    """rel_paths without the team-owned ones, in the original order and spelling."""
    rel_paths = list(rel_paths)
    owned = team_owned(root, rel_paths)
    return [p for p in rel_paths if p.replace("\\", "/") not in owned]

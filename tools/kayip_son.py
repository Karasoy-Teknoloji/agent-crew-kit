"""The lost-work result cache shared by tools/kayip_is.py (writes it) and tools/rules_watch.py (reads it at session start).

Why: a full scan takes 15-25 s on a machine with many repos, so the SessionStart hook must never wait for it. The scan runs in the
background (the daily job, or a detached refresh the hook starts when the cache is old) and leaves its result here; the hook only reads
this file. One authority for the file's location, format, atomic write and the refresh lock.

File (default ~/.local/state/kayip-is-son.json, env KAYIP_IS_SON): {"ts": epoch seconds, "host": node name, "summary": {...},
"message": the human report text of the scan, or "" when nothing is at risk and nothing is stale}. Written to a temp file in the same
folder and renamed, so a reader never sees half a file. Lock: <file>.lock, created by whoever starts a refresh (O_EXCL), removed by
the scan when it ends; a lock older than LOCK_MAX seconds belongs to a dead process and is taken over.
Python 3, no dependencies; every function ignores I/O errors (a cache that cannot be read or written just means "no cache").
"""
import json
import os
import platform
import time
from pathlib import Path

LOCK_MAX = 600   # seconds; a scan (hook flavour, 120 s budget) never runs longer than this


def path():
    return Path(os.environ.get("KAYIP_IS_SON") or Path.home() / ".local" / "state" / "kayip-is-son.json")


def lock_path(cache=None):
    """Lock file of the cache file `cache` (default: the lost-work cache). tools/rules_watch.py uses the same lock for its own cache."""
    p = Path(cache) if cache else path()
    return p.with_name(p.name + ".lock")


def atomic_write(p, text):
    """Write text to p through a temp file in the same folder plus os.replace (raises OSError)."""
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
    finally:
        try:
            tmp.unlink()   # only still there when the rename failed
        except OSError:
            pass


def write(summary, message, cleaned=None):
    """Store the result of a scan (True when written). `cleaned` is what the daily run removed ([{repo, path, branch, sha, kind}], see
    kayip_is.clean_lossless): a list (even empty) replaces the stored one, None (a refresh scan that cleans nothing) keeps the last
    one with its time, so the removal notice survives the hourly refresh."""
    doc = {"ts": int(time.time()), "host": platform.node(), "summary": summary, "message": message or ""}
    if cleaned is not None:
        doc["cleaned"], doc["cleaned_ts"] = cleaned, doc["ts"]
    old = read_raw()
    if cleaned is None and old and isinstance(old.get("cleaned"), list):
        doc["cleaned"], doc["cleaned_ts"] = old["cleaned"], old.get("cleaned_ts", old.get("ts", 0))
    if old and isinstance(old.get("rules"), dict):   # the other writer's part of the one file survives
        doc["rules"] = old["rules"]
    try:
        atomic_write(path(), json.dumps(doc, ensure_ascii=False))
        return True
    except OSError:
        return False


def cleaned_text(cleaned):
    """'temizlendi: N worktree, M dal' for a list of removals, "" when there are none."""
    w = sum(1 for c in cleaned if c.get("kind") == "worktree")
    b = sum(1 for c in cleaned if c.get("kind") == "branch")
    return f"temizlendi: {w} worktree, {b} dal" if w or b else ""


def cleaned_line(doc, max_age=86400):
    """The removal notice of a cache document (read()), "" when there is none or it is older than a day."""
    c = doc.get("cleaned")
    if not isinstance(c, list) or time.time() - doc.get("cleaned_ts", 0) > max_age:
        return ""
    return cleaned_text([x for x in c if isinstance(x, dict)])


def read_raw():
    """The file's JSON object whatever it holds (a rules-only file has no scan result yet), or None."""
    try:
        d = json.loads(path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def rules_write(doc):
    """Store the result of the rules refresh (tools/rules_watch.py --yenile: notes, pull state) under "rules" of the SAME file, keeping
    the scan result as it is. A file without a scan result still reads as "no cache" for read() (no top-level ts)."""
    d = read_raw() or {}
    d["rules"] = doc
    try:
        atomic_write(path(), json.dumps(d, ensure_ascii=False))
        return True
    except OSError:
        return False


def rules_read():
    """The last rules refresh result of this machine (a dict with "ts"), or None."""
    r = (read_raw() or {}).get("rules")
    return r if isinstance(r, dict) and isinstance(r.get("ts"), (int, float)) and r.get("host") == platform.node() else None


def fetch_taze(repo, minutes=None):
    """True when `repo` (a normal clone) was fetched within `minutes` (mtime of .git/FETCH_HEAD): the scan, rules_watch and other callers
    share one fetch per repo instead of each fetching again. False for a worktree-style .git file or a never-fetched repo.
    `minutes` defaults to env KAYIP_FETCH_TAZE_DK, else 10 (0 = always fetch)."""
    if minutes is None:
        minutes = float(os.environ.get("KAYIP_FETCH_TAZE_DK") or 10)
    try:
        return time.time() - (Path(repo) / ".git" / "FETCH_HEAD").stat().st_mtime < minutes * 60
    except OSError:
        return False


def read():
    """The cached result {"ts", "host", "summary", "message"} of this machine, or None (missing, unreadable, another host)."""
    try:
        d = json.loads(path().read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("ts"), (int, float)) and isinstance(d.get("message"), str) \
                and d.get("host") == platform.node():
            return d
    except (OSError, ValueError):
        pass
    return None


def take_lock(cache=None):
    """True when this process may start a refresh (no live lock). False: another refresh runs, or the lock cannot be made."""
    p = lock_path(cache)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                os.close(os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return True
            except FileExistsError:
                if time.time() - p.stat().st_mtime < LOCK_MAX:
                    return False
                p.unlink()   # a dead refresh left it; one retry
    except OSError:
        pass
    return False


def release_lock(cache=None):
    try:
        lock_path(cache).unlink()
    except OSError:
        pass

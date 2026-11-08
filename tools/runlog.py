"""The one rolling run log of claude-shared (standard library only), for every scheduled entry point.

A scheduled task runs under pythonw: no stdout, no stderr, so without a file log a failing run leaves no trace.

    sys.path.insert(0, <tools dir>)     # python -I drops the script directory
    import runlog
    LOG = runlog.runlog("lost-work")     # ~/.local/state/lost-work.log, env LOST_WORK_LOG overrides the path
    LOG.info("start pid=%d", os.getpid()); LOG.exception("crash")

Rolling files: 1 MB each, at most 64 files (the live file + 63 rotated), utf-8. Secrets (URL credentials, GitHub / Slack tokens, Bearer)
are masked in every line. A log that cannot be opened or rotated is ignored silently (logging never changes a run's result); no console
handler, so it is safe without sys.stderr. Children are started with tools/hidden.py, not here.
"""
import logging
import logging.handlers
import os
import re

LOG_BYTES = 1_000_000          # one log file
LOG_BACKUP = 63                 # rotated copies kept: the live file + 63 = at most 64 files (team standard)


_HIDDEN = (
    (re.compile(r"://[^/\s@]+@"), r"://***@"),                                                      # scheme://user:pass@host
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{10,})"), "***"),
    (re.compile(r"(?i)\b(bearer|token)[ =:]+[A-Za-z0-9._~+/=-]{16,}"), r"\1 ***"),
)


def mask_out(text):
    """Mask URL credentials and common token shapes (GitHub, Slack, Bearer) in a log line."""
    text = str(text)
    for rx, instead in _HIDDEN:
        text = rx.sub(instead, text)
    return text


class _Fmt(logging.Formatter):
    def format(self, record):
        return mask_out(super().format(record))


class _File(logging.handlers.RotatingFileHandler):
    def handleError(self, record):      # a locked file during rotation (two runs at once on Windows) must never reach the caller
        pass


def log_path(name):
    """~/.local/state/<ad>.log (the env variable <AD>_LOG overrides it in runlog(); dashes become underscores: lost-work -> LOST_WORK_LOG)."""
    return os.path.join(os.path.expanduser("~"), ".local", "state", name + ".log")


def runlog(name, level=logging.INFO, path=None, fmt="%(asctime)s %(levelname)s %(message)s"):
    """Rolling run log for the scheduled entry point `name`: a logging.Logger without propagation; calling it again is a no-op.

    File: env <AD>_LOG, else `path`, else ~/.local/state/<ad>.log. 1 MB per file, at most 64 files, utf-8, timestamps
    `YYYY-MM-DD HH:MM:SS`, credentials in URLs and token shapes masked. `fmt` is the logging format (default: time, level,
    message). A log that cannot be opened or rotated is ignored silently (the logger then holds a NullHandler): logging never
    changes a run's result. No console handler, so it is safe with sys.stderr None (pythonw). A call with a different
    path than the attached handler's (tests, a moved log) replaces that handler."""
    lg = logging.getLogger(name)
    lg.propagate = False
    try:
        path = os.path.abspath(os.environ.get(re.sub(r"\W", "_", name).upper() + "_LOG") or path or log_path(name))
        for h in list(lg.handlers):
            if isinstance(h, _File) and h.baseFilename == path:
                return lg
            if isinstance(h, (_File, logging.NullHandler)):
                lg.removeHandler(h)
                h.close()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        h = _File(path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUP, encoding="utf-8", delay=True)
        h.setFormatter(_Fmt(fmt, "%Y-%m-%d %H:%M:%S"))
        h.stream = h._open()        # fail now (bad path) instead of at the first record
        lg.addHandler(h)
        lg.setLevel(level)
    except Exception:
        if not lg.handlers:
            lg.addHandler(logging.NullHandler())
    return lg

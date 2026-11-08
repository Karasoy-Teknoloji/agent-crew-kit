"""Real-screen test of the Windows status line: runs the REAL `claude` CLI in a pseudo-terminal (ConPTY), reads the rendered screen with a
terminal emulator (pyte) and measures what the status line really looks like at several widths and after a live resize.
SKIPPED by default. It starts a real Claude Code session (no prompt is ever sent to the model, so no quota is used; /exit ends it).

How to run (Windows, PowerShell; nothing is installed globally or into the repo):
    python -m venv $env:TEMP\\quota-ui-venv ; & $env:TEMP\\quota-ui-venv\\Scripts\\pip install pywinpty pyte
    $env:QUOTA_UI_TEST = "1"
    & $env:TEMP\\quota-ui-venv\\Scripts\\python -I -m unittest -v plugins/quota-pace/tests/test_real_screen.py   (from the repo root)
Needs: `claude` on PATH (logged in), pwsh, pywinpty + pyte. Optional: QUOTA_UI_WIDTH="80,120,204" to limit the widths,
QUOTA_UI_OUTPUT=<folder> to keep the screen dumps (otherwise they are only printed).

What it does NOT touch: the live ~/.claude/settings.json (the session loads only project/local settings plus a temporary --settings file that sets
the statusLine and the bypass-permissions mode), other running claude processes, the real wrapper's state files: pace.json, pace-history.csv,
pace-forecast.*, cost/durum.* are copied to a temp folder before the end-to-end tests run the real wrapper and put back afterwards (SHA-256
compared; anything a developer's own live session wrote in that window is overwritten too, so run it while no other session is busy). Side effects that
remain: the cwd (%TEMP%\\quota-ui-cwd) gets a project entry in ~/.claude.json, and every session leaves a transcript under ~/.claude/projects and
/exit etc. in the prompt history. The renderer is forced with CLAUDE_CODE_NO_FLICKER so killed test sessions do not count as "fullscreen failed
to start" strikes (~/.claude.json fullscreenBootStrikes; /tui fullscreen clears a strike-out). Startup dialogs: the harness answers only the two
it knows (external CLAUDE.md imports -> Enter = "No"; trust folder -> Enter) and fails on anything else, never guessing.

Measured at Claude Code 2.1.310 / Windows 11 (details in INSTALL.md, "Satir sarma: olculen kenar payi"): COLUMNS and LINES equal the pty size; Claude
Code never wraps a status line row, it cuts it with an ellipsis; a row shows at most width - 4 cells (2 cells indent on the left, 2 empty on the
right) plus 2 per unit of settings.json statusLine.padding; a resize alone does not re-run the command (the next event does, with the new COLUMNS).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

try:
    import pyte
    from winpty import PtyProcess
except Exception:  # pragma: no cover - libs missing
    pyte = PtyProcess = None

HERE = os.path.dirname(os.path.abspath(__file__))
PLUG = os.path.normpath(os.path.join(HERE, ".."))
CLAUDE = shutil.which("claude")
PWSH = shutil.which("pwsh") or shutil.which("powershell")
ENABLED = os.environ.get("QUOTA_UI_TEST") == "1" and bool(CLAUDE and PWSH and pyte and PtyProcess and os.name == "nt")
WIDTHS = [int(x) for x in os.environ.get("QUOTA_UI_WIDTH", "80,120,160,204,250").split(",") if x.strip()]
OUTPUT = os.environ.get("QUOTA_UI_OUTPUT")
LINE = 40                                    # pty height


# ---------------------------------------------------------------------------------------------------------------------------------------
# Terminal harness
# ---------------------------------------------------------------------------------------------------------------------------------------
FOOTER = re.compile(r"(bypass permissions|mode on|\? for shortcuts|shift\+tab to cycle)")
_PRIV = re.compile(r"\x1b\[[<>=][0-9;]*[a-zA-Z]")   # kitty-keyboard / xterm modifyOtherKeys sequences pyte would misread


class Screen:
    """One real claude session in a pty of the given size, with its screen mirrored into a pyte emulator."""

    def __init__(self, setting, cols, rows=LINE, cwd=None, env_extra=None, extra=()):
        self.cols, self.rows = cols, rows
        self.lock = threading.Lock()
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)
        self.raw = bytearray()
        env = {k: v for k, v in os.environ.items() if not re.match(r"(CLAUDE|ANTHROPIC_|WT_|COLUMNS$|LINES$|QUOTA_)", k, re.I)}
        env["CLAUDE_CODE_NO_FLICKER"] = "1"                   # fullscreen (what a developer runs), and a killed test session is not counted as a failed start
        env.update(env_extra or {})
        env = {k: v for k, v in env.items() if v is not None}
        self.cwd = cwd or os.path.join(tempfile.gettempdir(), "quota-ui-cwd")      # fixed: one trust entry, not one per run
        os.makedirs(self.cwd, exist_ok=True)
        self.proc = PtyProcess.spawn([CLAUDE, "--setting-sources", "project,local", "--settings", setting, *extra], cwd=self.cwd, env=env,
                                     dimensions=(rows, cols))
        self.t = threading.Thread(target=self._read, daemon=True)
        self.t.start()

    def _read(self):
        try:
            while True:
                d = self.proc.read(65536)
                with self.lock:
                    self.raw += d.encode("utf-8", "replace")
                    self.stream.feed(_PRIV.sub("", d))
        except Exception:
            pass

    def resize(self, cols, rows=None):
        rows = rows or self.rows
        with self.lock:
            self.screen.resize(rows, cols)
            self.cols, self.rows = cols, rows
        self.proc.setwinsize(rows, cols)

    def lines(self):
        """[(text, first_col, end_col)] for every screen row; end_col is exclusive; wide chars count 2 (pyte stores a '' continuation cell)."""
        with self.lock:
            out = []
            for y in range(self.rows):
                buf = self.screen.buffer[y]
                last = -1
                first = -1
                chars = []
                for x in range(self.cols):
                    c = buf[x].data
                    chars.append(c if c != "" else "")
                    if c not in (" ",):
                        if first < 0:
                            first = x
                        last = x
                out.append(("".join(chars).rstrip(), first, last + 1))
            return out

    def text(self):
        return "\n".join("%2d|%s" % (i, s[0]) for i, s in enumerate(self.lines()) if s[0])

    def entry_box_ready(self):
        """True when the prompt box is on screen: two full-width borders with the '>' prompt line between them."""
        r = self.lines()
        edge = [i for i, (s, a, b) in enumerate(r) if len(s) >= 0.9 * self.cols and set(s.replace("─", "")) <= {" "} and "─" in s]
        for i, j in zip(edge, edge[1:]):
            if j > i + 1 and any(r[k][0].lstrip().startswith(("❯", ">")) for k in range(i + 1, j)):
                return j
        return None

    # startup dialogs the harness knows: (text on screen, key to press). Anything else is a failure: never answer a question blindly.
    DIALOGS = [("Allow external CLAUDE.md file imports?", "\r"),     # default "No, disable external imports"
                  ("trust this folder", "\r"), ("Do you trust the files", "\r")]

    def wait_ready(self, time_=90):
        """Wait for the prompt box and the footer; answers the known startup dialogs, raises on unknown ones."""
        end_at = time.time() + time_
        son_enter = 0
        while time.time() < end_at:
            time.sleep(0.5)
            j = self.entry_box_ready()
            if j is not None and any(FOOTER.search(s[0]) for s in self.lines()):
                time.sleep(1.5)                                   # let the first status line command finish
                return j
            t = self.text()
            if ("Enter to confirm" in t or "Press Enter to continue" in t or "Enter to select" in t) and time.time() - son_enter > 3:
                key = next((k for s, k in self.DIALOGS if s in t), None)
                if key is None:
                    raise AssertionError("unknown startup dialog, not answering it. Screen:\n%s" % t)
                self.proc.write(key)
                son_enter = time.time()
        raise AssertionError("prompt box did not appear in %ss. Screen:\n%s" % (time_, self.text()))

    def status_lines(self):
        """Rows between the bottom border of the prompt box and the footer ('bypass permissions' / '? for shortcuts')."""
        r = self.lines()
        j = self.entry_box_ready()
        if j is None:
            return None, []
        f = next((i for i in range(j + 1, len(r)) if FOOTER.search(r[i][0])), len(r))
        return j, [(i, r[i]) for i in range(j + 1, f)]

    def wait(self, condition, time_=20, interval=0.25):
        end_at = time.time() + time_
        while time.time() < end_at:
            v = condition()
            if v:
                return v
            time.sleep(interval)
        return None

    def off(self):
        try:
            self.proc.write("\x03")
            time.sleep(0.3)
            self.proc.write("/exit\r")
            time.sleep(1.5)
        except Exception:
            pass
        try:
            if self.proc.isalive():
                self.proc.terminate(force=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------------------------------------------------------------------
# Probe status line command: prints what the control file says and logs the environment it was started with (writes nothing else)
# ---------------------------------------------------------------------------------------------------------------------------------------
PROBE = r'''
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
raw = sys.stdin.buffer.read()
try:
    ctl = json.load(open(os.path.join(here, "ctl.json"), encoding="utf-8"))
except Exception:
    ctl = {"text": "?"}
with open(os.path.join(here, "probe-log.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"t": time.time(), "COLUMNS": os.environ.get("COLUMNS"), "LINES": os.environ.get("LINES"), "stdin_len": len(raw),
                        "ctl": ctl}) + "\n")
sys.stdout.buffer.write(ctl["text"].encode("utf-8"))
'''


class Prob:
    """Temp folder with the probe script, its control file (ctl.json) and log (probe-log.jsonl) and a --settings file using them."""

    def __init__(self, padding=None, refresh=None):
        self.dir = tempfile.mkdtemp(prefix="quota-ui-probe-")
        with open(os.path.join(self.dir, "probe.py"), "w", encoding="utf-8") as f:
            f.write(PROBE)
        self.ctl = os.path.join(self.dir, "ctl.json")
        self.log = os.path.join(self.dir, "probe-log.jsonl")
        self.setting = os.path.join(self.dir, "settings.json")
        self.write("?")
        sl = {"type": "command", "command": "%s %s" % (sys.executable.replace("\\", "/"), os.path.join(self.dir, "probe.py").replace("\\", "/"))}
        write_setting(self.setting, sl, padding, refresh)

    def write(self, text):
        with open(self.ctl, "w", encoding="utf-8") as f:
            json.dump({"text": text}, f)

    def records(self):
        try:
            with open(self.log, encoding="utf-8") as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def clean(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def write_setting(path, statusline, padding=None, refresh=None):
    """Temporary --settings file: only the statusLine (plus the bypass-permissions mode, so the footer looks like the real one)."""
    sl = dict(statusline)
    if padding is not None:
        sl["padding"] = padding
    if refresh is not None:
        sl["refreshInterval"] = refresh
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"statusLine": sl, "permissions": {"defaultMode": "bypassPermissions"}, "skipDangerousModePermissionPrompt": True}, f)


RENDERERLER = {"full-screen": {"CLAUDE_CODE_NO_FLICKER": "1"}, "classic": {"CLAUDE_CODE_NO_FLICKER": "0"}}


def measure(e, prob, text, wait=15):
    """Make the probe print `text`, wait until Claude Code has re-run it (needs refreshInterval) and the screen has settled; return the status rows."""
    n0 = len(prob.records())
    prob.write(text)
    if not e.wait(lambda: any(k["ctl"].get("text") == text for k in prob.records()[n0:]), wait):
        raise AssertionError("status line was not re-run within %ss" % wait)
    time.sleep(0.9)                                           # debounce + draw
    return e.status_lines()[1]


# ---------------------------------------------------------------------------------------------------------------------------------------
# End-to-end helpers: REAL wrapper + REAL statusline.ps1 fed with a realistic status-line JSON, live state files guarded
# ---------------------------------------------------------------------------------------------------------------------------------------
HOME_CLAUDE = os.path.join(os.path.expanduser("~"), ".claude")
SHIM = r'''
import json, os, subprocess, sys
wrap, over, ek = sys.argv[1], sys.argv[2], sys.argv[3]
import time
LOG = os.path.join(os.path.dirname(over), "shim-log.jsonl")
d = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
o = json.load(open(over, encoding="utf-8"))
sl = o.pop("_statusline")
for k, v in o.items():
    if isinstance(v, dict) and isinstance(d.get(k), dict):
        d[k].update(v)
    else:
        d[k] = v
cmd = ["pwsh", "-NoProfile", "-File", wrap, "-StatusLine", sl, "-Ek", ek]
r = subprocess.run(cmd, input=json.dumps(d).encode("utf-8"), capture_output=True)
out = r.stdout.rstrip(b"\r\n").replace(b"\r\n", b"\n")
with open(LOG, "a", encoding="utf-8") as lf:
    lf.write(json.dumps({"t": time.time(), "COLUMNS": os.environ.get("COLUMNS"), "LINES": os.environ.get("LINES"), "out": out.decode("utf-8")}) + "\n")
sys.stdout.buffer.write(out)
'''


def _read(p):
    if not os.path.isfile(p):
        return None
    with open(p, "rb") as f:
        return f.read()


class Guard:
    """Copies the live state files the real status line writes (pace.json, pace-history.csv, pace-forecast.*, cost/durum.*) and puts them
    back on exit; `changing` lists what had changed (also by a developer's own live session, which writes them too)."""
    FILES = ["pace.json", "pace-history.csv", "pace-forecast.json", "pace-forecast.lock", "pace-forecast.log", "pace-announced.json", "pace-activity.json",
                os.path.join("cost", "status.cache"), os.path.join("cost", "status.line"), os.path.join("cost", "status.lock"),
                os.path.join("cost", "cache.json")]

    def __enter__(self):
        self.once = {}
        for g in self.FILES:
            p = os.path.join(HOME_CLAUDE, g)
            self.once[g] = _read(p)
        self.sha_once = {g: (hashlib.sha256(b).hexdigest() if b is not None else None) for g, b in self.once.items()}
        self.backup = tempfile.mkdtemp(prefix="quota-ui-guard-")      # on disk too: if the run is killed, copy these files back by hand
        for g, b in self.once.items():
            if b is not None:
                os.makedirs(os.path.dirname(os.path.join(self.backup, g)), exist_ok=True)
                with open(os.path.join(self.backup, g), "wb") as f:
                    f.write(b)
        print("\nstate files copied to %s (removed after a verified restore)" % self.backup)
        return self

    def __exit__(self, *a):
        lock = os.path.join(HOME_CLAUDE, "pace-forecast.lock")
        end_at = time.time() + 40
        while os.path.exists(lock) and time.time() < end_at:       # a detached idle-priority forecast may still be running: wait for it
            time.sleep(1)
        time.sleep(4)                                             # ... and for a cost refresh
        self.changing = []
        for g, b in self.once.items():
            p = os.path.join(HOME_CLAUDE, g)
            now = _read(p)
            if now != b:
                self.changing.append(g)
                if b is None:
                    os.remove(p)
                else:
                    with open(p, "wb") as f:
                        f.write(b)
        self.sha_after = {g: (hashlib.sha256(b).hexdigest() if b is not None else None)
                          for g, b in ((g, _read(os.path.join(HOME_CLAUDE, g))) for g in self.once)}
        self.equal = self.sha_once == self.sha_after
        if self.equal:
            shutil.rmtree(self.backup, ignore_errors=True)


WRAP = os.path.join(PLUG, "bin", "statusline-local.ps1")
EK = os.path.join(HOME_CLAUDE, "bin", "statusline-ek.ps1")


def default_pay():
    """The margin the wrapper uses when nothing is configured (the number in `$pay = N` after its measurement comment)."""
    m = re.search(r"^\$pay = (\d+)\s*$", open(WRAP, encoding="utf-8-sig").read(), re.M)
    return int(m.group(1))


def cell(text):
    """Cells a text takes on screen (wide chars/emoji 2), independent of the wrapper's own PowerShell implementation."""
    import wcwidth
    return sum(max(wcwidth.wcwidth(c), 0) for c in re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text))


def example_load(model_name="Sonnet 5.5", dir_=None):
    """A status-line JSON like Claude Code sends late in a long session (limits, context, cache, cost), merged over the real one of the session.
    The weekly/5-hour values are the live ones from pace.json so the real script finds nothing 'new' to log or forecast."""
    now = int(time.time())
    kul, bit = (64.0, 0.0), (now + 3 * 86400, now + 12000)
    try:
        pj = json.load(open(os.path.join(HOME_CLAUDE, "pace.json"), encoding="utf-8-sig"))
        if pj["week_resets_at"] > now and pj["five_hour_resets_at"] > now:
            kul, bit = (pj["week_used"], pj["five_hour_used"]), (pj["week_resets_at"], pj["five_hour_resets_at"])
    except Exception:
        pass
    return {"model": {"id": "claude-sonnet-5-5", "display_name": model_name}, "effort": {"level": "xhigh"},
            "workspace": {"current_dir": (dir_ or os.path.dirname(os.path.dirname(PLUG))).replace("\\", "/")},
            "context_window": {"used_percentage": 11, "context_window_size": 1000000,
                               "current_usage": {"input_tokens": 100000, "cache_creation_input_tokens": 5000, "cache_read_input_tokens": 5000}},
            "rate_limits": {"seven_day": {"used_percentage": kul[0], "resets_at": bit[0]},
                            "five_hour": {"used_percentage": kul[1], "resets_at": bit[1]}},
            "prompt_cache": {"hit_ratio": 0.98, "misses": 12}, "cost": {"total_cost_usd": 1268.30},
            "_statusline": os.path.join(PLUG, "statusline.ps1")}


class Real:
    """Temp folder with the shim (real wrapper + real statusline.ps1 behind a real status-line command) and a --settings file using it."""

    def __init__(self, model_name="Sonnet 5.5", dir_=None):
        self.dir = tempfile.mkdtemp(prefix="quota-ui-real-")
        self.shim = os.path.join(self.dir, "shim.py")
        self.over = os.path.join(self.dir, "overlay.json")
        self.log = os.path.join(self.dir, "shim-log.jsonl")
        self.setting = os.path.join(self.dir, "settings.json")
        with open(self.shim, "w", encoding="utf-8") as f:
            f.write(SHIM)
        self.ek = EK if os.path.isfile(EK) and os.environ.get("QUOTA_UI_EXTRA") != "0" else os.path.join(self.dir, "yok-ek.ps1")
        self.model(model_name, dir_)
        self.argv = [sys.executable, self.shim, WRAP, self.over, self.ek]
        self.command = " ".join(x.replace("\\", "/") for x in self.argv)
        write_setting(self.setting, {"type": "command", "command": self.command})

    def model(self, name, dir_=None):
        with open(self.over, "w", encoding="utf-8") as f:
            json.dump(example_load(name, dir_), f)

    def records(self):
        try:
            with open(self.log, encoding="utf-8") as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def direct(self, width, **env):
        """Run the same command outside Claude Code at COLUMNS=genislik (the reference output); returns the list of lines."""
        e = {k: v for k, v in os.environ.items() if not re.match(r"(CLAUDE|QUOTA_|COLUMNS$)", k)}
        e.update(env, COLUMNS=str(width))
        r = subprocess.run(self.argv, input=b"{}", capture_output=True, env=e, timeout=120)
        return r.stdout.decode("utf-8").split("\n")

    def clean(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def hour_mask(s):
    return re.sub(r"\b\d\d:\d\d\b", "SS:DD", s)


def wait_status(e, time_=40):
    """Wait until the first real status line (not the placeholder) is on screen; returns its rows."""
    def ready():
        j, r = e.status_lines()
        return r if j is not None and r and any(len(s[0].strip()) > 10 for _, s in r) else None
    r = e.wait(ready, time_, 0.5)
    if not r:
        raise AssertionError("no status line on screen after %ss:\n%s" % (time_, e.text()))
    time.sleep(1.0)
    return e.status_lines()[1]


def screen_summary(lines):
    return "\n".join("   row %d: indent %d, right edge %d, width %d cells | %s" % (i + 1, a, b, b - a, s) for i, (s, a, b) in enumerate(lines))


def en_big_l(e, prob, n):
    """Largest probe line (cells) shown whole (one row, no ellipsis) at width n; binary search between n-20 and n."""
    def ok(r, l):
        return len(r) == 1 and r[0][1][0].count("x") == l and "…" not in r[0][1][0]
    lo, hi = n - 20, n
    assert ok(measure(e, prob, "x" * lo), lo), "even %d cells are not shown whole at width %d" % (lo, n)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ok(measure(e, prob, "x" * mid), mid):
            lo = mid
        else:
            hi = mid
    return lo, measure(e, prob, "x" * (lo + 1))


@unittest.skipUnless(ENABLED, "real-screen test: set QUOTA_UI_TEST=1 (Windows, claude + pwsh on PATH, pip install pywinpty pyte)")
class RealScreen(unittest.TestCase):
    ek_env = RENDERERLER["full-screen"]

    def _session(self, setting, n, env=None):
        e = Screen(setting, n, env_extra=dict(self.ek_env, **(env or {})))
        self.addCleanup(e.off)
        e.wait_ready(90)
        return e

    # 1 + 2a: the real wrapper on the real screen at several widths, and the COLUMNS the status line process really gets
    def test_1_real_wrapper_width_per(self):
        with Guard() as k:
            for n in WIDTHS:
                g = Real()
                self.addCleanup(g.clean)
                e = self._session(g.setting, n)
                rows = wait_status(e)
                e.off()
                kay = g.records()                                                 # before the reference run below logs too
                ref = [hour_mask(s) for s in kay[-1]["out"].split("\n")]          # exactly what Claude Code was given (live forecast drifts)
                screen = [hour_mask(s[0].strip()) for _, s in rows]
                print("\n== width %d (pty %dx%d) renderer full-screen: %d status row(s)\n%s" % (n, n, LINE, len(rows), screen_summary([s for _, s in rows])))
                self.assertEqual(screen, ref, "screen rows differ from the wrapper output at COLUMNS=%d" % n)    # nothing cut/wrapped by Claude Code
                for _, (s, a, b) in rows:
                    self.assertNotIn("…", s)
                    self.assertEqual(a, 2)                                         # Claude Code's own left indent
                    self.assertLessEqual(b - a, n - 4)                             # inside the measured limit
                self.assertTrue(kay)
                self.assertEqual({x["COLUMNS"] for x in kay}, {str(n)}, "status line process COLUMNS != pty width")
                self.assertEqual({x["LINES"] for x in kay}, {str(LINE)})
        print("\nstate files restored: %s (changed during the run: %s)" % (k.equal, k.changing))
        self.assertTrue(k.equal, "pace.json / pace-history.csv ... could not be restored: %s" % k.changing)

    # 2b: what happens on a live resize
    def test_2_sizing(self):
        p = Prob(refresh=None)
        self.addCleanup(p.clean)
        p.write("x" * 100)
        e = self._session(p.setting, 120)
        time.sleep(2.5)

        def last_():
            k = p.records()
            return len(k), (k[-1]["COLUMNS"] if k else None)
        self.assertEqual(last_()[1], "120")                                          # COLUMNS == pty width at start
        n0 = last_()[0]
        e.resize(160)
        time.sleep(5)
        single = last_()[0] > n0
        e.resize(90)
        time.sleep(5)
        single = single or last_()[0] > n0
        print("\nresize alone re-ran the status line command: %s (COLUMNS seen: %s)" % (single, last_()[1]))
        print("stale 100-cell line after resize to 90, rows: %s" % [(s[1], s[2], s[0][-3:]) for _, s in e.status_lines()[1]])
        e.proc.write("\x1b[Z")                                                     # permission mode change = a documented trigger
        self.assertTrue(e.wait(lambda: last_()[0] > n0, 10), "no re-run after the mode change")
        self.assertEqual(last_()[1], "90", "after a re-run COLUMNS must be the new width")

    # 3: the margin Claude Code really leaves
    def test_3_measured_pay(self):
        pay = default_pay()
        for rn, env in RENDERERLER.items():
            for n in (WIDTHS if rn == "full-screen" else [x for x in WIDTHS if x in (120, 204)]):
                p = Prob(refresh=1)
                self.addCleanup(p.clean)
                e = Screen(p.setting, n, env_extra=env)
                self.addCleanup(e.off)
                e.wait_ready(90)
                l, upper = en_big_l(e, p, n)
                print("width %3d %-9s: largest whole line %3d cells -> margin %d (L+1 gives: %s) | wrapper default margin %d"
                      % (n, rn, l, n - l, [(s[1], s[2], s[0][-4:]) for _, s in upper], pay))
                self.assertEqual(n - l, pay, "measured margin != wrapper default QUOTA_LINE_PAY")
                e.off()
        p = Prob(padding=2, refresh=1)                                             # settings.json padding counts on both sides: 2 per unit
        self.addCleanup(p.clean)
        e = Screen(p.setting, 120, env_extra=self.ek_env)
        self.addCleanup(e.off)
        e.wait_ready(90)
        l, _ = en_big_l(e, p, 120)
        print("padding=2 at width 120: largest whole line %d cells -> margin %d" % (l, 120 - l))
        self.assertEqual(120 - l, pay + 4)

    # 4: end to end at 204 columns with the real wrapper: 1 line when the full line fits the measured limit, 2 lines when not
    def test_4_uctan_uca_204(self):
        n = 204
        limit = n - default_pay()
        with Guard() as k:
            g = Real()
            self.addCleanup(g.clean)
            full = g.direct(1000)
            self.assertEqual(len(full), 1, full)
            print("\nfull line (like a developer's): %d cells" % cell(full[0]))
            e = self._session(g.setting, n)
            rows = wait_status(e)
            print("\n== real line at %d columns, margin %d (limit %d): %d row(s)\n%s"
                  % (n, default_pay(), limit, len(rows), screen_summary([s for _, s in rows])))
            self.assertEqual(len(rows), 1 if cell(full[0]) <= limit else 2)
            e.off()
            short = os.path.expanduser("~")                                         # home folder: a short folder segment, so the line can be shrunk to the limit
            g.model("S", short)
            base = cell(g.direct(1000)[0])
            self.assertLessEqual(base, limit, "even the shortest sample line is longer than the limit")
            pin = {}
            for target, line_count in ((limit, 1), (limit + 1, 2)):
                long = 1 + target - base                                           # model name padded so the full line is exactly `target` cells
                for attempt in range(4):                                            # (the live forecast text can change a digit between runs)
                    g.model("S" * max(1, long), short)
                    n0 = len(g.records())
                    e = self._session(g.setting, n)
                    rows = wait_status(e)
                    output = g.records()[-1]["out"]
                    if len(g.records()) > n0 and cell(" | ".join(output.split("\n"))) == target:
                        break
                    long += target - cell(" | ".join(output.split("\n")))
                    e.off()
                else:
                    self.fail("could not pin the full line to %d cells" % target)
                pin[target] = long
                print("\n== full line %d cells at %d columns: %d row(s)\n%s" % (target, n, len(rows), screen_summary([s for _, s in rows])))
                self.assertEqual(len(rows), line_count)
                self.assertTrue(all("…" not in s[0] for _, s in rows))
                self.assertEqual(" | ".join(s[0].strip() for _, s in rows), " | ".join(output.split("\n")))    # nothing lost
                e.off()
            g.model("S" * pin[limit], short)                                        # the old default (10): this same 200-cell line wrapped although it fits
            e = self._session(g.setting, n, env={"QUOTA_LINE_PAY": "10"})
            rows = wait_status(e)
            print("\n== the %d-cell line with the old margin 10: %d row(s)\n%s" % (limit, len(rows), screen_summary([s for _, s in rows])))
            e.off()
        self.assertTrue(k.equal, "state files not restored: %s" % k.changing)


if __name__ == "__main__":
    unittest.main()

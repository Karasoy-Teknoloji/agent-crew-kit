"""Tests for last_window (the last-window wrapper): gate, quota readings, journal, shared log rows, the choice of the live session, and full
runs (assignment to a live session through a fake relay `claude`, and the tmux fallback through a fake `tmux`).

Run:  python3 -I -m unittest discover -s plugins/quota-pace/tests     (from the repo root)

Stdlib only, no network, no real claude or tmux: the relay is a small fake script (LAST_WINDOW_CLAUDE), tmux another (LAST_WINDOW_TMUX),
live sessions are fixture files in <config dir>/sessions; the shared repo a local bare repository (CLAUDE_SHARED_REMOTE), the picks a JSON file (LAST_WINDOW_CHOICE). Every process runs under a scratch HOME.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUG = os.path.normpath(os.path.join(HERE, ".."))
SP = os.path.join(PLUG, "bin", "last_window.py")
sys.path.insert(0, os.path.join(PLUG, "bin"))
import last_window as S  # noqa: E402

REAL_TAB = S.TabSession      # the real Windows tab class (the scene replaces it with a fake by default)
SESSION_CLASS = "TabSession" if os.name == "nt" else "TmuxSession"   # the class main() picks for its own session

H = 3600
NOW = 1_800_000_000


def rd(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def jrd(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def jwr(p, obj):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def git(*args, cwd=None):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


class Gate(unittest.TestCase):
    RESET = NOW + 3 * H            # reset in 3 h, window of 5 h: we are inside

    def quota(self, used=93, reset=None, **kw):
        return {"week_used": used, "week_resets_at": reset or self.RESET, "five_hour_used": None, "five_hour_resets_at": None, **kw}

    def test_inside_the_window_with_quota_left_starts(self):
        self.assertEqual(S.decision(NOW, self.quota(), 5, False, None)[:2], ("start", "in-window"))

    def test_before_the_window_after_the_reset_and_unknown_reset_wait(self):
        self.assertEqual(S.decision(NOW, self.quota(reset=NOW + 6 * H), 5, False, None)[1], "before-window")
        self.assertEqual(S.decision(NOW, self.quota(reset=NOW - 1), 5, False, None)[1], "after-reset")
        self.assertEqual(S.decision(NOW, {"week_resets_at": None}, 5, False, None)[1], "reset-unknown")
        self.assertEqual(S.decision(NOW, self.quota(), 0, False, None)[1], "window-missing")     # target hours 0 = no window

    def test_window_opens_exactly_at_reset_minus_target(self):
        self.assertEqual(S.decision(NOW, self.quota(reset=NOW + 5 * H), 5, False, None)[0], "start")
        self.assertEqual(S.decision(NOW - 1, self.quota(reset=NOW + 5 * H), 5, False, None)[1], "before-window")

    def test_used_up_weekly_limit_and_full_five_hour_window_wait(self):
        self.assertEqual(S.decision(NOW, self.quota(used=100), 5, False, None)[1], "quota-done")
        self.assertEqual(S.decision(NOW, self.quota(five_hour_used=100, five_hour_resets_at=NOW + H), 5, False, None)[1], "five-hour-full")
        self.assertEqual(S.decision(NOW, self.quota(used=None), 5, False, None)[0], "start")   # unknown use: the stream will tell

    def test_one_coordinator_and_at_most_one_start_per_15_minutes(self):
        self.assertEqual(S.decision(NOW, self.quota(), 5, True, None)[1], "running")
        self.assertEqual(S.decision(NOW, self.quota(), 5, False, NOW - 14 * 60)[1], "retry-interval")
        self.assertEqual(S.decision(NOW, self.quota(), 5, False, NOW - 15 * 60)[0], "start")

    def test_no_restart_in_the_last_ten_minutes_and_backoff_after_quick_failures(self):
        self.assertEqual(S.decision(NOW, self.quota(reset=NOW + 9 * 60), 5, False, NOW - 3600)[1], "reset-near")
        self.assertEqual(S.decision(NOW, self.quota(), 5, False, NOW - 20 * 60, fast_error=3)[1], "error-backoff")
        self.assertEqual(S.decision(NOW, self.quota(), 5, False, NOW - 3601, fast_error=3)[0], "start")


class Quota(unittest.TestCase):
    def test_newer_source_wins_and_use_is_trusted_only_for_the_chosen_reset(self):
        pace = {"week_used": 90, "week_resets_at": NOW + H, "updated": NOW - 5 * H}
        status = {"week_used": 97.5, "week_resets_at": NOW + H, "updated": NOW - 60}
        k = S.quota_status(pace, status, NOW)
        self.assertEqual((k["week_used"], k["source"]), (97.5, "status"))

    def test_stale_reset_is_rolled_forward_by_whole_weeks_with_unknown_use(self):
        k = S.quota_status({"week_used": 50, "week_resets_at": NOW - 8 * 86400, "updated": NOW - 9 * 86400}, {}, NOW)
        self.assertIsNone(k["week_used"])
        self.assertEqual(k["source"], "forecast")
        self.assertTrue(NOW < k["week_resets_at"] <= NOW + 7 * 86400)
        self.assertEqual((k["week_resets_at"] - (NOW - 8 * 86400)) % (7 * 86400), 0)

    def test_nothing_known(self):
        self.assertIsNone(S.quota_status({}, {}, NOW)["week_resets_at"])

    def test_five_hour_reading_expires_with_its_reset(self):
        k = S.quota_status({}, {"week_resets_at": NOW + H, "updated": NOW, "five_hour_used": 100, "five_hour_resets_at": NOW - 1}, NOW)
        self.assertIsNone(k["five_hour_used"])


class Lines(unittest.TestCase):
    def record(self, begin_t, **kw):
        k = {"begin": begin_t, "end": begin_t + 600, "week_begin": 93, "week_last": 100, "reason": "limit", "cost": 41.7,
             "finished": [("https://github.com/example-org/product-a/issues/61", "x | y"), ("https://example.com/not-github", "z")],
             "resume": [("https://github.com/example-org/product-c/issues/7", "wip")]}
        k.update(kw)
        return k

    def test_row_roundtrip_and_links_only_for_issue_or_pr_urls(self):
        r = S.line_parse(S.build_line(self.record(NOW)))
        self.assertEqual((r["Week %"], r["Reason"], r["Done"], r["WIP"], r["API $"]), ("93→100", "limit", "2", "1", "42"))
        self.assertEqual(r["Items"], "[product-a#61](https://github.com/example-org/product-a/issues/61)")

    def test_running_row_is_replaced_by_the_final_one_and_other_rows_stay(self):
        a = S.person_file("Ali", "", {"begin": NOW, "week_begin": 93, "reason": None, "finished": [], "resume": []})
        self.assertIn("running", a)
        b = S.person_file("Ali", a, self.record(NOW + 86400))
        c = S.person_file("Ali", b, self.record(NOW, reason="reset"))
        rows = S.file_lines(c)
        self.assertEqual([r["Reason"] for r in rows], ["reset", "limit"])
        self.assertEqual(len(rows), 2)

    def test_pipe_in_text_never_breaks_the_table(self):
        self.assertEqual(S.cell("a | b\nc"), "a / b c")

    def test_noise_is_not_a_row(self):
        for s in ("", "| Start | End |", "|---|---|", "text", "| 2026-13-45 | x |"):
            self.assertIsNone(S.line_parse(s))

    def test_all_people_table_is_newest_first(self):
        data = {"Ali": S.file_lines(S.person_file("Ali", "", self.record(NOW))),
                "Parent": S.file_lines(S.person_file("Parent", "", self.record(NOW + 86400)))}
        m = S.records_text(data)
        self.assertLess(m.index("Parent"), m.index("Ali"))


class Choice(unittest.TestCase):
    def test_board_picks_skip_foreign_assignees_and_prod_approval_and_sort_by_priority(self):
        o = [{"repo": "O/A", "number": 5, "url": "u5", "title": "t", "state": "open", "labels": [], "assignees": [], "rank": 2},
             {"repo": "O/A", "number": 6, "url": "u6", "title": "t", "state": "open", "labels": ["prod-approval-waiting"], "assignees": [], "rank": 0},
             {"repo": "O/B", "number": 1, "url": "u1", "title": "t", "state": "open", "labels": [], "assignees": ["other"], "rank": 0},
             {"repo": "O/B", "number": 2, "url": "u2", "title": "t", "state": "open", "labels": [], "assignees": ["Me"], "rank": 1},
             {"repo": "O/B", "number": 3, "url": "u3", "title": "t", "state": "closed", "labels": [], "assignees": [], "rank": 0},
             {"repo": "O/C", "number": 9, "url": "u9", "title": "t", "state": "open", "labels": [], "assignees": [], "rank": 99},
             {"repo": "O/C", "number": 4, "url": "u4", "title": "t", "state": "open", "labels": [], "assignees": [], "rank": 1}]
        b = S.pick_b("me", o, excluded={"u4"})
        self.assertEqual([i["number"] for i in b], [2, 5, 9])
        self.assertEqual(b[0]["repo"], "B")

    def test_journal_lines_and_short_refs(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "j.md")
            S.add_journal(p, "## old\n- [10:00] DONE https://github.com/O/R/issues/1 | old\n")
            offset = os.path.getsize(p)
            S.add_journal(p, "- [11:00] DONE https://github.com/O/R/issues/2 | fixed | with pipe\n- [11:05] WIP https://github.com/O/R/pull/3 | branch x\n"
                             "- [11:06] SKIP https://github.com/O/R/issues/4 | needs decision\n- [11:07] NOTE hand-off text\nrandom line\n")
            items = S.journal_items(p, offset)
            self.assertEqual([(k, S.short_ref(r)) for k, r, _ in items], [("DONE", "R#2"), ("WIP", "R#3"), ("SKIP", "R#4"), ("NOTE", None)])
            self.assertEqual(items[0][2], "fixed | with pipe")
        finally:
            shutil.rmtree(d, ignore_errors=True)




def session_json(pid, name, sid, cwd, started, status="idle", entry="cli"):
    return {"pid": pid, "sessionId": sid, "cwd": cwd, "startedAt": started * 1000, "kind": "interactive", "entrypoint": entry, "name": name, "status": status}


def assistant(model, mid="m1"):
    return json.dumps({"type": "assistant", "message": {"id": mid, "model": model, "content": []}}) + "\n"


def limit_line(t, text="You've hit your session limit · resets 6:30pm (Europe/Istanbul)", uuid="dur-1"):
    """The synthetic entry Claude Code writes when a usage limit stops a turn (fields as in a real transcript of 16.10.2026)."""
    return json.dumps({"type": "assistant", "uuid": uuid, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t)),
                       "error": "rate_limit", "isApiErrorMessage": True, "apiErrorStatus": 429,
                       "message": {"id": "x", "model": "<synthetic>", "content": [{"type": "text", "text": text}]}}) + "\n"


class Sessions(unittest.TestCase):
    """The choice of the live session: registry + transcript, in a scratch config dir (S.CLAUDE patched)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sp-session-")
        self.old = (S.CLAUDE, S.DIR)
        S.CLAUDE, S.DIR = os.path.join(self.root, ".claude"), os.path.join(self.root, ".claude", "last-window")
        os.makedirs(os.path.join(S.CLAUDE, "sessions"))
        self.live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])   # a live pid to register
        self.n = 0

    def tearDown(self):
        self.live.kill()
        self.live.wait()
        S.CLAUDE, S.DIR = self.old
        shutil.rmtree(self.root, ignore_errors=True)

    def add(self, name, model, started, pid=None, entry="cli", status="idle", cwd="/home/u/work"):
        self.n += 1
        sid = "00000000-0000-0000-0000-%012d" % self.n
        pid = pid or self.live.pid
        jwr(os.path.join(S.CLAUDE, "sessions", "%d-%d.json" % (pid, self.n)), session_json(pid, name, sid, cwd, started, status, entry))
        if model:
            path = S.transcript_path(cwd, sid)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("garbage line\n" + assistant("claude-haiku-4-5", "x") + assistant(model, "y"))   # the LAST assistant entry decides
        return sid

    def sort(self, **kw):
        candidate, dropped = S.sort_candidates(S.read_sessions(), **kw)
        return [o["name"] for o in candidate], {o["name"]: n for o, n in dropped}

    def test_model_scores_and_versions(self):
        self.assertEqual(S.model_score("claude-opus-5-5"), (58, (5, 5)))
        self.assertEqual(S.model_score("claude-opus-4-8"), (58, (4, 8)))
        self.assertEqual(S.model_score("claude-sonnet-5-5[1m]"), (54, (5, 5)))
        self.assertEqual(S.model_score("claude-haiku-4-5-20251001"), (40, (4, 5)))
        self.assertEqual(S.model_score("claude-fable-5-1")[0], 49)
        self.assertEqual(S.model_score(None), (-1, ()))

    def test_transcript_path_and_last_model(self):
        self.assertEqual(S.transcript_path("/home/u/a.b", "ID"), os.path.join(S.CLAUDE, "projects", "-home-u-a-b", "ID.jsonl"))
        self.add("A", "claude-sonnet-5-5", 100)
        self.assertEqual(S.read_sessions()[0]["model"], "claude-sonnet-5-5")

    def test_best_model_first_then_newer_version_then_oldest(self):
        self.add("old-sonnet", "claude-sonnet-5-5", 10)
        self.add("new-opus", "claude-opus-5-5", 300)
        self.add("older-opus-version", "claude-opus-4-8", 5)
        self.add("old-opus", "claude-opus-5-5", 200)
        self.add("fable", "claude-fable-5-1", 1)
        self.add("no-transcript", None, 1)
        candidates, _ = self.sort()
        self.assertEqual(candidates, ["old-opus", "new-opus", "older-opus-version", "old-sonnet", "fable", "no-transcript"])

    def test_headless_last_window_excluded_dead_and_shared_names_are_out(self):
        self.add("terminal", "claude-opus-5-5", 100)
        self.add("headless", "claude-opus-5-5", 1, entry="sdk-cli")
        self.add("last-window-emre", "claude-opus-5-5", 1)
        self.add("last-window-test", "claude-opus-5-5", 1)
        self.add("dup", "claude-opus-5-5", 1)
        self.add("dup", "claude-opus-5-5", 2)
        self.add("", "claude-opus-5-5", 1)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.add("dead", "claude-opus-5-5", 1, pid=dead.pid)
        candidates, dropped = self.sort()
        self.assertEqual(candidates, ["terminal"])
        self.assertIn("sdk-cli", dropped["headless"])
        self.assertIn("own session", dropped["last-window-emre"])
        self.assertIn("shared", dropped["dup"])
        self.assertNotIn("dead", dropped)                        # a dead process is not a session at all

    def test_a_newer_model_version_ranks_first_even_when_shorter_ids_compare(self):
        self.add("opus-5", "claude-opus-5", 1)
        self.add("opus-5-5", "claude-opus-5-5", 2)
        self.add("opus-4-8", "claude-opus-4-8", 0)
        self.assertEqual(self.sort()[0], ["opus-5-5", "opus-5", "opus-4-8"])

    @unittest.skipUnless(os.path.exists("/proc/self/stat"), "procStart is the Linux /proc start time")
    def test_a_leftover_entry_with_a_reused_pid_does_not_shadow_the_live_session(self):
        self.add("emre-07", "claude-opus-5-5", 1)                       # the live session (no procStart: not compared)
        sid = "55555555-5555-5555-5555-555555555555"
        d = session_json(self.live.pid, "emre-07", sid, "/home/u/work", 0)
        d["procStart"] = "1"                                               # written by an older process with the same pid number
        jwr(os.path.join(S.CLAUDE, "sessions", "leftover.json"), d)
        candidates, dropped = self.sort()
        self.assertEqual(candidates, ["emre-07"])
        self.assertEqual(dropped, {})
        d["procStart"] = S.process_begin(self.live.pid)                # the same process: valid
        self.assertTrue(S.record_valid(d))

    def test_a_lock_is_live_for_the_whole_longest_window(self):
        os.makedirs(S.DIR, exist_ok=True)
        old = S.LOCK
        S.LOCK = os.path.join(S.DIR, "coordinator.lock")
        try:
            jwr(S.LOCK, {"pid": self.live.pid, "t": NOW - 50 * H})       # a window run, 50 h in (the cap is 72 h by default, 96 h at most: stale only after 97 h)
            self.assertTrue(S.lock_live(NOW))
            jwr(S.LOCK, {"pid": self.live.pid, "t": NOW - 100 * H})
            self.assertFalse(S.lock_live(NOW))
        finally:
            S.LOCK = old

    def test_user_exclusions_by_pattern_and_the_test_hook(self):
        self.add("emre-07", "claude-opus-5-5", 1)
        self.add("worker", "claude-opus-5-5", 2)
        self.assertEqual(self.sort(excluding=["emre-*"])[0], ["worker"])
        os.makedirs(S.DIR)
        with open(os.path.join(S.DIR, "excluding.txt"), "w") as f:
            f.write("# the coordinator\nemre-07\n")
        self.assertEqual(S.excluding_patterns(), ["emre-07"])
        self.add("last-window-test-x", "claude-opus-5-5", 3)
        self.assertEqual(self.sort(target="last-window-test-x")[0], ["last-window-test-x"])


FAKE_ROLE = r'''
import json, os, re, subprocess, sys, time
inp = sys.stdin.read()
m = re.search(r"<message>\n(.*)\n</message>", inp, re.S)
name = re.search(r"whose name is exactly '([^']*)'", inp).group(1)
mark = os.environ["FAKE_MARK"]
before = sum(1 for _ in open(mark)) if os.path.exists(mark) else 0
open(mark, "a").write(json.dumps({"to": name, "text": m.group(1)}) + "\n")
order = [x for x in os.environ.get("FAKE_ORDER", "").split(",") if x]     # one mode per relay call; the last one repeats
mod = order[min(before, len(order) - 1)] if order else os.environ.get("FAKE_MODE", "ok")
path = os.environ.get("FAKE_TRANSCRIPT")
line = json.dumps({"type": "user", "message": "<cross-session-message from=x>" + m.group(1) + "</cross-session-message>"}) + "\n"
line += json.dumps({"type": "assistant", "message": {"id": "r%d" % os.getpid(), "model": "claude-opus-5-5"}}) + "\n"
target_ok = path and name == os.environ.get("FAKE_NAME", name)
if mod == "time":                       # the relay hangs past its timeout
    time.sleep(60)
elif mod == "silent":                    # exits cleanly without an answer word
    print("I could not decide.")
elif mod == "error":                      # an error, no answer word
    print("API Error: 500", file=sys.stderr)
    sys.exit(1)
elif mod in ("ok", "late", "unmarked") and target_ok:
    if mod == "ok":
        with open(path, "a") as f:
            f.write(line)
    elif mod == "late":                   # a busy target: the message shows up only at the end of its turn
        subprocess.Popen([sys.executable, "-c", "import sys, time; time.sleep(float(sys.argv[1])); open(sys.argv[2], 'a').write(sys.argv[3])",
                          os.environ.get("FAKE_DELAY", "3"), path, line], start_new_session=True)
    print("**HANDOFF**" if os.environ.get("FAKE_MARKDOWN") else "HANDOFF")
else:
    print("MISSING")
'''

FAKE_TMUX = r'''
import json, os, sys
a = sys.argv[1:]
sock = None
if a[:1] == ["-L"]:
    sock, a = a[1], a[2:]
state = os.environ["FAKE_TMUX_STATE"]
os.makedirs(state, exist_ok=True)
open(os.path.join(state, "log"), "a").write(json.dumps({"L": sock, "a": a}) + "\n")
def name():
    return a[a.index("-t") + 1].lstrip("=").rstrip(":") if "-t" in a else a[a.index("-s") + 1]
def fpath(n):
    return os.path.join(state, n)
def register():
    # the started claude registers itself (FAKE_TMUX_RECORD = "<sessions dir>:<live pid>"), unless FAKE_TMUX_UNREGISTERED=1
    reg = os.environ.get("FAKE_TMUX_RECORD")
    if not reg or os.environ.get("FAKE_TMUX_UNREGISTERED") == "1":
        return
    d, pid = reg.rsplit(":", 1)
    k = json.load(open(fpath("session")))
    json.dump({"pid": int(pid), "sessionId": k["sid"], "cwd": k["cwd"], "startedAt": 0, "kind": "interactive", "entrypoint": "cli",
               "name": k["name"], "status": "busy"}, open(os.path.join(d, "%s.json" % pid), "w"))
screen = os.environ.get("FAKE_TMUX_SCREEN")       # a dialog on screen (capture-pane); the trust dialog moves with Down, closes with Enter
if a[0] == "new-session":
    open(fpath("s-" + name()), "w").write(a[-1])
    k = a[-1].split()
    json.dump({"name": name(), "cwd": a[a.index("-c") + 1], "sid": k[k.index("--session-id") + 1]}, open(fpath("session"), "w"))
    if not screen:
        register()
elif a[0] == "has-session":
    sys.exit(0 if os.path.exists(fpath("s-" + name())) else 1)
elif a[0] == "kill-session":
    try: os.remove(fpath("s-" + name()))
    except OSError: pass
    reg = os.environ.get("FAKE_TMUX_RECORD")
    if reg:
        try: os.remove(os.path.join(reg.rsplit(":", 1)[0], "%s.json" % reg.rsplit(":", 1)[1]))
        except OSError: pass
elif a[0] == "send-keys" and screen and not os.path.exists(fpath("closed")):
    if a[-1] == "Down":
        open(fpath("down"), "w").close()
    elif a[-1] == "Enter" and os.path.exists(fpath("down")) and "I trust this folder" in open(screen, encoding="utf-8").read():
        open(fpath("closed"), "w").close()
        register()
elif a[0] == "capture-pane" and screen and not os.path.exists(fpath("closed")):
    text = open(screen, encoding="utf-8").read()
    if os.path.exists(fpath("down")):
        text = text.replace(" ❯ No, exit", "   No, exit").replace("   Yes, I trust", " ❯ Yes, I trust")
    sys.stdout.write(text)
'''


def tmux_calls(status):
    # [args] of every fake tmux call, without the -L socket (asserted separately)
    return [json.loads(s)["a"] for s in rd(os.path.join(status, "log")).splitlines()]


@unittest.skipUnless(shutil.which("git") and os.name != "nt", "needs git and a POSIX shell (the fakes are executable scripts)")
class Working(unittest.TestCase):
    """last_window.py trigger / status end to end under a scratch HOME."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="sp-")
        cls.seed = os.path.join(cls.root, "seed.git")
        git("init", "-q", "--bare", "-b", "main", cls.seed)
        seed = os.path.join(cls.root, "seed")
        git("clone", "-q", cls.seed, seed)
        os.makedirs(os.path.join(seed, "plugins", "quota-pace"))
        shutil.copy(os.path.join(PLUG, "tests", "fixtures", "people-example.json"), os.path.join(seed, "plugins", "quota-pace", "people.json"))
        git("add", "-A", cwd=seed)
        git("commit", "-q", "-m", "seed", cwd=seed)
        git("push", "-q", "origin", "HEAD:main", cwd=seed)
        cls.fake = os.path.join(cls.root, "fake_claude.py")
        cls.tmux = os.path.join(cls.root, "fake_tmux.py")
        for path, code in ((cls.fake, FAKE_ROLE), (cls.tmux, FAKE_TMUX)):
            with open(path, "w") as f:
                f.write("#!%s\n%s" % (sys.executable, code))
            os.chmod(path, 0o755)
        cls.choice = os.path.join(cls.root, "choice.json")
        with open(cls.choice, "w") as f:
            json.dump({"login": "tester", "a": [], "errors": [], "c": {"red_ci": [], "dependabot": []},
                       "b": [{"repo": "product-a", "number": 61, "title": "Security list", "url": "https://github.com/example-org/product-a/issues/61",
                              "state": "open", "labels": [], "assignees": [], "priority": "urgent", "rank": 0}]}, f)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="home-", dir=self.root)
        self.claude = os.path.join(self.home, ".claude")
        os.makedirs(os.path.join(self.claude, "sessions"))
        self.mark = os.path.join(self.home, "mark")
        self.tstate = os.path.join(self.home, "tmux")
        self.clone = os.path.join(self.home, "clone")
        self.remote = os.path.join(self.home, "remote.git")                # a fresh copy of the seeded shared repo for every test
        git("clone", "-q", "--bare", self.seed, self.remote)
        self.live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        self.addCleanup(lambda: (self.live.kill(), self.live.wait()))
        self.live2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])   # the pid the fallback claude registers with
        self.addCleanup(lambda: (self.live2.kill(), self.live2.wait()))

    def pace(self, reset_in, used=93, **kw):
        d = {"week_used": used, "week_resets_at": int(time.time() + reset_in), "updated": int(time.time()), "target_hours": 5}
        d.update(kw)
        jwr(os.path.join(self.claude, "pace.json"), d)

    def session(self, name="Alpha", model="claude-opus-5-5", status="idle"):
        """A live interactive session; returns its transcript path."""
        sid, cwd = "11111111-1111-1111-1111-111111111111", "/home/u/w"
        jwr(os.path.join(self.claude, "sessions", "%d.json" % self.live.pid), session_json(self.live.pid, name, sid, cwd, int(time.time()) - 3600, status))
        path = os.path.join(self.claude, "projects", "-home-u-w", sid + ".jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(assistant(model))
        return path

    def run_sp(self, *args, mode="ok", **extra):
        return subprocess.run([sys.executable, "-I", SP, *args], capture_output=True, text=True, encoding="utf-8", env=self.env_(mode, **extra), timeout=120)

    def env_(self, mode="ok", **extra):
        env = {k: v for k, v in os.environ.items() if k not in ("PACE_TARGET_HOURS", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "LAST_WINDOW_DIR", "LAST_WINDOW_EXCLUDING",
                                                                 "LAST_WINDOW", "LAST_WINDOW_GH_LOGIN")}
        env.update(HOME=self.home, USERPROFILE=self.home, CLAUDE_CONFIG_DIR=self.claude, LAST_WINDOW_CLAUDE=self.fake, LAST_WINDOW_TMUX=self.tmux,
                   LAST_WINDOW_CHOICE=self.choice, LAST_WINDOW_POLL="0.2", LAST_WINDOW_ACK_SEC="5", LAST_WINDOW_STOP_SEC="1", LAST_WINDOW_RECORD_SEC="5",
                   FAKE_TMUX_RECORD="%s:%d" % (os.path.join(self.claude, "sessions"), self.live2.pid), FAKE_MODE=mode, FAKE_MARK=self.mark,
                   FAKE_TMUX_STATE=self.tstate, CLAUDE_SHARED_REMOTE=self.remote, CLAUDE_SHARED_LAST_WINDOW_CLONE=self.clone, CLAUDE_SHARED_PERSON="Emre",
                   CLAUDE_SHARED_ROOT=self.home, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        env.update(extra)
        return env

    def sent(self):
        if not os.path.exists(self.mark):
            return []
        with open(self.mark, encoding="utf-8") as f:
            return [json.loads(s) for s in f]

    def brief(self):
        d = os.path.join(self.claude, "last-window", "runs")
        return rd(os.path.join(d, [f for f in os.listdir(d) if f.endswith("-brief.md")][0]))

    def shared(self):
        v = os.path.join(self.root, "verify-" + os.path.basename(self.home))
        git("clone", "-q", self.remote, v)
        try:
            p = os.path.join(v, "reports", "last-window", "emre.md")
            return rd(p) if os.path.exists(p) else ""
        finally:
            shutil.rmtree(v, ignore_errors=True)

    def test_the_assignment_goes_to_the_best_live_session_and_the_reset_message_ends_the_run(self):
        path = self.session("Alpha")
        self.pace(5)                                                   # reset in 5 s
        t = time.time()
        r = self.run_sp("trigger", PACE_TARGET_HOURS="5", FAKE_TRANSCRIPT=path)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertLess(time.time() - t, 60)
        sent_ = self.sent()
        self.assertEqual([g["to"] for g in sent_], ["Alpha", "Alpha"])
        self.assertIn("last-window on", sent_[0]["text"])        # who sends it
        self.assertIn("-brief.md", sent_[0]["text"])             # the brief is a file
        self.assertIn("#ai-status", sent_[0]["text"])     # the Slack rule travels with the message
        self.assertIn("Reset reached", sent_[1]["text"])         # the cutoff message
        b = self.brief()
        self.assertIn("product-a/issues/61", b)                         # the pick is in the brief
        self.assertNotIn("{{", b)                                      # every placeholder was filled
        self.assertIn("EXCLUDED", b)
        self.assertIn("started", b)
        self.assertIn("go idle (do not exit", b)                       # an assigned session is the owner's: never told to exit
        rows = S.file_lines(self.shared())
        self.assertEqual(len(rows), 1)                                 # the "running" row was replaced, not duplicated
        self.assertEqual(rows[0]["Reason"], "reset")
        self.assertIn("session:Alpha", rows[0]["Items"])
        self.assertFalse(os.path.exists(os.path.join(self.claude, "last-window", "coordinator.lock")))

    def test_a_candidate_that_cannot_be_reached_falls_back_to_the_visible_session(self):
        path = self.session("Alpha")
        self.pace(8)
        r = self.run_sp("trigger", PACE_TARGET_HOURS="5", FAKE_TRANSCRIPT=path, FAKE_NAME="Nobody", LAST_WINDOW_ACK_SEC="1")   # the fake relay delivers only to 'Nobody'
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # the fake answers MISSING for Alpha: no candidate took it -> the visible fallback session was started instead
        log = rd(os.path.join(self.tstate, "log"))
        self.assertIn("new-session", log)
        self.assertIn("last-window-emre", log)

    def test_without_a_live_session_the_visible_tmux_fallback_starts_and_is_cut_off_at_the_reset(self):
        self.pace(6)
        t = time.time()
        r = self.run_sp("trigger", PACE_TARGET_HOURS="5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertLess(time.time() - t, 60)
        self.assertEqual(self.sent(), [])                              # nobody to relay to
        calls = tmux_calls(self.tstate)
        sockets = {json.loads(s)["L"] for s in rd(os.path.join(self.tstate, "log")).splitlines()}
        self.assertEqual(sockets, {"last-window"})                   # every call goes to last-window's own tmux server, never the user's
        new = [c for c in calls if c[0] == "new-session"][0]
        self.assertEqual(new[new.index("-s") + 1], "last-window-emre")
        self.assertIn("--name last-window-emre", new[-1])
        self.assertIn("--permission-mode bypassPermissions", new[-1])
        self.assertIn("-brief.md", new[-1])
        self.assertIn("--remote-control last-window-emre", new[-1])   # visible from claude.ai/code and the app
        self.assertIn("LAST_WINDOW_JOURNAL=", new[-1])
        self.assertTrue(any(c[0] == "send-keys" and "/exit" in c for c in calls))   # /exit first ...
        self.assertTrue(any(c[0] == "kill-session" for c in calls[calls.index(new):]))   # ... then the kill
        self.assertFalse(os.path.exists(os.path.join(self.tstate, "s-last-window-emre")))
        self.assertIn("exit (`/exit`)", self.brief())                  # our own session may be told to exit
        rows = S.file_lines(self.shared())
        self.assertEqual((rows[0]["Reason"], "session:last-window-emre" in rows[0]["Items"]), ("reset", True))

    def test_outside_the_window_or_with_the_limit_used_nothing_starts(self):
        self.session()
        self.pace(8 * H)
        self.assertIn("before-window", self.run_sp("trigger").stdout)
        self.pace(3 * H, used=100)
        self.assertIn("quota-done", self.run_sp("trigger").stdout)
        self.assertEqual(self.sent(), [])
        self.assertFalse(os.path.exists(self.tstate))

    def test_dry_run_prints_the_ranking_and_sends_and_starts_nothing(self):
        self.session("Alpha")
        self.pace(3 * H)
        r = self.run_sp("trigger", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("(b) board", r.stdout)
        self.assertIn("product-a#61", r.stdout)
        self.assertRegex(r.stdout, r"->\s+Alpha ")
        self.assertIn("DRY RUN: would send the assignment to 'Alpha'", r.stdout)
        self.assertEqual(self.sent(), [])
        self.assertFalse(os.path.exists(self.tstate))
        self.assertFalse(os.path.exists(os.path.join(self.claude, "last-window", "coordinator.lock")))
        self.assertEqual(self.shared(), "")
        r = self.run_sp("session")
        self.assertIn("Assignment goes to: Alpha", r.stdout)

    def test_sigterm_records_the_run_and_frees_the_lock_without_touching_the_assigned_session(self):
        path = self.session("Alpha")
        self.pace(3 * H)
        env = {k: v for k, v in os.environ.items() if k not in ("PACE_TARGET_HOURS", "XDG_CONFIG_HOME")}
        env.update(HOME=self.home, USERPROFILE=self.home, CLAUDE_CONFIG_DIR=self.claude, LAST_WINDOW_CLAUDE=self.fake, LAST_WINDOW_TMUX=self.tmux, LAST_WINDOW_CHOICE=self.choice,
                   LAST_WINDOW_POLL="0.2", LAST_WINDOW_ACK_SEC="5", FAKE_MARK=self.mark, FAKE_TRANSCRIPT=path, FAKE_TMUX_STATE=self.tstate,
                   CLAUDE_SHARED_REMOTE=self.remote, CLAUDE_SHARED_LAST_WINDOW_CLONE=self.clone, CLAUDE_SHARED_PERSON="Emre",
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        p = subprocess.Popen([sys.executable, "-I", SP, "trigger"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for _ in range(150):                                       # wait until the assignment was delivered
                if len(self.sent()) >= 1:
                    break
                time.sleep(0.1)
            self.assertEqual(len(self.sent()), 1)
            time.sleep(1.5)
            p.terminate()
            p.communicate(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertEqual(p.returncode, 0)
        self.assertEqual(S.file_lines(self.shared())[0]["Reason"], "stopped")
        self.assertFalse(os.path.exists(os.path.join(self.claude, "last-window", "coordinator.lock")))
        self.assertEqual(len(self.sent()), 1)                          # no stop message, the owner's session keeps working
        self.assertEqual(self.live.poll(), None)

    def test_a_live_lock_blocks_and_a_dead_one_is_taken_over(self):
        self.pace(3 * H)
        os.makedirs(os.path.join(self.claude, "last-window"))
        lock = os.path.join(self.claude, "last-window", "coordinator.lock")
        jwr(lock, {"pid": os.getpid(), "t": int(time.time())})   # this test process is alive
        self.assertIn("running", self.run_sp("trigger").stdout)
        self.assertFalse(os.path.exists(self.tstate))
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        jwr(lock, {"pid": p.pid, "t": int(time.time())})          # a dead pid
        self.pace(6)
        self.run_sp("trigger", PACE_TARGET_HOURS="5")
        self.assertTrue(os.path.exists(os.path.join(self.tstate, "log")))   # it started (the fallback, no live session here)

    def test_status_shows_the_session_of_a_running_assignment(self):
        self.pace(3 * H)
        os.makedirs(os.path.join(self.claude, "last-window"))
        jwr(os.path.join(self.claude, "last-window", "coordinator.lock"), {"pid": os.getpid(), "t": int(time.time()), "session": "Alpha", "via": "assignment"})
        self.assertIn("running now in session 'Alpha' (assigned live session)", self.run_sp("status", "--local").stdout)

    def test_the_fallback_test_never_relays_to_a_live_session(self):
        self.session("Alpha")                                           # a live session that WOULD win a real run
        r = self.run_sp("attempt", "--tmux", "--reset-sec", "4")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.sent(), [])                              # nothing was relayed to Alpha
        self.assertIn("new-session", rd(os.path.join(self.tstate, "log")))
        self.assertIn("reason=reset session=last-window-test mode=tmux", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.claude, "last-window", "coordinator.lock")))   # scratch state only
        self.assertEqual(self.shared(), "")                            # no shared-log row

    def log(self):
        return rd(os.path.join(self.claude, "last-window", "last-window.log"))

    def test_the_folder_trust_dialog_is_answered_and_the_session_is_then_followed(self):
        self.pace(6)
        r = self.run_sp("trigger", PACE_TARGET_HOURS="5", FAKE_TMUX_SCREEN=os.path.join(HERE, "fixtures", "trust-screen-2.1.316.txt"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        keys = [c[-1] for c in tmux_calls(self.tstate) if c[0] == "send-keys"]
        self.assertEqual(keys[:2], ["Down", "Enter"])          # the captured 2.1.316 dialog starts on "No, exit"
        self.assertIn("answered the folder-trust dialog", self.log())
        self.assertEqual(S.file_lines(self.shared())[0]["Reason"], "reset")

    def test_a_session_that_never_registers_is_logged_killed_and_recorded_as_a_quick_failure(self):
        self.pace(60)
        t = time.time()
        r = self.run_sp("trigger", PACE_TARGET_HOURS="5", LAST_WINDOW_RECORD_SEC="2", FAKE_TMUX_UNREGISTERED="1",
                        FAKE_TMUX_SCREEN=os.path.join(HERE, "fixtures", "bypass-screen.txt"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertLess(time.time() - t, 45)                     # it did not wait for the reset
        calls = tmux_calls(self.tstate)
        self.assertEqual([c for c in calls if c[0] == "send-keys"], [])   # the bypass warning is never answered, not even by the cutoff
        new = [i for i, c in enumerate(calls) if c[0] == "new-session"][0]
        self.assertTrue(any(c[0] == "kill-session" for c in calls[new:]))
        log = self.log()
        self.assertIn("bypass-permissions warning: not answered", log)
        self.assertRegex(log, r"\|\s+WARNING: Claude Code running in Bypass Permissions mode")   # the pane is in the log
        self.assertEqual(S.file_lines(self.shared())[0]["Reason"], "error")
        self.assertEqual(jrd(os.path.join(self.claude, "last-window", "status.json"))["fast_error"], 1)

    def test_pythonw_without_stdout_and_stderr_still_runs_the_gate(self):
        self.pace(8 * H)
        code = ("import runpy, sys; sys.stdout = None; sys.stderr = None; sys.argv = ['last-window', 'trigger']; "
               "runpy.run_path(%r, run_name='__main__')" % SP)
        env = dict(os.environ, HOME=self.home, USERPROFILE=self.home, CLAUDE_CONFIG_DIR=self.claude, PACE_TARGET_HOURS="5")
        env.pop("LAST_WINDOW_DIR", None)
        r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("before-window", self.log())              # the tick ran and logged its decision

    # ---- resume alarm and burn mode
    def halt(self, path, t, text="You've hit your session limit · resets 6:30pm (Europe/Istanbul)", uuid="dur-1"):
        """Append the synthetic 429 entry Claude Code writes when the limit stops a session (shape copied from a real transcript)."""
        with open(path, "a", encoding="utf-8") as f:
            f.write(limit_line(t, text, uuid))

    def write_burn(self, **kw):
        d = {"active": True, "opener": "Onur Demir", "opener_login": "example-dev-2", "opening": int(time.time()) - 60}
        d.update(kw)
        os.makedirs(os.path.join(self.claude, "last-window"), exist_ok=True)
        jwr(os.path.join(self.claude, "last-window", "burn.json"), d)

    def read_burn(self):
        p = os.path.join(self.claude, "last-window", "burn.json")
        return jrd(p) if os.path.exists(p) else None

    def test_resume_alarm_relays_once_per_reset_to_the_session_the_limit_stopped(self):
        path = self.session("Alpha")
        now = time.time()
        self.halt(path, now - 2 * H)
        self.pace(3 * 86400, used=60, five_hour_used=100, five_hour_resets_at=int(now - 11 * 60))   # 5 h used up, reset 11 min ago
        r = self.run_sp("trigger", "--modes", "resume")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([g["to"] for g in self.sent()], ["Alpha"])
        self.assertIn("resume alarm", self.sent()[0]["text"])
        self.assertIn("pick nothing new", self.sent()[0]["text"])                  # normal mode: only already-started work
        r = self.run_sp("trigger", "--modes", "resume")                                 # the next tick: the same reset is not woken twice
        self.assertIn("resume-missing", r.stdout)
        self.assertEqual(len(self.sent()), 1)
        self.assertFalse(os.path.exists(self.tstate))                               # no new session is ever started for a resume
        res_state = jrd(os.path.join(self.claude, "last-window", "resume.json"))
        self.assertEqual((res_state["key"], res_state["warned"]), (int(now - 11 * 60), ["dur-1"]))

    def test_resume_alarm_after_the_new_window_shows_and_while_a_coordinator_runs(self):
        # review finding: auto-continued sessions already write the NEW window (5 h at 3%) before the alarm's grace is over, and a
        # last-window or burn-mode run may hold the coordinator lock; the session that was stopped with Esc must still be resumed
        path = self.session("Alpha")
        now = time.time()
        self.halt(path, now - 4 * H)                                               # stopped in the previous window
        self.pace(3 * 86400, used=60, five_hour_used=3, five_hour_resets_at=int(now + 4 * H))   # new window started 1 h ago
        os.makedirs(os.path.join(self.claude, "last-window"), exist_ok=True)
        jwr(os.path.join(self.claude, "last-window", "coordinator.lock"), {"pid": self.live.pid, "t": int(now)})   # a live run
        r = self.run_sp("trigger", "--modes", "resume")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([g["to"] for g in self.sent()], ["Alpha"])
        self.assertTrue(os.path.exists(os.path.join(self.claude, "last-window", "coordinator.lock")))   # the run's lock is left alone
        self.assertFalse(os.path.exists(os.path.join(self.claude, "last-window", "resume.lock")))
        r = self.run_sp("burn", "--on", LAST_WINDOW_GH_LOGIN="example-dev-2")              # no switching on while a run is live
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIsNone(self.read_burn())

    def test_resume_alarm_waits_for_the_reset_and_claude_codes_own_auto_continue(self):
        path = self.session("Alpha")
        now = time.time()
        self.halt(path, now - H)
        self.pace(3 * 86400, used=60, five_hour_used=100, five_hour_resets_at=int(now + H))       # 5 h used up, reset in 1 h
        r = self.run_sp("trigger", "--modes", "resume")
        self.assertIn("resume-waiting", r.stdout)
        self.pace(3 * 86400, used=60, five_hour_used=100, five_hour_resets_at=int(now - 5 * 60))  # reset 5 min ago: inside the grace
        self.assertIn("resume-waiting", self.run_sp("trigger", "--modes", "resume").stdout)
        self.assertEqual(self.sent(), [])

    def test_resume_alarm_falls_back_to_the_transcripts_without_pace_json(self):
        path = self.session("Alpha")
        self.halt(path, time.time() - 5 * H - 15 * 60)                               # no pace.json at all: the stop alone dates the reset
        r = self.run_sp("trigger", "--modes", "resume", FAKE_TRANSCRIPT=path)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([g["to"] for g in self.sent()], ["Alpha"])
        rows = S.file_lines(self.shared())
        self.assertEqual((rows[0]["Reason"], "session:Alpha" in rows[0]["Items"]), ("resume/wake", True))
        self.halt(path, time.time() - 5 * H - 15 * 60, uuid="dur-1")                  # the same stop seen again: never a second relay
        self.run_sp("trigger", "--modes", "resume")
        self.assertEqual(len(self.sent()), 1)

    def test_resume_alarm_leaves_working_sessions_other_limits_and_last_window_only_timers_alone(self):
        path = self.session("Alpha")
        now = time.time()
        self.pace(3 * 86400, used=60, five_hour_used=100, five_hour_resets_at=int(now - 20 * 60))
        self.halt(path, now - H, text="You've hit your org's monthly spend limit · your session limit resets 4:20am")
        self.run_sp("trigger", "--modes", "resume")                                      # a spend limit: a 5-hour reset does not lift it
        self.assertEqual(self.sent(), [])
        os.remove(os.path.join(self.claude, "last-window", "resume.json"))
        self.halt(path, now - H, uuid="dur-2")
        with open(path, "a") as f:
            f.write(assistant("claude-opus-5-5", "after"))                               # it already went on (auto-continue): leave it
        self.run_sp("trigger", "--modes", "resume")
        self.assertEqual(self.sent(), [])
        os.remove(os.path.join(self.claude, "last-window", "resume.json"))
        self.halt(path, now - H, uuid="dur-3")
        r = self.run_sp("trigger")                                                       # a last-window-only timer (no --modes resume)
        self.assertIn("resume alarm: not installed", r.stdout)
        self.assertEqual(self.sent(), [])

    def test_burn_mode_is_switched_on_and_off_only_by_a_developer(self):
        self.pace(3 * 86400)
        r = self.run_sp("burn", "--on", LAST_WINDOW_GH_LOGIN="example-tester-1")          # a tester (role: tester)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("Refused", r.stdout)
        self.assertIsNone(self.read_burn())
        self.assertEqual(self.run_sp("burn", "--on", LAST_WINDOW_GH_LOGIN="nobody-here").returncode, 3)   # not in people.json
        r = self.run_sp("burn", "--on", LAST_WINDOW_GH_LOGIN="example-dev-2", LAST_WINDOW="1")              # from inside a last-window session
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIsNone(self.read_burn())
        r = self.run_sp("burn", "--on", LAST_WINDOW_GH_LOGIN="example-dev-2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Burn mode ON", r.stdout)
        b = self.read_burn()
        self.assertEqual((b["active"], b["opener"], b["opener_login"]), (True, "Onur Demir", "example-dev-2"))
        self.assertEqual(b["week_reset"], jrd(os.path.join(self.claude, "pace.json"))["week_resets_at"])   # auto-off at THIS weekly reset
        self.assertIn("Burn mode ON", self.run_sp("burn", "status").stdout)
        self.assertEqual(self.run_sp("burn", "--off", LAST_WINDOW_GH_LOGIN="example-tester-1").returncode, 3)
        self.assertTrue(self.read_burn()["active"])
        r = self.run_sp("burn", "--off", LAST_WINDOW_GH_LOGIN="example-dev-1")      # another developer may switch it off
        self.assertEqual(r.returncode, 0, r.stdout)
        b = self.read_burn()
        self.assertEqual((b["active"], b["closer"]), (False, "Emre"))
        self.assertIn("Burn mode OFF", self.run_sp("burn").stdout)

    def test_burn_mode_needs_a_known_weekly_reset_and_quota_left(self):
        self.assertEqual(self.run_sp("burn", "--on", LAST_WINDOW_GH_LOGIN="example-dev-2").returncode, 2)   # no pace.json
        self.pace(3 * 86400, used=100)
        self.assertEqual(self.run_sp("burn", "--on", LAST_WINDOW_GH_LOGIN="example-dev-2").returncode, 2)
        self.assertIsNone(self.read_burn())

    def test_burn_mode_gate_at_99_and_100_percent_and_its_empty_run_backoff(self):
        self.write_burn(week_reset=int(time.time() + 3 * 86400))
        self.pace(3 * 86400, used=99)
        r = self.run_sp("trigger", "--dry-run")
        self.assertIn("burn-is-open", r.stdout)                                          # outside the window, quota left: burn mode starts
        self.assertIn("burn mode: ON", r.stdout)
        self.pace(3 * 86400, used=100)
        self.assertIn("quota-done", self.run_sp("trigger", "--dry-run").stdout)
        self.pace(3 * 86400, used=50)
        os.makedirs(os.path.join(self.claude, "last-window"), exist_ok=True)
        jwr(os.path.join(self.claude, "last-window", "status.json"), {"wait_idle": int(time.time() + 600)})
        self.assertIn("idle-wait", self.run_sp("trigger", "--dry-run").stdout)

    def test_burn_mode_run_outside_the_window_ends_at_the_weekly_reset_and_switches_itself_off(self):
        path = self.session("Alpha")
        self.pace(6)
        reset = jrd(os.path.join(self.claude, "pace.json"))["week_resets_at"]
        self.write_burn(week_reset=reset)
        r = self.run_sp("trigger", PACE_TARGET_HOURS="0", FAKE_TRANSCRIPT=path)              # no last window at all: only burn mode can start
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        g = self.sent()
        self.assertEqual([x["to"] for x in g], ["Alpha", "Alpha"])
        self.assertIn("burn mode", g[0]["text"])
        self.assertIn("Reset reached", g[1]["text"])
        b = self.brief()
        self.assertIn("switched on burn mode", b)
        self.assertNotIn("{{", b)
        self.assertNotIn("mode:", b)                                                    # the mode blocks are resolved
        self.assertIn("switching burn mode on or off", b)                              # the never-list covers the switch itself
        rows = S.file_lines(self.shared())
        self.assertEqual(rows[0]["Reason"], "burn/reset")
        r = self.run_sp("trigger", PACE_TARGET_HOURS="0")                                  # the next tick, after the weekly reset
        self.assertIn("burn mode: off", r.stdout)
        b = self.read_burn()
        self.assertEqual((b["active"], b["closer"]), (False, "automatic (weekly reset)"))

    def test_switching_burn_mode_off_ends_the_running_assignment(self):
        path = self.session("Alpha")
        self.pace(120)
        self.write_burn(week_reset=jrd(os.path.join(self.claude, "pace.json"))["week_resets_at"])
        p = subprocess.Popen([sys.executable, "-I", SP, "trigger"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                             env=self.env_(PACE_TARGET_HOURS="0", FAKE_TRANSCRIPT=path))
        self.addCleanup(lambda: p.poll() is None and p.kill())
        end_at = time.time() + 60
        while not self.sent() and time.time() < end_at:
            time.sleep(0.2)
        self.assertEqual(len(self.sent()), 1)
        b = self.read_burn()
        b.update(active=False, closer="Onur Demir")
        jwr(os.path.join(self.claude, "last-window", "burn.json"), b)
        out, err = p.communicate(timeout=90)
        self.assertEqual(p.returncode, 0, out + err)
        self.assertIn("was switched off", self.sent()[1]["text"])
        self.assertEqual(S.file_lines(self.shared())[0]["Reason"], "burn/burn-off")


class StageBase(unittest.TestCase):
    """run_cmd() in-process with a fake clock: live sessions are registry files of this test process's own pid, the relay (S.forward) is
    scripted, sleeping advances the clock and fires scheduled events (a late message, a status change). Nothing real is started."""
    RESET_IN = 1200

    def setUp(self):
        from unittest import mock
        self.root = tempfile.mkdtemp(prefix="sp-stage-")
        self.addCleanup(shutil.rmtree, self.root, True)
        claude = os.path.join(self.root, ".claude")
        dir_ = os.path.join(claude, "last-window")
        os.makedirs(os.path.join(claude, "sessions"))
        self.t, self.events, self.sent, self.behavior, self.sessions = float(NOW), [], [], [], {}
        self.launched = []
        for name, v in (("CLAUDE", claude), ("DIR", dir_), ("LOCK", os.path.join(dir_, "coordinator.lock")),
                      ("STATUS", os.path.join(dir_, "status.json")), ("TIME", lambda: self.t), ("forward", self.role),
                      ("build_prompt", lambda *a, **k: "brief"), ("_person", lambda: "Tester"), ("machine_name", lambda: "m"),
                      ("window_name", lambda: "last-window-tester"), ("repos_root", lambda: self.root), ("TmuxSession", self.fake_session_kind()),
                      ("TabSession", self.fake_session_kind())):
            p = mock.patch.object(S, name, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(S.time, "sleep", self.sleep)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"LAST_WINDOW_TMUX": "/bin/true"})
        p.start()
        self.addCleanup(p.stop)
        for k in ("LAST_WINDOW_ACK_SEC", "LAST_WINDOW_STOP_SEC", "LAST_WINDOW_IDLE_SEC", "LAST_WINDOW_RESET_ATTEMPT", "LAST_WINDOW_EXCLUDING"):
            os.environ.pop(k, None)

    # --- the scene
    def sleep(self, secs):
        self.t += max(float(secs), 0.01)
        for event in [o for o in self.events if o[0] <= self.t]:
            self.events.remove(event)
            event[1]()

    def after(self, secs, f):
        self.events.append((self.t + secs, f))

    def session(self, name, status="busy", model="claude-opus-5-5"):
        sid = "22222222-2222-2222-2222-%012d" % (len(self.sessions) + 1)
        cwd = "/home/u/" + name
        path = S.transcript_path(cwd, sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(assistant(model, "old-" + name))
        self.sessions[name] = {"sid": sid, "cwd": cwd, "path": path, "pid": os.getpid() if not self.sessions else self.helper_pid()}
        self.write_status(name, status)
        return path

    def helper_pid(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p.pid

    def write_status(self, name, status):
        o = self.sessions[name]
        jwr(os.path.join(S.CLAUDE, "sessions", "%d.json" % o["pid"]), session_json(o["pid"], name, o["sid"], o["cwd"], 1000 + len(self.sessions), status))

    def write_message(self, name, text, turn=True):
        with open(self.sessions[name]["path"], "a") as f:
            f.write(json.dumps({"type": "user", "message": "<cross-session-message>" + text + "</cross-session-message>"}) + "\n")
            if turn:
                f.write(assistant("claude-opus-5-5", "t%d" % len(self.sent)))

    def role(self, name, text, time_overrun=None):
        """Scripted relay: each call takes the next (answer, delay of the message in s or None = never shows, clock jump)."""
        self.sent.append((name, text))
        answer, delay, skip = (self.behavior.pop(0) if self.behavior else ("handoff", 0, 0))
        self.t += skip
        if answer == "handoff" and delay is not None:
            if delay == 0:
                self.write_message(name, text)
            else:
                self.after(delay, lambda: self.write_message(name, text))
        return answer

    def fake_session_kind(self):
        test = self

        class Fake(S.Session):
            error = None

            def __init__(self, name, cwd):
                super().__init__(name, "33333333-3333-3333-3333-333333333333", cwd, "tmux")
                self.stopped = 0
                test.launched.append(self)

            def start(self, command, env):
                self.command = command

            def wait_record(self, secs=None):
                if Fake.error:
                    raise Fake.error
                return True

            def live(self):
                return not self.stopped

            def stop(self):
                self.stopped += 1
        return Fake

    def run_cmd(self):
        quota = {"week_used": 93, "week_resets_at": NOW + self.RESET_IN, "five_hour_used": None, "five_hour_resets_at": None}
        return S.run_cmd(quota, {}, {}, NOW)

    def log(self):
        p = os.path.join(S.DIR, "last-window.log")
        return rd(p) if os.path.exists(p) else ""



class Stage(StageBase):
    # --- finding 1: a relay's HANDOFF binds the run to that session
    def test_handoff_with_the_message_showing_late_stays_with_that_session_no_second_delivery(self):
        self.session("Alpha", "busy")
        self.session("Beta", "idle")
        self.behavior = [("handoff", 900, 0)]                 # Alpha is busy: the message shows up 15 min later, after ACK_SEC
        path = self.sessions["Alpha"]["path"]
        self.after(100, lambda: open(path, "a").write(assistant("claude-opus-5-5", "before-the-marker")))   # its own turn: not this run's
        k = self.run_cmd()
        self.assertEqual([a for a, _ in self.sent], ["Alpha", "Alpha"])   # the assignment once, then "reset reached"
        self.assertIn("Reset reached", self.sent[1][1])
        self.assertEqual((k["reason"], k["session"], k["via"]), ("reset", "Alpha", "assignment"))
        self.assertEqual(k["turn"], 1)                          # only the turn after the marker counts
        self.assertEqual(self.launched, [])                  # no fallback session
        self.assertRegex(self.log(), r"shows the assignment, 9\d\d s")

    def test_handoff_without_the_message_on_an_idle_session_gives_up_without_a_second_delivery(self):
        self.session("Alpha", "idle")
        self.session("Beta", "idle")
        self.behavior = [("handoff", None, 0)]                 # the relay says HANDOFF, the message never shows up
        k = self.run_cmd()
        self.assertEqual([a for a, _ in self.sent], ["Alpha"])
        self.assertEqual(k["reason"], "handoff-unseen")
        self.assertEqual(self.launched, [])

    def test_handoff_without_the_message_on_a_busy_session_follows_it_to_the_reset(self):
        self.session("Alpha", "busy")
        self.behavior = [("handoff", None, 0), ("handoff", 0, 0)]
        k = self.run_cmd()
        self.assertEqual([a for a, _ in self.sent], ["Alpha", "Alpha"])
        self.assertEqual(k["reason"], "reset")
        self.assertEqual(self.launched, [])

    def test_a_relay_that_times_out_hands_nothing_to_another_session_or_the_fallback(self):
        self.session("Alpha", "idle")
        self.session("Beta", "idle")
        self.behavior = [("unknown", None, 0)]
        k = self.run_cmd()
        self.assertEqual([a for a, _ in self.sent], ["Alpha"])
        self.assertEqual((k["reason"], k["exit"]), ("handoff-unknown", 1))
        self.assertEqual(self.launched, [])

    def test_a_clear_missing_or_a_relay_error_moves_on_and_then_to_the_fallback(self):
        self.session("Alpha", "idle")
        self.session("Beta", "idle")
        self.behavior = [("missing", None, 0), ("error", None, 0)]
        k = self.run_cmd()
        self.assertEqual([a for a, _ in self.sent], ["Alpha", "Beta"])
        self.assertEqual(len(self.launched), 1)
        self.assertEqual((k["via"], k["reason"]), ("tmux", "reset"))

    # --- finding 2: our own session never outlives the wrapper
    def test_an_exception_after_the_start_stops_our_own_session_and_records_the_run(self):
        getattr(S, SESSION_CLASS).error = RuntimeError("boom")
        k = self.run_cmd()
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.launched[0].stopped, 1)
        self.assertEqual((k["reason"], k["exit"]), ("error", -1))
        self.assertIn("RuntimeError: boom", self.log())

    def test_a_signal_after_the_start_stops_our_own_session(self):
        getattr(S, SESSION_CLASS).error = S.Stopped()
        k = self.run_cmd()
        self.assertEqual(self.launched[0].stopped, 1)
        self.assertEqual(k["reason"], "stopped")

    # --- finding 6: "reset reached" is confirmed, not fire-and-forget
    def test_a_failed_reset_relay_is_retried_until_the_message_shows(self):
        self.session("Alpha", "busy")
        self.behavior = [("handoff", 0, 0), ("missing", None, 0), ("error", None, 0), ("handoff", 0, 0)]
        k = self.run_cmd()
        self.assertEqual([a for a, _ in self.sent], ["Alpha"] * 4)
        self.assertEqual(k["reset_notifier"], "seen")
        self.assertIn("reset message reached 'Alpha' (relay 3 of 3)", self.log())

    def test_reset_relays_are_bounded_and_the_failure_is_logged(self):
        self.session("Alpha", "busy")
        self.behavior = [("handoff", 0, 0)] + [("missing", None, 0)] * 10
        k = self.run_cmd()
        self.assertEqual(len(self.sent), 1 + 3)
        self.assertEqual(k["reset_notifier"], "missing")
        self.assertIn("NOT confirmed", self.log())

    def test_an_idle_session_that_does_not_show_the_reset_message_gets_it_again(self):
        self.session("Alpha", "busy")
        self.behavior = [("handoff", 0, 0), ("handoff", None, 0), ("handoff", 0, 0)]
        self.after(self.RESET_IN - 5, lambda: self.write_status("Alpha", "idle"))
        k = self.run_cmd()
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(k["reset_notifier"], "seen")

    # --- finding 7: nothing is handed out once the reset (or its last 10 minutes) comes during delivery
    def test_the_reset_coming_during_candidate_delivery_stops_the_hand_out(self):
        self.session("Alpha", "idle")
        self.session("Beta", "idle")
        self.behavior = [("missing", None, self.RESET_IN - 300)]   # the first relay took until 5 min before the reset
        k = self.run_cmd()
        self.assertEqual([a for a, _ in self.sent], ["Alpha"])   # Beta is not tried
        self.assertEqual(self.launched, [])                          # and no fallback session
        self.assertEqual(k["reason"], "reset-near")


class StageTab(StageBase):
    """The Windows tab (TabSession) through the scene: wt.exe / cmd are not run (Popen recorded), taskkill is recorded."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        self.killed, self.opened = [], []
        self.tab_pid = self.helper_pid()
        for name, v in ((SESSION_CLASS, REAL_TAB), ("_taskkill", self.killed.append), ("_sid_processes", lambda sid: [4242])):
            p = mock.patch.object(S, name, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(S.subprocess, "Popen", lambda command, **kw: self.opened.append(command))
        p.start()
        self.addCleanup(p.stop)

    def register(self):
        """The tab's claude registers: its session id is the one in the generated .cmd file."""
        cmd = rd(os.path.join(S.DIR, "runs", "last-window-tester.cmd"))
        sid = cmd.split("--session-id ")[1].split()[0]
        jwr(os.path.join(S.CLAUDE, "sessions", "%d.json" % self.tab_pid), session_json(self.tab_pid, "last-window-tester", sid, self.root, 1, "busy"))

    def test_a_windows_tab_that_registers_late_is_followed_not_recorded_as_failed(self):
        self.after(60, self.register)                            # wt.exe returned at once; the tab registers a minute later
        k = self.run_cmd()
        self.assertEqual(len(self.opened), 1)
        self.assertEqual((k["reason"], k["via"]), ("reset", "tab"))
        self.assertEqual(self.killed, [self.tab_pid])       # ended by its registered pid at the reset

    def test_a_windows_tab_that_never_registers_is_stopped_by_its_session_id(self):
        k = self.run_cmd()
        self.assertEqual((k["reason"], k.get("fast")), ("error", True))
        self.assertIn("not registered within 120 s", k["detail"])
        self.assertEqual(self.killed, [4242])


class StageClear(StageBase):
    def test_a_new_session_id_after_clear_is_followed_by_pid_and_name(self):
        self.session("Alpha", "busy")
        self.behavior = [("handoff", 0, 0)]

        def clear():                                           # same process, new session id and transcript
            o = self.sessions["Alpha"]
            o["sid"] = "44444444-4444-4444-4444-444444444444"
            o["path"] = S.transcript_path(o["cwd"], o["sid"])
            with open(o["path"], "w") as f:
                f.write(assistant("claude-opus-5-5", "after-clear"))
            self.write_status("Alpha", "busy")
        self.after(300, clear)
        k = self.run_cmd()
        self.assertEqual(k["reason"], "reset")                  # not "session-closed"
        self.assertEqual(k["reset_notifier"], "seen")       # the reset message was seen in the NEW transcript
        self.assertEqual(k["turn"], 2)                          # the turn after the marker and the one after /clear
        self.assertIn("has a new session id", self.log())


class Forward(unittest.TestCase):
    """The relay's answer, through the fake relay script (no real claude)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="forward-sp-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.fake = os.path.join(self.root, "fake_claude.py")
        with open(self.fake, "w") as f:
            f.write("#!%s\n%s" % (sys.executable, FAKE_ROLE))
        os.chmod(self.fake, 0o755)
        if os.name == "nt":                                    # Windows runs no shebang: a .cmd shim starts the fake with this interpreter
            shim = os.path.join(self.root, "fake_claude.cmd")
            with open(shim, "w") as f:
                f.write('@"%s" "%s" %%*' % (sys.executable, self.fake) + chr(10))
            self.fake = shim
        from unittest import mock
        for name, v in (("DIR", self.root), ("HOME", self.root)):
            p = mock.patch.object(S, name, v)
            p.start()
            self.addCleanup(p.stop)

    def forward(self, via, **env):
        from unittest import mock
        path = os.path.join(self.root, "t.jsonl")
        with mock.patch.dict(os.environ, dict(LAST_WINDOW_CLAUDE=self.fake, FAKE_MARK=os.path.join(self.root, "mark"), FAKE_MODE=via,
                                              FAKE_TRANSCRIPT=path, LAST_WINDOW_ROLE_SEC="2", **env)):
            return S.forward("Alpha", "hello")

    def test_answers(self):
        self.assertEqual(S.forward_result("**HANDOFF**"), "handoff")
        self.assertEqual(S.forward_result("Done.\nHANDOFF."), "handoff")
        self.assertEqual(S.forward_result("MISSING"), "missing")
        self.assertIsNone(S.forward_result("HANDOFFS"))
        self.assertEqual(self.forward("ok", FAKE_MARKDOWN="1"), "handoff")
        self.assertEqual(self.forward("missing"), "missing")
        self.assertEqual(self.forward("error"), "error")
        self.assertEqual(self.forward("silent"), "unknown")

    @unittest.skipIf(os.name == "nt", "the fake relay is an executable script")
    def test_a_relay_timeout_work_unknown(self):
        t = time.time()
        self.assertEqual(self.forward("time"), "unknown")
        self.assertLess(time.time() - t, 30)


class Attempt(unittest.TestCase):
    def test_the_test_command_refuses_foreign_sessions(self):
        r = subprocess.run([sys.executable, "-I", SP, "attempt", "--send", "emre-07"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("refused", r.stdout)


class ModeGate(unittest.TestCase):
    """The one gate with its modes: last window, burn mode, resume alarm (pure functions, simulated pace.json states)."""
    RESET = NOW + 3 * 86400        # weekly reset in 3 days: outside the 5 h window

    def quota(self, used=50, reset=None, **kw):
        return {"week_used": used, "week_resets_at": reset or self.RESET, "five_hour_used": None, "five_hour_resets_at": None, **kw}

    def test_burn_mode_runs_outside_the_window_until_the_weekly_limit(self):
        k = lambda **kw: S.decision(NOW, self.quota(**{x: v for x, v in kw.items() if x not in ("last", "idle_run")}), 5, False, kw.get("last"),
                                 mode="burn", wait_idle=kw.get("idle_run"))
        self.assertEqual(k()[:2], ("start", "burn-is-open"))
        self.assertEqual(k(used=99)[0], "start")                                       # weekly at 99%: still works
        self.assertEqual(k(used=100)[1], "quota-done")
        self.assertEqual(k(five_hour_used=100, five_hour_resets_at=NOW + H)[1], "five-hour-full")   # 5 h used up: waits for its reset
        self.assertEqual(k(idle_run=NOW + 60)[1], "idle-wait")
        self.assertEqual(k(last=NOW - 60)[1], "retry-interval")
        self.assertEqual(S.decision(NOW, self.quota(), 5, False, None)[1], "before-window")   # the same state without burn mode: nothing

    def test_resume_skips_the_window_and_spacing_but_not_the_lock_or_the_weekly_limit(self):
        self.assertEqual(S.decision(NOW, self.quota(), 5, False, NOW - 60, mode="resume")[:2], ("start", "resume"))
        self.assertEqual(S.decision(NOW, {"week_resets_at": None}, 5, False, None, mode="resume")[0], "start")   # no weekly data: not blocked
        self.assertEqual(S.decision(NOW, self.quota(), 5, True, None, mode="resume")[1], "running")
        self.assertEqual(S.decision(NOW, self.quota(used=100), 5, False, None, mode="resume")[1], "quota-done")

    def test_precedence_window_then_burn_mode_then_resume(self):
        picks = lambda quota, modes, burn, resume: (S.select_mode(NOW, quota, 5, False, None, 0, modes, burn, resume)[0] or [None])[0]
        self.assertEqual(picks(self.quota(), ("window",), True, True), "burn")
        self.assertEqual(picks(self.quota(), ("window", "resume"), False, True), "resume")
        self.assertIsNone(picks(self.quota(), ("window",), False, False))
        self.assertEqual(picks(self.quota(reset=NOW + 3 * H), ("window",), True, True), "window")
        self.assertEqual(len(S.select_mode(NOW, self.quota(), 5, False, None, 0, ("resume",), False, False)[1]), 0)   # nothing installed/due

    def test_resume_state_from_pace_json(self):
        f = lambda u5, r5, res_state=None: S.resume_status({"five_hour_used": u5, "five_hour_resets_at": r5}, res_state or {}, NOW)
        self.assertEqual(f(100, NOW + H)["reason"], "resume-waiting")                     # 5 h used up, reset to come
        self.assertEqual(f(100, NOW - 60)["reason"], "resume-waiting")                    # reset passed, auto-continue goes first
        d = f(100, NOW - 11 * 60)
        self.assertEqual((d["ready"], d["key"]), (True, NOW - 11 * 60))              # reset passed: due, keyed by that reset
        self.assertIsNone(f(100, NOW - 11 * 60, {"key": NOW - 11 * 60})["ready"])    # handled: only unrelayed stops (uuids) remain
        self.assertIsNone(f(3, NOW + 4 * H)["ready"])                                    # a newer window already shows: the transcripts decide
        self.assertIsNone(S.resume_status({}, {}, NOW)["ready"])                           # pace.json missing: the transcripts decide
        self.assertIsNone(f(100, NOW - 6 * H)["ready"])                                  # stale: the same fallback, no crash
        self.assertIsNone(S.resume_status({"five_hour_used": "x", "five_hour_resets_at": None}, {}, NOW)["ready"])

    def test_reset_of_a_stop(self):
        self.assertEqual(S.halt_reset(NOW - H, NOW + H), NOW + H)                       # in pace.json's window: its reset
        self.assertEqual(S.halt_reset(NOW - 4 * H, NOW + 4 * H), NOW - H)               # a newer window shows: reset by its start at the latest
        self.assertEqual(S.halt_reset(NOW - H, None), NOW + 4 * H)                      # nothing known: 5 h after the stop at the latest
        self.assertEqual(S.halt_reset(NOW - H, NOW - 2 * H), NOW + 4 * H)               # an older reading says nothing about this stop


class LimitHalt(unittest.TestCase):
    """Which live session the 5-hour limit stopped, from the registry and the transcript tail (scratch config dir)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sp-limit-")
        self.old = (S.CLAUDE, S.DIR)
        S.CLAUDE, S.DIR = os.path.join(self.root, ".claude"), os.path.join(self.root, ".claude", "last-window")
        os.makedirs(os.path.join(S.CLAUDE, "sessions"))
        self.live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        self.n = 0

    def tearDown(self):
        self.live.kill()
        self.live.wait()
        S.CLAUDE, S.DIR = self.old
        shutil.rmtree(self.root, ignore_errors=True)

    def add(self, name, *lines, status="idle"):
        self.n += 1
        sid, cwd = "00000000-0000-0000-0000-%012d" % self.n, "/home/u/w%d" % self.n
        jwr(os.path.join(S.CLAUDE, "sessions", "%d-%d.json" % (self.live.pid, self.n)), session_json(self.live.pid, name, sid, cwd, 1, status))
        path = S.transcript_path(cwd, sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(assistant("claude-opus-5-5") + "".join(lines))
        return path

    def test_the_stop_is_the_last_assistant_entry_and_only_a_5_hour_one(self):
        t = NOW - H
        read = lambda *s: S.limit_halt(self.add("x", *s))
        d = read(limit_line(t))
        self.assertEqual((round(d["t"]), d["uuid"]), (t, "dur-1"))
        self.assertIsNotNone(read(limit_line(t), json.dumps({"type": "user", "message": "<task-notification>"}) + "\n"))   # user lines after it
        self.assertIsNone(read(limit_line(t), assistant("claude-opus-5-5", "later")))      # it went on afterwards
        self.assertIsNone(read(limit_line(t, "You've hit your weekly limit · resets Oct 21, 3am")))
        self.assertIsNone(read(limit_line(t, "You've hit your individual spend limit · your session limit resets 9:20am")))
        self.assertIsNone(read())

    def test_due_sessions_by_pace_reset_or_by_the_stop_alone(self):
        self.add("stopped", limit_line(NOW - 2 * H))
        self.add("running", limit_line(NOW - 2 * H, uuid="d2"), status="busy")       # busy = it already went on
        self.add("old", limit_line(NOW - 13 * H, uuid="d3"))                          # older than RESUME_MAX_AGE
        self.add("last-window-emre", limit_line(NOW - 2 * H, uuid="d4"))             # last-window's own session
        name = lambda r5, **kw: [o["name"] for o in S.limit_stopped(S.read_sessions(model=False), r5, NOW, **kw)]
        self.assertEqual(name(NOW - 11 * 60), ["stopped"])                                    # reset 11 min ago (pace.json)
        self.assertEqual(name(NOW - 5 * 60), [])                                            # reset 5 min ago: the grace is not over
        self.assertEqual(name(NOW - 11 * 60, warned=["dur-1"]), [])                       # already relayed for this stop
        self.assertEqual(name(NOW - 11 * 60, skip={"stopped"}), [])
        self.assertEqual(name(None), [])                                                    # no pace: stop + 5 h is still ahead
        self.assertEqual([o["name"] for o in S.limit_stopped(S.read_sessions(model=False), None, NOW + 3 * H + 11 * 60)], ["stopped"])


class BurnStatus(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sp-burn-")
        self.old = S.DIR
        S.DIR = self.root

    def tearDown(self):
        S.DIR = self.old
        shutil.rmtree(self.root, ignore_errors=True)

    def test_on_until_the_stored_weekly_reset_then_switched_off_once(self):
        jwr(S.burn_path(), {"active": True, "opener": "Onur Demir", "week_reset": NOW + 60})
        self.assertTrue(S.burn_is_open(NOW))
        self.assertFalse(S.burn_duration_full(NOW))
        self.assertFalse(S.burn_is_open(NOW + 60))                                          # at the reset it is off, even unwritten
        self.assertTrue(S.burn_duration_full(NOW + 61))
        self.assertEqual(jrd(S.burn_path())["closer"], "automatic (weekly reset)")
        self.assertFalse(S.burn_duration_full(NOW + 62))
        self.assertFalse(S.burn_is_open(NOW, {}))                                           # never switched on

    def test_the_brief_keeps_only_the_blocks_of_its_mode(self):
        text = "a\n<!-- mode:window -->\nP\n<!-- /mode -->\n<!-- mode:burn,window -->\nB\n<!-- /mode -->\nz\n"
        self.assertEqual(S.mode_blocks(text, "window"), "a\nP\nB\nz\n")
        self.assertEqual(S.mode_blocks(text, "burn"), "a\nB\nz\n")


if __name__ == "__main__":
    unittest.main()

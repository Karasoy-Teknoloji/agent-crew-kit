#!/usr/bin/env python3
"""Install kota-temposu into a SCRATCH home and render the main status line and one sub-agent row from a masked sample
(tests/fixtures/*.json), exactly through the commands the installer wrote into settings.json. Fails (exit 1) when an expected
segment is missing. Works on Linux, macOS and Windows; used by .github/workflows/statusline-platformlar.yml and by hand on a lab VM.

  python3 plugins/kota-temposu/tests/platform_render.py [--scratch DIR] [--keep]

PLATFORM_RENDER_SHELL=<exe> replaces the shell of the statusLine command (Windows PowerShell 5.1 check).
Never touches the real profile: HOME and USERPROFILE point at the scratch folder for every child process.
Linux/macOS install with `bash kur.sh`, Windows with `pwsh|powershell -File kur.ps1` (the default install: cost statistics included).
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUG = os.path.normpath(os.path.join(HERE, ".."))
FIX = os.path.join(HERE, "fixtures")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
EN_DAYS = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b")


def run(cmd, env, stdin=None, timeout=900):
    r = subprocess.run(cmd, input=None if stdin is None else stdin.encode("utf-8"), capture_output=True, env=env, timeout=timeout)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def assistant_line(mid, tokens, ts):
    u = {"input_tokens": tokens, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
         "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}
    return json.dumps({"type": "assistant", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts)), "requestId": "req_" + mid,
                       "uuid": "u" + mid, "message": {"id": mid, "model": "claude-opus-5-5", "usage": u}}) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    win = os.name == "nt"
    home = os.path.abspath(args.scratch or tempfile.mkdtemp(prefix="sl-platform-"))
    os.makedirs(home, exist_ok=True)
    claude = os.path.join(home, ".claude")
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "MALIYET_DURUM", "MALIYET_TZ", "MALIYET_FIYATLAR")}
    env.update(HOME=home, USERPROFILE=home, COLUMNS="400", LC_ALL=env.get("LC_ALL", "C.UTF-8") if not win else "")
    if win:
        env.pop("LC_ALL", None)
    sys.stdout.reconfigure(encoding="utf-8")
    failures = []

    def check(ok, what):
        print("  [%s] %s" % ("OK" if ok else "EKSIK", what))
        if not ok:
            failures.append(what)

    print("scratch home: %s (%s)" % (home, sys.platform))
    # 1. install (default = with the cost statistics)
    if win:
        shell = shutil.which("pwsh") or shutil.which("powershell")
        cmd = [shell, "-NoProfile", "-File", os.path.join(PLUG, "kur.ps1")]
    else:
        cmd = ["bash", os.path.join(PLUG, "kur.sh")]
    rc, out, err = run(cmd, env)
    print(out + err)
    check(rc == 0, "installer exit 0 (got %d)" % rc)
    settings_path = os.path.join(claude, "settings.json")
    if not os.path.isfile(settings_path):
        print("no settings.json: installer failed")
        return 1
    with open(settings_path, encoding="utf-8") as f:
        settings = json.load(f)
    for key in ("statusLine", "subagentStatusLine"):
        print("  %s: %s" % (key, str(settings.get(key, {}).get("command", "")).replace(home.replace("\\", "/"), "<HOME>")))
        check(isinstance(settings.get(key), dict) and settings[key].get("command"), "settings.json has %s" % key)
    check(all("\\" not in str(settings.get(k, {}).get("command", "")) for k in ("statusLine", "subagentStatusLine")),
          "both commands use forward slashes only")

    # 2. data the segments need: a lira rate, the calibration, local transcripts (for the windows and the agent row), pace history
    now = int(time.time())
    mal = os.path.join(claude, "maliyet")
    os.makedirs(mal, exist_ok=True)
    proj = os.path.join(claude, "projects", "fixture-project")
    transcript = os.path.join(proj, "sess-fixture-0001.jsonl")
    agent = os.path.join(proj, "sess-fixture-0001", "subagents", "agent-fx1.jsonl")
    os.makedirs(os.path.dirname(agent), exist_ok=True)
    with open(transcript, "w", encoding="utf-8") as f:
        f.write(assistant_line("big", 440_000_000, now - 3600))          # $1,760 at the Opus 5.5 input price: a 4-digit 7g amount
        f.write(json.dumps({"type": "user", "isSidechain": False, "message": {"role": "user", "content": "fixture prompt"}}) + "\n")
        f.write(assistant_line("turn", 201_250, now - 600))              # the last action: $0.805 = ₺40,25 at 50 TL/USD, main session only
    with open(agent, "w", encoding="utf-8") as f:
        f.write(assistant_line("ag", 1_000_000, now - 1800))             # $4.00 for the agent row
    py = sys.executable
    rc, out, err = run([py, "-I", os.path.join(claude, "bin", "maliyet.py"), "--kaydet"], env)
    check(rc == 0, "maliyet --kaydet on the fixture transcripts (rc %d)" % rc)
    with open(os.path.join(mal, "kur.json"), "w", encoding="utf-8") as f:
        json.dump({"kur": 50.0, "t": now}, f)
    with open(os.path.join(mal, "oran.json"), "w", encoding="utf-8") as f:
        json.dump({"t": now, "katsayi": 0.05}, f)                        # gercek = API x 0.05
    r5, r7 = now + 3600, now + 3 * 86400
    with open(os.path.join(claude, "pace-history.csv"), "w", encoding="utf-8") as f:   # 5h window: 0 % an hour ago, 60 % now -> runs out before the reset
        f.write("ts,week_used,five_hour_used,week_resets_at,five_hour_resets_at,host\n")
        f.write("%d,60,0,%d,%d,fixture\n" % (now - 3500, r7, r5))

    def fixture(name, total=22.805):
        with open(os.path.join(FIX, name), encoding="utf-8") as f:
            t = f.read()
        fwd = lambda p: p.replace("\\", "/")
        return (t.replace('"__R5__"', str(r5)).replace('"__R7__"', str(r7)).replace('"__TOTAL__"', repr(total)).replace('"__START__"', str((now - 1584) * 1000))
                .replace("__TRANSCRIPT__", fwd(transcript)).replace("__HOME__", fwd(home)))

    # 3. main line, through the exact command in settings.json
    print("\nMain line:")
    main_cmd = shlex.split(settings["statusLine"]["command"])
    if os.environ.get("PLATFORM_RENDER_SHELL"):                          # e.g. powershell.exe: the same script under Windows PowerShell 5.1
        main_cmd[0] = os.environ["PLATFORM_RENDER_SHELL"]
    run(main_cmd, env, fixture("statusline-girdi.json", 22.0))           # first sighting of the prompt: the baseline is stored
    rc, out, err = run(main_cmd, env, fixture("statusline-girdi.json"))  # the last action comes from the main transcript: $0.805 = ₺40,25 at 50 TL/USD (gercek x 0.05 = ₺2,01); the total_cost_usd delta of the same prompt (+0.805) is the first window "son Ns" (seconds in the first minute)
    line = ANSI.sub("", out).strip()
    print(line)
    if err.strip():
        print("stderr: " + err.strip()[:300])
    check(rc == 0 and bool(line), "status line command printed something (rc %d)" % rc)
    check("\n" not in line, "one line (no wrapping at COLUMNS=400)")
    segs = {
        "model and effort": r"^Opus 5\.5 · high(?!\*)",
        "context %15 (155K/1M)": r"\| %15 \(155K/1M\) \|",
        "clock": r"\| \d\d:\d\d ",
        "last action: lira amount, real cost in parentheses": r"\d\d:\d\d ₺40,25 \(₺2,01\) \|",
        "first window: son Ndk (this session incl. agents)": r"\| son \d+sn ₺40,25 \(₺2,01\) · son 24sa ",
        "5h segment with a forecast time (→bitiş HH:MM)": r"5sa %60→bitiş \d\d:\d\d \(\d+(sa\d+)?dk\)",
        "weekly segment with the mode symbol": r"hafta %63→%\d+(±\d+)? (🟢|🟡|🔴)",
        "folder": r"\| ~$|\| ~ \|",
        "cache hit rate": r"önb %94 \(2 kaçış\)",
        "oturum in lira, real cost in parentheses": r"oturum ₺1\.140 \(₺57,01\) \|",
        "rolling windows, real cost in parentheses, 7g also in dollars (no parentheses)": r"son 24sa ₺[\d.]+ \(₺[\d.,]+\) · 7g ₺[\d.]+ \$[\d.]+ \(₺[\d.,]+\) · 30g ₺[\d.]+ \(₺[\d.,]+\) · ×20$",
    }
    for what, rx in segs.items():
        check(re.search(rx, line) is not None, what)
    check(EN_DAYS.search(line) is None, "no English day names")

    # 4. sub-agent row
    print("\nSub-agent row:")
    sub_cmd = shlex.split(settings["subagentStatusLine"]["command"])
    rc, out, err = run(sub_cmd, env, fixture("subagent-statusline-girdi.json"))
    rows = [json.loads(l) for l in out.splitlines() if l.strip()]
    row = ANSI.sub("", rows[0]["content"]) if rows else ""
    print(row or "(no row) " + err.strip()[:300])
    check(rc == 0 and len(rows) == 1 and rows[0].get("id") == "fx1", "one row for the fixture task")
    check(row.startswith("Fixture task: check the backup chain · senior-developer-oh · 68k"), "task, agent type and tokens")
    check(re.search(r"· ₺200 \(₺10,00\) · ", row) is not None or re.search(r"· ₺[\d.,]+ \(₺[\d.,]+\) · ", row) is not None, "agent cost in lira with the real cost in parentheses")
    check(re.search(r"· \d+(sn|dk)( \d+sn)?$", row) is not None, "elapsed time")

    for name in ("statusline-girdi.json", "subagent-statusline-girdi.json"):
        check(os.path.isfile(os.path.join(claude, name)), "stdin dump %s written" % name)

    if not args.keep and not args.scratch:
        shutil.rmtree(home, ignore_errors=True)
    print("\n%s" % ("TUMU TAMAM" if not failures else "EKSIK: " + "; ".join(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

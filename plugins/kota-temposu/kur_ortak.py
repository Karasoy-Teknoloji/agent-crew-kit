#!/usr/bin/env python3
"""Shared installer logic for kota-temposu (called by kur.ps1 and kur.sh).

Copies the scripts into ~/.claude, merges statusLine + subagentStatusLine + pace-hook entries into settings.json
(forward slashes only, idempotent), runs a self-check. Never creates pace-peer.json.
--repodan copies nothing: settings.json, the cost command and the daily job point at the scripts in this claude-shared clone (the
plugin folder has the same layout as ~/.claude), old copies are removed, and ~/.claude/kota-temposu.json records the mode so
tools/rules_watch.py keeps the clone up to date (git pull --ff-only, then this self-check, rolled back on a failure). Runtime data
stays in ~/.claude. --sinama runs only the quick self-check (no install, no first scan); rules_watch.py calls it after a pull.
The cost statistics are part of the default install (bin/maliyet*.py, price table, dashboard template, a `maliyet`
command and a per-user job that records the totals every 2 hours: a systemd user timer on Linux, a launchd user agent on macOS, a Task
Scheduler task on Windows; without systemd the status-line background refresh records them). --maliyetsiz leaves them out (--maliyet
is accepted and means the default). --remove always removes them too (the recorded history under ~/.claude/maliyet is kept).
--paylas [--kisi "Ad Soyad"] [--plan 100|200] joins the team usage ranking: checks the name against kisiler.json and push access to the
shared repo (nothing is written when a check fails), writes ~/.config/claude-shared/kisi and plan (the files maliyet_paylas.py reads),
and makes that job run `--kaydet --paylas` (it commits only when the row or the ranking changed).
Every install (any mode) also sets up the team's lost-work protection from this clone, nothing copied: the SessionStart hook
tools/rules_watch.py (rule warnings + the cached kayip_is.py result, refreshed in the background; silent when the clone is missing) and a daily job running
tools/gunluk_kayip.py (= kayip_is.py --push-unpushed --clean-lossless --cache-yaz; Windows task / systemd user timer 'kayip-is', 17:30). --remove removes both.
Run with `python -I`. Output is Turkish for the end user.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
STAMP = datetime.date.today().isoformat()
EVENTS = ("SessionStart", "UserPromptSubmit")
# (source file, destination relative to ~/.claude, platform: "win" / "posix" / "both"). The plugin folder has the same layout as
# ~/.claude (status line scripts at the top, everything else in bin/), so the scripts find their siblings relative to themselves.
FILES = [
    ("bin/pace_forecast.py", "bin/pace_forecast.py", "both"),
    ("bin/pace-hook.py", "bin/pace-hook.py", "both"),
    ("statusline.ps1", "statusline.ps1", "win"),
    ("bin/statusline-yerel.ps1", "bin/statusline-yerel.ps1", "win"),
    ("statusline.sh", "statusline.sh", "posix"),
    ("bin/satir_sar.py", "bin/satir_sar.py", "posix"),     # width wrap for statusline.sh (Windows wraps inside statusline-yerel.ps1)
    # the sub-agent rows live in the sibling plugin ekip-ajanlari (one source); the installed copy finds the cost tools in ~/.claude/bin
    ("../ekip-ajanlari/tools/subagent-statusline.py", "bin/subagent-statusline.py", "both"),
    # the one hidden-launch helper (tools/gizli.*): the bin scripts and statusline.ps1 find it at ../../tools in the repo layout, in bin/ in a copy
    ("../../tools/gizli.py", "bin/gizli.py", "both"),
    ("../../tools/gizli_yukle.py", "bin/gizli_yukle.py", "both"),   # the one import of gizli (falls back to plain subprocess if gizli.py is missing)
    ("../../tools/gizli.ps1", "bin/gizli.ps1", "win"),
]
# Optional cost statistics (source relative to this folder, destination relative to ~/.claude, platform)
MALIYET_FILES = [
    ("bin/maliyet.py", "bin/maliyet.py", "both"),
    ("bin/maliyet_cekirdek.py", "bin/maliyet_cekirdek.py", "both"),
    ("bin/maliyet_ayristir.py", "bin/maliyet_ayristir.py", "both"),
    ("bin/maliyet_paylas.py", "bin/maliyet_paylas.py", "both"),
    ("../../tools/gunluk.py", "bin/gunluk.py", "both"),     # the rolling run log of the scheduled job (a clone install finds it in tools/)
    ("bin/maliyet_artimli.py", "bin/maliyet_artimli.py", "both"),
    ("bin/maliyet_oran.py", "bin/maliyet_oran.py", "both"),
    ("bin/maliyet_kur.py", "bin/maliyet_kur.py", "both"),
    ("bin/maliyet-fiyatlar.json", "bin/maliyet-fiyatlar.json", "both"),
    ("bin/maliyet-panel.html", "bin/maliyet-panel.html", "both"),
]
# Optional "son pencere" mode (kur.sh --son-pencere): the last-window wrapper, its coordinator prompt; needs the cost/share modules above
SON_PENCERE_FILES = [
    ("bin/son_pencere.py", "bin/son_pencere.py", "both"),
    ("bin/son-pencere-istem.md", "bin/son-pencere-istem.md", "both"),
]
UNIT = "maliyet-gunluk"
SP_UNIT = "son-pencere"
SP_GOREV = "son-pencere"   # Windows Task Scheduler task name
LAUNCHD_ETIKET = "com.agentcrewkit.maliyet-gunluk"   # launchd agent label; the plist is ~/Library/LaunchAgents/<label>.plist
EXIT_PAYLAS_OPSIYON = 2     # bad --paylas/--kisi/--plan (nothing written)
EXIT_PAYLAS_ERISIM = 3      # no push access to the shared repo (nothing written)
OUR_STATUS = ("statusline-yerel.ps1", "statusline.ps1", "statusline.sh")
OUR_SUBAGENT = "subagent-statusline.py"
SUB_KEY = "bin/subagent-statusline.py"
DURUM = "kota-temposu.json"   # ~/.claude/<DURUM>: written by a direct-source (repodan) install, which is the default; tools/rules_watch.py reads it
# Its "kurulum" key holds the sharing options of the install (--paylas --kisi --plan --son-pencere); a bare re-run (`kur.sh` with no
# options) reuses them, so repairing or moving the clone never drops a teammate out of the ranking.
# Team tools every install sets up from this clone (never copied, in every mode): the SessionStart hook tools/rules_watch.py (rule
# change warnings, repodan auto-update, and the cached lost-work result of tools/kayip_is.py) and a daily lost-work job that pushes own
# unpushed branches and refreshes that cache (gunluk_kayip.py -> kayip_is.py --push-unpushed --cache-yaz: never main/master/test/dev, never force, never someone else's commits).
KLON = SRC.parent.parent
sys.path.insert(0, str(KLON / "tools"))   # tools/gizli.py: the one hidden-launch helper (python -I drops the script dir)
from gizli_yukle import gizli  # noqa: E402
RULES_WATCH = KLON / "tools" / "rules_watch.py"
GIZLI_PS1 = KLON / "tools" / "gizli.ps1"   # the one hidden-launch helper every Windows task snippet dot-sources
KAYIP = KLON / "tools" / "kayip_is.py"
KAYIP_GIRIS = KLON / "tools" / "gunluk_kayip.py"   # the ONE entry the daily job calls; its options live in the clone (no copy in the scheduler)
KANIT = KLON / "tools" / "kanit_kancasi.py"   # Stop + SubagentStop hook: hedge words, missing-access claims without a `kasa.py bul` search, unrecorded decisions (Stop)
KANIT_EVENTS = ("Stop", "SubagentStop")
ARAC_KONTROL = KLON / "tools" / "arac_kontrol.py"   # tool check + the Windows python3 shim (kur.ps1 -Araclar / kur.sh --araclar installs)
GH_SINIR = KLON / "tools" / "gh_sinir.py"   # gh shim ~/.local/bin/gh -> this clone's machine-wide GitHub API limiter
BEKCI = KLON / "tools" / "ajan_bekci_notu.py"   # PostToolUse (matcher "Agent") hook: reminds to keep the 30-min agent watchdog CronCreate
AJAN_ADI = KLON / "plugins" / "ekip-ajanlari" / "tools" / "ajan_adi.py"   # PreToolUse (matcher "Agent|Task") hook: names every subagent
SG_DIR = KLON / "tools" / "slack-triage"      # optional token-free Slack inbox poller (kur.sh --slack-gelen, kur.ps1 -SlackGelen), run from the clone
SG_GELEN = SG_DIR / "gelen.py"
SG_EVENTS = ("SessionStart", "UserPromptSubmit")   # hook events that print the unread count (status line shows it too)
SG_UNIT = "slack-gelen"                       # systemd user unit
SG_ETIKET = "com.agentcrewkit.slack-gelen"         # launchd agent label
SG_GOREV = "slack-gelen"                      # Windows Task Scheduler task name
SG_DAKIKA = 3                                 # the Windows task starts at hh:03 and repeats every 5 min (:03, :08, ...), off the round minutes
# SessionStart + UserPromptSubmit hook: prints "Slack: N yeni" (only when unread items exist); silent when the clone is missing
SG_YEDEK = "import os,runpy,sys; p=sys.argv[1]; sys.argv=[p,'count']; os.path.isfile(p) and runpy.run_path(p, run_name='__main__')"
KAYIP_UNIT = "kayip-is"      # systemd user unit
KAYIP_GOREV = "kayip-is"     # Windows Task Scheduler task
# The plugin plugins/ekip-ajanlari (agent tools, the proaktif-kisa output style) runs straight from this clone: the claude-shared
# marketplace is declared as a local `directory` source and the plugin is enabled in settings.json (merge_ekip_ajanlari), and
# `claude plugin install` registers it (claude_eklentileri; enabledPlugins alone does not install it: without the install record Claude
# Code does not load its output style). Claude Code then loads the plugin in place (no cache copy).
# The team agents are NOT in the plugin: Claude Code always prefixes plugin agents with the plugin name (`ekip-ajanlari:quick-task-hm`)
# and has no setting for the agents directory, so they live in <clone>/agents and ~/.claude/agents/claude-shared is a link to that
# directory (ajan_baglantisi: a junction on Windows, no admin needed; a symlink elsewhere). Claude Code loads subdirectories of
# ~/.claude/agents, so a person's own agents stay beside it untouched; a link, not a copy, so a git pull updates the agents.
AJAN_KAYNAK = KLON / "agents"
AJAN_BAG = "claude-shared"                                # ~/.claude/agents/<AJAN_BAG> -> AJAN_KAYNAK
PAZAR = "claude-shared"                                   # "name" in .claude-plugin/marketplace.json
AJAN_EKLENTI = f"ekip-ajanlari@{PAZAR}"
ESKI_EKLENTILER = (f"team-agents@{PAZAR}", f"legacy-agents@{PAZAR}")   # merged into ekip-ajanlari (0.6.0); dropped from enabledPlugins
# The ONE old -> new agent name mapping (table: plugins/ekip-ajanlari/README.md "Old names"; every row has a 1:1 successor, merged sets
# map several old names to one new name; `Explore` is unchanged and not listed). Used twice: copies or symlinks of the old names in
# ~/.claude/agents are backed up and removed (the keys), and old names in the personal ~/.claude/CLAUDE.md are rewritten (the pairs).
ESKI_AJANLAR = {
    "haiku-isci": "quick-task-hm", "quick-task": "quick-task-hm",
    "sonnet-isci": "developer-sm", "developer": "developer-sm",
    "sonnet-derin": "hard-task-sh",
    "opus-derin": "senior-developer-oh", "senior-developer": "senior-developer-oh",
    "opus-max": "principal-engineer-omax",
    "fable-derin": "deep-analysis-fh", "deep-analysis": "deep-analysis-fh",
    "long-task": "long-task-fxh",
    "research": "research-sm", "reviewer": "reviewer-oh", "release-ops": "release-ops-oh", "security": "security-oh",
    "dogrulayici": "verifier-hm", "operasyon": "operations-sm", "panel-okuyucu": "panel-reader-sm",
}
# Old names that are also plain English words: renaming them inside prose would corrupt it ("security review"), so the CLAUDE.md
# rewrite skips them (their agent files are still removed).
ESKI_AJAN_SOZCUK = frozenset({"developer", "research", "reviewer", "security"})
# The hook runs the clone's script only when it exists: a moved or missing clone must not show a hook error at every start.
RW_YEDEK ="import os,runpy,sys; p=sys.argv[1]; os.path.isfile(p) and runpy.run_path(p, run_name='__main__')"


def kayitli_bayraklari_ekle(a, kayit):
    """Add the options saved by the last install (the "kurulum" list `kayit`) to the parsed options `a` (in place) and return `a`.
    A flag given now wins over the saved value (--kisi, --plan) and is added on top of the others (--son-pencere keeps --paylas).
    Nothing drops a saved option except --kaldir (there is no per-option off switch). The one exception: an explicit --maliyetsiz
    cannot run with the cost modules that --paylas and --son-pencere need, so those two saved flags are left out, with a note."""
    if a.maliyetsiz:
        for f in ("--paylas", "--son-pencere", "--devam-alarmi"):
            if f in kayit and not getattr(a, f[2:].replace("-", "_"), False):
                say(f"UYARI: --maliyetsiz ile {f} birlikte calismaz; kayitli {f} bu kurulumda birakildi.")
    else:
        a.paylas = a.paylas or "--paylas" in kayit
        a.son_pencere = a.son_pencere or "--son-pencere" in kayit
        a.devam_alarmi = getattr(a, "devam_alarmi", False) or "--devam-alarmi" in kayit
    a.slack_gelen = a.slack_gelen or "--slack-gelen" in kayit
    a.slack_triage = a.slack_triage or "--slack-triage" in kayit
    a.bayat = a.bayat or "--bayat" in kayit
    if a.kisi is None and "--kisi" in kayit and (a.paylas or a.son_pencere or getattr(a, "devam_alarmi", False)):
        a.kisi = kayit[kayit.index("--kisi") + 1]
    if a.plan is None and a.paylas and "--plan" in kayit:
        a.plan = kayit[kayit.index("--plan") + 1]
    return a


def kayitli_kurulum(claude):
    """The sharing options saved by the last direct-source install (list of strings), or []."""
    try:
        d = json.loads((claude / DURUM).read_text(encoding="utf-8"))
        k = d.get("kurulum")
        return k if isinstance(k, list) and all(isinstance(x, str) for x in k) else []
    except (OSError, ValueError, AttributeError):
        return []


def yerler(claude, repodan, want):
    """Where each script runs from, keyed by its destination under ~/.claude ("bin/pace-hook.py"): the copy under ~/.claude, or with
    --repodan the file in this clone (same layout, see FILES). settings.json, the cost job and the self-check all take paths from here."""
    return {d: (Path(os.path.normpath(SRC / s)) if repodan else claude / d) for s, d, plat in FILES + MALIYET_FILES + SON_PENCERE_FILES if plat in want}


def backup_name(path):
    """path.yedek-<date>; if that exists already (rerun the same day), keep the older backup."""
    bak = path.with_name(path.name + f".yedek-{STAMP}")
    if bak.exists():
        bak = path.with_name(path.name + f".yedek-{STAMP}-{datetime.datetime.now():%H%M%S}")
    return bak


def say(msg):
    print(msg)


def posix(p):
    return str(p).replace("\\", "/")


def is_ours_hook(h):
    # Matches broken entries too: backslashes (JSON-escaped or not) or eaten separators.
    blob = json.dumps(h, ensure_ascii=False)
    return "pace-hook" in blob


def is_ours_status(s):
    blob = json.dumps(s)
    return isinstance(s, dict) and any(k in blob for k in OUR_STATUS)


def merge(cfg, python, hook, status_cmd, remove, sub_cmd=None):
    """Mutate cfg; return a list of change descriptions (Turkish)."""
    changes = []
    hooks = cfg.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ValueError("'hooks' bir nesne degil")
    for ev in EVENTS:
        groups = (hooks or {}).get(ev)
        if groups is None:
            groups = []
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{ev} bir liste degil")
        new_entry = {"type": "command", "command": python, "args": ["-I", hook], "timeout": 10}
        placed = False
        out = []
        for g in groups:
            inner = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(inner, list):
                out.append(g)
                continue
            kept = []
            for h in inner:
                if is_ours_hook(h):
                    if not remove and not placed:
                        kept.append(new_entry)
                        placed = True
                        if h != new_entry:
                            changes.append(f"{ev}: pace-hook girisi guncellendi")
                    else:
                        changes.append(f"{ev}: pace-hook girisi kaldirildi")
                else:
                    kept.append(h)
            if kept:
                out.append({**g, "hooks": kept})
        if not remove and not placed:
            out.append({"hooks": [new_entry]})
            changes.append(f"{ev}: pace-hook girisi eklendi")
        if out:
            cfg.setdefault("hooks", {})[ev] = out
        elif isinstance(cfg.get("hooks"), dict):
            cfg["hooks"].pop(ev, None)
    if remove and cfg.get("hooks") == {}:
        del cfg["hooks"]
    cur = cfg.get("statusLine")
    if remove:
        if is_ours_status(cur):
            del cfg["statusLine"]
            changes.append("statusLine kaldirildi")
    elif not (isinstance(cur, dict) and cur.get("type") == "command" and cur.get("command") == status_cmd):
        base = {k: v for k, v in cur.items() if k not in ("args",)} if isinstance(cur, dict) else {}
        cfg["statusLine"] = {**base, "type": "command", "command": status_cmd}
        changes.append("statusLine ayarlandi")
    # sub-agent rows: only a command that is ours is replaced or removed; somebody else's subagentStatusLine is left alone
    cur = cfg.get("subagentStatusLine")
    ours = isinstance(cur, dict) and OUR_SUBAGENT in json.dumps(cur)
    if remove:
        if ours:
            del cfg["subagentStatusLine"]
            changes.append("subagentStatusLine kaldirildi")
    elif sub_cmd:
        if cur is not None and not ours:
            changes.append("subagentStatusLine baska bir komut: dokunulmadi")
        elif not (isinstance(cur, dict) and cur.get("type") == "command" and cur.get("command") == sub_cmd):
            cfg["subagentStatusLine"] = {"type": "command", "command": sub_cmd}
            changes.append("subagentStatusLine ayarlandi")
    return changes


# (event, matcher, extra argument) of the tools/rules_watch.py entries: the session start report and the cheap per-prompt delta ("--istek")
RW_OLAYLAR = (("SessionStart", "startup", None), ("UserPromptSubmit", None, "--istek"))


def merge_rules_watch(cfg, python, remove):
    """Mutate cfg: one entry per event of RW_OLAYLAR for tools/rules_watch.py of this clone, replacing any earlier rules_watch entry
    (e.g. one added by hand from the README); removed with --remove. Returns change descriptions (Turkish)."""
    hooks = cfg.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ValueError("'hooks' bir nesne degil")
    changes = []
    for olay, matcher, ek in RW_OLAYLAR:
        groups = (hooks or {}).get(olay) or []
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{olay} bir liste degil")
        new_entry = {"type": "command", "command": python, "args": ["-I", "-c", RW_YEDEK, posix(RULES_WATCH)] + ([ek] if ek else []), "timeout": 120}
        out, placed = [], False
        for g in groups:
            inner = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(inner, list):
                out.append(g)
                continue
            kept = []
            for h in inner:
                if "rules_watch" not in json.dumps(h, ensure_ascii=False):
                    kept.append(h)
                elif not remove and not placed:
                    kept.append(new_entry)
                    placed = True
                    if h != new_entry:
                        changes.append(f"{olay}: rules_watch girisi guncellendi")
                else:
                    changes.append(f"{olay}: rules_watch girisi kaldirildi")
            if kept:
                out.append({**g, "hooks": kept})
        if not remove and not placed:
            out.append({**({"matcher": matcher} if matcher else {}), "hooks": [new_entry]})
            changes.append(f"{olay}: rules_watch girisi eklendi (kural uyarilari + kayip is raporu)")
        if out:
            cfg.setdefault("hooks", {})[olay] = out
        elif isinstance(cfg.get("hooks"), dict):
            cfg["hooks"].pop(olay, None)
            if not cfg["hooks"]:
                del cfg["hooks"]
    return changes


EXIT_AYAR_GECERSIZ = 3


def rules_watch_uygula(settings, python):
    """--ayar (run by tools/rules_watch.py's background run after a pull): bring ONLY the rules_watch hook entries of settings.json up to
    date with merge_rules_watch, the same function the install uses, so a new RW_OLAYLAR event arrives with git pull and needs no re-run.
    Touches nothing else; no write when nothing changes; one backup (.yedek-<date>) before the first write; the write is atomic (temp file
    + replace). A missing file is left alone (no install yet); invalid JSON is not touched (EXIT_AYAR_GECERSIZ). Returns the exit code."""
    if not settings.is_file():
        say("settings.json yok: dokunulmadi")
        return 0
    try:
        cfg = json.loads(settings.read_text(encoding="utf-8-sig"))
        if not isinstance(cfg, dict):
            raise ValueError("kok nesne degil")
        changes = merge_rules_watch(cfg, python, False)
    except (ValueError, OSError) as e:
        say(f"UYARI: settings.json okunamadi/ayristirilamadi ({e}); rules_watch girisleri guncellenmedi, dosyaya dokunulmadi.")
        return EXIT_AYAR_GECERSIZ
    if not changes:
        say("settings.json: rules_watch girisleri guncel, degisiklik yok")
        return 0
    bak = backup_name(settings)
    shutil.copy2(settings, bak)
    say(f"yedek: {posix(bak)}")
    tmp = settings.with_name(settings.name + ".gecici")
    tmp.write_bytes((json.dumps(cfg, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    os.replace(tmp, settings)
    for c in changes:
        say(f"settings.json: {c}")
    return 0


def merge_kanit_kancasi(cfg, python, remove):
    """Mutate cfg: one entry per event in KANIT_EVENTS (Stop for the main session, SubagentStop for subagents) running
    tools/kanit_kancasi.py of this clone, replacing any earlier kanit_kancasi entry (e.g. the `test -f ... ||true` line added by hand
    from the README); removed with --remove. Returns change descriptions (Turkish)."""
    hooks = cfg.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ValueError("'hooks' bir nesne degil")
    new_entry = {"type": "command", "command": python, "args": ["-I", "-c", RW_YEDEK, posix(KANIT)], "timeout": 10}
    changes = []
    for ev in KANIT_EVENTS:
        groups = (hooks or {}).get(ev) or []
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{ev} bir liste degil")
        out, placed = [], False
        for g in groups:
            inner = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(inner, list):
                out.append(g)
                continue
            kept = []
            for h in inner:
                if "kanit_kancasi" not in json.dumps(h, ensure_ascii=False):
                    kept.append(h)
                elif not remove and not placed:
                    kept.append(new_entry)
                    placed = True
                    if h != new_entry:
                        changes.append(f"{ev}: kanit_kancasi girisi guncellendi")
                else:
                    changes.append(f"{ev}: kanit_kancasi girisi kaldirildi")
            if kept:
                out.append({**g, "hooks": kept})
        if not remove and not placed:
            out.append({"hooks": [new_entry]})
            changes.append(f"{ev}: kanit_kancasi girisi eklendi (tahmin sozcukleri, kasa.py bul'suz 'yetki yok', yazilmamis karar)")
        if out:
            cfg.setdefault("hooks", {})[ev] = out
        elif isinstance(cfg.get("hooks"), dict):
            cfg["hooks"].pop(ev, None)
            if not cfg["hooks"]:
                del cfg["hooks"]
    return changes


def _merge_ajan_kancasi(cfg, python, remove, olay, matcher, betik, aciklama):
    """Mutate cfg: one `olay` group with `matcher` running `betik` of this clone (an Agent-tool hook); an existing entry for the same
    script is updated in place, other hooks of the event are kept; removed with --remove. Returns change descriptions (Turkish)."""
    hooks = cfg.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ValueError("'hooks' bir nesne degil")
    groups = (hooks or {}).get(olay) or []
    if not isinstance(groups, list):
        raise ValueError(f"hooks.{olay} bir liste degil")
    isaret = betik.stem
    new_entry = {"type": "command", "command": python, "args": ["-I", "-c", RW_YEDEK, posix(betik)], "timeout": 10}
    out, changes, placed = [], [], False
    for g in groups:
        inner = g.get("hooks") if isinstance(g, dict) else None
        if not isinstance(inner, list):
            out.append(g)
            continue
        kept = []
        for h in inner:
            if isaret not in json.dumps(h, ensure_ascii=False):
                kept.append(h)
            elif not remove and not placed and g.get("matcher") == matcher:
                kept.append(new_entry)
                placed = True
                if h != new_entry:
                    changes.append(f"{olay}: {isaret} girisi guncellendi")
            else:
                changes.append(f"{olay}: {isaret} girisi kaldirildi")
        if kept:
            out.append({**g, "hooks": kept})
    if not remove and not placed:
        out.append({"matcher": matcher, "hooks": [new_entry]})
        changes.append(f"{olay}: {isaret} girisi eklendi ({aciklama})")
    if out:
        cfg.setdefault("hooks", {})[olay] = out
    elif isinstance(cfg.get("hooks"), dict):
        cfg["hooks"].pop(olay, None)
        if not cfg["hooks"]:
            del cfg["hooks"]
    return changes


def merge_ajan_bekci(cfg, python, remove):
    """PostToolUse (matcher "Agent"): tools/ajan_bekci_notu.py, the 30-minute agent watchdog reminder."""
    return _merge_ajan_kancasi(cfg, python, remove, "PostToolUse", "Agent", BEKCI, "arka plan ajani baslayinca 30 dk bekci hatirlatmasi")


def merge_ajan_adi(cfg, python, remove):
    """PreToolUse (matcher "Agent|Task"): plugins/ekip-ajanlari/tools/ajan_adi.py, a random name for every spawned subagent."""
    return _merge_ajan_kancasi(cfg, python, remove, "PreToolUse", "Agent|Task", AJAN_ADI, "her alt ajana rastgele bir ad")


def merge_ekip_ajanlari(cfg, remove):
    """Mutate cfg: extraKnownMarketplaces.claude-shared -> this clone as a `directory` source (other keys of the entry, e.g. autoUpdate,
    are kept) and enabledPlugins[ekip-ajanlari@claude-shared] = true; the merged-away team-agents/legacy-agents entries are dropped.
    --remove drops the enabledPlugins entry, and the marketplace entry only when it points at this clone and no other claude-shared
    plugin is enabled. Returns change descriptions (Turkish)."""
    pazarlar, eklentiler = cfg.get("extraKnownMarketplaces"), cfg.get("enabledPlugins")
    if pazarlar is not None and not isinstance(pazarlar, dict):
        raise ValueError("'extraKnownMarketplaces' bir nesne degil")
    if eklentiler is not None and not isinstance(eklentiler, dict):
        raise ValueError("'enabledPlugins' bir nesne degil")
    pazarlar, eklentiler = dict(pazarlar or {}), dict(eklentiler or {})
    kaynak = {"source": "directory", "path": posix(KLON)}
    changes = []
    for eski in ESKI_EKLENTILER:
        if eski in eklentiler:
            del eklentiler[eski]
            changes.append(f"enabledPlugins: {eski} kaldirildi (ekip-ajanlari ile birlesti)")
    giris = pazarlar.get(PAZAR)
    bizim = isinstance(giris, dict) and giris.get("source") == kaynak
    if remove:
        if eklentiler.pop(AJAN_EKLENTI, None) is not None:
            changes.append(f"enabledPlugins: {AJAN_EKLENTI} kaldirildi")
        if bizim and not any(k.endswith("@" + PAZAR) and v for k, v in eklentiler.items()):
            del pazarlar[PAZAR]
            changes.append(f"extraKnownMarketplaces: {PAZAR} (bu klon) kaldirildi")
    else:
        if not bizim:
            pazarlar[PAZAR] = {**(giris if isinstance(giris, dict) else {}), "source": kaynak}
            changes.append(f"extraKnownMarketplaces: {PAZAR} -> bu klon ({posix(KLON)}), eklentiler yerinde yuklenir")
        if eklentiler.get(AJAN_EKLENTI) is not True:
            eklentiler[AJAN_EKLENTI] = True
            changes.append(f"enabledPlugins: {AJAN_EKLENTI} acildi (ekip ajanlari klondan)")
    for anahtar, deger in (("extraKnownMarketplaces", pazarlar), ("enabledPlugins", eklentiler)):
        if deger:
            cfg[anahtar] = deger
        else:
            cfg.pop(anahtar, None)
    return changes


def claude_plugin(exe, args, timeout=180):
    """`claude plugin <args>` -> (ok, last output line). Never raises; stdin is closed so nothing can prompt."""
    try:
        r = gizli.run([exe, "plugin", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=timeout)
    except Exception as e:
        return False, str(e)
    cikti = (r.stdout.strip() or r.stderr.strip()).splitlines()
    return r.returncode == 0, (cikti[-1][:200].encode("ascii", "replace").decode() if cikti else "")   # ASCII: the console may be cp1254


def claude_eklentileri(cfg, a, tag):
    """Register the claude-shared plugins that settings.json enables (--remove: ekip-ajanlari) with the `claude plugin` CLI, user
    scope. `install` is idempotent ("already installed") and the plugin loads in place from this clone, so there is no update step.
    A marketplace Claude Code has not registered yet (a fresh machine) is added from this clone and the install retried. Skips (True)
    when `claude` is not on PATH or HOME is not the real home (tests); False only when a CLI call fails."""
    istenen = [AJAN_EKLENTI] if a.remove else sorted(k for k, v in (cfg.get("enabledPlugins") or {}).items()
                                                      if v is True and k.endswith("@" + PAZAR))
    if not istenen:
        return True
    exe = shutil.which("claude")
    kendi = " && ".join(f"claude plugin {'uninstall' if a.remove else 'install'} {k} --scope user" for k in istenen)
    if not exe:
        say(f"{tag}claude komutu PATH'te yok: eklentiler kaydedilmedi (claude kurulunca kur'u yeniden calistirin veya: {kendi})")
        return True
    real = gercek_ev()
    if real is None or Path.home().resolve() != real.resolve():
        say(f"{tag}HOME gercek ev dizini degil: claude plugin calistirilmadi. Gercek kurulumda: {kendi}")
        return True
    if a.whatif:
        say(f"{tag}claude plugin: {kendi}")
        return True
    ok = True
    for k in istenen:
        if a.remove:
            durum, ileti = claude_plugin(exe, ["uninstall", k, "--scope", "user"])
            durum = durum or "not installed" in ileti.lower() or "not found" in ileti.lower()   # nothing to remove is fine
        else:
            durum, ileti = claude_plugin(exe, ["install", k, "--scope", "user"])
            if not durum:   # marketplace not registered with Claude Code yet: add this clone, retry once
                eklendi, _ = claude_plugin(exe, ["marketplace", "add", posix(KLON), "--scope", "user"])
                if eklendi:
                    durum, ileti = claude_plugin(exe, ["install", k, "--scope", "user"])
        say(f"  [{'OK' if durum else 'HATA'}] claude plugin {'uninstall' if a.remove else 'install'} {k}: {ileti}")
        ok &= durum
    return ok


def eski_ajanlari_temizle(claude, tag, whatif):
    """Back up and remove ~/.claude/agents/<old name>.md (a copy or a symlink of either old agent set, dangling ones included), so
    only the merged team set loads. A symlink is only removed (its target lives in git); a file is copied to .yedek-<date>
    first (the backup does not end in .md, so Claude Code does not load it)."""
    klasor = claude / "agents"
    for ad in ESKI_AJANLAR:
        p = klasor / f"{ad}.md"
        if not (p.is_symlink() or p.exists()):
            continue
        if not p.is_symlink():
            bak = backup_name(p)
            say(f"{tag}yedek: {posix(bak)}")
            if not whatif:
                shutil.copy2(p, bak)
        say(f"{tag}sil: {posix(p)} (eski ajan adi; yerine ~/.claude/agents/{AJAN_BAG}, bkz. plugins/ekip-ajanlari/README.md)")
        if not whatif:
            p.unlink()


def _bag_mi(p):
    """True when p is a symlink or a Windows junction (dangling ones included); never follows it."""
    if p.is_symlink():
        return True
    isj = getattr(os.path, "isjunction", None)   # Python 3.12+
    if isj is not None:
        return isj(p)
    try:
        import stat
        return getattr(os.lstat(p), "st_reparse_tag", 0) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)
    except OSError:
        return False


def _bag_sil(p):
    """Remove the link itself, never what it points at: a junction or a Windows directory symlink is a directory entry (rmdir, which
    removes only the reparse point), a POSIX symlink is a file entry (unlink)."""
    if os.name == "nt":
        try:
            os.rmdir(p)
            return
        except OSError:
            pass
    os.unlink(p)


def _bag_kur(hedef, p):
    """Create p -> hedef: a directory junction on Windows (no admin or developer mode needed, unlike a symlink), a symlink elsewhere."""
    if os.name == "nt":
        try:
            import _winapi
            _winapi.CreateJunction(str(hedef), str(p))
            return
        except (ImportError, AttributeError):
            pass
    os.symlink(hedef, p, target_is_directory=True)


def ajan_baglantisi(claude, tag, remove, hedef=None):
    """~/.claude/agents/claude-shared -> <clone>/agents (see AJAN_KAYNAK). Idempotent: a link to the right place is left alone; a link
    that points elsewhere or nowhere (moved or deleted clone) is replaced. Only that one entry is ever touched: a real directory or file
    with the same name is not ours and stays (warning, False). A missing source (no clone, or a clone without agents/) is a no-op.
    --remove takes the link away (only a link, never a directory). Returns True when the link is in place or nothing was to be done."""
    hedef = Path(hedef or AJAN_KAYNAK)
    p = claude / "agents" / AJAN_BAG
    bag = _bag_mi(p)
    if remove:
        if bag:
            say(f"{tag}sil: {posix(p)} (ajan baglantisi)")
            if not tag:
                _bag_sil(p)
        return True
    if not hedef.is_dir():
        say(f"{tag}ajan kaynagi yok, baglanti kurulmadi: {posix(hedef)}")
        return True
    if bag:
        try:
            ayni = os.path.isdir(p) and os.path.samefile(p, hedef)
        except OSError:
            ayni = False
        if ayni:
            say(f"{tag}ayni: {posix(p)} -> {posix(hedef)}")
            return True
    elif p.exists():
        say(f"{tag}UYARI: {posix(p)} bir baglanti degil; dokunulmadi, ekip ajanlari bu makinede yuklenmez. Baska yere tasiyip kur'u "
            f"yeniden calistirin.")
        return False
    say(f"{tag}ajan baglantisi: {posix(p)} -> {posix(hedef)}" + (" (eski/kirik baglanti yenilendi)" if bag else ""))
    if tag:
        return True
    try:
        if bag:
            _bag_sil(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        _bag_kur(hedef, p)
    except OSError as e:
        say(f"  [HATA] ajan baglantisi kurulamadi: {e}")
        return False
    return True


def ajan_adlarini_guncelle(claude, tag, whatif):
    """Rewrite whole-word old agent names (ESKI_AJANLAR) in the personal ~/.claude/CLAUDE.md to the new names, so sessions stop asking
    for agents that no longer exist. Only that file (never repos, rules files or memory). The file is backed up (.yedek-<date>) first;
    nothing is written with whatif; a second run finds nothing. A line that talks about the old names themselves ("old names ...",
    "eski ad ...") is history text and is left alone. `quick-task-hm` is not matched by `quick-task` (no word or hyphen neighbours)."""
    p = claude / "CLAUDE.md"
    try:
        ham = p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return
    adlar = [k for k in ESKI_AJANLAR if k not in ESKI_AJAN_SOZCUK]
    desen = re.compile(r"(?<![\w-])(" + "|".join(re.escape(k) for k in sorted(adlar, key=len, reverse=True)) + r")(?![\w-])")
    tarih = re.compile(r"\bold names?\b|\beski (?:ad|isim)", re.IGNORECASE)
    yapilan = {}

    def degistir(m):
        yapilan[m.group(1)] = ESKI_AJANLAR[m.group(1)]
        return ESKI_AJANLAR[m.group(1)]

    satirlar = [l if tarih.search(l) else desen.sub(degistir, l) for l in ham.splitlines(keepends=True)]
    if not yapilan:
        return
    bak = backup_name(p)
    say(f"{tag}yedek: {posix(bak)}")
    say(f"{tag}ajan adlari guncellendi: {posix(p)} (" + ", ".join(f"{k} -> {v}" for k, v in yapilan.items()) + ")")
    if not whatif:
        shutil.copy2(p, bak)
        p.write_bytes("".join(satirlar).encode("utf-8"))


def merge_slack_gelen(cfg, python, install):
    """Mutate cfg: one entry per event in SG_EVENTS (SessionStart, UserPromptSubmit) running `gelen.py count` of this clone ("Slack: N yeni";
    prints nothing when the inbox is empty, so a running session sees new items on its next prompt), replacing any earlier gelen.py entry
    (e.g. one added by hand); install=False removes them. Returns change descriptions (Turkish)."""
    hooks = cfg.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ValueError("'hooks' bir nesne degil")
    new_entry = {"type": "command", "command": python, "args": ["-I", "-c", SG_YEDEK, posix(SG_GELEN)], "timeout": 10}
    changes = []
    for ev in SG_EVENTS:
        groups = (cfg.get("hooks") or {}).get(ev) or []
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{ev} bir liste degil")
        out, placed = [], False
        for g in groups:
            inner = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(inner, list):
                out.append(g)
                continue
            kept = []
            for h in inner:
                hs = json.dumps(h, ensure_ascii=False)
                if not ("gelen.py" in hs and "slack-triage" in hs):
                    kept.append(h)
                elif install and not placed:
                    kept.append(new_entry)
                    placed = True
                    if h != new_entry:
                        changes.append(f"{ev}: slack-gelen girisi guncellendi")
                else:
                    changes.append(f"{ev}: slack-gelen girisi kaldirildi")
            if kept:
                out.append({**g, "hooks": kept})
        if install and not placed:
            out.append({"hooks": [new_entry]})
            changes.append(f"{ev}: slack-gelen girisi eklendi (\"Slack: N yeni\")")
        if out:
            cfg.setdefault("hooks", {})[ev] = out
        elif isinstance(cfg.get("hooks"), dict):
            cfg["hooks"].pop(ev, None)
            if not cfg["hooks"]:
                del cfg["hooks"]
    return changes


def run_check(cmd, stdin, label, timeout=60):
    try:
        r = gizli.run(cmd, input=stdin, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as e:
        say(f"  [HATA] {label}: {e}")
        return False
    ok = r.returncode == 0
    say(f"  [{'OK' if ok else 'HATA'}] {label} (cikis {r.returncode})")
    if not ok and r.stderr.strip():
        say("    " + r.stderr.strip().splitlines()[-1][:200])
    return ok


def gercek_ev():
    """The account's real home directory from the user database (Windows: the profile folder from the shell API, not
    from USERPROFILE, so a scratch USERPROFILE is never taken for the real one); None when it cannot be determined."""
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            # FOLDERID_Profile {5E6C858F-0E22-4760-9AFE-EA3317B67173}
            class GUID(ctypes.Structure):
                _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]
            g = GUID(0x5E6C858F, 0x0E22, 0x4760, (ctypes.c_ubyte * 8)(0x9A, 0xFE, 0xEA, 0x33, 0x17, 0xB6, 0x71, 0x73))
            ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(ptr)) == 0:
                val = ptr.value
                ctypes.windll.ole32.CoTaskMemFree(ptr)
                return Path(val)
        except Exception:
            pass
    return None


GOREV = "maliyet-gunluk"   # Windows Task Scheduler task name (the systemd unit is named UNIT)

GOREV_KAYIT_PS = r"""
$ErrorActionPreference = 'Stop'
. ([scriptblock]::Create((Get-Content -Raw -LiteralPath $env:KUR_GIZLI_PS1)))   # tools/gizli.ps1: the one hidden-task registration (read as text: works under any execution policy)
$t = New-ScheduledTaskTrigger -Once -At ((Get-Date).Date.AddMinutes(23)) -RepetitionInterval (New-TimeSpan -Hours 2) -RepetitionDuration (New-TimeSpan -Days 3650)
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$s.Priority = 8
Register-GizliGorev -Ad $env:KUR_GOREV_AD -Exe $env:KUR_GOREV_EXE -Argument ($env:KUR_GOREV_ON + ' "' + $env:KUR_GOREV_BETIK + '" --kaydet' + $env:KUR_GOREV_EK) -Trigger $t -Settings $s -Aciklama 'Claude Code maliyet kaydi, 2 saatte bir (kota-temposu, maliyet --kaydet [--paylas]; yalniz degisim varsa commit)'
"""
KAYIP_GOREV_KAYIT_PS = r"""
$ErrorActionPreference = 'Stop'
. ([scriptblock]::Create((Get-Content -Raw -LiteralPath $env:KUR_GIZLI_PS1)))   # tools/gizli.ps1: the one hidden-task registration (read as text: works under any execution policy)
$t = New-ScheduledTaskTrigger -Daily -At '17:30'
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-GizliGorev -Ad $env:KUR_GOREV_AD -Exe $env:KUR_GOREV_EXE -Argument ($env:KUR_GOREV_ON + ' "' + $env:KUR_GOREV_BETIK + '"') -Trigger $t -Settings $s -Aciklama 'Kayip is: kendi push edilmemis dallarini push eder, kaybolmayan worktree ve yerel dallari temizler (kayip_is.py --push-unpushed --clean-lossless; main/master/test/dev ve force yok)'
"""
GOREV_SIL_PS = r"""
$ErrorActionPreference = 'Stop'
if (Get-ScheduledTask -TaskName $env:KUR_GOREV_AD -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName $env:KUR_GOREV_AD -Confirm:$false }
"""

GOREV_PAYLASIYOR_PS = r"""
$t = Get-ScheduledTask -TaskName $env:KUR_GOREV_AD -ErrorAction SilentlyContinue
if ($t -and (($t.Actions | ForEach-Object { $_.Arguments }) -join ' ') -match '--paylas') { exit 7 }
exit 0
"""


def gorev_paylasiyor(shell):
    """True when the registered Task Scheduler task of the current user already runs with --paylas (exit code 7 of the snippet). Replaced by tests."""
    try:
        r = gizli.run([shell, "-NoProfile", "-NonInteractive", "-Command", GOREV_PAYLASIYOR_PS], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=dict(os.environ, KUR_GOREV_AD=GOREV), timeout=60)
    except Exception:
        return False
    return r.returncode == 7


def gorev_env(python):
    """Env entries for every Windows task snippet of this installer (one code path). The hidden launch itself (`conhost.exe --headless <python.exe>
    -I <script>`, why, and the pythonw fallback) lives in ONE place, tools/gizli.ps1 (New-GizliEylem / Register-GizliGorev); the snippets dot-source
    it from KUR_GIZLI_PS1 and hand over the plain interpreter (KUR_GOREV_EXE) and its leading arguments (KUR_GOREV_ON)."""
    return {"KUR_GOREV_EXE": str(python), "KUR_GOREV_ON": "-I", "KUR_GIZLI_PS1": str(GIZLI_PS1)}


def gorev_calistir(shell, ps_betik, python, script, paylas=False, ad=GOREV, ek=None):
    """Run a Task Scheduler PowerShell snippet for the CURRENT user only (no admin, no UAC), for the task `ad` (the daily cost
    task by default, KAYIP_GOREV for the lost-work job). Replaced by tests."""
    env = dict(os.environ, KUR_GOREV_AD=ad, **gorev_env(python), KUR_GOREV_BETIK=str(script),
               KUR_GOREV_EK=" --paylas" if paylas else "", KUR_SG_DAKIKA=str(SG_DAKIKA), **(ek or {}))
    try:
        r = gizli.run([shell, "-NoProfile", "-NonInteractive", "-Command", ps_betik], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=120)
    except Exception as e:
        say(f"  [HATA] gorev zamanlayici: {e}")
        return False
    if r.returncode != 0:
        say("  [HATA] gorev zamanlayici (cikis %d)" % r.returncode)
        if (r.stderr or r.stdout).strip():
            say("    " + (r.stderr or r.stdout).strip().splitlines()[-1][:200])
    return r.returncode == 0


def yaz_uret(dst, text, tag, executable=False):
    """Write a generated file; a different existing file is kept as a backup first. Returns True when something changed."""
    data = text.encode("utf-8")
    if dst.exists():
        if dst.read_bytes() == data:
            say(f"{tag}ayni: {posix(dst)}")
            return False
        bak = backup_name(dst)
        say(f"{tag}yedek: {posix(bak)}")
        if not tag:
            shutil.copy2(dst, bak)
    say(f"{tag}yaz: {posix(dst)}")
    if not tag:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        if executable:
            dst.chmod(0o755)
    return True


def sil_dosya(p, tag):
    if p.exists():
        say(f"{tag}sil: {posix(p)}")
        if not tag:
            p.unlink()
        return True
    return False


def unit_q(p):
    """systemd ExecStart token: '%' doubled, quoted when it holds a space."""
    p = str(p).replace("%", "%%")
    return f'"{p}"' if " " in p else p


def eski_dropin_temizle(dropin_dir, claude, home, tag):
    """An old manual drop-in (`maliyet-gunluk.service.d/paylas.conf` or any other *.conf there) whose ExecStart runs a script under
    ~/.claude/bin/ breaks once the direct-source install removes that copy. Back it up (.yedek-<date>), remove it, and say so in one
    line. Drop-ins that run anything else are left alone. Returns True when one was found (the caller does the daemon-reload)."""
    kok = os.path.normpath(str(claude / "bin"))
    bulundu = False
    for conf in sorted(dropin_dir.glob("*.conf")) if dropin_dir.is_dir() else []:
        try:
            satirlar = conf.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        yollar = [t.strip('"\'') for l in satirlar if l.strip().startswith("ExecStart") and "=" in l for t in l.split("=", 1)[1].split()]
        yollar = [os.path.normpath(t.replace("%h", str(home)).replace("${HOME}", str(home)).replace("$HOME", str(home))
                                   .replace("~", str(home), 1) if t.startswith(("~", "%h", "$")) else t) for t in yollar]
        if not any(y.startswith(kok + os.sep) for y in yollar):
            continue
        bak = backup_name(conf)
        say(f"{tag}eski suruklenen ayar (drop-in) kaldirildi, kopya zaten ~/.claude/bin'den kalkiyor: {posix(conf)} -> yedek {posix(bak)}")
        if not tag:
            shutil.copy2(conf, bak)
            conf.unlink()
        bulundu = True
    return bulundu


def eski_zamanlayici_dropin_temizle(dropin_dir, tag):
    """An old manual timer drop-in (`maliyet-gunluk.timer.d/*.conf`) that only re-sets OnCalendar= keeps the old daily time alive
    over the 2-hourly schedule of the shipped timer (a drop-in wins). Back it up (.yedek-<date>), remove it, say so in one line.
    A drop-in with anything else in it is left alone. Returns True when one was removed (the caller does the daemon-reload)."""
    bulundu = False
    for conf in sorted(dropin_dir.glob("*.conf")) if dropin_dir.is_dir() else []:
        try:
            satirlar = [l.strip() for l in conf.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip() and not l.strip().startswith(("#", ";"))]
        except OSError:
            continue
        if not any(l.startswith("OnCalendar") for l in satirlar) or any(l != "[Timer]" and not l.startswith("OnCalendar") for l in satirlar):
            continue
        bak = backup_name(conf)
        say(f"{tag}eski zamanlayici ayari (drop-in) kaldirildi, 2 saatlik takvimi eziyordu: {posix(conf)} -> yedek {posix(bak)}")
        if not tag:
            shutil.copy2(conf, bak)
            conf.unlink()
        bulundu = True
    return bulundu


def systemctl(args):
    try:
        r = gizli.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)
    except Exception as e:
        say(f"  [HATA] systemctl --user {' '.join(args)}: {e}")
        return False
    say(f"  [{'OK' if r.returncode == 0 else 'HATA'}] systemctl --user {' '.join(args)}")
    if r.returncode != 0 and r.stderr.strip():
        say("    " + r.stderr.strip().splitlines()[-1][:200])
    return r.returncode == 0


# ---------------------------------------------------------------- joining the usage ranking (--paylas)
def _paylas_modulu():
    """maliyet_paylas.py from this plugin (the one source of the name/plan rules); python -I does not add its folder."""
    if str(SRC / "bin") not in sys.path:
        sys.path.insert(0, str(SRC / "bin"))
    import maliyet_paylas
    return maliyet_paylas


def paylas_ad(kisi_arg):
    """(name, None) or (None, Turkish error): the exact kisiler.json value (the same comparison the job makes), from --kisi, else
    the way the job resolves it (CLAUDE_SHARED_KISI, the kisi file, git user.name). Shared by --paylas and --son-pencere."""
    mp = _paylas_modulu()
    listede = sorted({mp.temiz_ad(v) for v in mp.kisiler_oku(SRC.parent.parent).values()})
    if not listede:   # this clone carries no people list (the kit): read it from the team's shared repo (CLAUDE_SHARED_UZAK)
        try:
            mp.klon_hazirla(mp.klon_yolu(), mp.uzak_adres())
            listede = sorted({mp.temiz_ad(v) for v in mp.kisiler_oku(mp.klon_yolu()).values()})
        except RuntimeError as e:
            return None, "HATA: kisiler.json okunamadi: %s" % e
    if not listede:
        return None, "HATA: kisiler.json okunamadi ya da bos (%s)." % posix(SRC / "kisiler.json")
    ham = kisi_arg if kisi_arg else mp.kisi_adi()
    ad = mp.temiz_ad(ham)
    if ad not in listede:
        kaynak = "--kisi ile verilen" if kisi_arg else "bulunan (git user.name ya da ayar dosyasi)"
        return None, (f"HATA: {kaynak} ad '{ham}' kisiler.json listesinde yok. Gecerli adlar: {', '.join(listede)}.\n"
                      "Adiniz farkliysa listedeki adi verin: --kisi \"Ad Soyad\" (Windows: -Kisi \"Ad Soyad\"). "
                      "Listede degilseniz once plugins/kota-temposu/kisiler.json dosyasina ekleyip main'e commitleyin.")
    return ad, None


def paylas_ayar(kisi_arg, plan_arg):
    """(name, plan, None) or (None, None, Turkish error). The name comes from paylas_ad. The plan is
    --plan, else what the job would read today (CLAUDE_SHARED_PLAN, the plan file); 100 or 200."""
    mp = _paylas_modulu()
    ad, hata = paylas_ad(kisi_arg)
    if hata:
        return None, None, hata
    if plan_arg is None:
        plan = mp.plan_oku()
        if plan is None:
            return None, None, "HATA: plan belli degil. --plan 100 ya da --plan 200 verin (Windows: -Plan 100)."
    else:
        try:
            plan = int(str(plan_arg).strip().lstrip("$"))
        except ValueError:
            plan = None
        if plan not in mp.PLANLAR:
            return None, None, f"HATA: gecersiz plan '{plan_arg}': --plan {' ya da --plan '.join(str(x) for x in mp.PLANLAR)} verin."
    return ad, plan, None


def paylas_onkontrol(python, kisi, plan):
    """Exit code (0 ok) after the two checks, before anything is installed: the existing dry run (`maliyet --paylas --kuru`, from
    the plugin's own copy; it only builds and prints the row) and a real access probe (clone + `git push --dry-run`), because the
    dry run never reaches git."""
    env = dict(os.environ, CLAUDE_SHARED_KISI=kisi, CLAUDE_SHARED_PLAN=str(plan))
    say("Paylasim onkontrolu (maliyet --paylas --kuru; ilk taramada birkac dakika surebilir):")
    try:
        r = gizli.run([python, "-I", str(SRC / "bin" / "maliyet.py"), "--paylas", "--kuru"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=1800)
    except Exception as e:
        say(f"HATA: maliyet --paylas --kuru calistirilamadi: {e}")
        return EXIT_PAYLAS_OPSIYON
    if r.returncode != 0:
        say(f"HATA: maliyet --paylas --kuru basarisiz (cikis {r.returncode}). {(r.stderr.strip().splitlines() or [''])[-1][:200]}")
        return EXIT_PAYLAS_OPSIYON
    for satir in r.stdout.strip().splitlines()[-1:]:
        say("  satir: " + satir[:200])
    say("Ortak depoya erisim denetleniyor (git push --dry-run; hicbir sey yazilmaz):")
    ok, neden = _paylas_modulu().erisim_kontrol(kisi)
    if not ok:
        yok = "" if shutil.which("gh") else " (gh kurulu degil: https://cli.github.com)"
        say(f"HATA: ortak depo ayarlanmadi (CLAUDE_SHARED_UZAK veya ~/.config/claude-shared/uzak), push yetkisi yok ya da GitHub girisi yapilmamis{yok}.\n"
            f"  Ayrinti: {neden}\n"
            "  Once calistirin: gh auth login  ve  gh auth setup-git   (hesabin ortak depoya yazma yetkisi olmali),\n"
            "  sonra bu kurulum komutunu ayni seceneklerle tekrar calistirin. Hicbir sey kurulmadi.")
        return EXIT_PAYLAS_ERISIM
    say("  [OK] ad kisiler.json'da, push yetkisi var")
    proje_kapsami_uyarisi()
    return 0


def proje_kapsami_uyarisi():
    """Non-fatal: the weekly 'biten is' (finished work) column reads the GitHub Projects boards (GraphQL), which needs the gh token scope
    `project` (read:project is enough). Without it the share still works and the column shows a dash. No new scheduled step is needed: the
    existing 2-hourly job runs the weekly summary (maliyet_verim) from the clone, so the update arrives with the clone pull."""
    try:
        r = gizli.run(["gh", "auth", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        metin = (r.stdout or "") + (r.stderr or "")
    except Exception:
        return
    kapsam = [x for x in metin.splitlines() if "scopes" in x.lower()]
    if kapsam and not any("project" in x for x in kapsam):
        say("  UYARI: gh token'inda 'project' kapsami yok; siralamadaki 'biten is' sutunu '-' kalir. Duzeltmek icin: gh auth refresh -s read:project")
    elif kapsam:
        say("  [OK] gh token'inda 'project' kapsami var (haftalik biten is sayisi icin)")


def paylas_ayar_yaz(kisi, plan, tag):
    """~/.config/claude-shared/kisi and plan: exactly the files maliyet_paylas.py reads (its AYAR; Windows: USERPROFILE\\.config)."""
    dizin = Path(_paylas_modulu().AYAR)
    yaz_uret(dizin / "kisi", kisi + "\n", tag)
    yaz_uret(dizin / "plan", f"{plan}\n", tag)


def eski_paylasim_uyarisi(*dosyalar):
    """Re-installing without --paylas turns the job back into plain `--kaydet`; say so when the job on disk shared."""
    for d in dosyalar:
        try:
            if "--paylas" in d.read_text(encoding="utf-8", errors="replace"):
                say("UYARI: onceki kurulumda gunluk is paylasiyordu; --paylas verilmedigi icin artik yalniz kaydeder. "
                    "Siralamada kalmak icin kurulumu --paylas ile tekrar calistirin.")
                return
        except OSError:
            pass


def macos():
    """True on macOS (replaced by tests)."""
    return sys.platform == "darwin"


def plist_uret(python, script, claude, paylas, env=None):
    """launchd user agent for the record: same times and command as the systemd timer / Task Scheduler task (every 2 hours at :23,
    `maliyet.py --kaydet [--paylas]`, low priority). Deterministic bytes, so a re-install is a no-op."""
    import plistlib
    ortam = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}   # launchd's default PATH has no Homebrew
    ortam.update(env or {})
    d = {
        "Label": LAUNCHD_ETIKET,
        "ProgramArguments": [str(python), "-I", posix(script), "--kaydet"] + (["--paylas"] if paylas else []),
        "StartCalendarInterval": [{"Hour": h, "Minute": 23} for h in range(0, 24, 2)],   # a Mac asleep at one of them runs it on wake
        "Nice": 19,
        "LowPriorityIO": True,
        "ProcessType": "Background",
        "EnvironmentVariables": ortam,
        "StandardOutPath": posix(claude / "maliyet" / "launchd.log"),
        "StandardErrorPath": posix(claude / "maliyet" / "launchd.log"),
    }
    return plistlib.dumps(d).decode("utf-8")


def launchctl(args, sessiz=False):
    """launchctl <args>; returns True on exit 0. `sessiz`: a failure is normal (booting out an agent that is not loaded)."""
    try:
        r = gizli.run(["launchctl", *args], capture_output=True, text=True, timeout=60)
    except Exception as e:
        say(f"  [HATA] launchctl {' '.join(args)}: {e}")
        return False
    if not sessiz:
        say(f"  [{'OK' if r.returncode == 0 else 'HATA'}] launchctl {' '.join(args)}")
        if r.returncode != 0 and (r.stderr or r.stdout).strip():
            say("    " + (r.stderr or r.stdout).strip().splitlines()[-1][:200])
    return r.returncode == 0


def macos_gorev(a, python, claude, script, home, tag):
    """The macOS daily job: a launchd user agent. Written under any HOME; launchctl is only called for the real home."""
    real = gercek_ev()
    gercek = real is not None and home.resolve() == real.resolve()
    live = gercek and not a.whatif
    plist = home / "Library" / "LaunchAgents" / f"{LAUNCHD_ETIKET}.plist"
    domain = f"gui/{os.getuid()}"
    if a.remove:
        if live and plist.exists():
            launchctl(["bootout", f"{domain}/{LAUNCHD_ETIKET}"], sessiz=True)
        sil_dosya(plist, tag)
        say(f"{tag}Kayitli gecmis korunur: {posix(claude / 'maliyet')} (silmek isterseniz kendiniz silin)")
        return True
    env = {k: os.environ[k] for k in ("CLAUDE_CONFIG_DIR", "MALIYET_TZ", "MALIYET_FIYATLAR") if os.environ.get(k)}
    if not a.paylas:
        eski_paylasim_uyarisi(plist)
    yaz_uret(plist, plist_uret(python, script, claude, a.paylas, env), tag)
    if a.whatif:
        return True
    if not live:
        say("  HOME gercek ev dizini degil: launchctl calistirilmadi. Gercek kurulumda: "
            f"launchctl bootout {domain}/{LAUNCHD_ETIKET}; launchctl bootstrap {domain} {posix(plist)}")
        return True
    # idempotent: drop a loaded copy first (not loaded is normal), then load the current file
    launchctl(["bootout", f"{domain}/{LAUNCHD_ETIKET}"], sessiz=True)
    return launchctl(["bootstrap", domain, str(plist)])


def maliyet_ekstra(a, python, claude, tag, script):
    """Wrapper command + (Linux/systemd) user timer for the daily record, or their removal. Returns False on a failure.
    `script` is the maliyet.py they run: the copy in ~/.claude/bin, or the clone's one with --repodan."""
    home = Path.home()
    ok = True
    if a.windows:
        wrapper = claude / "bin" / "maliyet.cmd"   # in ~/.claude/bin in both modes: statusline.ps1 reads the python path from it
        target = f'"{posix(script)}"' if a.repodan else '"%~dp0maliyet.py"'
        body = f'@"{python}" {target} %*\r\n'
    else:
        wrapper = home / ".local" / "bin" / "maliyet"
        body = f'#!/bin/sh\n# Claude Code spending statistics (API-price equivalent, not a bill). Help: maliyet --help\nexec "{python}" "{posix(script)}" "$@"\n'
    if a.remove:
        sil_dosya(wrapper, tag)
        for pyc in sorted((claude / "bin" / "__pycache__").glob("maliyet*.pyc")):   # byte code Python left next to our scripts
            sil_dosya(pyc, tag)
    else:
        yaz_uret(wrapper, body, tag, executable=True)

    if a.windows:
        # per-user task, runs without any window (headless conhost, see tools/gizli.ps1), no admin rights; only for the account's REAL profile
        # (a scratch HOME never touches the real Task Scheduler, same rule as the systemd branch)
        real = gercek_ev()
        gercek = real is not None and home.resolve() == real.resolve()
        exe = str(python)   # tools/gizli.ps1 (New-GizliEylem) wraps it in a headless conhost
        wscript = str(script).replace("/", "\\")
        shell = a.shell or "powershell"
        if a.remove:
            if not gercek:
                say(f"{tag}HOME gercek profil klasoru degil: gorev zamanlayici '{GOREV}' gorevine dokunulmadi.")
            elif a.whatif:
                say(f"{tag}gorev zamanlayici: '{GOREV}' gorevi varsa silinir")
            else:
                ok &= gorev_calistir(shell, GOREV_SIL_PS, exe, wscript)
                say(f"  gorev zamanlayici: '{GOREV}' gorevi yoksa/silindi")
            say(f"{tag}Kayitli gecmis korunur: {posix(claude / 'maliyet')} (silmek isterseniz kendiniz silin)")
            return ok
        if not gercek:
            say(f"{tag}HOME gercek profil klasoru degil: gorev zamanlayici calistirilmadi (gercek kurulumda '{GOREV}' gorevi 2 saatte bir, :23'te kaydeder).")
        elif a.whatif:
            say(f"{tag}gorev zamanlayici: '{GOREV}' (kullanici gorevi, 2 saatte bir :23, dusuk oncelik, yonetici hakki gerekmez): {exe} -I {wscript} --kaydet{' --paylas' if a.paylas else ''}")
        else:
            if not a.paylas and gorev_paylasiyor(shell):
                say("UYARI: onceki kurulumda gunluk is paylasiyordu; --paylas verilmedigi icin artik yalniz kaydeder. "
                    "Siralamada kalmak icin kurulumu --paylas ile tekrar calistirin.")
            if gorev_calistir(shell, GOREV_KAYIT_PS, exe, wscript, paylas=a.paylas):
                say(f"  [OK] gorev zamanlayici: '{GOREV}' (2 saatte bir :23, bilgisayar kapaliysa acilista, dusuk oncelik"
                    f"{', --kaydet --paylas' if a.paylas else ''})")
            else:
                ok = False
        return ok
    if macos():
        return ok & macos_gorev(a, python, claude, script, home, tag)
    if not (sys.platform.startswith("linux") and shutil.which("systemctl")):
        if not a.remove:
            # no timer on Linux without systemd: the status-line background refresh (maliyet --durum-yenile, at most every
            # 5 minutes while Claude Code is in use) writes the same daily and hourly records, so nothing else is needed
            say(f"{tag}zamanlayici yok (systemd'siz Linux): gunluk kaydi durum cubugunun arka plan yenilemesi yazar "
                f"(Claude Code kullanilirken en cok 5 dakikada bir). Istege bagli crontab satiri: 23 */2 * * * \"{python}\" \"{posix(script)}\" --kaydet{' --paylas' if a.paylas else ''}")
        return ok

    real = gercek_ev()
    gercek = real is not None and home.resolve() == real.resolve()
    # XDG_CONFIG_HOME is honoured only for the real home: a test run with a scratch HOME must never reach the real unit directory
    base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") if gercek else home / ".config"
    udir = base / "systemd" / "user"
    svc, tmr = udir / f"{UNIT}.service", udir / f"{UNIT}.timer"
    live = gercek and not a.whatif
    if a.remove:
        if live and (svc.exists() or tmr.exists()):
            systemctl(["disable", "--now", f"{UNIT}.timer"])
        sil_dosya(svc, tag)
        sil_dosya(tmr, tag)
        if live:
            systemctl(["daemon-reload"])
        say(f"{tag}Kayitli gecmis korunur: {posix(claude / 'maliyet')} (silmek isterseniz kendiniz silin)")
        return ok
    eski_a = eski_dropin_temizle(udir / f"{UNIT}.service.d", claude, home, tag)
    eski_b = eski_zamanlayici_dropin_temizle(udir / f"{UNIT}.timer.d", tag)
    if (eski_a or eski_b) and live:
        systemctl(["daemon-reload"])
    env = "".join(f'Environment="{k}={os.environ[k]}"\n' for k in ("CLAUDE_CONFIG_DIR", "MALIYET_TZ", "MALIYET_FIYATLAR") if os.environ.get(k))
    tpl = (SRC / "systemd" / f"{UNIT}.service.in").read_text(encoding="utf-8")
    svc_text = tpl.replace("@EXEC@", f"{unit_q(python)} {unit_q(posix(script))}").replace("@ENV@", env).replace("@PAYLAS@", " --paylas" if a.paylas else "")
    if not a.paylas:
        eski_paylasim_uyarisi(svc)
    yaz_uret(svc, svc_text, tag)
    yaz_uret(tmr, (SRC / "systemd" / f"{UNIT}.timer").read_text(encoding="utf-8"), tag)
    if a.whatif:
        return ok
    if live:
        ok &= systemctl(["daemon-reload"])
        ok &= systemctl(["enable", "--now", f"{UNIT}.timer"])
    else:
        say("  HOME gercek ev dizini degil: systemctl calistirilmadi. Gercek kurulumda: "
            f"systemctl --user daemon-reload && systemctl --user enable --now {UNIT}.timer")
    return ok

# ---------------------------------------------------------------- "son pencere" (kur.sh --son-pencere)
SP_GOREV_KAYIT_PS = r"""
$ErrorActionPreference = 'Stop'
. ([scriptblock]::Create((Get-Content -Raw -LiteralPath $env:KUR_GIZLI_PS1)))   # tools/gizli.ps1: the one hidden-task registration (read as text: works under any execution policy)
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 8)
Register-GizliGorev -Ad $env:KUR_GOREV_AD -Exe $env:KUR_GOREV_EXE -Argument ($env:KUR_GOREV_ON + ' "' + $env:KUR_GOREV_BETIK + '" ' + $env:KUR_SP_TETIK) -Trigger $t -Settings $s -Aciklama 'Claude Code son-pencere (kota-temposu, son-pencere tetik, 5 dakikada bir): son pencere, 5 saatlik devam alarmi, token bitirme modu'
"""


def sp_tetik(a):
    """The arguments of `son-pencere tetik` the timer runs: the modes this install opted into (--son-pencere = pencere, --devam-alarmi =
    devam). Burn mode needs no entry: it runs whenever a developer switches it on and the timer exists."""
    kipler = (["pencere"] if a.son_pencere else []) + (["devam"] if getattr(a, "devam_alarmi", False) else [])
    if kipler in ([], ["pencere"]):   # the last window alone stays a bare `tetik`: a rolled-back script still understands it
        return "tetik"
    return "tetik --kipler " + ",".join(kipler)


def sp_gorev_calistir(shell, ps_betik, python, script, tetik="tetik"):
    """Run a Task Scheduler snippet for the CURRENT user only, for the task SP_GOREV (no admin). Replaced by tests."""
    env = dict(os.environ, KUR_GOREV_AD=SP_GOREV, **gorev_env(python), KUR_GOREV_BETIK=str(script), KUR_SP_TETIK=tetik)
    try:
        r = gizli.run([shell, "-NoProfile", "-NonInteractive", "-Command", ps_betik], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=120)
    except Exception as e:
        say(f"  [HATA] gorev zamanlayici: {e}")
        return False
    if r.returncode != 0:
        say("  [HATA] gorev zamanlayici (cikis %d)" % r.returncode)
        if (r.stderr or r.stdout).strip():
            say("    " + (r.stderr or r.stdout).strip().splitlines()[-1][:200])
    return r.returncode == 0


def sp_sarmal(a, claude):
    """The `son-pencere` command file: ~/.local/bin/son-pencere (POSIX) or ~/.claude/bin/son-pencere.cmd (Windows)."""
    return claude / "bin" / "son-pencere.cmd" if a.windows else Path.home() / ".local" / "bin" / "son-pencere"


def ortam_satirlari(anahtar):
    """systemd `Environment=` lines for the variables in `anahtar` that the installing shell has set."""
    return "".join('Environment="%s=%s"\n' % (k, os.environ[k].replace("%", "%%").replace('"', '\\"')) for k in anahtar if os.environ.get(k))


def sp_ortam():
    """Environment lines for the systemd unit, taken from the installing shell: a user timer does not see the login session's PATH or
    SSH agent. Only what the coordinator needs (claude, gh; git goes over https with the gh credential helper, see the template), plus the
    son-pencere options when set."""
    anahtar = ("PATH", "SSH_AUTH_SOCK", "CLAUDE_CONFIG_DIR", "GH_CONFIG_DIR", "XDG_CONFIG_HOME", "PACE_HEDEF_SAAT", "SON_PENCERE_MODEL",
               "SON_PENCERE_ARGS", "SON_PENCERE_MAX_TURNS", "CLAUDE_SHARED_KISI", "CLAUDE_SHARED_KOK")
    satirlar = [ortam_satirlari(anahtar)]
    exe = shutil.which("claude")
    if exe:
        satirlar.append('Environment="SON_PENCERE_CLAUDE=%s"\n' % exe.replace("%", "%%"))
    return "".join(satirlar)


def son_pencere_onkontrol(kisi):
    """Exit code (0 ok): claude and gh exist, and the shared log is writable by this person (clone + git push --dry-run) -- the same
    probe --paylas uses, against the son-pencere clone."""
    eksik = [x for x in ("claude", "gh", "git") if not shutil.which(x)]
    if eksik:
        say(f"HATA: bulunamadi: {', '.join(eksik)}. son-pencere `claude`, `gh` ve `git` kullanir. Hicbir sey kurulmadi.")
        return EXIT_PAYLAS_OPSIYON
    if os.name != "nt" and not shutil.which("tmux"):
        say("UYARI: tmux bulunamadi. Canli bir oturum varsa is ona verilir; yoksa gorunur yedek oturum (tmux) baslatilamaz. Kurmaniz onerilir (pacman -S tmux / apt install tmux).")
    say("Ortak kayit erisimi denetleniyor (git push --dry-run; hicbir sey yazilmaz):")
    ok, neden = _paylas_modulu().erisim_kontrol(kisi, klon=os.path.join(str(Path.home()), ".cache", "claude-shared-son-pencere"))
    if not ok:
        say(f"HATA: ortak depo claude-shared'e push yetkisi yok ya da GitHub girisi yapilmamis.\n  Ayrinti: {neden}\n"
            "  Once calistirin: gh auth login  ve  gh auth setup-git, sonra bu komutu tekrarlayin. Hicbir sey kurulmadi.")
        return EXIT_PAYLAS_ERISIM
    say("  [OK] ad kisiler.json'da, push yetkisi var")
    return 0


def son_pencere_ekstra(a, python, claude, tag, script):
    """The `son-pencere` command, the 15-minute timer (Linux/systemd user timer, Windows per-user task) or their removal; macOS gets
    the command only. `script` is the son_pencere.py they run (the copy in ~/.claude/bin, or the clone's with --repodan)."""
    home = Path.home()
    ok = True
    wrapper = sp_sarmal(a, claude)
    if a.windows:
        target = f'"{posix(script)}"' if a.repodan else '"%~dp0son_pencere.py"'
        body = f'@"{python}" -I {target} %*\r\n'
    else:
        body = f'#!/bin/sh\n# Claude Code "son pencere" (last quota window). Help: son-pencere --help\nexec "{python}" -I "{posix(script)}" "$@"\n'
    if a.remove:
        sil_dosya(wrapper, tag)
        for pyc in sorted((claude / "bin" / "__pycache__").glob("son_pencere*.pyc")):
            sil_dosya(pyc, tag)
    else:
        yaz_uret(wrapper, body, tag, executable=True)

    real = gercek_ev()
    gercek = real is not None and home.resolve() == real.resolve()
    if a.windows:
        exe = str(python)   # tools/gizli.ps1 (New-GizliEylem) wraps it in a headless conhost
        wscript = str(script).replace("/", "\\")
        shell = a.shell or "powershell"
        if a.remove:
            if not gercek:
                say(f"{tag}HOME gercek profil klasoru degil: '{SP_GOREV}' gorevine dokunulmadi.")
            elif a.whatif:
                say(f"{tag}gorev zamanlayici: '{SP_GOREV}' gorevi varsa silinir")
            else:
                ok &= sp_gorev_calistir(shell, GOREV_SIL_PS, exe, wscript)
                say(f"  gorev zamanlayici: '{SP_GOREV}' gorevi yoksa/silindi")
        elif not gercek:
            say(f"{tag}HOME gercek profil klasoru degil: gorev zamanlayici calistirilmadi (gercek kurulumda '{SP_GOREV}' gorevi her 5 dakikada bir kapiyi kontrol eder).")
        elif a.whatif:
            say(f"{tag}gorev zamanlayici: '{SP_GOREV}' (kullanici gorevi, her 5 dakikada bir, yonetici hakki gerekmez): {exe} -I {wscript} {sp_tetik(a)}")
        elif sp_gorev_calistir(shell, SP_GOREV_KAYIT_PS, exe, wscript, sp_tetik(a)):
            say(f"  [OK] gorev zamanlayici: '{SP_GOREV}' (her 5 dakikada bir; {sp_tetik(a)}: yalniz kapisi acik kipte is baslatir)")
        else:
            ok = False
        return ok
    if macos():
        say(f"{tag}macOS: zamanlayici kurulmadi (yalniz `son-pencere` komutu). Elle: launchd ya da cron ile 5 dakikada bir `son-pencere {sp_tetik(a)}`.")
        return ok
    if not (sys.platform.startswith("linux") and shutil.which("systemctl")):
        if not a.remove:
            say(f"{tag}zamanlayici yok (systemd'siz Linux). Istege bagli crontab satiri: */5 * * * * \"{python}\" -I \"{posix(script)}\" {sp_tetik(a)}")
        return ok
    base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") if gercek else home / ".config"
    udir = base / "systemd" / "user"
    svc, tmr = udir / f"{SP_UNIT}.service", udir / f"{SP_UNIT}.timer"
    live = gercek and not a.whatif
    if a.remove:
        if live and (svc.exists() or tmr.exists()):
            systemctl(["disable", "--now", f"{SP_UNIT}.timer"])
        sil_dosya(svc, tag)
        sil_dosya(tmr, tag)
        if live:
            systemctl(["daemon-reload"])
        say(f"{tag}Ortak kayit (raporlar/son-pencere/) ve yerel gunluk {posix(claude / 'son-pencere')} korunur.")
        return ok
    tpl = (SRC / "systemd" / f"{SP_UNIT}.service.in").read_text(encoding="utf-8")
    svc_text = tpl.replace("@EXEC@", f"{unit_q(python)} -I {unit_q(posix(script))}").replace("@TETIK@", sp_tetik(a)).replace("@ENV@", sp_ortam()).replace("@GITORG@", sp_git_org())
    yaz_uret(svc, svc_text, tag)
    yaz_uret(tmr, (SRC / "systemd" / f"{SP_UNIT}.timer").read_text(encoding="utf-8"), tag)
    if a.whatif:
        return ok
    if live:
        ok &= systemctl(["daemon-reload"])
        ok &= systemctl(["enable", "--now", f"{SP_UNIT}.timer"])
    else:
        say("  HOME gercek ev dizini degil: systemctl calistirilmadi. Gercek kurulumda: "
            f"systemctl --user daemon-reload && systemctl --user enable --now {SP_UNIT}.timer")
    return ok


SG_GOREV_KAYIT_PS = r"""
$ErrorActionPreference = 'Stop'
. ([scriptblock]::Create((Get-Content -Raw -LiteralPath $env:KUR_GIZLI_PS1)))   # tools/gizli.ps1: the one hidden-task registration (read as text: works under any execution policy)
$t = New-ScheduledTaskTrigger -Once -At ((Get-Date).Date.AddMinutes([int]$env:KUR_SG_DAKIKA)) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$s.Priority = 8
Register-GizliGorev -Ad $env:KUR_GOREV_AD -Exe $env:KUR_GOREV_EXE -Argument ($env:KUR_GOREV_ON + ' "' + $env:KUR_GOREV_BETIK + '" poll') -Trigger $t -Settings $s -Aciklama 'Slack gelen kutusu (claude-shared slack-gelen: token-free, her 5 dakikada bir, model cagrisi yok)'
"""


def sg_windows(a, python, claude, tag, home, gercek):
    """Windows part of the Slack inbox poller: the `slack-gelen.cmd` read command in ~/.claude/bin (a wrapper, no symlinks without admin)
    and a per-user Task Scheduler task running the clone's gelen.py poll every 5 minutes without any window (headless conhost, see tools/gizli.ps1), or their
    removal. A scratch HOME never reaches the real Task Scheduler."""
    install = a.slack_gelen and not a.remove
    cmd = claude / "bin" / "slack-gelen.cmd"
    if install:
        yaz_uret(cmd, f'@"{python}" -I "{posix(SG_DIR / "gelen.py")}" read %*\r\n', tag)
    else:
        sil_dosya(cmd, tag)
    exe = str(python)   # tools/gizli.ps1 (New-GizliEylem) wraps it in a headless conhost
    wscript = str(SG_GELEN).replace("/", "\\")
    shell = a.shell or "powershell"
    if not gercek:
        say(f"{tag}HOME gercek profil klasoru degil: '{SG_GOREV}' gorevine dokunulmadi.")
        return True
    if not install:
        if a.whatif:
            say(f"{tag}gorev zamanlayici: '{SG_GOREV}' gorevi varsa silinir")
            return True
        ok = gorev_calistir(shell, GOREV_SIL_PS, exe, wscript, ad=SG_GOREV)
        say(f"  gorev zamanlayici: '{SG_GOREV}' gorevi yoksa/silindi")
        return ok
    if a.whatif:
        say(f"{tag}gorev zamanlayici: '{SG_GOREV}' (kullanici gorevi, her 5 dakikada bir, :{SG_DAKIKA:02d}'te baslar, yonetici hakki gerekmez): {exe} -I {wscript} poll")
        return True
    if gorev_calistir(shell, SG_GOREV_KAYIT_PS, exe, wscript, ad=SG_GOREV):
        say(f"  [OK] gorev zamanlayici: '{SG_GOREV}' (her 5 dakikada bir, :{SG_DAKIKA:02d}'ten baslar, bilgisayar kapaliysa acilista)")
        return True
    return False


def sg_link(claude, tag, remove):
    """~/.claude/bin/slack-gelen -> the clone's slack-gelen (a link, never a copy)."""
    dst, src = claude / "bin" / "slack-gelen", SG_DIR / "slack-gelen"
    if remove:
        if dst.is_symlink():
            sil_dosya(dst, tag)
        return
    if dst.is_symlink() and Path(os.readlink(dst)) == src:
        say(f"{tag}ayni: {posix(dst)}")
        return
    say(f"{tag}baglanti: {posix(dst)} -> {posix(src)}")
    if not tag:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        dst.symlink_to(src)


def sg_plist(python):
    import plistlib
    d = {"Label": SG_ETIKET, "ProgramArguments": [str(python), "-I", posix(SG_GELEN), "poll"], "StartInterval": 300,
         "RunAtLoad": True, "Nice": 10, "ProcessType": "Background",
         "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
         "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}
    return plistlib.dumps(d).decode("utf-8")


def sg_unit_yaz(path, text, tag):
    """Write a unit file; a symlink left by `systemctl link` (into the clone) is replaced, never written through."""
    if path.is_symlink() and not tag:
        path.unlink()
    return yaz_uret(path, text, tag)


def slack_gelen_ekstra(a, python, claude, tag):
    """Optional token-free Slack inbox poller (kur.sh --slack-gelen / kur.ps1 -SlackGelen): a 5-minute systemd user timer (Linux), launchd
    agent (macOS) or Task Scheduler task (Windows) that runs the clone's gelen.py poll, plus the ~/.claude/bin/slack-gelen command. Removal with --kaldir. Same real-home
    rule as the other jobs: a scratch HOME never reaches the real scheduler."""
    home = Path.home()
    ok = True
    install = a.slack_gelen and not a.remove
    real = gercek_ev()
    gercek = real is not None and home.resolve() == real.resolve()
    live = gercek and not a.whatif
    if install and not (SG_GELEN.is_file() and (SG_DIR / "systemd" / f"{SG_UNIT}.service").is_file()):
        say(f"{tag}HATA: slack-gelen dosyalari bu klonda yok: {posix(SG_DIR)} (agent-crew-kit bu araclari icermez: tools/slack-triage/ ekibin kendi ekidir; README, Optional team add-ons)")
        return False
    if a.windows:
        return sg_windows(a, python, claude, tag, home, gercek)
    sg_link(claude, tag, not install)
    if macos():
        plist = home / "Library" / "LaunchAgents" / f"{SG_ETIKET}.plist"
        domain = f"gui/{os.getuid()}"
        if not install:
            if live and plist.exists():
                launchctl(["bootout", f"{domain}/{SG_ETIKET}"], sessiz=True)
            sil_dosya(plist, tag)
            return ok
        yaz_uret(plist, sg_plist(python), tag)
        if a.whatif:
            return ok
        if not live:
            say(f"  HOME gercek ev dizini degil: launchctl calistirilmadi. Gercek kurulumda: launchctl bootstrap {domain} {posix(plist)}")
            return ok
        launchctl(["bootout", f"{domain}/{SG_ETIKET}"], sessiz=True)
        return launchctl(["bootstrap", domain, str(plist)])
    if not (sys.platform.startswith("linux") and shutil.which("systemctl")):
        if install:
            say(f"{tag}slack-gelen: zamanlayici kurulmadi. Istege bagli crontab satiri: */5 * * * * \"{python}\" -I \"{posix(SG_GELEN)}\" poll")
        return ok
    base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") if gercek else home / ".config"
    udir = base / "systemd" / "user"
    svc, tmr = udir / f"{SG_UNIT}.service", udir / f"{SG_UNIT}.timer"
    if not install:
        if live and (svc.exists() or svc.is_symlink() or tmr.exists() or tmr.is_symlink()):
            systemctl(["disable", "--now", f"{SG_UNIT}.timer"])
        for f in (svc, tmr):
            if f.is_symlink() and not tag:
                f.unlink()
            else:
                sil_dosya(f, tag)
        if live:
            systemctl(["daemon-reload"])
        return ok
    tpl = (SG_DIR / "systemd" / f"{SG_UNIT}.service").read_text(encoding="utf-8")
    svc_text = re.sub(r"(?m)^ExecStart=.*$", lambda m: f"ExecStart={unit_q(python)} -I {unit_q(posix(SG_GELEN))} poll", tpl)
    sg_unit_yaz(svc, svc_text, tag)
    sg_unit_yaz(tmr, (SG_DIR / "systemd" / f"{SG_UNIT}.timer").read_text(encoding="utf-8"), tag)
    if a.whatif:
        return ok
    if live:
        ok &= systemctl(["daemon-reload"])
        ok &= systemctl(["enable", "--now", f"{SG_UNIT}.timer"])
    else:
        say("  HOME gercek ev dizini degil: systemctl calistirilmadi. Gercek kurulumda: "
            f"systemctl --user daemon-reload && systemctl --user enable --now {SG_UNIT}.timer")
    return ok


def arac_ekstra(a, python):
    """The tool check (tools/arac_kontrol.py, the one list of the command-line tools agents need): every install prints the report and, on
    Windows, writes the python3 shim (~/.local/bin/python3[.cmd]) when no real python3 is on PATH; --araclar is the consent flag that also
    installs the missing tools (winget / apt-get / brew, pip --user). --kaldir removes the shim. A missing optional tool never fails the install."""
    if str(ARAC_KONTROL.parent) not in sys.path:
        sys.path.insert(0, str(ARAC_KONTROL.parent))
    import arac_kontrol
    if a.remove:
        return arac_kontrol.python3_shim(a.python, kaldir=True, whatif=a.whatif, yaz=say)
    arac_kontrol.python3_shim(a.python, whatif=a.whatif, yaz=say)   # a skipped / foreign-file shim is a warning, not a failed install
    return arac_kontrol.calis(a.python, kur=a.araclar, whatif=a.whatif, yaz=say) == 0


def gh_sinir_ekstra(a):
    """The `gh` shim (~/.local/bin/gh, on Windows also gh.ps1 and a PATH line in the pwsh profile) in front of the real gh:
    tools/gh_sinir.py of this clone limits concurrency, spaces writes and retries secondary-limit answers machine-wide; the shim
    falls back to the real gh when the clone is gone. A scratch HOME (tests, --whatif trials elsewhere) never touches the pwsh
    profile: gh_sinir.kur only follows the default target."""
    if str(GH_SINIR.parent) not in sys.path:
        sys.path.insert(0, str(GH_SINIR.parent))
    import gh_sinir
    return gh_sinir.kur(kaldir=a.remove, whatif=a.whatif) == 0


ST_GOREV = "slack-triage"                      # Windows Task Scheduler task name (kur.ps1 -SlackTriage)
ST_UNIT = "slack-triage"                       # systemd user unit (kur.sh --slack-triage)
ST_CALISTIR = SG_DIR / "calistir.sh"
ST_GOREV_PY = SG_DIR / "gorev.py"

ST_GOREV_KAYIT_PS = r"""
$ErrorActionPreference = 'Stop'
. ([scriptblock]::Create((Get-Content -Raw -LiteralPath $env:KUR_GIZLI_PS1)))   # tools/gizli.ps1: the one hidden-task registration (read as text: works under any execution policy)
$t = New-ScheduledTaskTrigger -Once -At ((Get-Date).Date) -RepetitionInterval (New-TimeSpan -Minutes ([int]$env:KUR_ST_DAKIKA)) -RepetitionDuration (New-TimeSpan -Days 3650)
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 50)
$s.Priority = 8
Register-GizliGorev -Ad $env:KUR_GOREV_AD -Exe $env:KUR_GOREV_EXE -Argument ($env:KUR_GOREV_ON + ' "' + $env:KUR_GOREV_BETIK + '"') -Trigger $t -Settings $s -Aciklama 'Slack/e-posta talep triyaji (claude-shared slack-triage: birkac dakikada bir ucuz pencere kontrolu, asil is yalniz bu makinenin sirasi geldiginde)'
"""


def st_ayar(conf):
    """ARALIK_MIN / ARALIK_MAX (minutes) from the triage config; defaults 2 and 4."""
    d = {"ARALIK_MIN": 2, "ARALIK_MAX": 4}
    try:
        for l in conf.read_text(encoding="utf-8-sig").splitlines():
            m = re.match(r"^\s*(ARALIK_MIN|ARALIK_MAX)=([0-9]+)", l)
            if m:
                d[m.group(1)] = max(1, int(m.group(2)))
    except OSError:
        pass
    d["ARALIK_MAX"] = max(d["ARALIK_MAX"], d["ARALIK_MIN"])
    return d


def slack_triage_ekstra(a, python, tag):
    """Optional Slack/mail triage runner (kur.sh --slack-triage / kur.ps1 -SlackTriage). Every developer's machine may run it; one
    owns triage until a request/ack handover (owner lease, tools/slack-triage/pencere.py) and runs one tick per window. Installs, pointing at the clone:
    a per-user Task Scheduler task running gorev.py every ARALIK_MIN minutes (it adds the random spread and starts Git Bash) on
    Windows, a systemd user timer with RandomizedDelaySec on Linux. The config ~/.config/slack-triage/config is COPIED from
    config.example when missing, because it holds per-machine settings (MAIL_ENABLED etc.); an existing one is never touched.
    --kaldir removes the task/timer and keeps the config and the state."""
    home = Path.home()
    install = a.slack_triage and not a.remove
    real = gercek_ev()
    gercek = real is not None and home.resolve() == real.resolve()
    live = gercek and not a.whatif
    if install and not (ST_CALISTIR.is_file() and ST_GOREV_PY.is_file()):
        say(f"{tag}HATA: slack-triage dosyalari bu klonda yok: {posix(SG_DIR)} (agent-crew-kit bu araclari icermez: tools/slack-triage/ ekibin kendi ekidir; README, Optional team add-ons)")
        return False
    conf = Path(os.environ.get("SLACK_TRIAGE_CONFIG") or home / ".config" / "slack-triage" / "config")
    if install and not conf.exists():
        say(f"{tag}kopyala (makineye ozel ayar): {posix(conf)} <- config.example")
        if not a.whatif:
            conf.parent.mkdir(parents=True, exist_ok=True)
            conf.write_bytes((SG_DIR / "config.example").read_bytes())
    ay = st_ayar(conf)
    if a.windows:
        exe = str(python)   # tools/gizli.ps1 (New-GizliEylem) wraps it in a headless conhost
        wscript = str(ST_GOREV_PY).replace("/", "\\")
        shell = a.shell or "powershell"
        if not gercek:
            say(f"{tag}HOME gercek profil klasoru degil: '{ST_GOREV}' gorevine dokunulmadi.")
            return True
        if not install:
            if a.whatif:
                say(f"{tag}gorev zamanlayici: '{ST_GOREV}' gorevi varsa silinir")
                return True
            ok = gorev_calistir(shell, GOREV_SIL_PS, exe, wscript, ad=ST_GOREV)
            say(f"  gorev zamanlayici: '{ST_GOREV}' gorevi yoksa/silindi (ayar ve durum dosyalari korunur)")
            return ok
        metin = f"'{ST_GOREV}' (her {ay['ARALIK_MIN']} dakikada bir, gorev.py {ay['ARALIK_MAX'] - ay['ARALIK_MIN']} dakikaya kadar rastgele bekler): {exe} -I {wscript}"
        if a.whatif:
            say(f"{tag}gorev zamanlayici: {metin}")
            return True
        if gorev_calistir(shell, ST_GOREV_KAYIT_PS, exe, wscript, ad=ST_GOREV, ek={"KUR_ST_DAKIKA": str(ay["ARALIK_MIN"])}):
            say(f"  [OK] gorev zamanlayici: {metin}")
            return True
        return False
    if not (sys.platform.startswith("linux") and shutil.which("systemctl")):
        if install:
            say(f"{tag}slack-triage: zamanlayici kurulmadi. Istege bagli crontab satiri: */{ay['ARALIK_MIN']} * * * * bash \"{posix(ST_CALISTIR)}\"")
        return True
    base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") if gercek else home / ".config"
    udir = base / "systemd" / "user"
    svc, tmr = udir / f"{ST_UNIT}.service", udir / f"{ST_UNIT}.timer"
    if not install:
        if live and (svc.exists() or svc.is_symlink() or tmr.exists() or tmr.is_symlink()):
            systemctl(["disable", "--now", f"{ST_UNIT}.timer"])
        for f in (svc, tmr):
            if f.is_symlink() and not tag:
                f.unlink()
            else:
                sil_dosya(f, tag)
        if live:
            systemctl(["daemon-reload"])
        return True
    svc_text = re.sub(r"(?m)^ExecStart=.*$", lambda m: f"ExecStart=/bin/bash {unit_q(posix(ST_CALISTIR))}",
                      (SG_DIR / "systemd" / f"{ST_UNIT}.service").read_text(encoding="utf-8"))
    tmr_text = (SG_DIR / "systemd" / f"{ST_UNIT}.timer").read_text(encoding="utf-8")
    tmr_text = re.sub(r"(?m)^OnUnitActiveSec=.*$", f"OnUnitActiveSec={ay['ARALIK_MIN']}min", tmr_text)
    tmr_text = re.sub(r"(?m)^RandomizedDelaySec=.*$", f"RandomizedDelaySec={ay['ARALIK_MAX'] - ay['ARALIK_MIN']}min", tmr_text)
    sg_unit_yaz(svc, svc_text, tag)
    sg_unit_yaz(tmr, tmr_text, tag)
    if a.whatif:
        return True
    if live:
        return systemctl(["daemon-reload"]) & systemctl(["enable", "--now", f"{ST_UNIT}.timer"])
    say("  HOME gercek ev dizini degil: systemctl calistirilmadi. Gercek kurulumda: "
        f"systemctl --user daemon-reload && systemctl --user enable --now {ST_UNIT}.timer")
    return True


BAYAT_GH_VAULT = os.environ.get("BAYAT_GH_VAULT", "")   # config key: Vault path of the GitHub token for the stale-branch job, e.g. kv/<team>/github#token
BAYAT_UNIT = "bayat"                            # systemd user unit (kur.sh --bayat): the daily stale-branch job, always-on host only


def sp_git_org():
    """The org-scoped ssh->https rewrite of the son-pencere unit (config key GH_ORG); empty when no org is configured."""
    org = _paylas_modulu().github_org()
    if not org:
        return ""
    return (f'Environment="GIT_CONFIG_COUNT=1"\nEnvironment="GIT_CONFIG_KEY_0=url.https://github.com/{org}/.insteadOf"\n'
            f'Environment="GIT_CONFIG_VALUE_0=git@github.com:{org}/"\n')


def bayat_ekstra(a, python, tag):
    """Daily stale-branch cleanup + report (tools/slack-triage/bayat.py) as a systemd user timer on an always-on Linux host (agent-host),
    Mon-Fri 09:30 Europe/Istanbul, Persistent so a missed run catches up. The units live in tools/slack-triage/systemd/; ExecStart is
    rewritten to this clone's bayat.py (use the source, no copy). --kaldir removes them. Linux only (no Windows/macOS scheduler)."""
    home = Path.home()
    install = a.bayat and not a.remove
    real = gercek_ev()
    gercek = real is not None and home.resolve() == real.resolve()
    live = gercek and not a.whatif
    if not (sys.platform.startswith("linux") and shutil.which("systemctl")):
        if install:
            say(f"{tag}bayat: yalniz Linux systemd (agent-host) icin; bu makinede kurulmadi.")
        return True
    udir = (Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") if gercek else home / ".config") / "systemd" / "user"
    svc, tmr = udir / f"{BAYAT_UNIT}.service", udir / f"{BAYAT_UNIT}.timer"
    if not install:
        if live and (svc.exists() or svc.is_symlink() or tmr.exists() or tmr.is_symlink()):
            systemctl(["disable", "--now", f"{BAYAT_UNIT}.timer"])
        for f in (svc, tmr):
            sil_dosya(f, tag)
        if live and (svc.exists() or tmr.exists()):
            systemctl(["daemon-reload"])
        return True
    kaynak = SG_DIR / "systemd"
    if not (kaynak / f"{BAYAT_UNIT}.service").is_file():
        say(f"{tag}HATA: bayat birimleri bu klonda yok: {posix(kaynak)} (agent-crew-kit bu araclari icermez: tools/slack-triage/ ekibin kendi ekidir; README, Optional team add-ons)")
        return False
    bayat_py = SG_DIR / "bayat.py"
    kasa_py = KLON / "tools" / "kasa.py"
    # GitHub identity: explicit Vault token (GH_TOKEN via kasa.py run, never printed), not the host's ambient `gh` login.
    svc_text = re.sub(r"(?m)^ExecStart=.*$", lambda m: f"ExecStart={unit_q(str(python))} -X utf8 -I {unit_q(posix(kasa_py))} run --env GH_TOKEN={BAYAT_GH_VAULT} -- {unit_q(str(python))} -X utf8 -I {unit_q(posix(bayat_py))} gunluk %h/.local/state/slack-triage %h/.local/state/slack-triage.log",
                      (kaynak / f"{BAYAT_UNIT}.service").read_text(encoding="utf-8"))
    sg_unit_yaz(svc, svc_text, tag)
    sg_unit_yaz(tmr, (kaynak / f"{BAYAT_UNIT}.timer").read_text(encoding="utf-8"), tag)
    if a.whatif:
        return True
    if live:
        return systemctl(["daemon-reload"]) & systemctl(["enable", "--now", f"{BAYAT_UNIT}.timer"])
    say(f"  HOME gercek ev dizini degil: systemctl calistirilmadi. Gercek kurulumda: systemctl --user daemon-reload && systemctl --user enable --now {BAYAT_UNIT}.timer")
    return True


def kayip_ekstra(a, python, tag):
    """The daily lost-work job (kayip_is.py --push-unpushed from this clone): a Windows per-user task, a systemd user timer on Linux, a
    crontab hint elsewhere; or its removal. Same real-home rule as the cost job: a scratch HOME never reaches the real scheduler."""
    home = Path.home()
    ok = True
    real = gercek_ev()
    gercek = real is not None and home.resolve() == real.resolve()
    if a.windows:
        exe = str(python)   # tools/gizli.ps1 (New-GizliEylem) wraps it in a headless conhost
        wscript = str(KAYIP_GIRIS).replace("/", "\\")
        shell = a.shell or "powershell"
        if not gercek:
            say(f"{tag}HOME gercek profil klasoru degil: '{KAYIP_GOREV}' gorevine dokunulmadi.")
        elif a.whatif:
            say(f"{tag}gorev zamanlayici: '{KAYIP_GOREV}' " + ("gorevi varsa silinir" if a.remove else
                f"(kullanici gorevi, her gun 17:30, bilgisayar kapaliysa acilista): {exe} -I {wscript}"))
        elif a.remove:
            ok &= gorev_calistir(shell, GOREV_SIL_PS, exe, wscript, ad=KAYIP_GOREV)
            say(f"  gorev zamanlayici: '{KAYIP_GOREV}' gorevi yoksa/silindi")
        elif gorev_calistir(shell, KAYIP_GOREV_KAYIT_PS, exe, wscript, ad=KAYIP_GOREV):
            say(f"  [OK] gorev zamanlayici: '{KAYIP_GOREV}' (her gun 17:30; kendi push edilmemis dallarinizi push eder, kaybolmayan worktree ve yerel dallari temizler, force yok)")
        else:
            ok = False
        return ok
    if macos() or not (sys.platform.startswith("linux") and shutil.which("systemctl")):
        if not a.remove:
            say(f"{tag}kayip is: zamanlayici kurulmadi. Istege bagli crontab satiri: 30 17 * * * \"{python}\" -I \"{posix(KAYIP_GIRIS)}\"")
        return ok
    base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") if gercek else home / ".config"
    udir = base / "systemd" / "user"
    svc, tmr = udir / f"{KAYIP_UNIT}.service", udir / f"{KAYIP_UNIT}.timer"
    live = gercek and not a.whatif
    if a.remove:
        if live and (svc.exists() or tmr.exists()):
            systemctl(["disable", "--now", f"{KAYIP_UNIT}.timer"])
        sil_dosya(svc, tag)
        sil_dosya(tmr, tag)
        if live:
            systemctl(["daemon-reload"])
        return ok
    tpl = (SRC / "systemd" / f"{KAYIP_UNIT}.service.in").read_text(encoding="utf-8")
    svc_text = tpl.replace("@EXEC@", f"{unit_q(python)} -I {unit_q(posix(KAYIP_GIRIS))}") \
        .replace("@ENV@", ortam_satirlari(("PATH", "SSH_AUTH_SOCK", "GH_CONFIG_DIR", "XDG_CONFIG_HOME")))
    yaz_uret(svc, svc_text, tag)
    yaz_uret(tmr, (SRC / "systemd" / f"{KAYIP_UNIT}.timer").read_text(encoding="utf-8"), tag)
    if a.whatif:
        return ok
    if live:
        ok &= systemctl(["daemon-reload"])
        ok &= systemctl(["enable", "--now", f"{KAYIP_UNIT}.timer"])
    else:
        say("  HOME gercek ev dizini degil: systemctl calistirilmadi. Gercek kurulumda: "
            f"systemctl --user daemon-reload && systemctl --user enable --now {KAYIP_UNIT}.timer")
    return ok


def sinama(a, python, yer, kaydet):
    """The self-check, run exactly as settings.json runs things: the hook for both events, `maliyet --help` (with `kaydet` also the first
    scan), the status line with a sample whose resets lie in 2100 (not a real reset: it writes no pace.json / history and starts no
    forecast) and the sub-agent rows. True when all pass. The installer runs it after every install; `--sinama` runs it alone (quick, no
    first scan), which is what tools/rules_watch.py does after pulling the clone in --repodan mode."""
    say("Kendi kendine sinama:")
    ok = True
    hook = posix(yer["bin/pace-hook.py"])
    for ev, extra in (("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": "test"})):
        payload = json.dumps({"hook_event_name": ev, "session_id": "kur-sinama", **extra})
        ok &= run_check([python, "-I", hook], payload, f"hook {ev}")
    # cost statistics first: the first scan writes the status-line cache, so the status-line check below starts no background refresh
    if a.maliyet:
        mal = yer["bin/maliyet.py"]
        ok &= run_check([python, "-I", str(mal), "--help"], None, "maliyet --help")
        if kaydet:
            say("Ilk tarama (maliyet --kaydet; buyuk gecmiste birkac dakika surebilir):")
            ok &= run_check([python, "-I", str(mal), "--kaydet"], None, "maliyet --kaydet", timeout=1800)
    if KAYIP_GIRIS.is_file():   # the daily job's entry (it runs kayip_is.py)
        ok &= run_check([python, "-I", str(KAYIP_GIRIS), "--help"], None, "gunluk_kayip --help")
    if KANIT.is_file():   # both events, exactly as settings.json runs it; the hook fails open, so prove it runs: a hedge must be blocked
        for ev in KANIT_EVENTS:
            payload = json.dumps({"hook_event_name": ev, "session_id": "kur-sinama", "last_assistant_message": "Bu probably yanlis."})
            try:
                r = gizli.run([python, "-I", "-c", RW_YEDEK, posix(KANIT)], input=payload, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=60)
                good = r.returncode == 0 and '"decision": "block"' in r.stdout
            except Exception:
                good = False
            say(f"  [{'OK' if good else 'HATA'}] kanit_kancasi {ev}")
            ok &= good
    if BEKCI.is_file():   # fails open, so prove it runs: a background Agent call must produce the nudge (own state file, nothing written to ~/.cache)
        try:
            with tempfile.TemporaryDirectory() as td:
                r = gizli.run([python, "-I", "-c", RW_YEDEK, posix(BEKCI)], capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=60, env={**os.environ, "AJAN_BEKCI_STATE": str(Path(td) / "s.json")},
                                   input=json.dumps({"session_id": "kur-sinama", "tool_name": "Agent", "tool_input": {"run_in_background": True}}))
                good = r.returncode == 0 and "additionalContext" in r.stdout
        except Exception:
            good = False
        say(f"  [{'OK' if good else 'HATA'}] ajan_bekci_notu PostToolUse")
        ok &= good
    if AJAN_ADI.is_file():   # fails open, so prove it runs: an Agent call must come back named (own state folder, nothing left behind)
        try:
            with tempfile.TemporaryDirectory() as td:
                r = gizli.run([python, "-I", "-c", RW_YEDEK, posix(AJAN_ADI)], capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=60, env={**os.environ, "EKIP_AJAN_ADI_DIR": td},
                                   input=json.dumps({"session_id": "kur-sinama", "tool_name": "Agent", "tool_input": {"prompt": "x", "description": "y"}}))
                good = r.returncode == 0 and "updatedInput" in r.stdout
        except Exception:
            good = False
        say(f"  [{'OK' if good else 'HATA'}] ajan_adi PreToolUse")
        ok &= good
    if a.slack_gelen:
        ok &= run_check([python, "-I", str(SG_GELEN), "count"], None, "slack-gelen count")
    if a.slack_triage:   # imports the window/turn module of the clone exactly as the tick does (no GitHub call, no claude)
        ok &= run_check([python, "-X", "utf8", "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import pencere; pencere.ayar(); print(pencere.kisi()[1])",
                         str(SG_DIR)], None, "slack-triage pencere")
    if a.son_pencere or a.devam_alarmi or (yer["bin/son_pencere.py"].is_file() and sp_sarmal(a, Path.home() / ".claude").is_file()):
        ok &= run_check([python, "-I", str(yer["bin/son_pencere.py"]), "--help"], None, "son-pencere --help")
    sample = json.dumps({"session_id": "kur-sinama", "model": {"display_name": "Test"},
                         "workspace": {"current_dir": str(Path.home())},
                         "rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": 4102444800},
                                         "seven_day": {"used_percentage": 20, "resets_at": 4102444800}}})
    if a.windows:
        cmd = [a.shell, "-NoProfile", "-File", str(yer["bin/statusline-yerel.ps1"])]
    else:
        cmd = ["bash", str(yer["statusline.sh"])]
    ok &= run_check(cmd, sample, "durum cubugu")
    if (SRC / FILES[-1][0]).is_file():   # exactly the command written to settings.json (python path, -I, script path)
        task = {"id": "kur-sinama", "agentType": "x", "description": "kur sinama", "tokenCount": 1234}
        sub = [python, "-I", posix(yer[SUB_KEY])]
        try:
            r = gizli.run(sub, input=json.dumps({"tasks": [task]}), capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60)
            good = r.returncode == 0 and '"id": "kur-sinama"' in r.stdout and "kur sinama" in r.stdout
        except Exception:
            good = False
        say(f"  [{'OK' if good else 'HATA'}] ajan satiri (subagentStatusLine)")
        ok &= good
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", required=True)
    ap.add_argument("--shell", help="pwsh/powershell path (Windows only)")
    ap.add_argument("--windows", action="store_true")
    ap.add_argument("--whatif", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--maliyet", action="store_true", help="install the cost statistics (the default; kept for old command lines)")
    ap.add_argument("--maliyetsiz", action="store_true", help="leave the cost statistics out")
    ap.add_argument("--paylas", action="store_true", help="join the usage ranking: write kisi/plan, check access, daily job runs --kaydet --paylas")
    ap.add_argument("--kisi", help='with --paylas: the name as listed in kisiler.json (default: the name the job would resolve)')
    ap.add_argument("--plan", help="with --paylas: 100 or 200 (monthly USD of the subscription)")
    ap.add_argument("--son-pencere", dest="son_pencere", action="store_true",
                    help="opt in to the last-window mode: leftover weekly quota is used on pre-approved work before the reset; runs are logged in the shared repo")
    ap.add_argument("--devam-alarmi", dest="devam_alarmi", action="store_true",
                    help="opt in to the 5-hour resume alarm: after a 5-hour limit reset the sessions it stopped get one 'continue' relay "
                         "(same son-pencere command and timer; also what burn mode `son-pencere bitir --ac` runs on)")
    ap.add_argument("--slack-gelen", dest="slack_gelen", action="store_true",
                    help="opt in to the token-free Slack inbox poller (Linux/macOS/Windows): 5-minute timer (systemd, launchd or Task Scheduler), slack-gelen command, SessionStart 'Slack: N yeni' line")
    ap.add_argument("--slack-triage", dest="slack_triage", action="store_true",
                    help="opt in to running the Slack/mail request triage on this machine (every developer's machine may; one owns triage until a request/ack handover): scheduler task/timer, config copied when missing")
    ap.add_argument("--bayat", action="store_true",
                    help="opt in to the daily stale-branch cleanup + report on this (always-on Linux) host: systemd user timer Mon-Fri 09:30 Europe/Istanbul, Persistent; it replaces the triage tick call")
    ap.add_argument("--araclar", action="store_true",
                    help="install the missing command-line tools (jq, poppler, tesseract, shellcheck, ...; winget / apt-get / brew, pip --user); without it the tool check only reports")
    ap.add_argument("--repodan", action="store_true", help="run the scripts from this clone instead of copies in ~/.claude (updates come with git pull); this is the default")
    ap.add_argument("--kopya", action="store_true", help="legacy: copy the scripts into ~/.claude/bin (needs a re-run for every update); the default runs them from this clone")
    ap.add_argument("--ayar", action="store_true", help="only bring the rules_watch hook entries of settings.json and the ~/.claude/agents/claude-shared link up to date (no install; rules_watch.py runs it after a pull, --repodan installs only)")
    ap.add_argument("--sinama", action="store_true", help="only run the quick self-check for these options (no install, no first scan)")
    a = ap.parse_args()
    a.maliyet = not a.maliyetsiz
    if a.repodan and a.kopya:
        say("HATA: --repodan ve --kopya birlikte verilemez.")
        return EXIT_PAYLAS_OPSIYON
    # every run starts from the sharing options saved by the last install and adds the flags given now, so `kur.sh --son-pencere`
    # keeps the ranking and the Slack jobs (a bare run is the same with no new flag). --kaldir and --sinama use only the flags given.
    # --whatif previews the same result, writing nothing.
    if not (a.remove or a.sinama or a.ayar):
        kayit = kayitli_kurulum(Path.home() / ".claude")
        if kayit:
            kayitli_bayraklari_ekle(a, kayit)
            say("Kayitli kurulum bayraklari korunuyor: " + " ".join(kayit))
    if a.plan is not None and not a.paylas:
        say("HATA: --plan yalniz --paylas ile birlikte verilir.")
        return EXIT_PAYLAS_OPSIYON
    sp = a.son_pencere or a.devam_alarmi   # both install the son-pencere command and its timer
    if a.kisi is not None and not (a.paylas or sp):
        say("HATA: --kisi yalniz --paylas ile (ya da --son-pencere / --devam-alarmi ile) birlikte verilir.")
        return EXIT_PAYLAS_OPSIYON
    if sp and a.maliyetsiz:
        say("HATA: --son-pencere / --devam-alarmi maliyet/paylasim modullerini gerektirir; --maliyetsiz ile birlikte verilemez.")
        return EXIT_PAYLAS_OPSIYON
    if a.paylas and a.maliyetsiz:
        say("HATA: --paylas maliyet istatistiklerini gerektirir; --maliyetsiz ile birlikte verilemez.")
        return EXIT_PAYLAS_OPSIYON
    if a.repodan and a.maliyetsiz and not a.remove:
        # the status line turns the cost segments on when bin/maliyet.py sits next to it, which is always true in the clone
        say("HATA: --repodan maliyet istatistikleriyle birlikte calisir; --maliyetsiz ile birlikte verilemez "
            "(maliyet bolumunu kapatmak icin ~/.claude/maliyet/kapali dosyasini olusturun).")
        return EXIT_PAYLAS_OPSIYON
    # direct source is the default: every consumer points at the clone's files (git pull updates them, no re-run); --kopya or
    # --maliyetsiz (which the clone's layout cannot honour) keep the old copy mode. --kaldir undoes either mode the same way.
    a.repodan = (a.repodan or not (a.kopya or a.maliyetsiz)) and not a.remove

    claude = Path.home() / ".claude"
    settings = claude / "settings.json"
    python = posix(a.python)
    want = ("win", "both") if a.windows else ("posix", "both")
    yer = yerler(claude, a.repodan, want)
    if a.ayar:
        if not a.repodan:
            say("rules_watch girisleri otomatik guncellenmedi: kurulum depodan (--repodan) degil")
            return 0
        ajan_baglantisi(claude, "", False)   # the background run after every pull: creates or repairs the agents link (no-op when in place)
        return rules_watch_uygula(settings, python)
    if a.sinama:
        return 0 if sinama(a, python, yer, kaydet=False) else 1

    paylas_kisi = paylas_plan = None
    if a.paylas and not a.remove:   # --kaldir ignores the sharing options: it removes the job, the kisi/plan files stay
        paylas_kisi, paylas_plan, hata = paylas_ayar(a.kisi, a.plan)
        if hata:
            say(hata)
            return EXIT_PAYLAS_OPSIYON
        if not a.whatif:
            rc = paylas_onkontrol(a.python, paylas_kisi, paylas_plan)
            if rc:
                return rc
    a.paylas = a.paylas and not a.remove
    sp_kisi = None
    if sp and not a.remove:
        sp_kisi, hata = paylas_ad(a.kisi)
        if hata:
            say(hata)
            return EXIT_PAYLAS_OPSIYON
        if not a.whatif:
            rc = son_pencere_onkontrol(sp_kisi)
            if rc:
                return rc

    hook = posix(yer["bin/pace-hook.py"])
    files = [(s, claude / d) for s, d, plat in FILES if plat in want]
    if a.maliyet or a.remove:
        files += [(s, claude / d) for s, d, plat in MALIYET_FILES if plat in want]
    if sp or a.remove:
        files += [(s, claude / d) for s, d, plat in SON_PENCERE_FILES if plat in want]

    def q(p):
        return f'"{p}"' if " " in p else p
    if a.windows:
        status_cmd = f"{q(posix(a.shell))} -NoProfile -File {q(posix(yer['bin/statusline-yerel.ps1']))}"
    else:
        status_cmd = f"bash {q(posix(yer['statusline.sh']))}"
    if a.repodan:   # direct source: a missing clone or a failing script must never show an error in the status line
        sp = posix(yer["bin/statusline-yerel.ps1"] if a.windows else yer["statusline.sh"])
        if a.windows:
            status_cmd = (f"{q(posix(a.shell))} -NoProfile -Command \"if (Test-Path '{sp}') {{ & '{sp}' }} else {{ 'Claude' }}\"")
        else:
            status_cmd = (f"bash -c 'f=\"$1\"; o=$([ -f \"$f\" ] && bash \"$f\" 2>/dev/null); "
                          f"if [ -n \"$o\" ]; then printf %s \"$o\"; else printf Claude; fi' _ {q(sp)}")
    # sub-agent rows: the same interpreter as the hook (absolute path: python3 does not exist on Windows), forward slashes only
    sub_cmd = f"{q(python)} -I {q(posix(yer[SUB_KEY]))}" if (SRC / FILES[-1][0]).is_file() else None

    tag = "[-WhatIf] " if a.whatif else ""
    say(f"{tag}Hedef klasor: {posix(claude)}")
    if a.repodan:
        say(f"{tag}Kip: depodan ({posix(SRC.parent.parent)}); betikler kopyalanmaz, guncellemeler git pull ile gelir")

    # 1. settings.json: parse before touching anything; abort without writing on failure.
    cfg, existed = {}, settings.exists()
    try:
        if existed:
            cfg = json.loads(settings.read_text(encoding="utf-8-sig"))
            if not isinstance(cfg, dict):
                raise ValueError("kok nesne degil")
        changes = merge(cfg, python, hook, status_cmd, a.remove, sub_cmd)
        changes += merge_rules_watch(cfg, python, a.remove)
        changes += merge_kanit_kancasi(cfg, python, a.remove)
        changes += merge_ajan_bekci(cfg, python, a.remove)
        changes += merge_ajan_adi(cfg, python, a.remove)
        changes += merge_ekip_ajanlari(cfg, a.remove)
        if a.slack_gelen or a.remove:
            changes += merge_slack_gelen(cfg, python, a.slack_gelen and not a.remove)
    except (ValueError, OSError) as e:
        say(f"HATA: settings.json okunamadi/ayristirilamadi ({e}). Hicbir sey yazilmadi.")
        return 2
    if not existed and not a.remove:
        changes.insert(0, "settings.json olusturuldu")

    if a.paylas:
        say(f"{tag}Siralamaya katilim: ad '{paylas_kisi}', plan ${paylas_plan}")
        paylas_ayar_yaz(paylas_kisi, paylas_plan, tag)
    if sp_kisi:
        say(f"{tag}Son pencere: ad '{sp_kisi}' (ortak kayit: raporlar/son-pencere/)")
        if not (a.paylas and paylas_kisi == sp_kisi):
            yaz_uret(Path(_paylas_modulu().AYAR) / "kisi", sp_kisi + "\n", tag)

    # 2. files (--repodan: copies left by an earlier install are removed; the clone's files run instead)
    for src, dst in files:
        if a.remove or a.repodan:
            if dst.exists():
                if a.repodan:   # migration from a copy install: keep a backup of what was copied, then drop it
                    bak = backup_name(dst)
                    say(f"{tag}yedek: {posix(bak)}")
                    if not a.whatif:
                        shutil.copy2(dst, bak)
                say(f"{tag}sil: {posix(dst)}")
                if not a.whatif:
                    dst.unlink()
            continue
        if not (SRC / src).is_file():
            say(f"{tag}UYARI: kaynak yok, atlandi: {src}")
            continue
        data = (SRC / src).read_bytes()
        if dst.exists():
            if dst.read_bytes() == data:
                say(f"{tag}ayni: {posix(dst)}")
                continue
            bak = backup_name(dst)
            say(f"{tag}yedek: {posix(bak)}")
            if not a.whatif:
                shutil.copy2(dst, bak)
        say(f"{tag}kopyala: {posix(dst)}")
        if not a.whatif:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)

    if not a.remove:   # every install: the old agent names give way to the team agents linked from this clone
        eski_ajanlari_temizle(claude, tag, a.whatif)
        ajan_adlarini_guncelle(claude, tag, a.whatif)
    ajan_baglantisi(claude, tag, a.remove)   # every install and --kaldir: ~/.claude/agents/claude-shared -> <clone>/agents

    # the mode record tools/rules_watch.py reads; only a --repodan install has one (a copy install or --kaldir removes it).
    # argv: the options the self-check after an automatic pull runs with (`kur_ortak.py <argv> --sinama`).
    if a.repodan:
        argv = ["--python", python] + (["--windows"] if a.windows else []) + (["--shell", posix(a.shell)] if a.shell else []) + ["--repodan"]
        kurulum = (["--paylas"] if a.paylas else []) + (["--kisi", paylas_kisi or sp_kisi] if (paylas_kisi or sp_kisi) else []) \
            + (["--plan", str(paylas_plan)] if a.paylas and paylas_plan else []) + (["--son-pencere"] if a.son_pencere else []) + (["--devam-alarmi"] if a.devam_alarmi else []) \
            + (["--slack-gelen"] if a.slack_gelen else []) + (["--slack-triage"] if a.slack_triage else []) + (["--bayat"] if a.bayat else [])
        durum = {"kip": "repodan", "depo": posix(SRC.parent.parent), "python": python, "argv": argv, "kurulum": kurulum}
        yaz_uret(claude / DURUM, json.dumps(durum, indent=2, ensure_ascii=False) + "\n", tag)
    else:
        sil_dosya(claude / DURUM, tag)

    ok_maliyet = True
    if a.maliyet or a.remove:
        ok_maliyet = maliyet_ekstra(a, python, claude, tag, yer["bin/maliyet.py"])

    ok_sp = True
    if sp or a.remove:
        ok_sp = son_pencere_ekstra(a, python, claude, tag, yer["bin/son_pencere.py"])

    ok_kayip = kayip_ekstra(a, python, tag)   # every install: the daily lost-work push from this clone
    ok_gh = gh_sinir_ekstra(a)   # every install: the gh shim -> tools/gh_sinir.py (GitHub API budget guard)
    ok_arac = arac_ekstra(a, a.python)   # every install: tool report + python3 shim (Windows); --araclar installs the missing tools
    ok_sg = True
    if a.slack_gelen or a.remove:
        ok_sg = slack_gelen_ekstra(a, python, claude, tag)
    if a.slack_triage or a.remove:
        ok_sg &= slack_triage_ekstra(a, python, tag)
    if a.bayat or a.remove:
        ok_sg &= bayat_ekstra(a, python, tag)

    # 3. settings.json write (UTF-8 without BOM, 2-space indent)
    for c in changes:
        say(f"{tag}settings.json: {c}")
    if not changes:
        say(f"{tag}settings.json: degisiklik yok")
    elif not a.whatif:
        if existed:
            bak = backup_name(settings)
            shutil.copy2(settings, bak)
            say(f"yedek: {posix(bak)}")
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_bytes((json.dumps(cfg, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

    ok_eklenti = claude_eklentileri(cfg, a, tag)   # every install: register the enabled claude-shared plugins (after settings.json is written)

    if a.whatif:
        say(f"{tag}Hicbir sey yazilmadi.")
        return 0
    if a.remove:
        say("Kaldirma tamamlandi. Siralama ayarlari (~/.config/claude-shared/kisi, plan) kaldi; istemezseniz silin.")
        return 0

    # 4. self-check: run everything exactly as configured
    ok = sinama(a, python, yer, kaydet=True)
    if a.maliyet:
        ok &= ok_maliyet
    ok &= ok_sp & ok_kayip & ok_sg & ok_gh & ok_arac & ok_eklenti
    say("Kurulum tamam. Claude Code'u yeniden baslatin." if ok
        else "Kurulum yapildi ama sinama basarisiz, yukaridaki hataya bakin.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

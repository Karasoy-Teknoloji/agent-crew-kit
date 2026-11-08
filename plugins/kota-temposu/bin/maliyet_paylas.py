#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""maliyet_paylas: a usage row per person in the team's shared markdown files (opt-in, runs from the existing 2-hourly job).

Plain Python: it spends NO model tokens; it reads the local transcripts' totals, one `git` clone and nothing else.

  maliyet --paylas            (the 2-hourly timer run) record the totals, then compare the ranking on origin/main with the ranking
                              including this person's fresh row. Same (timestamps, rolling-window drift and the 7-day dollars on their
                              own do not count) = nothing is written, nothing committed. Different, or the first run of a date = today's
                              row is written (replaced in place when it exists), SIRALAMA.md is regenerated from ALL files, commit
                              (the message names the rank / mode / weekly % / Slack move) and push, normal push, no force
  maliyet --paylas --kuru     print the row; touch nothing
  maliyet --paylas --otomatik the status line's throttled run (every paylas_aralik_saat hours, default 2, while Claude Code is in use):
                              pushes only when the integer weekly % differs from the last pushed one, replacing today's row in place
  --siralama                  accepted for old drop-ins; has no effect (every run regenerates the ranking)

Every machine that runs it commits its own row AND the regenerated ranking, only when something changed. The job keeps its own clone in
~/.cache/claude-shared-kullanim (reset to origin/main before each attempt, which is a pull --rebase that cannot conflict: the person's
file has one writer and SIRALAMA.md is regenerated, never merged; on a rejected push it re-fetches and redoes the work, up to 3 times).
A rank or mode change of the person's own row also raises a desktop notification on that machine (notify-send; bildir()).
Ownership: a row is only written to the file of the runner's own name, and that name must be a value of kisiler.json; the ranking
reads only the files of the names in kisiler.json. Who committed what stays visible in the git history.

Row (a markdown table row): date | 24h | 7d | 30d $ | weekly limit % | plan ($100/$200) | week id | week-to-date $ | per-model $ and
Mtokens (Opus, Sonnet, Haiku, Fable, other: the previous calendar day) | own commits that reached dev in the last 24 h | lines added | lines deleted
(numstat of those commits, noise excluded: GURULTU_RE; old rows lack the last two columns and show "—"), then forecast median, reset time,
pace mode, projected run-out, forecast time, and this machine's Slack inbox state (slack-gelen: acik/hata/kapali/yok) with its last poll.
Dollars are the API list-price equivalent of token usage (not a bill). Only the name and these numbers leave the machine.

Person name: env CLAUDE_SHARED_KISI, else ~/.config/claude-shared/kisi, else `git config user.name`.
Plan: env CLAUDE_SHARED_PLAN, else ~/.config/claude-shared/plan (100 or 200).
Commit count in each daily row: own commits (author email = git user.email) of the last 24 h on the integration branch
(origin/dev, else origin/main|master) of every clone under the team root (env CLAUDE_SHARED_KOK, else
~/.config/claude-shared/kok, else ~/source/repos or ~/src, whichever exists). Local
knowledge only: a machine without clones or user.email writes "—", so the row figure is NOT used for the ranking when GitHub
answers. The "Commit (7 gün)" column of SIRALAMA.md comes from GitHub (github_commitleri): `gh api search/commits` with
q=author:<login> org:<GH_ORG> author-date:>=<today-6>, minus the automation subjects chore(kullanim) and chore(son-pencere), logins from kisiler.json, cached 6 h in
<config dir>/maliyet/github-commit-onbellek.json (search API: 30 requests/min; one request per person per 6 h). Falls back to
the sum of the row figures when `gh` is missing or fails. Default branch of each repo only (GitHub search limitation).
Never fails loudly: any error goes to <config dir>/maliyet/paylas.log and the exit code stays 0.

Config keys (env, else the first line of ~/.config/claude-shared/<file>; README, "Configuration"):
  CLAUDE_SHARED_UZAK (uzak)                the team's shared usage repo (clone URL). No default: unset = the ranking is off.
  CLAUDE_SHARED_RAPOR_DIZIN (rapor-dizin)  report folder inside that repo, default raporlar/kullanim.
  CLAUDE_SHARED_KISILER (kisiler-yol)      the people list inside that repo, default plugins/kota-temposu/kisiler.json.
  GH_ORG (org)                             GitHub org for the 7-day commit column; unset = the row figures are used.
"""
import datetime
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", "tools"))  # repo layout: tools/gizli.py (a --kopya install ships it next to this file)
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from gizli_yukle import gizli  # noqa: E402  (one hidden-launch helper)
import gunluk  # noqa: E402  (the rolling run log)
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # python -I does not add the script directory
import maliyet_verim as V  # noqa: E402  weekly cost per finished work item (sidecar <kisi>.hafta.json, leaderboard section)

SIRALAMA_DOSYA = "SIRALAMA.md"
KOTA_TAZELIK = 48 * 3600      # pace.json older than this is not quoted
GIT_ZAMAN_ASIMI = 120
PLANLAR = (100, 200)
MODELLER = (("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"), ("fable", "Fable"))

HOME = os.path.expanduser("~")
AYAR = os.path.join(HOME, ".config", "claude-shared")

# A scanned repo's own config must not run code: command-line -c beats .git/config (fsmonitor, hooks, ssh, pager, external
# diff, credential helpers, ext:: transport). Same list as tools/kayip_is.py (SAFE_GIT); tools/ is not installed next to this file.
SAFE_GIT = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull, "-c", "core.sshCommand=ssh",
            "-c", "core.pager=cat", "-c", "core.editor=true", "-c", "diff.external=", "-c", "credential.helper=",
            "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=user", "-c", "gc.auto=0",
            "-c", "maintenance.auto=false", "-c", "fetch.fsckObjects=false"]
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "LC_ALL": "C"}
ATLA = {"node_modules", "bin", "obj", "venv", "__pycache__"}

_KONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")



# ------------------------------------------------------------------ small helpers
def ayar_yolu(ad):
    return os.path.join(AYAR, ad)


def dosyadan_oku(yol):
    try:
        with open(yol, encoding="utf-8") as f:
            return f.readline().strip()
    except OSError:
        return ""


def ayar_degeri(ad, env, varsayilan=""):
    """One config key: env `env`, else the first line of ~/.config/claude-shared/<ad>, else `varsayilan` (see the module doc)."""
    return (os.environ.get(env) or dosyadan_oku(ayar_yolu(ad)) or varsayilan).strip()


RAPOR_DIZIN = ayar_degeri("rapor-dizin", "CLAUDE_SHARED_RAPOR_DIZIN", "raporlar/kullanim").strip("/")
KISILER_YOL = ayar_degeri("kisiler-yol", "CLAUDE_SHARED_KISILER", "plugins/kota-temposu/kisiler.json").strip("/")
UZAK_YOK = "ortak depo ayarlanmadi (CLAUDE_SHARED_UZAK veya ~/.config/claude-shared/uzak; README, Configuration)"


def temiz_ad(s, sinir=30):
    """A name safe to post: no control characters, no field separator, no @mention or markup, one line, capped."""
    s = _KONTROL.sub(" ", str(s or ""))
    s = re.sub(r"[·|@<>`*_\[\]\\:./!#~&;()]", " ", s)    # separators, mentions, links/autolinks, refs, entities, markup
    s = " ".join(s.split())
    return s[:sinir].strip()


def kisi_adi():
    ad = os.environ.get("CLAUDE_SHARED_KISI") or dosyadan_oku(ayar_yolu("kisi"))
    if not ad:
        ad = git_cikti(HOME, "config", "--get", "user.name") or ""
    return temiz_ad(ad)


def git_cikti(cwd, *args, timeout=30):
    """stdout of a git command run with the repo-config code-execution switches off, or None on any failure."""
    try:
        r = gizli.run(["git", "--no-optional-locks", *SAFE_GIT, "-C", str(cwd), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=GIT_ENV, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as e:
        kom_hata("git " + (args[0] if args else ""), type(e).__name__)
        return None
    if r.returncode != 0:
        if not (args[:1] in (("config",), ("rev-parse",)) and r.returncode == 1):      # `config --get` of an unset key and `rev-parse` outside a repo are answers, not failures
            kom_hata("git " + (args[0] if args else ""), r.returncode, r.stderr)
        return None
    return r.stdout.strip()


def gunluk_yaz(seviye, mesaj, *args):
    """One line in the run log of the scheduled job (tools/gunluk.py "maliyet": ~/.local/state/maliyet.log, env MALIYET_LOG). Never raises."""
    try:
        gunluk.gunluk("maliyet").log(seviye, mesaj, *args)
    except Exception:
        pass


def temiz(metin, n=300):
    """Output of a failed command made fit for a log line: tokens and URL credentials masked, control characters and line breaks gone."""
    try:
        return _KONTROL.sub(" ", gunluk.maskele(metin or "")).strip()[:n]
    except Exception:
        return ""


def kom_hata(ad, rc, cikti=""):
    """Log one failed git / gh call. `ad` is the program and sub-command only ("git fetch", "gh api"): never the arguments, a remote URL could carry a credential."""
    gunluk_yaz(logging.WARNING, "%s failed rc=%s: %s", ad, rc, temiz(cikti))


def _komut_adi(args):
    """Program name, plus the sub-command of git and gh ("git fetch", "gh api", "notify-send")."""
    ad = os.path.splitext(os.path.basename(str(args[0])))[0] if args else "?"
    return ad + " " + str(args[1]) if ad in ("git", "gh") and len(args) > 1 and not str(args[1]).startswith("-") else ad


def log_yaz(mesaj):
    """Append to <config dir>/maliyet/paylas.log (kept short; also mirrored into the run log); a logging failure is swallowed too."""
    gunluk_yaz(logging.INFO, "%s", temiz(mesaj, 500))
    try:
        import maliyet
        os.makedirs(maliyet.DIZIN, exist_ok=True)
        yol = os.path.join(maliyet.DIZIN, "paylas.log")
        try:
            if os.path.getsize(yol) > 200_000:
                with open(yol, encoding="utf-8", errors="replace") as f:
                    son = f.readlines()[-200:]
                with open(yol, "w", encoding="utf-8") as f:
                    f.writelines(son)
        except OSError:
            pass
        with open(yol, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), _KONTROL.sub(" ", str(mesaj))[:500]))
    except Exception:
        pass


# ------------------------------------------------------------------ the numbers
def kota_bilgi(simdi=None):
    """(weekly quota used %, epoch when that week resets) from pace.json (the status line's rate-limit data), or (None, None)
    when missing, stale or from a week that already reset."""
    import maliyet
    simdi = time.time() if simdi is None else simdi
    try:
        with open(os.path.join(maliyet.CLAUDE, "pace.json"), encoding="utf-8-sig") as f:
            p = json.load(f)
        used, upd, reset = p.get("week_used"), float(p.get("updated") or 0), float(p.get("week_resets_at") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None, None
    if not isinstance(used, (int, float)) or isinstance(used, bool) or not 0 <= used <= 100:
        return None, None
    if simdi - upd > KOTA_TAZELIK or reset <= simdi or reset > simdi + 8 * 86400:
        return None, None
    return int(round(used)), reset


def pace_ek(simdi=None):
    """(forecast median % at the target, local reset time 'YYYY-MM-DD HH:MM', pace mode) from pace.json; each None when unknown.
    Same freshness rule as kota_bilgi: nothing is quoted from a stale file or a week that already reset."""
    import maliyet
    kota, reset = kota_bilgi(simdi)
    if kota is None:
        return None, None, None
    try:
        with open(os.path.join(maliyet.CLAUDE, "pace.json"), encoding="utf-8-sig") as f:
            p = json.load(f)
    except (OSError, ValueError):
        p = {}
    if not isinstance(p, dict):
        p = {}
    t, mod = p.get("p50"), p.get("mode")
    t = int(round(t)) if isinstance(t, (int, float)) and not isinstance(t, bool) and 0 <= t <= 1000 else None
    mod = mod if isinstance(mod, str) and MOD_RE.match(mod) else None
    return t, time.strftime("%Y-%m-%d %H:%M", time.localtime(reset)), mod


def bitis_ek(simdi=None):
    """(run-out, forecast time) from pace.json as local 'YYYY-MM-DD HH:MM' strings; run-out is "yeter" when the forecast says the
    limit lasts until the reset. (None, None) when unknown (stale file, no forecast, file from an older pace_forecast)."""
    import maliyet
    kota, _ = kota_bilgi(simdi)
    if kota is None:
        return None, None
    try:
        with open(os.path.join(maliyet.CLAUDE, "pace.json"), encoding="utf-8-sig") as f:
            p = json.load(f)
        upd = float(p.get("updated") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None, None
    if "runout_at" not in p or not upd:
        return None, None
    ra = p.get("runout_at")
    fmt = "%Y-%m-%d %H:%M"
    if isinstance(ra, (int, float)) and not isinstance(ra, bool) and ra > 0:
        bitis = time.strftime(fmt, time.localtime(ra))
    elif ra is None and isinstance(p.get("p50"), (int, float)):
        bitis = YETER
    else:
        return None, None
    return bitis, time.strftime(fmt, time.localtime(upd))


def bitis_metni(b):
    """`2026-11-01 12:00` -> `01.11 12:00`; "yeter" -> "sıfırlanmaya yetiyor"; "—" when unknown."""
    return "sıfırlanmaya yetiyor" if b == YETER else sifirla_metni(b)


def slack_metni(durum, son):
    """`açık · 04.11 02:35` (state and the last poll of the person's Slack inbox poller), `kapalı`, `kurulu değil`, "—" when unknown."""
    ad = {"acik": "açık", "hata": "hata", "kapali": "kapalı", "yok": "kurulu değil"}.get(durum)
    if ad is None:
        return "—"
    zaman = sifirla_metni(son)
    return ad if durum == "yok" or zaman == "—" else "%s · %s" % (ad, zaman)


def sifirla_metni(s):
    """`2026-11-01 12:00` -> `01.11 12:00`; "—" when empty."""
    m = re.match(r"^\d{4}-(\d\d)-(\d\d) (\d\d:\d\d)$", s or "")
    return "%s.%s %s" % (m.group(2), m.group(1), m.group(3)) if m else "—"


def haftalik_kota(simdi=None):
    return kota_bilgi(simdi)[0]


def kok_dizin():
    yol = os.environ.get("CLAUDE_SHARED_KOK") or dosyadan_oku(ayar_yolu("kok"))
    adaylar = [yol] if yol else [os.path.join(HOME, "source", "repos"), os.path.join(HOME, "src")]   # documented defaults
    for a in adaylar:
        a = os.path.expanduser(a)
        if os.path.isdir(a):
            return a
    return None


def klonlar(kok, derinlik=2):
    """Git clones/worktrees under the root (the root's children, and theirs); a clone is not descended into."""
    bulunan = []

    def gez(d, k):
        try:
            adlar = sorted(os.listdir(d))
        except OSError:
            return
        for ad in adlar:
            if ad.startswith(".") or ad in ATLA:
                continue
            yol = os.path.join(d, ad)
            if os.path.exists(os.path.join(yol, ".git")):
                bulunan.append(yol)
            elif k < derinlik and os.path.isdir(yol):
                gez(yol, k + 1)
    if os.path.exists(os.path.join(kok, ".git")):
        bulunan.append(kok)
    else:
        gez(kok, 1)
    return bulunan


# Noise that is not work output: lock files, generated EF files, minified/map files, build and vendor folders (matched on the new path).
GURULTU_RE = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|packages\.lock\.json|[^/]*\.lock"
    r"|[^/]*\.min\.js|[^/]*\.min\.css|[^/]*\.map|[^/]*ModelSnapshot\.cs|Migrations/[^/]*\.Designer\.cs"
    r"|[^/]*\.(pdf|cdr|bad|jsonl|svg)|[^/]*\.g\.(i\.)?cs|(bad_REHBER_|PRODUCT_A_)[^/]*)$"   # PRODUCT_A_*, bad_REHBER_*: extensionless CDR/phonebook dumps; pdf: standards/manuals; cdr, bad: CDR/phonebook data dumps; jsonl: machine logs; svg, *.g.cs: generated
    r"|(^|/)(bin|obj|node_modules|dist|build)/|(^|/)wwwroot/lib/")


def numstat_yolu(yol):
    """New path of a numstat path field (`a => b`, `dir/{a => b}/f`)."""
    if " => " not in yol:
        return yol
    if "{" in yol:
        yol = re.sub(r"\{([^{}]*) => ([^{}]*)\}", r"\2", yol)
        return yol.replace("//", "/")
    return yol.split(" => ", 1)[1]


def numstat_ayristir(metin):
    """{hash: (added, deleted)} from `git log --format=%x01%H --numstat -M`. Binary files (`-`), noise paths (GURULTU_RE)
    and pure renames (0/0) add nothing; a commit that only had such files still appears with (0, 0)."""
    sonuc, hash_ = {}, None
    for satir in (metin or "").split("\n"):
        if satir.startswith("\x01"):
            hash_ = satir[1:].strip()
            if hash_:
                sonuc.setdefault(hash_, (0, 0))
            continue
        parca = satir.split("\t", 2)
        if hash_ is None or len(parca) != 3 or not (parca[0].isdigit() and parca[1].isdigit()):
            continue
        ek, sil = int(parca[0]), int(parca[1])
        if (ek == 0 and sil == 0) or GURULTU_RE.search(numstat_yolu(parca[2])):
            continue
        a, d = sonuc[hash_]
        sonuc[hash_] = (a + ek, d + sil)
    return sonuc


def commit_ve_satir(eposta, kok, saat=24):
    """(commits, added lines, deleted lines) of the own commits of the last `saat` hours that are on the integration branch of
    any clone under `kok`; the same commit seen in two clones counts once. None when unknown."""
    if not eposta or not kok:
        return None
    toplam = {}
    for klon in klonlar(kok):
        for ref in ("origin/dev", "origin/main", "origin/master"):
            if git_cikti(klon, "rev-parse", "--verify", "-q", ref + "^{commit}") is not None:
                out = git_cikti(klon, "-c", "core.quotepath=false", "log", ref, "--no-merges", "--numstat", "-M", "--since=%d hours ago" % saat, "-F", "-i",
                                "--author=" + eposta, "--format=%x01%H", timeout=120)
                toplam.update(numstat_ayristir(out))
                break
    return len(toplam), sum(a for a, _ in toplam.values()), sum(d for _, d in toplam.values())


def github_org():
    """The GitHub org of the 7-day commit column (config key GH_ORG / ~/.config/claude-shared/org); "" = not configured."""
    return ayar_degeri("org", "GH_ORG")


GITHUB_ONBELLEK_SAAT = 6
# Subjects of the commits this plugin writes itself (bir_deneme: the usage row; son_pencere: its journal). They are automation, not a
# person's work, so the GitHub commit column leaves them out.
OTOMASYON_KONULARI = ("chore(kullanim)", "chore(son-pencere)")
TR = datetime.timezone(datetime.timedelta(hours=3))


def _onbellek_yolu():
    import maliyet
    return os.path.join(maliyet.DIZIN, "github-commit-onbellek.json")


def github_commit_say(login, bas, simdi=None):
    """Commits by `login` in the org's repos with author date >= `bas` (YYYY-MM-DD), via gh search/commits, minus the automation
    commits (OTOMASYON_KONULARI, matched on the subject line); cached per (login, bas) for GITHUB_ONBELLEK_SAAT hours. None when
    unknown (no gh, no network, API error)."""
    simdi = time.time() if simdi is None else simdi
    anahtar = "%s|%s" % (login, bas)
    yol = _onbellek_yolu()
    try:
        with open(yol, encoding="utf-8") as f:
            ob = json.load(f)
        if not isinstance(ob, dict):
            ob = {}
    except (OSError, ValueError):
        ob = {}
    e = ob.get(anahtar)
    if isinstance(e, dict) and isinstance(e.get("n"), int) and 0 <= simdi - float(e.get("t", 0)) < GITHUB_ONBELLEK_SAAT * 3600:
        return e["n"]
    org = github_org()
    if not org:
        return None   # no org configured: the column falls back to the row figures
    try:
        # one subject line per commit (all pages); the automation subjects are dropped here, not in the query
        r = gizli.run(["gh", "api", "-X", "GET", "search/commits", "-f", "q=author:%s org:%s author-date:>=%s" % (login, org, bas),
                            "-f", "per_page=100", "--paginate", "--jq", '.items[].commit.message | split("\\n")[0]'],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, env=GIT_ENV,
                           stdin=subprocess.DEVNULL)
        # text mode without an explicit encoding decodes with the Windows ANSI codepage; a non-cp1252 byte in a commit subject kills
        # the reader thread and leaves stdout None (AttributeError on splitlines). Explicit utf-8 + this guard keep it a plain "unknown".
        if r.returncode == 0 and r.stdout is not None:
            n = sum(1 for konu in r.stdout.splitlines() if not konu.startswith(OTOMASYON_KONULARI))
        else:
            n = None
            kom_hata("gh api", r.returncode, r.stderr)
            print("github_commit_say: gh api basarisiz (rc=%s, stdout=%s)" % (r.returncode, "yok" if r.stdout is None else "var"), file=sys.stderr)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        kom_hata("gh api", type(e).__name__)
        n = None
    if n is None:
        return None
    ob[anahtar] = {"n": n, "t": simdi}
    try:
        os.makedirs(os.path.dirname(yol), exist_ok=True)
        with open(yol, "w", encoding="utf-8") as f:
            json.dump(ob, f)
    except OSError:
        pass
    return n


def github_commitleri(harita, bugun, simdi=None):
    """{display name: commits in the 7-day window [bugun-6 .. bugun]} for every person of kisiler.json ({login: name}); people whose
    count is unknown are left out (the ranking then falls back to the sum of their row figures)."""
    bas = (datetime.date.fromisoformat(bugun) - datetime.timedelta(days=6)).isoformat()
    out = {}
    for login, ad in harita.items():
        n = github_commit_say(login, bas, simdi)
        if n is not None:
            out[ad] = n
    return out


def commit_say(eposta, kok, saat=24):
    """Own commits of the last `saat` hours that are on the integration branch of any clone under `kok` (None when unknown)."""
    c = commit_ve_satir(eposta, kok, saat)
    return None if c is None else c[0]


def pencere_dolar(kayitlar, saat):
    """(24h, 7d, 30d) in whole dollars, from the same rolling windows the status line shows."""
    import maliyet_cekirdek as CK
    return tuple(int(round(P["usd"])) for P in CK.pencere_hesapla(kayitlar, saat))




# ------------------------------------------------------------------ this machine's Slack inbox (tools/slack-triage/gelen.py)
SG_BIRIM = "slack-gelen"                        # systemd user unit (kur.sh --slack-gelen)
SG_ETIKET = "com.agentcrewkit.slack-gelen"           # launchd agent label
SG_TAZE = 20 * 60                               # no service manager: a poll this recent counts as running
SON_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d")


def komut(args, timeout=10):
    """(returncode, stdout, stderr) of a short read-only command; (None, "", "") when it cannot run. Replaced by tests."""
    try:
        r = gizli.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           env=GIT_ENV, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        if _komut_adi(args).startswith(("git", "gh")):
            kom_hata(_komut_adi(args), type(e).__name__)
        return None, "", ""
    if r.returncode != 0 and _komut_adi(args).startswith(("git", "gh")):      # service probes (systemctl is-enabled ...) answer non-zero as a normal answer
        kom_hata(_komut_adi(args), r.returncode, r.stderr)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def slack_gelen_son():
    """(last poll 'YYYY-MM-DD HH:MM' local or None, whether that poll reported an error) from gelen.py's state file. Only the two
    `status` fields are read: the inbox file and the message text are never opened."""
    yol = os.path.join(os.environ.get("SLACK_GELEN_DATA") or os.path.join(HOME, ".local", "state", "slack-gelen"), "state.json")
    try:
        with open(yol, encoding="utf-8") as f:
            st = json.load(f).get("status")
    except (OSError, ValueError, AttributeError):
        return None, False
    if not isinstance(st, dict):
        return None, False
    son = st.get("last_run")
    return (son[:16] if isinstance(son, str) and SON_RE.match(son) else None), bool(st.get("error"))


def slack_gelen_durumu(simdi=None):
    """(state word, last poll) of this machine's token-free Slack inbox poller, both None when it cannot be told (Windows without a
    poll on record; no service manager answers). State: acik (enabled and running), hata (running, but the last poll failed), kapali (installed,
    switched off), yok (not installed). Linux: `systemctl --user` on slack-gelen.timer; macOS: launchd agent com.agentcrewkit.slack-gelen.
    Windows (kur.ps1 -SlackGelen, a Task Scheduler task): only the last poll is read, a recent one counts as running (acik/hata), an old one
    as kapali; no poll on record is unknown. Nothing but these two values leaves the machine."""
    simdi = time.time() if simdi is None else simdi
    son, hata = slack_gelen_son()
    if sys.platform.startswith("win"):
        if son is None:
            return None, None
        try:
            yuklu = simdi - time.mktime(time.strptime(son, "%Y-%m-%d %H:%M")) < SG_TAZE
        except (ValueError, OverflowError):
            yuklu = False
        return ("hata" if hata else "acik") if yuklu else "kapali", son
    if sys.platform == "darwin":
        if not os.path.exists(os.path.join(HOME, "Library", "LaunchAgents", SG_ETIKET + ".plist")):
            return "yok", None
        rc, _, _ = komut(["launchctl", "print", "gui/%d/%s" % (os.getuid(), SG_ETIKET)])
        yuklu = rc == 0
        kurulu = True
    else:
        rc, out, err = komut(["systemctl", "--user", "is-enabled", SG_BIRIM + ".timer"])
        if rc is None:   # no systemctl at all (cron install, or no systemd): the poll's own timestamp is all there is
            try:
                yuklu = son is not None and simdi - time.mktime(time.strptime(son, "%Y-%m-%d %H:%M")) < SG_TAZE
            except (ValueError, OverflowError):
                yuklu = False
            return ("acik" if yuklu and not hata else "hata" if yuklu else "yok" if son is None else "kapali"), son
        if rc != 0 and out != "disabled" and "bus" in err.lower():
            return None, None   # the user manager cannot be reached from here: unknown, not "not installed"
        kurulu = rc == 0 or out == "disabled"
        yuklu = rc == 0 and komut(["systemctl", "--user", "is-active", SG_BIRIM + ".timer"])[1] == "active"
    if not kurulu:
        return "yok", None
    return ("hata" if hata else "acik") if yuklu else "kapali", son


# ------------------------------------------------------------------ the row
SUTUNLAR = (["Tarih", "24sa $", "7g $", "30g $", "Kota %", "Plan", "Hafta", "Hafta $"]
            + ["%s $" % ad for _, ad in MODELLER] + ["Diğer $"]
            + ["%s Mtok" % ad for _, ad in MODELLER] + ["Diğer Mtok", "Commit"])
EK_SUTUN = ["Satır +", "Satır −"]   # added later; rows with SUTUNLAR only (old rows) still parse and show "—"
EK2_SUTUN = ["Tahmin %", "Sıfırlanma", "Mod"]   # weekly forecast median, reset time, pace mode (from pace.json); empty ("—") when unknown
EK3_SUTUN = ["Bitiş", "Tahmin zamanı"]   # projected weekly-limit run-out (pace.json runout_at; "yeter" = lasts until reset) and when the forecast was made
ARA_SUTUN = SUTUNLAR + EK_SUTUN
ARA2_SUTUN = ARA_SUTUN + EK2_SUTUN   # 24 columns, before EK3_SUTUN
ARA3_SUTUN = ARA2_SUTUN + EK3_SUTUN   # 26 columns, before EK4_SUTUN
EK4_SUTUN = ["Slack", "Slack son"]   # this machine's token-free Slack inbox (slack-gelen): state word and the last poll; never any message content
TUM_SUTUN = ARA3_SUTUN + EK4_SUTUN
ESKI_BASLIKLAR = (SUTUNLAR, ARA_SUTUN, ARA2_SUTUN, ARA3_SUTUN)
YETER = "yeter"
SLACK_DURUMLARI = ("acik", "hata", "kapali", "yok")   # running | running but the last poll failed (e.g. token expired) | installed, switched off | not installed
MOD_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
TARIH_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
GUN_ADLARI = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]


def dosya_adi(kisi):
    """File stem for a person: lower case, Turkish letters folded, anything else becomes '-'."""
    s = str(kisi).translate(str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")).lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "kisi"


def model_ailesi(model):
    m = str(model).lower()
    for anahtar, _ in MODELLER:
        if anahtar in m:
            return anahtar
    return "diger"


def model_dagilim(kayit):
    """({family: $}, {family: Mtoken}) of one daily record (all token kinds summed; Fable runs report $ only)."""
    usd, tok = {}, {}
    for m, v in ((kayit or {}).get("model") or {}).items():
        f = model_ailesi(m)
        usd[f] = usd.get(f, 0.0) + float(v.get("usd", 0.0) or 0.0)
        n = sum(int(v.get(k, 0) or 0) for k in ("in", "out", "cr", "cw5", "cw1h"))
        tok[f] = tok.get(f, 0.0) + n / 1e6
    return usd, tok


def plan_oku():
    """100 or 200 from env CLAUDE_SHARED_PLAN / ~/.config/claude-shared/plan; None when not set (the row then says '—')."""
    ham = os.environ.get("CLAUDE_SHARED_PLAN") or dosyadan_oku(ayar_yolu("plan"))
    m = re.search(r"\d+", ham or "")
    return int(m.group()) if m and int(m.group()) in PLANLAR else None


def hafta_dolar(kayitlar, reset):
    """Dollars since the start of the current weekly-limit week (daily records from the local date of reset-7d; approximate)."""
    import maliyet_cekirdek as CK
    bas = CK.yerel_gun(reset - 7 * 86400)
    return int(round(sum(r.get("usd", 0.0) for d, r in kayitlar.items() if d >= bas)))


def _h(x, nd=1):
    return "—" if x is None else ("%d" % x if isinstance(x, int) else "%.*f" % (nd, x))


def _k(n):
    """Compact Turkish number: 950, 12,3K, 1,2M."""
    if n < 1000:
        return "%d" % n
    return ("%.1f" % (n / 1000.0) + "K" if n < 1000000 else "%.1f" % (n / 1000000.0) + "M").replace(".", ",")


def satir_metni(ekle, sil):
    """`+12,3K / −4,1K`, or "—" when the rows carry no line figures."""
    return "—" if ekle is None or sil is None else "+%s / −%s" % (_k(ekle), _k(sil))


def dolar_bin_satir(d7, ekle, sil):
    """7-day spend per 1000 changed lines (`$4,12`), or "—" without line figures."""
    if ekle is None or sil is None or ekle + sil <= 0:
        return "—"
    return ("$%.2f" % (d7 * 1000.0 / (ekle + sil))).replace(".", ",")


def satir_kur(r):
    """The markdown table row of a record dict (see satir_ayristir for the inverse)."""
    h = [r["tarih"], "%d" % r["d24"], "%d" % r["d7"], "%d" % r["d30"], _h(r.get("kota")),
         "$%d" % r["plan"] if r.get("plan") else "—", r.get("hafta") or "—", _h(r.get("hafta_usd"))]
    fam = [a for a, _ in MODELLER] + ["diger"]
    h += ["%.1f" % r["usd"].get(f, 0.0) for f in fam] + ["%.1f" % r["tok"].get(f, 0.0) for f in fam]
    h += [_h(r.get("commit")), _h(r.get("ekle")), _h(r.get("sil"))]
    h += [_h(r.get("tahmin")), r.get("sifirla") or "—", r.get("mod") or "—"]
    h += [r.get("bitis") or "—", r.get("tahmin_zamani") or "—"]
    h += [r.get("slack") or "—", r.get("slack_son") or "—"]
    return "| " + " | ".join(h) + " |"


def _sayi(s, tip=float):
    try:
        return tip(s)
    except ValueError:
        return None


def satir_ayristir(satir):
    """A table row back into a record dict, or None for anything else (header, separator, prose, a malformed row)."""
    if not isinstance(satir, str) or not satir.startswith("| 20"):
        return None
    c = [x.strip() for x in satir.strip().strip("|").split("|")]
    if len(c) not in (len(SUTUNLAR), len(ARA_SUTUN), len(ARA2_SUTUN), len(ARA3_SUTUN), len(TUM_SUTUN)) or not TARIH_RE.match(c[0]):
        return None
    try:
        datetime.date.fromisoformat(c[0])
    except ValueError:
        return None
    d24, d7, d30 = (_sayi(c[i], int) for i in (1, 2, 3))
    if None in (d24, d7, d30):
        return None
    fam = [a for a, _ in MODELLER] + ["diger"]
    usd = {f: _sayi(c[8 + i]) or 0.0 for i, f in enumerate(fam)}
    tok = {f: _sayi(c[13 + i]) or 0.0 for i, f in enumerate(fam)}
    plan = _sayi(c[5].lstrip("$"), int)
    return {"tarih": c[0], "d24": d24, "d7": d7, "d30": d30, "kota": _sayi(c[4], int), "plan": plan if plan in PLANLAR else None,
            "hafta": c[6] if TARIH_RE.match(c[6]) else None, "hafta_usd": _sayi(c[7], int), "usd": usd, "tok": tok,
            "commit": _sayi(c[18], int),
            "ekle": _sayi(c[19], int) if len(c) > 19 else None, "sil": _sayi(c[20], int) if len(c) > 20 else None,
            "tahmin": _sayi(c[21], int) if len(c) > 21 else None,
            "sifirla": c[22] if len(c) > 22 and sifirla_metni(c[22]) != "—" else None,
            "mod": c[23] if len(c) > 23 and MOD_RE.match(c[23]) else None,
            "bitis": c[24] if len(c) > 24 and (c[24] == YETER or sifirla_metni(c[24]) != "—") else None,
            "tahmin_zamani": c[25] if len(c) > 25 and sifirla_metni(c[25]) != "—" else None,
            "slack": c[26] if len(c) > 26 and c[26] in SLACK_DURUMLARI else None,
            "slack_son": c[27] if len(c) > 27 and sifirla_metni(c[27]) != "—" else None}


def dosya_satirlari(metin):
    """Records of a person's file, one per date (the first row of a date wins), oldest first."""
    gorulen = {}
    for s in (metin or "").splitlines():
        r = satir_ayristir(s)
        if r and r["tarih"] not in gorulen:
            gorulen[r["tarih"]] = r
    return [gorulen[t] for t in sorted(gorulen)]


def dosya_basligi(kisi):
    return ("# Claude kullanımı — %s\n\n"
            "Günde bir satır; `maliyet --paylas` işi ekler (yerel Python, model token'ı harcamaz). Elle düzenlemeyin. "
            "Dolar = API liste fiyatı karşılığı (fatura değil). Model ve token sütunları bir önceki takvim gününe aittir; "
            "Kota %% = haftalık limit kullanımı, Hafta $ = limit haftasının başından beri dolar. Sıralama: [SIRALAMA.md](SIRALAMA.md).\n\n"
            "| %s |\n|%s\n" % (kisi, " | ".join(TUM_SUTUN), "---|" * len(TUM_SUTUN)))


def dosyaya_ekle(metin, kisi, satir, tarih, kota_guncelle=False, yerine=False):
    """The person's file with `satir` appended unless that date already has a row (so a second run the same day changes nothing).
    kota_guncelle (the status line's automatic share): a row of that date whose weekly % differs from the new row's is replaced in
    place, so a date never gets a second row and the 7-day / week-over-week figures keep one row per day.
    yerine (the timer run that has already decided the change is real): the row of that date is replaced unconditionally, still in place."""
    if not metin or not metin.strip():
        metin = dosya_basligi(kisi)
    mevcut = [r for r in dosya_satirlari(metin) if r["tarih"] == tarih]
    if mevcut:
        yeni_r = satir_ayristir(satir) or {}
        bos = all(mevcut[0].get(k) is None for k in ("tahmin", "sifirla", "mod", "bitis", "tahmin_zamani"))
        eksik = (yeni_r.get("commit") is not None and (mevcut[0].get("commit") is None or yeni_r["commit"] > mevcut[0]["commit"])) or             (not sum(mevcut[0]["usd"].values()) and sum(yeni_r.get("usd", {}).values()) > 0)   # earlier run knew no repos / saw an idle day
        bitis_bos = mevcut[0].get("bitis") is None and mevcut[0].get("tahmin_zamani") is None and (yeni_r.get("bitis") or yeni_r.get("tahmin_zamani"))
        kota_farkli = kota_guncelle and yeni_r.get("kota") is not None and yeni_r["kota"] != mevcut[0].get("kota")
        if yerine or (bos and any(yeni_r.get(k) is not None for k in ("tahmin", "sifirla", "mod", "bitis", "tahmin_zamani"))) or bitis_bos or eksik or kota_farkli:   # a same-day row that this run can fill in
            sat = metin.split("\n")
            i = next(i for i, x in enumerate(sat) if (satir_ayristir(x) or {}).get("tarih") == tarih)
            sat[i] = satir
            metin = "\n".join(sat)
            for eski_s in ESKI_BASLIKLAR:
                metin = metin.replace("| %s |\n|%s" % (" | ".join(eski_s), "---|" * len(eski_s)),
                                      "| %s |\n|%s" % (" | ".join(TUM_SUTUN), "---|" * len(TUM_SUTUN)), 1)
        return metin
    yeni = "| %s |\n|%s" % (" | ".join(TUM_SUTUN), "---|" * len(TUM_SUTUN))
    for eski_s in ESKI_BASLIKLAR:
        metin = metin.replace("| %s |\n|%s" % (" | ".join(eski_s), "---|" * len(eski_s)), yeni, 1)   # an old header grows the new columns
    return metin.rstrip("\n") + "\n" + satir + "\n"


def kapsam_basi(kayit, onceki):
    """First day this row covers: a row dated R covers R-1 (normally just yesterday), so today's row covers [latest earlier row
    date .. yesterday] and a person's first row the last 7 days. One rule for the model split and for commits/lines."""
    bugun = _gun(kayit["tarih"])
    onc = [_gun(r["tarih"]) for r in onceki if _gun(r["tarih"]) < bugun]
    return max(onc) if onc else bugun - datetime.timedelta(days=7)


def kapsam_uygula(kayit, onceki):
    """Fill the model columns and commits/lines of `kayit` for the days no earlier row of this person covers (nothing counted
    twice; an idle yesterday no longer zeroes a new joiner's row)."""
    bugun = _gun(kayit["tarih"])
    bas = kapsam_basi(kayit, onceki)
    gunler = kayit.get("model_gunleri")
    if gunler:
        usd, tok = {}, {}
        for g, (u, t) in gunler.items():
            if bas <= _gun(g) < bugun:
                for f, v in u.items():
                    usd[f] = usd.get(f, 0.0) + v
                for f, v in t.items():
                    tok[f] = tok.get(f, 0.0) + v
        kayit["usd"], kayit["tok"] = usd, tok
    if kayit.get("eposta") and kayit.get("kok"):
        cs = commit_ve_satir(kayit["eposta"], kayit["kok"], max(1, (bugun - bas).days) * 24)
        if cs:
            kayit["commit"], kayit["ekle"], kayit["sil"] = cs


def bugunku_kayit(kisi, bugun, simdi=None):
    import maliyet_cekirdek as CK
    kayitlar, saat = CK.gunluk_oku(), CK.saatlik_oku()
    d24, d7, d30 = pencere_dolar(kayitlar, saat)
    eposta, kok = git_cikti(HOME, "config", "--get", "user.email"), kok_dizin()
    cs = commit_ve_satir(eposta, kok)
    kota, reset = kota_bilgi(simdi)
    tahmin, sifirla, mod = pace_ek(simdi)
    bitis, tahmin_zamani = bitis_ek(simdi)
    slack, slack_son = slack_gelen_durumu()
    dun = (datetime.date.fromisoformat(bugun) - datetime.timedelta(days=1)).isoformat()
    usd, tok = model_dagilim(kayitlar.get(dun))
    gunler = {}
    for i in range(1, 8):   # per-day split of the last 7 days; bir_deneme keeps only the days no earlier row of this person covered
        g = (datetime.date.fromisoformat(bugun) - datetime.timedelta(days=i)).isoformat()
        if g in kayitlar:
            gunler[g] = model_dagilim(kayitlar[g])
    return {"model_gunleri": gunler, "eposta": eposta, "kok": kok, "kisi": kisi, "tarih": bugun, "d24": d24, "d7": d7, "d30": d30, "kota": kota, "plan": plan_oku(), "tahmin": tahmin, "sifirla": sifirla, "mod": mod, "bitis": bitis, "tahmin_zamani": tahmin_zamani, "slack": slack, "slack_son": slack_son,
            "hafta": CK.yerel_gun(reset) if reset else None, "hafta_usd": hafta_dolar(kayitlar, reset) if reset else None,
            "usd": usd, "tok": tok, "commit": cs[0] if cs else None,
            "ekle": cs[1] if cs else None, "sil": cs[2] if cs else None}


# ------------------------------------------------------------------ the ranking and the plan comparison
def _gun(t):
    return datetime.date.fromisoformat(t)


def siralama_hesapla(kisi_satirlari, bugun):
    """Rank by each person's 7-day $ in their latest row of the last 7 days. kisi_satirlari: {name: [records]}.
    Trend = that 7g against the row dated 7 days earlier (±1 day, nearest). Commits = sum of the daily figures of the last 7 days."""
    t0 = _gun(bugun)
    sonuc = []
    for kisi, ls in kisi_satirlari.items():
        son7 = [k for k in ls if 0 <= (t0 - _gun(k["tarih"])).days <= 6]
        if not son7:
            continue
        son = son7[-1]
        aday = [(abs((_gun(son["tarih"]) - _gun(k["tarih"])).days - 7), k) for k in ls
                if 6 <= (_gun(son["tarih"]) - _gun(k["tarih"])).days <= 8]
        onceki = min(aday, key=lambda a: (a[0], a[1]["tarih"]))[1] if aday else None
        commits = [k["commit"] for k in son7 if k["commit"] is not None]
        ekle = [k["ekle"] for k in son7 if k.get("ekle") is not None]
        sil = [k["sil"] for k in son7 if k.get("sil") is not None]
        sonuc.append({"kisi": kisi, "d7": son["d7"], "onceki_d7": onceki["d7"] if onceki else None, "son_tarih": son["tarih"],
                      "kota": son["kota"], "plan": son["plan"],
                      "tahmin": son.get("tahmin"), "sifirla": son.get("sifirla"), "mod": son.get("mod"), "bitis": son.get("bitis"), "tahmin_zamani": son.get("tahmin_zamani"), "hafta_usd": son["hafta_usd"],
                      "slack": son.get("slack"), "slack_son": son.get("slack_son"),
                      "commit": sum(commits) if commits else None,
                      "ekle": sum(ekle) if ekle else None, "sil": sum(sil) if sil else None})
    sonuc.sort(key=lambda r: (-r["d7"], r["kisi"]))
    return sonuc


MOD_SIRA = {"BOL": 0, "PLENTY": 0, "NORMAL": 1}   # pace modes that may take work; SIKI/TIGHT and unknown modes never do


def is_alma_sirasi(kisi_satirlari, bugun, roller=None):
    """Who takes the next piece of work (rules/WORKING-RULES.md, "Who picks next"): (sira, disarida).
    sira = names that may take work, best first: pace mode BOL/PLENTY before NORMAL, then the lowest forecast % at the target
    ("Tahmin"; the weekly % when there is no forecast), then the name. disarida = [(name, reason)] of the people who never take
    work: role testci or yonetici in kisiler.json (only developers take board work), mode SIKI/TIGHT, weekly use at 100 %, or no leaderboard
    data (no row in the last 7 days, or no mode / weekly %). roller: {name: role} (default: roller_oku() of this clone; a name
    without a role is a developer). The single ranking used by the leaderboard and by the triage job's owner lease
    (tools/slack-triage/pencere.py)."""
    roller = roller_oku() if roller is None else roller
    sira, disarida = [], []
    gorulen = set()
    for r in siralama_hesapla(kisi_satirlari, bugun):
        gorulen.add(r["kisi"])
        mod = (r.get("mod") or "").upper()
        if roller.get(r["kisi"]) in IS_ALMAZ:
            disarida.append((r["kisi"], IS_ALMAZ[roller[r["kisi"]]]))
        elif not mod or r.get("kota") is None:
            disarida.append((r["kisi"], "veri yok"))
        elif mod not in MOD_SIRA:
            disarida.append((r["kisi"], "mod %s" % mod))
        elif r["kota"] >= 100:
            disarida.append((r["kisi"], "haftalık %100"))
        else:
            t = r.get("tahmin") if r.get("tahmin") is not None else r["kota"]
            sira.append((MOD_SIRA[mod], t, r["kisi"]))
    disarida += [(k, IS_ALMAZ.get(roller.get(k), "veri yok")) for k in sorted(kisi_satirlari) if k not in gorulen]
    return [k for _, _, k in sorted(sira)], disarida


def trend_metni(d7, onceki):
    if not onceki or onceki <= 0:
        return "yeni"
    yuzde = int(round((d7 - onceki) * 100.0 / onceki))
    if abs(yuzde) < 3:
        return "≈ aynı"
    return ("▲ +%d%%" if yuzde > 0 else "▼ %d%%") % yuzde


def onemli_anahtar(r):
    """What makes a person's row count as changed for the timer run: weekly %, plan, pace mode, forecast %, the day the limit is
    projected to run out (never the hour: the forecast moves it every few minutes), the Slack state word. Not part of it: the forecast
    time, the Slack last-poll time, the rolling 24 h / 30 d dollars, the week-to-date dollars (all of them drift with the clock alone).
    The 7-day dollars matter through the ranking (see sira_anahtari), not on their own."""
    b = r.get("bitis")
    return (r.get("kota"), r.get("plan"), r.get("mod"), r.get("tahmin"), b if b in (None, YETER) else b[:10], r.get("slack"))


def sira_anahtari(kisi_satirlari, bugun):
    """The ranking as a comparable value: [(name, onemli_anahtar of the person's latest row)] in rank order. Two states with the same
    key look the same in SIRALAMA.md apart from timestamps and the 7-day dollar figures."""
    return [(r["kisi"], onemli_anahtar(r)) for r in siralama_hesapla(kisi_satirlari, bugun)]


def degisim_ozeti(onceki, yeni, kisi):
    """(short text for the commit message, whether to tell the person) from two sira_anahtari lists: the rank move, mode, weekly %
    and Slack state of `kisi`. Telling = a rank or mode change; the other parts are only logged."""
    def bul(sira):
        for i, (ad, k) in enumerate(sira, 1):
            if ad == kisi:
                return i, k
        return None, None
    (i0, k0), (i1, k1) = bul(onceki), bul(yeni)
    k0, k1 = k0 or (None,) * 6, k1 or (None,) * 6
    parca, haber = [], False
    if i0 != i1 and i1 is not None:
        parca.append("sıra %s→%d" % (i0 if i0 else "—", i1))
        haber = True
    if k0[2] != k1[2] and k1[2]:
        parca.append("mod %s→%s" % (k0[2] or "—", k1[2]))
        haber = True
    if k0[0] != k1[0] and k1[0] is not None:
        parca.append("kota %s→%%%d" % ("%%%d" % k0[0] if k0[0] is not None else "—", k1[0]))
    if k0[5] != k1[5] and k1[5]:
        parca.append("slack %s→%s" % (k0[5] or "—", k1[5]))
    return " · ".join(parca) or "güncelleme", haber


MIN_KOTA = 10   # a week's $-per-% is only quoted once at least this much of the limit is used (small % are mostly rounding)


def hafta_oranlari(kisi_satirlari):
    """[(name, week id, plan, $ per 1% of the weekly limit)]: the latest row of each person-week with kota >= MIN_KOTA."""
    sonuc = []
    for kisi, ls in kisi_satirlari.items():
        son = {}
        for k in ls:
            if k["hafta"] and k["plan"] and k["kota"] is not None and k["kota"] >= MIN_KOTA and k["hafta_usd"] is not None:
                son[k["hafta"]] = k
        sonuc += [(kisi, h, k["plan"], k["hafta_usd"] / float(k["kota"])) for h, k in son.items()]
    return sonuc


def ortalama(x):
    return sum(x) / len(x) if x else None


def plan_karsilastir(kisi_satirlari):
    """{plan: {kisi, hafta, oran, kapasite, aylik, plan_basi}}: per plan the mean $ per 1% of the weekly limit, the implied weekly
    capacity at 100% ($), its monthly equivalent (x30/7) and that per plan dollar. Plans without data are absent."""
    gruplar = {}
    for kisi, _h_, plan, oran in hafta_oranlari(kisi_satirlari):
        g = gruplar.setdefault(plan, {"kisi": set(), "oranlar": []})
        g["kisi"].add(kisi)
        g["oranlar"].append(oran)
    out = {}
    for plan, g in gruplar.items():
        oran = ortalama(g["oranlar"])
        out[plan] = {"kisi": len(g["kisi"]), "hafta": len(g["oranlar"]), "oran": oran, "kapasite": oran * 100,
                     "aylik": oran * 100 * 30 / 7.0, "plan_basi": oran * 100 * 30 / 7.0 / plan}
    return out


def hafta_tablosu(kisi_satirlari):
    """{week id: {plan: mean $ per 1%}}."""
    t = {}
    for _k, hafta, plan, oran in hafta_oranlari(kisi_satirlari):
        t.setdefault(hafta, {}).setdefault(plan, []).append(oran)
    return {h: {p: ortalama(v) for p, v in d.items()} for h, d in t.items()}


def gun_deseni(kisi_satirlari):
    """{(plan, weekday 0=Mon): $ per 1% of the weekly limit that day}: from consecutive rows of one limit week, the growth of
    week-to-date $ over the growth of the limit %. The usage day is the day before the later row's date. Days with < 3 points of
    limit growth in total are left out."""
    topla = {}
    for ls in kisi_satirlari.values():
        for a, b in zip(ls, ls[1:]):
            if ((_gun(b["tarih"]) - _gun(a["tarih"])).days != 1 or not b["plan"] or a["hafta"] != b["hafta"] or not b["hafta"]
                    or None in (a["kota"], b["kota"], a["hafta_usd"], b["hafta_usd"])):
                continue
            dk, du = b["kota"] - a["kota"], b["hafta_usd"] - a["hafta_usd"]
            if dk < 0 or du < 0:
                continue
            g = (_gun(a["tarih"])).weekday()
            t = topla.setdefault((b["plan"], g), [0, 0])
            t[0] += du
            t[1] += dk
    return {k: v[0] / float(v[1]) for k, v in topla.items() if v[1] >= 3}


def model_ozeti(kisi_satirlari, bugun):
    """{name: ({family: $}, {family: Mtok})} over the rows of the last 7 days (each row = the previous calendar day)."""
    t0 = _gun(bugun)
    out = {}
    for kisi, ls in kisi_satirlari.items():
        u, t = {}, {}
        for k in ls:
            if 0 <= (t0 - _gun(k["tarih"])).days <= 6:
                for f, v in k["usd"].items():
                    u[f] = u.get(f, 0.0) + v
                for f, v in k["tok"].items():
                    t[f] = t.get(f, 0.0) + v
        if u:
            out[kisi] = (u, t)
    return out


MADALYA = {1: "🥇", 2: "🥈", 3: "🥉"}
MODEL_ADLARI = [(a, ad) for a, ad in MODELLER] + [("diger", "Diğer")]


def zaman_metni(t):
    """'dd.mm HH:MM' (Istanbul time, UTC+3 on every machine) of an epoch or ISO string; '—' when unknown."""
    try:
        if isinstance(t, str):
            t = datetime.datetime.fromisoformat(t).timestamp()
        return datetime.datetime.fromtimestamp(t, TR).strftime("%d.%m %H:%M")
    except (TypeError, ValueError, OSError):
        return "—"


MOD_SIRASI = {"BOL": 0, "NORMAL": 1, "SIKI": 2}


def siradaki_is_satirlari(sira, roller=None):
    """"Sıradaki işi kim alır" section (WORKING-RULES, hand-overs return work to the board: the most unused quota picks next):
    developers ordered by pace mode (BOL, NORMAL, SIKI), then by lowest forecast %; no mode or forecast = last, marked "veri yok".
    Testers and managers (roles testci, yonetici; roller as in is_alma_sirasi) never take board work: they are listed under a
    "testçi (iş almaz)" / "yönetici (iş almaz)" note."""
    roller = roller_oku() if roller is None else roller

    def anahtar(r):
        m, t = MOD_SIRASI.get(r.get("mod")), r.get("tahmin")
        return (m is None or t is None, m if m is not None else 9, t if t is not None else 0, r["kisi"])
    L = ["", "### Sıradaki işi kim alır", "",
         "Devredilen iş panoya döner; panodan yalnız yazılımcılar alır, en çok kullanılmamış kotası olan önce: önce mod (BOL, NORMAL, SIKI), sonra en düşük tahmin %.", "",
         "| # | Kişi | Mod | Tahmin |", "|---|---|---|---:|"]
    for i, r in enumerate(sorted((r for r in sira if roller.get(r["kisi"]) not in IS_ALMAZ), key=anahtar), 1):
        veri = r.get("mod") in MOD_SIRASI and r.get("tahmin") is not None
        L.append("| %d | %s | %s | %s |" % (i, r["kisi"].split()[0], r["mod"] if veri else "veri yok", "%%%d" % r["tahmin"] if veri else "—"))
    for rol, not_ in IS_ALMAZ.items():
        adlar = [r["kisi"].split()[0] for r in sorted(sira, key=lambda r: r["kisi"]) if roller.get(r["kisi"]) == rol]
        if adlar:
            L += ["", "_%s: %s_" % (not_, ", ".join(adlar))]
    return L


def siralama_metni(kisi_satirlari, bugun, github=None, son_zaman=None, hafta=None, roller=None):
    """Markdown of SIRALAMA.md (Turkish). github: {name: 7-day commits from GitHub} overrides the sum of the row figures;
    son_zaman: {name: epoch/ISO time of the person's latest row}. hafta: {name: weekly summary of maliyet_verim} for the finished-work
    section (none = the section is left out). roller: {name: role} for "Sıradaki işi kim alır" (default: roller_oku())."""
    github, son_zaman, hafta = github or {}, son_zaman or {}, hafta or {}
    L = ["# 🏆 Claude kullanımı — sıralama ve plan karşılaştırması (%s)" % bugun, "",
         "Bu dosya her `maliyet --paylas` çalıştırmasında `raporlar/kullanim/*.md` dosyalarından yeniden üretilir; elle düzenlemeyin. "
         "Dolar = API liste fiyatı karşılığı (fatura değil). Kullanılmayan haftalık kota sıfırlanmada yanar.", "",
         "## Son 7 gün", ""]
    sira = siralama_hesapla(kisi_satirlari, bugun)
    if not sira:
        L.append("_Son 7 günde satır yok. Katılmak için README'deki \"Günlük kullanım ve sıralama\" bölümü._")
    else:
        eski = (_gun(bugun) - datetime.timedelta(days=1)).isoformat()
        flag = lambda r: "" if r["son_tarih"] >= eski else " ⏳ " + r["son_tarih"]
        L += ["### Kota", "",
              "| # | Kişi | Haftalık % | Tahmin | Tahmini bitiş | Sıfırlanma | Mod | Slack gelen | Son güncelleme |", "|---|---|---:|---:|---|---|---|---|---|"]
        for i, r in enumerate(sira, 1):
            L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                MADALYA.get(i, str(i)), r["kisi"].split()[0] + flag(r),
                "%%%d" % r["kota"] if r["kota"] is not None else "—",
                "%%%d" % r["tahmin"] if r.get("tahmin") is not None else "—", bitis_metni(r.get("bitis")), sifirla_metni(r.get("sifirla")), r.get("mod") or "—",
                slack_metni(r.get("slack"), r.get("slack_son")),
                zaman_metni(son_zaman.get(r["kisi"])) if r["kisi"] in son_zaman else r["son_tarih"]))
        L += ["", "### Maliyet ve verim", "",
              "| # | Kişi | Plan | 7 gün | Geçen haftaya göre | Commit (7 gün) | Satır (7 gün) | $ / 1000 satır |", "|---|---|---|---:|---|---:|---:|---:|"]
        for i, r in enumerate(sira, 1):
            L.append("| %s | %s | %s | $%d | %s | %s | %s | %s |" % (
                MADALYA.get(i, str(i)), r["kisi"] + flag(r), "$%d" % r["plan"] if r["plan"] else "—", r["d7"], trend_metni(r["d7"], r["onceki_d7"]),
                _h(github.get(r["kisi"], r["commit"])), satir_metni(r["ekle"], r["sil"]), dolar_bin_satir(r["d7"], r["ekle"], r["sil"])))
        L += ["", "Ekip toplamı: $%d · %d kişi. ⏳ = son satırı dünden eski. Tahmini bitiş = haftalık limitin tahmini bittiği an (yerel tahmin); "
                  "\"—\" = kişinin yeni sürümle ilk günlük çalışması bekleniyor. Slack gelen = kişinin token harcamayan Slack gelen kutusu "
                  "(`kur.sh --slack-gelen`): açık / hata (son yoklama başarısız) / kapalı / kurulu değil ve son yoklama zamanı; mesaj içeriği paylaşılmaz." % (sum(r["d7"] for r in sira), len(sira))]
        L += siradaki_is_satirlari(sira, roller)
        if hafta:
            L += [""] + V.tablo_satirlari([r["kisi"] for r in sira], hafta, bugun, MADALYA)
    L += ["", "## Plan karşılaştırması: $100 mı $200 mü?", "",
          "Ölçü: haftalık limitin **%%1'i kaç $ karşılığı kullanım** ediyor (limit haftasının dolar toplamı ÷ kota %%; kota ≥ %d olan haftalar). "
          "Yüksek = aynı %% ile daha çok iş. Aylık karşılık = %%100 × 30/7." % MIN_KOTA, ""]
    pk = plan_karsilastir(kisi_satirlari)
    if not pk:
        L.append("_Henüz yeterli veri yok (plan, kota ve hafta bilgisi olan satırlar gerekir)._")
    else:
        L += ["| Plan | Kişi | Hafta | $ / %1 kota | Haftalık kapasite ($) | Aylık karşılık ($) | Aylık karşılık ÷ plan fiyatı |", "|---|---:|---:|---:|---:|---:|---:|"]
        for p in sorted(pk):
            v = pk[p]
            L.append("| $%d | %d | %d | %.2f | %.0f | %.0f | %.1f× |" % (p, v["kisi"], v["hafta"], v["oran"], v["kapasite"], v["aylik"], v["plan_basi"]))
        if 100 in pk and 200 in pk:
            k = pk[200]["oran"] / pk[100]["oran"]
            L += ["", "$200 planı, $100 planına göre **%.2f×** kapasite veriyor (fiyat 2×). %s" % (
                k, "Ek $100 karşılığını veriyor." if k >= 1.8 else "Ek $100'ın karşılığı bu veride görünmüyor." if k < 1.3 else
                "Ek $100'ın karşılığı kısmen görünüyor.")]
        else:
            L += ["", "_Karşılaştırma için iki plandan da veri gerekiyor._"]
    ht = hafta_tablosu(kisi_satirlari)
    if ht:
        L += ["", "## Hafta hafta: %1 kota kaç $ yaptı", "", "| Limit haftası (bitiş) | $100 | $200 |", "|---|---:|---:|"]
        for h in sorted(ht)[-8:]:
            L.append("| %s | %s | %s |" % (h, *("%.2f" % ht[h][p] if p in ht[h] else "—" for p in PLANLAR)))
    gd = gun_deseni(kisi_satirlari)
    if gd:
        L += ["", "## Haftanın günü: %1 kota kaç $ yaptı", "", "| Gün | $100 | $200 |", "|---|---:|---:|"]
        for g in range(7):
            if any((p, g) in gd for p in PLANLAR):
                L.append("| %s | %s |" % (GUN_ADLARI[g], " | ".join("%.2f" % gd[(p, g)] if (p, g) in gd else "—" for p in PLANLAR)))
    mo = model_ozeti(kisi_satirlari, bugun)
    if mo:
        L += ["", "## Modeller (son 7 gün: dolar payı · milyon token)", "",
              "| # | Kişi | " + " | ".join(ad for _, ad in MODEL_ADLARI) + " |", "|---|---|" + "---:|" * len(MODEL_ADLARI)]
        toplam_u, toplam_t = {}, {}
        sirali = [r["kisi"] for r in sira if r["kisi"] in mo] + sorted(k for k in mo if k not in {r["kisi"] for r in sira})   # same order as the tables above
        for i, kisi in enumerate(sirali, 1):
            u, t = mo[kisi]
            top = sum(u.values()) or 1.0
            L.append("| %s | %s | %s |" % (MADALYA.get(i, str(i)), kisi.split()[0], " | ".join("%d%% · %.0f" % (round(100 * u.get(f, 0.0) / top), t.get(f, 0.0)) for f, _ in MODEL_ADLARI)))
            for f in u:
                toplam_u[f] = toplam_u.get(f, 0.0) + u[f]
            for f in t:
                toplam_t[f] = toplam_t.get(f, 0.0) + t[f]
        top = sum(toplam_u.values()) or 1.0
        L.append("| | **Ekip** | %s |" % " | ".join("%d%% · %.0f" % (round(100 * toplam_u.get(f, 0.0) / top), toplam_t.get(f, 0.0)) for f, _ in MODEL_ADLARI))
    L += ["", "Nasıl daha az parayla daha çok iş: [NASIL-TASARRUF.md](NASIL-TASARRUF.md).", ""]
    return "\n".join(L)


# ------------------------------------------------------------------ git (the job's own clone)
def klon_yolu():
    return os.environ.get("CLAUDE_SHARED_KLON") or os.path.join(HOME, ".cache", "claude-shared-kullanim")


def uzak_adres():
    return ayar_degeri("uzak", "CLAUDE_SHARED_UZAK")


def git(klon, *args, timeout=GIT_ZAMAN_ASIMI):
    """(returncode, output) of a git command in the job's own clone (credential helpers stay on for the push; hooks are off)."""
    kom = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", "-c", "gc.auto=0", "-C", str(klon), *args]
    try:
        r = gizli.run(kom, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           env=GIT_ENV, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as e:
        kom_hata("git " + (args[0] if args else ""), type(e).__name__)
        return 1, type(e).__name__
    if r.returncode != 0 and not (args[:1] == ("diff",) and r.returncode == 1):     # `diff --quiet` answers 1 when there is a difference
        kom_hata("git " + (args[0] if args else ""), r.returncode, r.stdout + r.stderr)
    return r.returncode, (r.stdout + r.stderr).strip()


def klon_hazirla(klon, uzak):
    """The job's clone, reset to the remote main (it is the job's own: nothing in it is precious). Raises RuntimeError."""
    if not uzak:
        raise RuntimeError(UZAK_YOK)
    if not os.path.isdir(os.path.join(klon, ".git")):
        os.makedirs(os.path.dirname(klon), exist_ok=True)
        try:
            r = gizli.run(["git", "clone", "--quiet", "--branch", "main", uzak, klon], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=300, env=GIT_ENV, stdin=subprocess.DEVNULL)
        except (subprocess.TimeoutExpired, OSError) as e:
            kom_hata("git clone", type(e).__name__)
            raise RuntimeError("clone: %s" % type(e).__name__)
        if r.returncode != 0:
            kom_hata("git clone", r.returncode, r.stderr or r.stdout)
            raise RuntimeError("clone: " + (r.stderr or r.stdout).strip().replace("\n", " ")[:200])
    for args in (("fetch", "--quiet", "origin", "main"), ("checkout", "--quiet", "-B", "main", "origin/main"),
                 ("reset", "--quiet", "--hard", "origin/main"), ("clean", "-fdq")):
        rc, out = git(klon, *args)
        if rc != 0:
            raise RuntimeError("git %s: %s" % (args[0], out.replace("\n", " ")[:200]))


ROLLER = ("yazilimci", "testci", "yonetici")   # kisiler.json roles; only "yazilimci" takes board work and owns triage
IS_ALMAZ = {"testci": "testçi (iş almaz)", "yonetici": "yönetici (iş almaz)"}   # roles that never take work, with their note


def kisi_ayristir(veri):
    """{github login (lower case): (display name, role)} from parsed kisiler.json, the single parser of that file. An entry is either
    "Name" (old format = role yazilimci, so old clones keep working) or {"ad": "Name", "rol": "yazilimci"|"testci"|"yonetici"}; a missing or
    unknown role is yazilimci. Invalid entries are skipped; {} when `veri` is not an object."""
    if not isinstance(veri, dict):
        return {}
    out = {}
    for l, v in veri.items():
        ad, rol = (v, None) if isinstance(v, str) else ((v.get("ad"), v.get("rol")) if isinstance(v, dict) else (None, None))
        if isinstance(l, str) and isinstance(ad, str) and temiz_ad(ad):
            out[l.lower()] = (temiz_ad(ad), rol if rol in ROLLER else "yazilimci")
    return out


def kisiler_json(klon):
    """kisi_ayristir of kisiler.json in the clone; {} when unreadable."""
    try:
        with open(os.path.join(klon, *KISILER_YOL.split("/")), encoding="utf-8") as f:
            return kisi_ayristir(json.load(f))
    except (OSError, ValueError):
        return {}


def kisiler_oku(klon):
    """{github login (lower case): display name} from kisiler.json in the clone; {} when unreadable."""
    return {l: a for l, (a, _r) in kisiler_json(klon).items()}


def roller_oku(klon=None):
    """{display name: role} from kisiler.json in the clone (default: the clone this script runs from)."""
    klon = klon or os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
    return {a: r for a, r in kisiler_json(klon).values()}


def dosyalari_oku(klon, adlar):
    """{name: [records]} of the files of the listed names only (a stray file nobody listed is ignored)."""
    out = {}
    for ad in sorted(set(adlar)):
        yol = os.path.join(klon, *RAPOR_DIZIN.split("/"), dosya_adi(ad) + ".md")
        try:
            with open(yol, encoding="utf-8") as f:
                out[ad] = dosya_satirlari(f.read())
        except OSError:
            pass
    return out


def hafta_oku(klon, adlar):
    """{name: weekly summary} of the <kisi>.hafta.json sidecars of the listed names only (maliyet_verim)."""
    out = {}
    for ad in sorted(set(adlar)):
        o = V.ozet_oku(V.dosya_yolu(os.path.join(klon, *RAPOR_DIZIN.split("/")), dosya_adi(ad)))
        if o:
            out[ad] = o
    return out


def son_zamanlar(klon, adlar, simdi=None):
    """{name: epoch of the person's latest row}: the commit time of their file in the clone (rows are only added by the daily
    job, which commits them at once); a file with uncommitted changes (the row being written now) counts as now."""
    simdi = time.time() if simdi is None else simdi
    out = {}
    for ad in sorted(set(adlar)):
        yol = RAPOR_DIZIN + "/" + dosya_adi(ad) + ".md"
        rc, durum = git(klon, "status", "--porcelain", "--", yol)
        if rc == 0 and durum.strip():
            out[ad] = simdi
            continue
        rc, t = git(klon, "log", "-1", "--format=%ct", "--", yol)
        if rc == 0 and t.strip().isdigit():
            out[ad] = int(t.strip())
    return out


def yaz(yol, metin):
    os.makedirs(os.path.dirname(yol), exist_ok=True)
    with open(yol, "w", encoding="utf-8", newline="\n") as f:
        f.write(metin)


def bir_deneme(klon, uzak, kayit, bugun, otomatik=False, degisim=False):
    """Fetch, add this person's row, rebuild the ranking, commit, push. Returns 'itildi', 'degisiklik yok' or raises RuntimeError.
    otomatik (status-line trigger): only the weekly % (integer, as the table shows it) decides: same as today's pushed row = nothing
    is written, otherwise today's row is replaced in place.
    degisim (the 2-hourly timer run): the ranking as it stands on origin/main is compared with the ranking including this row (see
    sira_anahtari / onemli_anahtar: timestamp-only drift never counts). Same = nothing is written, nothing committed. Different, or
    the first run of a date = today's row is replaced in place, SIRALAMA.md is regenerated, the commit message says what moved
    (kayit["ozet"], kayit["haber"] carry that text and whether a rank or mode change is worth telling the person)."""
    klon_hazirla(klon, uzak)
    harita = kisiler_oku(klon)
    kisi = kayit["kisi"]
    if kisi not in harita.values():
        raise RuntimeError("'%s' kisiler.json icinde yok: satir yazilmadi" % kisi)
    yol = os.path.join(klon, *RAPOR_DIZIN.split("/"), dosya_adi(kisi) + ".md")
    try:
        with open(yol, encoding="utf-8") as f:
            mevcut = f.read()
    except OSError:
        mevcut = ""
    kapsam_uygula(kayit, dosya_satirlari(mevcut))
    ozet = ""
    hafta_yol = V.dosya_yolu(os.path.join(klon, *RAPOR_DIZIN.split("/")), dosya_adi(kisi))
    hafta_eski = hafta_oku(klon, harita.values())
    hafta_yeni = dict(hafta_eski)
    if kayit.get("hafta_ozet"):   # this run's weekly summary; the finished count falls back to the published one of the same week
        ho = dict(kayit["hafta_ozet"])
        onceki_h = hafta_eski.get(kisi) or {}
        if ho.get("bitti") is None and onceki_h.get("hafta") == ho.get("hafta"):
            ho["bitti"] = onceki_h.get("bitti")
        hafta_yeni[kisi] = ho
    verim_ayni = V.verim_anahtari(hafta_eski, bugun) == V.verim_anahtari(hafta_yeni, bugun)
    if degisim:
        eski = dosyalari_oku(klon, harita.values())
        yeni_r = satir_ayristir(satir_kur(kayit))
        yeni = {**eski, kisi: [r for r in eski.get(kisi, []) if r["tarih"] != kayit["tarih"]] + [yeni_r]}
        yeni[kisi].sort(key=lambda r: r["tarih"])
        onceki_sira, yeni_sira = sira_anahtari(eski, bugun), sira_anahtari(yeni, bugun)
        bugun_var = any(r["tarih"] == kayit["tarih"] for r in eski.get(kisi, []))
        if bugun_var and onceki_sira == yeni_sira and verim_ayni:
            return "degisiklik yok"
        ozet, kayit["haber"] = degisim_ozeti(onceki_sira, yeni_sira, kisi)
        if not verim_ayni and onceki_sira == yeni_sira and bugun_var:   # only the finished count / weekly dollars moved: say so, no desktop notification
            b0, b1 = (hafta_eski.get(kisi) or {}).get("bitti"), (hafta_yeni.get(kisi) or {}).get("bitti")
            ozet = "biten iş %s→%s" % (b0 if b0 is not None else "—", b1 if b1 is not None else "—") if b0 != b1 else "haftalık maliyet güncellendi"
        kayit["ozet"] = ozet
    if otomatik:
        bugunku = [r for r in dosya_satirlari(mevcut) if r["tarih"] == kayit["tarih"]]
        if bugunku and bugunku[0].get("kota") == kayit.get("kota"):
            return "degisiklik yok"
    yaz(yol, dosyaya_ekle(mevcut, kisi, satir_kur(kayit), kayit["tarih"], kota_guncelle=otomatik, yerine=degisim))
    if kayit.get("hafta_ozet"):
        yaz(hafta_yol, V.ozet_yaz_metni(hafta_yeni[kisi]))
    tum = dosyalari_oku(klon, harita.values())
    yaz(os.path.join(klon, *RAPOR_DIZIN.split("/"), SIRALAMA_DOSYA),
        siralama_metni(tum, bugun, github_commitleri(harita, bugun), son_zamanlar(klon, harita.values()), hafta_oku(klon, harita.values()), roller_oku(klon)))
    git(klon, "add", "--", RAPOR_DIZIN)
    rc, _ = git(klon, "diff", "--cached", "--quiet")
    if rc == 0:
        return "degisiklik yok"
    mesaj = "chore(kullanim): %s %s" % (kisi, bugun) + (" · " + ozet if ozet else "")
    rc, out = git(klon, "commit", "--quiet", "-m", mesaj)
    if rc != 0 and "identity" in out.lower():   # no git identity configured on this machine: commit under the person's name
        rc, out = git(klon, "-c", "user.name=" + kisi, "-c", "user.email=kullanim@claude-shared.invalid", "commit", "--quiet", "-m", mesaj)
    if rc != 0:
        raise RuntimeError("commit: " + out.replace("\n", " ")[:200])
    rc, out = git(klon, "push", "--quiet", "origin", "HEAD:main")
    if rc != 0:
        raise RuntimeError("push: " + out.replace("\n", " ")[:200])
    return "itildi"


def erisim_kontrol(kisi=None, klon=None, uzak=None):
    """(ok, reason): can this machine do what the daily job does? Clone/fetch the shared repo into the job's own clone, check
    that `kisi` is listed in kisiler.json on main, and `git push --dry-run` (contacts the remote and authenticates, writes
    nothing). Used by the installer's --paylas; `maliyet --paylas --kuru` only prints the row and never reaches git."""
    klon, uzak = klon or klon_yolu(), uzak or uzak_adres()
    try:
        klon_hazirla(klon, uzak)
    except RuntimeError as e:
        return False, str(e)
    if kisi is not None and kisi not in kisiler_oku(klon).values():
        return False, "'%s' ortak deponun main dalindaki kisiler.json icinde yok (once ekleyip main'e commitleyin)" % kisi
    rc, out = git(klon, "push", "--dry-run", "--quiet", "origin", "HEAD:main")
    return rc == 0, "" if rc == 0 else "push: " + out.replace("\n", " ")[:200]


# ------------------------------------------------------------------ the status line's automatic share
# statusline.sh / statusline.ps1 start `maliyet --paylas --otomatik` detached when the marker file paylas-sonraki (its mtime = the
# next due time) is in the past. State lives in <claude dir>/maliyet/: paylas-durum.json (son_kontrol, son_paylasim, itilen_kota),
# paylas-sonraki (the shell's one-stat gate), paylas.kilit (one share at a time; older than OTO_KILIT_ESKI s = stale = free).
OTO_ARALIK_SAAT = 2.0
OTO_KILIT_ESKI = 30 * 60
PUSH_DENEME = 3


def oto_yol(ad):
    import maliyet
    return os.path.join(maliyet.CLAUDE, "maliyet", ad)


def oto_aralik_saat():
    """Hours between checks: `paylas_aralik_saat` in <claude dir>/pace-plan.json (a positive number), else OTO_ARALIK_SAAT."""
    import maliyet
    try:
        with open(os.path.join(maliyet.CLAUDE, "pace-plan.json"), encoding="utf-8-sig") as f:
            v = json.load(f).get("paylas_aralik_saat")
    except (OSError, ValueError, AttributeError):
        return OTO_ARALIK_SAAT
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 24 * 30 else OTO_ARALIK_SAAT


def oto_durum_oku():
    try:
        with open(oto_yol("paylas-durum.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def oto_gerekli(durum, simdi, aralik_saat):
    """True when the last check is at least `aralik_saat` hours old (or there never was one)."""
    son = durum.get("son_kontrol")
    if not isinstance(son, (int, float)) or isinstance(son, bool):
        return True
    return simdi - son >= aralik_saat * 3600


def oto_karar(kota, durum):
    """'atla' (no usable weekly %), 'degisiklik yok' (same integer % as the last push) or 'it'. Nothing else counts."""
    if kota is None:
        return "atla"
    return "degisiklik yok" if durum.get("itilen_kota") == kota else "it"


def oto_yaz(durum, simdi, aralik_saat):
    """Save the state and move the marker to the next due time."""
    durum["son_kontrol"] = simdi
    yol = oto_yol("paylas-durum.json")
    try:
        os.makedirs(os.path.dirname(yol), exist_ok=True)
        tmp = yol + ".tmp%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(durum, f)
        os.replace(tmp, yol)
        isaret = oto_yol("paylas-sonraki")
        with open(isaret, "a"):
            pass
        os.utime(isaret, (simdi, simdi + aralik_saat * 3600))
    except OSError as e:
        log_yaz("uyari: paylas durumu yazilamadi: %s" % type(e).__name__)


def oto_kilit_al(simdi):
    """Take paylas.kilit (O_EXCL). A lock older than OTO_KILIT_ESKI seconds is stale and taken over. False = a share is running."""
    yol = oto_yol("paylas.kilit")
    os.makedirs(os.path.dirname(yol), exist_ok=True)
    for _ in (1, 2):
        try:
            os.close(os.open(yol, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if simdi - os.stat(yol).st_mtime < OTO_KILIT_ESKI:
                    return False
                os.unlink(yol)
            except OSError:
                return False
        except OSError:
            return False
    return False


def oto_kilit_birak():
    try:
        os.unlink(oto_yol("paylas.kilit"))
    except OSError:
        pass


# ------------------------------------------------------------------ the job
def paylas(siralama=False, kuru=False, simdi=None, otomatik=False, hazirla=None):
    """The whole daily job after the totals are recorded. Never raises; returns 0. (`siralama` is kept for old drop-ins.)
    otomatik: the status line's throttled run (see above); `hazirla` records the totals, called only when something will be pushed."""
    try:
        return _oto(simdi, hazirla) if otomatik else _paylas(kuru, simdi)
    except Exception as e:  # never fail loudly: the daily recording must not break because sharing did
        log_yaz("HATA %s: %s" % (type(e).__name__, e))
        if otomatik:   # a crashing run must not be restarted by every status-line render: wait for the next interval
            try:
                oto_yaz(oto_durum_oku(), time.time(), oto_aralik_saat())
            except Exception:
                pass
        return 0


def _it(kayit, bugun, otomatik=False, degisim=False):
    """Push attempts (each one re-fetches and resets the job's clone to origin/main, then redoes the row on the fresh base, which is
    what a pull --rebase would give). Returns (result or None, error)."""
    klon, uzak, hata = klon_yolu(), uzak_adres(), None
    for deneme in range(PUSH_DENEME):
        try:
            return bir_deneme(klon, uzak, kayit, bugun, otomatik, degisim), None
        except RuntimeError as e:
            hata = str(e)
            if "kisiler.json" in hata or hata == UZAK_YOK:
                break
            if deneme < PUSH_DENEME - 1:
                time.sleep(1 + random.random() * 3)
    return None, hata


def _oto(simdi, hazirla):
    simdi = time.time() if simdi is None else simdi
    aralik = oto_aralik_saat()
    if not oto_kilit_al(simdi):
        log_yaz("otomatik: baska bir paylasim suruyor, atlandi")
        return 0
    try:
        return _oto_is(simdi, hazirla, aralik)
    finally:
        oto_kilit_birak()


def _oto_is(simdi, hazirla, aralik):
    import maliyet_cekirdek as CK
    durum = oto_durum_oku()
    if not oto_gerekli(durum, simdi, aralik):
        oto_yaz(durum, durum["son_kontrol"], aralik)   # only re-aims the marker; the check time stays
        return 0
    kisi = kisi_adi()
    if not kisi:
        oto_yaz(durum, simdi, aralik)
        log_yaz("otomatik atlandi: kisi adi yok")
        return 0
    kota, _ = kota_bilgi(simdi)
    karar = oto_karar(kota, durum)
    if karar != "it":
        oto_yaz(durum, simdi, aralik)
        log_yaz("otomatik: %s (kota %s)" % (karar, kota))
        return 0
    if hazirla:
        hazirla()
    bugun = CK.bugun_str()
    kayit = bugunku_kayit(kisi, bugun, simdi)
    sonuc, hata = _it(kayit, bugun, otomatik=True)
    if sonuc:
        durum["itilen_kota"] = kayit["kota"]
        if sonuc == "itildi":
            durum["son_paylasim"] = simdi
        log_yaz("otomatik %s: %s" % (sonuc, satir_kur(kayit)))
    else:
        log_yaz("otomatik BASARISIZ: %s" % hata)
    oto_yaz(durum, simdi, aralik)   # a failed push waits for the next interval too
    return 0


def bildir(kisi, ozet):
    """Desktop notification on this machine (notify-send, the same mechanism as kayip_is.py and gelen.py) for a rank or mode change
    of the person's own row. Linux only, best effort, silent when notify-send is missing; CLAUDE_SHARED_BILDIRIM=0 switches it off.
    Nothing is sent to anyone else."""
    if os.environ.get("CLAUDE_SHARED_BILDIRIM") == "0" or not sys.platform.startswith("linux") or not shutil.which("notify-send"):
        return False
    rc, _, _ = komut(["notify-send", "-a", "claude-kullanim", "Claude kullanımı: %s" % kisi, ozet], timeout=10)
    return rc == 0


def hafta_ozeti_hazirla(kisi, bugun):
    """The weekly per-project cost summary plus the finished-work count (maliyet_verim) of this person, or None when it cannot be built
    (no tools/ next to the plugin, no cost data, or CLAUDE_SHARED_VERIM=0). Never raises; the daily row does not depend on it."""
    if os.environ.get("CLAUDE_SHARED_VERIM") == "0":
        return None
    try:
        ho = V.hafta_ozeti(kisi, bugun)
        if ho is None:
            return None
        login = next((l for l, a in kisiler_yerel().items() if a == kisi), None)
        ho["bitti"] = V.bitti_say(login, bugun) if login else None
        return ho
    except Exception as e:
        log_yaz("hafta ozeti hatasi: %s: %s" % (type(e).__name__, e))
        return None


def kisiler_yerel():
    """{login: name} from the kisiler.json next to this plugin (the clone this script runs from); {} when unreadable."""
    return kisiler_oku(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")))


def _paylas(kuru, simdi):
    import maliyet_cekirdek as CK
    kisi = kisi_adi()
    if not kisi:
        log_yaz("atlandi: kisi adi yok (CLAUDE_SHARED_KISI, ~/.config/claude-shared/kisi veya git user.name)")
        return 0
    bugun = CK.bugun_str()
    kayit = bugunku_kayit(kisi, bugun, simdi)
    if kayit["plan"] is None:
        log_yaz("uyari: plan bilinmiyor (CLAUDE_SHARED_PLAN veya ~/.config/claude-shared/plan: 100 ya da 200); satirda plan '—'")
    if kuru:
        print(satir_kur(kayit))
        return 0
    kayit["hafta_ozet"] = hafta_ozeti_hazirla(kisi, bugun)
    # the timer run and the status line's automatic share use the same clone: one at a time (a busy lock = the next 2-hourly run retries)
    if not oto_kilit_al(time.time()):
        log_yaz("paylas: baska bir paylasim suruyor, bu tur atlandi")
        return 0
    try:
        sonuc, hata = _it(kayit, bugun, degisim=True)
    finally:
        oto_kilit_birak()
    if sonuc:
        log_yaz("%s: %s" % (sonuc, satir_kur(kayit)) if sonuc == "itildi" else "degisiklik yok (satir ve sira ayni, zaman damgasi disinda)")
        if sonuc == "itildi":
            log_yaz("degisim: " + kayit.get("ozet", ""))
            if kayit.get("haber"):
                bildir(kisi, kayit["ozet"])
        if kayit.get("kota") is not None:   # the row is the new baseline for the automatic share
            durum = oto_durum_oku()
            durum["itilen_kota"] = kayit["kota"]
            if sonuc == "itildi":
                durum["son_paylasim"] = time.time()
            oto_yaz(durum, time.time(), oto_aralik_saat())
        return 0
    log_yaz("BASARISIZ: %s" % hata)
    return 0

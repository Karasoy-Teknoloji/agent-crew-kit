#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""son-pencere: use ALL leftover weekly Claude quota in the last hours before the reset (opt-in, runs from a timer).

Plain Python, stdlib only. The wrapper itself spends no tokens (the one-shot Haiku relay that delivers the assignment costs a few cents);
the work is done by a normal, visible, interactive Claude Code session. Commands:

  son-pencere tetik [--dry-run]   the timer entry (every 15 min). Gate: target_at <= now < week_resets_at, weekly use < 100%,
                                  no assignment already running (lock), not restarted within 15 min. Then, in the foreground:
                                  1. pick one LIVE interactive session of the owner (highest model first: Opus > Sonnet > Fable > Haiku,
                                     newer model version first, then the oldest session start; headless runs, son-pencere's own sessions
                                     and names in SON_PENCERE_HARIC / <state dir>/haric.txt are skipped) and give it the assignment
                                     with a one-shot headless Haiku relay (ListAgents + SendMessage), the full brief being a file;
                                     The relay's TESLIM binds the run to that session (no second delivery); the next candidate only
                                     on a clear YOK or a relay that could not run; a timed-out relay ends the hand-out for this run;
                                  2. no live session took it: start the fallback `son-pencere-<person>`: interactive claude in a
                                     detached tmux session on son-pencere's own server (`tmux -L son-pencere`; Windows: a Windows Terminal tab);
                                  3. follow the session's registry entry and transcript (status, turns), end the run at the reset (stop
                                     message / `/exit` + kill for our own session), record the run.
                                  --dry-run: print the gate decision, the ranked sessions and the picked items, send/start nothing.
  son-pencere oturum              print the live sessions, who would get the assignment and why the others are excluded
  son-pencere deneme              test of the delivery/follow/cutoff path with a trivial prompt in a scratch state dir (see --help)
  son-pencere secim               print the picked items (a: label, b: board "Yapay Zeka", c: standing-list signals), start nothing
  son-pencere durum [--son N]     latest runs of EVERYONE from the shared log (claude-shared raporlar/son-pencere/)
  son-pencere etiket [--kuru]     create the label yz-son-pencere in the product repos (idempotent)
  son-pencere bitir --ac|--kapat|durum
                                  burn mode ("token bitirme modu"): the same assignment, work order and never-list as the last window, but
                                  outside the window, until the weekly limit is used up, the weekly reset (auto-off) or `--kapat`. Only a
                                  developer (kisiler.json `rol: yazilimci`, checked by the gh login) may switch it on or off; state in bitir.json.

Modes of `tetik` (one gate, `karar(kip=...)`; the timer passes `--kipler`, default `pencere` for timers installed before the option):
  pencere  the last window (above).                       bitir  burn mode, while switched on (any install with the timer).
  devam    the resume alarm (kur --devam-alarmi): once per 5-hour reset that stopped work (pace.json five_hour_used >= 100, the reset
           passed), every live terminal session whose last assistant entry is still the 429 "session limit" stop DEVAM_GECIKME after the
           reset gets one "continue where you stopped" relay. Claude Code's own autoContinueAtUsageLimit resumes an open session by itself;
           this covers what it does not (Esc, three continuations used, sleep over 30 min). No new session, no new items. pace.json without
           a 5-hour reading or stale: the stops are found from the transcripts alone (the reset is at most 5 h after the stop).

Quota data: pace.json (written by the status line of any interactive session, including the one that does the work) and
~/.claude/son-pencere/durum.json. The coordinator prompt is son-pencere-istem.md (next to this file), rendered per run into
runs/<ts>-brief.md. Files, all under <config dir>/son-pencere/:
  <date>.md      the run journal (the coordinator appends DONE/WIP/SKIP/NOTE lines; the wrapper adds a header and a footer)
  runs/*-brief.md  the brief of each run (kept 14 days)     koordinator.kilit   pid lock + the session of the run     durum.json   quota, last start
Shared log: raporlar/son-pencere/<person>.md (one row per run; the Items cell starts with `session:<name>`) and KAYITLAR.md (all people,
newest first) in claude-shared main, pushed through its own clone (~/.cache/claude-shared-son-pencere) with the same ownership rules as
the usage ranking (the name must be listed in kisiler.json; only your own file is written).
Env: SON_PENCERE_CLAUDE (claude executable), SON_PENCERE_MODEL / SON_PENCERE_ARGS (extra `claude` args of the fallback session),
SON_PENCERE_RC (0 = no Remote Control for the fallback session; default on), SON_PENCERE_MAX_TURNS (default 200), SON_PENCERE_IDLE_SN (idle seconds that end a run, 600),
SON_PENCERE_ACK_SN (an assigned session idle this long without the assignment ends the run, 300), SON_PENCERE_ROLE_SN (relay timeout, 300),
SON_PENCERE_RESET_DENEME (relays of the "reset reached" message, 3), SON_PENCERE_HARIC (comma list of session-name patterns to skip),
PACE_HEDEF_SAAT (target hours before the reset, default as the forecast), CLAUDE_CONFIG_DIR, CLAUDE_SHARED_KISI/UZAK,
SON_PENCERE_SECIM (a JSON file replacing the live pick).
Never fails loudly in `tetik`: errors go to <config dir>/son-pencere/son-pencere.log and the exit code stays 0.
"""
import argparse
import datetime
import fnmatch
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # python -I does not add the script directory
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", "tools"))  # repo layout: tools/gizli.py (a --kopya install ships it next to this file)
from gizli_yukle import gizli  # noqa: E402  (one hidden-launch helper)

HOME = os.path.expanduser("~")
CLAUDE = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
DIZIN = os.environ.get("SON_PENCERE_DIZIN") or os.path.join(CLAUDE, "son-pencere")   # SON_PENCERE_DIZIN: tests / `deneme` only
KILIT = os.path.join(DIZIN, "koordinator.kilit")
DURUM = os.path.join(DIZIN, "durum.json")
BETIK_DIZINI = os.path.dirname(os.path.abspath(__file__))
ISTEM = os.path.join(BETIK_DIZINI, "son-pencere-istem.md")

ETIKET = "yz-son-pencere"
ONAY_ETIKET = "prod-onay-bekliyor"
ETIKET_RENK = "5319e7"
ETIKET_ACIKLAMA = "Pre-approved for the AI 'son pencere' (last quota window) run: dev-only, no human approval needed"
DEFAULT_HEDEF_SAAT = 5.0
TEKRAR_ARALIK = 15 * 60       # a coordinator is restarted at most this often
HIZLI_HATA_SN = 120           # a run shorter than this that failed counts as a quick failure
HIZLI_HATA_SINIR = 3          # this many in a row: back off for HATA_BEKLEME
HATA_BEKLEME = 3600
RESET_SON_DAKIKA = 10 * 60    # no restart in the last 10 minutes before the reset
HEDEF_AZAMI_SAAT = 96.0       # the longest window ever (pace_forecast.ABS_MAX_TARGET_HOURS, the hard ceiling of target hours before the reset)
VARSAYILAN_AZAMI_SAAT = 72.0  # the default cap of the target hours; "max_target_hours" in pace-plan.json moves it up to HEDEF_AZAMI_SAAT (as in pace_forecast.py)
KILIT_AZAMI_YAS = (HEDEF_AZAMI_SAAT + 1) * 3600   # a lock older than the longest window is stale even if the pid number is alive (pid reuse)
KIPLER = ("pencere", "bitir", "devam")
BES_SAAT = 5 * 3600
DEVAM_GECIKME = 10 * 60       # the resume alarm waits this long after the 5-hour reset (Claude Code's own auto-continue goes first)
DEVAM_AZAMI_YAS = 12 * 3600   # a limit stop older than this is not resumed by the transcript-only fallback (a session left open for days)
DEVAM_AZAMI_OTURUM = 5        # at most this many sessions get the resume relay per reset
BITIR_BOS_BEKLEME = 3600      # burn mode: a run that ended with nothing done or in progress holds the next start this long
REPO_DIZIN = "raporlar/son-pencere"
KAYITLAR = "KAYITLAR.md"
SATIR_SINIRI = 200
ZAMAN = time.time             # replaced by tests
POLL = float(os.environ.get("SON_PENCERE_POLL") or 10)

_KONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# ------------------------------------------------------------------ small helpers

def org():
    """The GitHub org (config key GH_ORG / ~/.config/claude-shared/org, the same key as the ranking); "" = not configured."""
    import maliyet_paylas as MP
    return MP.github_org()


def urun_repolari():
    """Product repos = the repos linked to the project boards that have a "Yapay Zeka" Status (ops and infra repos are not products).
    Config key SON_PENCERE_REPOLAR / ~/.config/claude-shared/urun-repolari: repo names separated by commas or spaces; default none."""
    import maliyet_paylas as MP
    return tuple(r for r in re.split(r"[,\s]+", MP.ayar_degeri("urun-repolari", "SON_PENCERE_REPOLAR")) if r)



def durum_kanali():
    """(name, id) of the team's AI status channel: config keys SON_PENCERE_KANAL (default #ai-status) and SON_PENCERE_KANAL_ID
    (~/.config/claude-shared/kanal, kanal-id; no default id: the coordinator then finds the channel by name)."""
    import maliyet_paylas as MP
    ad = MP.ayar_degeri("kanal", "SON_PENCERE_KANAL", "#ai-status")
    return ad, MP.ayar_degeri("kanal-id", "SON_PENCERE_KANAL_ID") or "look it up by name"


def log_yaz(mesaj):
    try:
        os.makedirs(DIZIN, exist_ok=True)
        yol = os.path.join(DIZIN, "son-pencere.log")
        try:
            if os.path.getsize(yol) > 200_000:
                with open(yol, encoding="utf-8", errors="replace") as f:
                    son = f.readlines()[-300:]
                with open(yol, "w", encoding="utf-8") as f:
                    f.writelines(son)
        except OSError:
            pass
        with open(yol, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ZAMAN())), _KONTROL.sub(" ", str(mesaj))[:600]))
    except Exception:
        pass


def json_oku(yol):
    try:
        with open(yol, encoding="utf-8-sig") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def json_yaz(yol, nesne):
    os.makedirs(os.path.dirname(yol), exist_ok=True)
    gecici = "%s.%d.tmp" % (yol, os.getpid())
    with open(gecici, "w", encoding="utf-8") as f:
        json.dump(nesne, f, ensure_ascii=False, indent=1)
    os.replace(gecici, yol)


def yerel_zaman(t, saniye=False):
    return time.strftime("%Y-%m-%d %H:%M:%S" if saniye else "%Y-%m-%d %H:%M", time.localtime(t))


def sayi(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ quota: window, readings, gate
def hedef_saat(pace):
    """Hours before the reset where the window starts: the forecast's own setting (PACE_HEDEF_SAAT env, pace-plan.json), else what
    pace.json recorded, else 5. 0 means "no window" (the old behaviour of the forecast): the mode is then off."""
    plan = json_oku(os.path.join(CLAUDE, "pace-plan.json"))
    v = sayi(os.environ.get("PACE_HEDEF_SAAT"))
    if v is None:
        v = sayi(plan.get("target_hours"))
    if v is None:
        v = sayi(pace.get("target_hours"))
    if v is None:
        v = DEFAULT_HEDEF_SAAT
    azami = sayi(plan.get("max_target_hours"))
    azami = VARSAYILAN_AZAMI_SAAT if azami is None else min(max(azami, 0.0), HEDEF_AZAMI_SAAT)
    return min(max(v, 0.0), azami)


def kota_durumu(pace, durum, simdi):
    """The best knowledge of the weekly quota now: {week_used (%, None = unknown), week_resets_at, five_hour_used,
    five_hour_resets_at, kaynak}. The source with the newer `updated` wins (the status line writes pace.json while someone works
    interactively, the wrapper writes durum.json while it runs). A week_used is only trusted when its reset is the chosen reset.
    No reset in the future anywhere: the last known one is rolled forward by whole weeks (the week length is fixed), unknown use."""
    kaynaklar = []
    for ad, d in (("pace", pace), ("durum", durum)):
        r, u = sayi(d.get("week_resets_at")), sayi(d.get("updated"))
        if r:
            kaynaklar.append((u or 0, ad, d, r))
    kaynaklar.sort(key=lambda k: k[0], reverse=True)
    gelecek = [k for k in kaynaklar if k[3] > simdi]
    if gelecek:
        _, ad, d, reset = gelecek[0]
        kullanim = sayi(d.get("week_used"))
        taze = True
    elif kaynaklar:
        reset, ad, kullanim, taze = kaynaklar[0][3], "tahmin", None, False
        while reset <= simdi:
            reset += 7 * 86400
    else:
        return {"week_used": None, "week_resets_at": None, "five_hour_used": None, "five_hour_resets_at": None, "kaynak": "yok"}
    sec = d if gelecek and sayi(d.get("five_hour_resets_at")) else durum   # the status line (pace.json) carries the 5-hour window too
    bes_sonu, bes_oran = sayi(sec.get("five_hour_resets_at")), sayi(sec.get("five_hour_used"))
    return {"week_used": kullanim if taze else None, "week_resets_at": reset, "kaynak": ad,
            "five_hour_used": bes_oran if bes_sonu and bes_sonu > simdi else None,
            "five_hour_resets_at": bes_sonu if bes_sonu and bes_sonu > simdi else None}


def karar(simdi, kota, hedef, kilit_canli, son_baslangic, hizli_hata=0, kip="pencere", bos_bekle=None):
    """Pure gate: ("baslat" | "bekle", reason code, detail). Order matters: window first, then the quota, then the process state.
    `kip`: "pencere" (the last window), "bitir" (burn mode: no window, the caller checked it is switched on) or "devam" (the resume
    alarm: no window, no restart spacing, the caller checked a reset is due; an unknown weekly reset does not block it)."""
    reset = kota.get("week_resets_at")
    if not reset and kip != "devam":
        return "bekle", "reset-bilinmiyor", "no weekly reset time known (pace.json and durum.json are empty)"
    if kip == "pencere":
        if hedef <= 0:
            return "bekle", "pencere-yok", "PACE_HEDEF_SAAT is 0: the forecast has no target window, so there is no last window"
        pencere_basi = reset - hedef * 3600
        if simdi < pencere_basi:
            return "bekle", "pencere-oncesi", "window opens %s (reset %s - %gh)" % (yerel_zaman(pencere_basi), yerel_zaman(reset), hedef)
    if reset and simdi >= reset:
        return "bekle", "reset-sonrasi", "reset %s has passed" % yerel_zaman(reset)
    if kip != "devam" and reset - simdi <= RESET_SON_DAKIKA and son_baslangic:
        return "bekle", "reset-yakin", "less than 10 min to the reset: no restart"
    kullanim = kota.get("week_used")
    if kullanim is not None and kullanim >= 100:
        return "bekle", "kota-bitti", "weekly limit is used up (%g%%); nothing left until the reset" % kullanim
    if kota.get("five_hour_used") is not None and kota["five_hour_used"] >= 100 and kota.get("five_hour_resets_at"):
        return "bekle", "bes-saat-dolu", "5-hour window is full until %s" % yerel_zaman(kota["five_hour_resets_at"])
    if kilit_canli:
        return "bekle", "calisiyor", "a coordinator is already running (lock)"
    if kip == "devam":    # a relay per stopped session: the coordinator's failure back-off and restart spacing do not apply
        return "baslat", "devam", "the 5-hour window that stopped work has reset"
    if hizli_hata >= HIZLI_HATA_SINIR and son_baslangic and simdi - son_baslangic < HATA_BEKLEME:
        return "bekle", "hata-beklemesi", "%d quick failures in a row: backing off 1 h" % hizli_hata
    if kip == "bitir" and bos_bekle and simdi < bos_bekle:
        return "bekle", "bos-bekleme", "the last burn-mode run found nothing to do: next start after %s" % yerel_zaman(bos_bekle)
    if son_baslangic and simdi - son_baslangic < TEKRAR_ARALIK:
        return "bekle", "tekrar-araligi", "last start %s: at most one start per 15 min" % yerel_zaman(son_baslangic)
    if kip == "bitir":
        return "baslat", "bitir-acik", "burn mode is on until the weekly reset %s" % yerel_zaman(reset)
    return "baslat", "pencerede", "inside the window %s .. %s" % (yerel_zaman(pencere_basi), yerel_zaman(reset))


def kip_sec(simdi, kota, hedef, kilit_canli, son_baslangic, hizli_hata, kipler, bitir_acik, devam_hazir, bos_bekle=None):
    """The gate of every mode of this tick: (chosen (kip, eylem, neden, ayrinti) | None, all results). Precedence: the last window, then
    burn mode, then the resume alarm. A mode that is not installed (kipler), not switched on or not due is not asked."""
    sonuclar = []
    for kip, var in (("pencere", "pencere" in kipler), ("bitir", bitir_acik), ("devam", devam_hazir)):
        if var:
            sonuclar.append((kip,) + karar(simdi, kota, hedef, kilit_canli and kip != "devam", son_baslangic, hizli_hata, kip, bos_bekle))
    return next((s for s in sonuclar if s[1] == "baslat"), None), sonuclar


# ------------------------------------------------------------------ burn mode (token bitirme modu): state and the developer check
def bitir_yolu():
    return os.path.join(DIZIN, "bitir.json")


def bitir_oku():
    return json_oku(bitir_yolu())


def bitir_acik(simdi, b=None):
    """Switched on and its weekly reset (stored at activation, never the rolled-forward estimate) not reached."""
    b = bitir_oku() if b is None else b
    return bool(b.get("aktif")) and (sayi(b.get("hafta_reset")) or 0) > simdi


def bitir_suresi_doldu(simdi):
    """Auto-off: a burn mode whose weekly reset passed is switched off (recorded). True when it did that now."""
    b = bitir_oku()
    if not b.get("aktif") or (sayi(b.get("hafta_reset")) or 0) > simdi:
        return False
    b.update(aktif=False, kapatan="otomatik (haftalik sifirlanma)", kapanis=int(simdi))
    json_yaz(bitir_yolu(), b)
    log_yaz("burn mode switched off: the weekly reset %s passed" % yerel_zaman(sayi(b.get("hafta_reset")) or simdi))
    return True


def kisi_rolu(login):
    """(display name, role) of a GitHub login from kisiler.json: the clone this script runs from, else son-pencere's own clone
    (refreshed from origin). (None, None) when the login is not listed or nothing is readable."""
    import maliyet_paylas as MP
    if not login:
        return None, None
    for klon in (os.path.normpath(os.path.join(BETIK_DIZINI, "..", "..", "..")), None):
        if klon is None:
            klon = klon_yolu()
            try:
                MP.klon_hazirla(klon, MP.uzak_adres())
            except Exception as e:
                log_yaz("kisiler.json clone: %s" % e)
        kayit = MP.kisiler_json(klon)
        if kayit:
            return kayit.get(login.lower(), (None, None))
    return None, None


# ------------------------------------------------------------------ resume alarm (devam): the 5-hour reset and the sessions it stopped
LIMIT_RE = re.compile(r"session limit|5-hour limit|usage limit", re.I)
LIMIT_DISI_RE = re.compile(r"spend limit|weekly limit|monthly", re.I)   # a 5-hour reset does not lift these
LIMIT_KUYRUK = 64_000


def iso_epoch(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def limit_durusu(yol):
    """The 5-hour limit stop that ended this transcript, or None: the LAST assistant entry is Claude Code's synthetic 429
    (`error: "rate_limit"`, text "You've hit your session limit · resets 6:30pm"; seen in our transcripts since 16.10.2026).
    Spend and weekly limits are not 5-hour stops. Returns {t (epoch), uuid, metin}."""
    try:
        with open(yol, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - LIMIT_KUYRUK))
            veri = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    son = None
    for satir in veri.splitlines():
        try:
            e = json.loads(satir)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("type") == "assistant":
            son = e
    if not son or son.get("error") != "rate_limit":
        return None
    icerik = (son.get("message") or {}).get("content") or []
    metin = " ".join(c.get("text") or "" for c in icerik if isinstance(c, dict)) if isinstance(icerik, list) else str(icerik)
    t = iso_epoch(son.get("timestamp"))
    if t is None or not LIMIT_RE.search(metin) or LIMIT_DISI_RE.search(metin):
        return None
    return {"t": t, "uuid": son.get("uuid") or son.get("timestamp"), "metin": metin[:160]}


def devam_yolu():
    return os.path.join(DIZIN, "devam.json")


def devam_oku():
    """The resume alarm's own state {anahtar: the last handled 5-hour reset, uyarilan: relayed stop uuids}: its own file (and lock), so it
    works while a coordinator run holds koordinator.kilit and rewrites durum.json from memory."""
    return json_oku(devam_yolu())


def devam_kilit_al(simdi):
    """The resume alarm's own lock (relays take at most DEVAM_AZAMI_OTURUM x the relay timeout); a lock older than 1 h is stale."""
    yol = os.path.join(DIZIN, "devam.kilit")
    os.makedirs(DIZIN, exist_ok=True)
    for _ in range(2):
        try:
            os.close(os.open(yol, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return yol
        except FileExistsError:
            try:
                if simdi - os.path.getmtime(yol) < 3600:
                    return None
                os.remove(yol)
            except OSError:
                return None
    return None


def devam_durumu(pace, dvs, simdi):
    """Is a resume due? {hazir, anahtar, neden, ayrinti, r5}. hazir True: pace.json shows a used-up 5-hour window (five_hour_used >= 100)
    whose reset passed DEVAM_GECIKME ago and is not handled yet (anahtar = that resets_at, recorded once). hazir False: that window is
    still full (or inside the grace): everything waits for it. hazir None, every other case (pace.json missing or stale, or already
    showing a newer window because a session went on): the transcripts decide (limitte_duranlar), each stop uuid at most once."""
    u5, r5 = sayi(pace.get("five_hour_used")), sayi(pace.get("five_hour_resets_at"))
    if r5 and u5 is not None and u5 >= 100 and simdi < r5 + BES_SAAT:
        if simdi < r5 + DEVAM_GECIKME:
            return {"hazir": False, "anahtar": None, "r5": r5, "neden": "devam-bekliyor",
                    "ayrinti": "5-hour window full, resets %s; resume check at %s" % (yerel_zaman(r5), yerel_zaman(r5 + DEVAM_GECIKME))}
        if dvs.get("anahtar") != int(r5):
            return {"hazir": True, "anahtar": int(r5), "r5": r5, "neden": "devam", "ayrinti": "the 5-hour window reset at %s" % yerel_zaman(r5)}
    return {"hazir": None, "anahtar": None, "r5": r5, "neden": "devam-oturumlar",
            "ayrinti": "the limit stops are read from the transcripts"}


def durus_reseti(t, r5):
    """When the 5-hour window of a stop at `t` resets: pace.json's r5 when the stop lies in that window; at the latest the start of a
    newer window pace.json already shows (r5 - 5 h); otherwise at most 5 h after the stop."""
    if r5 and r5 - BES_SAAT <= t < r5:
        return r5
    if r5 and t < r5 - BES_SAAT:
        return min(t + BES_SAAT, r5 - BES_SAAT)
    return t + BES_SAAT


def limitte_duranlar(oturumlar, r5, simdi, uyarilan=(), atla=(), hepsi=False):
    """Live terminal sessions still stopped by a 5-hour limit whose reset (+ DEVAM_GECIKME) has passed: idle, last assistant entry the
    429, not relayed for that stop yet (`uyarilan` = stop uuids), not in `atla` (names). Stops older than DEVAM_AZAMI_YAS are left
    alone. hepsi=True (dry run) lists the stops whose reset has not come yet too. Reads only the registry and a 64 KB tail per idle session."""
    out, haric = [], haric_desenleri()
    for o in oturumlar:
        if elenme_nedeni(o, haric) or o["ad"] in atla or o["durum"] != "idle":
            continue
        d = limit_durusu(o["yol"])
        if not d or d["uuid"] in uyarilan or simdi - d["t"] > DEVAM_AZAMI_YAS:
            continue
        reset = durus_reseti(d["t"], r5)
        if hepsi or simdi >= reset + DEVAM_GECIKME:
            out.append(dict(o, durus=d, reset=reset))
    return sorted(out, key=lambda o: o["basladi"])[:DEVAM_AZAMI_OTURUM]


def devam_mesaji(o):
    return ("[son-pencere on %s] Devam alarmi (resume alarm), automatic, not from a person: the 5-hour usage window that stopped this session "
            "at %s has reset (%s). Continue the work this session was doing when the limit stopped it, from where it stopped: check `git "
            "status`, your task list and your agents' hand-off notes first; commit and push WIP after every step. Only already-started work: "
            "pick nothing new. If that work is finished, stop. Do not reply to this message (the sender is a one-shot relay that has exited)." % (
                makine_adi(), yerel_zaman(o["durus"]["t"]), yerel_zaman(o["reset"])))


def devam_uyandir(duranlar, dvs):
    """Relay the resume message to every stopped session once; record each stop (uuid) in devam.json before the relay, so a crash or a
    slow relay never sends it twice. Returns [(name, relay answer)]."""
    uyarilan = dvs.setdefault("uyarilan", [])
    sonuc = []
    for o in duranlar:
        uyarilan.append(o["durus"]["uuid"])
        del uyarilan[:-50]
        json_yaz(devam_yolu(), dvs)
        cevap = ilet(o["ad"], devam_mesaji(o))
        log_yaz("resume alarm: '%s' (stopped %s) -> %s" % (o["ad"], yerel_zaman(o["durus"]["t"]), cevap))
        sonuc.append((o["ad"], cevap))
    return sonuc


# ------------------------------------------------------------------ process helpers (lock, kill)
class Durduruldu(BaseException):
    """SIGTERM/SIGINT reached the wrapper (systemctl stop, logout): stop the coordinator, still record the run and free the lock."""


def _durdurma_isleyici(signum, frame):
    raise Durduruldu()


def _isleyicileri_kur():
    """SIGTERM/SIGINT/SIGHUP raise Durduruldu, so the run still stops its own session, records itself and frees the lock."""
    for sig in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), _durdurma_isleyici)


def pid_canli(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            kod = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(kod))
            k.CloseHandle(h)
            return bool(ok) and kod.value == 259    # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kilit_oku():
    return json_oku(KILIT)


def kilit_canli(simdi):
    k = kilit_oku()
    if not k:
        return False
    return pid_canli(int(k.get("pid") or 0)) and simdi - (sayi(k.get("t")) or 0) < KILIT_AZAMI_YAS


def kilit_al(simdi):
    os.makedirs(DIZIN, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(KILIT, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if kilit_canli(simdi):
                return False
            try:
                os.remove(KILIT)
            except OSError:
                return False
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "t": int(simdi)}, f)
        return True
    return False


def kilit_guncelle(**alanlar):
    """Add fields (the session that took the assignment) to our own lock."""
    k = kilit_oku()
    if k.get("pid") == os.getpid():
        k.update(alanlar)
        json_yaz(KILIT, k)


def kilit_birak():
    try:
        if kilit_oku().get("pid") == os.getpid():
            os.remove(KILIT)
    except OSError:
        pass


# ------------------------------------------------------------------ the pick (scripted: gh REST + tools/gh_board.py)
def gh(*args, timeout=120):
    """(parsed JSON | None, error text). Only `gh api` and read-only calls are made here."""
    try:
        r = gizli.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, type(e).__name__
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip().replace("\n", " ")[:200]
    try:
        return json.loads(r.stdout), ""
    except ValueError:
        return None, "bad JSON from gh"


def gh_login():
    v = os.environ.get("SON_PENCERE_GH_LOGIN")
    if v:
        return v
    out = gizli.run(["gh", "api", "user", "-q", ".login"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    return out.stdout.strip() if out.returncode == 0 else ""


def araclar_dizini(klon=None):
    """Directory of the kit's tools (gh_board.py): the clone this plugin sits in (a --repodan install, or a
    run from the repo), else the job's own clone refreshed from origin."""
    aday = os.path.normpath(os.path.join(BETIK_DIZINI, "..", "..", "..", "tools"))
    if os.path.isfile(os.path.join(aday, "gh_board.py")):
        return aday
    import maliyet_paylas as MP
    klon = klon or klon_yolu()
    MP.klon_hazirla(klon, MP.uzak_adres())
    return os.path.join(klon, "tools")


def uygun(it, login):
    """Open, not waiting for a production approval, unassigned or assigned to this user."""
    if it.get("state", "open") != "open" or ONAY_ETIKET in it.get("labels", []):
        return False
    atanan = [a.lower() for a in it.get("assignees", [])]
    return not atanan or login.lower() in atanan


def sec_a(login):
    """Issues with the label yz-son-pencere across the org (one REST search call)."""
    if not org():
        return [], "label search: GH_ORG ayarlanmadi (README, Configuration)"
    q = "org:%s label:%s is:issue is:open" % (org(), ETIKET)
    veri, hata = gh("api", "-X", "GET", "search/issues", "-f", "q=" + q, "-f", "per_page=100")
    if veri is None:
        return [], "label search: " + hata
    out = []
    for i in veri.get("items", []):
        repo = i["repository_url"].rsplit("/", 1)[-1]
        it = {"repo": repo, "number": i["number"], "title": i["title"], "url": i["html_url"], "state": i["state"],
              "labels": [l["name"] for l in i.get("labels", [])], "assignees": [a["login"] for a in i.get("assignees", [])]}
        if uygun(it, login):
            out.append(it)
    return sorted(out, key=lambda x: (x["repo"], x["number"])), ""


def tahta_ogeleri(durum_adi, araclar):
    try:
        r = gizli.run([sys.executable, os.path.join(araclar, "gh_board.py"), "items", "--status", durum_adi],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [], "gh_board items: " + type(e).__name__
    if r.returncode != 0:
        return [], "gh_board items: " + (r.stderr or r.stdout).strip().replace("\n", " ")[:200]
    try:
        return json.loads(r.stdout), ""
    except ValueError:
        return [], "gh_board items: bad JSON"


def sec_b(login, ogeler, dislanan=()):
    """Board items in "Yapay Zeka": highest board priority first (rank 0 = most urgent; none = last), then oldest issue first."""
    dis = set(dislanan)
    adaylar = [i for i in ogeler if uygun(i, login) and i["url"] not in dis]
    adaylar.sort(key=lambda i: (i.get("rank", 99), i["number"]))
    for i in adaylar:
        i["repo"] = i["repo"].split("/")[-1]
    return adaylar


def sec_c():
    """Cheap signals for the standing list: dev CI red, open Dependabot PRs, per product repo (2 REST calls each)."""
    kirmizi, dependabot, hatalar = [], [], []
    for repo in urun_repolari():
        runs, h = gh("api", "repos/%s/%s/actions/runs?branch=dev&status=completed&per_page=1" % (org(), repo))
        if runs is None:
            hatalar.append("%s runs: %s" % (repo, h))
        elif runs.get("workflow_runs") and runs["workflow_runs"][0].get("conclusion") in ("failure", "timed_out", "startup_failure"):
            r = runs["workflow_runs"][0]
            kirmizi.append({"repo": repo, "workflow": r.get("name"), "url": r.get("html_url")})
        prs, h = gh("api", "repos/%s/%s/pulls?state=open&per_page=100" % (org(), repo))
        if prs is None:
            hatalar.append("%s pulls: %s" % (repo, h))
        else:
            n = sum(1 for p in prs if str((p.get("user") or {}).get("login", "")).startswith("dependabot"))
            if n:
                dependabot.append({"repo": repo, "acik": n})
    return {"kirmizi_ci": kirmizi, "dependabot": dependabot, "hatalar": hatalar}


def secimler(login=None, hafif=False):
    """Everything the coordinator is handed, plus errors (a failed source is reported, never fatal)."""
    if os.environ.get("SON_PENCERE_SECIM"):    # a JSON file {login, a, b, c, hatalar} replacing the live pick (offline use, tests)
        with open(os.environ["SON_PENCERE_SECIM"], encoding="utf-8") as f:
            return json.load(f)
    login = login or gh_login()
    hatalar = []
    a, h = sec_a(login)
    hatalar += [h] if h else []
    try:
        ogeler, h = tahta_ogeleri("Yapay Zeka", araclar_dizini())
    except Exception as e:
        ogeler, h = [], "board: %s: %s" % (type(e).__name__, e)
    hatalar += [h] if h else []
    b = sec_b(login, ogeler, dislanan={i["url"] for i in a})
    c = {"kirmizi_ci": [], "dependabot": [], "hatalar": []} if hafif else sec_c()
    hatalar += c.pop("hatalar")
    return {"login": login, "a": a, "b": b, "c": c, "hatalar": hatalar}


def oge_satiri(i):
    return "- %s/%s#%s %s | %s%s%s" % (org(), i["repo"], i["number"], i["url"], i["title"][:100],
                                       " | priority: " + i["priority"] if i.get("priority") else "",
                                       " | assigned" if i.get("assignees") else "")


def sec_metni(s, ust_sinir=40):
    def liste(ogeler):
        if not ogeler:
            return "- (none)"
        satirlar = [oge_satiri(i) for i in ogeler[:ust_sinir]]
        if len(ogeler) > ust_sinir:
            satirlar.append("- ... and %d more (re-query)" % (len(ogeler) - ust_sinir))
        return "\n".join(satirlar)
    c = s["c"]
    sinyal = []
    for k in c["kirmizi_ci"]:
        sinyal.append("- CI on dev is red: %s (%s) %s" % (k["repo"], k["workflow"], k["url"]))
    for d in c["dependabot"]:
        sinyal.append("- %d open Dependabot PR(s) in %s" % (d["acik"], d["repo"]))
    return liste(s["a"]), liste(s["b"]), "\n".join(sinyal) or "- (none found)"


def etiket_olustur(kuru=False):
    """Create the label in every product repo (REST, idempotent: 422 = exists). Returns [(repo, durum)]."""
    sonuc = []
    for repo in urun_repolari():
        if kuru:
            sonuc.append((repo, "would create"))
            continue
        r = gizli.run(["gh", "api", "-X", "POST", "repos/%s/%s/labels" % (org(), repo), "-f", "name=" + ETIKET,
                            "-f", "color=" + ETIKET_RENK, "-f", "description=" + ETIKET_ACIKLAMA],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=60)
        if r.returncode == 0:
            sonuc.append((repo, "created"))
        elif "already_exists" in (r.stdout + r.stderr) or "422" in (r.stdout + r.stderr):
            sonuc.append((repo, "exists"))
        else:
            sonuc.append((repo, "ERROR " + (r.stderr or r.stdout).strip().replace("\n", " ")[:120]))
    return sonuc


# ------------------------------------------------------------------ journal
JURNAL_RE = re.compile(r"^- \[\d\d:\d\d\] (DONE|WIP|SKIP|NOTE)(?: (.*))?$")


def jurnal_yolu(t):
    return os.path.join(DIZIN, time.strftime("%Y-%m-%d", time.localtime(t)) + ".md")


def jurnal_ekle(yol, metin):
    os.makedirs(os.path.dirname(yol), exist_ok=True)
    with open(yol, "a", encoding="utf-8") as f:
        f.write(metin)


def jurnal_ogeleri(yol, ofset=0):
    """[(kind, ref, text)] of the DONE/WIP/SKIP/NOTE lines written after byte `ofset`."""
    try:
        with open(yol, "rb") as f:
            f.seek(ofset)
            veri = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for satir in veri.splitlines():
        m = JURNAL_RE.match(satir.strip())
        if not m:
            continue
        kind, rest = m.group(1), (m.group(2) or "").strip()
        if kind == "NOTE":
            out.append((kind, "", rest))
        else:
            ref, _, text = rest.partition(" | ")
            out.append((kind, ref.strip(), text.strip()))
    return out


URL_RE = re.compile(r"^https://github\.com/([^/\s]+)/([^/\s]+)/(issues|pull)/(\d+)")


def kisa_ref(url):
    m = URL_RE.match(url or "")
    return "%s#%s" % (m.group(2), m.group(4)) if m else None


# ------------------------------------------------------------------ live sessions: who gets the assignment
# Claude Code registers every running session in <config dir>/sessions/<pid>.json (name, sessionId, cwd, startedAt, status, kind,
# entrypoint); its transcript is <config dir>/projects/<cwd with every non-alphanumeric char as '-'>/<sessionId>.jsonl, whose
# `assistant` entries carry the model. Verified 01.11.2026 against the live sessions on this machine.
MODEL_PUAN = {"opus": 58, "sonnet": 54, "fable": 49, "haiku": 40}   # AUTONOMY.md / ekip-ajanlari ladder (Artificial Analysis index)
TRANSKRIPT_KUYRUK = 400_000


def oturum_dizini():
    return os.path.join(CLAUDE, "sessions")


def transkript_yolu(cwd, sid):
    return os.path.join(CLAUDE, "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd or ""), "%s.jsonl" % sid)


def son_model(yol):
    """`message.model` of the last assistant entry in the transcript's tail, or None (new session, no file)."""
    try:
        with open(yol, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - TRANSKRIPT_KUYRUK))
            veri = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    model = None
    for satir in veri.splitlines():
        try:
            e = json.loads(satir)
        except ValueError:
            continue
        m = e.get("message") if isinstance(e, dict) and e.get("type") == "assistant" else None
        if isinstance(m, dict) and m.get("model") and not str(m["model"]).startswith("<"):
            model = m["model"]
    return model


def model_puani(model):
    """(ladder score, version tuple) of a model id: claude-opus-5-5 -> (58, (5, 5)). Unknown -> (-1, ()), ranked last."""
    m = str(model or "").lower()
    for aile, puan in MODEL_PUAN.items():
        if aile in m:
            surum = tuple(int(x) for x in re.findall(r"(?<!\d)\d{1,2}(?!\d)", m.split(aile, 1)[1])[:2])
            return puan, surum
    return -1, ()


def oturumlari_oku(model=True):
    """Every registered session whose process is alive: [{pid, ad, sid, cwd, basladi, durum, kind, giris, model, yol}]. model=False skips
    the transcript read (the resume check needs only the registry and its own short tail read)."""
    out = []
    try:
        adlar = os.listdir(oturum_dizini())
    except OSError:
        return out
    for ad in sorted(adlar):
        if not ad.endswith(".json"):
            continue
        d = json_oku(os.path.join(oturum_dizini(), ad))
        pid, sid = int(sayi(d.get("pid")) or 0), d.get("sessionId")
        if not pid or not sid or not kayit_gecerli(d):     # dead, or a leftover file whose pid number was reused
            continue
        yol = transkript_yolu(d.get("cwd"), sid)
        out.append({"pid": pid, "ad": d.get("name") or "", "sid": sid, "cwd": d.get("cwd") or "", "basladi": (sayi(d.get("startedAt")) or 0) / 1000.0,
                    "durum": d.get("status") or "", "kind": d.get("kind"), "giris": d.get("entrypoint"), "yol": yol,
                    "model": son_model(yol) if model else None})
    return out


def haric_desenleri():
    """Names the user keeps out of the assignment (shell patterns): SON_PENCERE_HARIC (comma list) and <son-pencere dir>/haric.txt (one per line)."""
    desen = [x.strip() for x in (os.environ.get("SON_PENCERE_HARIC") or "").split(",") if x.strip()]
    try:
        with open(os.path.join(DIZIN, "haric.txt"), encoding="utf-8") as f:
            desen += [s.strip() for s in f if s.strip() and not s.lstrip().startswith("#")]
    except OSError:
        pass
    return desen


def elenme_nedeni(o, haric, hedef=None):
    """Why this session cannot take the assignment, or None. Only a person's terminal session (`cli`) qualifies: headless/SDK runs
    (the old `claude -p` coordinator shows up as kind=interactive, entrypoint=sdk-cli) and son-pencere's own sessions never do."""
    if hedef:                                   # test hook: exactly this name, nothing else is considered
        return None if o["ad"] == hedef and o["giris"] == "cli" else "not the test target"
    if o["giris"] != "cli":
        return "not a terminal session (%s)" % o["giris"]
    if not o["ad"]:
        return "no name (cannot be addressed)"
    if o["ad"].lower().startswith("son-pencere"):
        return "son-pencere's own session"
    if any(fnmatch.fnmatchcase(o["ad"].lower(), d.lower()) for d in haric):
        return "excluded by SON_PENCERE_HARIC / haric.txt"
    return None


def adaylari_sirala(oturumlar, haric=(), hedef=None):
    """(candidates best first, [(session, reason)] for the rest). Highest model score first, then the newer model version, then the
    oldest session start. A name shared by several live sessions cannot be addressed and is dropped."""
    aday, elenen = [], []
    for o in oturumlar:
        n = elenme_nedeni(o, haric, hedef)
        (elenen if n else aday).append((o, n) if n else o)
    adetler = {}
    for o in aday:
        adetler[o["ad"]] = adetler.get(o["ad"], 0) + 1
    tekil = [o for o in aday if adetler[o["ad"]] == 1]
    elenen += [(o, "name shared by %d live sessions" % adetler[o["ad"]]) for o in aday if adetler[o["ad"]] > 1]
    tekil.sort(key=lambda o: (-model_puani(o["model"])[0], tuple(-x for x in (model_puani(o["model"])[1] + (0, 0, 0))[:3]), o["basladi"]))
    return tekil, elenen


def oturum_tablosu(oturumlar, haric=(), hedef=None):
    aday, elenen = adaylari_sirala(oturumlar, haric, hedef)
    simdi = ZAMAN()
    satirlar = []
    for i, o in enumerate(aday):
        satirlar.append("  %s %-24s %-20s %-5s age %-7s" % ("-> " if i == 0 else "   ", o["ad"][:24], (o["model"] or "model unknown")[:20], o["durum"] or "?", sure_metni(simdi - o["basladi"])))
    for o, n in elenen:
        satirlar.append("     %-24s %-20s %-5s excluded: %s" % (o["ad"][:24] or "(no name)", (o["model"] or "-")[:20], o["durum"] or "?", n))
    return aday, satirlar


def sure_metni(sn):
    sn = max(int(sn), 0)
    return "%dd%02dh" % (sn // 86400, sn % 86400 // 3600) if sn >= 86400 else "%dh%02dm" % (sn // 3600, sn % 3600 // 60)


# ------------------------------------------------------------------ delivery: the existing relay pattern
ILET_RE = re.compile(r"\b(TESLIM|YOK)\b")


def ilet_sonucu(cikti):
    """'teslim' | 'yok' | None: the LAST answer word of the relay's output, with or without markdown (`**TESLIM**`)."""
    m = ILET_RE.findall(cikti or "")
    return {"TESLIM": "teslim", "YOK": "yok"}[m[-1]] if m else None


def ilet(ad, metin, zaman_asimi=None):
    """Hand `metin` to the live session named `ad`. A cross-session message can only be sent from inside a Claude session, so a one-shot
    headless Haiku does it with ListAgents + SendMessage (same pattern as ~/.claude/bin/takilan-bekci.py and sunucu-oturum.sh `ilet`;
    no direct CLI/IPC route exists: the per-session socket in cc-socks is undocumented). Returns:
      'teslim'   the relay answered TESLIM (sent; a busy target shows it only at the end of its current turn)
      'yok'      a clear YOK (no such session, not sent)
      'hata'     the relay could not run, or exited with an error and no answer word: nothing is known to be sent
      'belirsiz' the relay timed out, or exited cleanly without an answer word: it MAY have sent, so nobody else may get the same text."""
    istem = ("You are only a message relay. Call ListAgents and find the live session whose name is exactly '%s' (there must be exactly one). "
             "If it exists, call SendMessage to it with the text inside the <ileti> block below, verbatim, and nothing else. Do nothing else. "
             "Finish with one single line: TESLIM (sent) or YOK (no such session or not sent).\n\n<ileti>\n%s\n</ileti>" % (ad, metin))
    kom = [claude_yolu(), "-p", "--model", "haiku", "--tools", "SendMessage,ListAgents", "--allowedTools", "SendMessage,ListAgents",
           "--strict-mcp-config", "--permission-mode", "bypassPermissions", "--no-session-persistence"]
    sn = zaman_asimi or float(os.environ.get("SON_PENCERE_ROLE_SN") or 300)
    try:
        r = gizli.run(kom, input=istem, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=sn,
                           cwd=HOME if os.path.isdir(HOME) else None,
                           env=dict(os.environ, ISTEK_GUNLUGU_ATLA="1"))
    except subprocess.TimeoutExpired:
        log_yaz("relay to '%s' timed out after %g s: it may have sent" % (ad, sn))
        return "belirsiz"
    except OSError as e:
        log_yaz("relay to '%s' could not run: %s" % (ad, type(e).__name__))
        return "hata"
    sonuc = ilet_sonucu(r.stdout)
    son = (r.stdout.strip().splitlines() or ["?"])[-1].strip()
    log_yaz("relay to '%s': %s%s -> %s" % (ad, son[:80], "" if r.returncode == 0 else " (exit %s)" % r.returncode, sonuc or "no answer word"))
    if sonuc:
        return sonuc
    return "hata" if r.returncode != 0 else "belirsiz"


def transkript_oku(yol, ofset):
    """(new complete lines after byte `ofset`, new offset)."""
    try:
        with open(yol, "rb") as f:
            f.seek(ofset)
            veri = f.read()
    except OSError:
        return [], ofset
    son = veri.rfind(b"\n") + 1
    return [s.decode("utf-8", errors="replace") for s in veri[:son].split(b"\n") if s], ofset + son


# ------------------------------------------------------------------ the run
def claude_yolu():
    return os.environ.get("SON_PENCERE_CLAUDE") or shutil.which("claude") or "claude"


def kok_dizin():
    import maliyet_paylas as MP
    return MP.kok_dizin() or HOME


def makine_adi():
    import socket
    return socket.gethostname()


def pencere_adi():
    """Name of the session son-pencere starts itself (the fallback): son-pencere-<person>; also the tmux session name."""
    import maliyet_paylas as MP
    return "son-pencere-" + MP.dosya_adi(MP.kisi_adi() or "kisi")


KIP_ADI = {"pencere": "\"son pencere\" (last quota window)", "bitir": "\"token bitirme modu\" (burn mode)"}
GIRIS_ATAMA = ("You were chosen by `son-pencere` on {{MAKINE}} (plugin kota-temposu; the owner's timer started it, this is not a message from a person) to run "
               "{{KISI}}'s {{KIP_ADI}} NOW, in this session. The owner may be watching: this is a normal, visible session. "
               "Anything the owner tells you directly outranks this brief. If you are in the middle of something, bring it to a clean point (commit, push) first, "
               "then start. Nobody can approve anything for you: work only on the pre-approved items below.")
GIRIS_OTURUM = ("You are the coordinator of {{KISI}}'s {{KIP_ADI}}, started automatically by `son-pencere tetik` (plugin kota-temposu) in the "
                "interactive session `{{OTURUM}}`. The owner may be watching it live (tmux / terminal / Remote Control) and may type into it: his own "
                "instructions outrank this brief. Nobody can approve anything for you: work only on the pre-approved items below.")
KIP_BLOK_RE = re.compile(r"<!-- kip:([a-z,]+) -->\n(.*?)<!-- /kip -->\n", re.S)


def kip_bloklari(metin, kip):
    """Keep the `<!-- kip:a,b -->` ... `<!-- /kip -->` blocks of the brief that list `kip`, drop the others (one brief for every mode)."""
    return KIP_BLOK_RE.sub(lambda m: m.group(2) if kip in m.group(1).split(",") else "", metin)
BITIS_ATAMA = ("go idle (do not exit; it is the owner's session). The wrapper sends a fresh assignment at the next tick if quota is left")
BITIS_OTURUM = "exit (`/exit`); the wrapper starts a fresh coordinator at the next tick"


def istem_kur(kota, baslangic, jurnal, sec, mod="oturum", oturum="", kip="pencere"):
    import maliyet_paylas as MP
    a, b, c = sec_metni(sec)
    kisi = MP.kisi_adi() or "the user"
    giris = (GIRIS_ATAMA if mod == "atama" else GIRIS_OTURUM).replace("{{MAKINE}}", makine_adi()).replace("{{KISI}}", kisi).replace(
        "{{OTURUM}}", oturum).replace("{{KIP_ADI}}", KIP_ADI.get(kip, KIP_ADI["pencere"]))
    bt = bitir_oku() if kip == "bitir" else {}
    degerler = {"GIRIS": giris, "BITIS": BITIS_ATAMA if mod == "atama" else BITIS_OTURUM,
                "BITIR_ACAN": str(bt.get("acan") or "a developer"), "BITIR_ACILIS": yerel_zaman(sayi(bt.get("acilis")) or baslangic),
                "KISI": kisi, "LOGIN": sec.get("login") or "the user", "RESET_ISO": yerel_zaman(kota["week_resets_at"]),
                "RESET_EPOCH": str(int(kota["week_resets_at"])),
                "WEEK_USED": ("%g%%" % kota["week_used"]) if kota.get("week_used") is not None else "unknown",
                "BASLANGIC_ISO": yerel_zaman(baslangic), "JOURNAL": jurnal, "TOOLS": sec.get("araclar") or "tools",
                "ETIKET": ETIKET, "ONAY_ETIKET": ONAY_ETIKET, "KANAL": durum_kanali()[0], "KANAL_ID": durum_kanali()[1], "SEC_A": a, "SEC_B": b, "SEC_C": c}
    with open(ISTEM, encoding="utf-8") as f:
        metin = kip_bloklari(f.read(), kip)
    for k, v in degerler.items():
        metin = metin.replace("{{%s}}" % k, v)
    return metin


def ozet_mesaj(isaret, brief, jurnal, reset, kisi, kip="pencere"):
    """The message that goes to the chosen session: who sends it, the essentials, and the path of the full brief (rendered to a file so
    the relay does not have to copy 7 KB and the brief survives)."""
    kapanis = " (or when burn mode is switched off)" if kip == "bitir" else ""
    return ("[son-pencere on %s] %s: assignment for the %s, automatic, not from a person. Run id %s.\n"
            "Read %s now and follow it; it is the full brief (pre-approved item order, the never-list, journal format, Slack status lines).\n"
            "Essentials: work only on the pre-approved items in the brief; post exactly ONE Slack status line per item in {{KANAL}} "
            "(<person> · <repo> · <area/path> · <issue url> · başladı, then bitti|devredildi as a reply in that item's thread) and nothing else in Slack; "
            "append DONE/WIP/SKIP/NOTE lines to %s after every step; commit and push WIP after every step; start nothing new in the last 10 minutes; "
            "the weekly quota resets at %s%s, when I will send \"reset reached\": then start nothing new, push WIP, report.\n"
            "I am a one-shot relay that exits right after sending: do not reply to me (SendMessage to me fails). Report through the journal and the issues." % (
                makine_adi(), kisi, KIP_ADI.get(kip, KIP_ADI["pencere"]), isaret, brief, jurnal, yerel_zaman(reset), kapanis)).replace(
        "{{KANAL}}", durum_kanali()[0])


def surec_baslangici(pid):
    """Start time of process `pid` as Claude Code writes it into the registry's `procStart` (Linux: field 22 of /proc/<pid>/stat,
    clock ticks since boot; checked 01.11.2026 against every live entry), or None where it cannot be read (other systems)."""
    try:
        with open("/proc/%d/stat" % pid, encoding="ascii", errors="replace") as f:
            st = f.read()
        return st[st.rfind(")") + 2:].split()[19]
    except (OSError, IndexError, ValueError):
        return None


def kayit_gecerli(d):
    """A registry entry whose process is alive AND is the process that wrote it: a leftover file of a dead session whose pid number was
    reused has a different procStart (compared only where both are known)."""
    pid = int(sayi(d.get("pid")) or 0)
    if not pid or not pid_canli(pid):
        return False
    yazilan, simdiki = d.get("procStart"), surec_baslangici(pid)
    return yazilan in (None, "") or simdiki is None or str(yazilan) == simdiki


class Oturum:
    """A session the run follows: either an existing live session that received the assignment (`atama`), or the one son-pencere
    starts itself (tmux on POSIX, a Windows Terminal tab on Windows)."""
    def __init__(self, ad, sid=None, cwd=None, mod="atama"):
        self.ad, self.sid, self.cwd, self.mod, self.pid = ad, sid, cwd, mod, None

    @property
    def yol(self):
        return transkript_yolu(self.cwd, self.sid)

    def kayit(self):
        """Registry entry of this session, or None when it is gone. Found by sessionId, else (once its pid is known) by the same pid and
        name: `/clear` gives the same process a new sessionId and transcript, which the follow loop then reads (self.sid is updated)."""
        girdiler = []
        for ad in os.listdir(oturum_dizini()) if os.path.isdir(oturum_dizini()) else ():
            if ad.endswith(".json"):
                girdiler.append(json_oku(os.path.join(oturum_dizini(), ad)))
        for d in girdiler:
            if d.get("sessionId") == self.sid and kayit_gecerli(d):
                self.pid = int(d["pid"])
                return d
        if self.pid:
            for d in girdiler:
                if int(sayi(d.get("pid")) or 0) == self.pid and (d.get("name") or "") == self.ad and d.get("sessionId") and kayit_gecerli(d):
                    log_yaz("'%s' (pid %d) has a new session id %s (was %s)" % (self.ad, self.pid, d["sessionId"], self.sid))
                    self.sid = d["sessionId"]
                    self.cwd = d.get("cwd") or self.cwd
                    return d
        return None

    def canli(self):
        return self.kayit() is not None

    def bosta(self):
        d = self.kayit()
        return bool(d) and d.get("status") == "idle"

    def mesaj(self, metin, zaman_asimi=None):
        """'teslim' | 'yok' | 'hata' | 'belirsiz' (see ilet)."""
        return ilet(self.ad, metin, zaman_asimi)

    def durdur(self):
        """Assigned sessions are the owner's: they are told to stop (mesaj), never killed."""


def kayit_sn():
    return float(os.environ.get("SON_PENCERE_KAYIT_SN") or 120)


class TmuxOturum(Oturum):
    def __init__(self, ad, cwd):
        import uuid
        super().__init__(ad, str(uuid.uuid4()), cwd, "tmux")
        self.neden = None

    def baslat(self, kom, env):
        """Detached tmux session running the interactive claude. `env` is passed with env(1) so it also reaches a pre-existing tmux server."""
        tmux("kill-session", "-t", "=" + self.ad)             # a leftover of a crashed wrapper under the same, own name
        satir = "exec env " + " ".join("%s=%s" % (k, shlex.quote(v)) for k, v in env.items()) + " " + " ".join(shlex.quote(x) for x in kom)
        rc, out = tmux("new-session", "-d", "-s", self.ad, "-x", "220", "-y", "50", "-c", self.cwd, satir)
        if rc != 0:
            raise OSError("tmux new-session: " + out.strip()[:200])

    def canli(self):
        return tmux("has-session", "-t", "=" + self.ad)[0] == 0

    def ekran(self):
        return tmux("capture-pane", "-p", "-t", "=" + self.ad + ":")[1]

    def kayit_bekle(self, sn=None):
        """Wait until the started claude registered itself (it is then up and working on the brief). Meanwhile the one dialog the wrapper
        answers is the folder trust ("Yes, I trust this folder": a folder Claude Code has not seen interactively asks it, and the folder
        is the owner's own repos root, the cwd the coordinator always ran in; dialog text checked on Claude Code 2.1.316). Nothing else
        is ever pressed: the bypass-permissions warning is the owner's decision (accept it once interactively, see KURULUM.md). Not
        registered within SON_PENCERE_KAYIT_SN (120 s): the pane goes to the log, the session is killed, self.neden says why."""
        bitis = ZAMAN() + (sn or kayit_sn())
        bypass_goruldu = guven_cevaplandi = False
        while True:
            if self.kayit():
                return True
            if not self.canli():
                self.neden = "the session ended before it registered"
                break
            ekran = self.ekran()
            if "I trust this folder" in ekran and not guven_cevaplandi:
                if re.search(r"[❯>]\s*(1\.\s*)?Yes, I trust", ekran):
                    tmux("send-keys", "-t", "=" + self.ad + ":", "Enter")
                    log_yaz("answered the folder-trust dialog of '%s' for %s" % (self.ad, self.cwd))
                    guven_cevaplandi = True
                else:
                    tmux("send-keys", "-t", "=" + self.ad + ":", "Down")
            elif "bypass" in ekran.lower() and not bypass_goruldu:
                bypass_goruldu = True
                log_yaz("'%s' shows the bypass-permissions warning: not answered (accept it once in an interactive session)" % self.ad)
            if ZAMAN() >= bitis:
                self.neden = "not registered within %d s%s" % (sn or kayit_sn(), " (bypass-permissions warning on screen)" if bypass_goruldu else "")
                break
            time.sleep(1)
        satirlar = [s.rstrip() for s in self.ekran().splitlines() if s.strip()][-25:]
        log_yaz("'%s' %s; its pane:" % (self.ad, self.neden))
        for s in satirlar:
            log_yaz("  | " + s)
        tmux("kill-session", "-t", "=" + self.ad)             # no keys: a dialog must never be answered by the cutoff either
        return False

    def mesaj(self, metin, zaman_asimi=None):
        tmux("send-keys", "-t", "=" + self.ad + ":", "-l", metin.replace("\n", " "))
        time.sleep(0.5)
        return "teslim" if tmux("send-keys", "-t", "=" + self.ad + ":", "Enter")[0] == 0 else "hata"

    def durdur(self):
        """The cutoff: interrupt the turn, `/exit`, then kill the session (this is our own session)."""
        if not self.canli():
            return
        tmux("send-keys", "-t", "=" + self.ad + ":", "Escape")
        time.sleep(1)
        tmux("send-keys", "-t", "=" + self.ad + ":", "-l", "/exit")
        time.sleep(0.5)
        tmux("send-keys", "-t", "=" + self.ad + ":", "Enter")
        for _ in range(20):
            if not self.canli():
                return
            time.sleep(1)
        tmux("kill-session", "-t", "=" + self.ad)


def _taskkill(pid):
    gizli.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=30)


def _sid_surecleri(sid):
    """Windows: pids of the processes whose command line carries our own session id (the claude of a tab that never registered).
    The id is a fresh uuid4 of this run, so this matches only our own claude; the PowerShell process itself is left out."""
    if not re.fullmatch(r"[0-9a-f-]{36}", sid or ""):
        return []
    ps = ("Get-CimInstance Win32_Process -Filter \"CommandLine like '%%%s%%'\" | Where-Object { $_.ProcessId -ne $PID } | "
          "ForEach-Object { $_.ProcessId }" % sid)
    try:
        r = gizli.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=60,
                      stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(x) for x in r.stdout.split() if x.isdigit()]


class SekmeOturum(Oturum):
    """Windows: an interactive claude in a Windows Terminal tab (UNTESTED on Windows). The tab is not our child (wt.exe returns at once),
    so it is followed through the session registry once it registered, and ended by pid after it was told to stop."""
    def __init__(self, ad, cwd):
        import uuid
        super().__init__(ad, str(uuid.uuid4()), cwd, "sekme")
        self.neden = None

    def baslat(self, kom, env):
        os.makedirs(os.path.join(DIZIN, "runs"), exist_ok=True)
        cmd = os.path.join(DIZIN, "runs", "%s.cmd" % self.ad)
        with open(cmd, "w", encoding="utf-8", newline="\r\n") as f:
            f.write("@echo off\r\ncd /d \"%s\"\r\n" % self.cwd)
            for k, v in env.items():
                f.write("set \"%s=%s\"\r\n" % (k, v.replace("%", "%%")))
            f.write(subprocess.list2cmdline(kom) + "\r\n")
        wt = shutil.which("wt.exe")
        kom_wt = [wt, "-w", "0", "new-tab", "--title", self.ad, "cmd", "/c", cmd] if wt else ["cmd", "/c", "start", self.ad, "cmd", "/c", cmd]
        # gorunur: a terminal tab opened on purpose for the person (they watch and answer in it): its window must stay visible
        gizli.popen(kom_wt, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, gorunur=True)

    def kayit_bekle(self, sn=None):
        """The tab registers only after claude started in it: wait for it (SON_PENCERE_KAYIT_SN, 120 s) before following it, so the
        first follow pass does not mistake a starting tab for a closed one. Not registered: stop its claude (found by our session id)."""
        bitis = ZAMAN() + (sn or kayit_sn())
        while not self.kayit():
            if ZAMAN() >= bitis:
                self.neden = "not registered within %d s" % (sn or kayit_sn())
                pids = _sid_surecleri(self.sid)
                log_yaz("tab '%s' %s; stopping its claude: %s" % (self.ad, self.neden, ", ".join(map(str, pids)) or "no process found"))
                for pid in pids:
                    _taskkill(pid)
                return False
            time.sleep(2)
        return True

    def durdur(self):
        if not getattr(self, "_durdu", False) and self.kayit() and self.pid:
            self._durdu = True                    # once: the registry entry may outlive the killed process for a moment
            _taskkill(self.pid)


TMUX_SOKET = "son-pencere"   # own tmux server (`tmux -L son-pencere`): never the user's server, and it lives and dies with the unit's cgroup


def tmux(*args, timeout=30):
    """Every tmux call of son-pencere goes to its own server socket: a server started here lives in the systemd unit's cgroup and is
    killed with it, and the user's own tmux sessions (default socket) are never in that cgroup or touched."""
    try:
        r = gizli.run([os.environ.get("SON_PENCERE_TMUX") or shutil.which("tmux") or "tmux", "-L", TMUX_SOKET, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, type(e).__name__


ORTAM_ANAHTARLARI = ("PATH", "HOME", "SSH_AUTH_SOCK", "CLAUDE_CONFIG_DIR", "GH_CONFIG_DIR", "XDG_CONFIG_HOME", "GIT_TERMINAL_PROMPT", "GIT_CONFIG_COUNT",
                     "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "CLAUDE_SHARED_KISI", "CLAUDE_SHARED_KOK", "CLAUDE_SHARED_UZAK", "PACE_HEDEF_SAAT")


def oturum_komutu(ad, sid, brief):
    """Interactive claude with the brief as the first prompt (the prompt goes first so an optional-value flag in SON_PENCERE_ARGS, e.g.
    `--remote-control NAME`, cannot swallow it). bypassPermissions like the owner's own sessions."""
    kom = [claude_yolu(), "Read %s and follow it. It is the \"son pencere\" brief." % brief, "--name", ad, "--session-id", sid,
           "--permission-mode", "bypassPermissions"]
    if os.environ.get("SON_PENCERE_RC", "1") != "0":   # visible from claude.ai/code and the mobile app (verified 01.11.2026); SON_PENCERE_RC=0 turns it off
        kom += ["--remote-control", ad]
    if os.environ.get("SON_PENCERE_MODEL"):
        kom += ["--model", os.environ["SON_PENCERE_MODEL"]]
    if os.environ.get("SON_PENCERE_ARGS"):
        kom += shlex.split(os.environ["SON_PENCERE_ARGS"], posix=(os.name != "nt"))
    return kom


def zaman_var(reset, son_dakika):
    """True while a new hand-out may still start: before the reset minus `son_dakika` (the gate's last-10-minutes rule)."""
    return ZAMAN() < reset - son_dakika


def teslim_et(adaylar, mesaj_metni, reset, son_dakika):
    """Relay the assignment to the best candidate; the next candidate ONLY on a clear 'yok' or a relay 'hata' (nothing was sent).
    Returns (Oturum | None, durum):
      'teslim'   the relay answered TESLIM: this session has it. The run stays with it even when the message does not show up in its
                 transcript yet (a busy session receives it at the end of its turn); no other session and no fallback get it.
      'belirsiz' the relay timed out or gave no answer word: it may have sent, so no other session and no fallback get it this run.
      'yok'      no candidate took it: the fallback may start.
      'reset'    the reset (or its last 10 minutes) came while candidates were tried: nothing more is handed out."""
    for o in adaylar[:3]:
        if not zaman_var(reset, son_dakika):
            log_yaz("reset is near (%s): no more candidates" % yerel_zaman(reset))
            return None, "reset"
        ofset = os.path.getsize(o["yol"]) if os.path.exists(o["yol"]) else 0
        h = Oturum(o["ad"], o["sid"], o["cwd"], "atama")
        h.pid = o.get("pid")
        sonuc = ilet(o["ad"], mesaj_metni)
        if sonuc == "teslim":
            h.ofset, h.teslim_t = ofset, ZAMAN()
            return h, "teslim"
        if sonuc == "belirsiz":
            log_yaz("relay to '%s' gave no clear answer: it may have sent, so no other session gets the assignment this run" % o["ad"])
            return None, "belirsiz"
        log_yaz("assignment not sent to '%s' (%s); trying the next candidate" % (o["ad"], sonuc))
    return None, "yok"


def _sinyalsiz(is_):
    """Run a cleanup step with SIGTERM/SIGINT/SIGHUP ignored, so a second signal (systemctl stop, Ctrl-C) cannot cut it short."""
    eski = {}
    for ad in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, ad):
            try:
                eski[ad] = signal.signal(getattr(signal, ad), signal.SIG_IGN)
            except (ValueError, OSError):      # not the main thread
                pass
    try:
        is_()
    except Exception as e:
        log_yaz("cleanup failed: %s: %s" % (type(e).__name__, e))
    finally:
        for ad, f in eski.items():
            try:
                signal.signal(getattr(signal, ad), f)
            except (ValueError, OSError, TypeError):
                pass


def calistir(kota, durum, sec, baslangic, deneme=None, kip="pencere", atla=()):
    """One run (`kip` pencere or bitir; `atla` = names of sessions that just got the resume relay and keep their own work): give the assignment to the best live session (or start the visible fallback session), follow it, end it at the reset.
    Returns the run record (without the shared-log part). `deneme` ({hedef, tmux_ad, istem}) is the test mode of `son-pencere deneme`.
    Whatever happens after the fallback session was started (an exception, SIGTERM), that session is stopped before this returns."""
    reset = kota["week_resets_at"]
    # the gate's last-10-minutes rule inside the run: a run that started before the last 10 minutes hands nothing out in them (several
    # candidates x relay timeouts take a while); a first start inside them, which the gate allows, is bounded by the reset itself
    son_dakika = RESET_SON_DAKIKA if reset - baslangic > RESET_SON_DAKIKA else 0
    jurnal = jurnal_yolu(baslangic)
    os.makedirs(os.path.join(DIZIN, "runs"), exist_ok=True)
    damga = time.strftime("%Y%m%d-%H%M%S", time.localtime(baslangic))
    isaret = "SP-" + damga
    brief = os.path.join(DIZIN, "runs", damga + "-brief.md")
    jurnal_ekle(jurnal, "\n## Run %s%s (weekly use %s at start, reset %s)\n\n" % (
        yerel_zaman(baslangic, True), " [burn mode]" if kip == "bitir" else "", ("%g%%" % kota["week_used"]) if kota.get("week_used") is not None else "?", yerel_zaman(reset)))
    ofset = os.path.getsize(jurnal)
    kayit = {"bas": baslangic, "hafta_bas": kota.get("week_used"), "jurnal": jurnal, "reset": reset, "neden": None, "maliyet": None, "cikis": None,
             "oturum": None, "mod": None, "tur": 0, "kip": kip}
    env = {k: os.environ[k] for k in ORTAM_ANAHTARLARI if os.environ.get(k)}
    env.update(SON_PENCERE="1", SON_PENCERE_JURNAL=jurnal, SON_PENCERE_RESET=str(int(reset)))
    kisi = _kisi()
    h, basladi = None, False
    try:
        # 1. primary path: a live interactive session of the owner
        hedef = deneme["hedef"] if deneme else None
        # the test of the fallback (deneme --tmux) must never hand anything to a live session: skip the live-session step altogether
        aday = [] if deneme and deneme.get("tmux_ad") else adaylari_sirala([o for o in oturumlari_oku() if o["ad"] not in atla], haric_desenleri(), hedef)[0]
        teslim = None
        if aday:
            _yaz(brief, deneme["istem"] if deneme else istem_kur(kota, baslangic, jurnal, sec, "atama", kip=kip))
            h, teslim = teslim_et(aday, ozet_mesaj(isaret, brief, jurnal, reset, kisi, kip), reset, son_dakika)
        elif not deneme:
            log_yaz("no live interactive session can take the assignment")
        if teslim == "belirsiz":
            kayit["neden"], kayit["cikis"] = "teslim-belirsiz", 1
        elif h is None and (not deneme or deneme.get("tmux_ad")):
            # 2. fallback: a visible session of our own
            if teslim == "reset" or not zaman_var(reset, son_dakika):
                log_yaz("reset is near (%s): the fallback session is not started" % yerel_zaman(reset))
                kayit["neden"], kayit["cikis"] = "reset-yakin", 0
            else:
                ad = (deneme or {}).get("tmux_ad") or pencere_adi()
                _yaz(brief, deneme["istem"] if deneme else istem_kur(kota, baslangic, jurnal, sec, "oturum", ad, kip))
                h = (SekmeOturum if os.name == "nt" else TmuxOturum)(ad, kok_dizin())
                if os.name != "nt" and not (os.environ.get("SON_PENCERE_TMUX") or shutil.which("tmux")):
                    raise OSError("tmux is not installed (needed for the visible fallback session)")
                basladi = True                    # from here on the session may exist: the finally below stops it
                h.baslat(oturum_komutu(ad, h.sid, brief), env)
                h.ofset = 0
                if h.kayit_bekle():
                    log_yaz("started session '%s' (%s)" % (ad, h.mod))
                else:                             # pane logged and session killed by kayit_bekle; counts as a quick failure
                    kayit["oturum"], kayit["mod"] = h.ad, h.mod
                    kayit["neden"], kayit["cikis"], kayit["hizli"], kayit["ayrinti"] = "hata", -1, True, h.neden
                    h = None
        if h is None:
            if kayit["neden"] is None:
                kayit["neden"], kayit["cikis"] = "teslim-yok", 1
        else:
            kayit["oturum"], kayit["mod"] = h.ad, h.mod
            kilit_guncelle(oturum=h.ad, mod=h.mod, sid=h.sid)
            durum["atama"] = {"oturum": h.ad, "sid": h.sid, "mod": h.mod, "t": int(baslangic)}
            json_yaz(DURUM, durum)
            _izle(h, kayit, durum, kota, isaret, reset, baslangic)
            kayit["cikis"] = 0
    except Durduruldu:
        log_yaz("stopped by a signal during the run")
        kayit["neden"], kayit["cikis"] = "durduruldu", 0
    except Exception as e:
        log_yaz("run failed: %s: %s" % (type(e).__name__, e))
        kayit["neden"] = "hata" if basladi or (h is not None and h.mod == "atama") else "baslamadi"
        kayit["cikis"] = -1
    finally:
        if h is not None and h.mod != "atama":    # our own session never outlives the wrapper (stopping it twice is harmless)
            _sinyalsiz(h.durdur)
    bitis = ZAMAN()
    kayit["bit"] = bitis
    _, durum_son, kota_son = simdi_kota(bitis)
    kayit["hafta_son"] = kota_son["week_used"] if kota_son["week_used"] is not None else kota.get("week_used")
    ogeler = jurnal_ogeleri(jurnal, ofset)
    kayit["biten"] = [(r, t) for k, r, t in ogeler if k == "DONE" and r]
    kayit["devam"] = [(r, t) for k, r, t in ogeler if k == "WIP" and r]
    jurnal_ekle(jurnal, "\n_Run ended %s: %s, session %s, %d turns, weekly use %s, %d done, %d wip_\n" % (
        yerel_zaman(bitis, True), str(kayit["neden"]) + (" (%s)" % kayit["ayrinti"] if kayit.get("ayrinti") else ""), kayit["oturum"] or "-", kayit["tur"],
        ("%g%%" % kayit["hafta_son"]) if kayit["hafta_son"] is not None else "?", len(kayit["biten"]), len(kayit["devam"])))
    return kayit


def _kisi():
    import maliyet_paylas as MP
    return MP.kisi_adi() or "the user"


def _yaz(yol, metin):
    with open(yol, "w", encoding="utf-8") as f:
        f.write(metin)


def _boyut(yol):
    try:
        return os.path.getsize(yol)
    except OSError:
        return 0


def reset_bildir(h, isaret, reset, sebep=None):
    """Tell an assigned session that the reset came, and make sure it got it: relay again while the relay fails, or while the session sits
    idle without the message in its transcript (an idle session shows a delivered message at once). A busy session that the relay says
    has it is not sent a second copy: it shows it at the end of its turn. Bounded: SON_PENCERE_RESET_DENEME relays (3), about
    (tries + 1) x SON_PENCERE_STOP_SN (90 s) in all. Returns 'gorundu' (in the transcript), 'teslim' (relayed, not seen yet) or 'yok'."""
    damga = isaret + "-RESET"
    metin = ("[son-pencere on %s] %s (%s, %s): start nothing new, push your WIP and write the final NOTE in the journal. "
             "Do not reply to this message (the sender is a one-shot relay)." % (makine_adi(), sebep or "Reset reached", yerel_zaman(reset), damga))
    deneme = int(os.environ.get("SON_PENCERE_RESET_DENEME") or 3)
    bekle = float(os.environ.get("SON_PENCERE_STOP_SN") or 90)
    yol, ofset = h.yol, _boyut(h.yol)
    gonderim, teslim_t, son = 0, None, None
    bitis = ZAMAN() + bekle * (deneme + 1)
    while True:
        if h.yol != yol:                          # the session moved to a new transcript (/clear)
            yol, ofset = h.yol, 0
        satirlar, ofset = transkript_oku(yol, ofset)
        if any(damga in s for s in satirlar):
            log_yaz("reset message reached '%s' (relay %d of %d)" % (h.ad, gonderim, deneme))
            return "gorundu"
        if not h.canli():
            log_yaz("'%s' closed before the reset message was confirmed" % h.ad)
            break
        simdi = ZAMAN()
        if teslim_t is None or (simdi - teslim_t >= bekle and h.bosta()):
            if gonderim >= deneme:
                break
            gonderim += 1
            son = h.mesaj(metin, zaman_asimi=min(120.0, float(os.environ.get("SON_PENCERE_ROLE_SN") or 120)))
            if son in ("teslim", "belirsiz"):
                teslim_t = ZAMAN()
            else:
                log_yaz("reset message to '%s' not sent (%s), relay %d of %d" % (h.ad, son, gonderim, deneme))
        elif simdi >= bitis:
            break
        time.sleep(min(POLL, 5))
    log_yaz("reset message to '%s' NOT confirmed in its transcript after %d relay(s) (last answer: %s)" % (h.ad, gonderim, son))
    return "teslim" if teslim_t else "yok"


def _izle(h, kayit, durum, kota, isaret, reset, baslangic):
    """Follow the session until it ends: the reset (stop message / `/exit`), the weekly or 5-hour limit, the session closing, the turn cap, or
    the session idling for IDLE_SN after it has worked (it finished; the next tick may assign again). Sets kayit['neden'] and kayit['tur'].
    An assigned session counts turns only from the assignment marker on (its earlier work is not this run's). Until the marker shows up
    the run waits with that session; it gives up (`teslim-gorulmedi`, nobody else gets the assignment) only when the session sat idle for
    ACK_SN without it, which means the relay's TESLIM was wrong: an idle session shows a delivered message at once."""
    maks_tur = int(os.environ.get("SON_PENCERE_MAX_TURNS") or 200)
    bosta_sn = float(os.environ.get("SON_PENCERE_IDLE_SN") or 600)
    ack_sn = float(os.environ.get("SON_PENCERE_ACK_SN") or 300)
    yol, ofset = h.yol, getattr(h, "ofset", 0)
    gorundu = h.mod != "atama"
    teslim_t = getattr(h, "teslim_t", None) or ZAMAN()
    uyarildi, bekle_bosta_t = False, None
    mesajlar, tur_t, bosta_t, limit_t = set(), None, None, None
    while True:
        if h.yol != yol:                          # the session moved to a new transcript (/clear): follow it there
            log_yaz("'%s' moved to a new transcript (%s): following it" % (h.ad, h.sid))
            yol, ofset = h.yol, 0
        satirlar, ofset = transkript_oku(yol, ofset)
        for s in satirlar:
            if not gorundu:
                if isaret in s:
                    gorundu = True
                    log_yaz("'%s' shows the assignment, %d s after the relay" % (h.ad, ZAMAN() - teslim_t))
                continue
            try:
                e = json.loads(s)
            except ValueError:
                continue
            m = e.get("message") if isinstance(e, dict) and e.get("type") == "assistant" else None
            if isinstance(m, dict):
                mesajlar.add(m.get("id") or s[:80])
        kayit["tur"] = len(mesajlar)
        simdi = ZAMAN()
        if simdi >= reset:
            kayit["neden"] = "reset"
            if h.mod == "atama":
                kayit["reset_bildirim"] = reset_bildir(h, isaret, reset)
            h.durdur()
            return
        if kayit.get("kip") == "bitir" and not bitir_acik(simdi):     # a developer switched burn mode off (one small file read per poll)
            kayit["neden"] = "bitir-kapandi"
            log_yaz("burn mode was switched off: ending the run")
            if h.mod == "atama":
                kayit["reset_bildirim"] = reset_bildir(h, isaret, simdi, "Burn mode (token bitirme modu) was switched off")
            h.durdur()
            return
        if not h.canli():
            if h.mod == "atama":
                kayit["neden"] = "oturum-kapandi"
            else:   # our own session ended by itself: a crash right after the start is a quick failure, otherwise it simply finished
                kayit["neden"] = "hata" if simdi - baslangic < HIZLI_HATA_SN and kayit["tur"] == 0 else "cikti"
            return
        pace, d2, k2 = simdi_kota(simdi)
        if (k2.get("week_used") or 0) >= 100:
            limit_t = limit_t or simdi
            if simdi - limit_t > 60:
                kayit["neden"] = "limit"
                h.durdur()
                return
        elif (k2.get("five_hour_used") or 0) >= 100 and k2.get("five_hour_resets_at"):
            kayit["neden"] = "bes-saat"
            h.durdur()
            return
        if not gorundu:
            if simdi - teslim_t >= ack_sn and not uyarildi:
                uyarildi = True
                log_yaz("'%s' has not shown the assignment %d s after the relay's TESLIM; staying with it (a busy session gets it at the end "
                        "of its turn)" % (h.ad, simdi - teslim_t))
            bekle_bosta_t = (bekle_bosta_t or simdi) if h.bosta() else None
            if bekle_bosta_t is not None and simdi - bekle_bosta_t >= ack_sn and simdi - teslim_t >= ack_sn:
                kayit["neden"] = "teslim-gorulmedi"
                log_yaz("'%s' sat idle for %d s without the assignment: the relay's TESLIM did not arrive; no other session gets it this run" % (
                    h.ad, simdi - bekle_bosta_t))
                return
            time.sleep(POLL)
            continue
        if tur_t is None and len(mesajlar) >= maks_tur:
            tur_t = simdi
            h.mesaj("[son-pencere on %s] Turn cap reached (%d): stop at a clean point, write the hand-off NOTE (done / remaining / next) and %s." % (
                makine_adi(), maks_tur, BITIS_ATAMA if h.mod == "atama" else BITIS_OTURUM))
        if tur_t is not None and (h.bosta() or simdi - tur_t > 600):
            kayit["neden"] = "tur-siniri"
            h.durdur()
            return
        if kayit["tur"] > 0 and h.bosta():
            bosta_t = bosta_t or simdi
            if simdi - bosta_t >= bosta_sn:
                kayit["neden"] = "cikti"
                h.durdur()
                return
        else:
            bosta_t = None
        time.sleep(POLL)


def eski_kosulari_sil():
    siniri = ZAMAN() - 14 * 86400
    kd = os.path.join(DIZIN, "runs")
    try:
        for ad in os.listdir(kd):
            yol = os.path.join(kd, ad)
            if os.path.getmtime(yol) < siniri:
                os.remove(yol)
    except OSError:
        pass


# ------------------------------------------------------------------ shared log (same clone/ownership rules as maliyet_paylas)
BASLIK = ["Start", "End", "Week %", "Reason", "Done", "WIP", "API $", "Items"]
TABLO_BASLIK = "| " + " | ".join(BASLIK) + " |\n|" + "---|" * len(BASLIK) + "\n"


def klon_yolu():
    return os.environ.get("CLAUDE_SHARED_SON_PENCERE_KLON") or os.path.join(HOME, ".cache", "claude-shared-son-pencere")


def hucre(s):
    return " ".join(str(s).replace("|", "/").split())


def satir_kur(k):
    """One markdown row from a run record; the Start cell is the row's key."""
    def oge(r):
        m = URL_RE.match(r)
        return "[%s](%s)" % (kisa_ref(r), r) if m else None
    bagli = [oge(r) for r, _ in k.get("biten", [])]
    bagli = [b for b in bagli if b]
    gosterilen = bagli[:8] + (["+%d more" % (len(bagli) - 8)] if len(bagli) > 8 else [])
    hafta = "%s→%s" % (("%g" % k["hafta_bas"]) if k.get("hafta_bas") is not None else "?",
                      ("%g" % k["hafta_son"]) if k.get("hafta_son") is not None else "?")
    maliyet = ("%.0f" % k["maliyet"]) if k.get("maliyet") else "—"
    if k.get("oturum"):   # which session took the run (no extra column: older rows and readers keep their 8-cell shape)
        gosterilen = ["session:%s" % k["oturum"]] + gosterilen
    neden = k.get("neden") or "running"
    if k.get("kip") in ("bitir", "devam") and not neden.startswith(k["kip"] + "/"):   # the mode in the Reason cell (no extra column)
        neden = "%s/%s" % (k["kip"], neden)
    hucreler = [yerel_zaman(k["bas"], True), yerel_zaman(k["bit"]) if k.get("bit") else "running", hafta, neden,
                str(len(k.get("biten", []))), str(len(k.get("devam", []))), maliyet, " ".join(gosterilen) or "—"]
    return "| " + " | ".join(hucre(h) for h in hucreler) + " |"


def satir_ayristir(satir):
    p = [c.strip() for c in satir.strip().strip("|").split("|")]
    if len(p) != len(BASLIK) or not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", p[0]):
        return None
    return dict(zip(BASLIK, p))


def dosya_satirlari(metin):
    return [r for r in (satir_ayristir(s) for s in (metin or "").splitlines()) if r]


def kisi_dosyasi(kisi, metin, kayit):
    """The person's file with this run's row added, or replaced when a row with the same Start exists (a run is written when it
    starts and again when it ends). Rows beyond SATIR_SINIRI are dropped from the top."""
    yeni = satir_kur(kayit)
    baslangic = yerel_zaman(kayit["bas"], True)
    satirlar = (metin or "").splitlines()
    govde = [s for s in satirlar if satir_ayristir(s)]
    govde = [yeni if s.strip("| ").startswith(baslangic) else s for s in govde] if any(s.strip("| ").startswith(baslangic) for s in govde) else govde + [yeni]
    govde = govde[-SATIR_SINIRI:]
    baslik = ("# Son pencere runs — %s\n\nOne row per \"son pencere\" run (the last hours before the weekly quota reset, when leftover quota is used on "
              "pre-approved work). Written by `son-pencere tetik`; do not edit. `Week %%` = weekly limit used at start→end. `API $` = API list-price "
              "equivalent of that run, not a bill. All people: [KAYITLAR.md](KAYITLAR.md).\n\n" % kisi)
    return baslik + TABLO_BASLIK + "\n".join(govde) + "\n"


def kayitlar_metni(kisi_satirlari, sinir=60):
    tum = []
    for kisi, satirlar in kisi_satirlari.items():
        tum += [(r["Start"], kisi, r) for r in satirlar]
    tum.sort(reverse=True)
    baslik = ["Person"] + BASLIK
    L = ["# Son pencere records (all people)", "",
         "Latest runs of the \"son pencere\" mode (last hours before each person's weekly quota reset), newest first. Generated from "
         "`raporlar/son-pencere/<person>.md`; read it with `son-pencere durum`. Installation and rules: README of plugins/kota-temposu and "
         "rules/WORKING-RULES.md (Agents and quota).", "",
         "| " + " | ".join(baslik) + " |", "|" + "---|" * len(baslik)]
    for _, kisi, r in tum[:sinir]:
        L.append("| " + " | ".join([hucre(kisi)] + [r[b] for b in BASLIK]) + " |")
    if not tum:
        L.append("| (no runs yet) |" + " |" * (len(baslik) - 1))
    return "\n".join(L) + "\n"


def paylasim_oku(klon, adlar):
    import maliyet_paylas as MP
    out = {}
    for ad in sorted(set(adlar)):
        try:
            with open(os.path.join(klon, *REPO_DIZIN.split("/"), MP.dosya_adi(ad) + ".md"), encoding="utf-8") as f:
                out[ad] = dosya_satirlari(f.read())
        except OSError:
            pass
    return out


def bir_deneme(klon, uzak, kisi, kayit):
    import maliyet_paylas as MP
    MP.klon_hazirla(klon, uzak)
    harita = MP.kisiler_oku(klon)
    if kisi not in harita.values():
        raise RuntimeError("'%s' is not in kisiler.json: nothing written" % kisi)
    yol = os.path.join(klon, *REPO_DIZIN.split("/"), MP.dosya_adi(kisi) + ".md")
    try:
        with open(yol, encoding="utf-8") as f:
            mevcut = f.read()
    except OSError:
        mevcut = ""
    MP.yaz(yol, kisi_dosyasi(kisi, mevcut, kayit))
    MP.yaz(os.path.join(klon, *REPO_DIZIN.split("/"), KAYITLAR), kayitlar_metni(paylasim_oku(klon, harita.values())))
    MP.git(klon, "add", "--", REPO_DIZIN)
    rc, _ = MP.git(klon, "diff", "--cached", "--quiet")
    if rc == 0:
        return "no change"
    mesaj = "chore(son-pencere): %s %s" % (kisi, yerel_zaman(kayit["bas"]))
    rc, out = MP.git(klon, "commit", "--quiet", "-m", mesaj)
    if rc != 0 and "identity" in out.lower():
        rc, out = MP.git(klon, "-c", "user.name=" + kisi, "-c", "user.email=kullanim@claude-shared.invalid", "commit", "--quiet", "-m", mesaj)
    if rc != 0:
        raise RuntimeError("commit: " + out.replace("\n", " ")[:200])
    rc, out = MP.git(klon, "push", "--quiet", "origin", "HEAD:main")
    if rc != 0:
        raise RuntimeError("push: " + out.replace("\n", " ")[:200])
    return "pushed"


def paylas(kayit):
    """Write/replace this run's row in the shared log. Never raises; True when pushed (or already there)."""
    import maliyet_paylas as MP
    kisi = MP.kisi_adi()
    if not kisi:
        log_yaz("shared log skipped: no person name (CLAUDE_SHARED_KISI, ~/.config/claude-shared/kisi, git user.name)")
        return False
    hata = None
    for _ in range(2):
        try:
            durum = bir_deneme(klon_yolu(), MP.uzak_adres(), kisi, kayit)
            log_yaz("shared log: %s (%s)" % (durum, satir_kur(kayit)))
            return True
        except Exception as e:
            hata = str(e)
            if "kisiler.json" in hata:
                break
            time.sleep(1 + (os.getpid() % 3))
    log_yaz("shared log FAILED: %s" % hata)
    return False


# ------------------------------------------------------------------ commands
def simdi_kota(simdi):
    pace, durum = json_oku(os.path.join(CLAUDE, "pace.json")), json_oku(DURUM)
    return pace, durum, kota_durumu(pace, durum, simdi)


def kipler_ayristir(v):
    """--kipler pencere,devam (the timer's argument, written by the installer); burn mode needs no entry (it is switched on by hand)."""
    k = tuple(dict.fromkeys(x.strip() for x in (v or "").split(",") if x.strip()))
    yanlis = [x for x in k if x not in ("pencere", "devam")]
    if yanlis or not k:
        raise argparse.ArgumentTypeError("unknown mode(s) %s: use pencere and/or devam" % ",".join(yanlis or ["(empty)"]))
    return k


def tetik_durumu(simdi, kipler, yaz=True):
    """Everything this tick's gate needs, read once: pace/durum/quota, burn mode (auto-off written unless `yaz` is False), the resume
    check (pace.json first, else the transcripts of the idle live sessions) and the gate result of every mode."""
    pace, durum, kota = simdi_kota(simdi)
    if yaz:
        bitir_suresi_doldu(simdi)
    bt = bitir_oku()
    acik = bitir_acik(simdi, bt)
    canli = kilit_canli(simdi)
    dvs = devam_oku()
    dv = devam_durumu(pace, dvs, simdi) if ("devam" in kipler or acik) else None
    duranlar = None
    if dv and dv["hazir"] is None:
        duranlar = limitte_duranlar(oturumlari_oku(model=False), dv["r5"], simdi, dvs.get("uyarilan") or [])
        dv["hazir"] = bool(duranlar)
        dv["neden"] = "devam" if duranlar else "devam-yok"
        dv["ayrinti"] = ("%d live session(s) still stopped by a 5-hour limit that has reset" % len(duranlar)) if duranlar else \
            "no live session is still stopped by a 5-hour limit that has reset"
    secilen, sonuclar = kip_sec(simdi, kota, hedef_saat(pace), canli, sayi(durum.get("son_baslangic")), int(durum.get("hizli_hata") or 0),
                                kipler, acik, bool(dv and dv["hazir"]), sayi(durum.get("bos_bekle")))
    return {"pace": pace, "durum": durum, "kota": kota, "bitir": bt, "bitir_acik": acik, "devam": dv, "devam_durum": dvs, "duranlar": duranlar,
            "secilen": secilen, "sonuclar": sonuclar}


def tetik_yazdir(t, kipler):
    s, sonuclar = t["secilen"], t["sonuclar"]
    ilk = s or (sonuclar[0] if sonuclar else None)
    if ilk:
        print("son-pencere: %s (%s): %s%s" % (ilk[1], ilk[2], ilk[3], "" if ilk[0] == "pencere" else "  [mode %s]" % ilk[0]))
    else:
        print("son-pencere: bekle (kip-yok): no installed mode is due now")
    for r in sonuclar:
        if r is not ilk:
            print("  mode %s: %s (%s): %s" % r)
    if "pencere" not in kipler:
        print("  mode pencere: not installed (kur --son-pencere)")
    bt = t["bitir"]
    if t["bitir_acik"]:
        print("  burn mode: ON since %s by %s, until the weekly reset %s" % (
            yerel_zaman(sayi(bt.get("acilis")) or 0), bt.get("acan") or "?", yerel_zaman(sayi(bt.get("hafta_reset")) or 0)))
    else:
        print("  burn mode: off")
    dv = t["devam"]
    if dv is None:
        print("  resume alarm: not installed (kur --devam-alarmi)")
    elif not dv["hazir"]:
        print("  resume alarm: %s: %s" % (dv["neden"], dv["ayrinti"]))
    kota = t["kota"]
    if kota.get("week_used") is not None:
        print("  weekly use %g%% (source %s), reset %s" % (kota["week_used"], kota["kaynak"], yerel_zaman(kota["week_resets_at"])))


def devam_kaydet(simdi, kota, gonderilen):
    """The resume alarm's record: a journal block (every relay and its answer) and, when at least one relay went out (or may have), one
    shared-log row (Reason devam/uyandirma, Items session:<names>). Returns the row's Start (a run of the same tick starts after it:
    rows are keyed by their Start second)."""
    jurnal_ekle(jurnal_yolu(simdi), "\n## Resume alarm %s (5-hour reset)\n\n%s\n" % (yerel_zaman(simdi, True), "\n".join(
        "- [%s] NOTE resume relay to %s: %s" % (time.strftime("%H:%M", time.localtime(simdi)), ad, cevap) for ad, cevap in gonderilen)))
    giden = [ad for ad, cevap in gonderilen if cevap in ("teslim", "belirsiz")]
    if giden:
        paylas({"bas": simdi, "bit": ZAMAN(), "hafta_bas": kota.get("week_used"), "hafta_son": kota.get("week_used"), "neden": "uyandirma",
                "kip": "devam", "biten": [], "devam": [], "maliyet": None, "oturum": ",".join(giden)})
        return simdi
    return None


def devam_calistir(t, simdi, kip):
    """The resume alarm's part of a tick, under its own lock (independent of a coordinator run): record the reset key, relay to every
    session the limit stopped (minus the session of the last assignment when a coordinator run starts now: it gets the fresh assignment),
    record. Returns ([(name, answer)], Start of the shared-log row or None)."""
    dv, dvs = t["devam"], t["devam_durum"]
    kilit = devam_kilit_al(simdi)
    if not kilit:
        log_yaz("resume alarm: another tick is relaying (devam.kilit)")
        return [], None
    try:
        if dv["anahtar"]:
            dvs["anahtar"] = dv["anahtar"]                 # once per reset, recorded before any relay
            json_yaz(devam_yolu(), dvs)
        duranlar = t["duranlar"] if t["duranlar"] is not None else limitte_duranlar(
            oturumlari_oku(model=False), dv["r5"], simdi, dvs.get("uyarilan") or [])
        if kip in ("pencere", "bitir"):
            duranlar = [o for o in duranlar if o["sid"] != (t["durum"].get("atama") or {}).get("sid")]
        gonderilen = devam_uyandir(duranlar, dvs)
        if not gonderilen:
            log_yaz("resume alarm: no live session is stopped by the limit (%s)" % dv["ayrinti"])
            return [], None
        return gonderilen, devam_kaydet(simdi, t["kota"], gonderilen)
    finally:
        try:
            os.remove(kilit)
        except OSError:
            pass


def cmd_tetik(a):
    simdi = ZAMAN()
    kipler = a.kipler or ("pencere",)
    t = tetik_durumu(simdi, kipler, yaz=not a.dry_run)
    tetik_yazdir(t, kipler)
    durum, kota, dv = t["durum"], t["kota"], t["devam"]
    secilen = t["secilen"]
    eylem = "baslat" if secilen and secilen[0] in ("pencere", "bitir") else "bekle"
    if a.dry_run:
        if dv is not None:
            duranlar = limitte_duranlar(oturumlari_oku(model=False), dv["r5"], simdi, t["devam_durum"].get("uyarilan") or [], hepsi=True)
            print("Sessions stopped by the 5-hour limit (each gets the resume relay once, %d min after its reset):" % (DEVAM_GECIKME // 60))
            print("\n".join("  %-24s stopped %s, reset %s" % (o["ad"][:24], yerel_zaman(o["durus"]["t"]), yerel_zaman(o["reset"])) for o in duranlar) or "  (none)")
        if "pencere" not in kipler and not t["bitir_acik"]:
            print("DRY RUN: nothing sent, nothing written.")
            return 0
        s = secimler()
        yazdir_secim(s)
        aday, satirlar = oturum_tablosu(oturumlari_oku(), haric_desenleri())
        print("Live sessions (best first; the first one gets the assignment):")
        print("\n".join(satirlar) or "  (none)")
        if aday:
            print("DRY RUN: would %s the assignment to '%s' (relay: a one-shot headless Haiku with SendMessage), brief from %s. Nothing sent, nothing written." % (
                "send" if eylem == "baslat" else "NOT send now, otherwise send", aday[0]["ad"], ISTEM))
        else:
            print("DRY RUN: no live session can take it; would %s the visible session '%s' (cwd %s). Nothing started, nothing written." % (
                "start" if eylem == "baslat" else "NOT start now, otherwise start", pencere_adi(), kok_dizin()))
        return 0
    if not secilen:
        for r in t["sonuclar"]:
            log_yaz("%s: %s: %s" % (r[0], r[2], r[3]))
        return 0
    kip = secilen[0]
    gonderilen, devam_bas = [], None
    if dv and dv["hazir"]:                # the resume alarm first: the sessions the limit stopped get their own work back
        _isleyicileri_kur()
        gonderilen, devam_bas = devam_calistir(t, simdi, kip)
    if eylem != "baslat":
        return 0
    if not kilit_al(simdi):
        log_yaz("calisiyor: lock taken between the check and the start")
        return 0
    _isleyicileri_kur()
    try:
        bas = ZAMAN()
        if devam_bas is not None and int(bas) <= int(devam_bas):    # own Start second: rows are keyed by it
            bas = int(devam_bas) + 1
        sec = secimler()
        sec["araclar"] = araclar_dizini()
        for h in sec["hatalar"]:
            log_yaz("pick: " + h)
        durum["son_baslangic"] = int(bas)
        json_yaz(DURUM, durum)
        taslak = {"bas": bas, "hafta_bas": kota.get("week_used"), "neden": None, "biten": [], "devam": [], "maliyet": None, "kip": kip}
        paylas(taslak)                      # visible to the team as "running" from the start
        kayit = calistir(kota, durum, sec, bas, kip=kip, atla={ad for ad, c in gonderilen if c in ("teslim", "belirsiz")})
        hizli = kayit.get("hizli") or (kayit["bit"] - kayit["bas"] < HIZLI_HATA_SN and kayit["neden"] in ("hata", "baslamadi"))
        durum["hizli_hata"] = int(durum.get("hizli_hata") or 0) + 1 if hizli else 0
        bos = not kayit["biten"] and not kayit["devam"] and kayit["neden"] in ("cikti", "teslim-yok", "teslim-gorulmedi", "teslim-belirsiz", "oturum-kapandi")
        durum["bos_bekle"] = int(ZAMAN() + BITIR_BOS_BEKLEME) if kip == "bitir" and bos else None
        json_yaz(DURUM, durum)
        paylas(kayit)
        log_yaz("run ended (%s): %s, exit %s, done %d, wip %d" % (kip, kayit["neden"], kayit["cikis"], len(kayit["biten"]), len(kayit["devam"])))
        eski_kosulari_sil()
    finally:
        kilit_birak()
    return 0


def kurulum_kaydi():
    """The install flags saved by the installer (~/.claude/kota-temposu.json "kurulum"), or None (a copy install keeps no record)."""
    k = json_oku(os.path.join(CLAUDE, "kota-temposu.json")).get("kurulum")
    return k if isinstance(k, list) else None


def bitir_kurulu():
    """True when the son-pencere timer is installed (--son-pencere or --devam-alarmi saved), None when unknown."""
    k = kurulum_kaydi()
    return None if k is None else ("--son-pencere" in k or "--devam-alarmi" in k)


def kurulu_kipler():
    """The modes the timer runs, as the installer passed them (`son-pencere durum` shows the same gate the timer sees)."""
    k = kurulum_kaydi() or ["--son-pencere"]
    return tuple(m for m, f in (("pencere", "--son-pencere"), ("devam", "--devam-alarmi")) if f in k) or ("pencere",)


def cmd_bitir(a):
    """son-pencere bitir --ac | --kapat | durum. User-facing text is Turkish."""
    simdi = ZAMAN()
    if a.islem == "durum":
        bitir_suresi_doldu(simdi)
        b = bitir_oku()
        if bitir_acik(simdi, b):
            print("Token bitirme modu AÇIK: %s açtı (%s). Haftalık sıfırlanmada (%s) kendiliğinden kapanır; kapatmak için: son-pencere bitir --kapat" % (
                b.get("acan"), yerel_zaman(sayi(b.get("acilis")) or 0), yerel_zaman(sayi(b.get("hafta_reset")) or 0)))
        elif b:
            print("Token bitirme modu KAPALI (son kapanış: %s, %s)." % (b.get("kapatan") or "?", yerel_zaman(sayi(b.get("kapanis")) or 0)))
        else:
            print("Token bitirme modu KAPALI (hiç açılmadı).")
        return 0
    # Accident guards, not a security boundary (the login override and kisiler.json are local): a coordinator never switches the mode
    # itself. A session son-pencere started carries SON_PENCERE=1; an assigned session of the owner runs while the coordinator lock is live.
    if os.environ.get("SON_PENCERE") == "1" or (a.islem == "ac" and kilit_canli(simdi)):
        print("Reddedildi: token bitirme modu bir son-pencere koşusu sürerken ya da son-pencere oturumlarından açılamaz; bir yazılımcı "
              "kendi terminalinden, koşu yokken açar (kapatmak koşu sürerken de olur: kendi terminalinizden son-pencere bitir --kapat).")
        return 3
    login = gh_login()
    ad, rol = kisi_rolu(login)
    if not login:
        print("Reddedildi: GitHub girişi okunamadı (gh auth login). Rol doğrulanamadan token bitirme modu değiştirilemez.")
        return 3
    if rol != "yazilimci":
        print("Reddedildi: token bitirme modunu yalnız yazılımcılar (kisiler.json'da rol: yazilimci) açıp kapatabilir. "
              "GitHub girişi '%s' %s." % (login, "kisiler.json'da yok" if not ad else "rolü: %s" % rol))
        log_yaz("burn mode %s refused for %s (role %s)" % (a.islem, login, rol))
        return 3
    b = bitir_oku()
    if a.islem == "kapat":
        if not bitir_acik(simdi, b):
            print("Token bitirme modu zaten kapalı.")
            return 0
        b.update(aktif=False, kapatan=ad, kapatan_login=login, kapanis=int(simdi))
        json_yaz(bitir_yolu(), b)
        log_yaz("burn mode switched OFF by %s (%s)" % (ad, login))
        print("Token bitirme modu KAPATILDI (%s, %s). Çalışan bir koşu varsa bir sonraki kontrolde durur." % (ad, yerel_zaman(simdi)))
        return 0
    if bitir_acik(simdi, b):
        print("Token bitirme modu zaten açık: %s açtı (%s), haftalık sıfırlanmaya (%s) kadar." % (
            b.get("acan"), yerel_zaman(sayi(b.get("acilis")) or 0), yerel_zaman(sayi(b.get("hafta_reset")) or 0)))
        return 0
    _, _, kota = simdi_kota(simdi)
    reset = kota.get("week_resets_at")
    if not reset:
        print("Reddedildi: haftalık kota sıfırlanma zamanı bilinmiyor (pace.json yok). Önce bir Claude Code oturumu açın; durum çubuğu pace.json'ı yazar.")
        return 2
    if kota.get("week_used") is not None and kota["week_used"] >= 100:
        print("Reddedildi: haftalık kota zaten bitmiş (%%%g); sıfırlanma %s." % (kota["week_used"], yerel_zaman(reset)))
        return 2
    json_yaz(bitir_yolu(), {"aktif": True, "acan": ad, "acan_login": login, "acilis": int(simdi), "hafta_reset": int(reset),
                            "hafta_reset_kaynak": kota.get("kaynak"), "makine": makine_adi()})
    log_yaz("burn mode switched ON by %s (%s) until %s" % (ad, login, yerel_zaman(reset)))
    print("Token bitirme modu AÇIK: %s, %s. Açık izinli işler (son pencere sırası ve yasak listesi) haftalık kota bitene ya da haftalık "
          "sıfırlanmaya (%s%s) kadar işlenir; her 5 saatlik sıfırlanmadan sonra kaldığı yerden sürer. Kapatmak için: son-pencere bitir --kapat" % (
              ad, yerel_zaman(simdi), yerel_zaman(reset), ", tahmini" if kota.get("kaynak") == "tahmin" else ""))
    if bitir_kurulu() is False:
        print("UYARI: son-pencere zamanlayıcısı bu makinede kurulu görünmüyor; mod ancak zamanlayıcıyla çalışır. Kurmak için: "
              "pwsh -File plugins/kota-temposu/kur.ps1 -DevamAlarmi  (Linux: bash plugins/kota-temposu/kur.sh --devam-alarmi)")
    return 0


def yazdir_secim(s):
    a, b, c = s["a"], s["b"], s["c"]
    print("Picks for %s (re-picked at every start):" % (s["login"] or "?"))
    print("  (a) label %s: %d open issue(s)" % (ETIKET, len(a)))
    for i in a[:15]:
        print("      " + oge_satiri(i))
    print("  (b) board 'Yapay Zeka', not %s, unassigned or mine: %d issue(s), highest priority first" % (ONAY_ETIKET, len(b)))
    for i in b[:15]:
        print("      " + oge_satiri(i))
    if len(b) > 15:
        print("      ... %d more" % (len(b) - 15))
    print("  (c) standing list signals: %d repo(s) with red CI on dev, Dependabot PRs in %d repo(s)" % (len(c["kirmizi_ci"]), len(c["dependabot"])))
    for k in c["kirmizi_ci"]:
        print("      red CI on dev: %s (%s) %s" % (k["repo"], k["workflow"], k["url"]))
    for d in c["dependabot"]:
        print("      %d Dependabot PR(s): %s" % (d["acik"], d["repo"]))
    for h in s["hatalar"]:
        print("  [source error] " + h)


def cmd_secim(a):
    yazdir_secim(secimler())
    return 0


def cmd_etiket(a):
    sonuc = etiket_olustur(a.kuru)
    for repo, d in sonuc:
        print("%-22s %s" % (repo, d))
    return 1 if any(d.startswith("ERROR") for _, d in sonuc) else 0


def cmd_durum(a):
    import maliyet_paylas as MP
    klon = klon_yolu()
    notlar = []
    if not a.yerel:
        try:
            MP.klon_hazirla(klon, MP.uzak_adres())
        except RuntimeError as e:
            notlar.append("could not refresh the shared log (%s)" % e)
    harita = MP.kisiler_oku(klon) if os.path.isdir(os.path.join(klon, ".git")) else {}
    veri = paylasim_oku(klon, harita.values()) if harita else {}
    tum = sorted(((r["Start"], k, r) for k, rs in veri.items() for r in rs), reverse=True)[:a.son]
    if tum:
        print("%-18s %-17s %-17s %-8s %-9s %-4s %-3s %s" % ("Person", "Start", "End", "Week %", "Reason", "Done", "WIP", "Items"))
        for _, kisi, r in tum:
            print("%-18s %-17s %-17s %-8s %-9s %-4s %-3s %s" % (kisi[:18], r["Start"][:16], r["End"], r["Week %"], r["Reason"], r["Done"], r["WIP"],
                                                                 re.sub(r"\]\([^)]*\)", "]", r["Items"]).replace("[", "")))
    else:
        print("No son-pencere runs recorded in the shared log yet.")
    for n in notlar:
        print("note: " + n)
    simdi = ZAMAN()
    print("This machine:")
    kipler = kurulu_kipler()
    tetik_yazdir(tetik_durumu(simdi, kipler, yaz=False), kipler)
    k = kilit_oku()
    if kilit_canli(simdi) and k.get("oturum"):
        print("  running now in session '%s' (%s), since %s; watch it: %s" % (
            k["oturum"], "assigned live session" if k.get("mod") == "atama" else "started by son-pencere",
            yerel_zaman(sayi(k.get("t")) or simdi), ("tmux -L %s attach -t %s" % (TMUX_SOKET, k["oturum"])) if k.get("mod") == "tmux" else "open that session / `claude --resume`"))
    return 0


def cmd_oturum(a):
    """Print the live sessions, who would get the assignment and why the others are out. Starts and sends nothing."""
    aday, satirlar = oturum_tablosu(oturumlari_oku(), haric_desenleri())
    print("Live sessions on this machine (best first: model score, newer model version, oldest start):")
    print("\n".join(satirlar) or "  (none)")
    print("Assignment goes to: %s" % (aday[0]["ad"] if aday else "nobody (the visible session '%s' would be started)" % pencere_adi()))
    return 0


def cmd_deneme(a):
    """Test of the whole delivery + follow + cutoff path with a trivial prompt, in a scratch state dir (own lock, log and journal; no
    gate, no shared-log row). --gonder NAME: relay to the live session NAME (must start with son-pencere-test); --tmux: start the
    visible fallback session son-pencere-test. The run ends at --reset-sn seconds with the normal reset handling."""
    global DIZIN, KILIT, DURUM
    import tempfile
    if a.gonder and not a.gonder.startswith("son-pencere-test"):
        print("refused: --gonder only targets sessions named son-pencere-test*")
        return 2
    if bool(a.gonder) == bool(a.tmux):
        print("give exactly one of --gonder NAME or --tmux")
        return 2
    DIZIN = tempfile.mkdtemp(prefix="son-pencere-deneme-")
    KILIT, DURUM = os.path.join(DIZIN, "koordinator.kilit"), os.path.join(DIZIN, "durum.json")
    simdi = ZAMAN()
    kota = {"week_used": None, "week_resets_at": simdi + a.reset_sn}
    deneme = {"hedef": a.gonder, "tmux_ad": "son-pencere-test" if a.tmux else None, "istem": a.istem}
    kilit_al(simdi)
    _isleyicileri_kur()
    try:
        kayit = calistir(kota, {}, {}, simdi, deneme)
    finally:
        kilit_birak()
    print("deneme: reason=%s session=%s mode=%s turns=%s state dir %s" % (kayit["neden"], kayit["oturum"], kayit["mod"], kayit["tur"], DIZIN))
    shutil.rmtree(DIZIN, ignore_errors=True)
    return 0 if kayit["neden"] in ("reset", "cikti") else 1


def main(argv=None):
    for akis in (sys.stdout, sys.stderr):   # None under pythonw.exe (the Windows scheduled task): nothing to reconfigure
        if akis is not None and hasattr(akis, "reconfigure"):
            akis.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="son-pencere", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    t = sp.add_parser("tetik", help="timer entry: gate, then run one coordinator")
    t.add_argument("--dry-run", action="store_true", help="print the gate decision and the picks; start nothing, write nothing")
    t.add_argument("--kipler", type=kipler_ayristir, default=None, metavar="pencere,devam",
                   help="modes the timer runs (written by the installer; default pencere). Burn mode runs whenever it is switched on")
    t.set_defaults(f=cmd_tetik)
    b = sp.add_parser("bitir", help="burn mode (token bitirme modu): --ac / --kapat (developers only) or durum")
    g = b.add_mutually_exclusive_group()
    g.add_argument("--ac", dest="islem", action="store_const", const="ac", help="switch burn mode on (rol: yazilimci only)")
    g.add_argument("--kapat", dest="islem", action="store_const", const="kapat", help="switch burn mode off (rol: yazilimci only)")
    b.add_argument("durum", nargs="?", choices=["durum"], help="show the state (the default)")
    b.set_defaults(f=cmd_bitir, islem="durum")
    sp.add_parser("secim", help="print the picked items").set_defaults(f=cmd_secim)
    d = sp.add_parser("durum", help="latest runs of everyone (shared log)")
    d.add_argument("--son", type=int, default=15)
    d.add_argument("--yerel", action="store_true", help="do not refresh the shared log from GitHub")
    d.set_defaults(f=cmd_durum)
    sp.add_parser("oturum", help="print the live sessions and who would get the assignment").set_defaults(f=cmd_oturum)
    x = sp.add_parser("deneme", help="test the delivery / follow / cutoff path with a trivial prompt (scratch state, no real run)")
    x.add_argument("--gonder", metavar="AD", help="relay to the live test session AD (name must start with son-pencere-test)")
    x.add_argument("--tmux", action="store_true", help="start the visible fallback session son-pencere-test")
    x.add_argument("--reset-sn", type=int, default=120, help="seconds until the simulated reset (default 120)")
    x.add_argument("--istem", default="Reply with the single word OK and stop. Do not use any tool.", help="the trivial prompt")
    x.set_defaults(f=cmd_deneme)
    e = sp.add_parser("etiket", help="create the label in the product repos")
    e.add_argument("--kuru", action="store_true")
    e.set_defaults(f=cmd_etiket)
    a = ap.parse_args(argv)
    if a.cmd == "tetik" and not a.dry_run:
        try:
            return a.f(a)
        except Durduruldu:
            log_yaz("stopped by a signal")
            return 0
        except Exception as ex:   # the timer must never see a failure it cannot act on
            log_yaz("ERROR %s: %s" % (type(ex).__name__, ex))
            return 0
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())

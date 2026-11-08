"""Claude Code spending statistics: day / week / month / year (API-price equivalent of token usage, NOT a bill).
Stdlib only, spends no tokens, sends nothing anywhere (everything stays on this machine).

  maliyet                  summary: rolling windows, today, last 7 days, last 4 weeks, months, years (empty sections are hidden)
  maliyet --pencere        rolling windows: last 24 hours / 7 days / 30 days (with per-model breakdown)
  maliyet --gun|--hafta|--ay|--yil [--son N]   one view in detail (incl. the 10 most expensive sessions)
  maliyet --json           all views as one JSON document (ids and numbers only, no prompt text)
  maliyet --kaydet         write daily totals to gunluk.jsonl and hourly totals (last 45 days) to saatlik.jsonl (the timer runs this)
  maliyet --panel [DIR]    write flat JSON tables and a static dashboard index.html (default <config dir>/maliyet/panel)
  maliyet --durum          one status-line string "[son Ndk ₺A (₺a) · ]son 24sa ₺A (₺a) · 7g ₺B $C (₺b) · 30g ₺D (₺d) · ×N" (7g also in dollars; (₺..) = real cost, ×N = plan is N times cheaper than API prices; both only with a calibration) (reads only the cache file, never scans transcripts)
  maliyet --durum --para U1 U2 ...   same, then one line per USD amount U with the amount in lira (the status lines format
                           their amounts here, one place); no lira rate cached yet = dollars
  maliyet --durum --girdi  same, but reads the status-line JSON from stdin and prints 3 lines: the windows string, "oturum"
                           (cost.total_cost_usd) and the cost of the current/last action (per prompt_id, see tur_tutari) as "₺A (₺B)"
  maliyet --durum-yenile   incrementally refresh that cache (--durum starts it in the background when the cache is older than 5 min)

Source: <config dir>/projects/<project>/<session>.jsonl and <session>/subagents/**/agent-*.jsonl (+ .meta.json: agentType only).
The config dir is $CLAUDE_CONFIG_DIR, else ~/.claude. Parsing and the price table live in maliyet_ayristir.py and
maliyet-fiyatlar.json. Advisor calls are read from usage.iterations[] and priced with their own model.
Day boundary: $MALIYET_TZ (IANA name, or "local"); default Europe/Istanbul. Weeks are ISO (Mon-Sun).
Money is shown in lira: computation stays in USD, the displayed value is multiplied by the TCMB rate in <config dir>/maliyet/kur.json
(maliyet_kur.py fetches it in the background refresh only; no rate yet = dollars).
Files: maliyet.py (entry + fast --durum path), maliyet_cekirdek.py (scan, records, reports), maliyet_ayristir.py (parser, prices).
Writes: <config dir>/maliyet/{gunluk.jsonl, saatlik.jsonl, onbellek.json, durum.cache}; all 0600, directory 0700.
No prompt, output, title or description text is written anywhere (project folder names and session ids are).
"""
import json
import os
import sys
import time

# python -I (isolated) does not put the script's directory on sys.path: add it so the sibling modules import.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", "tools"))  # repo layout: tools/gizli.py (a --kopya install ships it next to this file)
from gizli_yukle import gizli  # noqa: E402  (one hidden-launch helper)

HOME = os.path.expanduser("~")
CLAUDE = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
DIZIN = os.path.join(CLAUDE, "maliyet")
DURUM = os.path.join(DIZIN, "durum.cache")
KILIT = os.path.join(DIZIN, "durum.kilit")
BETIK = os.path.abspath(__file__)
TZ_AD = os.environ.get("MALIYET_TZ") or "Europe/Istanbul"
DURUM_TTL = 300    # seconds: a status cache older than this is refreshed in the background
SAATLIK_GUN = 45   # days kept in saatlik.jsonl
PENCERELER = (("son 24sa", 1), ("7g", 7), ("30g", 30))  # rolling windows: (label, days)
API_ETIKET = "API eşdeğeri (fatura değil)"   # closes the cost segment while there is no "gerçek" calibration


# ------------------------------------------------------------------ status line (fast path, no heavy imports)
def _tz():
    if os.name == "nt":
        # no tzset, and the C runtime knows "UTC" but not IANA names: other zones use the system's local zone here
        # (only the completion of a window's start from daily totals depends on it)
        if TZ_AD.upper() == "UTC":
            os.environ["TZ"] = "UTC"
            try:
                import ctypes
                ctypes.CDLL("ucrtbase")._tzset()  # the CRT caches the zone: re-read TZ
            except Exception:
                pass
        return
    if TZ_AD.lower() != "local":
        os.environ["TZ"] = TZ_AD
    try:
        time.tzset()
    except AttributeError:
        pass


def _yenile_baslat():
    """One detached background refresh (`--durum-yenile` child); not restarted when one was started in the last 60 s.
    The child takes an exclusive non-blocking lock itself (_kilit_al: fcntl on POSIX, msvcrt on Windows), so two scans never
    overlap. No external `flock`/`nice` binary is needed (macOS has neither flock nor GNU tools)."""
    try:
        if time.time() - os.stat(KILIT).st_mtime < 60:
            return
    except OSError:
        pass
    if os.name == "nt":
        return _yenile_baslat_windows()
    try:
        pid = os.fork()
    except (OSError, AttributeError):
        return
    if pid:
        return
    try:
        os.setsid()
        dn = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(dn, fd)
        os.makedirs(DIZIN, mode=0o700, exist_ok=True)
        try:
            os.close(os.open(KILIT, os.O_WRONLY | os.O_CREAT, 0o600))
            os.utime(KILIT)
        except OSError:
            pass
        try:
            os.nice(19)
        except (OSError, AttributeError):
            pass
        exe = sys.executable or "python3"
        os.execv(exe, [exe, "-I", BETIK, "--durum-yenile"])
    finally:
        os._exit(0)


def _yenile_baslat_windows():
    """Windows: one detached, windowless, idle-priority `--durum-yenile` child (no fork/flock here). The 60 s stamp stops a
    restart storm; the child takes an exclusive lock itself (_kilit_al) so two scans never overlap."""
    try:
        os.makedirs(DIZIN, exist_ok=True)
        with open(KILIT, "ab"):
            pass
        os.utime(KILIT)
        exe = sys.executable or "python"
        pw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(pw):
            exe = pw
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | IDLE_PRIORITY_CLASS (+ CREATE_NO_WINDOW from gizli)
        import subprocess   # local: the hot path of this module never needs it (a missing import here was swallowed below and no refresh ever started)
        gizli.popen([exe, "-I", BETIK, "--durum-yenile"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, creationflags=0x00000008 | 0x00000200 | 0x00000040)
    except Exception:
        pass


def _kilit_al():
    """Non-blocking exclusive lock on durum.kilit, held until the process exits (fd kept in a global).
    Returns False when another refresh holds it. POSIX: fcntl.flock; Windows: msvcrt.locking on the first byte."""
    global _KILIT_FD
    try:
        os.makedirs(DIZIN, mode=0o700, exist_ok=True)
        fd = os.open(KILIT, os.O_RDWR | os.O_CREAT, 0o600)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _KILIT_FD = fd
        return True
    except OSError:
        return False
    except Exception:
        return True


def pencere_parcalar(n_gun, simdi_h, e, saat, gun_usd):
    """Rolling window = the last n_gun*24 hour buckets (the current partial hour included).
    Returns [("s", hour, 1.0) | ("g", "YYYY-MM-DD", share)].
    Limit: the hourly record starts at hour e (the oldest bucket). When the window reaches back before e, those hours are
    completed from the daily totals: the part of a day's total not covered by hourly buckets (daily - that day's hourly sum)
    is spread evenly over the hours before e and the share that overlaps the window is taken.
    saat: {hour: usd}; gun_usd: {day: usd}."""
    bas = simdi_h - 24 * n_gun + 1
    out = [("s", h, 1.0) for h in range(bas, simdi_h + 1) if h in saat]
    if e is None or bas >= e:
        return out
    lt = time.localtime(bas * 3600)
    gun_bas = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    while gun_bas // 3600 < e:
        lt = time.localtime(gun_bas)
        iso = time.strftime("%Y-%m-%d", lt)
        sonraki = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1)))
        h0 = -(-gun_bas // 3600)
        h_son = -(-sonraki // 3600)
        h1 = min(h_son, e)
        kes0 = max(h0, bas)
        top = gun_usd.get(iso) or 0.0
        if top > 0 and h1 > h0 and h1 > kes0:
            var = sum(saat[h] for h in range(h1, h_son) if h in saat)
            eksik = max(0.0, top - var)
            out.append(("g", iso, eksik / top * (h1 - kes0) / (h1 - h0)))
        gun_bas = sonraki
    return out


KUR = os.path.join(DIZIN, "kur.json")


def kur_oku():
    """Cached USD->TRY rate (TCMB ForexSelling), None when there is none yet."""
    try:
        with open(KUR, encoding="utf-8") as f:
            k = float(json.load(f).get("kur"))
        return k if 1.0 <= k <= 1000.0 else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def para(usd, kur=None, tam=False):
    """Display an USD amount in lira at the cached rate: '₺' prefix, Turkish number format (thousands '.', decimals ','),
    2 decimals under 100 and none from 100 up ('₺2,31', '₺205', '₺18.950'). Without a cached rate: dollars ('$2.31', '$205';
    tam=True: whole dollars, for rounded sums)."""
    usd = float(usd)
    kur = kur_oku() if kur is None else kur
    if not kur:
        return ("$%.2f" if abs(usd) < 100 and not tam else "$%.0f") % usd
    v = usd * kur
    if abs(v) < 100:
        return "₺" + ("%.2f" % v).replace(".", ",")
    return "₺" + ("{:,.0f}".format(v)).replace(",", ".")


def dolar_tr(usd):
    """Whole dollars in the same Turkish style as para(): '$' prefix, thousands '.', 2 decimals (',') under 100 ('$1.763', '$2,31')."""
    usd = float(usd)
    if abs(usd) < 100:
        return "$" + ("%.2f" % usd).replace(".", ",")
    return "$" + ("{:,.0f}".format(usd)).replace(",", ".")


def katsayi():
    """API $ -> 'gerçek' factor of the quota calibration (maliyet_oran.katsayi_oku), None without a fresh calibration."""
    try:
        import maliyet_oran
        return maliyet_oran.katsayi_oku()
    except Exception:
        return None


def maliyet_cifti(usd, tam=False):
    """Amount as in the agent rows: '₺20 (₺0,33)' = API-price amount, then in parentheses what that usage really cost on the plan
    (gerçek, the quota calibration of maliyet_oran.py); only '₺20' without a fresh calibration. tam=True: the API amount is
    rounded to whole dollars first (rolling-window sums)."""
    k = katsayi()
    return para(round(usd) if tam else usd, None, tam) + (" (%s)" % para(usd * k) if k else "")


TUR = os.path.join(DIZIN, "tur")
TUR_ESKI = 7 * 86400
SON_MIN_SN = 300    # floor of the "son <süre>" window of the status line (see girdi_tutarlar)
TUR_GECMIS = 40     # earlier prompt boundaries kept per session (lines 2+ of the state file)


def tur_tutari(sid, pid, toplam, dizin=None):
    return _tur(sid, pid, toplam, dizin)[0]


def _tur(sid, pid, toplam, dizin=None, gecmis=None):
    """(USD, epoch of the boundary) -- USD cost of the current/last action = Claude Code's cost.total_cost_usd now minus its value when the action began.
    An action is one user prompt (prompt_id). Baseline = the last total seen under the previous prompt_id (state per session in
    <config dir>/maliyet/tur/<session>: "prompt_id baseline last_total"); a total that dropped (/clear, new session) restarts at 0,
    so the result is never negative. The first sighting of a session has no baseline: it starts at the current total. Atomic write.
    The boundary time is when this prompt_id was first seen (4th field of the state file; 3-field files of older versions: unknown).
    Lines 2+ of the state file are the earlier prompts' "boundary baseline" pairs, newest first (older versions read line 1 only);
    when `gecmis` is a list it receives them as (epoch, baseline USD) tuples."""
    dizin = dizin or TUR
    ad = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(sid))[:120]
    yol = os.path.join(dizin, ad)
    sp = sb = sl = st = None
    eski = []
    try:
        with open(yol, encoding="utf-8") as f:
            parca = f.readline().split()
            for r in f:
                x = r.split()
                if len(x) == 2:
                    eski.append((float(x[0]), float(x[1])))
        if len(parca) in (3, 4):
            sp, sb, sl = parca[0], float(parca[1]), float(parca[2])
            st = float(parca[3]) if len(parca) == 4 else None
    except (OSError, ValueError):
        pass
    simdi = time.time()
    bts = st if sp == pid and st else simdi     # same prompt: keep the boundary time
    if sp is None:
        taban = toplam
    elif sp != pid:
        taban = sl if toplam >= sl else 0.0
    else:
        taban = sb
    if toplam < taban:
        taban = 0.0
    if sp is not None and sp != pid and st:
        eski.insert(0, (st, sb))     # the prompt that just ended becomes history
    eski = eski[:TUR_GECMIS]
    if gecmis is not None:
        gecmis.extend(eski)
    try:
        yeni = sp is None
        os.makedirs(dizin, mode=0o700, exist_ok=True)
        if yeni:     # a new session: drop state files older than a week
            sinir = time.time() - TUR_ESKI
            for e in os.scandir(dizin):
                try:
                    if e.is_file() and e.stat().st_mtime < sinir:
                        os.remove(e.path)
                except OSError:
                    pass
        tmp = "%s.tmp%d" % (yol, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("%s %.6f %.6f %.0f\n" % (pid, taban, toplam, bts) + "".join("%.0f %.6f\n" % e for e in eski))
        os.replace(tmp, yol)
    except OSError:
        pass
    return toplam - taban, bts


def sure_tr(sn):
    """Elapsed time: '45sn' under a minute, '7dk' from 60 s, '1sa20dk' from 60 min up (the son Ndk label)."""
    sn = max(0, int(sn))
    if sn < 60:
        return "%dsn" % sn
    dk = sn // 60
    return "%dsa%ddk" % (dk // 60, dk % 60) if dk >= 60 else "%ddk" % dk


def girdi_tutarlar(ham):
    """Status-line JSON text -> ["<session total USD>", "<last action USD>", "<USD since the prompt boundary incl. agents>",
    "<boundary epoch>"] (the last two only with a baseline); "-" for a value that cannot be known."""
    try:
        d = json.loads(ham)
        t = d["cost"]["total_cost_usd"]
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            return ["-", "-"]
        t = float(t)
        sid, pid = d.get("session_id"), d.get("prompt_id")
        if not sid or not pid:
            return ["%.6f" % t, "-"]
        gecmis = []
        delta, bts = _tur(sid, str(pid), t, None, gecmis)
        hepsi = delta     # total_cost_usd delta: main session + its agents
        # "son <süre>" floor: a prompt younger than SON_MIN_SN (a 2 s window says nothing) is widened to the newest earlier prompt
        # boundary that is at least that old (the oldest known one when none is), with the cost since that boundary
        if time.time() - bts < SON_MIN_SN:
            for b, tb in gecmis:
                if tb <= t:
                    bts, hepsi = b, t - tb
                    if time.time() - b >= SON_MIN_SN:
                        break
        tp = d.get("transcript_path")
        if isinstance(tp, str) and tp.endswith(".jsonl") and os.path.isfile(tp):
            # the last action = the main session's own responses since its last prompt (agents have their own rows); the
            # total_cost_usd delta (which includes agents) is the fallback when the transcript cannot be read
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # python -I does not add the script directory
            try:
                import maliyet_artimli
                v = maliyet_artimli.tur_toplam(tp, str(sid))
            except Exception:
                v = None
            if v is not None:
                delta = max(v, 0.0)
        return ["%.6f" % t, "%.6f" % delta, "%.6f" % hepsi, "%.0f" % bts]
    except (ValueError, KeyError, TypeError, AttributeError):
        return ["-", "-"]


def durum_satir():
    _tz()
    try:
        with open(DURUM, encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = None
    if c is None or "saat" not in c or time.time() - float(c.get("t") or 0) > DURUM_TTL:
        _yenile_baslat()
    if not c or "saat" not in c:
        return ""
    try:
        saat = {int(k): float(v) for k, v in c["saat"].items()}
        gun = c.get("gunler") or {}
        e = c.get("e")
        simdi_h = int(time.time() // 3600)
        parcalar = []
        kur = kur_oku()
        for ad, n in PENCERELER:
            t = 0.0
            for tur, k, w in pencere_parcalar(n, simdi_h, e, saat, gun):
                t += saat[k] if tur == "s" else float(gun[k]) * w
            if ad == "7g" and kur:    # the weekly window also in dollars, without parentheses (they are for the real cost)
                a = maliyet_cifti(t, True).partition(" (")
                txt = "%s %s %s%s" % (ad, a[0], dolar_tr(t), a[1] + a[2] if a[1] else "")
            else:
                txt = "%s %s" % (ad, maliyet_cifti(t, True))
            parcalar.append(txt)
    except (ValueError, TypeError, KeyError):
        return ""
    k = katsayi()
    if k:     # how many times cheaper the plan is than API list prices: 1/katsayi = (API $ per 100 % of the week) / (weekly plan $)
        parcalar.append("×%d" % round(1.0 / k))
    else:     # no calibration (plan unknown, e.g. on macOS the login may live in the Keychain; or too little data): the amounts of the whole
              # line are API list-price equivalents of the local transcripts, not what the subscription charges
        parcalar.append(API_ETIKET)
    return " · ".join(parcalar)


def durum_yaz(tutarlar=(), aksiyon=False):
    """Line 1: the rolling-window string (empty when there is no cache yet). Then one line per USD amount in `tutarlar`,
    in lira; with `aksiyon` each amount (oturum, last action) is the '₺A (₺B)' text instead, and the 3rd/4th items (USD since
    the prompt boundary incl. agents, boundary epoch) become the first window 'son 7dk ₺A (₺B)' of line 1 (omitted when 0 or unknown).
    UTF-8 bytes whatever the console code page is (Windows would write the middle dot as cp1252)."""
    satirlar = [durum_satir()]
    if aksiyon and len(tutarlar) >= 4:
        try:
            u, bts = float(tutarlar[2]), float(tutarlar[3])
            if u > 0 and bts > 0:
                on = "son %s %s" % (sure_tr(time.time() - bts), maliyet_cifti(u))
                satirlar[0] = on + (" · " + satirlar[0] if satirlar[0] else "")
        except ValueError:
            pass
        tutarlar = tutarlar[:2]
    for i, t in enumerate(tutarlar):
        try:
            satirlar.append(maliyet_cifti(float(t)) if aksiyon else para(float(t)))
        except ValueError:
            satirlar.append("")
    cikti = "\n".join(satirlar) if tutarlar else satirlar[0]
    sys.stdout.buffer.write(cikti.encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


def _planli_calistir(ana):
    """The scheduled run (--kaydet / --paylas: timer or Task Scheduler under pythonw, no stdout, no stderr): leaves a run log in
    ~/.local/state/maliyet.log (env MALIYET_LOG; rolling, tools/gunluk.py) with start, the sharing steps (maliyet_paylas), the exit
    code and, on a crash, the traceback. The exit code and the exception go on unchanged; a log that cannot be written is ignored."""
    lg = None
    try:
        import gunluk
        lg = gunluk.gunluk("maliyet")
        lg.info("start pid=%d args=%s", os.getpid(), " ".join(sys.argv[1:]))
    except Exception:
        pass
    t0, kod = time.time(), 1
    try:
        kod = ana() or 0
        return kod
    except SystemExit as e:
        kod = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        raise
    except BaseException:
        if lg:
            import traceback
            lg.error("crash: %s", traceback.format_exc())
        raise
    finally:
        if lg:
            lg.info("exit=%s duration=%.1fs", kod, time.time() - t0)


if __name__ == "__main__":
    if "--durum" in sys.argv[1:]:
        a = sys.argv[1:]
        if "--girdi" in a:
            tutarlar = girdi_tutarlar(sys.stdin.buffer.read().decode("utf-8", "replace"))
        else:
            tutarlar = a[a.index("--para") + 1:] if "--para" in a else ()
        sys.exit(durum_yaz(tutarlar, "--girdi" in a))  # fast path: only this small file is compiled, the core is not loaded
    if "--durum-yenile" in sys.argv[1:] and not _kilit_al():
        sys.exit(0)  # another refresh is running
    import maliyet_cekirdek
    if any(a in sys.argv[1:] for a in ("--kaydet", "--paylas", "--siralama")):
        sys.exit(_planli_calistir(maliyet_cekirdek.main))
    sys.exit(maliyet_cekirdek.main())


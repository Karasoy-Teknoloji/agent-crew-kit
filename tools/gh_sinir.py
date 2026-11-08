#!/usr/bin/env python3
"""gh-sinir: machine-wide GitHub API budget guard in front of the real `gh` (all sessions and agents of one machine share it).

  gh ...                                   # through the shim ~/.local/bin/gh that `--kur` writes (kur.sh / kur.ps1 do it)
  python3 tools/gh_sinir.py --kur          # write the shim (points at this clone; falls back to the real gh if the clone is gone)
  python3 tools/gh_sinir.py --kaldir       # remove the shim (only if it is ours)
  python3 tools/gh_sinir.py --ozet [--dakika 30]   # summary of the call log: calls/min, top endpoints, 403/429s, waits

Why: ~6 sessions x 10-20 agents share one token. The hourly budget has room, but GitHub's secondary limits (concurrency, points/min,
content-creating requests/min, search 30/min) stall agents with 403 "secondary rate limit". GitHub docs ("Rate limits for the REST
API", "secondary rate limits"): <=100 concurrent requests, ~900 points/min REST, ~80 content-creating requests/min and ~500/h,
search 30/min (code search 10/min); after a limit hit wait `retry-after`, else until `x-ratelimit-reset` when remaining is 0, else at
least one minute, growing exponentially.

What it does per call (state in ~/.cache/gh-sinir, shared through flock by every process of this user):
  - at most SLOTS (8) gh processes at once (one lock file per slot; a crashed process frees its slot with its file lock);
  - content-creating calls (gh api POST/PATCH/PUT/DELETE except GraphQL queries, GraphQL mutations, gh issue/pr/label/... writes):
    >=1 s apart, <=60/min, <=450/h; search: <=25/min (code search <=9/min);
  - on 403/429 rate-limit answers: a machine-wide pause (retry-after / x-ratelimit-reset / 60 s doubling, cap 300 s), then a retry
    (at most MAX_DENEME retries) -- only when stdout is not a terminal (output is buffered so a retry never duplicates output);
  - `gh api` GETs get an ETag cache (If-None-Match; a 304 replays the cached output, and 304s do not count against the primary limit);
  - `gh run watch` / `gh pr checks --watch` get `--interval 30` unless an interval is given (gh polls every 3-10 s by default);
  - when fewer than 1500 calls of the hourly core/graphql budget are left (x-ratelimit-* of earlier answers), calls are paced so
    the rest lasts until the reset (shared by MAKINE=2 machines, at most 30 s apart) instead of running dry and stalling everyone;
  - one JSON line per call in ~/.cache/gh-sinir/log (class, endpoint, status, waits, attempts, remaining budget).
Everything else passes through unchanged: arguments, stdin, exit code, output. `gh auth` (the git credential helper), `gh help`,
`gh --version`, nested calls (GH_SINIR_AKTIF) and GH_SINIR_KAPALI=1 go straight to the real gh.
Windows: same code path (the shared locks use msvcrt instead of flock). `--kur` writes ~/.local/bin/gh (sh shim, Git Bash) and
~/.local/bin/gh.ps1 (pwsh shim), and a marked block in the pwsh all-hosts profile that puts ~/.local/bin first on PATH (the machine
PATH, which holds gh.exe, always comes before the user PATH and needs admin to change); `--kaldir` removes all three.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # python -I drops the script dir
from gizli_yukle import gizli  # noqa: E402  (one hidden-launch helper)
import time
from pathlib import Path

try:
    import fcntl
except ImportError:          # Windows: shared locks come from msvcrt.locking
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None
WINDOWS = os.name == "nt"

DIZIN = Path(os.environ.get("GH_SINIR_DIZIN") or (Path.home() / ".cache" / "gh-sinir"))
SLOTS = int(os.environ.get("GH_SINIR_SLOT", "8"))
MAX_DENEME = int(os.environ.get("GH_SINIR_MAX_DENEME", "4"))
BEKLE_UST = 300.0            # cap of one backoff wait (s)
DURAKLA_UST = 900.0          # never wait longer than this for a pause (primary exhaustion -> fail fast with gh's own error)
LOG_UST = 5 * 1024 * 1024
TEMPO_ESIK = int(os.environ.get("GH_SINIR_TEMPO_ESIK", "1500"))   # below this many calls left in the hour, pace the calls
TEMPO_YEDEK = 200            # calls kept back for the end of the window
MAKINE = int(os.environ.get("GH_SINIR_MAKINE", "1"))   # config key: machines sharing one GitHub token (default 1)
TEMPO_UST = 30.0             # never space calls more than this (s): slower, but never stuck
IZLE_ARALIK = int(os.environ.get("GH_SINIR_IZLE_ARALIK", "30"))   # poll interval forced on `gh run watch` / `--watch`
ONBELLEK_UST = 2 * 1024 * 1024
ISARET = "# gh-sinir shim"   # marker line of the shim we write; --kur/--kaldir never touch a ~/.local/bin/gh without it
PROFIL_BAS, PROFIL_SON = "# >>> gh-sinir (tools/gh_sinir.py --kur) >>>", "# <<< gh-sinir <<<"   # managed block of the pwsh profile

# bucket: (min gap s, max per minute, max per hour)
KOVA = {"yazma": (1.0, 60, 450), "arama": (2.0, 25, 10**9), "kod_arama": (6.5, 9, 10**9)}

GECIS = {"auth", "help", "version", "completion", "config", "alias", "extension", "ext", "-h", "--help", "--version", "-v"}
IZLE = {("run", "watch")}
YAZMA = {
    "issue": {"create", "comment", "edit", "close", "reopen", "delete", "transfer", "pin", "unpin", "lock", "unlock", "develop"},
    "pr": {"create", "comment", "edit", "close", "reopen", "merge", "ready", "review", "lock", "unlock", "update-branch"},
    "label": {"create", "edit", "delete", "clone"}, "release": {"create", "edit", "delete", "upload", "delete-asset"},
    "run": {"rerun", "cancel", "delete"}, "workflow": {"run", "enable", "disable"}, "cache": {"delete"},
    "project": {"create", "edit", "delete", "close", "copy", "item-add", "item-create", "item-edit", "item-delete", "item-archive",
                "field-create", "field-delete", "link", "unlink", "mark-template"},
    "repo": {"create", "edit", "delete", "fork", "rename", "archive", "unarchive", "sync", "deploy-key", "autolink"},
    "gist": {"create", "edit", "delete", "rename", "clone"}, "secret": {"set", "delete"}, "variable": {"set", "delete"},
}
# gh api flags that take a value (both `-X POST` and `-XPOST` / `--method=POST` forms are handled)
API_DEGERLI = {"-X": "method", "--method": "method", "-H": "header", "--header": "header", "-f": "field", "--raw-field": "field",
               "-F": "field", "--field": "field", "--input": "input", "-q": "jq", "--jq": "jq", "-t": "template",
               "--template": "template", "--cache": "cache", "-p": "preview", "--preview": "preview", "--hostname": "hostname"}
API_BAYRAK = {"-i": "include", "--include": "include", "--paginate": "paginate", "--slurp": "slurp", "--silent": "silent",
              "--verbose": "verbose"}
SINIR_RE = re.compile(r"secondary rate limit|rate limit exceeded|abuse detection|HTTP 429|too many requests", re.I)


# ---------------------------------------------------------------- argument analysis (pure)

def api_ayristir(args):
    """Parse the arguments after `gh api`: {"endpoint", "method", "header": [...], "field": [...], flags...}."""
    r = {"endpoint": None, "header": [], "field": [], "input": None, "jq": None, "template": None, "cache": None}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            if r["endpoint"] is None and i + 1 < len(args):
                r["endpoint"] = args[i + 1]
            break
        ad, deger = a, None
        if a.startswith("--") and "=" in a:
            ad, deger = a.split("=", 1)
        elif len(a) > 2 and a[:2] in API_DEGERLI and not a.startswith("--"):
            ad, deger = a[:2], a[2:]
        if ad in API_DEGERLI:
            if deger is None:
                i += 1
                deger = args[i] if i < len(args) else ""
            k = API_DEGERLI[ad]
            if k in ("header", "field"):
                r[k].append(deger)
            else:
                r[k] = deger
        elif a in API_BAYRAK:
            r[API_BAYRAK[a]] = True
        elif not a.startswith("-") and r["endpoint"] is None:
            r["endpoint"] = a
        i += 1
    m = r.get("method")
    r["method"] = (m or ("POST" if (r["field"] or r["input"]) else "GET")).upper()
    return r


def siniflandir(args):
    """(class, endpoint label, api-parse or None). Classes: gecis, izle, yazma, arama, kod_arama, graphql, okuma."""
    poz = [a for a in args if not a.startswith("-")]
    if not args or args[0] in GECIS or (poz and poz[0] in GECIS) or "--help" in args or "-h" in args[:3]:
        return "gecis", " ".join(args[:2]), None
    grup = poz[0] if poz else args[0]
    fiil = poz[1] if len(poz) > 1 else ""
    if grup == "api":
        p = api_ayristir(args[args.index("api") + 1:])
        uc = p["endpoint"] or ""
        etiket = "api %s %s" % (p["method"], uc_normal(uc))
        if uc == "graphql":
            metin = " ".join(p["field"]) + " " + (p["input"] or "")
            return ("yazma" if re.search(r"\bmutation\b", metin) else "graphql"), etiket, p
        yol = uc.lstrip("/")
        if yol.startswith("search/code"):
            return "kod_arama", etiket, p
        if yol.startswith("search/"):
            return "arama", etiket, p
        return ("okuma" if p["method"] in ("GET", "HEAD") else "yazma"), etiket, p
    etiket = "%s %s" % (grup, fiil)
    if (grup, fiil) in IZLE or "--watch" in args:
        return "izle", etiket, None
    if grup == "search":
        return ("kod_arama" if fiil == "code" else "arama"), etiket, None
    if fiil in YAZMA.get(grup, ()):
        return "yazma", etiket, None
    return "okuma", etiket, None


def izle_araligi(args):
    """`gh run watch` / `--watch` poll every 3 s (pr checks: 10 s) by default: add `--interval IZLE_ARALIK` unless one is given."""
    if any(a in ("-i", "--interval") or a.startswith("--interval=") for a in args):
        return args
    return args + ["--interval", str(IZLE_ARALIK)]


def pr_kisa_devre(args):
    """Branch-lookup `gh pr view` / `gh pr status` (no PR number or URL) is answered locally: the team never opens PRs in its own repos, and
    Claude Code's PR-status lookup in every session made these the top caller (778 of 3102 calls in 20 min, 04.11.2026).
    GH_SINIR_PR=1 turns it off; an explicit PR number/URL/branch argument always goes to GitHub."""
    if os.environ.get("GH_SINIR_PR") == "1":
        return False
    poz = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-R", "--repo", "--json", "-q", "--jq", "-t", "--template"):
            i += 2
            continue
        if not a.startswith("-"):
            poz.append(a)
        i += 1
    return poz[:2] == ["pr", "status"] or (poz[:2] == ["pr", "view"] and len(poz) == 2)


def uc_normal(uc):
    """Endpoint label for the log: query string dropped, numbers -> :n, shas -> :sha (owner/repo kept, they matter for the top list)."""
    yol = uc.split("?", 1)[0].lstrip("/")
    parca = []
    for s in yol.split("/"):
        if s.isdigit():
            s = ":n"
        elif re.fullmatch(r"[0-9a-f]{7,40}", s):
            s = ":sha"
        parca.append(s)
    return "/".join(parca)


# ---------------------------------------------------------------- limiter math (pure)

def rezerve(zamanlar, simdi, aralik, dakika_ust, saat_ust):
    """Earliest start time t >= simdi for a new call given the reserved start times `zamanlar` (sorted): t is at least `aralik` after
    the last one, and the windows (t-60, t] and (t-3600, t] keep below their caps."""
    z = sorted(zamanlar)
    t = max(simdi, (z[-1] + aralik) if z else simdi)
    for _ in range(10000):
        son_dk = [x for x in z if x > t - 60]
        son_sa = [x for x in z if x > t - 3600]
        if len(son_dk) >= dakika_ust:
            t = son_dk[len(son_dk) - dakika_ust] + 60.0
            continue
        if len(son_sa) >= saat_ust:
            t = son_sa[len(son_sa) - saat_ust] + 3600.0
            continue
        return t
    return t


def tempo_araligi(kalan, sifirlanma, simdi):
    """Seconds between calls of one machine so the hourly budget lasts until its reset: 0 while more than TEMPO_ESIK calls are left,
    else (seconds to reset) / (calls left - TEMPO_YEDEK) * MAKINE, capped at TEMPO_UST."""
    if kalan is None or sifirlanma is None or sifirlanma <= simdi or kalan > TEMPO_ESIK:
        return 0.0
    return min((sifirlanma - simdi) / max(kalan - TEMPO_YEDEK, 1) * MAKINE, TEMPO_UST)


def kaynak(sinif, args, p):
    """Rate-limit resource a call spends: graphql for GraphQL and gh issue/pr/project/repo commands, else core (search has its own)."""
    if p is not None:
        return "graphql" if p["endpoint"] == "graphql" else "core"
    grup = next((a for a in args if not a.startswith("-")), "")
    return "core" if grup in ("run", "workflow", "release", "cache", "secret", "variable", "gist", "label") else "graphql"


def tempo_bekle(res):
    """Pace this call when the shared hourly budget of `res` runs low (last seen x-ratelimit-* values). Returns seconds waited."""
    simdi = time.time()

    def f(st):
        b = st.get("butce", {}).get(res)
        ara = tempo_araligi(b and b.get("kalan"), b and b.get("reset"), simdi)
        if not ara:
            return simdi
        z = [x for x in st.get("tempo_" + res, []) if x > simdi - 3600]
        t = rezerve(z[-50:], simdi, ara, 10**9, 10**9)
        t = min(t, simdi + 60.0)   # bounded backlog: one call waits at most 60 s (agent tool timeouts), never piles up
        st["tempo_" + res] = z[-50:] + [t]
        return t
    bekle = max(durum_degistir(f) - time.time(), 0.0)
    if bekle:
        time.sleep(bekle)
    return bekle


def butce_kaydet(bas_h):
    res, kalan, reset = bas_h.get("x-ratelimit-resource"), bas_h.get("x-ratelimit-remaining"), bas_h.get("x-ratelimit-reset")
    if res and kalan and kalan.isdigit() and reset and reset.isdigit():
        durum_degistir(lambda st: st.setdefault("butce", {}).__setitem__(res, {"kalan": int(kalan), "reset": int(reset)}))


def bekleme_suresi(deneme, retry_after=None, kalan=None, sifirlanma=None, simdi=None):
    """Seconds to wait after a rate-limited answer (GitHub's documented order): retry-after; x-ratelimit-reset when remaining is 0;
    else 60 s doubling per attempt (deneme = 1, 2, ...), capped at BEKLE_UST. Returns (seconds, primary_exhausted)."""
    simdi = time.time() if simdi is None else simdi
    if retry_after is not None:
        return min(max(float(retry_after), 1.0), BEKLE_UST), False
    if kalan == 0 and sifirlanma:
        return max(float(sifirlanma) - simdi + 1.0, 1.0), True
    return min(60.0 * (2 ** (deneme - 1)), BEKLE_UST), False


def baslik_ayir(veri):
    """Split `gh api -i` stdout into (status, headers{lower: value}, body bytes). Unknown shape -> (None, {}, veri)."""
    if not veri.startswith(b"HTTP/"):
        return None, {}, veri
    for ayrac in (b"\r\n\r\n", b"\n\n"):
        k = veri.find(ayrac)
        if k >= 0:
            break
    else:
        k, ayrac = len(veri), b""
    satirlar = veri[:k].decode("latin-1").splitlines()
    try:
        durum = int(satirlar[0].split()[1])
    except (IndexError, ValueError):
        return None, {}, veri
    bas = {}
    for s in satirlar[1:]:
        if ":" in s:
            a, d = s.split(":", 1)
            bas[a.strip().lower()] = d.strip()
    return durum, bas, veri[k + len(ayrac):]


def sinirli_mi(durum, bas, rc, stderr):
    """True for a rate-limit answer (primary or secondary), False for any other result."""
    if durum in (403, 429):
        return durum == 429 or "retry-after" in bas or bas.get("x-ratelimit-remaining") == "0" or bool(SINIR_RE.search(stderr))
    return rc != 0 and durum is None and bool(SINIR_RE.search(stderr))


# ---------------------------------------------------------------- shared state (flock)

def kilitle(f, engelsiz):
    """Exclusive lock on an open file, released when it is closed (or the process dies). OSError when engelsiz and it is taken."""
    if fcntl:
        fcntl.flock(f, fcntl.LOCK_EX | (fcntl.LOCK_NB if engelsiz else 0))
        return
    f.seek(0)
    while True:     # Windows: lock the first byte; LK_LOCK gives up after ~10 s, so the blocking form polls LK_NBLCK
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if engelsiz:
                raise
            time.sleep(0.05)


class Kilit:
    def __init__(self, yol, engelsiz=False):
        self.yol, self.engelsiz, self.f = yol, engelsiz, None

    def __enter__(self):
        self.f = open(self.yol, "a+")
        try:
            kilitle(self.f, self.engelsiz)
        except OSError:
            self.f.close()
            self.f = None
        return self.f

    def __exit__(self, *_):
        if self.f:
            self.f.close()


def durum_degistir(fn):
    """Read-modify-write ~/.cache/gh-sinir/kova.json under its lock; fn(state) returns a value."""
    with Kilit(DIZIN / "kova.lock"):
        yol = DIZIN / "kova.json"
        try:
            st = json.loads(yol.read_text())
        except (OSError, ValueError):
            st = {}
        sonuc = fn(st)
        tmp = yol.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        for i in range(20):     # Windows: replacing a file that another process is reading fails for a moment
            try:
                os.replace(tmp, yol)
                break
            except PermissionError:
                if i == 19:
                    raise
                time.sleep(0.05)
        return sonuc


def kova_bekle(sinif):
    """Reserve a start time in the class bucket; sleep until it. Returns the seconds waited."""
    if sinif not in KOVA:
        return 0.0
    aralik, dk, sa = KOVA[sinif]
    simdi = time.time()

    def f(st):
        z = [x for x in st.get(sinif, []) if x > simdi - 3600]
        t = rezerve(z, simdi, aralik, dk, sa)
        t = min(t, simdi + 60.0)   # bounded backlog: a write waits at most 60 s; past the caps GitHub's 403 + retry takes over
        z.append(t)
        st[sinif] = z
        return t
    t = durum_degistir(f)
    bekle = max(t - time.time(), 0.0)
    if bekle:
        time.sleep(bekle)
    return bekle


def durakla_bekle():
    """Honour a machine-wide pause set after a rate-limit answer (capped at DURAKLA_UST). Returns the seconds waited."""
    try:
        until = json.loads((DIZIN / "kova.json").read_text()).get("durdur", 0)
    except (OSError, ValueError):
        return 0.0
    bekle = min(max(until - time.time(), 0.0), DURAKLA_UST)
    if bekle:
        time.sleep(bekle)
    return bekle


def durakla_koy(saniye):
    until = time.time() + saniye
    durum_degistir(lambda st: st.__setitem__("durdur", max(st.get("durdur", 0), until)))


def slot_al(zaman_asimi=600.0):
    """Take one of SLOTS lock files (non-blocking scan, 0.2 s pause between scans). After zaman_asimi run without a slot."""
    bas = time.time()
    while True:
        for i in range(SLOTS):
            k = Kilit(DIZIN / ("slot.%d" % i), engelsiz=True)
            if k.__enter__():
                return k, time.time() - bas
        if time.time() - bas > zaman_asimi:
            return None, time.time() - bas
        time.sleep(0.2)


def log_yaz(kayit):
    yol = DIZIN / "log"
    try:
        if yol.exists() and yol.stat().st_size > LOG_UST:
            os.replace(yol, yol.with_name("log.1"))
        fd = os.open(yol, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, (json.dumps(kayit, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------- ETag cache

def onbellek_uygun(p):
    return (p is not None and p["method"] == "GET" and p["endpoint"] not in (None, "graphql") and not p.get("paginate")
            and not p.get("include") and not p.get("silent") and not p.get("cache") and not p.get("input")
            and not any(h.lower().startswith("if-none-match") for h in p["header"]))


def onbellek_anahtar(args):
    kimlik = [args, os.environ.get("GH_HOST", ""), os.environ.get("GH_REPO", ""),
              hashlib.sha256(os.environ.get("GH_TOKEN", "").encode()).hexdigest()[:12]]
    if any("{" in a for a in args):     # {owner}/{repo} placeholders resolve from the current directory's git remote
        kimlik.append(os.getcwd())
    return hashlib.sha256(json.dumps(kimlik).encode()).hexdigest()


# ---------------------------------------------------------------- main path

def gercek_gh():
    """The real gh: GH_SINIR_GERCEK (set by the shim) or the first `gh` on PATH that is not a gh-sinir shim."""
    g = os.environ.get("GH_SINIR_GERCEK")
    if g and os.access(g, os.X_OK):
        return g
    for d in os.environ.get("PATH", "").split(os.pathsep):
        c = os.path.join(d, "gh.exe" if WINDOWS else "gh")
        if os.path.isfile(c) and os.access(c, os.X_OK):
            try:
                with open(c, "rb") as f:
                    if ISARET.encode() in f.read(4096):
                        continue
            except OSError:
                continue
            return c
    return r"C:\Program Files\GitHub CLI\gh.exe" if WINDOWS else "/usr/bin/gh"


def gecis(gh, args):
    if WINDOWS:     # os.execv does not replace the process on Windows: the prompt would return before gh ends
        sys.exit(gizli.run([gh] + args, gorunur=True).returncode)   # gorunur: interactive pass-through, the terminal's console is inherited (Ctrl+C, prompts)
    os.execv(gh, [gh] + args)


def calistir(args):
    gh = gercek_gh()
    if (fcntl is None and msvcrt is None) or os.environ.get("GH_SINIR_KAPALI") == "1" or os.environ.get("GH_SINIR_AKTIF") == "1":
        gecis(gh, args)
    sinif, etiket, p = siniflandir(args)
    if sinif == "gecis":
        gecis(gh, args)
    if pr_kisa_devre(args):
        try:
            dal = gizli.run(["git", "branch", "--show-current"], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            dal = ""
        sys.stderr.write('no pull requests found for branch "%s"\n' % dal)
        try:
            DIZIN.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            pass
        log_yaz({"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "h": socket.gethostname(), "pid": os.getpid(), "sinif": "yerel",
                 "uc": etiket, "durum": None, "rc": 1, "onbellek": True, "kisa_devre": True})
        return 1
    try:
        DIZIN.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        gecis(gh, args)
    env = dict(os.environ, GH_SINIR_AKTIF="1")
    kayit = {"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "h": socket.gethostname(), "pid": os.getpid(), "sinif": sinif, "uc": etiket}
    bas = time.time()
    tampon = not sys.stdout.isatty() and sinif != "izle" and os.environ.get("GH_SINIR_AKIS") != "1"

    if sinif == "izle":
        args = izle_araligi(args)
        sys.stderr.write("gh-sinir: `gh %s` polls GitHub (default every 3-10 s; raised to %d s here); WORKING-RULES (GitHub API "
                         "budget) asks for one REST check every 4-5 min instead.\n" % (etiket, IZLE_ARALIK))
    if not tampon:      # terminal or watch: stream, no retry (output cannot be taken back)
        bekle = durakla_bekle() + kova_bekle(sinif)
        slot, sw = (None, 0.0) if sinif == "izle" else slot_al()
        try:
            rc = gizli.run([gh] + args, env=env, gorunur=True).returncode   # gorunur: streams to the terminal
        finally:
            if slot:
                slot.__exit__()
        kayit.update(rc=rc, bekle=round(bekle + sw, 2), sure=round(time.time() - bas, 2), mod="akis")
        log_yaz(kayit)
        return rc

    stdin_veri = None
    if not sys.stdin.isatty() and any(a == "-" or a.endswith("@-") for a in args):
        stdin_veri = sys.stdin.buffer.read()   # buffered once so a retry can send it again
    ek, ai = [], (args.index("api") if p is not None else 0)
    enjekte = p is not None and not any(p.get(k) for k in ("include", "paginate", "silent", "verbose"))
    if enjekte:
        ek.append("--include")
    anahtar, onb = None, None
    if onbellek_uygun(p):
        anahtar = onbellek_anahtar(args)
        try:
            onb = json.loads((DIZIN / "etag" / (anahtar + ".json")).read_text())
            ek += ["-H", "If-None-Match: " + onb["etag"]]
        except (OSError, ValueError, KeyError):
            onb = None
    toplam_bekle, deneme = 0.0, 0
    res = None if sinif in ("arama", "kod_arama") else kaynak(sinif, args, p)
    while True:
        deneme += 1
        toplam_bekle += durakla_bekle() + kova_bekle(sinif) + (tempo_bekle(res) if res else 0.0)
        slot, sw = slot_al()
        toplam_bekle += sw
        try:
            r = gizli.run([gh] + args[:ai + 1] + ek + args[ai + 1:], env=env, capture_output=True, input=stdin_veri)
        finally:
            if slot:
                slot.__exit__()
        out, err = r.stdout, r.stderr
        durum, bas_h = None, {}
        if enjekte:
            durum, bas_h, out = baslik_ayir(out)
            butce_kaydet(bas_h)
        err_metin = err.decode("utf-8", "replace")
        if sinirli_mi(durum, bas_h, r.returncode, err_metin) and deneme <= MAX_DENEME:
            ra = bas_h.get("retry-after")
            kalan = bas_h.get("x-ratelimit-remaining")
            sn, birincil = bekleme_suresi(deneme, float(ra) if ra and ra.replace(".", "", 1).isdigit() else None,
                                          int(kalan) if kalan and kalan.isdigit() else None, bas_h.get("x-ratelimit-reset"))
            log_yaz(dict(kayit, durum=durum, rc=r.returncode, deneme=deneme, sinirli=True, bekle_sonra=round(sn, 1)))
            if sn > DURAKLA_UST:      # primary budget gone for a long time: fail now with gh's own message
                break
            durakla_koy(sn)
            continue
        break
    rc = r.returncode
    onbellek = False
    if durum == 304 and onb is not None:
        out, err, rc, onbellek = onb["out"].encode("latin-1"), b"", 0, True
    elif anahtar and durum == 200 and rc == 0 and bas_h.get("etag") and len(out) <= ONBELLEK_UST:
        try:
            (DIZIN / "etag").mkdir(exist_ok=True, mode=0o700)
            tmp = DIZIN / "etag" / (anahtar + ".tmp%d" % os.getpid())
            tmp.write_text(json.dumps({"etag": bas_h["etag"], "out": out.decode("latin-1"), "t": time.time()}))
            os.replace(tmp, DIZIN / "etag" / (anahtar + ".json"))
        except OSError:
            pass
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(err)
    sys.stderr.buffer.flush()
    kayit.update(durum=durum, rc=rc, deneme=deneme, bekle=round(toplam_bekle, 2), sure=round(time.time() - bas, 2))
    for k, a in (("kalan", "x-ratelimit-remaining"), ("kaynak", "x-ratelimit-resource")):
        if a in bas_h:
            kayit[k] = bas_h[a]
    if onbellek:
        kayit["onbellek"] = True
    log_yaz(kayit)
    return rc


# ---------------------------------------------------------------- install / summary

def shim_metni(klon_betik, gercek, python):
    """The sh shim (Linux/macOS; Git Bash on Windows, where paths are written with forward slashes)."""
    q = lambda x: sh_q(Path(x).as_posix() if WINDOWS else x)
    return ("#!/bin/sh\n%s (written by tools/gh_sinir.py --kur; runs the clone's limiter, falls back to the real gh without it)\n"
            "w=%s\n"
            "GH_SINIR_GERCEK=%s; export GH_SINIR_GERCEK\n"
            "if [ -f \"$w\" ] && [ \"${GH_SINIR_KAPALI:-}\" != 1 ] && [ \"${GH_SINIR_AKTIF:-}\" != 1 ] && [ -x %s ]; then\n"
            "  GH_SINIR_SHIM=1 exec %s -I \"$w\" \"$@\"\nfi\nexec \"$GH_SINIR_GERCEK\" \"$@\"\n") % (ISARET, q(klon_betik), q(gercek), q(python),
                                                                                 q(python))


def shim_ps1_metni(klon_betik, gercek, python):
    """The pwsh shim (Windows): native argument passing, piped input forwarded, the session's environment restored afterwards."""
    ps = lambda x: "'" + str(x).replace("'", "''") + "'"
    return (ISARET + " (written by tools/gh_sinir.py --kur; runs the clone's limiter, falls back to the real gh without it)\r\n"
            "$w = %s; $py = %s; $gercek = %s\r\n"
            "$eski = @{}; foreach ($k in 'GH_SINIR_GERCEK', 'GH_SINIR_SHIM') { $eski[$k] = [Environment]::GetEnvironmentVariable($k) }\r\n"
            "try {\r\n"
            "  $env:GH_SINIR_GERCEK = $gercek\r\n"
            "  if ((Test-Path -LiteralPath $w) -and (Test-Path -LiteralPath $py) -and $env:GH_SINIR_KAPALI -ne '1' -and $env:GH_SINIR_AKTIF -ne '1') {\r\n"
            "    $env:GH_SINIR_SHIM = '1'\r\n"
            "    if ($MyInvocation.ExpectingInput) { $input | & $py -I $w @args } else { & $py -I $w @args }\r\n"
            "  } elseif ($MyInvocation.ExpectingInput) { $input | & $gercek @args } else { & $gercek @args }\r\n"
            "  $rc = $LASTEXITCODE\r\n"
            "} finally { foreach ($k in $eski.Keys) { [Environment]::SetEnvironmentVariable($k, $eski[$k]) } }\r\n"
            "exit $rc\r\n") % (ps(klon_betik), ps(python), ps(gercek))


def sh_q(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def profil_yolu():
    """The pwsh all-hosts profile <Documents>/PowerShell/profile.ps1 (Documents from the shell API, it may be redirected); None
    without pwsh, off Windows, or when Documents lies outside the home directory (a scratch HOME must never edit the real profile)."""
    if not (WINDOWS and shutil.which("pwsh")):
        return None
    try:
        import ctypes
        # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
        class GUID(ctypes.Structure):
            _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]
        g = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
        ptr = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(ptr)) != 0:
            return None
        belgeler = Path(ptr.value)
        ctypes.windll.ole32.CoTaskMemFree(ptr)
        yol = belgeler / "PowerShell" / "profile.ps1"
        yol.resolve().relative_to(Path.home().resolve())
        return yol
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def profil_blok(bin_dizin):
    return ("%s\r\n$__ghs = '%s'; $env:Path = $__ghs + ';' + (($env:Path -split ';' | Where-Object { $_ -ne $__ghs }) -join ';')\r\n%s\r\n"
            % (PROFIL_BAS, str(bin_dizin).replace("'", "''"), PROFIL_SON))


def profil_yaz(yol, bin_dizin, kaldir, tag, whatif):
    """Put (or remove) the managed block in the pwsh profile. Everything outside the block is kept."""
    try:
        eski = yol.read_bytes().decode("utf-8-sig")   # bytes: keep the file's own line endings
    except OSError:
        eski = ""
    bas = eski.find(PROFIL_BAS)
    son = eski.find(PROFIL_SON, bas) if bas >= 0 else -1
    temiz = eski if bas < 0 or son < 0 else (eski[:bas] + eski[son + len(PROFIL_SON):].lstrip("\r\n"))
    yeni = temiz if kaldir else (temiz.rstrip("\r\n") + ("\r\n" if temiz.strip() else "") + profil_blok(bin_dizin))
    if yeni.strip() == "" and kaldir:
        yeni = ""
    if yeni == eski:
        print("%sgh-sinir: profil ayni: %s" % (tag, yol))
        return
    print("%sgh-sinir: profil %s: %s" % (tag, "temizle" if kaldir else "yaz", yol))
    if not whatif:
        yol.parent.mkdir(parents=True, exist_ok=True)
        yol.write_bytes(yeni.encode("utf-8"))


def kur(kaldir=False, hedef=None, whatif=False):
    """Write (or remove) the shim ~/.local/bin/gh (+ gh.ps1 and the pwsh profile block on Windows).
    Returns 0 on success, 1 when a foreign ~/.local/bin/gh is in the way."""
    profilli = WINDOWS and hedef is None   # the pwsh profile only follows the default target (an explicit one is a test or a trial)
    hedef = Path(hedef or (Path.home() / ".local" / "bin" / "gh"))
    tag = "[-WhatIf] " if whatif else ""
    ps1 = hedef.with_name(hedef.name + ".ps1")
    bizim = hedef.is_file() and ISARET in hedef.read_text(errors="replace")
    if hedef.exists() and not bizim:
        print("gh-sinir: UYARI: %s baska bir dosya, dokunulmadi" % hedef)
        return 1
    if kaldir:
        for d in ([hedef, ps1] if WINDOWS else [hedef]):
            if d.is_file() and ISARET in d.read_text(errors="replace"):
                print("%sgh-sinir: sil: %s" % (tag, d))
                if not whatif:
                    d.unlink()
        if profilli and (pf := profil_yolu()):
            profil_yaz(pf, hedef.parent, True, tag, whatif)
        return 0
    gercek = gercek_gh()
    if not os.access(gercek, os.X_OK):
        print("gh-sinir: UYARI: gercek gh bulunamadi (%s); shim yazilmadi" % gercek)
        return 1
    python = sys.executable if WINDOWS else (shutil.which("python3") or sys.executable)   # Windows: python3 is the Store stub
    betik = Path(__file__).resolve()
    dosyalar = [(hedef, shim_metni(betik, gercek, python), 0o755)]
    if WINDOWS:
        dosyalar.append((ps1, shim_ps1_metni(betik, gercek, python), 0o644))
    for yol, metin, kip in dosyalar:
        if yol.is_file() and ISARET in yol.read_text(errors="replace") and yol.read_text() == metin.replace("\r\n", "\n"):
            print("%sgh-sinir: ayni: %s" % (tag, yol))
            continue
        if yol.exists() and ISARET not in yol.read_text(errors="replace"):
            print("gh-sinir: UYARI: %s baska bir dosya, dokunulmadi" % yol)
            return 1
        print("%sgh-sinir: yaz: %s (gercek gh: %s)" % (tag, yol, gercek))
        if not whatif:
            yol.parent.mkdir(parents=True, exist_ok=True)
            yol.write_bytes(metin.encode("utf-8"))   # bytes: the sh shim must keep LF on Windows
            yol.chmod(kip)
    if WINDOWS:
        pf = profil_yolu() if profilli else None
        if not profilli:
            pass
        elif pf:
            profil_yaz(pf, hedef.parent, False, tag, whatif)
        else:
            print("gh-sinir: not: pwsh profili yazilmadi (pwsh yok veya profil ev dizininin disinda); %s PATH'te gh.exe'den once "
                  "degilse shim devreye girmez" % hedef.parent)
        return 0
    yollar = os.environ.get("PATH", "").split(os.pathsep)
    if str(hedef.parent) not in yollar or (os.path.dirname(gercek) in yollar
                                           and yollar.index(str(hedef.parent)) > yollar.index(os.path.dirname(gercek))):
        print("gh-sinir: UYARI: %s PATH'te %s'den once degil; shim devreye girmez" % (hedef.parent, os.path.dirname(gercek)))
    return 0


def ozet(dakika):
    sinir = time.time() - dakika * 60
    satirlar = []
    for ad in ("log.1", "log"):
        try:
            for s in (DIZIN / ad).read_text(encoding="utf-8").splitlines():
                try:
                    k = json.loads(s)
                    t = time.mktime(time.strptime(k["t"][:19], "%Y-%m-%dT%H:%M:%S"))
                except (ValueError, KeyError):
                    continue
                if t >= sinir:
                    k["_t"] = t
                    satirlar.append(k)
        except OSError:
            pass
    if not satirlar:
        print("gh-sinir: son %d dakikada kayit yok (%s)" % (dakika, DIZIN / "log"))
        return 0
    cagri = [k for k in satirlar if not k.get("sinirli")]
    dk = {}
    for k in satirlar:
        dk[int(k["_t"] // 60)] = dk.get(int(k["_t"] // 60), 0) + 1
    uc, sinif = {}, {}
    for k in cagri:
        uc[k["uc"]] = uc.get(k["uc"], 0) + 1
        sinif[k["sinif"]] = sinif.get(k["sinif"], 0) + 1
    sinirli = [k for k in satirlar if k.get("sinirli")]
    hata = [k for k in cagri if k.get("durum") in (403, 429)]
    bekle = sorted(k.get("bekle", 0) for k in cagri)
    print("gh-sinir ozeti, son %d dk: %d cagri, %d rate-limit cevabi (yeniden denendi), %d son 403/429, %d ETag 304" % (
        dakika, len(cagri), len(sinirli), len(hata), sum(1 for k in cagri if k.get("onbellek"))))
    print("  dakika basina: ort %.1f, en cok %d" % (len(satirlar) / max(dakika, 1), max(dk.values())))
    print("  sinif: " + ", ".join("%s %d" % x for x in sorted(sinif.items(), key=lambda x: -x[1])))
    print("  bekleme (s): medyan %.2f, en cok %.2f" % (bekle[len(bekle) // 2], bekle[-1]))
    print("  en cok cagrilan:")
    for u, n in sorted(uc.items(), key=lambda x: -x[1])[:12]:
        print("    %5d  %s" % (n, u))
    return 0


def main(argv):
    if os.environ.get("GH_SINIR_SHIM") != "1" and argv[:1] in (["--kur"], ["--kaldir"], ["--ozet"]):
        if argv[0] == "--ozet":
            dk = int(argv[argv.index("--dakika") + 1]) if "--dakika" in argv else 30
            return ozet(dk)
        return kur(kaldir=argv[0] == "--kaldir", whatif="--whatif" in argv)
    return calistir(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

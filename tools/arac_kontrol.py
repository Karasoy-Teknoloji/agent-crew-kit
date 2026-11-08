#!/usr/bin/env python3
"""Command-line tool check for team machines (the single list of the tools agents and claude-shared scripts call).

  python tools/arac_kontrol.py            report only: which tools are present / missing, and the one-line install command per OS
  python tools/arac_kontrol.py --kur      install the missing ones (winget on Windows, apt-get on Linux, brew on macOS; pip --user for
                                          Python packages); only this flag installs anything
  python tools/arac_kontrol.py --whatif   with --kur: print what would run, run nothing
  python tools/arac_kontrol.py --python3-shim [--kaldir]   write / remove only the Windows python3 shim

The installers kur.ps1 / kur.sh run this through plugins/kota-temposu/kur_ortak.py: every install prints the report and writes the
python3 shim (Windows); `kur.ps1 -Araclar` / `kur.sh --araclar` is the consent flag that installs. Details: docs/arac-gereksinimleri.md.

Windows `python3`: the machine has `py` (the launcher) and `python.exe`, but `python3` is only the Microsoft Store stub (WindowsApps),
which opens the Store instead of running. The shim ~/.local/bin/python3.cmd (cmd, pwsh) and ~/.local/bin/python3 (Git Bash) run
`py -3` (or the interpreter found at install time) and are written only when no real python3 is on PATH.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # python -I drops the script dir
from gizli_yukle import gizli  # noqa: E402  (one hidden-launch helper)

WINDOWS = os.name == "nt"
ISARET = "python3-shim"   # marker of the shim files we write; we never touch a python3 file without it
TESSDATA_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/%s.traineddata"
ZORUNLU, ONERILEN, ISTEGE_BAGLI = "gerekli", "onerilen", "istege bagli"


class Arac:
    """One tool. komutlar: any one of these on PATH counts as present. winget/apt/brew: package ids; pip: a Python package (the
    name is imported, never run); elle: the manual hint, used where no package is listed or the OS is in elle_os (a system setup
    that this check never installs by itself)."""

    def __init__(self, ad, komutlar, onem, winget=None, apt=None, brew=None, pip=None, elle=None, kullanan="", hepsi=False, elle_os=()):
        self.ad, self.komutlar, self.onem, self.hepsi, self.elle_os = ad, komutlar, onem, hepsi, elle_os   # hepsi: every command must be present
        self.winget, self.apt, self.brew, self.pip, self.elle, self.kullanan = winget, apt, brew, pip, elle, kullanan


# One authority. docs/arac-gereksinimleri.md carries the same table with the file:line of each user.
ARACLAR = [
    Arac("python3", ["python3"], ZORUNLU, winget="Python.Python.3.13", apt="python3", brew="python",
         kullanan="kur.sh, statusline.sh, tools/*.py, docs"),
    Arac("git", ["git"], ZORUNLU, winget="Git.Git", apt="git", brew="git", kullanan="every script and agent"),
    Arac("gh", ["gh"], ZORUNLU, winget="GitHub.cli", apt="gh", brew="gh", kullanan="tools/gh_board.py, tools/gh_sinir.py, son_pencere.py"),
    Arac("ssh", ["ssh"], ZORUNLU, winget="Microsoft.OpenSSH.Beta", apt="openssh-client", brew="openssh",
         kullanan="remote hosts and lab/build VMs, git over ssh"),
    Arac("jq", ["jq"], ZORUNLU, winget="jqlang.jq", apt="jq", brew="jq", kullanan="kur.sh, statusline.sh, sh scripts and tests"),
    Arac("curl", ["curl"], ZORUNLU, winget="cURL.cURL", apt="curl", brew="curl", kullanan="tools/kasa.py, docs"),
    Arac("node/npx", ["node"], ONERILEN, winget="OpenJS.NodeJS.LTS", apt="nodejs npm", brew="node", kullanan="docs/browser-mcp.md (npx MCP servers)"),
    Arac("pdftoppm/pdftotext (poppler)", ["pdftoppm", "pdftotext"], ONERILEN, winget="oschwartz10612.Poppler", apt="poppler-utils",
         brew="poppler", hepsi=True, kullanan="Claude Code Read tool (PDF pages need pdftoppm)"),
    Arac("pypdf (Python)", [], ONERILEN, pip="pypdf", apt="python3-pypdf", kullanan="agents reading PDF text"),
    Arac("tesseract (OCR)", ["tesseract"], ONERILEN, winget="UB-Mannheim.TesseractOCR", apt="tesseract-ocr", brew="tesseract",
         kullanan="agents reading scanned PDFs / images"),
    Arac("shellcheck", ["shellcheck"], ONERILEN, winget="koalaman.shellcheck", apt="shellcheck", brew="shellcheck",
         kullanan="sh script reviews, actionlint run: steps"),
    Arac("actionlint", ["actionlint"], ONERILEN, winget="rhysd.actionlint", apt="actionlint", brew="actionlint",
         kullanan=".github/workflows/*.yml reviews"),
    Arac("openssl", ["openssl"], ISTEGE_BAGLI, winget="ShiningLight.OpenSSL.Light", apt="openssl", brew="openssl",
         kullanan="certificate / key checks (Git for Windows ships one)"),
    Arac("dig/nslookup", ["dig", "nslookup"], ISTEGE_BAGLI, apt="dnsutils", brew="bind",
         elle="Windows ships nslookup; dig is optional (winget ISC.Bind, a large package, is not installed automatically)",
         kullanan="DNS usage evidence (live dig / nslookup)"),
    Arac("rsync", ["rsync"], ISTEGE_BAGLI, apt="rsync", brew="rsync",
         elle="no winget package; on Windows use scp or robocopy", kullanan="server file sync from Linux hosts"),
    Arac("docker", ["docker"], ISTEGE_BAGLI, winget="Docker.DockerDesktop", apt="docker.io", brew="docker",
         elle="Docker Desktop / the daemon is a system setup, never installed by this check", elle_os=("windows", "linux", "macos"), kullanan="lab services and containers"),
    Arac("govc", ["govc"], ISTEGE_BAGLI,
         elle="no package: download the release of github.com/vmware/govmomi (govc) and put it on PATH", kullanan="vSphere / vCenter lab VM work"),
]


def os_adi():
    return "windows" if WINDOWS else ("macos" if sys.platform == "darwin" else "linux")


# ---------------------------------------------------------------- PATH / presence

def yol_yenile():
    """Windows: a process started before an install does not see the new PATH entries; add the registry PATH (machine, user)
    to this process so the check sees what winget just installed."""
    if not WINDOWS:
        return
    try:
        import winreg
    except ImportError:
        return
    mevcut = os.environ.get("PATH", "").split(os.pathsep)
    kucuk = {os.path.normcase(p.rstrip("\\")) for p in mevcut}
    eklenecek = []
    for kok, alt in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                     (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(kok, alt) as k:
                deger = os.path.expandvars(winreg.QueryValueEx(k, "Path")[0])
        except OSError:
            continue
        for p in deger.split(os.pathsep):
            if p and os.path.normcase(p.rstrip("\\")) not in kucuk:
                kucuk.add(os.path.normcase(p.rstrip("\\")))
                eklenecek.append(p)
    if eklenecek:
        os.environ["PATH"] = os.pathsep.join(mevcut + eklenecek)


YETKI_KODLARI = (2147943623, 1223, 740)   # winget 0x800704C7 (UAC cancelled), ERROR_CANCELLED, ERROR_ELEVATION_REQUIRED

# Windows installers that do not put themselves on PATH: the directory is added to the user PATH after the install
WINDOWS_DIZINLERI = {"tesseract": [r"%ProgramFiles%\Tesseract-OCR", r"%LOCALAPPDATA%\Programs\Tesseract-OCR"]}


def kullanici_path_ekle(dizin, basa=False):
    """Windows: put dizin into the user PATH (registry, REG_EXPAND_SZ; no setx, it cuts at 1024 characters) and into this process.
    Returns True when the user PATH changed."""
    if not WINDOWS:
        return False
    import winreg
    dizin = str(dizin)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
        try:
            eski, tur = winreg.QueryValueEx(k, "Path")
        except OSError:
            eski, tur = "", winreg.REG_EXPAND_SZ
        parcalar = [p for p in eski.split(";") if p]
        norm = lambda p: os.path.normcase(os.path.expandvars(p).rstrip("\\"))
        degisti = False
        if norm(dizin) not in [norm(p) for p in parcalar]:
            parcalar = [dizin] + parcalar if basa else parcalar + [dizin]
            winreg.SetValueEx(k, "Path", 0, tur if tur in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else winreg.REG_EXPAND_SZ, ";".join(parcalar))
            degisti = True
    if dizin not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = (dizin + os.pathsep + os.environ.get("PATH", "")) if basa else (os.environ.get("PATH", "") + os.pathsep + dizin)
    if degisti:
        try:
            import ctypes   # tell running shells the environment changed (new terminals pick it up regardless)
            ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 3000, ctypes.byref(ctypes.c_ulong()))
        except (OSError, AttributeError):
            pass
    return degisti


def bin_dizin():
    return Path.home() / ".local" / "bin"


def gercek_python3():
    """The first real python3 on PATH, or None. Windows: the WindowsApps Store stub and our own shim do not count; the candidate
    must run. POSIX: python3 from PATH."""
    if not WINDOWS:
        p = shutil.which("python3")
        return p if p and _python_calisir([p]) else None
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or "windowsapps" in d.lower():
            continue
        for ad in ("python3.exe", "python3.cmd", "python3.bat", "python3"):
            f = Path(d) / ad
            if f.is_file() and ISARET not in _oku(f) and _python_calisir([str(f)]):
                return str(f)
    return None


def _oku(f):
    try:
        if f.stat().st_size > 4096:   # a real interpreter, not a shim
            return ""
        return f.read_text(errors="replace")
    except OSError:
        return ""


def _python_calisir(komut):
    try:
        r = gizli.run(komut + ["-c", "import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)"],
                           capture_output=True, timeout=30, cwd=tempfile.gettempdir())
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def python_modulu_var(python, modul):
    """Import test with the interpreter the installer runs with, user site included (so no -I)."""
    kod = ("import sys; sys.path[:] = [p for p in sys.path if p]; import importlib.util as u; "
           "sys.exit(0 if u.find_spec(%r) else 1)" % modul)
    try:
        return gizli.run([python, "-c", kod], capture_output=True, timeout=60, cwd=tempfile.gettempdir()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def var_mi(t, python):
    if t.pip:
        return python_modulu_var(python, t.pip)
    if t.ad == "python3":
        if gercek_python3():
            return True
        # the shim counts as present once it resolves; it is only written when no real python3 exists
        p = shutil.which("python3")
        return bool(p and ISARET in _oku(Path(p)))
    return (all if t.hepsi else any)(shutil.which(c) for c in t.komutlar)


def tesseract_dilleri():
    """(tessdata dir or None, set of installed language codes) from `tesseract --list-langs`."""
    exe = shutil.which("tesseract")
    if not exe:
        return None, set()
    try:
        r = gizli.run([exe, "--list-langs"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None, set()
    satirlar = (r.stdout + "\n" + r.stderr).splitlines()
    dizin = None
    diller = set()
    for s in satirlar:
        s = s.strip()
        if s.startswith("List of available languages"):
            if '"' in s:
                dizin = s.split('"')[1]
            continue
        if s and " " not in s and "/" not in s and "\\" not in s:
            diller.add(s)
    return dizin, diller


# ---------------------------------------------------------------- install

def paket_yoneticisi(os_ad):
    if os_ad == "windows":
        return "winget" if shutil.which("winget") else None
    if os_ad == "macos":
        return "brew" if shutil.which("brew") else None
    return "apt" if shutil.which("apt-get") else None


def kur_komutlari(t, os_ad, python, pm=None):
    """The command lines that install tool t on this OS (a list of argv lists), or [] when only a manual step exists."""
    pm = pm if pm is not None else paket_yoneticisi(os_ad)
    if os_ad in t.elle_os:
        return []
    if t.pip and not (os_ad == "linux" and pm == "apt" and t.apt):
        return [[python, "-m", "pip", "install", "--user", t.pip]]
    if pm == "winget" and t.winget:
        return [["winget", "install", "--id", t.winget, "-e", "--silent",
                 "--accept-package-agreements", "--accept-source-agreements"]]
    if pm == "apt" and t.apt:
        sudo = [] if (hasattr(os, "geteuid") and os.geteuid() == 0) else ["sudo"]
        return [sudo + ["apt-get", "install", "-y"] + t.apt.split()]
    if pm == "brew" and t.brew:
        return [["brew", "install", t.brew]]
    return []


def satir_metni(argv):
    return " ".join(('"%s"' % a if " " in a else a) for a in argv)


def elle_ipucu(t, os_ad):
    if t.elle:
        return t.elle
    if os_ad == "windows" and not t.winget:
        return "no winget package on Windows"
    return "install it by hand (see docs/arac-gereksinimleri.md)"


def calistir(argv, zaman_asimi=1800):
    """Run an installer command; returns (rc, last output lines). Never raises."""
    try:
        r = gizli.run(argv, capture_output=True, text=True, errors="replace", timeout=zaman_asimi, stdin=subprocess.DEVNULL)
        cikti = (r.stdout + "\n" + r.stderr).strip().splitlines()
        return r.returncode, cikti[-4:]
    except FileNotFoundError:
        return 127, ["komut bulunamadi: %s" % argv[0]]
    except (OSError, subprocess.SubprocessError) as e:
        return 1, [str(e)]


def tur_verisi_indir(dil, hedef_dizin):
    """Download <dil>.traineddata (tessdata_fast) into hedef_dizin. Returns an error text or None."""
    try:
        with urllib.request.urlopen(TESSDATA_URL % dil, timeout=120) as r:
            veri = r.read()
        if len(veri) < 100_000:
            return "indirilen dosya cok kucuk (%d bayt)" % len(veri)
        hedef_dizin.mkdir(parents=True, exist_ok=True)
        (hedef_dizin / ("%s.traineddata" % dil)).write_bytes(veri)
        return None
    except (OSError, ValueError) as e:
        return str(e)


def tesseract_turkce(os_ad, whatif, yaz=print):
    """tesseract with Turkish data. apt installs tesseract-ocr-tur; elsewhere tur.traineddata goes into the tessdata dir, or (when
    that is not writable, e.g. Program Files) into ~/.local/share/tessdata with eng/osd copied and TESSDATA_PREFIX set for the user.
    Returns True when Turkish is available (or would be)."""
    dizin, diller = tesseract_dilleri()
    if dizin is None:
        return False
    if "tur" in diller:
        return True
    if whatif:
        yaz("  [-WhatIf] tesseract: tur dil verisi eklenecek")
        return True
    if os_ad == "linux" and paket_yoneticisi(os_ad) == "apt":
        rc, son = calistir(kur_komutlari(Arac("t", [], ONERILEN, apt="tesseract-ocr-tur"), os_ad, sys.executable)[0])
        yaz("  tesseract-ocr-tur: %s" % ("kuruldu" if rc == 0 else "HATA " + " | ".join(son)))
        return rc == 0
    hata = tur_verisi_indir("tur", Path(dizin))
    if hata is None:
        yaz("  tesseract: tur.traineddata yazildi: %s" % dizin)
        return True
    # not writable: a per-user tessdata copy and TESSDATA_PREFIX
    kullanici = Path.home() / ".local" / "share" / "tessdata"
    for dil in ("eng", "osd"):
        kaynak = Path(dizin) / ("%s.traineddata" % dil)
        if kaynak.is_file() and not (kullanici / kaynak.name).exists():
            kullanici.mkdir(parents=True, exist_ok=True)
            shutil.copy2(kaynak, kullanici / kaynak.name)
    hata2 = tur_verisi_indir("tur", kullanici)
    if hata2 is not None:
        yaz("  tesseract: tur dil verisi yazilamadi (%s; %s)" % (hata, hata2))
        return False
    os.environ["TESSDATA_PREFIX"] = str(kullanici)
    if WINDOWS:
        rc, _ = calistir(["setx", "TESSDATA_PREFIX", str(kullanici)], 60)
        yaz("  tesseract: tur verisi %s, TESSDATA_PREFIX kullanici ortamina %s" % (kullanici, "yazildi" if rc == 0 else "YAZILAMADI"))
    else:
        yaz("  tesseract: tur verisi %s; kabugunuzda `export TESSDATA_PREFIX=%s` ekleyin" % (kullanici, kullanici))
    return True


# ---------------------------------------------------------------- python3 shim (Windows)

def shim_sh(py_yolu):
    q = "'" + Path(py_yolu).as_posix().replace("'", "'\\''") + "'"
    return ("#!/bin/sh\n# %s (written by tools/arac_kontrol.py; runs `py -3`, else the interpreter found at install time)\n"
            "if command -v py >/dev/null 2>&1; then exec py -3 \"$@\"; fi\nexec %s \"$@\"\n") % (ISARET, q)


def shim_cmd(py_yolu):
    return ("@echo off\r\nrem %s (written by tools/arac_kontrol.py; runs `py -3`, else the interpreter found at install time)\r\n"
            "where py >nul 2>nul\r\nif errorlevel 1 goto direkt\r\npy -3 %%*\r\nexit /b %%errorlevel%%\r\n:direkt\r\n"
            "\"%s\" %%*\r\nexit /b %%errorlevel%%\r\n") % (ISARET, py_yolu)


def python3_shim(python, kaldir=False, whatif=False, hedef_dizin=None, yaz=print):
    """Write (or remove) ~/.local/bin/python3 and python3.cmd. Windows only; written only when no real python3 is on PATH and
    never over a file that is not ours. Returns True when nothing is wrong (a skipped shim is not an error)."""
    if not WINDOWS and hedef_dizin is None:
        return True
    tag = "[-WhatIf] " if whatif else ""
    d = Path(hedef_dizin) if hedef_dizin else bin_dizin()
    dosyalar = [d / "python3", d / "python3.cmd"]
    if kaldir:
        for f in dosyalar:
            if f.is_file() and ISARET in _oku(f):
                yaz("%spython3-shim: sil: %s" % (tag, f))
                if not whatif:
                    f.unlink()
        return True
    gercek = gercek_python3()
    if gercek and not any(f.is_file() and ISARET in _oku(f) for f in dosyalar):
        yaz("%spython3-shim: gerekmiyor, gercek python3 var: %s" % (tag, gercek))
        return True
    if not python or not Path(python).is_file():
        yaz("python3-shim: UYARI: yorumlayici bulunamadi (%s); shim yazilmadi" % python)
        return False
    icerik = {dosyalar[0]: shim_sh(python), dosyalar[1]: shim_cmd(python)}
    ok = True
    for f, metin in icerik.items():
        if f.exists() and ISARET not in _oku(f):
            yaz("python3-shim: UYARI: %s baska bir dosya, dokunulmadi" % f)
            ok = False
            continue
        if f.is_file() and f.read_bytes() == metin.encode("utf-8"):
            yaz("%spython3-shim: ayni: %s" % (tag, f))
            continue
        yaz("%spython3-shim: yaz: %s" % (tag, f))
        if not whatif:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(metin.encode("utf-8"))   # bytes: the sh shim keeps LF, the cmd shim CRLF
    return ok


def python3_shim_yolda_mi():
    """After writing: does `python3` resolve to our shim in this process's PATH? Returns (bool, resolved path or None)."""
    p = shutil.which("python3")
    return bool(p and ISARET in _oku(Path(p))), p


# ---------------------------------------------------------------- report / run

def kontrol(python, os_ad=None):
    """[(Arac, present bool)] in table order."""
    yol_yenile()
    return [(t, var_mi(t, python)) for t in ARACLAR]


def rapor(sonuc, os_ad, yaz=print, tur_ok=None):
    pm = paket_yoneticisi(os_ad)
    yaz("Arac kontrolu (%s, paket yoneticisi: %s):" % (os_ad, pm or "YOK"))
    for t, var in sonuc:
        if var:
            yaz("  tamam  %-30s %s" % (t.ad, t.onem))
            continue
        komut = kur_komutlari(t, os_ad, sys.executable, pm)
        ipucu = satir_metni(komut[0]) if komut else elle_ipucu(t, os_ad)
        yaz("  EKSIK  %-30s %-12s -> %s" % (t.ad, t.onem, ipucu))
    if tur_ok is False:
        yaz("  EKSIK  %-30s %-12s -> tur.traineddata (`kur ... --araclar` adds it)" % ("tesseract tur dil verisi", ONERILEN))


def calis(python, kur=False, whatif=False, yaz=print):
    """The check step. Report always; with kur=True install what is missing. Returns 0 (all well / only optional gaps), 1 (an
    install failed or a required tool is still missing after kur=True)."""
    os_ad = os_adi()
    sonuc = kontrol(python, os_ad)
    tur_ok = None
    tess = next((v for t, v in sonuc if t.ad.startswith("tesseract")), False)
    if tess:
        tur_ok = "tur" in tesseract_dilleri()[1]
    rapor(sonuc, os_ad, yaz, tur_ok)
    eksik = [t for t, var in sonuc if not var]
    if not kur:
        if eksik or tur_ok is False:
            yaz("Eksikleri kurmak icin: kur.ps1 -Araclar (Windows) / kur.sh --araclar (Linux, macOS).")
        return 0
    pm = paket_yoneticisi(os_ad)
    hata = False
    guncelle_yapildi = False
    if WINDOWS and not whatif and str(bin_dizin()) not in os.environ.get("PATH", "").split(os.pathsep):
        kullanici_path_ekle(bin_dizin(), basa=True)   # the shims (python3, gh) live there; consent flag only
        yaz("  %s kullanici PATH'inin basina eklendi" % bin_dizin())
    for t in eksik:
        komutlar = kur_komutlari(t, os_ad, python, pm)
        if not komutlar:
            yaz("  atlandi: %s (%s)" % (t.ad, elle_ipucu(t, os_ad)))
            hata = hata or t.onem == ZORUNLU
            continue
        if t.ad == "python3" and WINDOWS:
            continue   # handled by the shim step: a real Python install is the heavy path, the shim is the default
        for argv in komutlar:
            yaz("%s  kur: %s" % ("[-WhatIf]" if whatif else "", satir_metni(argv)))
            if whatif:
                continue
            if pm == "apt" and not guncelle_yapildi and "apt-get" in argv:
                calistir((["sudo"] if argv[0] == "sudo" else []) + ["apt-get", "update", "-qq"])
                guncelle_yapildi = True
            rc, son = calistir(argv)
            if rc != 0:
                yaz("  HATA (%s, kod %s): %s" % (t.ad, rc, " | ".join(son)))
                if WINDOWS and rc in YETKI_KODLARI:
                    yaz("  -> %s installer needs administrator rights (UAC was refused or could not be shown): run `kur.ps1 -Araclar` "
                        "(or this command) once from an elevated PowerShell." % t.ad)
                hata = True
    if whatif:
        return 0
    yol_yenile()
    if WINDOWS:
        for t in eksik:   # an installer that left its directory off PATH
            ad = t.komutlar[0] if t.komutlar else ""
            if ad and not shutil.which(ad):
                for d in WINDOWS_DIZINLERI.get(ad, []):
                    d = os.path.expandvars(d)
                    if (Path(d) / (ad + ".exe")).is_file():
                        kullanici_path_ekle(d)
                        yaz("  %s kullanici PATH'ine eklendi: %s" % (ad, d))
                        break
    if tess or any(t.ad.startswith("tesseract") for t in eksik):
        if not tesseract_turkce(os_ad, whatif, yaz):
            yaz("  UYARI: tesseract Turkce dil verisi eklenemedi")
    yaz("Kurulum sonrasi kontrol:")
    sonuc2 = kontrol(python, os_ad)
    tess2 = next((v for t, v in sonuc2 if t.ad.startswith("tesseract")), False)
    rapor(sonuc2, os_ad, yaz, ("tur" in tesseract_dilleri()[1]) if tess2 else None)
    kalan = [t for t, var in sonuc2 if not var and t.onem == ZORUNLU]
    if eksik and any(not var for _, var in sonuc2):
        yaz("Not: yeni kurulan araclar yeni acilan terminal/oturumlarda PATH'te gorunur.")
    return 1 if (hata or kalan) else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="claude-shared command-line tool check")
    ap.add_argument("--kur", action="store_true", help="install the missing tools (winget / apt-get / brew / pip --user)")
    ap.add_argument("--whatif", action="store_true", help="with --kur: print the commands, run nothing")
    ap.add_argument("--python", default=sys.executable, help="the interpreter used for the Python package check and pip --user")
    ap.add_argument("--python3-shim", dest="shim", action="store_true", help="only write the Windows python3 shim")
    ap.add_argument("--kaldir", action="store_true", help="with --python3-shim: remove the shim")
    a = ap.parse_args(argv)
    if a.shim:
        return 0 if python3_shim(a.python, a.kaldir, a.whatif) else 1
    if a.kur:
        python3_shim(a.python, False, a.whatif)
    return calis(a.python, a.kur, a.whatif)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(main())

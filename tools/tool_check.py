#!/usr/bin/env python3
"""Command-line tool check for team machines (the single list of the tools agents and claude-shared scripts call).

  python tools/tool_check.py            report only: which tools are present / missing, and the one-line install command per OS
  python tools/tool_check.py --install      install the missing ones (winget on Windows, apt-get on Linux, brew on macOS; pip --user for
                                          Python packages); only this flag installs anything
  python tools/tool_check.py --whatif   with --install: print what would run, run nothing
  python tools/tool_check.py --python3-shim [--remove]   write / remove only the Windows python3 shim

The installers install.ps1 / install.sh run this through plugins/quota-pace/install_common.py: every install prints the report and writes the
python3 shim (Windows); `install.ps1 -Tools` / `install.sh --tools` is the consent flag that installs. Details: docs/tool-requirements.md.

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
from hidden_loader import hidden  # noqa: E402  (one hidden-launch helper)

WINDOWS = os.name == "nt"
MARKER = "python3-shim"   # marker of the shim files we write; we never touch a python3 file without it
TESSDATA_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/%s.traineddata"
REQUIRED, RECOMMENDED, OPTIONAL_LINKED = "required", "recommended", "istege bagli"


class Tool:
    """One tool. komutlar: any one of these on PATH counts as present. winget/apt/brew: package ids; pip: a Python package (the
    name is imported, never run); elle: the manual hint, used where no package is listed or the OS is in manual_os (a system setup
    that this check never installs by itself)."""

    def __init__(self, name, commands, importance, winget=None, apt=None, brew=None, pip=None, manual=None, using="", all_=False, manual_os=()):
        self.name, self.commands, self.importance, self.all, self.manual_os = name, commands, importance, all_, manual_os   # hepsi: every command must be present
        self.winget, self.apt, self.brew, self.pip, self.manual, self.using = winget, apt, brew, pip, manual, using


# One authority. docs/tool-requirements.md carries the same table with the file:line of each user.
TOOLS = [
    Tool("python3", ["python3"], REQUIRED, winget="Python.Python.3.13", apt="python3", brew="python",
         using="install.sh, statusline.sh, tools/*.py, docs"),
    Tool("git", ["git"], REQUIRED, winget="Git.Git", apt="git", brew="git", using="every script and agent"),
    Tool("gh", ["gh"], REQUIRED, winget="GitHub.cli", apt="gh", brew="gh", using="tools/gh_board.py, tools/gh_limit.py, last_window.py"),
    Tool("ssh", ["ssh"], REQUIRED, winget="Microsoft.OpenSSH.Beta", apt="openssh-client", brew="openssh",
         using="remote hosts and lab/build VMs, git over ssh"),
    Tool("jq", ["jq"], REQUIRED, winget="jqlang.jq", apt="jq", brew="jq", using="install.sh, statusline.sh, sh scripts and tests"),
    Tool("curl", ["curl"], REQUIRED, winget="cURL.cURL", apt="curl", brew="curl", using="tools/vault_cli.py, docs"),
    Tool("node/npx", ["node"], RECOMMENDED, winget="OpenJS.NodeJS.LTS", apt="nodejs npm", brew="node", using="docs/browser-mcp.md (npx MCP servers)"),
    Tool("pdftoppm/pdftotext (poppler)", ["pdftoppm", "pdftotext"], RECOMMENDED, winget="oschwartz10612.Poppler", apt="poppler-utils",
         brew="poppler", all_=True, using="Claude Code Read tool (PDF pages need pdftoppm)"),
    Tool("pypdf (Python)", [], RECOMMENDED, pip="pypdf", apt="python3-pypdf", using="agents reading PDF text"),
    Tool("tesseract (OCR)", ["tesseract"], RECOMMENDED, winget="UB-Mannheim.TesseractOCR", apt="tesseract-ocr", brew="tesseract",
         using="agents reading scanned PDFs / images"),
    Tool("shellcheck", ["shellcheck"], RECOMMENDED, winget="koalaman.shellcheck", apt="shellcheck", brew="shellcheck",
         using="sh script reviews, actionlint run: steps"),
    Tool("actionlint", ["actionlint"], RECOMMENDED, winget="rhysd.actionlint", apt="actionlint", brew="actionlint",
         using=".github/workflows/*.yml reviews"),
    Tool("openssl", ["openssl"], OPTIONAL_LINKED, winget="ShiningLight.OpenSSL.Light", apt="openssl", brew="openssl",
         using="certificate / key checks (Git for Windows ships one)"),
    Tool("dig/nslookup", ["dig", "nslookup"], OPTIONAL_LINKED, apt="dnsutils", brew="bind",
         manual="Windows ships nslookup; dig is optional (winget ISC.Bind, a large package, is not installed automatically)",
         using="DNS usage evidence (live dig / nslookup)"),
    Tool("rsync", ["rsync"], OPTIONAL_LINKED, apt="rsync", brew="rsync",
         manual="no winget package; on Windows use scp or robocopy", using="server file sync from Linux hosts"),
    Tool("docker", ["docker"], OPTIONAL_LINKED, winget="Docker.DockerDesktop", apt="docker.io", brew="docker",
         manual="Docker Desktop / the daemon is a system setup, never installed by this check", manual_os=("windows", "linux", "macos"), using="lab services and containers"),
    Tool("govc", ["govc"], OPTIONAL_LINKED,
         manual="no package: download the release of github.com/vmware/govmomi (govc) and put it on PATH", using="vSphere / vCenter lab VM work"),
]


def os_name():
    return "windows" if WINDOWS else ("macos" if sys.platform == "darwin" else "linux")


# ---------------------------------------------------------------- PATH / presence

def refresh_path():
    """Windows: a process started before an install does not see the new PATH entries; add the registry PATH (machine, user)
    to this process so the check sees what winget just installed."""
    if not WINDOWS:
        return
    try:
        import winreg
    except ImportError:
        return
    existing = os.environ.get("PATH", "").split(os.pathsep)
    small = {os.path.normcase(p.rstrip("\\")) for p in existing}
    to_add = []
    for kok, alt in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                     (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(kok, alt) as k:
                value = os.path.expandvars(winreg.QueryValueEx(k, "Path")[0])
        except OSError:
            continue
        for p in value.split(os.pathsep):
            if p and os.path.normcase(p.rstrip("\\")) not in small:
                small.add(os.path.normcase(p.rstrip("\\")))
                to_add.append(p)
    if to_add:
        os.environ["PATH"] = os.pathsep.join(existing + to_add)


PERMISSION_CODES = (2147943623, 1223, 740)   # winget 0x800704C7 (UAC cancelled), ERROR_CANCELLED, ERROR_ELEVATION_REQUIRED

# Windows installers that do not put themselves on PATH: the directory is added to the user PATH after the install
WINDOWS_DIRS = {"tesseract": [r"%ProgramFiles%\Tesseract-OCR", r"%LOCALAPPDATA%\Programs\Tesseract-OCR"]}


def add_user_path(dir_, to_start=False):
    """Windows: put dizin into the user PATH (registry, REG_EXPAND_SZ; no setx, it cuts at 1024 characters) and into this process.
    Returns True when the user PATH changed."""
    if not WINDOWS:
        return False
    import winreg
    dir_ = str(dir_)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
        try:
            old, turn = winreg.QueryValueEx(k, "Path")
        except OSError:
            old, turn = "", winreg.REG_EXPAND_SZ
        parts = [p for p in old.split(";") if p]
        norm = lambda p: os.path.normcase(os.path.expandvars(p).rstrip("\\"))
        changed = False
        if norm(dir_) not in [norm(p) for p in parts]:
            parts = [dir_] + parts if to_start else parts + [dir_]
            winreg.SetValueEx(k, "Path", 0, turn if turn in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else winreg.REG_EXPAND_SZ, ";".join(parts))
            changed = True
    if dir_ not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = (dir_ + os.pathsep + os.environ.get("PATH", "")) if to_start else (os.environ.get("PATH", "") + os.pathsep + dir_)
    if changed:
        try:
            import ctypes   # tell running shells the environment changed (new terminals pick it up regardless)
            ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 3000, ctypes.byref(ctypes.c_ulong()))
        except (OSError, AttributeError):
            pass
    return changed


def bin_dir():
    return Path.home() / ".local" / "bin"


def real_python3():
    """The first real python3 on PATH, or None. Windows: the WindowsApps Store stub and our own shim do not count; the candidate
    must run. POSIX: python3 from PATH."""
    if not WINDOWS:
        p = shutil.which("python3")
        return p if p and _python_runs([p]) else None
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or "windowsapps" in d.lower():
            continue
        for name in ("python3.exe", "python3.cmd", "python3.bat", "python3"):
            f = Path(d) / name
            if f.is_file() and MARKER not in _read(f) and _python_runs([str(f)]):
                return str(f)
    return None


def _read(f):
    try:
        if f.stat().st_size > 4096:   # a real interpreter, not a shim
            return ""
        return f.read_text(errors="replace")
    except OSError:
        return ""


def _python_runs(command):
    try:
        r = hidden.run(command + ["-c", "import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)"],
                           capture_output=True, timeout=30, cwd=tempfile.gettempdir())
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def python_module_present(python, module):
    """Import test with the interpreter the installer runs with, user site included (so no -I)."""
    code = ("import sys; sys.path[:] = [p for p in sys.path if p]; import importlib.util as u; "
           "sys.exit(0 if u.find_spec(%r) else 1)" % module)
    try:
        return hidden.run([python, "-c", code], capture_output=True, timeout=60, cwd=tempfile.gettempdir()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def var_mi(t, python):
    if t.pip:
        return python_module_present(python, t.pip)
    if t.name == "python3":
        if real_python3():
            return True
        # the shim counts as present once it resolves; it is only written when no real python3 exists
        p = shutil.which("python3")
        return bool(p and MARKER in _read(Path(p)))
    return (all if t.all else any)(shutil.which(c) for c in t.commands)


def tesseract_languages():
    """(tessdata dir or None, set of installed language codes) from `tesseract --list-langs`."""
    exe = shutil.which("tesseract")
    if not exe:
        return None, set()
    try:
        r = hidden.run([exe, "--list-langs"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None, set()
    lines = (r.stdout + "\n" + r.stderr).splitlines()
    dir_ = None
    languages = set()
    for s in lines:
        s = s.strip()
        if s.startswith("List of available languages"):
            if '"' in s:
                dir_ = s.split('"')[1]
            continue
        if s and " " not in s and "/" not in s and "\\" not in s:
            languages.add(s)
    return dir_, languages


# ---------------------------------------------------------------- install

def package_manager(os_ad):
    if os_ad == "windows":
        return "winget" if shutil.which("winget") else None
    if os_ad == "macos":
        return "brew" if shutil.which("brew") else None
    return "apt" if shutil.which("apt-get") else None


def install_commands(t, os_ad, python, pm=None):
    """The command lines that install tool t on this OS (a list of argv lists), or [] when only a manual step exists."""
    pm = pm if pm is not None else package_manager(os_ad)
    if os_ad in t.manual_os:
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


def line_text(argv):
    return " ".join(('"%s"' % a if " " in a else a) for a in argv)


def manual_hint(t, os_ad):
    if t.manual:
        return t.manual
    if os_ad == "windows" and not t.winget:
        return "no winget package on Windows"
    return "install it by hand (see docs/tool-requirements.md)"


def run_cmd(argv, time_overrun=1800):
    """Run an installer command; returns (rc, last output lines). Never raises."""
    try:
        r = hidden.run(argv, capture_output=True, text=True, errors="replace", timeout=time_overrun, stdin=subprocess.DEVNULL)
        output = (r.stdout + "\n" + r.stderr).strip().splitlines()
        return r.returncode, output[-4:]
    except FileNotFoundError:
        return 127, ["komut bulunamadi: %s" % argv[0]]
    except (OSError, subprocess.SubprocessError) as e:
        return 1, [str(e)]


def download_turn_data(language, target_dir):
    """Download <dil>.traineddata (tessdata_fast) into target_dir. Returns an error text or None."""
    try:
        with urllib.request.urlopen(TESSDATA_URL % language, timeout=120) as r:
            data = r.read()
        if len(data) < 100_000:
            return "indirilen dosya cok kucuk (%d bayt)" % len(data)
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / ("%s.traineddata" % language)).write_bytes(data)
        return None
    except (OSError, ValueError) as e:
        return str(e)


def tesseract_turkish(os_ad, whatif, write=print):
    """tesseract with Turkish data. apt installs tesseract-ocr-tur; elsewhere tur.traineddata goes into the tessdata dir, or (when
    that is not writable, e.g. Program Files) into ~/.local/share/tessdata with eng/osd copied and TESSDATA_PREFIX set for the user.
    Returns True when Turkish is available (or would be)."""
    dir_, languages = tesseract_languages()
    if dir_ is None:
        return False
    if "turn" in languages:
        return True
    if whatif:
        write("  [-WhatIf] tesseract: tur dil verisi eklenecek")
        return True
    if os_ad == "linux" and package_manager(os_ad) == "apt":
        rc, last = run_cmd(install_commands(Tool("t", [], RECOMMENDED, apt="tesseract-ocr-tur"), os_ad, sys.executable)[0])
        write("  tesseract-ocr-tur: %s" % ("kuruldu" if rc == 0 else "HATA " + " | ".join(last)))
        return rc == 0
    error = download_turn_data("turn", Path(dir_))
    if error is None:
        write("  tesseract: tur.traineddata yazildi: %s" % dir_)
        return True
    # not writable: a per-user tessdata copy and TESSDATA_PREFIX
    user = Path.home() / ".local" / "share" / "tessdata"
    for language in ("eng", "osd"):
        source = Path(dir_) / ("%s.traineddata" % language)
        if source.is_file() and not (user / source.name).exists():
            user.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, user / source.name)
    hata2 = download_turn_data("turn", user)
    if hata2 is not None:
        write("  tesseract: tur dil verisi yazilamadi (%s; %s)" % (error, hata2))
        return False
    os.environ["TESSDATA_PREFIX"] = str(user)
    if WINDOWS:
        rc, _ = run_cmd(["setx", "TESSDATA_PREFIX", str(user)], 60)
        write("  tesseract: tur verisi %s, TESSDATA_PREFIX kullanici ortamina %s" % (user, "written" if rc == 0 else "YAZILAMADI"))
    else:
        write("  tesseract: tur verisi %s; kabugunuzda `export TESSDATA_PREFIX=%s` ekleyin" % (user, user))
    return True


# ---------------------------------------------------------------- python3 shim (Windows)

def shim_sh(py_path):
    q = "'" + Path(py_path).as_posix().replace("'", "'\\''") + "'"
    return ("#!/bin/sh\n# %s (written by tools/tool_check.py; runs `py -3`, else the interpreter found at install time)\n"
            "if command -v py >/dev/null 2>&1; then exec py -3 \"$@\"; fi\nexec %s \"$@\"\n") % (MARKER, q)


def shim_cmd(py_path):
    return ("@echo off\r\nrem %s (written by tools/tool_check.py; runs `py -3`, else the interpreter found at install time)\r\n"
            "where py >nul 2>nul\r\nif errorlevel 1 goto direkt\r\npy -3 %%*\r\nexit /b %%errorlevel%%\r\n:direkt\r\n"
            "\"%s\" %%*\r\nexit /b %%errorlevel%%\r\n") % (MARKER, py_path)


def python3_shim(python, remove=False, whatif=False, target_dir=None, write=print):
    """Write (or remove) ~/.local/bin/python3 and python3.cmd. Windows only; written only when no real python3 is on PATH and
    never over a file that is not ours. Returns True when nothing is wrong (a skipped shim is not an error)."""
    if not WINDOWS and target_dir is None:
        return True
    tag = "[-WhatIf] " if whatif else ""
    d = Path(target_dir) if target_dir else bin_dir()
    files = [d / "python3", d / "python3.cmd"]
    if remove:
        for f in files:
            if f.is_file() and MARKER in _read(f):
                write("%spython3-shim: sil: %s" % (tag, f))
                if not whatif:
                    f.unlink()
        return True
    real = real_python3()
    if real and not any(f.is_file() and MARKER in _read(f) for f in files):
        write("%spython3-shim: gerekmiyor, gercek python3 var: %s" % (tag, real))
        return True
    if not python or not Path(python).is_file():
        write("python3-shim: UYARI: yorumlayici bulunamadi (%s); shim yazilmadi" % python)
        return False
    content = {files[0]: shim_sh(python), files[1]: shim_cmd(python)}
    ok = True
    for f, text in content.items():
        if f.exists() and MARKER not in _read(f):
            write("python3-shim: UYARI: %s baska bir dosya, dokunulmadi" % f)
            ok = False
            continue
        if f.is_file() and f.read_bytes() == text.encode("utf-8"):
            write("%spython3-shim: ayni: %s" % (tag, f))
            continue
        write("%spython3-shim: yaz: %s" % (tag, f))
        if not whatif:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(text.encode("utf-8"))   # bytes: the sh shim keeps LF, the cmd shim CRLF
    return ok


def python3_shim_yolda_mi():
    """After writing: does `python3` resolve to our shim in this process's PATH? Returns (bool, resolved path or None)."""
    p = shutil.which("python3")
    return bool(p and MARKER in _read(Path(p))), p


# ---------------------------------------------------------------- report / run

def check(python, os_ad=None):
    """[(Arac, present bool)] in table order."""
    refresh_path()
    return [(t, var_mi(t, python)) for t in TOOLS]


def report(result, os_ad, write=print, tur_ok=None):
    pm = package_manager(os_ad)
    write("Arac kontrolu (%s, paket yoneticisi: %s):" % (os_ad, pm or "MISSING"))
    for t, present in result:
        if present:
            write("  tamam  %-30s %s" % (t.name, t.importance))
            continue
        command = install_commands(t, os_ad, sys.executable, pm)
        hint = line_text(command[0]) if command else manual_hint(t, os_ad)
        write("  EKSIK  %-30s %-12s -> %s" % (t.name, t.importance, hint))
    if tur_ok is False:
        write("  EKSIK  %-30s %-12s -> tur.traineddata (`install ... --tools` adds it)" % ("tesseract tur dil verisi", RECOMMENDED))


def execute(python, install=False, whatif=False, write=print):
    """The check step. Report always; with install=True install what is missing. Returns 0 (all well / only optional gaps), 1 (an
    install failed or a required tool is still missing after install=True)."""
    os_ad = os_name()
    result = check(python, os_ad)
    tur_ok = None
    tess = next((v for t, v in result if t.name.startswith("tesseract")), False)
    if tess:
        tur_ok = "turn" in tesseract_languages()[1]
    report(result, os_ad, write, tur_ok)
    missing = [t for t, present in result if not present]
    if not install:
        if missing or tur_ok is False:
            write("Eksikleri kurmak icin: install.ps1 -Tools (Windows) / install.sh --tools (Linux, macOS).")
        return 0
    pm = package_manager(os_ad)
    error = False
    update_done = False
    if WINDOWS and not whatif and str(bin_dir()) not in os.environ.get("PATH", "").split(os.pathsep):
        add_user_path(bin_dir(), to_start=True)   # the shims (python3, gh) live there; consent flag only
        write("  %s kullanici PATH'inin basina eklendi" % bin_dir())
    for t in missing:
        commands = install_commands(t, os_ad, python, pm)
        if not commands:
            write("  atlandi: %s (%s)" % (t.name, manual_hint(t, os_ad)))
            error = error or t.importance == REQUIRED
            continue
        if t.name == "python3" and WINDOWS:
            continue   # handled by the shim step: a real Python install is the heavy path, the shim is the default
        for argv in commands:
            write("%s  install: %s" % ("[-WhatIf]" if whatif else "", line_text(argv)))
            if whatif:
                continue
            if pm == "apt" and not update_done and "apt-get" in argv:
                run_cmd((["sudo"] if argv[0] == "sudo" else []) + ["apt-get", "update", "-qq"])
                update_done = True
            rc, last = run_cmd(argv)
            if rc != 0:
                write("  HATA (%s, kod %s): %s" % (t.name, rc, " | ".join(last)))
                if WINDOWS and rc in PERMISSION_CODES:
                    write("  -> %s installer needs administrator rights (UAC was refused or could not be shown): run `install.ps1 -Tools` "
                        "(or this command) once from an elevated PowerShell." % t.name)
                error = True
    if whatif:
        return 0
    refresh_path()
    if WINDOWS:
        for t in missing:   # an installer that left its directory off PATH
            name = t.commands[0] if t.commands else ""
            if name and not shutil.which(name):
                for d in WINDOWS_DIRS.get(name, []):
                    d = os.path.expandvars(d)
                    if (Path(d) / (name + ".exe")).is_file():
                        add_user_path(d)
                        write("  %s kullanici PATH'ine eklendi: %s" % (name, d))
                        break
    if tess or any(t.name.startswith("tesseract") for t in missing):
        if not tesseract_turkish(os_ad, whatif, write):
            write("  UYARI: tesseract Turkce dil verisi eklenemedi")
    write("Kurulum sonrasi kontrol:")
    sonuc2 = check(python, os_ad)
    tess2 = next((v for t, v in sonuc2 if t.name.startswith("tesseract")), False)
    report(sonuc2, os_ad, write, ("turn" in tesseract_languages()[1]) if tess2 else None)
    remaining = [t for t, present in sonuc2 if not present and t.importance == REQUIRED]
    if missing and any(not present for _, present in sonuc2):
        write("Not: yeni kurulan araclar yeni acilan terminal/oturumlarda PATH'te gorunur.")
    return 1 if (error or remaining) else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="claude-shared command-line tool check")
    ap.add_argument("--install", action="store_true", help="install the missing tools (winget / apt-get / brew / pip --user)")
    ap.add_argument("--whatif", action="store_true", help="with --install: print the commands, run nothing")
    ap.add_argument("--python", default=sys.executable, help="the interpreter used for the Python package check and pip --user")
    ap.add_argument("--python3-shim", dest="shim", action="store_true", help="only write the Windows python3 shim")
    ap.add_argument("--remove", action="store_true", help="with --python3-shim: remove the shim")
    a = ap.parse_args(argv)
    if a.shim:
        return 0 if python3_shim(a.python, a.remove, a.whatif) else 1
    if a.install:
        python3_shim(a.python, False, a.whatif)
    return execute(a.python, a.install, a.whatif)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(main())

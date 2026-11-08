"""Width-based wrapping of the status line for statusline.sh (Linux/macOS): the same rule as statusline-yerel.ps1 (Windows).
stdin = the status line text, stdout = the same text, broken when a row is wider than the usable width.
Breaks fall at " | " first; a segment that does not fit is split at its " · " parts and filled greedily. A piece is never cut. Width: KOTA_SATIR_GENISLIK (manual) > COLUMNS (Claude Code sets it for the status-line process) >
the controlling terminal (/dev/tty); none of them known = no wrapping. Margin: KOTA_SATIR_PAY, else 4 (measured Claude Code margin)
+ 2 x settings.json statusLine.padding. Usage: satir_sar.py [claude dir]   (default ~/.claude, only used for settings.json)"""
import json
import os
import re
import sys

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
GENIS = ((0x1100, 0x115F), (0x2E80, 0xA4CF), (0xAC00, 0xD7A3), (0xF900, 0xFAFF), (0xFE30, 0xFE6F), (0xFF00, 0xFF60),
         (0xFFE0, 0xFFE6), (0x1F300, 0x1FAFF), (0x20000, 0x3FFFD))     # double-width ranges (same table as statusline-yerel.ps1)
SIFIR = ((0x300, 0x36F), (0x200B, 0x200F), (0xFE00, 0xFE0F))           # zero-width


def hucre(metin):
    """Cells a text takes on screen: ANSI escapes 0, wide characters/emoji 2, zero-width 0."""
    w = 0
    for ch in ANSI.sub("", metin):
        cp = ord(ch)
        if any(a <= cp <= b for a, b in SIFIR):
            continue
        w += 2 if any(a <= cp <= b for a, b in GENIS) else 1
    return w


def sayi(v, en_az, en_cok):
    v = (v or "").strip()
    if re.fullmatch(r"\d{1,4}", v) and en_az <= int(v) <= en_cok:
        return int(v)
    return 0


def genislik():
    g = sayi(os.environ.get("KOTA_SATIR_GENISLIK"), 20, 9999) or sayi(os.environ.get("COLUMNS"), 20, 9999)
    if g:
        return g
    try:    # controlling terminal (the status-line process usually has none; harmless when it does not)
        import fcntl
        import struct
        import termios
        fd = os.open("/dev/tty", os.O_RDONLY)
        try:
            return struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))[1]
        finally:
            os.close(fd)
    except Exception:
        return 0


def pay(dizin):
    if re.fullmatch(r"\d{1,3}", (os.environ.get("KOTA_SATIR_PAY") or "").strip()):
        return int(os.environ["KOTA_SATIR_PAY"].strip())
    try:
        with open(os.path.join(dizin, "settings.json"), encoding="utf-8-sig") as f:
            pd = json.load(f).get("statusLine", {}).get("padding")
        if isinstance(pd, (int, float)) and not isinstance(pd, bool) and 0 < int(pd) <= 50:
            return 4 + 2 * int(pd)
    except (OSError, ValueError, AttributeError):
        pass
    return 4


def sar(metin, sinir):
    """Greedy, piece by piece: the pieces are the ' | ' segments, and inside a segment its ' · ' parts. A piece joins the current row
    (with the separator it had) when it fits, otherwise it starts the next row, so a row never starts with a separator and a piece
    is never split. Each input row is wrapped on its own."""
    sonuc = []
    for ham in re.split(r"\r?\n", metin):
        if hucre(ham) <= sinir:
            sonuc.append(ham)
            continue
        cur = None
        for seg in ham.split(" | "):
            for i, parca in enumerate(seg.split(" · ")):
                ayrac = " | " if i == 0 else " · "
                if cur is None:
                    cur = parca
                elif hucre(cur + ayrac + parca) <= sinir:
                    cur += ayrac + parca
                else:
                    sonuc.append(cur)
                    cur = parca
        sonuc.append(cur)
    return "\n".join(sonuc)


def main():
    metin = sys.stdin.buffer.read().decode("utf-8", "replace")
    g = genislik()
    if g:
        metin = sar(metin, max(g - pay(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.claude")), 20))
    sys.stdout.buffer.write(metin.encode("utf-8"))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""PreToolUse hook (matcher "Agent|Task"): gives every spawned subagent a short random name.

The Claude Code UI shows a subagent only by its type ("Message from @senior-developer-oh"), so parallel agents of one type look
the same. This hook picks a name from AD_LISTESI (Turkish nature and weather words, none of them a colleague's first name) and
returns the Agent tool input through `hookSpecificOutput.updatedInput` (which replaces the whole input, so every other field is
passed on unchanged) with:
  - `description` prefixed with "<Name> · ";
  - one line prepended to `prompt`: "Your name is <Name>. Start every status message and your final report with '<Name>:'."
Names do not repeat inside one session: the used names are kept in <tempdir>/ekip-ajan-adlari/<session_id>.json (files older than
two days are removed on the way). When all names are used, a number is appended (Poyraz-2).
A person may replace the list with ~/.config/claude-shared/ajan-adlari (see ad_listesi).

It runs on every agent spawn, so it fails open: any error, an unknown tool, a missing prompt or an input that already carries a
name prints nothing and exits 0, and the call goes on unchanged. Python 3 stdlib only. Installed by kota-temposu kur.sh / kur.ps1
(settings.json PreToolUse entry running this file from the claude-shared clone).
"""
import json
import os
import random
import sys
import tempfile
import time

AD_LISTESI = (
    # winds and weather
    "Poyraz", "Lodos", "Karayel", "Meltem", "Ayaz", "Kırağı", "Tipi", "Bora", "Kasırga", "Şimşek", "Sis", "Dolu",
    # mountains
    "Toros", "Kaçkar", "Erciyes", "Uludağ", "Munzur", "Ilgaz", "Nemrut", "Süphan", "Cudi", "Bozdağ", "Spil", "Honaz",
    # rivers
    "Fırat", "Dicle", "Aras", "Çoruh", "Göksu", "Gediz", "Meriç", "Ceyhan", "Seyhan", "Sakarya", "Menderes", "Zamantı",
    # birds and animals
    "Kartal", "Martı", "Atmaca", "Turna", "Leylek", "Serçe", "Kumru", "Keklik", "Balaban", "Çaylak", "Kırlangıç", "Baykuş",
    "Vaşak", "Karakulak", "Tilki", "Sincap", "Kunduz",
    # trees
    "Çınar", "Meşe", "Ardıç", "Sedir", "Söğüt", "Ihlamur", "Gürgen",
)
KISISEL_DOSYA = ("~", ".config", "claude-shared", "ajan-adlari")   # personal override of AD_LISTESI, same folder as `kisi` / `plan`
MIN_KISISEL = 10
AYRAC = " · "
ARACLAR = ("Agent", "Task")   # "Task" is the subagent tool's older name
DURUM_KLASORU = os.environ.get("EKIP_AJAN_ADI_DIR") or os.path.join(tempfile.gettempdir(), "ekip-ajan-adlari")
ESKI_SANIYE = 2 * 86400


def _kilitli(yol, is_):
    """Run is_() while holding <yol>.lock (O_EXCL); parallel spawns of one session then never pick the same name. After ~2 s
    without the lock (a stale lock from a killed hook) it runs anyway."""
    kilit = yol + ".lock"
    fd = None
    for _ in range(200):
        try:
            fd = os.open(kilit, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(kilit) > 10:
                    os.remove(kilit)
                    continue
            except OSError:
                pass
            time.sleep(0.01)
    try:
        return is_()
    finally:
        if fd is not None:
            os.close(fd)
            try:
                os.remove(kilit)
            except OSError:
                pass


def _temizle():
    try:
        simdi = time.time()
        for ad in os.listdir(DURUM_KLASORU):
            yol = os.path.join(DURUM_KLASORU, ad)
            if simdi - os.path.getmtime(yol) > ESKI_SANIYE:
                os.remove(yol)
    except OSError:
        pass


def gecerli_ad(ad):
    return bool(ad) and ad[0].isupper() and ad.isalpha() and len(ad) <= 10


def ad_listesi():
    """The names to pick from: the valid lines of ~/.config/claude-shared/ajan-adlari (UTF-8, one name per line, `#` comments and
    blank lines ignored, de-duplicated, order kept) when that gives at least MIN_KISISEL names, else the team list AD_LISTESI."""
    try:
        yol = os.path.join(os.path.expanduser(KISISEL_DOSYA[0]), *KISISEL_DOSYA[1:])
        with open(yol, encoding="utf-8-sig") as f:
            satirlar = [s.strip() for s in f.read().splitlines()]
        adlar = list(dict.fromkeys(s for s in satirlar if s and not s.startswith("#") and gecerli_ad(s)))
        if len(adlar) >= MIN_KISISEL:
            return tuple(adlar)
    except (OSError, ValueError):
        pass
    return AD_LISTESI


def ad_sec(oturum, rastgele=random):
    """A name not used yet in this session (session_id `oturum`), recorded as used."""
    os.makedirs(DURUM_KLASORU, exist_ok=True)
    guvenli = "".join(c if c.isalnum() or c in "-_" else "_" for c in (oturum or "oturum-yok"))[:120]
    yol = os.path.join(DURUM_KLASORU, guvenli + ".json")

    liste = ad_listesi()

    def sec():
        try:
            with open(yol, encoding="utf-8") as f:
                kullanilan = json.load(f)
            if not isinstance(kullanilan, list):
                kullanilan = []
        except (OSError, ValueError):
            kullanilan = []
        bos = [a for a in liste if a not in kullanilan]
        if bos:
            ad = rastgele.choice(bos)
        else:
            tur = len(kullanilan) // len(liste) + 1
            ad = f"{rastgele.choice(liste)}-{tur}"
            while ad in kullanilan:
                ad = f"{rastgele.choice(liste)}-{tur}"
        kullanilan.append(ad)
        gecici = yol + f".{os.getpid()}.tmp"
        with open(gecici, "w", encoding="utf-8") as f:
            json.dump(kullanilan, f, ensure_ascii=False)
        os.replace(gecici, yol)
        return ad

    ad = _kilitli(yol, sec)
    _temizle()
    return ad


def adli_girdi(girdi, ad):
    """The new tool input: every field kept, description and prompt carry the name."""
    yeni = dict(girdi)
    aciklama = girdi.get("description") or ""
    yeni["description"] = f"{ad}{AYRAC}{aciklama}" if aciklama else ad
    yeni["prompt"] = f"Your name is {ad}. Start every status message and your final report with '{ad}:'.\n\n{girdi['prompt']}"
    return yeni


def zaten_adli(girdi):
    aciklama = girdi.get("description") or ""
    bas = aciklama.split(AYRAC, 1)[0]
    liste = ad_listesi() + AD_LISTESI
    return AYRAC in aciklama and (bas in liste or bas.rsplit("-", 1)[0] in liste) \
        or str(girdi.get("prompt", "")).startswith("Your name is ")


def cevap(olay):
    """The hook's stdout JSON for one PreToolUse event, or None to let the call pass unchanged."""
    if not isinstance(olay, dict) or olay.get("tool_name") not in ARACLAR:
        return None
    girdi = olay.get("tool_input")
    if not isinstance(girdi, dict) or not isinstance(girdi.get("prompt"), str) or not girdi["prompt"].strip():
        return None
    if zaten_adli(girdi):
        return None
    ad = ad_sec(str(olay.get("session_id") or ""))
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": adli_girdi(girdi, ad)}}


def main():
    try:
        try:
            sys.stdin.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
        sonuc = cevap(json.loads(sys.stdin.read() or "null"))
        if sonuc is not None:
            sys.stdout.write(json.dumps(sonuc, ensure_ascii=True))
    except Exception:   # fail open: never block or change an agent spawn because of this hook
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

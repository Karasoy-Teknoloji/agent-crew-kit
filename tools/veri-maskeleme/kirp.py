#!/usr/bin/env python3
"""Mask raw personal data in tool output before it reaches the model, and truncate big output.

BEST-EFFORT SAFETY NET, not a guarantee: regex masking of untrusted free text cannot be complete. The control is not reading
raw personal data (counts, aggregates, masked samples; masking at the source). See README "Not a guarantee" / "Known limits".

MASKING: TCKN (Turkish national ID, with checksum), payment cards (Luhn), Turkish IBANs (mod 97), e-mail addresses and
Turkish mobile/landline numbers are replaced by stable tokens built from a local keyed HMAC:
[TCKN:ab12cd] [TEL:..] [EPOSTA:..] [IBAN:..] [KART:..]. The same value always gives the same token (so the model can still
correlate rows) and the token cannot be reversed without the key. Masking runs before truncation and before the full
output is stored under ~/.claude/tool-cikti/, so the stored copy is masked too.

Files (all created with mode 0600, nothing is shipped with the tool):
  key file    ~/.claude/.maske-anahtari   random 32 bytes (hex), created on first use, never printed.
              Override the location with KIRP_KEY_FILE.
  count log   ~/.claude/maske-sayac.log   counts only, never values. Override with KIRP_COUNT_LOG.
  allowlist   ~/.claude/maske-izinli.txt  e-mail domains / phone numbers that stay readable. Override with KIRP_ALLOWLIST.
Switch masking off: KIRP_MASKE_KAPALI=1 (set it in the environment of the process that runs the hook, e.g. the "env"
block of settings.json, not in a command the agent can write).

Scope when installed as a Claude Code PostToolUse hook: Bash (successful calls only), MCP tools and any other tool (Grep ...)
that your hook matcher routes to it, and Read of files under temporary directories. Not covered: a Bash command that exits non-zero (the hook does not
fire, the raw output reaches the model), Read of repo files, images. Pipe such reads through the filter mode instead.

Big output truncation: the full output goes to a file, the context gets the head, the tail and the error lines.

Two modes:
  python3 kirp.py --hook     PostToolUse hook. stdin: the hook JSON. When the output changed (masked and/or truncated) it
                             writes hookSpecificOutput.updatedToolOutput, otherwise nothing.
  <command> 2>&1 | python3 kirp.py   filter: stdin -> stdout (use `set -o pipefail` in the shell to keep the exit code).

Truncation threshold: more than 250 lines or 30 KB. The first 60 and last 60 lines stay; error|fail|exception|traceback|fatal
lines from the middle (at most 40) stay visible. The full output goes to ~/.claude/tool-cikti/<time>-<hash>.txt, files
older than 7 days are deleted. Add a `# tam-cikti` comment to the command to skip truncation. Environment: KIRP_ESIK_SATIR,
KIRP_ESIK_BAYT, KIRP_KAPALI=1 (truncation off), KIRP_MASKE_SURE (masking time budget, seconds).
Fail closed: masking never lets text through unmasked. Text is masked in pieces (PARCA characters) under a time budget
(KIRP_MASKE_SURE, default 7 s, hard maximum 20 s; the hook timeout is 30 s). A piece that is not finished when the budget
runs out, or that raises, is replaced by a one-line notice instead of being passed through. All patterns are linear-time.
"""
import collections
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import unicodedata

DIZIN = os.path.expanduser("~/.claude/tool-cikti")
ESIK_SATIR = int(os.environ.get("KIRP_ESIK_SATIR", "250"))
ESIK_BAYT = int(os.environ.get("KIRP_ESIK_BAYT", "30000"))
BAS = SON = 60
HATA_ENFAZLA = 40
SATIR_UZUNLUK = 600
HATA_RE = re.compile(r"error|fail|exception|traceback|fatal", re.I)
# Commands whose full output the model needs or slices itself (git diff, head, tail ...): leave untouched.
ATLA_RE = re.compile(
    r"#\s*tam-cikti|\bgit\b[^|;&\n]{0,300}\b(diff|show|format-patch|blame)\b|\bgit\b[^|;&\n]{0,300}\blog\b[^|;&\n]{0,300}(-p\b|--patch|-u\b)"
    r"|(^|[|;&(\s])(diff|sed\s+-n|head|tail)\b")


# ----------------------------------------------------------------------------- MASKING
ANAHTAR_YOLU = os.path.expanduser(os.environ.get("KIRP_KEY_FILE") or "~/.claude/.maske-anahtari")
SAYAC_LOG = os.path.expanduser(os.environ.get("KIRP_COUNT_LOG") or "~/.claude/maske-sayac.log")
IZIN_YOLU = os.path.expanduser(os.environ.get("KIRP_ALLOWLIST") or "~/.claude/maske-izinli.txt")
MASKE_SURE_AZAMI = 20.0  # the hook timeout is 30 s: the masking budget stays well below it, whatever the environment says
try:
    MASKE_SURE = min(float(os.environ.get("KIRP_MASKE_SURE", "7")), MASKE_SURE_AZAMI)  # seconds; the rest is withheld, never passed raw
except ValueError:
    MASKE_SURE = 7.0
PARCA = 128_000  # text is masked in pieces of about this many characters, always cut after a newline (see _parcalar)
BIRIM_AZAMI = 8_000_000  # a single line / PEM block longer than this is withheld instead of masked
# Without SIGALRM (not the main thread of a POSIX process) the deadline is only checked between pieces: pieces and units stay small.
PARCA_ALARMSIZ = 16_000
BIRIM_ALARMSIZ = 1_000_000
_BITIS = None  # process-wide deadline (time.monotonic) set by the hook / filter entry points; None = per-call budget
_ANAHTAR = None
_IZIN = None

# Built-in allowed e-mail domains: the organisation's own and technical addresses (so infrastructure and config work stays fast).
IZINLI_ALAN = ("github.com", "users.noreply.github.com", "anthropic.com", "example.com", "example.org", "example.net",
               "localhost", "example.com", "example.org", "example.net", "invalid", "test")
# look like e-mail addresses but are file names / systemd units: the suffix is not a TLD
EPOSTA_SONEK_DISI = frozenset(
    "png jpg jpeg gif svg webp ico bmp css js mjs json xml txt log yml yaml cs dll exe o h c cpp go gz tar tgz bz2 xz "
    "pdf html htm map ts tsx jsx lock conf cfg ini toml sql db sqlite bak tmp service socket timer target mount path scope slice swap "
    "device automount nupkg snupkg whl jar war class rpm deb img iso vhd vhdx ova ovf".split())

# Digit boundaries: a number must not be part of a longer digit run and must not be the fraction of a decimal (`1.<digits>`).
# Letters, `_`, `.` and `,` around it do not protect it: `No.<TCKN>`, `TC<TCKN>`, `kimlik_<TCKN>.pdf`, `<TCKN>.0`, CSV `1,<TCKN>,x`.
_SOL = r"(?<!\d)(?<!\d\.)"
_SAG = r"(?!\d)"
_BOSLUK = "    "  # NBSP, figure space, thin space, narrow NBSP
_TIRE = "‐‑‒–—−"  # hyphen, non-breaking hyphen, figure dash, en dash, em dash, minus sign
# separator between digit groups: one or two blanks, or a dash (any dash) with an optional blank on each side
_GAYR = r"(?:[ \t" + _BOSLUK + r"]{1,2}|[ \t]?[\-" + _TIRE + r"][ \t]?)"
RE_IBAN = re.compile(r"(?<!\d)[Tt][Rr]\d{2}(?:" + _GAYR + r"?\d{4}){5}" + _GAYR + r"?\d{2}" + _SAG)
RE_KART = re.compile(
    _SOL + r"(?:\d{4}(" + _GAYR + r")\d{4}\1\d{4}\1\d{4}|\d{4}(" + _GAYR + r")\d{6}\2\d{5}|\d{13,19})" + _SAG)
RE_EPOSTA = re.compile(
    r"(?<![\w.%+\-])[^\W_][\w.%+\-]*@(?:[^\W_](?:[\w\-]*[^\W_])?\.)+[^\W\d_]{2,24}(?![\w\-]|@)")  # Unicode letters: [EPOSTA:bacc144a] in Turkish
RE_TCKN = re.compile(_SOL + r"[1-9]\d{10}" + _SAG)
# 3-3-3-2 groups (`123 456 789 01`, also with dots or dashes); a dotted group that is a valid IPv4 address is left alone
RE_TCKN_GRUP = re.compile(_SOL + r"[1-9]\d{2}(" + _GAYR + r"|\.)\d{3}\1\d{3}\1\d{2}" + _SAG)
_SON = _SAG
_ON = _SOL
_AYR = _GAYR  # separator inside a phone number with a 0 / +90 prefix: blanks (also NBSP, thin), any dash, tab
_AYR1 = r"[ \t\-" + _BOSLUK + _TIRE + r"]"  # without a prefix: ONE character (two blanks between numbers look like table columns)
# with country prefix: +90 / 0090 / 90; optional (0); 3+3+2+2 digits
RE_TEL_ULKE = re.compile(
    _ON + r"(?:\+" + _AYR + r"?90|0090|90)" + _AYR + r"?(?:\(0\)" + _AYR + r"?)?\(?[2-5]\d{2}\)?" + _AYR + r"?\d{3}" + _AYR + r"?\d{2}" + _AYR + r"?\d{2}" + _SON)
# leading-0 form: 0 5XX XXX XX XX (also with dashes, parentheses, 0(5XX)...)
RE_TEL_SIFIR = re.compile(_ON + r"0" + _AYR + r"?\(?[2-5]\d{2}\)?" + _AYR + r"?\d{3}" + _AYR + r"?\d{2}" + _AYR + r"?\d{2}" + _SON)
# dotted: 0532.123.45.67, +90.532.123.45.67, 0532.123.4567 (a dot is only accepted after a 0 / 90 / +90 / 0090 prefix, in the 3.3.2.2 shape)
RE_TEL_NOKTA = re.compile(
    _ON + r"(?:(?:\+90|0090|90)\.?|0)[2-5]\d{2}\.\d{3}\.(?:\d{2}\.\d{2}|\d{4})" + _SON)
# parenthesised area code without prefix: (5XX) XXX XX XX
RE_TEL_PAREN = re.compile(_ON + r"\([2-5]\d{2}\)" + _AYR + r"?\d{3}" + _AYR + r"?\d{2}" + _AYR + r"?\d{2}" + _SON)
# mobile without prefix: 5XX XXX XX XX, 5XX-XXX-XXXX; the bare 5XXXXXXXXX form only next to a phone-like key (see _tel_baglami):
# without one it is a size, a byte count or a row number far more often than a phone number
RE_TEL_CEP = re.compile(
    _ON + r"5\d{2}(?:(" + _AYR1 + r")\d{3}\1\d{2}\1\d{2}(?!\1\d)|(" + _AYR1 + r")\d{3}\2\d{4}(?!\2\d)|\d{7})" + _SON)
# phone-like key right before a bare number: `tel: `, `"msisdn":"`, `callerPhone=`, `Telefon No. ` (not `hotel`, `except`, `intel`)
RE_TEL_ANAHTAR = re.compile(
    r"(?:(?<![A-Za-z])(?i:tel(?:efon(?:u|no)?)?|phone|gsm|cep|mobil(?:e)?|msisdn|numara(?:s[iı])?|caller|callee|arayan|aranan"
    r"|clid|dnis|ani|fax|faks|sms|whatsapp|src|dst)|(?<=[a-z0-9])(?:Tel|Phone|Gsm|Cep|Mobile|Mobil|Msisdn|Numara|Caller|Callee))"
    r"(?i:s|lar|ler|lar[i\u0131]|ler[i\u0131])?"  # plural: phones, telefonlar, numaraları
    r"(?:[_.\-][\w.\-]{0,30}|(?-i:[A-Z0-9])[\w.\-]{0,30})?(?i:[ \t]?(?:no|nr|number|numaras[iı])\.?)?"
    r"[\"'`]?[ \t]{0,5}(?:=>|[:=])?[ \t]{0,5}[\"'`]?$")  # backtick: markdown code span, `Test GSM: `5XXXXXXXXX``
# The key in front of a number: the WHOLE key token (letters, digits, `_`, `.`, `-`), preceded by a non-key character.
RE_ID_ANAHTAR = re.compile(r"(?<![A-Za-z0-9_.\-])([A-Za-z0-9_.\-]{1,60})[\"']?\s*[:=]\s*[\"']?$")
# ALLOWLIST: a number is left alone only when the full key, normalised (camelCase boundaries and `.`/`-` become `_`, lower case),
# is exactly one of these CI / GitHub style ids. Anything else - `caller_id`, `x-customer-id`, `abone_no_id`, `kimlik-id`, `paid`,
# `Said`, a bare `id` - is masked when the value is a valid TCKN / card / phone: no list of person words can be complete.
ID_ANAHTARLARI = frozenset((
    "run_id job_id check_run_id check_suite_id workflow_id workflow_run_id artifact_id release_id asset_id comment_id deployment_id "
    "review_id status_id milestone_id label_id event_id build_id pipeline_id issue_id pull_request_id installation_id repository_id "
    "repo_id step_id task_id github_run_id github_job_id ci_job_id ci_pipeline_id ci_build_id database_id").split())
RE_ANAHTAR_SINIR = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
RE_ID_YOL = re.compile(
    r"(?i)/(?:runs|jobs|job|comments|check-runs|check-suites|artifacts|releases|assets|statuses|deployments|reviews|labels|milestones|events)/$")


def _anahtar():
    """Local HMAC key (32 bytes hex, mode 0600). If it cannot be created, a per-process temporary key is used: tokens are
    not stable across runs, but masking still works."""
    global _ANAHTAR
    if _ANAHTAR:
        return _ANAHTAR
    for _ in range(2):
        try:
            with open(ANAHTAR_YOLU, "rb") as f:
                k = f.read().strip()
            if len(k) >= 32:
                try:
                    if os.stat(ANAHTAR_YOLU).st_mode & 0o077:
                        os.chmod(ANAHTAR_YOLU, 0o600)
                except OSError:
                    pass
                _ANAHTAR = k
                return k
        except FileNotFoundError:
            pass
        except OSError:
            break
        try:
            yeni = secrets.token_hex(32).encode()
            gecici = f"{ANAHTAR_YOLU}.{os.getpid()}.{secrets.token_hex(4)}"
            fd = os.open(gecici, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(yeni)
            try:
                os.link(gecici, ANAHTAR_YOLU)  # atomic: when processes race, one wins
            except FileExistsError:
                pass
            finally:
                os.unlink(gecici)
        except OSError:
            break
    _ANAHTAR = os.urandom(32).hex().encode()
    _say_log_hata("key-temporary")
    return _ANAHTAR


def _jeton(tur, norm):
    h = hmac.new(_anahtar(), f"{tur}:{norm}".encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()[:8]
    return f"[{tur}:{h}]"


def _izinler():
    global _IZIN
    if _IZIN is None:
        alan, tel = set(IZINLI_ALAN), set()
        try:
            with open(IZIN_YOLU, encoding="utf-8") as f:
                for s in f:
                    s = s.split("#")[0].strip().lower()
                    if not s:
                        continue
                    if re.fullmatch(r"[\d +()\-]+", s):
                        tel.add(re.sub(r"\D", "", s)[-10:])
                    else:
                        alan.add(s.lstrip("@"))
        except OSError:
            pass
        _IZIN = (alan, tel)
    return _IZIN


def _tckn_gecerli(s):
    d = [int(c) for c in s]
    return (((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10 == d[9]
            and sum(d[:10]) % 10 == d[10])


def _luhn(s):
    t, ters = 0, False
    for c in reversed(s):
        n = int(c)
        if ters:
            n *= 2
            if n > 9:
                n -= 9
        t += n
        ters = not ters
    return t % 10 == 0


def _kart_gecerli(s):
    if not (13 <= len(s) <= 19):
        return False
    if not (s[0] in "3456" or "22" <= s[:2] <= "27" or s[:4] == "9792"):
        return False  # 1xxxxx: timestamps / ids; 7, 8, 0: not card ranges
    return _luhn(s)


def _iban_gecerli(s):
    s = s.upper()
    r = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in r)) % 97 == 1


# Context of a dict leaf being masked (set by _agaci_maskele for short leaves): the leaf's key stands in for the text in front
# of a number at the very start of the leaf (`{"run_id": "<11 digits>"}`, `{"tel": "5XXXXXXXXX"}`).
_YAPRAK_ANAHTAR = None
# `gh run list|view|watch` and `gh api .../actions/runs|jobs` print 11-digit run / job ids that can pass the TCKN checksum.
# _bash sets _GH_KIMLIK only when the WHOLE command is one such gh call (no `;`, `&&`, `|`, `$(`, backticks, newline ...), and even
# then only numbers in an id POSITION stay readable (see _gh_konumu): the ID column of `gh run list`, `· <id>` / `(ID <id>)` in
# `gh run view`, an `id` key in `gh api` JSON. Any other valid TCKN in the output is masked.
_GH_KIMLIK = False
RE_GH_KOMUT = re.compile(r"\s*gh\s+(?:run\s+(?:list|view|watch)|api\s+\S*actions/(?:runs|jobs))\b[^\n]*")
RE_GH_LOG = re.compile(r"--log|/logs\b")
RE_KOMUT_AYIRICI = re.compile(r"[;&|\n\r`]|\$\(|[<>]\(")
RE_GH_ONU = re.compile(r"(?:\(ID |\bID:? |\u00b7 |\brun #?|#)$")
GH_ID_ANAHTARLARI = frozenset(("id", "database_id", "run_id", "job_id", "check_run_id", "workflow_id"))


def _gh_komutu(komut):
    """Is the command exactly one gh call that prints run / job ids (and no logs)?"""
    return (isinstance(komut, str) and not RE_KOMUT_AYIRICI.search(komut) and RE_GH_KOMUT.fullmatch(komut) is not None
            and not RE_GH_LOG.search(komut))


def _gh_konumu(metin, bas, son):
    """In the output of a single gh run/api call: is the number at metin[bas:son] in a run / job id position?"""
    ls = metin.rfind("\n", 0, bas) + 1
    on = metin[ls:bas]
    if on.count("\t") == 6 and metin[son:son + 1] in ("\t", "\n", ""):
        return True  # `gh run list` (not a terminal): STATUS CONCLUSION TITLE WORKFLOW BRANCH EVENT ID ELAPSED AGE
    if RE_GH_ONU.search(on[-8:]):
        return True
    m = RE_ID_ANAHTAR.search(metin[max(0, bas - 80):bas])
    return bool(m) and _anahtar_norm(m.group(1)) in GH_ID_ANAHTARLARI


def _yaprak_basi(metin, bas):
    """Is the number at the start of a dict leaf (only blanks / quotes before it)? Then the leaf key is its context."""
    return _YAPRAK_ANAHTAR is not None and bas <= 4 and not metin[:bas].strip(" \t\"'")


def _anahtar_norm(k):
    return RE_ANAHTAR_SINIR.sub("_", k).lower().replace("-", "_").replace(".", "_").replace(" ", "_")


def _tel_baglami(metin, bas):
    """Is a bare 5XXXXXXXXX number the value of a phone-like key? Only then it is masked as a phone number."""
    if _yaprak_basi(metin, bas):
        return RE_TEL_ANAHTAR.search(_YAPRAK_ANAHTAR[-60:] + ":") is not None
    return RE_TEL_ANAHTAR.search(metin[max(0, bas - 60):bas]) is not None


def _id_baglami(metin, bas):
    """Is the number the value of an allowlisted CI id field (run_id=..., /runs/...)? Then it is not masked."""
    if _yaprak_basi(metin, bas):
        return _anahtar_norm(_YAPRAK_ANAHTAR) in ID_ANAHTARLARI
    on = metin[max(0, bas - 80):bas]
    m = RE_ID_ANAHTAR.search(on)
    if m:
        if m.start(1) == 0 and bas > len(on):
            return False  # the key is cut off at the left edge of the look-back window: unknown, so mask
        k = RE_ANAHTAR_SINIR.sub("_", m.group(1)).lower().replace("-", "_").replace(".", "_")
        if k in ID_ANAHTARLARI:
            return True
    return bool(RE_ID_YOL.search(on))


# --- credentials (conservative): key=value pairs with a known credential key + well-known token shapes ---
_SIR_ANAHTAR = (r"password|passwd|pwd|secret|client_secret|token|access_token|refresh_token|api_key|apikey|api-key|"
                r"authorization|private_key|private-key|privatekey|sifre|şifre|parola|anahtar-sifre|passphrase|"
                r"access[_.\-]?key|signing[_.\-]?key|auth[_.\-]?key|encryption[_.\-]?key|master[_.\-]?key|"
                r"validation[_.\-]?key|decryption[_.\-]?key")  # the last two: ASP.NET <machineKey validationKey= decryptionKey=>
# The key is found by itself (no repeated prefix group: `(?:\w+[_.\-])*` backtracks exponentially on "a_a_a_..."). It must not be
# preceded by a letter/digit, so `DB_PASSWORD`, `db.password`, `x-token` match while `mypassword` and `compass` do not;
# plain `pass` additionally must not be preceded by `_`.
# camelCase / PascalCase keys (`ClientSecret`, `adminPassword`, `accessToken`): a capitalised keyword right after a letter or digit.
# Case-sensitive on purpose (`(?-i:...)`), so lower-case `mypassword` / `compass` still do not match. `cancellationToken` is C# plumbing.
_SIR_DEVE = (r"(?-i:(?<=[A-Za-z0-9])(?<!ancellation)(?:Password|Passwd|Pwd|Secret|Token|ApiKey|Authorization|PrivateKey|Sifre|\u015eifre|Parola"
             r"|Passphrase|AccessKey|SigningKey|AuthKey|EncryptionKey|MasterKey|ValidationKey|DecryptionKey|Pass(?![a-z])))")  # `SmtpPass`, `MailPass`; not `Passed`, `Bypass`
# ALL-CAPS glued to a prefix: PGPASSWORD, VAULTTOKEN, MYSQLPASSWORD (not PWD: OLDPWD / PWD are directories)
_SIR_BUYUK = r"(?-i:(?<=[A-Z0-9])(?:PASSWORD|PASSWD|SECRET|TOKEN|APIKEY|PASSPHRASE))"
# A suffix after the keyword starts with a separator, a digit or an upper-case letter: `aws_secret_access_key`, `SECRET_KEY`,
# `secretKey`, `Password1`, `passwordConfirm`. `tokens`, `secretary`, `tokenizer` have none, so they are not keys.
# A suffix whose last segment names metadata (`_min_length`, `_expiry`, `-name`, `SecretRef`) is dropped in _sir_anahtar_mi.
_SIR_SONEK = r"(?:(?-i:[_.\-A-Z0-9])[\w.\-]{0,40})?"
_SIR_KEY_TAM = (r"(?:(?:(?<![^\W_])(?:" + _SIR_ANAHTAR + r")|" + _SIR_DEVE + r"|" + _SIR_BUYUK + r")" + _SIR_SONEK
                + r"|(?<!\w)pass)")
SIR_SONEK_DISI = frozenset(
    "length len min max count size limit expiry expires expiration exp ttl timeout age lifetime type kind name names ref refs "
    "file path dir url uri endpoint host port header field fields policy rule rules enabled enable disabled required prompt "
    "hint label title env var vars source mode format prefix suffix version provider store manager strategy scheme method "
    "algorithm algo regex pattern template placeholder reset changed date time at created updated expired valid validity "
    "location loc set exists through".split())
RE_SIR_SEGMENT = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
RE_SIR_GUCLU = re.compile(r"(?i)passw|passphrase|secret|sifre|şifre|parola|key")


def _sir_anahtar_mi(k):
    """Is the matched key a credential key? False when its last segment names metadata (`password_min_length`, `token_type`)."""
    seg = RE_SIR_SEGMENT.findall(k)
    return not (len(seg) > 1 and seg[-1].lower() in SIR_SONEK_DISI)


def _sir_deger_atla(k, v):
    """Values that are not secrets for this key: a short number under a weak key (`pass=1234`, `token=4096`; `password=1234`
    and six digits or more are still masked) and a directory under PWD (`PWD=/home/emre`)."""
    if v.isdigit() and len(v) < 6 and not RE_SIR_GUCLU.search(k):
        return True
    return k.lower().endswith("pwd") and (v[:1] in "/~" or re.match(r"[A-Za-z]:\\", v) is not None)


RE_SIR_KV = re.compile(
    r"(?P<pre>(?P<k>" + _SIR_KEY_TAM + r")(?:\\?[\"'])?[ \t]*(?:=>|:=|[:=])[ \t]*)"
    r"(?:(?P<sch>(?:Bearer|Basic)[ \t]+)(?=[^\s\"']))?"
    # unquoted: up to a comma / semicolon; but a head shorter than 4 characters runs on over commas (`password=Ab1,xyz9Q`)
    # C# verbatim `Password=""v"";` (e3) and an escaped single quote `Password=\'v\'` (e4) come before the plain quoted forms, which would read `""` as an empty value
    r"(?:\"\"(?P<e3>(?![;,\s])[^\"\r\n]+)\"\"|\\'(?P<e4>(?![;,\s])[^'\\\r\n]+)\\'|"
    r"\"(?P<d>(?:[^\"\\\n]|\\.)*)\"|'(?P<s>(?:[^'\\\n]|\\.)*)'|\\\"(?P<e>.*?)\\\"|(?P<u>[^\s,;\"']{4,}|[^\s;\"']+)"
    # an opening quote with no closing one on the line (a line cut short or already partly masked): the rest of the line is the value
    r"|\"(?P<e5>[^\"\r\n]{4,})(?=[\r\n]|\Z))",
    re.I)
# Code that passes the name and the value as two string literals: `headers.Add("X-API-Key", "v")`, `AddWithValue("@password", "v")`,
# `new AuthenticationHeaderValue("Bearer", "v")`, `headers["X-API-Key"] = "v"`, `new NetworkCredential("user", "v")`.
# A literal is `"v"`, `$"v"`, `@"v""w"` (verbatim) or `'v'`; a value written as an expression (a variable, `"Bearer " + t`) has no literal to mask.
_SIR_LIT = (r"(?:(?:@\$?|\$@)\"(?P<vb>(?:[^\"\r\n]|\"\")*)\"|\$?\"(?P<d>(?:[^\"\\\r\n]|\\.)*)\"|'(?P<s>(?:[^'\\\r\n]|\\.)*)')")
_SIR_BASLIK = r"[\w.\-]{1,40}-(?:key|token|secret|auth|password|passwd|pwd)"  # X-Api-Key, Ocp-Apim-Subscription-Key, X-Auth-Token
RE_SIR_KOD = re.compile(
    r"(?P<pre>(?:\([ \t]*|\[[ \t]*)(?P<q>[\"'])(?P<k>@?[\w.\-:]{0,40}?(?:" + _SIR_KEY_TAM + r")|" + _SIR_BASLIK + r"|Bearer|Basic)(?P=q)"
    r"[ \t]*(?:(?<=[\"'])(?:,\s{0,60})|\][ \t]*=(?!=)[ \t]*))" + _SIR_LIT, re.I)
RE_SIR_KOD_KIMLIK = re.compile(
    r"(?P<pre>\b(?:NetworkCredential|UsernamePasswordCredentials|SetBasicAuth|setBasicAuth)[ \t]*\([ \t]*"
    r"(?:@\$?|\$@|\$)?\"(?:[^\"\\\r\n]|\\.)*\"[ \t]*,\s{0,60})" + _SIR_LIT)
# A bare `Bearer <opaque token>` (20+ characters with a digit) anywhere in text; the `Authorization:` form is RE_SIR_AUTH's job.
RE_SIR_BASLIK_TAM = re.compile(_SIR_BASLIK, re.I)
RE_SIR_SEMA = re.compile(r"(?:Bearer|Basic|Token|Digest)[ \t]+", re.I)
RE_SIR_ENTERPOLE = re.compile(r"\{[^{}]*\}")  # `$"Bearer {token}"`: an interpolated variable, not a literal secret
RE_SIR_BEARER = re.compile(r"(?<=[Bb]earer )(?=[\w\-.~+/]{20})(?=[\w\-.~+/]*\d)[\w\-.~+/]+=*")
# URL userinfo: scheme://user:PASSWORD@host (the password may contain `@`: it ends at the last `@` before the host)
RE_SIR_URL = re.compile(
    r"(?P<pre>(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]{1,20}://[^\s/:@\"'<>?#]{0,100}:)(?P<w>[^\s/\"'<>]{1,200})(?=@[^\s@/\"'<>]+)")
# XML: <password>v</password>, <add key="Password" value="v"/>, <setting name="Password"><value>v</value></setting>,
# <entry key="password">v</entry>. Every repeat is bounded.
_XML_ATTR_KEY = (r"\b(?:key|name)\s{0,20}=\s{0,20}(?:\"[\w.\-:]{0,40}?" + _SIR_KEY_TAM + r"\"|'[\w.\-:]{0,40}?" + _SIR_KEY_TAM + r"')")
RE_SIR_XML = (
    re.compile(r"(?P<pre><[\w.\-:]{0,40}?" + _SIR_KEY_TAM + r"\s{0,20}>)(?P<v>[^<>]{4,})(?=</)", re.I),
    re.compile(r"(?P<pre>" + _XML_ATTR_KEY + r"\s{1,20}value\s{0,20}=\s{0,20})(?:\"(?P<v>[^\"<>]*)\"|'(?P<v2>[^'<>]*)')", re.I),
    re.compile(r"(?P<pre>" + _XML_ATTR_KEY + r"[^<>]{0,60}>(?:\s{0,20}<value>)?)(?P<v>[^<>]{4,})(?=</)", re.I),
)
# YAML: the value on the line after `key:` (indented). Not a nested key (`name: x`, `secretKeyRef:`), not a comment, not a list item.
RE_SIR_YAML = re.compile(
    r"(?P<pre>(?P<k>" + _SIR_KEY_TAM + r")(?:\\?[\"'])?[ \t]*:[ \t]*\r?\n[ \t]+(?![ \t]))"
    r"(?P<v>(?![#\r\n]|-[ \t]|[^\r\n]*?:(?:[ \t\r\n]|\Z))[^\r\n]+)", re.I)
# a JSON key written with unicode escapes is decoded before matching (only credential-like keys are rewritten)
RE_ESC_ANAHTAR = re.compile(r'"((?:[^"\\\n]|\\.){1,120})"(?=[ \t]*:)')
RE_UESC = re.compile(r"\\u([0-9a-fA-F]{4})")
# Well-known token shapes. One pattern per shape, each run only when its literal marker is in the text (a single alternation of
# all shapes cannot use the engine's literal-prefix scan and costs a full pass per alternative).
_SIR_SEKILLER = (
    (("BEGIN", "begin"), r"(?i:-{4,5}BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-{4,5}.*?(?:-{4,5}END [A-Z ]*PRIVATE KEY(?: BLOCK)?-{4,5}|\Z))"),
    (("hvs.", "hvb."), r"(?<![\w])hv[sb]\.[A-Za-z0-9_\-]{20,}"),
    (("gh", "github_pat_"), r"(?<![\w])gh[pousr]_[A-Za-z0-9]{20,}|(?<![\w])github_pat_[A-Za-z0-9_]{20,}"),
    (("cfut_",), r"(?<![\w])cfut_[A-Za-z0-9]{20,}"),
    (("AKIA", "ASIA"), r"(?<![A-Za-z0-9])A[KS]IA[0-9A-Z]{16}(?![A-Za-z0-9])"),
    (("xox",), r"(?<![\w])xox[abprs]-[A-Za-z0-9\-]{10,}"),
    (("eyJ",), r"(?<![\w\-])eyJ[\w\-]{8,}\.[\w\-]{8,}\.[\w\-]{8,}"),  # JWT
    (("sk-",), r"(?<![\w\-])sk-(?:ant|proj)-[\w\-]{20,}"),
    (("glpat-",), r"(?<![\w\-])glpat-[\w\-]{20,}"),
    (("AIza",), r"(?<![\w\-])AIza[\w\-]{30,40}"),
    (("k_live_", "k_test_"), r"(?<![\w\-])[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    (("npm_",), r"(?<![\w\-])npm_[A-Za-z0-9]{30,}"),
    (("earer ",), RE_SIR_BEARER.pattern),
    (("hooks.slack.com",), r"https://hooks\.slack\.com/(?:services|workflows|triggers)/[\w/\-]{20,}"),
    (("Private-Lines:",), r"Private-Lines:.*?(?:Private-MAC:[^\r\n]*|\Z)"),  # PuTTY .ppk private part
)
RE_SIR_SEKIL = tuple((isaret, re.compile(desen, re.S)) for isaret, desen in _SIR_SEKILLER)
RE_SIR_ON_KONTROL = re.compile(
    r"(?i)pass|pwd|secret|token|api.?key|authorization|private|sifre|şifre|parola|anahtar|hv[sb]\.|gh[pousr]_|github_pat_|cfut_|AKIA|xox[abprs]-"
    r"|key|cookie|eyJ|sk-|glpat-|AIza|_live_|_test_|npm_|ASIA|hooks\.slack|--user|-u[ \t=]|-U[ \t]|[?&]sig=|signature|x-amz-|x-goog-|machine")
# Authorization header / field with ANY scheme (`Token`, `Digest ...`, `AWS4-HMAC-SHA256 Credential=..., Signature=...`, none):
# everything after the scheme up to the end of the line or the closing quote is the secret.
RE_SIR_AUTH = re.compile(
    r"(?P<pre>(?<![\w\-])(?:proxy-)?authorization\\?[\"']?[ \t]*[:=][ \t]*(?P<q>\\?[\"'])?(?:[A-Za-z][\w\-]{0,30}[ \t]+(?=[^\s\"'\\]))?)"
    r"(?P<v>[^\s\"'\\](?(q)[^\r\n\"'\\]{0,4000}|[^\r\n]{0,4000}))", re.I)  # quoted: up to the quote, else the whole line
# Cookie / Set-Cookie header values (`Cookie: a=v; b=w`: every value; `Set-Cookie: s=v; Path=/`: the first pair only)
RE_SIR_CEREZ = re.compile(r"(?P<pre>(?<![\w\-])(?P<set>set-)?cookie[\"']?[ \t]*:[ \t]*[\"']?)(?P<v>[^\r\n\"']{1,8000})", re.I)
RE_CEREZ_CIFT = re.compile(r"(?P<a>[ \t]*[^=;\s]{1,200}=)(?P<v>[^;]*)")
# curl / wget style user:password (`-u user:pw`, `--user=user:pw`, `--proxy-user`)
RE_SIR_CURL = re.compile(r"(?P<pre>(?<![\w\-])(?:-u|-U|--user|--proxy-user)(?:[ \t]+|=)[\"']?[^\s:\"']{1,100}:)(?P<v>[^\s\"']{1,200})")
# URL userinfo without a password: https://<token>@host (only token-like: 12+ characters with letters and digits)
RE_SIR_URL_TOKEN = re.compile(
    r"(?P<pre>(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]{1,20}://)(?P<w>[A-Za-z0-9_\-.~%]{12,200})(?=@[^\s@/\"'<>]+)")
# signed-URL / API query parameters
RE_SIR_SORGU = re.compile(
    r"(?P<pre>[?&](?:key|sig|signature|x-amz-signature|x-amz-security-token|x-amz-credential|x-goog-signature|x-goog-credential"
    r"|access_key|accesskey|apikey|api_key|auth|token|secret|password)=)(?P<v>[^&#\s\"'<>]{4,2000})", re.I)
# name / value pairs: k8s env (`- name: DB_PASSWORD` + `value: x`), JSON {"name": "DB_PASSWORD", "value": "x"} (either order),
# {"key": "password", "value": ...}
_SIR_AD = r"[\w.\-]{0,40}?" + _SIR_KEY_TAM
RE_SIR_K8S = re.compile(
    r"(?P<pre>(?<![\w\-])name:[ \t]*[\"']?(?P<k>" + _SIR_AD + r")[\"']?[ \t]*\r?\n[ \t]*(?:-[ \t]+)?value:[ \t]*)"
    r"(?:\"(?P<d>[^\"\r\n]*)\"|'(?P<s>[^'\r\n]*)'|(?P<u>[^\s#][^\r\n]*))", re.I)
_JSON_DEGER = r"(?:\"(?P<d>(?:[^\"\\\n]|\\.)*)\"|(?P<u>-?\d[\d.]*))"
RE_SIR_NV = re.compile(
    r"(?P<pre>\"(?:name|key)\"\s{0,20}:\s{0,20}\"(?P<k>" + _SIR_AD + r")\"\s{0,20},\s{0,40}\"value\"\s{0,20}:\s{0,20})" + _JSON_DEGER, re.I)
RE_SIR_VN = re.compile(
    r"(?P<pre>\"value\"\s{0,20}:\s{0,20})" + _JSON_DEGER
    + r"(?=\s{0,20},\s{0,40}\"(?:name|key)\"\s{0,20}:\s{0,20}\"(?P<k>" + _SIR_AD + r")\")", re.I)
# one key and one value on a line: `vault kv get` (`password    x`), psql \x (`password | x`). The value is one cell: no `|`,
# no run of two blanks (`kubectl get secrets` rows have more columns).
RE_SIR_SATIR = re.compile(
    r"(?P<pre>^[ \t]*(?=[A-Za-z])(?P<k>" + _SIR_AD + r")(?:[ \t]*\|[ \t]*|[ \t]{2,}))"
    r"(?P<u>[^\s|](?:[^\s|]|[ \t](?![ \t]))*)(?=[ \t]*\r?$)", re.I | re.M)
# .netrc: machine <host> login <user> password <pw> (one line or several)
RE_SIR_NETRC = re.compile(
    r"(?P<pre>(?:\bmachine[ \t]+\S+|\bdefault)(?:\s+(?:login|account|port)[ \t]+\S+){0,3}\s+password[ \t]+)(?P<u>\S+)")
# psql / mysql result tables: a header row with a credential column, a ----+---- rule, then the rows
RE_TABLO_CIZGI = re.compile(r"[ \t]*\+?-{2,}(?:\+-{2,})+\+?[ \t]*\r?")
RE_SIR_SUTUN = re.compile(r"[ \t]*(?P<k>" + _SIR_AD + r")[ \t]*", re.I)
# The same markers as plain substrings of the lower-cased text: the credential pass runs only when one is present. A substring
# search per marker is far cheaper than one big case-insensitive alternation over megabytes of clean log.
SIR_ISARETLERI = tuple(
    "pass pwd secret token key authorization private sifre şifre parola anahtar hvs. hvb. ghp_ gho_ ghu_ ghs_ ghr_ github_pat_ "
    "cfut_ akia asia xox cookie eyj sk- glpat- aiza _live_ _test_ npm_ hooks.slack --user -u -U sig= signature x-amz- x-goog- "
    "machine begin private-lines bearer credential basicauth".lower().split())


def _sir_aday(t):
    tl = t.lower()
    return any(x in tl for x in SIR_ISARETLERI)


SIR_DEGER_DISI = frozenset(("null", "true", "false", "none", "nil", "undefined", "required", "optional", "string", "empty",
                            "redacted", "hidden", "masked"))


# A value is skipped ONLY when the WHOLE value is clearly a placeholder. Real passwords start with `$`, `!`, `%`, `<` or look like
# code (`Admin(2026)`, `a.b.c`), so neither the first character nor a code shape is a reason to skip.
_SIR_SEG = r"[A-Z]+[0-9]{0,2}"  # a name segment: letters, at most two digits (`S3`, `OAUTH2`); `ADMIN2026` looks like a password
_SIR_AD = _SIR_SEG + r"(?:_[A-Z0-9]{1,12})*"
SIR_YER_TUTUCU = re.compile(
    r"\$\{[A-Za-z_][\w.\-]*\}"  # ${DB_PASS}, ${secrets.pw}
    r"|\$" + _SIR_AD + r"|\$\(" + _SIR_AD + r"\)"  # $DB_PASS, $(DB_PASS): upper-case names; `$ADMIN2026`, `$ecretPass1` are passwords
    r"|<(?:[a-z]+(?:[ _.\-][a-z0-9]+)*|[A-Z]+(?:[ _.\-][A-Z0-9]+)*)>"  # <your-password>, <DB_PASS>; `<Secret123>` is a password
    r"|%(?:" + _SIR_AD + r"|[a-z]+(?:_[a-z0-9]+)*)%"  # %DB_PASS%, %appdata%; `%Pass9%` is a password
    r"|\{\{[^{}]*\}\}"  # {{ secrets.pw }}
    r"|\[KEY:masked\]|\[[A-Z]+:[0-9a-f]{6,8}\]"  # already masked ([SIR:xxxxxx], [TEL:xxxxxx] ...; 6 hex from older versions, 8 now)
    r"|[*\u2022xX.#\-_]+")  # ****, xxxx


def _sir_atla(v):
    return len(v) < 4 or v.lower() in SIR_DEGER_DISI or SIR_YER_TUTUCU.fullmatch(v) is not None


def _sir_kapanis(v):
    """Split trailing closing brackets that do not belong to the value off the value (`(password=hunter22)`, `{"x": password=...}`);
    brackets that close an opener inside the value (`Admin(2026)`) stay in it. One pass: the counts are taken once and
    decremented while closers are removed (the old version recounted for every bracket, quadratic on `password=a` + ")))...")."""
    k = len(v)
    acik = {c: v.count(c) for c in "([{"}
    kapali = {c: v.count(c) for c in ")]}"}
    es = {")": "(", "]": "[", "}": "{"}
    while k and v[k - 1] in ")]}":
        c = v[k - 1]
        if acik[es[c]] >= kapali[c]:
            break
        kapali[c] -= 1
        k -= 1
    return v[:k], v[k:]


RE_SIR_SATIR_BASI = re.compile(r"[ \t]*(?:[#;\-*]|//)?[ \t]*(?:(?:export|set|setx)[ \t]+)?[\w.\-]*")
RE_SIR_BITISIK = re.compile(r"\S+?(?:=>|:=|[:=])[ \t]*")


def _sir_satir_sonu(m):
    """The rest of the line after an unquoted value (with the value's own text), or "" . An assignment at the start of a line
    (`SmtpPassword=Ab9 xY 12`, `password: a b c`, `export PW=..`) with no blank before the `=` / `:` owns the whole rest of the
    line when the value stops at a blank: the secret may contain both, and the old rule left the tail as a readable
    fragment. A value that stops at `;` or a quote (connection string, code) and code-style `password = expr` (blanks around
    `=`) keep the short rule."""
    t, e = m.string, m.end("u")
    if e >= len(t) or t[e] not in " \t" or not RE_SIR_GUCLU.search(m.group("k")):
        return ""  # only a blank ends the value (a comma run-on keeps the old rule) and only a strong key (`Pass: 1234 Failed: 0` is a test summary)
    ls = t.rfind("\n", 0, m.start()) + 1
    if not RE_SIR_SATIR_BASI.fullmatch(t, ls, m.start("k")):
        return ""
    if not RE_SIR_BITISIK.fullmatch(m.group("pre"), m.start("k") - m.start()):
        return ""
    le = t.find("\n", e)
    return t[e:le if le >= 0 else len(t)].rstrip("\r")


def _sir_maskele(t, say):
    def sekil(m):
        say["SIR"] += 1
        return _jeton("SIR", m.group(0))

    kv_bitis = [0]

    def kv(m):
        k = m.group("k")
        if not _sir_anahtar_mi(k) or "uthorization" in k or "UTHORIZATION" in k:
            return m.group(0)  # metadata key; an Authorization value is RE_SIR_AUTH's job
        g = next((x for x in ("d", "s", "e", "e3", "e4", "e5", "u") if m.group(x) is not None), None)
        v = m.group(g)
        son = ""
        if g == "u":
            ek = ""
            if not (v.lower() in SIR_DEGER_DISI or SIR_YER_TUTUCU.fullmatch(v) or _sir_deger_atla(k, v)):
                ek = _sir_satir_sonu(m)
                if ek and SIR_YER_TUTUCU.fullmatch((v + ek).rstrip()):
                    ek = ""  # the whole line is a placeholder with blanks (`{{ secrets.pw }}`, `<db password>`)
            if ek:
                v, son = v + ek.rstrip(), ek[len(ek.rstrip()):]
                kv_bitis[0] = m.end("u") + len(ek)  # the match ends before the tail: the caller skips the text up to here
            else:
                if _sir_atla(v):
                    return m.group(0)
                v, son = _sir_kapanis(v)
        if _sir_atla(v) or _sir_deger_atla(k, v):
            return m.group(0)
        say["SIR"] += 1
        tok = _jeton("SIR", v)
        if g == "u":
            return m.group("pre") + (m.group("sch") or "") + tok + son
        a, b = m.start(g), m.end(g)
        return m.group(0)[:a - m.start()] + tok + m.group(0)[b - m.start():]

    def grup(g):
        """Replace only group g of the match (value of an XML element / attribute, URL password, YAML next-line value)."""
        def f(m):
            v = m.group(g)
            if v is None:
                return m.group(0)
            if g == "v" and m.re is RE_SIR_YAML:
                if not _sir_anahtar_mi(m.group("k")):
                    return m.group(0)
                c = re.search(r"[ \t]#", v)
                if c:
                    v = v[:c.start()]
            ic = v.strip()
            if _sir_atla(ic) or (m.re is RE_SIR_CURL and _sir_deger_atla("", ic)):
                return m.group(0)
            ba = m.start(g) - m.start() + (len(v) - len(v.lstrip()))
            say["SIR"] += 1
            return m.group(0)[:ba] + _jeton("SIR", ic) + m.group(0)[ba + len(ic):]
        return f

    def deger(m):
        """Value group d (double-quoted), s (single-quoted) or u (bare) of a key / value structure."""
        try:
            k = m.group("k")
        except IndexError:
            k = None
        if k is not None and not _sir_anahtar_mi(k):
            return m.group(0)
        g = next((x for x in ("d", "s", "u") if x in m.re.groupindex and m.group(x) is not None), None)
        if g is None:
            return m.group(0)
        v = m.group(g).rstrip()
        if _sir_atla(v) or _sir_deger_atla(k or "", v):
            return m.group(0)
        say["SIR"] += 1
        a = m.start(g) - m.start()
        return m.group(0)[:a] + _jeton("SIR", v) + m.group(0)[a + len(v):]

    def auth(m):
        v = m.group("v").rstrip()
        if _sir_atla(v):
            return m.group(0)
        say["SIR"] += 1
        return m.group("pre") + _jeton("SIR", v) + m.group(0)[len(m.group("pre")) + len(v):]

    def kod(m):
        """Value literal of a code pair (`Add("X-API-Key", "v")`, `NetworkCredential("u", "v")`); a scheme word (`Bearer `) stays."""
        k = m.groupdict().get("k")
        if k is not None and k.lower() not in ("bearer", "basic") and not RE_SIR_BASLIK_TAM.fullmatch(k) and not _sir_anahtar_mi(k):
            return m.group(0)
        g = next((x for x in ("vb", "d", "s") if m.group(x) is not None), None)
        if g is None:
            return m.group(0)
        v = m.group(g)
        sch = RE_SIR_SEMA.match(v)
        off = sch.end() if sch else 0
        v2 = v[off:]
        if (_sir_atla(v2) or _sir_deger_atla(k or "", v2) or v2.lower() in ("bearer", "basic", "token", "digest")
                or RE_SIR_ENTERPOLE.fullmatch(v2)):
            return m.group(0)
        say["SIR"] += 1
        a = m.start(g) - m.start() + off
        return m.group(0)[:a] + _jeton("SIR", v2) + m.group(0)[a + len(v2):]

    def cerez(m):
        ilk = [True]

        def cift(c):
            v = c.group("v").strip()
            if not (ilk[0] or not m.group("set")) or _sir_atla(v):
                ilk[0] = False
                return c.group(0)
            ilk[0] = False
            say["SIR"] += 1
            return c.group("a") + c.group("v").replace(v, _jeton("SIR", v), 1)
        return m.group("pre") + RE_CEREZ_CIFT.sub(cift, m.group("v"))

    def url_token(m):
        w = m.group("w")
        if not (re.search(r"\d", w) and re.search(r"[A-Za-z]", w)) or _sir_atla(w):
            return m.group(0)
        say["SIR"] += 1
        return m.group("pre") + _jeton("SIR", w)

    if "\\u" in t:
        t = _anahtar_coz(t)
    url = "://" in t and "@" in t
    if not (url or _sir_aday(t)):
        return t
    for isaret, desen in RE_SIR_SEKIL:
        if any(x in t for x in isaret):
            t = desen.sub(sekil, t)
    if url:
        t = RE_SIR_URL.sub(grup("w"), t)
        t = RE_SIR_URL_TOKEN.sub(url_token, t)
    if "<" in t:
        t = RE_SIR_XML[0].sub(grup("v"), t)
        t = RE_SIR_XML[1].sub(grup("v"), RE_SIR_XML[1].sub(grup("v2"), t))
        t = RE_SIR_XML[2].sub(grup("v"), t)
    dk = t.lower()
    if "uthorization" in dk:
        t = RE_SIR_AUTH.sub(auth, t)
    if "cookie" in dk:
        t = RE_SIR_CEREZ.sub(cerez, t)
    if "-u" in t or "-U" in t:
        t = RE_SIR_CURL.sub(grup("v"), t)
    if "=" in t and ("?" in t or "&" in t):
        t = RE_SIR_SORGU.sub(grup("v"), t)
    if "value" in dk:
        if "name" in dk:
            t = RE_SIR_K8S.sub(deger, t)
        if '"' in t:
            t = RE_SIR_VN.sub(deger, RE_SIR_NV.sub(deger, t))
    if "machine" in dk or "default" in dk:
        t = RE_SIR_NETRC.sub(deger, t)
    if "," in t or "]" in t:
        t = RE_SIR_KOD.sub(kod, t)
    if "redential" in t or "asicAuth" in t:
        t = RE_SIR_KOD_KIMLIK.sub(kod, t)
    cikti, son_yer = [], 0
    for m in RE_SIR_KV.finditer(t):  # like sub(), but kv may consume the rest of the line (see _sir_satir_sonu)
        if m.start() < son_yer:
            continue
        kv_bitis[0] = 0
        r = kv(m)
        cikti += (t[son_yer:m.start()], r)
        son_yer = max(m.end(), kv_bitis[0])
    cikti.append(t[son_yer:])
    t = "".join(cikti)
    if ":" in t and "\n" in t:
        t = RE_SIR_YAML.sub(grup("v"), t)
    t = RE_SIR_SATIR.sub(deger, t)
    if "|" in t and "--" in t:
        t = _tablo_maskele(t, say)
    return t


def _tablo_maskele(t, say):
    """psql / mysql result table: the cells under a credential column (`password`, `api_key`, `token_hash` ...) are masked."""
    satirlar = t.split("\n")
    degisti = False
    i = 1
    while i < len(satirlar):
        if not ("|" in satirlar[i - 1] and RE_TABLO_CIZGI.fullmatch(satirlar[i])):
            i += 1
            continue
        baslik = satirlar[i - 1].split("|")
        sira = []
        for j, h in enumerate(baslik):
            mm = RE_SIR_SUTUN.fullmatch(h.rstrip("\r"))
            if mm and _sir_anahtar_mi(mm.group("k")):
                sira.append(j)
        i += 1
        if not sira:
            continue
        while i < len(satirlar) and ("|" in satirlar[i] or RE_TABLO_CIZGI.fullmatch(satirlar[i])):
            hucre = satirlar[i].split("|")
            if len(hucre) == len(baslik):
                for j in sira:
                    v = hucre[j].strip()
                    if v and not _sir_atla(v):
                        say["SIR"] += 1
                        hucre[j] = hucre[j].replace(v, _jeton("SIR", v), 1)
                        degisti = True
                satirlar[i] = "|".join(hucre)
            i += 1
    return "\n".join(satirlar) if degisti else t


def _anahtar_coz(t):
    """Decode unicode escapes inside JSON keys that look like credential keys (`"passw\\u006frd"`), so the key can be matched."""
    def f(m):
        k = m.group(1)
        if "\\u" not in k:
            return m.group(0)
        d = RE_UESC.sub(lambda x: chr(int(x.group(1), 16)), k)
        if not RE_SIR_ON_KONTROL.search(d) or re.search(r'["\\\x00-\x1f\ud800-\udfff]', d):
            return m.group(0)
        return '"' + d + '"'
    return RE_ESC_ANAHTAR.sub(f, t)


# Terminal colour / cursor sequences. An SGR code ends in `m` (a word character), so it hides the boundary in front of a number
# or a key (`ESC[32m<TCKN>ESC[0m`, `ESC[1mpasswordESC[0m=...`): the text is matched with them removed.
RE_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x9b[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b\n]{0,2000}(?:\x07|\x1b\\)?|\x1b[()][0-9A-Za-z]|\x1b[@-Z\\-_]")
# invisible characters between digits (`1234<ZWSP>5678901`, LRM, NUL, backspace, CR, combining marks): every Unicode Cf / Cc /
# Mn / Me character except newline and tab is removed there. Blanks and dashes stay: the patterns treat them as separators.
RE_ARA = re.compile(r"(?<=\d)[^\w\n\t !-~]{1,8}(?=\d)")
RE_KONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_GORUNMEZ_TUR = frozenset(("Cf", "Cc", "Mn", "Me"))


def _ara_sil(m):
    g = m.group(0)
    return "" if all(unicodedata.category(c) in _GORUNMEZ_TUR for c in g) else g


RE_RAKAM_DIGER = re.compile(r"(?![0-9])\d")  # a decimal digit that is not ASCII (fullwidth, Arabic-Indic, Devanagari ...)
_RAKAM = None


def _rakam_tablosu():
    global _RAKAM
    if _RAKAM is None:
        _RAKAM = {cp: 48 + unicodedata.decimal(chr(cp)) for cp in range(0x660, 0x1FC00) if chr(cp).isdecimal()}
    return _RAKAM


def _normalle(t):
    """Text as the patterns should see it: colour codes and invisible (Cf/Cc/Mn/Me) characters between digits removed,
    non-ASCII decimal digits written as ASCII digits (one for one)."""
    if "\x1b" in t or "\x9b" in t:
        t = RE_ANSI.sub("", t)
    if not t.isascii() or RE_KONTROL.search(t):
        t = RE_ARA.sub(_ara_sil, t)
    if not t.isascii():
        if RE_RAKAM_DIGER.search(t):
            t = t.translate(_rakam_tablosu())
    return t


_ANAHTAR_YOK = False
_ANAHTAR_METNI = None


def _anahtar_metni():
    """The HMAC key as text, for masking its literal. Read WITHOUT creating the file (once per process); None when there is no key."""
    global _ANAHTAR_YOK, _ANAHTAR_METNI
    if _ANAHTAR_METNI:
        return _ANAHTAR_METNI
    k = _ANAHTAR
    if not k and not _ANAHTAR_YOK:
        try:
            with open(ANAHTAR_YOLU, "rb") as f:
                k = f.read().strip()
        except OSError:
            _ANAHTAR_YOK = True
            k = None
    if k and len(k) >= 32:
        _ANAHTAR_METNI = k.decode("ascii", "replace")
        return _ANAHTAR_METNI
    return None


# a run of hex pairs with up to three separators between pairs (blank, tab, newline, `,`, `:`, `-`) and an optional `0x` / `\x`
# per byte: `ab cd`, `ab:cd`, `abcd ef01`, xxd / hexdump -C columns, C arrays `{0x30, 0x31}`, Python bytes `b'\x30\x31'`
RE_HEX_DIZI = re.compile(r"(?<![0-9A-Za-z])(?:0[xX]|\\x)?[0-9a-fA-F]{2}(?:[\s,:\-]{0,3}(?:0[xX]|\\x)?[0-9a-fA-F]{2}){7,}")
RE_HEX_AYRAC = re.compile(r"0[xX]|\\x|[\s,:\-]")
_ANAHTAR_PENCERE = None


def _anahtar_pencereleri(k):
    """16-character windows of the key text and of its bytes in hex (what xxd / hexdump print for the key file)."""
    global _ANAHTAR_PENCERE
    if _ANAHTAR_PENCERE is None:
        a, b = k.lower(), k.encode("utf-8", "replace").hex()
        _ANAHTAR_PENCERE = frozenset({a[i:i + 16] for i in range(len(a) - 15)} | {b[i:i + 16] for i in range(len(b) - 15)})
    return _ANAHTAR_PENCERE


def _anahtar_maskele(t, say):
    k = _anahtar_metni()
    if not k:
        return t
    if k.lower() in t.lower():
        t, n = re.subn(re.escape(k), "[KEY:masked]", t, flags=re.I)
        say["KEY"] += n
    pencere = _anahtar_pencereleri(k)

    def grup(m):
        d = RE_HEX_AYRAC.sub("", m.group(0)).lower()
        if (any(d[i:i + 16] in pencere for i in range(len(d) - 15)) if len(d) <= 4096 else any(w in d for w in pencere)):
            say["KEY"] += 1
            return "[KEY:masked]"
        return m.group(0)
    return RE_HEX_DIZI.sub(grup, t)


# Encoded forms of an identifier: percent (`%40`, `%2B90`), HTML entities (`&#64;`, `&#x40;`, `&commat;`) and `\uXXXX` escapes.
RE_KOD = re.compile(r"%[0-9A-Fa-f]{2}|&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});|\\u[0-9A-Fa-f]{4}")
RE_YUZDE = re.compile(r"(?:%[0-9A-Fa-f]{2})+")
RE_VARLIK = re.compile(r"&(?:#([0-9]{1,7})|#[xX]([0-9A-Fa-f]{1,6})|([A-Za-z][A-Za-z0-9]{1,31}));")
RE_UESC_ZINCIR = re.compile(r"(?:\\u[0-9A-Fa-f]{4})+")


def _guvenli(d):
    """A decoded part is used only when it adds no line break, control character or lone surrogate."""
    return all(ord(c) >= 0x20 and ord(c) != 0x7f and not 0xD800 <= ord(c) <= 0xDFFF for c in d)


def _yuzde_coz(m):
    try:
        d = bytes.fromhex(m.group(0).replace("%", "")).decode("utf-8")
    except ValueError:
        return m.group(0)
    return d if _guvenli(d) else m.group(0)


def _varlik_coz(m):
    if m.group(3):
        import html.entities
        d = html.entities.html5.get(m.group(3) + ";")
        return d if d and _guvenli(d) else m.group(0)
    cp = int(m.group(1)) if m.group(1) else int(m.group(2), 16)
    if cp > 0x10FFFF:
        return m.group(0)
    d = chr(cp)
    return d if _guvenli(d) else m.group(0)


def _uesc_coz(m):
    try:
        d = json.loads('"' + m.group(0) + '"')
    except ValueError:
        return m.group(0)
    return d if _guvenli(d) else m.group(0)


def _coz(t):
    """Decode percent / entity / \\u forms (three passes at most, for double encoding such as `%2540`)."""
    for _ in range(3):
        y = t
        if "%" in y:
            y = RE_YUZDE.sub(_yuzde_coz, y)
        if "&" in y:
            y = RE_VARLIK.sub(_varlik_coz, y)
        if "\\u" in y:
            y = RE_UESC_ZINCIR.sub(_uesc_coz, y)
        if y == t:
            break
        t = y
    return t


_HAM = None  # while set (a list), the identifier callbacks record (raw text, token) for every value they mask
RE_JETON = re.compile(r"\[([A-Z]+):[0-9a-f]{8}\]")


def _kayit(ham, tok):
    if _HAM is not None:
        _HAM.append((ham, tok))
    return tok


def _kodlu_satirlar(t, say):
    """A line with an encoded form is decoded in a copy and masked alone; when that finds an identifier, the DECODED line takes
    its place (and is masked with the rest). Lines where decoding reveals nothing stay exactly as they were.
    Decoding can glue a character onto an identifier the original line had (`&#48;<TCKN>` becomes a 12-digit run): every value
    the original masking caught whose token is missing from the decoded result is replaced by its token in the decoded line; if
    that raw text is no longer there either, the original line is kept (masked as usual)."""
    global _HAM
    if not RE_KOD.search(t):
        return t
    satirlar = t.splitlines(keepends=True)
    degisti = False
    for i, s in enumerate(satirlar):
        if not RE_KOD.search(s):
            continue
        d = _coz(s)
        if d == s:
            continue
        md = _maskele_cekirdek(d, collections.Counter())
        if md == d:
            continue
        ham = []
        _HAM = ham
        try:
            _maskele_cekirdek(s, collections.Counter())
        finally:
            _HAM = None
        var = {m.group(0) for m in RE_JETON.finditer(md)}
        for h, tok in ham:
            if tok in var:
                continue
            if h not in d:
                d = None
                break
            d = d.replace(h, tok)
            say[RE_JETON.match(tok).group(1)] += 1
        if d is not None:
            satirlar[i] = d
            degisti = True
    return "".join(satirlar) if degisti else t


def _maskele_parca(t, say):
    """Mask one piece. The patterns run on the normalised text and the masked NORMALISED text is what comes back, always: the
    original is never returned, because the normaliser removes parts (colour codes, OSC payloads) that were never scanned."""
    n = _kodlu_satirlar(_anahtar_maskele(_normalle(t), say), say)
    return _maskele_cekirdek(n, say)


def _maskele_cekirdek(t, say):
    izin_alan, izin_tel = _izinler()
    t = _sir_maskele(t, say)

    def iban(m):
        s = re.sub(r"[^0-9A-Za-z]", "", m.group(0))
        if not _iban_gecerli(s):
            return m.group(0)
        say["IBAN"] += 1
        return _kayit(m.group(0), _jeton("IBAN", s.upper()))

    def kart(m):
        s = re.sub(r"\D", "", m.group(0))
        if not _kart_gecerli(s) or _id_baglami(m.string, m.start()):
            return m.group(0)
        say["KART"] += 1
        return _kayit(m.group(0), _jeton("KART", s))

    def eposta(m):
        a = m.group(0)
        alan = a.rsplit("@", 1)[1].lower()
        son = alan.rsplit(".", 1)[1]
        if son in EPOSTA_SONEK_DISI or any(alan == x or alan.endswith("." + x) for x in izin_alan):
            return a
        say["EPOSTA"] += 1
        return _kayit(a, _jeton("EPOSTA", a.lower()))

    def tckn(m):
        s = m.group(0)
        r = re.sub(r"\D", "", s)
        if len(r) != 11 or not _tckn_gecerli(r) or _id_baglami(m.string, m.start()):
            return s
        if _GH_KIMLIK and m.re is RE_TCKN and _gh_konumu(m.string, m.start(), m.end()):
            return s
        if m.re is RE_TCKN_GRUP and m.group(1) == "." and all(int(x) <= 255 for x in s.split(".")):
            return s  # 192.0.2.25: an IPv4 address, not a dotted 3-3-3-2 TCKN
        say["TCKN"] += 1
        return _kayit(s, _jeton("TCKN", r))

    def tel(sirkesiz_baglam):
        def f(m):
            s = m.group(0)
            r = re.sub(r"\D", "", s)
            if r.startswith("0090"):
                r = r[4:]
            r = r[-10:]
            if len(r) != 10 or r in izin_tel:
                return s
            if len(s) == 10 and s[0] == "5" and s.isdigit() and not _tel_baglami(m.string, m.start()):
                return s  # bare 5XXXXXXXXX without a phone-like key: a size / count / row number
            if sirkesiz_baglam and not s.startswith(("0", "+", "(")) and _id_baglami(m.string, m.start()):
                return s
            say["TEL"] += 1
            return _kayit(s, _jeton("TEL", r))
        return f

    if re.search(r"(?i)tr\d\d", t):
        t = RE_IBAN.sub(iban, t)
    if re.search(r"\d{4}", t):
        t = RE_KART.sub(kart, t)
    if "@" in t:
        t = RE_EPOSTA.sub(eposta, t)
    if re.search(r"\d{11}", t):
        t = RE_TCKN.sub(tckn, t)
    if re.search(r"\d{3}\D{1,3}\d{3}", t):
        t = RE_TCKN_GRUP.sub(tckn, t)
    if re.search(r"\d{2}", t):
        t = RE_TEL_ULKE.sub(tel(True), t)
        t = RE_TEL_SIFIR.sub(tel(True), t)
        t = RE_TEL_NOKTA.sub(tel(True), t)
        t = RE_TEL_PAREN.sub(tel(False), t)
        t = RE_TEL_CEP.sub(tel(True), t)
    return t


class _ZamanAsimi(Exception):
    pass


def _alarm_isle(signum, frame):
    raise _ZamanAsimi()


class _zaman_siniri:
    """Interrupt the body with _ZamanAsimi after `saniye` seconds (SIGALRM; CPython's regex engine checks signals while it
    matches). This is the second line of defence behind the linear-time patterns; it only works in the main thread of a
    POSIX process and silently does nothing elsewhere (the piece-wise budget check still applies)."""

    def __init__(self, saniye):
        self.saniye, self.eski, self.etkin = saniye, None, False

    def __enter__(self):
        try:
            import signal
            self.eski = signal.signal(signal.SIGALRM, _alarm_isle)
            signal.setitimer(signal.ITIMER_REAL, max(self.saniye, 0.001))
            self.etkin = True
        except Exception:
            pass
        return self

    def __exit__(self, *a):
        if self.etkin:
            import signal
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self.eski)
        return False


_BLOKLAR = (("-----BEGIN ", "-----END "), ("Private-Lines:", "Private-MAC:"))
RE_AD_SATIRI = re.compile(r"[ \t]*(?:-[ \t]+)?[\"']?(?:name|key)[\"']?[ \t]*:", re.I)


def _parcalar(metin, boyut=None):
    """(start, end) pairs of pieces for piece-wise masking. A match must never straddle a cut, so a piece always ends after a
    newline (or at the end of the text): about PARCA characters, but a line longer than that is kept whole (the patterns are
    linear-time), a piece that ends inside a `-----BEGIN` block is extended past the matching `-----END` line (or to the end
    of the text when there is none, as the pattern itself would mask it), and a piece never ends between a `key:` line and its value line;
    both rules repeat until the cut stops moving. maskele() withholds a piece over BIRIM_AZAMI."""
    n, i = len(metin), 0
    while i < n:
        j = min(i + (boyut or PARCA), n)
        if j < n:
            k = metin.rfind("\n", i, j)
            if k > i:
                j = k + 1
            else:
                k = metin.find("\n", j)
                j = n if k < 0 else k + 1
        # start of the last opener of each block kind in the piece; -1 once its block is closed or when there is none
        bs = [metin.rfind(ac, i, j) for ac, _ in _BLOKLAR]
        while j < n:
            # two rules widen a piece; repeat both until neither moves the cut (a widened piece can open a new block / key line):
            # 1. it must not end inside a -----BEGIN ... block (or a PuTTY Private-Lines part): extend past the closing line
            #    (or to the end of the text, as the pattern itself would mask it)
            # 2. a line ending in `key:` (or a `name: <credential key>` line) belongs together with the value line after it
            j0 = j
            for b, (ac, kapa) in enumerate(_BLOKLAR):
                if bs[b] >= 0 and metin.find(kapa, bs[b], j) < 0:
                    e = metin.find(kapa, j)
                    k = -1 if e < 0 else metin.find("\n", e)
                    j = n if k < 0 else k + 1
                    break
                bs[b] = -1
            else:
                ls = metin.rfind("\n", i, j - 1) + 1
                son = metin[ls:j].rstrip()
                if (son.endswith(":") or RE_AD_SATIRI.match(son)) and RE_SIR_ON_KONTROL.search(son):
                    k = metin.find("\n", j)
                    j = n if k < 0 else k + 1
            if j == j0:
                break
            for b, (ac, _) in enumerate(_BLOKLAR):
                nb = metin.rfind(ac, j0, j)  # only the newly added part can hold a new opener
                if nb >= 0:
                    bs[b] = nb
        yield i, j
        i = j


def _gizlendi(metin, neden):
    satir = metin.count("\n") + (0 if metin.endswith("\n") else 1)
    return f"[mask: {neden}, {satir} lines withheld]\n"


KISA = 2048  # a text this short is masked in one go (no piece splitting)
ONBELLEK_UZUNLUK = 256  # short texts (dict keys, repeated values) are cached: a tree repeats the same keys millions of times
ONBELLEK_AZAMI = 200_000
_ONBELLEK = {}
_BUTCE_BITTI = False  # the process-wide budget (_BITIS) is spent: _agaci_maskele stops and the rest of the tree is withheld


def _kisa_maskele(metin, bitis):
    global _BUTCE_BITTI
    if time.monotonic() >= bitis:
        if _BITIS is not None:
            _BUTCE_BITTI = True
        return _gizlendi(metin, "time budget exceeded"), collections.Counter({"ATLANDI": 1})
    anahtar = (metin, _YAPRAK_ANAHTAR, _GH_KIMLIK) if len(metin) <= ONBELLEK_UZUNLUK else None
    if anahtar is not None:
        o = _ONBELLEK.get(anahtar)
        if o is not None:
            return o[0], collections.Counter(o[1])
    say = collections.Counter()
    try:
        with _zaman_siniri(bitis - time.monotonic()):  # the interrupt stays: a runaway pattern can stall on short text too
            y = _maskele_parca(metin, say)
    except _ZamanAsimi:
        if _BITIS is not None:
            _BUTCE_BITTI = True
        return _gizlendi(metin, "time budget exceeded"), collections.Counter({"ATLANDI": 1})
    except Exception:
        _say_log_hata("mask-error")
        return _gizlendi(metin, "masking error"), collections.Counter({"ATLANDI": 1})
    if anahtar is not None:
        if len(_ONBELLEK) >= ONBELLEK_AZAMI:
            _ONBELLEK.clear()
        _ONBELLEK[anahtar] = (y, dict(say))
    return y, say


def maskele(metin):
    """Return (masked_text, Counter). Never raises and never lets unmasked text through (fail closed): text is masked piece by
    piece; when the time budget runs out (or the interrupt fires inside a piece) the rest of the text is replaced by ONE notice
    line, and a piece that raises is replaced by a notice. Counter key ATLANDI counts withheld parts."""
    global _BUTCE_BITTI
    say = collections.Counter()
    if not isinstance(metin, str) or not metin or os.environ.get("KIRP_MASKE_KAPALI") == "1":
        return metin, say
    bitis = _BITIS if _BITIS is not None else time.monotonic() + MASKE_SURE
    if len(metin) <= KISA:
        return _kisa_maskele(metin, bitis)
    cikti = []
    alarm = _alarm_var()
    boyut = PARCA if alarm else min(PARCA, PARCA_ALARMSIZ)
    birim_azami = BIRIM_AZAMI if alarm else min(BIRIM_AZAMI, BIRIM_ALARMSIZ)
    try:
        for a, b in _parcalar(metin, boyut):
            kalan = bitis - time.monotonic()
            if kalan <= 0:
                cikti.append(_gizlendi(metin[a:], "time budget exceeded"))
                say["ATLANDI"] += 1
                _BUTCE_BITTI = _BITIS is not None
                break
            if b - a > birim_azami:
                cikti.append(_gizlendi(metin[a:b], "line too long"))
                say["ATLANDI"] += 1
                continue
            try:
                with _zaman_siniri(kalan):
                    cikti.append(_maskele_parca(metin[a:b], say))
            except _ZamanAsimi:
                cikti.append(_gizlendi(metin[a:], "time budget exceeded"))
                say["ATLANDI"] += 1
                _BUTCE_BITTI = _BITIS is not None
                break
            except Exception:
                _say_log_hata("mask-error")
                cikti.append(_gizlendi(metin[a:b], "masking error"))
                say["ATLANDI"] += 1
    except BaseException:  # e.g. the interrupt fired between pieces
        _say_log_hata("mask-error")
        return "[mask: masking error, output withheld]\n", collections.Counter({"ATLANDI": 1})
    return "".join(cikti), say


def _alarm_var():
    """Can SIGALRM interrupt a piece? Only in the main thread of a POSIX process."""
    try:
        import signal
        import threading
        return hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
    except Exception:
        return False


def _butce_baslat():
    """One masking budget for the whole process (the hook masks stdout, stderr and a persisted copy in turn)."""
    global _BITIS, _BUTCE_BITTI
    _BITIS = time.monotonic() + MASKE_SURE
    _BUTCE_BITTI = False


_DUGUM = [0]
BUTCE_NOTU = "[mask: time budget exceeded, the rest of the tool output is withheld]"


def _butce_bitti_mi():
    """In the hook (process-wide budget): is the budget spent? Checked by the clock every 1024 tree nodes as well, so a tree of
    numbers (which never reaches maskele) stops too."""
    global _BUTCE_BITTI
    if _BITIS is None:
        return False
    if not _BUTCE_BITTI:
        _DUGUM[0] += 1
        if not _DUGUM[0] & 1023 and time.monotonic() >= _BITIS:
            _BUTCE_BITTI = True
    return _BUTCE_BITTI


def _not_ekle(o):
    """After the tree walk stopped on the budget: ONE notice for everything that was left out."""
    if isinstance(o, dict):
        o = dict(o)
        if isinstance(o.get("content"), list):
            o["content"] = _not_ekle(o["content"])
        else:
            o["[mask]"] = BUTCE_NOTU
    elif isinstance(o, list):
        o = list(o) + ([{"type": "text", "text": BUTCE_NOTU}] if o and isinstance(o[0], dict) and "type" in o[0] else [BUTCE_NOTU])
    return o


def _yaprak_maskele(o, anahtar):
    """Mask one string leaf; a short leaf gets its dict key as context (see _yaprak_basi)."""
    global _YAPRAK_ANAHTAR
    if anahtar is None or len(o) > 1000:
        return maskele(o)
    _YAPRAK_ANAHTAR = anahtar
    try:
        return maskele(o)
    finally:
        _YAPRAK_ANAHTAR = None


def _sayi_maskele(o, anahtar, say):
    """A JSON number that is TCKN / phone / card shaped (`{"tckn": 12345678901}`) comes back as its masked text; None = unchanged."""
    try:
        if isinstance(o, float):
            if not o.is_integer() or abs(o) >= 1e20:
                return None  # a fraction, NaN or infinity is not an identifier
            t = str(int(o))
        else:
            t = str(o)
    except (ValueError, OverflowError):
        return None
    t = t.lstrip("-")
    n = len(t)
    if not (n == 11 and _tckn_gecerli(t) or n in (10, 12) and t[0] in "59" or 13 <= n <= 19 and _kart_gecerli(t)):
        return None  # cheap shape check first: a tree can hold millions of numbers
    y, s = _yaprak_maskele(t, anahtar)
    if y == t:
        return None
    say.update(s)
    return y


def _agaci_maskele(o, say, anahtar=None):
    """tool_response tree: string leaves and identifier-shaped numbers are masked, structure and other types stay as they are.
    The dict key of a leaf is its context (`run_id` skips, `tel` makes a bare 5XXXXXXXXX a phone). Returns (new, changed)."""
    if isinstance(o, str):
        y, s = _yaprak_maskele(o, anahtar)
        say.update(s)
        return (y, True) if y != o else (o, False)
    if isinstance(o, (int, float)) and not isinstance(o, bool):
        y = _sayi_maskele(o, anahtar, say)
        return (o, False) if y is None else (y, True)
    if isinstance(o, list):
        sonuc, ch = [], False
        for x in o:
            if _butce_bitti_mi():
                say["ATLANDI"] += 1
                ch = True
                break
            y, c = _agaci_maskele(x, say, anahtar)
            sonuc.append(y)
            ch = ch or c
        return (sonuc, True) if ch else (o, False)
    if isinstance(o, dict):
        if o.get("type") == "image":
            # image block: the base64 `data` stays as it is (masking could break the picture); every other field is text
            ch = False
            sonuc = dict(o)
            for k, x in o.items():
                if k not in ("type", "data", "mimeType", "media_type"):
                    y, c = _agaci_maskele(x, say, k if isinstance(k, str) else None)
                    if c:
                        sonuc[k], ch = y, True
            return (sonuc, True) if ch else (o, False)
        sonuc, ch = {}, False
        for k, x in o.items():
            if _butce_bitti_mi():
                # budget spent: only the envelope's fixed keys stay (value withheld), so the response keeps its shape; the rest
                # of this dict is dropped and _genel / _bash add ONE notice for everything left out
                say["ATLANDI"] += 1
                ch = True
                if k in YAPI_ANAHTARLARI and k not in sonuc:
                    if isinstance(x, str):
                        sonuc[k] = "[mask: withheld]" if x else x
                    elif x is None or isinstance(x, bool):
                        sonuc[k] = x
                continue
            y, c = _agaci_maskele(x, say, k if isinstance(k, str) else None)
            if isinstance(k, str):  # a key can carry data too ({"<id number>": ...})
                k2 = _anahtar_gizle(k, len(sonuc), say)
                if k2 != k:
                    k, c = k2, True
                    while k in sonuc:  # two keys that came out equal must not swallow one value
                        k = f"{k}~{len(sonuc)}"
            sonuc[k] = y
            ch = ch or c
        return (sonuc, True) if ch else (o, False)
    return o, False


def _say_log_hata(ne):
    _say_log("system", collections.Counter({ne: 1}))


def _say_log(arac, say):
    """Counts only (never values) into the count log."""
    try:
        if not say:
            return
        satir = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} arac={arac} " + " ".join(f"{k}={v}" for k, v in sorted(say.items())) + "\n"
        try:
            if os.path.getsize(SAYAC_LOG) > 1_000_000:
                with open(SAYAC_LOG, encoding="utf-8", errors="replace") as f:
                    son = f.readlines()[-2000:]
                with open(os.open(SAYAC_LOG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                    f.writelines(son)
        except OSError:
            pass
        with open(os.open(SAYAC_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8") as f:
            f.write(satir)
    except Exception:
        pass


def _dosyayi_maskeli_yaz(yol, metin):
    """Replace the raw copy (Claude Code's persisted output file) by the masked text: temp file + atomic rename."""
    try:
        gecici = f"{yol}.maske{os.getpid()}"
        mod = os.stat(yol).st_mode & 0o777
        with open(os.open(gecici, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mod or 0o600), "w", encoding="utf-8") as f:
            f.write(metin)
        os.replace(gecici, yol)
        return True
    except Exception:
        _say_log_hata("file-write-failed")
        try:
            os.unlink(gecici)
        except Exception:
            pass
        return False


def temizle(simdi):
    try:
        for ad in os.listdir(DIZIN):
            if ad.endswith(".txt"):
                yol = os.path.join(DIZIN, ad)
                if simdi - os.path.getmtime(yol) > 7 * 86400:
                    os.remove(yol)
    except OSError:
        pass


def kaydet(metin, kaynak=None):
    os.makedirs(DIZIN, mode=0o700, exist_ok=True)
    simdi = time.time()
    temizle(simdi)
    h = hashlib.sha1(metin.encode("utf-8", "replace")).hexdigest()[:8]
    yol = os.path.join(DIZIN, f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(simdi))}-{h}.txt")
    if kaynak:  # Claude Code already wrote output over 30000 characters to a file: hard-link it instead of copying
        try:
            os.link(kaynak, yol)
            return yol
        except OSError:
            pass
    with open(os.open(yol, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8", errors="replace") as f:
        f.write(metin)
    return yol


def kisalt(s):
    if len(s) <= SATIR_UZUNLUK:
        return s
    m = HATA_RE.search(s)
    if m and m.start() >= SATIR_UZUNLUK - 100:  # error word would be cut off: open a window around it
        a = max(m.start() - 250, 0)
        return f"[+{a} chars]..." + s[a:a + SATIR_UZUNLUK - 100] + f"... [+{max(len(s) - a - SATIR_UZUNLUK + 100, 0)} chars]"
    return s[:SATIR_UZUNLUK] + f"... [+{len(s) - SATIR_UZUNLUK} chars]"


def kirp(metin, kaynak=None):
    """Return (new_text, truncated). Below the threshold the text comes back unchanged and no file is written.
    kaynak: path of a file that already holds the whole text (hook mode, persistedOutputPath)."""
    satirlar = metin.split("\n")
    sonda_bos = satirlar and satirlar[-1] == ""
    if sonda_bos:
        satirlar = satirlar[:-1]
    n = len(satirlar)
    if n <= ESIK_SATIR and len(metin.encode("utf-8", "replace")) <= ESIK_BAYT:
        return metin, False
    tut = set(range(min(BAS, n))) | set(range(max(n - SON, 0), n))
    orta = [i for i in range(n) if i not in tut and HATA_RE.search(satirlar[i])]
    if len(orta) > HATA_ENFAZLA:
        orta = orta[:HATA_ENFAZLA // 2] + orta[-HATA_ENFAZLA // 2:]
    tut |= set(orta)
    yol = kaydet(metin, kaynak)
    cikti = [f"[truncated: {n} lines, full output: {yol}]"]
    onceki = -1
    for i in sorted(tut):
        if i > onceki + 1:
            cikti.append(f"[... lines {onceki + 2}-{i}: {i - onceki - 1} lines skipped ...]")
        cikti.append(kisalt(satirlar[i]))
        onceki = i
    return "\n".join(cikti) + ("\n" if sonda_bos else ""), True


MASKE_OKU_DIZINLERI = ("/tmp/", os.path.expanduser("~/.claude/tool-cikti/"), os.path.expanduser("~/.claude/projects/"),
                       os.path.expanduser("~/Downloads/"), os.path.expanduser("~/downloads/"))


def _json_yaz(yeni):
    sys.stdout.buffer.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": yeni}},
                                       ensure_ascii=True).encode("ascii"))  # \uXXXX escapes: a lone surrogate cannot break it
    sys.stdout.buffer.write(b"\n")
    sys.stdout.flush()


def _bash(d):
    girdi = d.get("tool_input")
    komut = girdi.get("command") if isinstance(girdi, dict) else None
    komut = komut if isinstance(komut, str) else ""
    cevap = d.get("tool_response")
    if not isinstance(cevap, dict):
        return _genel(d)  # not the usual {stdout, stderr, ...} shape: mask it as a tree
    so = cevap.get("stdout")
    resim = bool(cevap.get("isImage")) and isinstance(so, str) and so.startswith("data:image/")
    atla = bool(ATLA_RE.search(komut))
    global _GH_KIMLIK
    _GH_KIMLIK = _gh_komutu(komut)
    kirpma_kapali = os.environ.get("KIRP_KAPALI") == "1"
    yeni = dict(cevap)
    degisti = False
    toplam = collections.Counter()
    for alan in ("stdout", "stderr"):
        v = cevap.get(alan)
        if alan == "stdout" and resim:
            continue  # image data (base64): not text
        if v is not None and not isinstance(v, str):
            y, c = _agaci_maskele(v, toplam, alan)
            if c:
                yeni[alan], degisti = y, True
                if _BUTCE_BITTI and isinstance(y, (list, dict)):
                    yeni[alan] = _not_ekle(y)
            continue
        kaynak = None
        yol = cevap.get("persistedOutputPath") if alan == "stdout" else None
        if isinstance(yol, str) and os.path.isfile(yol) and os.path.getsize(yol) < 200_000_000:
            try:  # Claude Code gives the hook only the first 30000 characters; the whole output is in this file
                with open(yol, encoding="utf-8", errors="replace") as f:
                    tam, kaynak = f.read(), yol
            except OSError:
                tam = None
        else:
            tam = None
        if tam is not None and (atla or kirpma_kapali):
            # no truncation: the model slices the file itself -> the masked text replaces the file in place, the preview is masked separately
            m, say = maskele(tam)
            if m != tam and _dosyayi_maskeli_yaz(yol, m):
                toplam.update(say)
            if isinstance(v, str) and v:
                mv, _ = maskele(v)
                if mv != v:
                    yeni[alan] = mv
                    degisti = True
            continue
        if tam is not None:
            v = tam
        if not (isinstance(v, str) and v):
            continue
        m, say = maskele(v)
        maskelendi = m != v
        toplam.update(say)
        if maskelendi and kaynak:
            _dosyayi_maskeli_yaz(kaynak, m)  # do not leave the raw copy on disk
        if atla or kirpma_kapali:
            y, k = m, False
        else:
            y, k = kirp(m, None if maskelendi else kaynak)
        if maskelendi or k:
            yeni[alan] = y
            degisti = True
            if kaynak:
                yeni.pop("persistedOutputPath", None)
                yeni.pop("persistedOutputSize", None)
    for alan, v in cevap.items():  # every other field too (returnCodeInterpretation, future fields ...)
        if alan in ("stdout", "stderr", "persistedOutputPath", "persistedOutputSize", "isImage", "interrupted"):
            continue
        y, c = _agaci_maskele(v, toplam, alan if isinstance(alan, str) else None)
        if c:
            yeni[alan], degisti = y, True
            if _BUTCE_BITTI and isinstance(y, (list, dict)):
                yeni[alan] = _not_ekle(y)  # a container field cut short says so inside itself
    # (no extra notice here: stdout / stderr already carry their inline notice, and the Bash envelope gets no invented key)
    _say_log("Bash", toplam)
    if degisti:
        _json_yaz(yeni)


def _genel(d):
    """MCP tools and Read under temporary directories: the texts in the tool_response tree are masked."""
    cevap = d.get("tool_response")
    if cevap is None:
        return
    say = collections.Counter()
    yeni, degisti = _agaci_maskele(cevap, say)
    if _BUTCE_BITTI and not isinstance(yeni, str):
        yeni, degisti = _not_ekle(yeni), True
    _say_log(str(d.get("tool_name"))[:80], say)
    if degisti:
        _json_yaz(yeni)


GIZLE_AZAMI = 20_000


def _gizle(o, sayac=None):
    """Replace every string leaf by a notice (fail closed after an unexpected error in the hook). A tree of more than
    GIZLE_AZAMI nodes raises instead (the caller then emits one fixed notice): no per-leaf notices for millions of leaves."""
    sayac = sayac if sayac is not None else [0]
    sayac[0] += 1
    if sayac[0] > GIZLE_AZAMI:
        raise OverflowError("tree too big")
    if isinstance(o, str):
        return "[mask: internal error, output withheld]" if o else o
    if isinstance(o, list):
        return [_gizle(x, sayac) for x in o]
    if isinstance(o, dict):
        if o.get("type") == "image":
            return o
        return {(_anahtar_gizle(k, i) if isinstance(k, str) else k): _gizle(x, sayac)
                for i, (k, x) in enumerate(o.items())}
    return o


# Fixed names of the tool-response envelope. Masking leaves them alone; they only matter when the budget is spent or masking
# fails, so the response keeps its shape (an exact match, never a pattern: any other key is a placeholder in that case).
YAPI_ANAHTARLARI = frozenset({"type", "text", "content", "stdout", "stderr", "isError", "structuredContent", "file", "filePath"})


def _anahtar_gizle(k, i, say=None):
    """EVERY string key is masked like a value (a lone surrogate becomes `?` first); there is no pre-filter that decides which
    keys may carry data. A key the masking leaves as it was (structural keys such as `type`, `content`, `isError`) is returned
    unchanged. When the masking budget is spent or masking fails, the key becomes a numbered placeholder - never the
    withheld-notice line, which would make keys collide."""
    try:
        k1 = k.encode("utf-8", "replace").decode("utf-8", "replace")
        k2, s2 = maskele(k1)
        if s2.get("ATLANDI"):
            return k if k in YAPI_ANAHTARLARI else f"[mask: key {i}]"
        if k2 == k:
            return k
        if say is not None:
            say.update(s2)
        return k2
    except BaseException:
        return k if k in YAPI_ANAHTARLARI else f"[mask: key {i}]"


_SON_CARE = b'{"hookSpecificOutput":{"hookEventName":"PostToolUse","updatedToolOutput":"[mask: internal error, output withheld]"}}\n'


def _son_care(cevap):
    """Last resort when even the withholding output cannot be built or written: a fixed ASCII payload. The tool output must
    never reach the model raw, so the hook always emits something."""
    try:
        if isinstance(cevap, dict) and ("stdout" in cevap or "stderr" in cevap):
            _json_yaz({"stdout": "[mask: internal error, output withheld]", "stderr": ""})
            return
    except BaseException:
        pass
    try:
        sys.stdout.buffer.write(_SON_CARE)
        sys.stdout.flush()
    except BaseException:
        try:
            os.write(1, _SON_CARE)
        except BaseException:
            pass


def kanca():
    d, ilgili = None, False
    try:
        veri = sys.stdin.buffer.read()  # the budget starts after the input is complete (a slow producer must not eat it)
        _butce_baslat()
        metin = veri.decode("utf-8", "replace")
        if metin.startswith("\ufeff"):
            metin = metin[1:]
        try:
            d = json.loads(metin)
        except (ValueError, RecursionError):
            try:  # trailing data after the object
                d = json.JSONDecoder().raw_decode(metin.lstrip())[0]
            except (ValueError, RecursionError):
                d = None
        if not isinstance(d, dict):
            # cannot tell which tool this is, so cannot tell whether it would have been masked: withhold (fail closed)
            _say_log_hata("hook-unparseable")
            _son_care(None)
            return
        arac = d.get("tool_name")
        arac = arac if isinstance(arac, str) else ""
        if arac == "Bash":
            ilgili = True
            _bash(d)
        elif arac.startswith("mcp__"):
            ilgili = True
            _genel(d)
        elif arac == "Read":
            yol = os.path.realpath(str((d.get("tool_input") or {}).get("file_path") or ""))
            if yol.startswith(MASKE_OKU_DIZINLERI):
                ilgili = True
                _genel(d)
        else:  # Grep and any other tool the hook matcher is wired to
            ilgili = True
            _genel(d)
    except BaseException:
        _say_log_hata("hook-error")
        try:  # a tool we would have masked: do not let its raw output through
            if ilgili and isinstance(d, dict) and d.get("tool_response") is not None:
                try:
                    _json_yaz(_gizle(d["tool_response"]))
                except BaseException:
                    _son_care(d["tool_response"])
        except BaseException:
            pass


def suzgec():
    veri = sys.stdin.buffer.read().decode("utf-8", "replace")
    _butce_baslat()  # after the input is read: `(sleep 8; echo ...) | kirp.py` must not use up the budget while waiting
    try:
        veri, _ = maskele(veri)
    except BaseException:
        veri = "[mask: masking error, output withheld]\n"
    try:
        yeni, _ = kirp(veri)
    except Exception:
        yeni = veri  # truncation failed: the text is already masked
    sys.stdout.write(yeni)


if __name__ == "__main__":
    if "--hook" in sys.argv[1:]:
        kanca()
    else:
        suzgec()

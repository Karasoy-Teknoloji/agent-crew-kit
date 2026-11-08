#!/usr/bin/env python3
"""Mask raw personal data in tool output before it reaches the model, and truncate big output.

BEST-EFFORT SAFETY NET, not a guarantee: regex masking of untrusted free text cannot be complete. The control is not reading
raw personal data (counts, aggregates, masked samples; masking at the source). See README "Not a guarantee" / "Known limits".

MASKING: TR_ID (Turkish national ID, with checksum), payment cards (Luhn), Turkish IBANs (mod 97), e-mail addresses and
Turkish mobile/landline numbers are replaced by stable tokens built from a local keyed HMAC:
[TR_ID:ab12cd] [PHONE:..] [EMAIL:..] [IBAN:..] [CARD:..]. The same value always gives the same token (so the model can still
correlate rows) and the token cannot be reversed without the key. Masking runs before truncation and before the full
output is stored under ~/.claude/tool-output/, so the stored copy is masked too.

Files (all created with mode 0600, nothing is shipped with the tool):
  key file    ~/.claude/.mask-key   random 32 bytes (hex), created on first use, never printed.
              Override the location with TRIM_KEY_FILE.
  count log   ~/.claude/mask-count.log   counts only, never values. Override with TRIM_COUNT_LOG.
  allowlist   ~/.claude/mask-allowlist.txt  e-mail domains / phone numbers that stay readable. Override with TRIM_ALLOWLIST.
Switch masking off: TRIM_MASK_CLOSED=1 (set it in the environment of the process that runs the hook, e.g. the "env"
block of settings.json, not in a command the agent can write).

Scope when installed as a Claude Code PostToolUse hook: Bash (successful calls only), MCP tools and any other tool (Grep ...)
that your hook matcher routes to it, and Read of files under temporary directories. Not covered: a Bash command that exits non-zero (the hook does not
fire, the raw output reaches the model), Read of repo files, images. Pipe such reads through the filter mode instead.

Big output truncation: the full output goes to a file, the context gets the head, the tail and the error lines.

Two modes:
  python3 trim.py --hook     PostToolUse hook. stdin: the hook JSON. When the output changed (masked and/or truncated) it
                             writes hookSpecificOutput.updatedToolOutput, otherwise nothing.
  <command> 2>&1 | python3 trim.py   filter: stdin -> stdout (use `set -o pipefail` in the shell to keep the exit code).

Truncation threshold: more than 250 lines or 30 KB. The first 60 and last 60 lines stay; error|fail|exception|traceback|fatal
lines from the middle (at most 40) stay visible. The full output goes to ~/.claude/tool-output/<time>-<hash>.txt, files
older than 7 days are deleted. Add a `# full-output` comment to the command to skip truncation. Environment: TRIM_THRESHOLD_LINE,
TRIM_THRESHOLD_BYTES, TRIM_CLOSED=1 (truncation off), TRIM_MASK_DURATION (masking time budget, seconds).
Fail closed: masking never lets text through unmasked. Text is masked in pieces (PIECE characters) under a time budget
(TRIM_MASK_DURATION, default 7 s, hard maximum 20 s; the hook timeout is 30 s). A piece that is not finished when the budget
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

DIR = os.path.expanduser("~/.claude/tool-output")
THRESHOLD_LINE = int(os.environ.get("TRIM_THRESHOLD_LINE", "250"))
THRESHOLD_BYTES = int(os.environ.get("TRIM_THRESHOLD_BYTES", "30000"))
HEAD = LAST = 60
ERROR_MAX = 40
LINE_LENGTH = 600
ERROR_RE = re.compile(r"error|fail|exception|traceback|fatal", re.I)
# Commands whose full output the model needs or slices itself (git diff, head, tail ...): leave untouched.
SKIP_RE = re.compile(
    r"#\s*full-output|\bgit\b[^|;&\n]{0,300}\b(diff|show|format-patch|blame)\b|\bgit\b[^|;&\n]{0,300}\blog\b[^|;&\n]{0,300}(-p\b|--patch|-u\b)"
    r"|(^|[|;&(\s])(diff|sed\s+-n|head|tail)\b")


# ----------------------------------------------------------------------------- MASKING
KEY_PATH = os.path.expanduser(os.environ.get("TRIM_KEY_FILE") or "~/.claude/.mask-key")
COUNTER_LOG = os.path.expanduser(os.environ.get("TRIM_COUNT_LOG") or "~/.claude/mask-count.log")
PERMISSION_PATH = os.path.expanduser(os.environ.get("TRIM_ALLOWLIST") or "~/.claude/mask-allowlist.txt")
MASK_DURATION_MAX = 20.0  # the hook timeout is 30 s: the masking budget stays well below it, whatever the environment says
try:
    MASK_DURATION = min(float(os.environ.get("TRIM_MASK_DURATION", "7")), MASK_DURATION_MAX)  # seconds; the rest is withheld, never passed raw
except ValueError:
    MASK_DURATION = 7.0
PART = 128_000  # text is masked in pieces of about this many characters, always cut after a newline (see _parts)
UNIT_MAX = 8_000_000  # a single line / PEM block longer than this is withheld instead of masked
# Without SIGALRM (not the main thread of a POSIX process) the deadline is only checked between pieces: pieces and units stay small.
PART_NO_ALARM = 16_000
UNIT_NO_ALARM = 1_000_000
_END_AT = None  # process-wide deadline (time.monotonic) set by the hook / filter entry points; None = per-call budget
_KEY = None
_PERMISSION = None

# Built-in allowed e-mail domains: the organisation's own and technical addresses (so infrastructure and config work stays fast).
ALLOWED_FIELD = ("github.com", "users.noreply.github.com", "anthropic.com", "example.com", "example.org", "example.net",
               "localhost", "example.com", "example.org", "example.net", "invalid", "test")
# look like e-mail addresses but are file names / systemd units: the suffix is not a TLD
EMAIL_SUFFIX_EXCLUDED = frozenset(
    "png jpg jpeg gif svg webp ico bmp css js mjs json xml txt log yml yaml cs dll exe o h c cpp go gz tar tgz bz2 xz "
    "pdf html htm map ts tsx jsx lock conf cfg ini toml sql db sqlite bak tmp service socket timer target mount path scope slice swap "
    "device automount nupkg snupkg whl jar war class rpm deb img iso vhd vhdx ova ovf".split())

# Digit boundaries: a number must not be part of a longer digit run and must not be the fraction of a decimal (`1.<digits>`).
# Letters, `_`, `.` and `,` around it do not protect it: `No.<TR_ID>`, `TC<TR_ID>`, `identity_<TR_ID>.pdf`, `<TR_ID>.0`, CSV `1,<TR_ID>,x`.
_LEFT_EDGE = r"(?<!\d)(?<!\d\.)"
_RIGHT = r"(?!\d)"
_SPACE = "    "  # NBSP, figure space, thin space, narrow NBSP
_DASH = "‐‑‒–—−"  # hyphen, non-breaking hyphen, figure dash, en dash, em dash, minus sign
# separator between digit groups: one or two blanks, or a dash (any dash) with an optional blank on each side
_BLANK_DASH_SEP = r"(?:[ \t" + _SPACE + r"]{1,2}|[ \t]?[\-" + _DASH + r"][ \t]?)"
RE_IBAN = re.compile(r"(?<!\d)[Tt][Rr]\d{2}(?:" + _BLANK_DASH_SEP + r"?\d{4}){5}" + _BLANK_DASH_SEP + r"?\d{2}" + _RIGHT)
RE_CARD = re.compile(
    _LEFT_EDGE + r"(?:\d{4}(" + _BLANK_DASH_SEP + r")\d{4}\1\d{4}\1\d{4}|\d{4}(" + _BLANK_DASH_SEP + r")\d{6}\2\d{5}|\d{13,19})" + _RIGHT)
RE_MAIL = re.compile(
    r"(?<![\w.%+\-])[^\W_][\w.%+\-]*@(?:[^\W_](?:[\w\-]*[^\W_])?\.)+[^\W\d_]{2,24}(?![\w\-]|@)")  # Unicode letters: [EMAIL:bacc144a] in Turkish
RE_TR_ID = re.compile(_LEFT_EDGE + r"[1-9]\d{10}" + _RIGHT)
# 3-3-3-2 groups (`123 456 789 01`, also with dots or dashes); a dotted group that is a valid IPv4 address is left alone
RE_TR_ID_GROUPED = re.compile(_LEFT_EDGE + r"[1-9]\d{2}(" + _BLANK_DASH_SEP + r"|\.)\d{3}\1\d{3}\1\d{2}" + _RIGHT)
_LAST = _RIGHT
_LEAD = _LEFT_EDGE
_SEP = _BLANK_DASH_SEP  # separator inside a phone number with a 0 / +90 prefix: blanks (also NBSP, thin), any dash, tab
_SEP1 = r"[ \t\-" + _SPACE + _DASH + r"]"  # without a prefix: ONE character (two blanks between numbers look like table columns)
# with country prefix: +90 / 0090 / 90; optional (0); 3+3+2+2 digits
RE_PHONE_COUNTRY = re.compile(
    _LEAD + r"(?:\+" + _SEP + r"?90|0090|90)" + _SEP + r"?(?:\(0\)" + _SEP + r"?)?\(?[2-5]\d{2}\)?" + _SEP + r"?\d{3}" + _SEP + r"?\d{2}" + _SEP + r"?\d{2}" + _LAST)
# leading-0 form: 0 5XX XXX XX XX (also with dashes, parentheses, 0(5XX)...)
RE_PHONE_ZERO = re.compile(_LEAD + r"0" + _SEP + r"?\(?[2-5]\d{2}\)?" + _SEP + r"?\d{3}" + _SEP + r"?\d{2}" + _SEP + r"?\d{2}" + _LAST)
# dotted: 0532.123.45.67, +90.532.123.45.67, 0532.123.4567 (a dot is only accepted after a 0 / 90 / +90 / 0090 prefix, in the 3.3.2.2 shape)
RE_PHONE_POINT = re.compile(
    _LEAD + r"(?:(?:\+90|0090|90)\.?|0)[2-5]\d{2}\.\d{3}\.(?:\d{2}\.\d{2}|\d{4})" + _LAST)
# parenthesised area code without prefix: (5XX) XXX XX XX
RE_PHONE_PAREN = re.compile(_LEAD + r"\([2-5]\d{2}\)" + _SEP + r"?\d{3}" + _SEP + r"?\d{2}" + _SEP + r"?\d{2}" + _LAST)
# mobile without prefix: 5XX XXX XX XX, 5XX-XXX-XXXX; the bare 5XXXXXXXXX form only next to a phone-like key (see _tel_context):
# without one it is a size, a byte count or a row number far more often than a phone number
RE_PHONE_CEP = re.compile(
    _LEAD + r"5\d{2}(?:(" + _SEP1 + r")\d{3}\1\d{2}\1\d{2}(?!\1\d)|(" + _SEP1 + r")\d{3}\2\d{4}(?!\2\d)|\d{7})" + _LAST)
# phone-like key right before a bare number: `tel: `, `"msisdn":"`, `callerPhone=`, `Telefon No. ` (not `hotel`, `except`, `intel`)
# Turkish locale data: the Turkish words in the phone-key pattern below (telefon, numara, arayan, aranan, ...) are matching input (the masker reads Turkish tool output), not untranslated prose.
RE_PHONE_KEY = re.compile(
    r"(?:(?<![A-Za-z])(?i:tel(?:efon(?:u|no)?)?|phone|gsm|cep|mobil(?:e)?|msisdn|numara(?:s[iı])?|caller|callee|arayan|aranan"
    r"|clid|dnis|ani|fax|faks|sms|whatsapp|src|dst)|(?<=[a-z0-9])(?:Tel|Phone|Gsm|Cep|Mobile|Mobil|Msisdn|Numara|Caller|Callee))"
    r"(?i:s|lar|ler|lar[i\u0131]|ler[i\u0131])?"  # plural: phones, telefonlar, numaraları
    r"(?:[_.\-][\w.\-]{0,30}|(?-i:[A-Z0-9])[\w.\-]{0,30})?(?i:[ \t]?(?:no|nr|number|numaras[iı])\.?)?"
    r"[\"'`]?[ \t]{0,5}(?:=>|[:=])?[ \t]{0,5}[\"'`]?$")  # backtick: markdown code span, `Test GSM: `5XXXXXXXXX``
# The key in front of a number: the WHOLE key token (letters, digits, `_`, `.`, `-`), preceded by a non-key character.
RE_ID_KEY = re.compile(r"(?<![A-Za-z0-9_.\-])([A-Za-z0-9_.\-]{1,60})[\"']?\s*[:=]\s*[\"']?$")
# ALLOWLIST: a number is left alone only when the full key, normalised (camelCase boundaries and `.`/`-` become `_`, lower case),
# is exactly one of these CI / GitHub style ids. Anything else - `caller_id`, `x-customer-id`, `abone_no_id`, `identity-id`, `paid`,
# `Said`, a bare `id` - is masked when the value is a valid TR_ID / card / phone: no list of person words can be complete.
ID_KEYS = frozenset((
    "run_id job_id check_run_id check_suite_id workflow_id workflow_run_id artifact_id release_id asset_id comment_id deployment_id "
    "review_id status_id milestone_id label_id event_id build_id pipeline_id issue_id pull_request_id installation_id repository_id "
    "repo_id step_id task_id github_run_id github_job_id ci_job_id ci_pipeline_id ci_build_id database_id").split())
RE_KEY_LIMIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
RE_ID_PATH = re.compile(
    r"(?i)/(?:runs|jobs|job|comments|check-runs|check-suites|artifacts|releases|assets|statuses|deployments|reviews|labels|milestones|events)/$")


def _key():
    """Local HMAC key (32 bytes hex, mode 0600). If it cannot be created, a per-process temporary key is used: tokens are
    not stable across runs, but masking still works."""
    global _KEY
    if _KEY:
        return _KEY
    for _ in range(2):
        try:
            with open(KEY_PATH, "rb") as f:
                k = f.read().strip()
            if len(k) >= 32:
                try:
                    if os.stat(KEY_PATH).st_mode & 0o077:
                        os.chmod(KEY_PATH, 0o600)
                except OSError:
                    pass
                _KEY = k
                return k
        except FileNotFoundError:
            pass
        except OSError:
            break
        try:
            new = secrets.token_hex(32).encode()
            temp = f"{KEY_PATH}.{os.getpid()}.{secrets.token_hex(4)}"
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(new)
            try:
                os.link(temp, KEY_PATH)  # atomic: when processes race, one wins
            except FileExistsError:
                pass
            finally:
                os.unlink(temp)
        except OSError:
            break
    _KEY = os.urandom(32).hex().encode()
    _count_log_error("key-temporary")
    return _KEY


def _token(kind, norm):
    h = hmac.new(_key(), f"{kind}:{norm}".encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()[:8]
    return f"[{kind}:{h}]"


def _permissions():
    global _PERMISSION
    if _PERMISSION is None:
        field, tel = set(ALLOWED_FIELD), set()
        try:
            with open(PERMISSION_PATH, encoding="utf-8") as f:
                for s in f:
                    s = s.split("#")[0].strip().lower()
                    if not s:
                        continue
                    if re.fullmatch(r"[\d +()\-]+", s):
                        tel.add(re.sub(r"\D", "", s)[-10:])
                    else:
                        field.add(s.lstrip("@"))
        except OSError:
            pass
        _PERMISSION = (field, tel)
    return _PERMISSION


def _tr_id_valid(s):
    d = [int(c) for c in s]
    return (((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10 == d[9]
            and sum(d[:10]) % 10 == d[10])


def _luhn(s):
    t, reverse = 0, False
    for c in reversed(s):
        n = int(c)
        if reverse:
            n *= 2
            if n > 9:
                n -= 9
        t += n
        reverse = not reverse
    return t % 10 == 0


def _card_valid(s):
    if not (13 <= len(s) <= 19):
        return False
    if not (s[0] in "3456" or "22" <= s[:2] <= "27" or s[:4] == "9792"):
        return False  # 1xxxxx: timestamps / ids; 7, 8, 0: not card ranges
    return _luhn(s)


def _iban_valid(s):
    s = s.upper()
    r = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in r)) % 97 == 1


# Context of a dict leaf being masked (set by _mask_out_tree for short leaves): the leaf's key stands in for the text in front
# of a number at the very start of the leaf (`{"run_id": "<11 digits>"}`, `{"tel": "5XXXXXXXXX"}`).
_LEAF_KEY = None
# `gh run list|view|watch` and `gh api .../actions/runs|jobs` print 11-digit run / job ids that can pass the TR_ID checksum.
# _bash sets _GH_IDENTITY only when the WHOLE command is one such gh call (no `;`, `&&`, `|`, `$(`, backticks, newline ...), and even
# then only numbers in an id POSITION stay readable (see _gh_location): the ID column of `gh run list`, `· <id>` / `(ID <id>)` in
# `gh run view`, an `id` key in `gh api` JSON. Any other valid TR_ID in the output is masked.
_GH_IDENTITY = False
RE_GH_COMMAND = re.compile(r"\s*gh\s+(?:run\s+(?:list|view|watch)|api\s+\S*actions/(?:runs|jobs))\b[^\n]*")
RE_GH_LOG = re.compile(r"--log|/logs\b")
RE_COMMAND_SEPARATOR = re.compile(r"[;&|\n\r`]|\$\(|[<>]\(")
RE_GH_PREFIX = re.compile(r"(?:\(ID |\bID:? |\u00b7 |\brun #?|#)$")
GH_ID_KEYS = frozenset(("id", "database_id", "run_id", "job_id", "check_run_id", "workflow_id"))


def _gh_command(command):
    """Is the command exactly one gh call that prints run / job ids (and no logs)?"""
    return (isinstance(command, str) and not RE_COMMAND_SEPARATOR.search(command) and RE_GH_COMMAND.fullmatch(command) is not None
            and not RE_GH_LOG.search(command))


def _gh_location(text, start, last):
    """In the output of a single gh run/api call: is the number at text[start:end] in a run / job id position?"""
    ls = text.rfind("\n", 0, start) + 1
    on = text[ls:start]
    if on.count("\t") == 6 and text[last:last + 1] in ("\t", "\n", ""):
        return True  # `gh run list` (not a terminal): STATUS CONCLUSION TITLE WORKFLOW BRANCH EVENT ID ELAPSED AGE
    if RE_GH_PREFIX.search(on[-8:]):
        return True
    m = RE_ID_KEY.search(text[max(0, start - 80):start])
    return bool(m) and _key_norm(m.group(1)) in GH_ID_KEYS


def _leaf_head(text, start):
    """Is the number at the start of a dict leaf (only blanks / quotes before it)? Then the leaf key is its context."""
    return _LEAF_KEY is not None and start <= 4 and not text[:start].strip(" \t\"'")


def _key_norm(k):
    return RE_KEY_LIMIT.sub("_", k).lower().replace("-", "_").replace(".", "_").replace(" ", "_")


def _tel_context(text, start):
    """Is a bare 5XXXXXXXXX number the value of a phone-like key? Only then it is masked as a phone number."""
    if _leaf_head(text, start):
        return RE_PHONE_KEY.search(_LEAF_KEY[-60:] + ":") is not None
    return RE_PHONE_KEY.search(text[max(0, start - 60):start]) is not None


def _id_context(text, start):
    """Is the number the value of an allowlisted CI id field (run_id=..., /runs/...)? Then it is not masked."""
    if _leaf_head(text, start):
        return _key_norm(_LEAF_KEY) in ID_KEYS
    on = text[max(0, start - 80):start]
    m = RE_ID_KEY.search(on)
    if m:
        if m.start(1) == 0 and start > len(on):
            return False  # the key is cut off at the left edge of the look-back window: unknown, so mask
        k = RE_KEY_LIMIT.sub("_", m.group(1)).lower().replace("-", "_").replace(".", "_")
        if k in ID_KEYS:
            return True
    return bool(RE_ID_PATH.search(on))


# --- credentials (conservative): key=value pairs with a known credential key + well-known token shapes ---
# Turkish locale data: the Turkish words in the credential-key list below (sifre, parola, anahtar-sifre) are matching input (the masker reads Turkish tool output), not untranslated prose.
_SECRET_KEY = (r"password|passwd|pwd|secret|client_secret|token|access_token|refresh_token|api_key|apikey|api-key|"
                r"authorization|private_key|private-key|privatekey|sifre|şifre|parola|anahtar-sifre|passphrase|"
                r"access[_.\-]?key|signing[_.\-]?key|auth[_.\-]?key|encryption[_.\-]?key|master[_.\-]?key|"
                r"validation[_.\-]?key|decryption[_.\-]?key")  # the last two: ASP.NET <machineKey validationKey= decryptionKey=>
# The key is found by itself (no repeated prefix group: `(?:\w+[_.\-])*` backtracks exponentially on "a_a_a_..."). It must not be
# preceded by a letter/digit, so `DB_PASSWORD`, `db.password`, `x-token` match while `mypassword` and `compass` do not;
# plain `pass` additionally must not be preceded by `_`.
# camelCase / PascalCase keys (`ClientSecret`, `adminPassword`, `accessToken`): a capitalised keyword right after a letter or digit.
# Case-sensitive on purpose (`(?-i:...)`), so lower-case `mypassword` / `compass` still do not match. `cancellationToken` is C# plumbing.
# Turkish locale data: the Turkish words in the camelCase credential keys below (Sifre, Parola) are matching input (the masker reads Turkish tool output), not untranslated prose.
_SECRET_CAMEL = (r"(?-i:(?<=[A-Za-z0-9])(?<!ancellation)(?:Password|Passwd|Pwd|Secret|Token|ApiKey|Authorization|PrivateKey|Sifre|\u015eifre|Parola"
             r"|Passphrase|AccessKey|SigningKey|AuthKey|EncryptionKey|MasterKey|ValidationKey|DecryptionKey|Pass(?![a-z])))")  # `SmtpPass`, `MailPass`; not `Passed`, `Bypass`
# ALL-CAPS glued to a prefix: PGPASSWORD, VAULTTOKEN, MYSQLPASSWORD (not PWD: OLDPWD / PWD are directories)
_SECRET_UPPER = r"(?-i:(?<=[A-Z0-9])(?:PASSWORD|PASSWD|SECRET|TOKEN|APIKEY|PASSPHRASE))"
# A suffix after the keyword starts with a separator, a digit or an upper-case letter: `aws_secret_access_key`, `SECRET_KEY`,
# `secretKey`, `Password1`, `passwordConfirm`. `tokens`, `secretary`, `tokenizer` have none, so they are not keys.
# A suffix whose last segment names metadata (`_min_length`, `_expiry`, `-name`, `SecretRef`) is dropped in _is_secret_key.
_SECRET_SUFFIX = r"(?:(?-i:[_.\-A-Z0-9])[\w.\-]{0,40})?"
# `[SECRET:xxxxxxxx]` is our own token: its label must not read as the credential key `secret` followed by a value.
_SECRET_KEY_FULL = (r"(?!(?<=\[)SECRET:[0-9a-f]{6,8}\])(?:(?:(?<![^\W_])(?:" + _SECRET_KEY + r")|" + _SECRET_CAMEL + r"|" + _SECRET_UPPER + r")" + _SECRET_SUFFIX
                + r"|(?<!\w)pass)")
SECRET_SUFFIX_EXCLUDED = frozenset(
    "length len min max count size limit expiry expires expiration exp ttl timeout age lifetime type kind name names ref refs "
    "file path dir url uri endpoint host port header field fields policy rule rules enabled enable disabled required prompt "
    "hint label title env var vars source mode format prefix suffix version provider store manager strategy scheme method "
    "algorithm algo regex pattern template placeholder reset changed date time at created updated expired valid validity "
    "location loc set exists through".split())
RE_SECRET_SEGMENT = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
# Turkish locale data: the Turkish words in this key pattern (sifre, parola) are matching input (the masker reads Turkish tool output), not untranslated prose.
RE_SECRET_STRONG = re.compile(r"(?i)passw|passphrase|secret|sifre|şifre|parola|key")


def _is_secret_key(k):
    """Is the matched key a credential key? False when its last segment names metadata (`password_min_length`, `token_type`)."""
    seg = RE_SECRET_SEGMENT.findall(k)
    return not (len(seg) > 1 and seg[-1].lower() in SECRET_SUFFIX_EXCLUDED)


def _skip_secret_value(k, v):
    """Values that are not secrets for this key: a short number under a weak key (`pass=1234`, `token=4096`; `password=1234`
    and six digits or more are still masked) and a directory under PWD (`PWD=/home/emre`)."""
    if v.isdigit() and len(v) < 6 and not RE_SECRET_STRONG.search(k):
        return True
    return k.lower().endswith("pwd") and (v[:1] in "/~" or re.match(r"[A-Za-z]:\\", v) is not None)


RE_SECRET_KV = re.compile(
    r"(?P<pre>(?P<k>" + _SECRET_KEY_FULL + r")(?:\\?[\"'])?[ \t]*(?:=>|:=|[:=])[ \t]*)"
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
_SECRET_LIT = (r"(?:(?:@\$?|\$@)\"(?P<vb>(?:[^\"\r\n]|\"\")*)\"|\$?\"(?P<d>(?:[^\"\\\r\n]|\\.)*)\"|'(?P<s>(?:[^'\\\r\n]|\\.)*)')")
_SECRET_TITLE = r"[\w.\-]{1,40}-(?:key|token|secret|auth|password|passwd|pwd)"  # X-Api-Key, Ocp-Apim-Subscription-Key, X-Auth-Token
RE_SECRET_CODE = re.compile(
    r"(?P<pre>(?:\([ \t]*|\[[ \t]*)(?P<q>[\"'])(?P<k>@?[\w.\-:]{0,40}?(?:" + _SECRET_KEY_FULL + r")|" + _SECRET_TITLE + r"|Bearer|Basic)(?P=q)"
    r"[ \t]*(?:(?<=[\"'])(?:,\s{0,60})|\][ \t]*=(?!=)[ \t]*))" + _SECRET_LIT, re.I)
RE_SECRET_CODE_IDENTITY = re.compile(
    r"(?P<pre>\b(?:NetworkCredential|UsernamePasswordCredentials|SetBasicAuth|setBasicAuth)[ \t]*\([ \t]*"
    r"(?:@\$?|\$@|\$)?\"(?:[^\"\\\r\n]|\\.)*\"[ \t]*,\s{0,60})" + _SECRET_LIT)
# A bare `Bearer <opaque token>` (20+ characters with a digit) anywhere in text; the `Authorization:` form is RE_SECRET_AUTH's job.
RE_SECRET_TITLE_FULL = re.compile(_SECRET_TITLE, re.I)
RE_SECRET_SCHEMA = re.compile(r"(?:Bearer|Basic|Token|Digest)[ \t]+", re.I)
RE_SECRET_INTERPOLATE = re.compile(r"\{[^{}]*\}")  # `$"Bearer {token}"`: an interpolated variable, not a literal secret
RE_SECRET_BEARER = re.compile(r"(?<=[Bb]earer )(?=[\w\-.~+/]{20})(?=[\w\-.~+/]*\d)[\w\-.~+/]+=*")
# URL userinfo: scheme://user:PASSWORD@host (the password may contain `@`: it ends at the last `@` before the host)
RE_SECRET_URL = re.compile(
    r"(?P<pre>(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]{1,20}://[^\s/:@\"'<>?#]{0,100}:)(?P<w>[^\s/\"'<>]{1,200})(?=@[^\s@/\"'<>]+)")
# XML: <password>v</password>, <add key="Password" value="v"/>, <setting name="Password"><value>v</value></setting>,
# <entry key="password">v</entry>. Every repeat is bounded.
_XML_ATTR_KEY = (r"\b(?:key|name)\s{0,20}=\s{0,20}(?:\"[\w.\-:]{0,40}?" + _SECRET_KEY_FULL + r"\"|'[\w.\-:]{0,40}?" + _SECRET_KEY_FULL + r"')")
RE_SECRET_XML = (
    re.compile(r"(?P<pre><[\w.\-:]{0,40}?" + _SECRET_KEY_FULL + r"\s{0,20}>)(?P<v>[^<>]{4,})(?=</)", re.I),
    re.compile(r"(?P<pre>" + _XML_ATTR_KEY + r"\s{1,20}value\s{0,20}=\s{0,20})(?:\"(?P<v>[^\"<>]*)\"|'(?P<v2>[^'<>]*)')", re.I),
    re.compile(r"(?P<pre>" + _XML_ATTR_KEY + r"[^<>]{0,60}>(?:\s{0,20}<value>)?)(?P<v>[^<>]{4,})(?=</)", re.I),
)
# YAML: the value on the line after `key:` (indented). Not a nested key (`name: x`, `secretKeyRef:`), not a comment, not a list item.
RE_SECRET_YAML = re.compile(
    r"(?P<pre>(?P<k>" + _SECRET_KEY_FULL + r")(?:\\?[\"'])?[ \t]*:[ \t]*\r?\n[ \t]+(?![ \t]))"
    r"(?P<v>(?![#\r\n]|-[ \t]|[^\r\n]*?:(?:[ \t\r\n]|\Z))[^\r\n]+)", re.I)
# a JSON key written with unicode escapes is decoded before matching (only credential-like keys are rewritten)
RE_ESC_KEY = re.compile(r'"((?:[^"\\\n]|\\.){1,120})"(?=[ \t]*:)')
RE_UESC = re.compile(r"\\u([0-9a-fA-F]{4})")
# Well-known token shapes. One pattern per shape, each run only when its literal marker is in the text (a single alternation of
# all shapes cannot use the engine's literal-prefix scan and costs a full pass per alternative).
_SECRET_SHAPES = (
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
    (("earer ",), RE_SECRET_BEARER.pattern),
    (("hooks.slack.com",), r"https://hooks\.slack\.com/(?:services|workflows|triggers)/[\w/\-]{20,}"),
    (("Private-Lines:",), r"Private-Lines:.*?(?:Private-MAC:[^\r\n]*|\Z)"),  # PuTTY .ppk private part
)
RE_SECRET_SHAPE = tuple((marker, re.compile(pattern, re.S)) for marker, pattern in _SECRET_SHAPES)
# Turkish locale data: the Turkish words in this precheck pattern (sifre, parola, anahtar) are matching input (the masker reads Turkish tool output), not untranslated prose.
RE_SECRET_PRECHECK = re.compile(
    r"(?i)pass|pwd|secret|token|api.?key|authorization|private|sifre|şifre|parola|anahtar|hv[sb]\.|gh[pousr]_|github_pat_|cfut_|AKIA|xox[abprs]-"
    r"|key|cookie|eyJ|sk-|glpat-|AIza|_live_|_test_|npm_|ASIA|hooks\.slack|--user|-u[ \t=]|-U[ \t]|[?&]sig=|signature|x-amz-|x-goog-|machine")
# Authorization header / field with ANY scheme (`Token`, `Digest ...`, `AWS4-HMAC-SHA256 Credential=..., Signature=...`, none):
# everything after the scheme up to the end of the line or the closing quote is the secret.
RE_SECRET_AUTH = re.compile(
    r"(?P<pre>(?<![\w\-])(?:proxy-)?authorization\\?[\"']?[ \t]*[:=][ \t]*(?P<q>\\?[\"'])?(?:[A-Za-z][\w\-]{0,30}[ \t]+(?=[^\s\"'\\]))?)"
    r"(?P<v>[^\s\"'\\](?(q)[^\r\n\"'\\]{0,4000}|[^\r\n]{0,4000}))", re.I)  # quoted: up to the quote, else the whole line
# Cookie / Set-Cookie header values (`Cookie: a=v; b=w`: every value; `Set-Cookie: s=v; Path=/`: the first pair only)
RE_SECRET_COOKIE = re.compile(r"(?P<pre>(?<![\w\-])(?P<set>set-)?cookie[\"']?[ \t]*:[ \t]*[\"']?)(?P<v>[^\r\n\"']{1,8000})", re.I)
RE_COOKIE_DOUBLE = re.compile(r"(?P<a>[ \t]*[^=;\s]{1,200}=)(?P<v>[^;]*)")
# curl / wget style user:password (`-u user:pw`, `--user=user:pw`, `--proxy-user`)
RE_SECRET_CURL = re.compile(r"(?P<pre>(?<![\w\-])(?:-u|-U|--user|--proxy-user)(?:[ \t]+|=)[\"']?[^\s:\"']{1,100}:)(?P<v>[^\s\"']{1,200})")
# URL userinfo without a password: https://<token>@host (only token-like: 12+ characters with letters and digits)
RE_SECRET_URL_TOKEN = re.compile(
    r"(?P<pre>(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]{1,20}://)(?P<w>[A-Za-z0-9_\-.~%]{12,200})(?=@[^\s@/\"'<>]+)")
# signed-URL / API query parameters
RE_SECRET_QUERY = re.compile(
    r"(?P<pre>[?&](?:key|sig|signature|x-amz-signature|x-amz-security-token|x-amz-credential|x-goog-signature|x-goog-credential"
    r"|access_key|accesskey|apikey|api_key|auth|token|secret|password)=)(?P<v>[^&#\s\"'<>]{4,2000})", re.I)
# name / value pairs: k8s env (`- name: DB_PASSWORD` + `value: x`), JSON {"name": "DB_PASSWORD", "value": "x"} (either order),
# {"key": "password", "value": ...}
_SECRET_NAME = r"[\w.\-]{0,40}?" + _SECRET_KEY_FULL
RE_SECRET_K8S = re.compile(
    r"(?P<pre>(?<![\w\-])name:[ \t]*[\"']?(?P<k>" + _SECRET_NAME + r")[\"']?[ \t]*\r?\n[ \t]*(?:-[ \t]+)?value:[ \t]*)"
    r"(?:\"(?P<d>[^\"\r\n]*)\"|'(?P<s>[^'\r\n]*)'|(?P<u>[^\s#][^\r\n]*))", re.I)
_JSON_VALUE = r"(?:\"(?P<d>(?:[^\"\\\n]|\\.)*)\"|(?P<u>-?\d[\d.]*))"
RE_SECRET_NV = re.compile(
    r"(?P<pre>\"(?:name|key)\"\s{0,20}:\s{0,20}\"(?P<k>" + _SECRET_NAME + r")\"\s{0,20},\s{0,40}\"value\"\s{0,20}:\s{0,20})" + _JSON_VALUE, re.I)
RE_SECRET_VN = re.compile(
    r"(?P<pre>\"value\"\s{0,20}:\s{0,20})" + _JSON_VALUE
    + r"(?=\s{0,20},\s{0,40}\"(?:name|key)\"\s{0,20}:\s{0,20}\"(?P<k>" + _SECRET_NAME + r")\")", re.I)
# one key and one value on a line: `vault kv get` (`password    x`), psql \x (`password | x`). The value is one cell: no `|`,
# no run of two blanks (`kubectl get secrets` rows have more columns).
RE_SECRET_LINE = re.compile(
    r"(?P<pre>^[ \t]*(?=[A-Za-z])(?P<k>" + _SECRET_NAME + r")(?:[ \t]*\|[ \t]*|[ \t]{2,}))"
    r"(?P<u>[^\s|](?:[^\s|]|[ \t](?![ \t]))*)(?=[ \t]*\r?$)", re.I | re.M)
# .netrc: machine <host> login <user> password <pw> (one line or several)
RE_SECRET_NETRC = re.compile(
    r"(?P<pre>(?:\bmachine[ \t]+\S+|\bdefault)(?:\s+(?:login|account|port)[ \t]+\S+){0,3}\s+password[ \t]+)(?P<u>\S+)")
# psql / mysql result tables: a header row with a credential column, a ----+---- rule, then the rows
RE_TABLE_RULE = re.compile(r"[ \t]*\+?-{2,}(?:\+-{2,})+\+?[ \t]*\r?")
RE_SECRET_COLUMN = re.compile(r"[ \t]*(?P<k>" + _SECRET_NAME + r")[ \t]*", re.I)
# The same markers as plain substrings of the lower-cased text: the credential pass runs only when one is present. A substring
# search per marker is far cheaper than one big case-insensitive alternation over megabytes of clean log.
# Turkish locale data: the Turkish words in the marker list below (sifre, parola, anahtar) are matching input (the masker reads Turkish tool output), not untranslated prose.
SECRET_MARKERS = tuple(
    "pass pwd secret token key authorization private sifre şifre parola anahtar hvs. hvb. ghp_ gho_ ghu_ ghs_ ghr_ github_pat_ "
    "cfut_ akia asia xox cookie eyj sk- glpat- aiza _live_ _test_ npm_ hooks.slack --user -u -U sig= signature x-amz- x-goog- "
    "machine begin private-lines bearer credential basicauth".lower().split())


def _secret_candidate(t):
    tl = t.lower()
    return any(x in tl for x in SECRET_MARKERS)


SECRET_VALUE_EXCLUDED = frozenset(("null", "true", "false", "none", "nil", "undefined", "required", "optional", "string", "empty",
                            "redacted", "hidden", "masked"))


# A value is skipped ONLY when the WHOLE value is clearly a placeholder. Real passwords start with `$`, `!`, `%`, `<` or look like
# code (`Admin(2026)`, `a.b.c`), so neither the first character nor a code shape is a reason to skip.
_SECRET_SEG = r"[A-Z]+[0-9]{0,2}"  # a name segment: letters, at most two digits (`S3`, `OAUTH2`); `ADMIN2026` looks like a password
_SECRET_VAR_NAME = _SECRET_SEG + r"(?:_[A-Z0-9]{1,12})*"
SECRET_PLACEHOLDER = re.compile(
    r"\$\{[A-Za-z_][\w.\-]*\}"  # ${DB_PASS}, ${secrets.pw}
    r"|\$" + _SECRET_VAR_NAME + r"|\$\(" + _SECRET_VAR_NAME + r"\)"  # $DB_PASS, $(DB_PASS): upper-case names; `$ADMIN2026`, `$ecretPass1` are passwords
    r"|<(?:[a-z]+(?:[ _.\-][a-z0-9]+)*|[A-Z]+(?:[ _.\-][A-Z0-9]+)*)>"  # <your-password>, <DB_PASS>; `<Secret123>` is a password
    r"|%(?:" + _SECRET_VAR_NAME + r"|[a-z]+(?:_[a-z0-9]+)*)%"  # %DB_PASS%, %appdata%; `%Pass9%` is a password
    r"|\{\{[^{}]*\}\}"  # {{ secrets.pw }}
    r"|\[KEY:masked\]|\[[A-Z_]+:[0-9a-f]{6,8}\]"  # already masked ([SECRET:xxxxxx], [PHONE:xxxxxx] ...; 6 hex from older versions, 8 now)
    r"|[*\u2022xX.#\-_]+")  # ****, xxxx


def _skip_secret(v):
    return len(v) < 4 or v.lower() in SECRET_VALUE_EXCLUDED or SECRET_PLACEHOLDER.fullmatch(v) is not None


def _secret_closing(v):
    """Split trailing closing brackets that do not belong to the value off the value (`(password=hunter22)`, `{"x": password=...}`);
    brackets that close an opener inside the value (`Admin(2026)`) stay in it. One pass: the counts are taken once and
    decremented while closers are removed (the old version recounted for every bracket, quadratic on `password=a` + ")))...")."""
    k = len(v)
    is_open = {c: v.count(c) for c in "([{"}
    closed = {c: v.count(c) for c in ")]}"}
    es = {")": "(", "]": "[", "}": "{"}
    while k and v[k - 1] in ")]}":
        c = v[k - 1]
        if is_open[es[c]] >= closed[c]:
            break
        closed[c] -= 1
        k -= 1
    return v[:k], v[k:]


RE_SECRET_LINE_HEAD = re.compile(r"[ \t]*(?:[#;\-*]|//)?[ \t]*(?:(?:export|set|setx)[ \t]+)?[\w.\-]*")
RE_SECRET_ADJACENT = re.compile(r"\S+?(?:=>|:=|[:=])[ \t]*")


def _secret_line_end(m):
    """The rest of the line after an unquoted value (with the value's own text), or "" . An assignment at the start of a line
    (`SmtpPassword=Ab9 xY 12`, `password: a b c`, `export PW=..`) with no blank before the `=` / `:` owns the whole rest of the
    line when the value stops at a blank: the secret may contain both, and the old rule left the tail as a readable
    fragment. A value that stops at `;` or a quote (connection string, code) and code-style `password = expr` (blanks around
    `=`) keep the short rule."""
    t, e = m.string, m.end("u")
    if e >= len(t) or t[e] not in " \t" or not RE_SECRET_STRONG.search(m.group("k")):
        return ""  # only a blank ends the value (a comma run-on keeps the old rule) and only a strong key (`Pass: 1234 Failed: 0` is a test summary)
    ls = t.rfind("\n", 0, m.start()) + 1
    if not RE_SECRET_LINE_HEAD.fullmatch(t, ls, m.start("k")):
        return ""
    if not RE_SECRET_ADJACENT.fullmatch(m.group("pre"), m.start("k") - m.start()):
        return ""
    le = t.find("\n", e)
    return t[e:le if le >= 0 else len(t)].rstrip("\r")


def _mask_out_secret(t, say):
    def shape(m):
        say["SECRET"] += 1
        return _token("SECRET", m.group(0))

    kv_end_at = [0]

    def kv(m):
        k = m.group("k")
        if not _is_secret_key(k) or "uthorization" in k or "UTHORIZATION" in k:
            return m.group(0)  # metadata key; an Authorization value is RE_SECRET_AUTH's job
        g = next((x for x in ("d", "s", "e", "e3", "e4", "e5", "u") if m.group(x) is not None), None)
        v = m.group(g)
        last = ""
        if g == "u":
            ek = ""
            if not (v.lower() in SECRET_VALUE_EXCLUDED or SECRET_PLACEHOLDER.fullmatch(v) or _skip_secret_value(k, v)):
                ek = _secret_line_end(m)
                if ek and SECRET_PLACEHOLDER.fullmatch((v + ek).rstrip()):
                    ek = ""  # the whole line is a placeholder with blanks (`{{ secrets.pw }}`, `<db password>`)
            if ek:
                v, last = v + ek.rstrip(), ek[len(ek.rstrip()):]
                kv_end_at[0] = m.end("u") + len(ek)  # the match ends before the tail: the caller skips the text up to here
            else:
                if _skip_secret(v):
                    return m.group(0)
                v, last = _secret_closing(v)
        if _skip_secret(v) or _skip_secret_value(k, v):
            return m.group(0)
        say["SECRET"] += 1
        tok = _token("SECRET", v)
        if g == "u":
            return m.group("pre") + (m.group("sch") or "") + tok + last
        a, b = m.start(g), m.end(g)
        return m.group(0)[:a - m.start()] + tok + m.group(0)[b - m.start():]

    def group(g):
        """Replace only group g of the match (value of an XML element / attribute, URL password, YAML next-line value)."""
        def f(m):
            v = m.group(g)
            if v is None:
                return m.group(0)
            if g == "v" and m.re is RE_SECRET_YAML:
                if not _is_secret_key(m.group("k")):
                    return m.group(0)
                c = re.search(r"[ \t]#", v)
                if c:
                    v = v[:c.start()]
            ic = v.strip()
            if _skip_secret(ic) or (m.re is RE_SECRET_CURL and _skip_secret_value("", ic)):
                return m.group(0)
            ba = m.start(g) - m.start() + (len(v) - len(v.lstrip()))
            say["SECRET"] += 1
            return m.group(0)[:ba] + _token("SECRET", ic) + m.group(0)[ba + len(ic):]
        return f

    def value(m):
        """Value group d (double-quoted), s (single-quoted) or u (bare) of a key / value structure."""
        try:
            k = m.group("k")
        except IndexError:
            k = None
        if k is not None and not _is_secret_key(k):
            return m.group(0)
        g = next((x for x in ("d", "s", "u") if x in m.re.groupindex and m.group(x) is not None), None)
        if g is None:
            return m.group(0)
        v = m.group(g).rstrip()
        if _skip_secret(v) or _skip_secret_value(k or "", v):
            return m.group(0)
        say["SECRET"] += 1
        a = m.start(g) - m.start()
        return m.group(0)[:a] + _token("SECRET", v) + m.group(0)[a + len(v):]

    def auth(m):
        v = m.group("v").rstrip()
        if _skip_secret(v):
            return m.group(0)
        say["SECRET"] += 1
        return m.group("pre") + _token("SECRET", v) + m.group(0)[len(m.group("pre")) + len(v):]

    def code(m):
        """Value literal of a code pair (`Add("X-API-Key", "v")`, `NetworkCredential("u", "v")`); a scheme word (`Bearer `) stays."""
        k = m.groupdict().get("k")
        if k is not None and k.lower() not in ("bearer", "basic") and not RE_SECRET_TITLE_FULL.fullmatch(k) and not _is_secret_key(k):
            return m.group(0)
        g = next((x for x in ("vb", "d", "s") if m.group(x) is not None), None)
        if g is None:
            return m.group(0)
        v = m.group(g)
        sch = RE_SECRET_SCHEMA.match(v)
        off = sch.end() if sch else 0
        v2 = v[off:]
        if (_skip_secret(v2) or _skip_secret_value(k or "", v2) or v2.lower() in ("bearer", "basic", "token", "digest")
                or RE_SECRET_INTERPOLATE.fullmatch(v2)):
            return m.group(0)
        say["SECRET"] += 1
        a = m.start(g) - m.start() + off
        return m.group(0)[:a] + _token("SECRET", v2) + m.group(0)[a + len(v2):]

    def cookie(m):
        first = [True]

        def double(c):
            v = c.group("v").strip()
            if not (first[0] or not m.group("set")) or _skip_secret(v):
                first[0] = False
                return c.group(0)
            first[0] = False
            say["SECRET"] += 1
            return c.group("a") + c.group("v").replace(v, _token("SECRET", v), 1)
        return m.group("pre") + RE_COOKIE_DOUBLE.sub(double, m.group("v"))

    def url_token(m):
        w = m.group("w")
        if not (re.search(r"\d", w) and re.search(r"[A-Za-z]", w)) or _skip_secret(w):
            return m.group(0)
        say["SECRET"] += 1
        return m.group("pre") + _token("SECRET", w)

    if "\\u" in t:
        t = _resolve_key(t)
    url = "://" in t and "@" in t
    if not (url or _secret_candidate(t)):
        return t
    for marker, pattern in RE_SECRET_SHAPE:
        if any(x in t for x in marker):
            t = pattern.sub(shape, t)
    if url:
        t = RE_SECRET_URL.sub(group("w"), t)
        t = RE_SECRET_URL_TOKEN.sub(url_token, t)
    if "<" in t:
        t = RE_SECRET_XML[0].sub(group("v"), t)
        t = RE_SECRET_XML[1].sub(group("v"), RE_SECRET_XML[1].sub(group("v2"), t))
        t = RE_SECRET_XML[2].sub(group("v"), t)
    min_ = t.lower()
    if "uthorization" in min_:
        t = RE_SECRET_AUTH.sub(auth, t)
    if "cookie" in min_:
        t = RE_SECRET_COOKIE.sub(cookie, t)
    if "-u" in t or "-U" in t:
        t = RE_SECRET_CURL.sub(group("v"), t)
    if "=" in t and ("?" in t or "&" in t):
        t = RE_SECRET_QUERY.sub(group("v"), t)
    if "value" in min_:
        if "name" in min_:
            t = RE_SECRET_K8S.sub(value, t)
        if '"' in t:
            t = RE_SECRET_VN.sub(value, RE_SECRET_NV.sub(value, t))
    if "machine" in min_ or "default" in min_:
        t = RE_SECRET_NETRC.sub(value, t)
    if "," in t or "]" in t:
        t = RE_SECRET_CODE.sub(code, t)
    if "redential" in t or "asicAuth" in t:
        t = RE_SECRET_CODE_IDENTITY.sub(code, t)
    output, son_yer = [], 0
    for m in RE_SECRET_KV.finditer(t):  # like sub(), but kv may consume the rest of the line (see _secret_line_end)
        if m.start() < son_yer:
            continue
        kv_end_at[0] = 0
        r = kv(m)
        output += (t[son_yer:m.start()], r)
        son_yer = max(m.end(), kv_end_at[0])
    output.append(t[son_yer:])
    t = "".join(output)
    if ":" in t and "\n" in t:
        t = RE_SECRET_YAML.sub(group("v"), t)
    t = RE_SECRET_LINE.sub(value, t)
    if "|" in t and "--" in t:
        t = _mask_out_table(t, say)
    return t


def _mask_out_table(t, say):
    """psql / mysql result table: the cells under a credential column (`password`, `api_key`, `token_hash` ...) are masked."""
    lines = t.split("\n")
    changed = False
    i = 1
    while i < len(lines):
        if not ("|" in lines[i - 1] and RE_TABLE_RULE.fullmatch(lines[i])):
            i += 1
            continue
        title = lines[i - 1].split("|")
        order = []
        for j, h in enumerate(title):
            mm = RE_SECRET_COLUMN.fullmatch(h.rstrip("\r"))
            if mm and _is_secret_key(mm.group("k")):
                order.append(j)
        i += 1
        if not order:
            continue
        while i < len(lines) and ("|" in lines[i] or RE_TABLE_RULE.fullmatch(lines[i])):
            cell = lines[i].split("|")
            if len(cell) == len(title):
                for j in order:
                    v = cell[j].strip()
                    if v and not _skip_secret(v):
                        say["SECRET"] += 1
                        cell[j] = cell[j].replace(v, _token("SECRET", v), 1)
                        changed = True
                lines[i] = "|".join(cell)
            i += 1
    return "\n".join(lines) if changed else t


def _resolve_key(t):
    """Decode unicode escapes inside JSON keys that look like credential keys (`"passw\\u006frd"`), so the key can be matched."""
    def f(m):
        k = m.group(1)
        if "\\u" not in k:
            return m.group(0)
        d = RE_UESC.sub(lambda x: chr(int(x.group(1), 16)), k)
        if not RE_SECRET_PRECHECK.search(d) or re.search(r'["\\\x00-\x1f\ud800-\udfff]', d):
            return m.group(0)
        return '"' + d + '"'
    return RE_ESC_KEY.sub(f, t)


# Terminal colour / cursor sequences. An SGR code ends in `m` (a word character), so it hides the boundary in front of a number
# or a key (`ESC[32m<TR_ID>ESC[0m`, `ESC[1mpasswordESC[0m=...`): the text is matched with them removed.
RE_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x9b[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b\n]{0,2000}(?:\x07|\x1b\\)?|\x1b[()][0-9A-Za-z]|\x1b[@-Z\\-_]")
# invisible characters between digits (`1234<ZWSP>5678901`, LRM, NUL, backspace, CR, combining marks): every Unicode Cf / Cc /
# Mn / Me character except newline and tab is removed there. Blanks and dashes stay: the patterns treat them as separators.
RE_BETWEEN = re.compile(r"(?<=\d)[^\w\n\t !-~]{1,8}(?=\d)")
RE_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_INVISIBLE_KIND = frozenset(("Cf", "Cc", "Mn", "Me"))


def _drop_between(m):
    g = m.group(0)
    return "" if all(unicodedata.category(c) in _INVISIBLE_KIND for c in g) else g


RE_DIGIT_OTHER = re.compile(r"(?![0-9])\d")  # a decimal digit that is not ASCII (fullwidth, Arabic-Indic, Devanagari ...)
_DIGIT = None


def _digit_table():
    global _DIGIT
    if _DIGIT is None:
        _DIGIT = {cp: 48 + unicodedata.decimal(chr(cp)) for cp in range(0x660, 0x1FC00) if chr(cp).isdecimal()}
    return _DIGIT


def _normalle(t):
    """Text as the patterns should see it: colour codes and invisible (Cf/Cc/Mn/Me) characters between digits removed,
    non-ASCII decimal digits written as ASCII digits (one for one)."""
    if "\x1b" in t or "\x9b" in t:
        t = RE_ANSI.sub("", t)
    if not t.isascii() or RE_CONTROL.search(t):
        t = RE_BETWEEN.sub(_drop_between, t)
    if not t.isascii():
        if RE_DIGIT_OTHER.search(t):
            t = t.translate(_digit_table())
    return t


_KEY_MISSING = False
_KEY_TEXT = None


def _key_text():
    """The HMAC key as text, for masking its literal. Read WITHOUT creating the file (once per process); None when there is no key."""
    global _KEY_MISSING, _KEY_TEXT
    if _KEY_TEXT:
        return _KEY_TEXT
    k = _KEY
    if not k and not _KEY_MISSING:
        try:
            with open(KEY_PATH, "rb") as f:
                k = f.read().strip()
        except OSError:
            _KEY_MISSING = True
            k = None
    if k and len(k) >= 32:
        _KEY_TEXT = k.decode("ascii", "replace")
        return _KEY_TEXT
    return None


# a run of hex pairs with up to three separators between pairs (blank, tab, newline, `,`, `:`, `-`) and an optional `0x` / `\x`
# per byte: `ab cd`, `ab:cd`, `abcd ef01`, xxd / hexdump -C columns, C arrays `{0x30, 0x31}`, Python bytes `b'\x30\x31'`
RE_HEX_ARRAY = re.compile(r"(?<![0-9A-Za-z])(?:0[xX]|\\x)?[0-9a-fA-F]{2}(?:[\s,:\-]{0,3}(?:0[xX]|\\x)?[0-9a-fA-F]{2}){7,}")
RE_HEX_DELIMITER = re.compile(r"0[xX]|\\x|[\s,:\-]")
_KEY_WINDOW = None


def _key_windows(k):
    """16-character windows of the key text and of its bytes in hex (what xxd / hexdump print for the key file)."""
    global _KEY_WINDOW
    if _KEY_WINDOW is None:
        a, b = k.lower(), k.encode("utf-8", "replace").hex()
        _KEY_WINDOW = frozenset({a[i:i + 16] for i in range(len(a) - 15)} | {b[i:i + 16] for i in range(len(b) - 15)})
    return _KEY_WINDOW


def _mask_out_key(t, say):
    k = _key_text()
    if not k:
        return t
    if k.lower() in t.lower():
        t, n = re.subn(re.escape(k), "[KEY:masked]", t, flags=re.I)
        say["KEY"] += n
    window = _key_windows(k)

    def group(m):
        d = RE_HEX_DELIMITER.sub("", m.group(0)).lower()
        if (any(d[i:i + 16] in window for i in range(len(d) - 15)) if len(d) <= 4096 else any(w in d for w in window)):
            say["KEY"] += 1
            return "[KEY:masked]"
        return m.group(0)
    return RE_HEX_ARRAY.sub(group, t)


# Encoded forms of an identifier: percent (`%40`, `%2B90`), HTML entities (`&#64;`, `&#x40;`, `&commat;`) and `\uXXXX` escapes.
RE_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}|&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});|\\u[0-9A-Fa-f]{4}")
RE_PERCENT = re.compile(r"(?:%[0-9A-Fa-f]{2})+")
RE_ASSET = re.compile(r"&(?:#([0-9]{1,7})|#[xX]([0-9A-Fa-f]{1,6})|([A-Za-z][A-Za-z0-9]{1,31}));")
RE_UESC_CHAIN = re.compile(r"(?:\\u[0-9A-Fa-f]{4})+")


def _safe(d):
    """A decoded part is used only when it adds no line break, control character or lone surrogate."""
    return all(ord(c) >= 0x20 and ord(c) != 0x7f and not 0xD800 <= ord(c) <= 0xDFFF for c in d)


def _resolve_percent(m):
    try:
        d = bytes.fromhex(m.group(0).replace("%", "")).decode("utf-8")
    except ValueError:
        return m.group(0)
    return d if _safe(d) else m.group(0)


def _resolve_asset(m):
    if m.group(3):
        import html.entities
        d = html.entities.html5.get(m.group(3) + ";")
        return d if d and _safe(d) else m.group(0)
    cp = int(m.group(1)) if m.group(1) else int(m.group(2), 16)
    if cp > 0x10FFFF:
        return m.group(0)
    d = chr(cp)
    return d if _safe(d) else m.group(0)


def _resolve_uesc(m):
    try:
        d = json.loads('"' + m.group(0) + '"')
    except ValueError:
        return m.group(0)
    return d if _safe(d) else m.group(0)


def _resolve(t):
    """Decode percent / entity / \\u forms (three passes at most, for double encoding such as `%2540`)."""
    for _ in range(3):
        y = t
        if "%" in y:
            y = RE_PERCENT.sub(_resolve_percent, y)
        if "&" in y:
            y = RE_ASSET.sub(_resolve_asset, y)
        if "\\u" in y:
            y = RE_UESC_CHAIN.sub(_resolve_uesc, y)
        if y == t:
            break
        t = y
    return t


_RAW = None  # while set (a list), the identifier callbacks record (raw text, token) for every value they mask
RE_TOKEN = re.compile(r"\[([A-Z_]+):[0-9a-f]{8}\]")


def _record(ham, tok):
    if _RAW is not None:
        _RAW.append((ham, tok))
    return tok


def _coded_lines(t, say):
    """A line with an encoded form is decoded in a copy and masked alone; when that finds an identifier, the DECODED line takes
    its place (and is masked with the rest). Lines where decoding reveals nothing stay exactly as they were.
    Decoding can glue a character onto an identifier the original line had (`&#48;<TR_ID>` becomes a 12-digit run): every value
    the original masking caught whose token is missing from the decoded result is replaced by its token in the decoded line; if
    that raw text is no longer there either, the original line is kept (masked as usual)."""
    global _RAW
    if not RE_ESCAPE.search(t):
        return t
    lines = t.splitlines(keepends=True)
    changed = False
    for i, s in enumerate(lines):
        if not RE_ESCAPE.search(s):
            continue
        d = _resolve(s)
        if d == s:
            continue
        md = _mask_out_core(d, collections.Counter())
        if md == d:
            continue
        ham = []
        _RAW = ham
        try:
            _mask_out_core(s, collections.Counter())
        finally:
            _RAW = None
        present = {m.group(0) for m in RE_TOKEN.finditer(md)}
        for h, tok in ham:
            if tok in present:
                continue
            if h not in d:
                d = None
                break
            d = d.replace(h, tok)
            say[RE_TOKEN.match(tok).group(1)] += 1
        if d is not None:
            lines[i] = d
            changed = True
    return "".join(lines) if changed else t


def _mask_out_part(t, say):
    """Mask one piece. The patterns run on the normalised text and the masked NORMALISED text is what comes back, always: the
    original is never returned, because the normaliser removes parts (colour codes, OSC payloads) that were never scanned."""
    n = _coded_lines(_mask_out_key(_normalle(t), say), say)
    return _mask_out_core(n, say)


def _mask_out_core(t, say):
    permission_field, permission_tel = _permissions()
    t = _mask_out_secret(t, say)

    def iban(m):
        s = re.sub(r"[^0-9A-Za-z]", "", m.group(0))
        if not _iban_valid(s):
            return m.group(0)
        say["IBAN"] += 1
        return _record(m.group(0), _token("IBAN", s.upper()))

    def card(m):
        s = re.sub(r"\D", "", m.group(0))
        if not _card_valid(s) or _id_context(m.string, m.start()):
            return m.group(0)
        say["CARD"] += 1
        return _record(m.group(0), _token("CARD", s))

    def mail(m):
        a = m.group(0)
        field = a.rsplit("@", 1)[1].lower()
        last = field.rsplit(".", 1)[1]
        if last in EMAIL_SUFFIX_EXCLUDED or any(field == x or field.endswith("." + x) for x in permission_field):
            return a
        say["EMAIL"] += 1
        return _record(a, _token("EMAIL", a.lower()))

    def tr_id(m):
        s = m.group(0)
        r = re.sub(r"\D", "", s)
        if len(r) != 11 or not _tr_id_valid(r) or _id_context(m.string, m.start()):
            return s
        if _GH_IDENTITY and m.re is RE_TR_ID and _gh_location(m.string, m.start(), m.end()):
            return s
        if m.re is RE_TR_ID_GROUPED and m.group(1) == "." and all(int(x) <= 255 for x in s.split(".")):
            return s  # 192.0.2.25: an IPv4 address, not a dotted 3-3-3-2 TR_ID
        say["TR_ID"] += 1
        return _record(s, _token("TR_ID", r))

    def tel(skip_id_context):
        def f(m):
            s = m.group(0)
            r = re.sub(r"\D", "", s)
            if r.startswith("0090"):
                r = r[4:]
            r = r[-10:]
            if len(r) != 10 or r in permission_tel:
                return s
            if len(s) == 10 and s[0] == "5" and s.isdigit() and not _tel_context(m.string, m.start()):
                return s  # bare 5XXXXXXXXX without a phone-like key: a size / count / row number
            if skip_id_context and not s.startswith(("0", "+", "(")) and _id_context(m.string, m.start()):
                return s
            say["PHONE"] += 1
            return _record(s, _token("PHONE", r))
        return f

    if re.search(r"(?i)tr\d\d", t):
        t = RE_IBAN.sub(iban, t)
    if re.search(r"\d{4}", t):
        t = RE_CARD.sub(card, t)
    if "@" in t:
        t = RE_MAIL.sub(mail, t)
    if re.search(r"\d{11}", t):
        t = RE_TR_ID.sub(tr_id, t)
    if re.search(r"\d{3}\D{1,3}\d{3}", t):
        t = RE_TR_ID_GROUPED.sub(tr_id, t)
    if re.search(r"\d{2}", t):
        t = RE_PHONE_COUNTRY.sub(tel(True), t)
        t = RE_PHONE_ZERO.sub(tel(True), t)
        t = RE_PHONE_POINT.sub(tel(True), t)
        t = RE_PHONE_PAREN.sub(tel(False), t)
        t = RE_PHONE_CEP.sub(tel(True), t)
    return t


class _TimeOverrun(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _TimeOverrun()


class _time_cap:
    """Interrupt the body with _TimeOverrun after `seconds` seconds (SIGALRM; CPython's regex engine checks signals while it
    matches). This is the second line of defence behind the linear-time patterns; it only works in the main thread of a
    POSIX process and silently does nothing elsewhere (the piece-wise budget check still applies)."""

    def __init__(self, seconds):
        self.seconds, self.old, self.enabled = seconds, None, False

    def __enter__(self):
        try:
            import signal
            self.old = signal.signal(signal.SIGALRM, _alarm_handler)
            signal.setitimer(signal.ITIMER_REAL, max(self.seconds, 0.001))
            self.enabled = True
        except Exception:
            pass
        return self

    def __exit__(self, *a):
        if self.enabled:
            import signal
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self.old)
        return False


_BLOCKS = (("-----BEGIN ", "-----END "), ("Private-Lines:", "Private-MAC:"))
RE_NAME_LINE = re.compile(r"[ \t]*(?:-[ \t]+)?[\"']?(?:name|key)[\"']?[ \t]*:", re.I)


def _parts(text, size=None):
    """(start, end) pairs of pieces for piece-wise masking. A match must never straddle a cut, so a piece always ends after a
    newline (or at the end of the text): about PART characters, but a line longer than that is kept whole (the patterns are
    linear-time), a piece that ends inside a `-----BEGIN` block is extended past the matching `-----END` line (or to the end
    of the text when there is none, as the pattern itself would mask it), and a piece never ends between a `key:` line and its value line;
    both rules repeat until the cut stops moving. mask_out() withholds a piece over UNIT_MAX."""
    n, i = len(text), 0
    while i < n:
        j = min(i + (size or PART), n)
        if j < n:
            k = text.rfind("\n", i, j)
            if k > i:
                j = k + 1
            else:
                k = text.find("\n", j)
                j = n if k < 0 else k + 1
        # start of the last opener of each block kind in the piece; -1 once its block is closed or when there is none
        bs = [text.rfind(on_, i, j) for on_, _ in _BLOCKS]
        while j < n:
            # two rules widen a piece; repeat both until neither moves the cut (a widened piece can open a new block / key line):
            # 1. it must not end inside a -----BEGIN ... block (or a PuTTY Private-Lines part): extend past the closing line
            #    (or to the end of the text, as the pattern itself would mask it)
            # 2. a line ending in `key:` (or a `name: <credential key>` line) belongs together with the value line after it
            j0 = j
            for b, (on_, close) in enumerate(_BLOCKS):
                if bs[b] >= 0 and text.find(close, bs[b], j) < 0:
                    e = text.find(close, j)
                    k = -1 if e < 0 else text.find("\n", e)
                    j = n if k < 0 else k + 1
                    break
                bs[b] = -1
            else:
                ls = text.rfind("\n", i, j - 1) + 1
                last = text[ls:j].rstrip()
                if (last.endswith(":") or RE_NAME_LINE.match(last)) and RE_SECRET_PRECHECK.search(last):
                    k = text.find("\n", j)
                    j = n if k < 0 else k + 1
            if j == j0:
                break
            for b, (on_, _) in enumerate(_BLOCKS):
                nb = text.rfind(on_, j0, j)  # only the newly added part can hold a new opener
                if nb >= 0:
                    bs[b] = nb
        yield i, j
        i = j


def _hidden(text, reason):
    line = text.count("\n") + (0 if text.endswith("\n") else 1)
    return f"[mask: {reason}, {line} lines withheld]\n"


SHORT = 2048  # a text this short is masked in one go (no piece splitting)
CACHE_LENGTH = 256  # short texts (dict keys, repeated values) are cached: a tree repeats the same keys millions of times
CACHE_MAX = 200_000
_CACHE = {}
_BUDGET_DONE = False  # the process-wide budget (_END_AT) is spent: _mask_out_tree stops and the rest of the tree is withheld


def _mask_out_short(text, end_at):
    global _BUDGET_DONE
    if time.monotonic() >= end_at:
        if _END_AT is not None:
            _BUDGET_DONE = True
        return _hidden(text, "time budget exceeded"), collections.Counter({"SKIPPED": 1})
    key = (text, _LEAF_KEY, _GH_IDENTITY) if len(text) <= CACHE_LENGTH else None
    if key is not None:
        o = _CACHE.get(key)
        if o is not None:
            return o[0], collections.Counter(o[1])
    say = collections.Counter()
    try:
        with _time_cap(end_at - time.monotonic()):  # the interrupt stays: a runaway pattern can stall on short text too
            y = _mask_out_part(text, say)
    except _TimeOverrun:
        if _END_AT is not None:
            _BUDGET_DONE = True
        return _hidden(text, "time budget exceeded"), collections.Counter({"SKIPPED": 1})
    except Exception:
        _count_log_error("mask-error")
        return _hidden(text, "masking error"), collections.Counter({"SKIPPED": 1})
    if key is not None:
        if len(_CACHE) >= CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = (y, dict(say))
    return y, say


def mask_out(text):
    """Return (masked_text, Counter). Never raises and never lets unmasked text through (fail closed): text is masked piece by
    piece; when the time budget runs out (or the interrupt fires inside a piece) the rest of the text is replaced by ONE notice
    line, and a piece that raises is replaced by a notice. Counter key SKIPPED counts withheld parts."""
    global _BUDGET_DONE
    say = collections.Counter()
    if not isinstance(text, str) or not text or os.environ.get("TRIM_MASK_CLOSED") == "1":
        return text, say
    end_at = _END_AT if _END_AT is not None else time.monotonic() + MASK_DURATION
    if len(text) <= SHORT:
        return _mask_out_short(text, end_at)
    output = []
    alarm = _alarm_var()
    size = PART if alarm else min(PART, PART_NO_ALARM)
    unit_max = UNIT_MAX if alarm else min(UNIT_MAX, UNIT_NO_ALARM)
    try:
        for a, b in _parts(text, size):
            remaining = end_at - time.monotonic()
            if remaining <= 0:
                output.append(_hidden(text[a:], "time budget exceeded"))
                say["SKIPPED"] += 1
                _BUDGET_DONE = _END_AT is not None
                break
            if b - a > unit_max:
                output.append(_hidden(text[a:b], "line too long"))
                say["SKIPPED"] += 1
                continue
            try:
                with _time_cap(remaining):
                    output.append(_mask_out_part(text[a:b], say))
            except _TimeOverrun:
                output.append(_hidden(text[a:], "time budget exceeded"))
                say["SKIPPED"] += 1
                _BUDGET_DONE = _END_AT is not None
                break
            except Exception:
                _count_log_error("mask-error")
                output.append(_hidden(text[a:b], "masking error"))
                say["SKIPPED"] += 1
    except BaseException:  # e.g. the interrupt fired between pieces
        _count_log_error("mask-error")
        return "[mask: masking error, output withheld]\n", collections.Counter({"SKIPPED": 1})
    return "".join(output), say


def _alarm_var():
    """Can SIGALRM interrupt a piece? Only in the main thread of a POSIX process."""
    try:
        import signal
        import threading
        return hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
    except Exception:
        return False


def _start_budget():
    """One masking budget for the whole process (the hook masks stdout, stderr and a persisted copy in turn)."""
    global _END_AT, _BUDGET_DONE
    _END_AT = time.monotonic() + MASK_DURATION
    _BUDGET_DONE = False


_NODE = [0]
BUDGET_NOTE = "[mask: time budget exceeded, the rest of the tool output is withheld]"


def _budget_is_done():
    """In the hook (process-wide budget): is the budget spent? Checked by the clock every 1024 tree nodes as well, so a tree of
    numbers (which never reaches mask_out) stops too."""
    global _BUDGET_DONE
    if _END_AT is None:
        return False
    if not _BUDGET_DONE:
        _NODE[0] += 1
        if not _NODE[0] & 1023 and time.monotonic() >= _END_AT:
            _BUDGET_DONE = True
    return _BUDGET_DONE


def _add_not(o):
    """After the tree walk stopped on the budget: ONE notice for everything that was left out."""
    if isinstance(o, dict):
        o = dict(o)
        if isinstance(o.get("content"), list):
            o["content"] = _add_not(o["content"])
        else:
            o["[mask]"] = BUDGET_NOTE
    elif isinstance(o, list):
        o = list(o) + ([{"type": "text", "text": BUDGET_NOTE}] if o and isinstance(o[0], dict) and "type" in o[0] else [BUDGET_NOTE])
    return o


def _mask_out_leaf(o, key):
    """Mask one string leaf; a short leaf gets its dict key as context (see _leaf_head)."""
    global _LEAF_KEY
    if key is None or len(o) > 1000:
        return mask_out(o)
    _LEAF_KEY = key
    try:
        return mask_out(o)
    finally:
        _LEAF_KEY = None


def _mask_out_count(o, key, say):
    """A JSON number that is TR_ID / phone / card shaped (`{"tckn": 12345678901}`) comes back as its masked text; None = unchanged."""
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
    if not (n == 11 and _tr_id_valid(t) or n in (10, 12) and t[0] in "59" or 13 <= n <= 19 and _card_valid(t)):
        return None  # cheap shape check first: a tree can hold millions of numbers
    y, s = _mask_out_leaf(t, key)
    if y == t:
        return None
    say.update(s)
    return y


def _mask_out_tree(o, say, key=None):
    """tool_response tree: string leaves and identifier-shaped numbers are masked, structure and other types stay as they are.
    The dict key of a leaf is its context (`run_id` skips, `tel` makes a bare 5XXXXXXXXX a phone). Returns (new, changed)."""
    if isinstance(o, str):
        y, s = _mask_out_leaf(o, key)
        say.update(s)
        return (y, True) if y != o else (o, False)
    if isinstance(o, (int, float)) and not isinstance(o, bool):
        y = _mask_out_count(o, key, say)
        return (o, False) if y is None else (y, True)
    if isinstance(o, list):
        result, ch = [], False
        for x in o:
            if _budget_is_done():
                say["SKIPPED"] += 1
                ch = True
                break
            y, c = _mask_out_tree(x, say, key)
            result.append(y)
            ch = ch or c
        return (result, True) if ch else (o, False)
    if isinstance(o, dict):
        if o.get("type") == "image":
            # image block: the base64 `data` stays as it is (masking could break the picture); every other field is text
            ch = False
            result = dict(o)
            for k, x in o.items():
                if k not in ("type", "data", "mimeType", "media_type"):
                    y, c = _mask_out_tree(x, say, k if isinstance(k, str) else None)
                    if c:
                        result[k], ch = y, True
            return (result, True) if ch else (o, False)
        result, ch = {}, False
        for k, x in o.items():
            if _budget_is_done():
                # budget spent: only the envelope's fixed keys stay (value withheld), so the response keeps its shape; the rest
                # of this dict is dropped and _general / _bash add ONE notice for everything left out
                say["SKIPPED"] += 1
                ch = True
                if k in STRUCTURE_KEYS and k not in result:
                    if isinstance(x, str):
                        result[k] = "[mask: withheld]" if x else x
                    elif x is None or isinstance(x, bool):
                        result[k] = x
                continue
            y, c = _mask_out_tree(x, say, k if isinstance(k, str) else None)
            if isinstance(k, str):  # a key can carry data too ({"<id number>": ...})
                k2 = _hide_key(k, len(result), say)
                if k2 != k:
                    k, c = k2, True
                    while k in result:  # two keys that came out equal must not swallow one value
                        k = f"{k}~{len(result)}"
            result[k] = y
            ch = ch or c
        return (result, True) if ch else (o, False)
    return o, False


def _count_log_error(ne):
    _say_log("system", collections.Counter({ne: 1}))


def _say_log(tool, say):
    """Counts only (never values) into the count log."""
    try:
        if not say:
            return
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} tool={tool} " + " ".join(f"{k}={v}" for k, v in sorted(say.items())) + "\n"
        try:
            if os.path.getsize(COUNTER_LOG) > 1_000_000:
                with open(COUNTER_LOG, encoding="utf-8", errors="replace") as f:
                    last = f.readlines()[-2000:]
                with open(os.open(COUNTER_LOG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                    f.writelines(last)
        except OSError:
            pass
        with open(os.open(COUNTER_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _write_file_masked(path, text):
    """Replace the raw copy (Claude Code's persisted output file) by the masked text: temp file + atomic rename."""
    try:
        temp = f"{path}.mask{os.getpid()}"
        mod = os.stat(path).st_mode & 0o777
        with open(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mod or 0o600), "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(temp, path)
        return True
    except Exception:
        _count_log_error("file-write-failed")
        try:
            os.unlink(temp)
        except Exception:
            pass
        return False


def clean(now):
    try:
        for name in os.listdir(DIR):
            if name.endswith(".txt"):
                path = os.path.join(DIR, name)
                if now - os.path.getmtime(path) > 7 * 86400:
                    os.remove(path)
    except OSError:
        pass


def save(text, source=None):
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    now = time.time()
    clean(now)
    h = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8]
    path = os.path.join(DIR, f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{h}.txt")
    if source:  # Claude Code already wrote output over 30000 characters to a file: hard-link it instead of copying
        try:
            os.link(source, path)
            return path
        except OSError:
            pass
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8", errors="replace") as f:
        f.write(text)
    return path


def shorten(s):
    if len(s) <= LINE_LENGTH:
        return s
    m = ERROR_RE.search(s)
    if m and m.start() >= LINE_LENGTH - 100:  # error word would be cut off: open a window around it
        a = max(m.start() - 250, 0)
        return f"[+{a} chars]..." + s[a:a + LINE_LENGTH - 100] + f"... [+{max(len(s) - a - LINE_LENGTH + 100, 0)} chars]"
    return s[:LINE_LENGTH] + f"... [+{len(s) - LINE_LENGTH} chars]"


def trim(text, source=None):
    """Return (new_text, truncated). Below the threshold the text comes back unchanged and no file is written.
    source: path of a file that already holds the whole text (hook mode, persistedOutputPath)."""
    lines = text.split("\n")
    at_end_bos = lines and lines[-1] == ""
    if at_end_bos:
        lines = lines[:-1]
    n = len(lines)
    if n <= THRESHOLD_LINE and len(text.encode("utf-8", "replace")) <= THRESHOLD_BYTES:
        return text, False
    keep = set(range(min(HEAD, n))) | set(range(max(n - LAST, 0), n))
    middle = [i for i in range(n) if i not in keep and ERROR_RE.search(lines[i])]
    if len(middle) > ERROR_MAX:
        middle = middle[:ERROR_MAX // 2] + middle[-ERROR_MAX // 2:]
    keep |= set(middle)
    path = save(text, source)
    output = [f"[truncated: {n} lines, full output: {path}]"]
    previous = -1
    for i in sorted(keep):
        if i > previous + 1:
            output.append(f"[... lines {previous + 2}-{i}: {i - previous - 1} lines skipped ...]")
        output.append(shorten(lines[i]))
        previous = i
    return "\n".join(output) + ("\n" if at_end_bos else ""), True


MASK_READ_DIRS = ("/tmp/", os.path.expanduser("~/.claude/tool-output/"), os.path.expanduser("~/.claude/projects/"),
                       os.path.expanduser("~/Downloads/"), os.path.expanduser("~/downloads/"))


def _json_yaz(new):
    sys.stdout.buffer.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": new}},
                                       ensure_ascii=True).encode("ascii"))  # \uXXXX escapes: a lone surrogate cannot break it
    sys.stdout.buffer.write(b"\n")
    sys.stdout.flush()


def _bash(d):
    input_ = d.get("tool_input")
    command = input_.get("command") if isinstance(input_, dict) else None
    command = command if isinstance(command, str) else ""
    answer = d.get("tool_response")
    if not isinstance(answer, dict):
        return _general(d)  # not the usual {stdout, stderr, ...} shape: mask it as a tree
    so = answer.get("stdout")
    image = bool(answer.get("isImage")) and isinstance(so, str) and so.startswith("data:image/")
    skip = bool(SKIP_RE.search(command))
    global _GH_IDENTITY
    _GH_IDENTITY = _gh_command(command)
    trimming_closed = os.environ.get("TRIM_CLOSED") == "1"
    new = dict(answer)
    changed = False
    total = collections.Counter()
    for field in ("stdout", "stderr"):
        v = answer.get(field)
        if field == "stdout" and image:
            continue  # image data (base64): not text
        if v is not None and not isinstance(v, str):
            y, c = _mask_out_tree(v, total, field)
            if c:
                new[field], changed = y, True
                if _BUDGET_DONE and isinstance(y, (list, dict)):
                    new[field] = _add_not(y)
            continue
        source = None
        path = answer.get("persistedOutputPath") if field == "stdout" else None
        if isinstance(path, str) and os.path.isfile(path) and os.path.getsize(path) < 200_000_000:
            try:  # Claude Code gives the hook only the first 30000 characters; the whole output is in this file
                with open(path, encoding="utf-8", errors="replace") as f:
                    full, source = f.read(), path
            except OSError:
                full = None
        else:
            full = None
        if full is not None and (skip or trimming_closed):
            # no truncation: the model slices the file itself -> the masked text replaces the file in place, the preview is masked separately
            m, say = mask_out(full)
            if m != full and _write_file_masked(path, m):
                total.update(say)
            if isinstance(v, str) and v:
                mv, _ = mask_out(v)
                if mv != v:
                    new[field] = mv
                    changed = True
            continue
        if full is not None:
            v = full
        if not (isinstance(v, str) and v):
            continue
        m, say = mask_out(v)
        masked = m != v
        total.update(say)
        if masked and source:
            _write_file_masked(source, m)  # do not leave the raw copy on disk
        if skip or trimming_closed:
            y, k = m, False
        else:
            y, k = trim(m, None if masked else source)
        if masked or k:
            new[field] = y
            changed = True
            if source:
                new.pop("persistedOutputPath", None)
                new.pop("persistedOutputSize", None)
    for field, v in answer.items():  # every other field too (returnCodeInterpretation, future fields ...)
        if field in ("stdout", "stderr", "persistedOutputPath", "persistedOutputSize", "isImage", "interrupted"):
            continue
        y, c = _mask_out_tree(v, total, field if isinstance(field, str) else None)
        if c:
            new[field], changed = y, True
            if _BUDGET_DONE and isinstance(y, (list, dict)):
                new[field] = _add_not(y)  # a container field cut short says so inside itself
    # (no extra notice here: stdout / stderr already carry their inline notice, and the Bash envelope gets no invented key)
    _say_log("Bash", total)
    if changed:
        _json_yaz(new)


def _general(d):
    """MCP tools and Read under temporary directories: the texts in the tool_response tree are masked."""
    answer = d.get("tool_response")
    if answer is None:
        return
    say = collections.Counter()
    new, changed = _mask_out_tree(answer, say)
    if _BUDGET_DONE and not isinstance(new, str):
        new, changed = _add_not(new), True
    _say_log(str(d.get("tool_name"))[:80], say)
    if changed:
        _json_yaz(new)


HIDE_MAX = 20_000


def _hide(o, counter=None):
    """Replace every string leaf by a notice (fail closed after an unexpected error in the hook). A tree of more than
    HIDE_MAX nodes raises instead (the caller then emits one fixed notice): no per-leaf notices for millions of leaves."""
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if counter[0] > HIDE_MAX:
        raise OverflowError("tree too big")
    if isinstance(o, str):
        return "[mask: internal error, output withheld]" if o else o
    if isinstance(o, list):
        return [_hide(x, counter) for x in o]
    if isinstance(o, dict):
        if o.get("type") == "image":
            return o
        return {(_hide_key(k, i) if isinstance(k, str) else k): _hide(x, counter)
                for i, (k, x) in enumerate(o.items())}
    return o


# Fixed names of the tool-response envelope. Masking leaves them alone; they only matter when the budget is spent or masking
# fails, so the response keeps its shape (an exact match, never a pattern: any other key is a placeholder in that case).
STRUCTURE_KEYS = frozenset({"type", "text", "content", "stdout", "stderr", "isError", "structuredContent", "file", "filePath"})


def _hide_key(k, i, say=None):
    """EVERY string key is masked like a value (a lone surrogate becomes `?` first); there is no pre-filter that decides which
    keys may carry data. A key the masking leaves as it was (structural keys such as `type`, `content`, `isError`) is returned
    unchanged. When the masking budget is spent or masking fails, the key becomes a numbered placeholder - never the
    withheld-notice line, which would make keys collide."""
    try:
        k1 = k.encode("utf-8", "replace").decode("utf-8", "replace")
        k2, s2 = mask_out(k1)
        if s2.get("SKIPPED"):
            return k if k in STRUCTURE_KEYS else f"[mask: key {i}]"
        if k2 == k:
            return k
        if say is not None:
            say.update(s2)
        return k2
    except BaseException:
        return k if k in STRUCTURE_KEYS else f"[mask: key {i}]"


_LAST_RESORT_OUTPUT = b'{"hookSpecificOutput":{"hookEventName":"PostToolUse","updatedToolOutput":"[mask: internal error, output withheld]"}}\n'


def _son_care(answer):
    """Last resort when even the withholding output cannot be built or written: a fixed ASCII payload. The tool output must
    never reach the model raw, so the hook always emits something."""
    try:
        if isinstance(answer, dict) and ("stdout" in answer or "stderr" in answer):
            _json_yaz({"stdout": "[mask: internal error, output withheld]", "stderr": ""})
            return
    except BaseException:
        pass
    try:
        sys.stdout.buffer.write(_LAST_RESORT_OUTPUT)
        sys.stdout.flush()
    except BaseException:
        try:
            os.write(1, _LAST_RESORT_OUTPUT)
        except BaseException:
            pass


def hook():
    d, related = None, False
    try:
        data = sys.stdin.buffer.read()  # the budget starts after the input is complete (a slow producer must not eat it)
        _start_budget()
        text = data.decode("utf-8", "replace")
        if text.startswith("\ufeff"):
            text = text[1:]
        try:
            d = json.loads(text)
        except (ValueError, RecursionError):
            try:  # trailing data after the object
                d = json.JSONDecoder().raw_decode(text.lstrip())[0]
            except (ValueError, RecursionError):
                d = None
        if not isinstance(d, dict):
            # cannot tell which tool this is, so cannot tell whether it would have been masked: withhold (fail closed)
            _count_log_error("hook-unparseable")
            _son_care(None)
            return
        tool = d.get("tool_name")
        tool = tool if isinstance(tool, str) else ""
        if tool == "Bash":
            related = True
            _bash(d)
        elif tool.startswith("mcp__"):
            related = True
            _general(d)
        elif tool == "Read":
            path = os.path.realpath(str((d.get("tool_input") or {}).get("file_path") or ""))
            if path.startswith(MASK_READ_DIRS):
                related = True
                _general(d)
        else:  # Grep and any other tool the hook matcher is wired to
            related = True
            _general(d)
    except BaseException:
        _count_log_error("hook-error")
        try:  # a tool we would have masked: do not let its raw output through
            if related and isinstance(d, dict) and d.get("tool_response") is not None:
                try:
                    _json_yaz(_hide(d["tool_response"]))
                except BaseException:
                    _son_care(d["tool_response"])
        except BaseException:
            pass


def filter_fn():
    data = sys.stdin.buffer.read().decode("utf-8", "replace")
    _start_budget()  # after the input is read: `(sleep 8; echo ...) | trim.py` must not use up the budget while waiting
    try:
        data, _ = mask_out(data)
    except BaseException:
        data = "[mask: masking error, output withheld]\n"
    try:
        new, _ = trim(data)
    except Exception:
        new = data  # truncation failed: the text is already masked
    sys.stdout.write(new)


if __name__ == "__main__":
    if "--hook" in sys.argv[1:]:
        hook()
    else:
        filter_fn()

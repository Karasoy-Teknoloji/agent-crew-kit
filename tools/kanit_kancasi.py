#!/usr/bin/env python3
"""Stop / SubagentStop hook: block a final assistant message that (1) contains a hedge word (a guess dressed as a fact) or
(2) claims missing access ("yetki yok", "no token", "permission denied") or asks for a credential/permission without a
`kasa.py bul` Vault search in the same turn or earlier in the session (last few hours), or (3, Stop only) ends a turn whose
user prompt was a decision that the turn did not record on GitHub.

Team rules: WORKING-RULES.md "No probabilities in reports" / "Evidence before claims" / "Decisions live on GitHub", AUTONOMY.md
"Act, don't ask" (search the Vault before reporting missing access). This hook enforces them on the assistant's own turn.
Python 3 standard library only.

Contract (https://code.claude.com/docs/en/hooks, Stop and SubagentStop): the hook gets JSON on stdin (`transcript_path`,
`stop_hook_active`, `last_assistant_message`; SubagentStop also `agent_transcript_path`, `agent_id`, `agent_type`). Printing
{"decision": "block", "reason": ...} and exiting 0 keeps Claude (or the subagent) working and shows it the reason.

What counts: text of the last assistant message of the turn (text blocks only, never tool calls), with fenced code blocks,
`inline code`, ">" quote lines and "quoted" / “quoted” text removed. Matching is case-insensitive with Turkish letters
folded (I/ı/İ/i), on word boundaries.

Access-claim check: a claim pattern (see _CLAIM_SOURCES: OUR missing access/permission/credential, or a request to the user for
one) in the same prose (code/quotes removed) is allowed when the turn's tool calls include a Bash command running `kasa.py bul`
(the turn = the transcript after the last real user prompt; for SubagentStop the subagent's own `agent_transcript_path`), when
the message itself cites `kasa.py bul`, or when the same transcript shows a `kasa.py bul` run or citation within the last
BUL_MEMORY_HOURS (3 h, env KANIT_KANCASI_BUL_HOURS; session memory, see bul_in_session). A bare HTTP status (401/403) in a result
report about an external system is not a claim. An unreadable
transcript means "unknown" and never blocks.

Decision check (Stop only; subagents act on briefs, not on user decisions): the turn's prompt is a human prompt (origin "human",
not a task notification, command output or meta entry) that reads as a decision: a DECISION_MARKERS word, or a short APPROVALS
reply ("tamam", "evet", "aynen") to a previous assistant message that made a proposal (PROPOSAL_MARKERS). Questions (ending
with "?" or holding the particle mı/mi/mu/mü) are not decisions. The turn is satisfied by any of: (a) a GitHub write in a
Bash/PowerShell call (`gh issue comment|create`, `gh api`/`api.github.com` POST to .../issues or .../issues/<n>/comments, or a
GitHub MCP issue-comment/create tool); (b) an Agent/SendMessage call whose text holds a github.com issue URL, or an issue
target plus a comment/open verb; (c) the final message has `Karar kaydı: <github issue/comment URL>` or
`Karar kaydı: ajan yazıyor`; (d) the final message has `Karar kaydı: yok (<short reason>)`. Otherwise it blocks once.

A block reason always ends with RESTATE: the agent must write its complete final report again, because the reader gets only the last message.

Never loops: when `stop_hook_active` is true (Claude is already continuing because of a Stop hook) it exits 0 without blocking.
Fails open: any error, missing file or parse problem exits 0; an unexpected exception appends one line to LOG
(~/.cache/kanit-kancasi.log, override with KANIT_KANCASI_LOG).
"""
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

HEDGES = ["muhtemelen", "büyük ihtimalle", "büyük olasılıkla", "galiba", "sanırım", "herhalde", "olsa gerek",
          "probably", "likely", "presumably"]
TAIL_BYTES = 4 * 1024 * 1024  # a transcript can be huge; the last message is always at the end

REASON = ("Your last message contains the hedge '{word}'. A guess is not a claim: either verify it now (command + evidence) "
          "and restate, or replace it with 'doğrulanmadı — <check that would verify it>' or 'kontrol ediyorum'. "
          "Do not repeat the guess.")


def fold(text):
    """Case-insensitive form with Turkish letters folded: İ/I/ı/i all become i. NFC first, so decomposed (NFD) input folds
    the same as the composed letters."""
    return unicodedata.normalize("NFC", text).replace("İ", "i").casefold().replace("ı", "i")


_FOLDED = {fold(h): h for h in HEDGES}
_PATTERN = re.compile(r"(?<!\w)(" + "|".join(re.escape(f).replace(r"\ ", r"\s+") for f in _FOLDED) + r")(?!\w)")

_FENCE = re.compile(r"^[ \t]*(```|~~~).*?(?:^[ \t]*\1[^\n]*$|\Z)", re.S | re.M)
_INLINE = re.compile(r"(`+)[^\n]*?\1")
_QUOTE_LINE = re.compile(r"^[ \t]*>.*$", re.M)
_QUOTED = re.compile(r"“[^”\n]{0,500}”|\"[^\"\n]{0,500}\"")


def strip_quoted(text):
    text = _FENCE.sub(" ", text)
    text = _INLINE.sub(" ", text)
    text = _QUOTE_LINE.sub(" ", text)
    return _QUOTED.sub(" ", text)


def find_hedge(text):
    """Return the first hedge word found outside code and quotes, or None."""
    m = _PATTERN.search(fold(strip_quoted(text)))
    return _FOLDED[re.sub(r"\s+", " ", m.group(1))] if m else None


# ---- access claims -------------------------------------------------------------------------------------------------------
REASON_ACCESS = ("Your last message claims missing access or asks for a credential/permission ('{claim}'), but neither this turn nor "
                 "the last hours of this session show a `kasa.py bul` search. A working credential may already sit in Vault (e.g. a 403 on the publish token while a "
                 "read-scope token existed). Now: run `kasa.py bul <system> [<scope>]` (e.g. `kasa.py bul panel okuma`), try every "
                 "matching credential with `kasa.py run --env NAME=<path>#<field> -- <command>`, then restate and list the searches "
                 "and the credentials you tried. Ask a person only for what still fails after that, as one action.")
# A subagent's coordinator receives only its last message, so a short follow-up ("the report above stands") loses the report.
RESTATE = ("Then write your COMPLETE final report again in your next message (the reader receives only your last message); "
           "do not refer to 'the report above'.")


def fold_ascii(text):
    """fold() plus Turkish diacritics removed (ş->s, ğ->g, ü->u, ö->o, ç->c), so patterns are written once, in ASCII."""
    return fold(text).translate(_ASCII_FOLD)


_ASCII_FOLD = str.maketrans("şğüöçâîû", "sguocaiu")


_AUX = r"(?:(?:have|the|a|an|any|valid|sufficient|required|read|write|enough)\s+)*"
_CLAIM_SOURCES = [
    # Turkish: "<thing> yok"
    r"yetki(?:si|m|miz|niz|leri)?\s+(?:yok|yetersiz|eksik|bulunmuyor|bulunamadi|verilmemis|verilmedi)",
    r"erisim(?:i|im|imiz)?\s+(?:yok|reddedildi|engellendi|saglanamadi|bulunmuyor)",
    r"(?:token|pat|anahtar|sifre|parola|kimlik\s+bilgisi|credential|api\s*key)\w*\s+(?:\S+\s+){0,2}?(?:yok|bulunamadi|bulunmuyor|eksik)\b",
    r"(?:token|pat|anahtar|sifre|parola|kimlik\s+bilgisi|credential)\w*\s+(?:gerekiyor|gerekli|lazim|verilmedi)\b",
    r"(?:izin|yetki|erisim)\s+(?:gerekiyor|gerekli|lazim|istiyor|verilmedi|verilmemis)\b",
    r"(?:token|pat|parola|sifre|anahtar|credential|yetki|erisim)\w*\s+(?:verir|verebilir|saglar|paylasir|paylasabilir)\s+misin",
    r"(?:token|pat|parola|sifre|anahtar|credential|yetki|erisim)\w*\s+(?:verin|verseniz|verirseniz|vermeniz|paylasin|paylasirsaniz)",
    # asking the user for access (b): "yetki ver", "erişim aç", "giriş yap"
    r"\byetki\s+ver(?:in|ir\s+misin|ebilir\s+misin)?(?!\w)",
    r"\berisim\s+(?:ac|acin|ver|verin|acar\s+misin|acabilir\s+misin)(?!\w)",
    r"\bgiris\s+yap(?:in|abilir\s+misin|ar\s+misin)?(?!\w)(?!\s+(?:buton|ekran|sayfa))",
    # NOT here: a bare HTTP status (401/403, "unauthorized", "forbidden", "kimlik doğrulanamadı") in a result report. That
    # relays an external system's answer; it is not a claim about OUR access. It counts only with the wording above/below.
    # English
    r"\bno\s+(?:access|permissions?|tokens?|credentials?|api\s*key)\b",
    r"\b(?:do(?:es)?\s+not|don't|doesn't|dont|lack(?:s|ed|ing)?|without|missing|insufficient)\s+" + _AUX
    + r"(?:access|permissions?|tokens?|credentials?|scopes?|privileges?|authori[sz]ation)\b",
    r"\blacks?\s+(?:the\s+)?(?:\w+\s+)?(?:scope|permission|access|rights?)\b",
    r"\b(?:permission|access)\s+denied\b",
    r"\bnot\s+(?:authori[sz]ed|permitted)\b",
    r"\b(?:please|could\s+you|can\s+you|would\s+you|need\s+you\s+to)\s+(?:grant|provide|give|share|paste|send)\b[^.\n]{0,60}"
    r"\b(?:token|pat|credentials?|access|permission|password|key)\b",
    r"\b(?:i|we)\s+(?:need|require)\s+(?:a\s+|an\s+|the\s+)?(?:\w+\s+)?(?:token|pat|credentials?|access|permission)\b",
]
_CLAIMS = re.compile("(?:" + "|".join(_CLAIM_SOURCES) + ")")
# "No secret in the code/repo" is good news, not missing access ("kodda artik sifre yok", "repoda token bulunmuyor").
_BENIGN_ABSENCE = re.compile(
    r"\b(?:kod|repo|kaynak|dosya|commit|diff|gecmis|branch|log)\w*\s+(?:\S+\s+){0,3}?"
    r"(?:token|pat|anahtar|sifre|parola|kimlik\s+bilgisi|credential|api\s*key)\w*\s+(?:yok|bulunmuyor|kalmadi)\b")
_BUL_RUN = re.compile(r"\bkasa(?:\.py)?[\"']?\s+bul\b")
# Session memory: a `kasa.py bul` run (Bash call) or citation (assistant text) anywhere earlier in the same transcript within
# the last BUL_MEMORY_HOURS counts like a search in the current turn. Env KANIT_KANCASI_BUL_HOURS overrides (0 = turn only).
BUL_MEMORY_HOURS = float(os.environ.get("KANIT_KANCASI_BUL_HOURS") or 3)
SESSION_TAIL_BYTES = 32 * 1024 * 1024  # read lazily, only when a claim was found and the turn has no search


def find_access_claim(text):
    """Return the first missing-access / credential-request claim in the prose outside code and quotes, or None.
    A message that cites `kasa.py bul` (the search it ran) has no claim to answer for."""
    if _BUL_RUN.search(text):
        return None
    m = _CLAIMS.search(_BENIGN_ABSENCE.sub(" ", fold_ascii(strip_quoted(text))))
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else None


def _entries_reversed(path, tail_bytes=None):
    """Transcript entries (dicts) from the tail, newest first."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - (tail_bytes or TAIL_BYTES)))
        lines = f.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield entry


def _is_real_prompt(entry):
    """A user entry that starts a turn: text, not a tool_result, not a meta entry attached to a prompt (turnCompanion)."""
    if entry.get("type") != "user" or (entry.get("isMeta") and entry.get("turnCompanion")):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    return isinstance(content, list) and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content) \
        and any(isinstance(b, dict) and b.get("type") == "text" for b in content)


def _entry_text(entry):
    """Text of a transcript entry: its string content or its text blocks."""
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


_TAGGED = re.compile(r"<(pasted_content|system-reminder)\b[^>]*>.*?</\1>", re.S)


def _human_text(entry):
    """The prompt text when a person started the turn; "" for task notifications, command output and meta entries."""
    origin = entry.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if entry.get("isMeta") or entry.get("isCompactSummary") or kind not in (None, "human"):
        return ""
    text = _entry_text(entry)
    if len(text) > MAX_PROMPT_CHARS:
        return ""  # a pasted brief, log or document, not a decision typed in chat
    if kind is None and text.lstrip().startswith("<"):
        return ""  # <command-name>, <local-command-stdout>, <task-notification> in transcripts without `origin`
    return _TAGGED.sub(" ", text)


class Turn:
    """What the current turn did. ran_bul: a Bash call ran `kasa.py bul`; handback: the SubagentHandback report text;
    recorded: a tool call recorded a decision on GitHub or delegated that (records_decision); prompt: the human prompt text
    ("" when no person started the turn); prev_text: the last assistant text before that prompt (the proposal a reply answers)."""

    def __init__(self):
        self.ran_bul, self.handback, self.recorded, self.prompt, self.prev_text = False, "", False, "", ""


def scan_turn(path):
    """Turn for the transcript (the entries after the last real user prompt), or None when it cannot be read (unknown)."""
    turn, in_turn = Turn(), True
    try:
        for entry in _entries_reversed(path):
            if not in_turn:  # past the prompt: only the previous assistant text is wanted
                if _is_real_prompt(entry):
                    break
                if entry.get("type") == "assistant" and _entry_text(entry).strip():
                    turn.prev_text = _entry_text(entry)
                    break
                continue
            if _is_real_prompt(entry):
                turn.prompt, in_turn = _human_text(entry), False
                continue
            if entry.get("type") != "assistant":
                continue
            content = (entry.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                    continue
                name = str(b.get("name", ""))
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                if name == "Bash" and _BUL_RUN.search(str(inp.get("command", ""))):
                    turn.ran_bul = True
                elif name == "SubagentHandback" and not turn.handback:
                    turn.handback = str(inp.get("message", ""))
                if not turn.recorded and records_decision(name, inp):
                    turn.recorded = True
    except OSError:
        return None
    return turn


def _entry_time(entry):
    """The entry's `timestamp` as an aware datetime, or None."""
    ts = entry.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        t = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def bul_in_session(path, hours=None):
    """Whether the transcript shows a `kasa.py bul` run or citation within the last `hours` (default BUL_MEMORY_HOURS), in any
    turn. Entries without a readable timestamp do not count (unknown age). Unreadable transcript: False (the caller decides)."""
    hours = BUL_MEMORY_HOURS if hours is None else hours
    if hours <= 0:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        for entry in _entries_reversed(path, SESSION_TAIL_BYTES):
            t = _entry_time(entry)
            if t is None or entry.get("type") != "assistant":
                continue
            if t < cutoff:
                break  # entries are in time order: everything further back is older
            content = (entry.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("name") == "Bash" \
                        and _BUL_RUN.search(str((b.get("input") or {}).get("command", ""))):
                    return True
                if b.get("type") == "text" and _BUL_RUN.search(str(b.get("text", ""))):
                    return True
    except OSError:
        return False
    return False


# ---- decisions -----------------------------------------------------------------------------------------------------------
# The one place to tune what reads as a decision. Words are matched after fold_ascii (lower case, Turkish letters folded, so
# they are written in ASCII) on word boundaries; a trailing "*" also matches longer words with that start.
# Imperative decision verbs: they make a prompt a decision even when it also holds a question word ("ne durumda, başla").
DECISION_VERBS = [
    "yapalim", "olsun", "erteleyelim", "ertele", "onayliyorum", "onayladim", "basla", "baslayalim", "baslat", "kalsin",
    "iptal", "gonder", "gonderelim", "gecelim", "ekleyelim", "kaldiralim", "silelim", "kullanalim", "secelim", "birakalim",
    "vazgectim", "vazgecelim", "yayinla", "yayinlayalim", "merge et", "artiralim", "azaltalim", "dusurelim", "yukseltelim",
    "let's", "go ahead", "approved", "i approve", "do it", "ship it", "postpone",
]
# Decision nouns: a decision only in a prompt without a question word ("öncelik" alone in "işler ne durumda" is a question).
DECISION_NOUNS = ["karar", "kararim", "kararimiz", "oncelik", "oncelikli", "decided"]
DECISION_MARKERS = DECISION_VERBS + DECISION_NOUNS
# Turkish question words: a prompt holding one is a question (also without "?") unless it holds a DECISION_VERBS word.
QUESTION_WORDS = ["ne", "neler", "nedir", "neyi", "neye", "nede", "nasil", "neden", "niye", "nicin", "nerede", "nereye",
                  "nereden", "kim", "kimi", "kime", "kimde", "hangi", "hangisi", "kac", "kaci", "kacta"]
# A short reply (at most APPROVAL_MAX_WORDS words) whose first word is one of these approves the previous proposal.
APPROVALS = ["tamam", "evet", "aynen", "olur", "ok", "okey", "okay", "uygun", "onay", "onayli", "yes", "sure"]
APPROVAL_MAX_WORDS = 4
# Longer prompts are pasted briefs, logs or documents and are not checked.
MAX_PROMPT_CHARS = 1500
# The previous assistant message made a proposal when it holds one of these ("?" is literal).
PROPOSAL_MARKERS = ["?", "oneri*", "onerim*", "tavsiye*", "ister misin*", "recommend*", "propose*", "suggest*", "shall i"]


def _word_pattern(words):
    alts = []
    for w in words:
        core = re.escape(w.rstrip("*")).replace(r"\ ", r"\s+") + (r"\w*" if w.endswith("*") else "")
        alts.append(r"(?<!\w)" + core + r"(?!\w)" if w[0].isalnum() else core)
    return re.compile("|".join(alts))


_DECISION = _word_pattern(DECISION_MARKERS)
_DECISION_VERB = _word_pattern(DECISION_VERBS)
_QUESTION_WORD = _word_pattern(QUESTION_WORDS)
_APPROVAL = _word_pattern(APPROVALS)
_PROPOSAL = _word_pattern(PROPOSAL_MARKERS)
# the question particle mı/mi/mu/mü (folded to mi/mu) with its personal endings: "mı", "misin", "miyiz", "midir", "muydu", ...
_QUESTION_PARTICLE = re.compile(r"(?<!\w)m[iu](?:s[iu]n(?:[iu]z)?|y[iu]z|y[iu]m|d[iu]r|yd[iu]|ym[iu]s)?(?!\w)")


def find_decision(prompt, prev_text=""):
    """The marker that makes the human prompt a decision, or None. Code, quotes and questions never count."""
    text = fold_ascii(strip_quoted(prompt)).strip()
    if not text or text.endswith("?") or _QUESTION_PARTICLE.search(text):
        return None
    m = (_DECISION_VERB if _QUESTION_WORD.search(text) else _DECISION).search(text)
    if m:
        return re.sub(r"\s+", " ", m.group(0))
    words = re.findall(r"[\w']+", text)
    if 0 < len(words) <= APPROVAL_MAX_WORDS and _APPROVAL.fullmatch(words[0]) and _PROPOSAL.search(fold_ascii(prev_text)):
        return words[0]
    return None


_ISSUE_PATH = r"repos/[\w.-]+/[\w.-]+/issues(?:/\d+/comments)?(?![/\w])"
_GH_ISSUE_CMD = re.compile(r"\bgh\s+issue\s+(?:comment|create)\b")
_GH_API_WRITE = re.compile(r"(?:\bgh\s+api\b|api\.github\.com/)(?=.*?" + _ISSUE_PATH + r")"
                           r"(?=.*?(?:-X\s*POST|--method[\s=]+POST|\s-[fFd]\s|--(?:raw-)?field\b|--input\b|--data\b))"
                           r"(?!.*?(?:-X\s*GET|--method[\s=]+GET))", re.S | re.I)
_MCP_ISSUE_WRITE = re.compile(r"github.*(?:add_issue_comment|create_issue|issue_write|add_comment)", re.I)
_ISSUE_URL = re.compile(r"github\.com/[\w.-]+/[\w.-]+/issues/\d+")
_RECORD_TARGET = re.compile(r"(?<!\w)(?:issue\w*|urun\s+kararlari|[\w.-]+#\d+)")
_RECORD_VERB = re.compile(r"(?<!\w)(?:comment\w*|yorum\w*|open|create|record|post|kaydet\w*|kaydi|ac|acin|olustur\w*|"
                          r"yaz\w*|write)(?!\w)")


def records_decision(name, inp):
    """Whether one tool call records a decision on GitHub: (a) an issue comment/create write, or (b) a delegation to do it."""
    if name in ("Bash", "PowerShell"):
        cmd = str(inp.get("command", ""))
        return bool(_GH_ISSUE_CMD.search(cmd) or _GH_API_WRITE.search(cmd))
    if name in ("Agent", "Task", "SendMessage"):
        text = fold_ascii(json.dumps(inp, ensure_ascii=False))
        return bool(_ISSUE_URL.search(text) or (_RECORD_TARGET.search(text) and _RECORD_VERB.search(text)))
    return bool(_MCP_ISSUE_WRITE.search(name))


# (c) / (d): the final message names the record, says an agent writes it, or says why there is none
_KAYIT_LINE = re.compile(r"karar\s+kaydi\s*:\s*(?:https?://github\.com/\S+/(?:issues|pull)/\d+|ajan\s+yaziyor|"
                         r"yok\s*\(\s*[^)\s][^)]*\))")
REASON_DECISION = ("The user's message in this turn reads as a decision ('{marker}'), but this turn did not record it on GitHub "
                   "(rule 'Decisions live on GitHub'). Now: post it as a short Turkish comment on the related issue (product-wide: "
                   "the repo's 'Ürün kararları' issue; team/process: the team decisions issue), or delegate that to an agent, and "
                   "end with the line 'Karar kaydı: <issue or comment URL>' (or 'Karar kaydı: ajan yazıyor'). If it is not a "
                   "decision, end with 'Karar kaydı: yok (<short reason>)'.")


def last_assistant_text(path):
    """Text blocks of the last assistant entry that has any, read from the transcript tail."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        lines = f.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # also covers the cut first line of a tail read
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        else:
            continue
        if text.strip():
            return text
    return ""


def main():
    # Claude Code writes UTF-8; never decode with the locale code page (cp1254/cp1252 on Windows turns "ı" into "Ä±")
    data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    if not isinstance(data, dict) or data.get("stop_hook_active"):
        return
    text = data.get("last_assistant_message")
    if not isinstance(text, str) or not text.strip():
        text = last_assistant_text(data["transcript_path"])
    reasons = []
    word = find_hedge(text)
    if word:
        reasons.append(REASON.format(word=word))
    # the subagent's own transcript is its turn; transcript_path of a SubagentStop is the parent session's
    subagent = data.get("hook_event_name") == "SubagentStop"
    tpath = data.get("agent_transcript_path") if subagent else data.get("transcript_path")
    turn = scan_turn(tpath) if isinstance(tpath, str) and tpath else None
    if turn is not None and not turn.ran_bul:
        claim = find_access_claim(text + "\n" + turn.handback)
        if claim and not bul_in_session(tpath):
            reasons.append(REASON_ACCESS.format(claim=claim))
    # decisions: main session only (subagents act on briefs, not on user decisions)
    if turn is not None and not subagent and not turn.recorded and not _KAYIT_LINE.search(fold_ascii(text)):
        marker = find_decision(turn.prompt, turn.prev_text)
        if marker:
            reasons.append(REASON_DECISION.format(marker=marker))
    if reasons:
        # ASCII-only JSON (escaped non-ASCII): readable whatever code page stdout has
        print(json.dumps({"decision": "block", "reason": " | ".join(reasons) + " " + RESTATE}, ensure_ascii=True))


LOG = Path(os.environ.get("KANIT_KANCASI_LOG") or Path.home() / ".cache" / "kanit-kancasi.log")
LOG_MAX_BYTES = 256 * 1024


def log_error(exc):
    """Append one line about an unexpected error (best effort; the file starts over past LOG_MAX_BYTES)."""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if LOG.exists() and LOG.stat().st_size > LOG_MAX_BYTES else "a"
        with open(LOG, mode, encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} kanit_kancasi allowed after error: {type(exc).__name__}: "
                    f"{str(exc)[:200]!r}\n")
    except Exception:
        pass


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        main()
    except Exception as exc:  # fail open: a broken guard must never stall the session
        log_error(exc)
    sys.exit(0)

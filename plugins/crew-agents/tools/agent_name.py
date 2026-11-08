#!/usr/bin/env python3
"""PreToolUse hook (matcher "Agent|Task"): gives every spawned subagent a short random name.

The Claude Code UI shows a subagent only by its type ("Message from @senior-developer-oh"), so parallel agents of one type look
the same. This hook picks a name from NAME_LIST (Turkish nature and weather words, none of them a colleague's first name) and
returns the Agent tool input through `hookSpecificOutput.updatedInput` (which replaces the whole input, so every other field is
passed on unchanged) with:
  - `description` prefixed with "<Name> · ";
  - one line prepended to `prompt`: "Your name is <Name>. Start every status message and your final report with '<Name>:'."
Names do not repeat inside one session: the used names are kept in <tempdir>/crew-agent-names/<session_id>.json (files older than
two days are removed on the way). When all names are used, a number is appended (Poyraz-2).
A person may replace the list with ~/.config/claude-shared/agent-names (see name_list).

It runs on every agent spawn, so it fails open: any error, an unknown tool, a missing prompt or an input that already carries a
name prints nothing and exits 0, and the call goes on unchanged. Python 3 stdlib only. Installed by quota-pace install.sh / install.ps1
(settings.json PreToolUse entry running this file from the claude-shared clone).
"""
import json
import os
import random
import sys
import tempfile
import time

NAME_LIST = (
    # winds and weather
    "Poyraz", "Lodos", "Karayel", "Meltem", "Ayaz", "Kırağı", "Tipi", "Bora", "Kasırga", "Şimşek", "Sis", "Full",
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
PERSONAL_FILE = ("~", ".config", "claude-shared", "agent-names")   # personal override of NAME_LIST, same folder as `person` / `plan`
MIN_PERSONAL = 10
DELIMITER = " · "
TOOLS = ("Agent", "Task")   # "Task" is the subagent tool's older name
STATUS_FOLDER = os.environ.get("CREW_AGENT_NAME_DIR") or os.path.join(tempfile.gettempdir(), "crew-agent-names")
OLD_SECONDS = 2 * 86400


def _locked(path, is_):
    """Run is_() while holding <yol>.lock (O_EXCL); parallel spawns of one session then never pick the same name. After ~2 s
    without the lock (a stale lock from a killed hook) it runs anyway."""
    lock = path + ".lock"
    fd = None
    for _ in range(200):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > 10:
                    os.remove(lock)
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
                os.remove(lock)
            except OSError:
                pass


def _clean():
    try:
        now = time.time()
        for name in os.listdir(STATUS_FOLDER):
            path = os.path.join(STATUS_FOLDER, name)
            if now - os.path.getmtime(path) > OLD_SECONDS:
                os.remove(path)
    except OSError:
        pass


def valid_name(name):
    return bool(name) and name[0].isupper() and name.isalpha() and len(name) <= 10


def name_list():
    """The names to pick from: the valid lines of ~/.config/claude-shared/agent-names (UTF-8, one name per line, `#` comments and
    blank lines ignored, de-duplicated, order kept) when that gives at least MIN_PERSONAL names, else the team list NAME_LIST."""
    try:
        path = os.path.join(os.path.expanduser(PERSONAL_FILE[0]), *PERSONAL_FILE[1:])
        with open(path, encoding="utf-8-sig") as f:
            lines = [s.strip() for s in f.read().splitlines()]
        names = list(dict.fromkeys(s for s in lines if s and not s.startswith("#") and valid_name(s)))
        if len(names) >= MIN_PERSONAL:
            return tuple(names)
    except (OSError, ValueError):
        pass
    return NAME_LIST


def ad_sec(session, randomly=random):
    """A name not used yet in this session (session_id `session`), recorded as used."""
    os.makedirs(STATUS_FOLDER, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (session or "session-missing"))[:120]
    path = os.path.join(STATUS_FOLDER, safe + ".json")

    list_ = name_list()

    def sec():
        try:
            with open(path, encoding="utf-8") as f:
                used = json.load(f)
            if not isinstance(used, list):
                used = []
        except (OSError, ValueError):
            used = []
        bos = [a for a in list_ if a not in used]
        if bos:
            name = randomly.choice(bos)
        else:
            turn = len(used) // len(list_) + 1
            name = f"{randomly.choice(list_)}-{turn}"
            while name in used:
                name = f"{randomly.choice(list_)}-{turn}"
        used.append(name)
        temp = path + f".{os.getpid()}.tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(used, f, ensure_ascii=False)
        os.replace(temp, path)
        return name

    name = _locked(path, sec)
    _clean()
    return name


def named_input(input_, name):
    """The new tool input: every field kept, description and prompt carry the name."""
    new = dict(input_)
    description = input_.get("description") or ""
    new["description"] = f"{name}{DELIMITER}{description}" if description else name
    new["prompt"] = f"Your name is {name}. Start every status message and your final report with '{name}:'.\n\n{input_['prompt']}"
    return new


def already_named(input_):
    description = input_.get("description") or ""
    bas = description.split(DELIMITER, 1)[0]
    list_ = name_list() + NAME_LIST
    return DELIMITER in description and (bas in list_ or bas.rsplit("-", 1)[0] in list_) \
        or str(input_.get("prompt", "")).startswith("Your name is ")


def answer(event):
    """The hook's stdout JSON for one PreToolUse event, or None to let the call pass unchanged."""
    if not isinstance(event, dict) or event.get("tool_name") not in TOOLS:
        return None
    input_ = event.get("tool_input")
    if not isinstance(input_, dict) or not isinstance(input_.get("prompt"), str) or not input_["prompt"].strip():
        return None
    if already_named(input_):
        return None
    name = ad_sec(str(event.get("session_id") or ""))
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": named_input(input_, name)}}


def main():
    try:
        try:
            sys.stdin.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
        result = answer(json.loads(sys.stdin.read() or "null"))
        if result is not None:
            sys.stdout.write(json.dumps(result, ensure_ascii=True))
    except Exception:   # fail open: never block or change an agent spawn because of this hook
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

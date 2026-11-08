#!/usr/bin/env python3
"""Claude Code subagentStatusLine: show each subagent's task first.

Row body: <description> · <agentType, dim> · <tokens, e.g. 179k> [· ₺205 (₺15)] [· <elapsed>] [· 12m quiet] [· idle]

Last activity (no model calls; reads only the last 256 KB of the agent's transcript, cached per file mtime+size):
  quiet      "<age> quiet" (e.g. "12m quiet"): age of the last assistant message or tool result in the transcript (the file
             mtime when none can be read). Shown only from SILENT_AFTER (10 min) on; hidden below it.
  idle       the task is still "running" but the last turn ended: the last assistant message has stop_reason "end_turn" and no
             tool_use, nothing follows it, and it is at least 30 s old. This is the agent that finished while something it started
             (a test process) keeps the row registered. Omitted when the transcript does not show that.
  Both parts need the quota-pace tools (transcript lookup cost_incremental.agent_file); without them the row is unchanged.

Cost (optional, needs the quota-pace cost tools; without them the row is unchanged):
  ₺205   what the agent's tokens would cost at Anthropic API list prices (shared price table, quota-pace/bin/cost-prices.json),
         read incrementally from the agent's own transcript <transcript minus .jsonl>/subagents/agent-<task id>.jsonl.
  (₺15)  the real cost: the share of the plan's weekly price that usage consumed, API $ x (plan price x 12/52) / (API $ per 100 % of
         the weekly limit, calibrated on this machine by cost_ratio.py). Shown only while a fresh calibration exists.
  Amounts are in lira at the cached TCMB rate (dollars until a rate was fetched).

Contract (code.claude.com/docs/en/statusline, "Subagent status lines"):
  stdin : one JSON object, base hook fields + "columns" + "tasks" (array).
          Task fields used: id, name, agentType, description, label,
          startTime (ms epoch), tokenCount.
  stdout: one JSON object per line: {"id": "<task id>", "content": "<row body>"}

Never crashes: on bad input it prints nothing and exits 0 (the default row stays).
Stdlib only, run with `python3 -I`.
"""
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN_CANDIDATES = (os.path.join(_HERE, "..", "..", "quota-pace", "bin"),            # same checkout (claude-shared)
                 os.path.join(os.path.expanduser("~"), ".claude", "bin"))           # installed by quota-pace/install.sh --cost

MAX_DESC = 60
DIM, RESET = "\x1b[2m", "\x1b[0m"


def clean(text):
    """Single line, no control characters."""
    return " ".join(str(text).split())


def truncate(text, limit=MAX_DESC):
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def fmt_tokens(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 1000:
        return f"{n // 1000}k"
    return str(n)


def fmt_elapsed(start_ms, now):
    try:
        secs = max(0, int(now - float(start_ms) / 1000.0))
    except (TypeError, ValueError):
        return ""
    return humanize_secs(secs)


def humanize_secs(secs):
    """Two-stage human duration (largest two units), abbreviations:
    45s, 4m 40s, 18h 42m, 2d 3h. A zero lower unit is dropped (3h, not 3h 0m)."""
    secs = max(0, int(secs))
    units = (("d", 86400), ("h", 3600), ("m", 60), ("s", 1))
    for i, (name, size) in enumerate(units):
        if secs >= size or i == len(units) - 1:
            top = f"{secs // size}{name}"
            if i == len(units) - 1:
                return top
            low_name, low_size = units[i + 1]
            low = (secs % size) // low_size
            return f"{top} {low}{low_name}" if low else top
    return "0s"


def load_cost_tools():
    """(cost_incremental, cost_ratio, cost) modules from the quota-pace bin directory, or None when they are not there."""
    for d in _BIN_CANDIDATES:
        if os.path.isfile(os.path.join(d, "cost_incremental.py")) and os.path.isfile(os.path.join(d, "cost_ratio.py")):
            sys.path.insert(0, os.path.abspath(d))
            try:
                import cost
                import cost_incremental
                import cost_ratio
                return cost_incremental, cost_ratio, cost
            except Exception:
                sys.path.pop(0)
    return None


def cost_parts(task, transcript_path, tools, factor):
    """['₺205 (₺15)'] for one task (just ['₺205'] without a calibration); [] when the agent's transcript or the cost tools are not available."""
    if not tools:
        return []
    incremental, _ratio, cost = tools
    try:
        path = incremental.agent_file(transcript_path, task.get("id"))
        usd = incremental.total(path, "agent-%s" % task.get("id")) if path else None
        if usd is None:
            return []
        txt = cost.money(usd)
        if factor:
            txt += " (%s)" % cost.money(usd * factor)
        return [txt]
    except Exception:
        return []


TAIL_BYTES = 256 * 1024
SILENT_AFTER = 600  # s: the "<age> quiet" part is shown only when the agent made no progress for this long
IDLE_GRACE = 30     # s: a final answer younger than this is still the normal hand-over, not "idle"
_CACHE_MAX = 200


def _parse_ts(value):
    """ISO-8601 timestamp ('2026-11-03T21:20:55.209Z') -> epoch seconds, or None."""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def parse_tail(lines):
    """Transcript tail (iterable of JSONL text lines) -> (last_ts or None, ended).
    last_ts: timestamp of the last assistant message or tool result. ended: that last conversational entry is an assistant
    message with stop_reason 'end_turn' and no tool_use (a final answer, nothing pending). Entries of other types
    (api-request, blobs, system) are skipped; unparseable lines (a cut first line) are skipped."""
    last_ts, ended = None, False
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict) or d.get("type") not in ("assistant", "user"):
            continue
        ts = _parse_ts(d.get("timestamp"))
        if ts is None:
            continue
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        content = msg.get("content")
        blocks = content if isinstance(content, list) else []
        has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks)
        is_tool_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks)
        if d["type"] == "assistant":
            last_ts = ts
            ended = msg.get("stop_reason") == "end_turn" and not has_tool_use
        elif is_tool_result:
            last_ts, ended = ts, False
        else:                      # a plain user message (e.g. a SendMessage to the agent): the agent has work again
            ended = False
    return last_ts, ended


def read_tail_lines(path):
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        f.seek(max(0, size - TAIL_BYTES))
        raw = f.read()
    return raw.decode("utf-8", "replace").splitlines()


def _cache_path():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "subagent-last-cache.json")


def last_activity(path, cache_file=None):
    """(epoch of the last activity, ended) for one transcript file; (None, False) on any error.
    The result is cached per (path, mtime, size) in cache_file, so an agent whose file did not change costs no read."""
    try:
        st = os.stat(path)
        key = "%s|%d|%d" % (path, int(st.st_mtime), st.st_size)
        cache_file = cache_file or _cache_path()
        cache = {}
        try:
            with open(cache_file, encoding="utf-8") as f:
                cache = json.load(f)
            if not isinstance(cache, dict):
                cache = {}
        except Exception:
            cache = {}
        hit = cache.get(key)
        if isinstance(hit, list) and len(hit) == 2:
            return hit[0], bool(hit[1])
        ts, ended = parse_tail(read_tail_lines(path))
        if ts is None:
            ts = st.st_mtime
        # drop the entries of older states of this same file, keep the cache small
        cache = {k: v for k, v in cache.items() if k.split("|")[0] != path}
        cache[key] = [ts, ended]
        if len(cache) > _CACHE_MAX:
            cache = dict(list(cache.items())[-_CACHE_MAX:])
        try:
            tmp = "%s.tmp%d" % (cache_file, os.getpid())
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp, cache_file)
        except Exception:
            pass
        return ts, ended
    except Exception:
        return None, False


def activity_parts(task, transcript_path, now, tools, cache_file=None):
    """['12m quiet'] (+ ["idle"]) for one task; [] when its transcript cannot be read."""
    if not tools:
        return []
    try:
        path = tools[0].agent_file(transcript_path, task.get("id"))
        if not path:
            return []
        ts, ended = last_activity(path, cache_file)
        if ts is None:
            return []
        age = max(0, int(now - ts))
        parts = ["%s quiet" % humanize_secs(age)] if age >= SILENT_AFTER else []
        if ended and age >= IDLE_GRACE and task.get("status", "running") == "running":
            parts.append("idle")
        return parts
    except Exception:
        return []


def row_body(task, now, costs=(), activity=()):
    # Task first: description, falling back to label, then name.
    title = ""
    for key in ("description", "label", "name"):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            title = clean(value)
            break
    if not title:
        title = clean(task.get("agentType") or "subagent")
    parts = [truncate(title)]

    agent_type = task.get("agentType")
    if isinstance(agent_type, str) and agent_type.strip():
        parts.append(f"{DIM}{clean(agent_type)}{RESET}")

    tokens = fmt_tokens(task.get("tokenCount"))
    if tokens:
        parts.append(tokens)

    parts.extend(costs)

    elapsed = fmt_elapsed(task.get("startTime"), now) if "startTime" in task else ""
    if elapsed:
        parts.append(elapsed)

    parts.extend(activity)

    return " · ".join(parts)


def dump_input(raw):
    """Debug aid (kept permanently): the last stdin JSON Claude Code sent, ~/.claude/subagent-statusline-input.json
    (0600, overwritten, atomic). Never raises."""
    try:
        target = os.path.join(os.path.expanduser("~"), ".claude", "subagent-statusline-input.json")
        tmp = "%s.tmp%d" % (target, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw)
        os.replace(tmp, target)
    except Exception:
        pass


def main():
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")   # UTF-8 whatever the console code page is (Windows: cp1252/cp1254)
        dump_input(raw)
        data = json.loads(raw)
        tasks = data.get("tasks") if isinstance(data, dict) else None
        if not isinstance(tasks, list):
            return
        now = time.time()
        tools = load_cost_tools()
        factor = None
        if tools:
            try:
                factor = tools[1].read_coefficient(now)
            except Exception:
                factor = None
        out = []
        for task in tasks:
            if not isinstance(task, dict) or "id" not in task:
                continue
            costs = cost_parts(task, data.get("transcript_path"), tools, factor)
            # ASCII-only JSON (\u20ba etc.): the same bytes under every code page; "₺" and "·" cannot be written to a cp1252 pipe
            activity = activity_parts(task, data.get("transcript_path"), now, tools)
            out.append(json.dumps({"id": task["id"], "content": row_body(task, now, costs, activity)}, ensure_ascii=True))
        if out:
            sys.stdout.buffer.write(("\n".join(out) + "\n").encode("ascii"))
            sys.stdout.buffer.flush()
    except Exception:
        return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)

# Claude Code prompt-cache hygiene

Why this matters: the prompt cache works on prefix matching. If the beginning of a request changes, everything after it can no longer be read from the cache and the whole context is written again, which costs about 10 times more than reading it. The cache hit rate (`cache NN%` in the status line when the optional cost statistics of `quota-pace` are installed, see `plugins/quota-pace/COST.md`) is the share of prompt tokens read from the cache in the main conversation; the number in brackets is the miss count. Aim for above 90%.

## What breaks the cache

1. Dynamic content in the system prompt or early context: dates, UUIDs, counters, hook output that differs on every request. Hooks should add a line to the context only when something changes (the `quota-pace` hook adds one only when the quota mode changes) and write nothing into the system prompt.
2. A changing tool list: adding or removing an MCP server, enabling or disabling a plugin, denying a tool. Tool definitions are part of the system prompt.
3. Changing the model in the main session (`/model`, `opusplan`, automatic switching). A pause longer than the cache lifetime also breaks it: the cache expires after 5 minutes idle (1 hour when the 1-hour cache is configured).

Also expected, not a bug: `/compact` and clearing old tool results (a planned rebuild), many images accumulating, and a Claude Code version upgrade.

## Seeing the cause

The `Prompt cache (main)` line in the session block of `/usage` shows the request count, hit rate, miss count, the tokens rewritten at the last miss and a "likely cause" (for example "tool definitions changed"). If `/cost` does not show the same numbers, use `/usage`. Raw data is in the status-line JSON as `prompt_cache` (`hit_ratio`, `misses`, `last_miss_cause`, `miss_causes`, `warm`, `expires_at`).

## Rule of thumb

Choose the model and the tools at the start of a session and do not change them mid-way; if a change is needed, make it between tasks together with a `/compact`.

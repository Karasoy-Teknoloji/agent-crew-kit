# crew-agents

**Optional.** The team's single agent set (merged from two older agent sets in 0.2.0 and 0.6.0). The agent files live in [`agents/`](../../agents) at the repo root (since 0.8.0); this plugin keeps their tools and the output style. Edit agents there; do not keep private copies.

## Names

Every agent is named `<role>-<model><effort>`: the role it is for, then the default model and effort.

- Model: `h` = Haiku 5.5, `s` = Sonnet 5.5, `o` = Opus 5.5, `f` = Fable 5.1.
- Effort: `m` = medium, `h` = high, `xh` = xhigh, `max` = max. `low` is never used for agentic work, so medium is the minimum.

The suffix is the type's default; the coordinator raises effort per call with the Agent tool's `effort` parameter. They load as user agents, so the names carry no prefix (`quick-task-hm`; as plugin agents they were `crew-agents:quick-task-hm`, and Claude Code offers no way to hide a plugin prefix).

## Escalation ladder: model + effort rungs, cheapest first

| Rung | Type | Model; tools | Effort, in escalation order | Use |
|---|---|---|---|---|
| 1 | `quick-task-hm` | Haiku 5.5; core tools | medium → high | mechanical, checkable jobs: lookups, triage, summaries, CI tracking, mechanical edits, small code changes with an exact brief; `high` for strict multi-rule briefs |
| 2 | `developer-sm` | Sonnet 5.5; core tools | medium | well-specified code/CI changes and multi-step work that is too long for Haiku or that Haiku failed |
| 3 | `hard-task-sh` | Sonnet 5.5; all tools | high → xhigh → max | hard debugging, tricky code changes, multi-source analysis |
| 4 | `senior-developer-oh` | Opus 5.5; all tools | high → xhigh | the hardest code work and analysis: multi-layer/multi-repo changes, architecture, unclear root causes |
| 5 | `principal-engineer-omax` | Opus 5.5; all tools | max | the top single-model rung |
| 6 | `deep-analysis-fh` | Fable 5.1; all tools | high → max | a different model family, not a higher rung: a second opinion when a `principal-engineer-omax` job failed twice, or an independent cross-check of a critical decision |

The coordinator picks the starting rung by difficulty and risk (hard, production or security steps start at an Opus rung), reviews every result against the brief and, when it falls short, re-runs the job, or its failed part, one step up with a sharper brief: effort first within the same model (cheaper), then the next model (`rules/AUTONOMY.md`). The first effort in a row is the type's own; later steps are passed per call with the Agent tool's `effort` parameter. Artificial Analysis Intelligence Index v5.0: Opus 5.5 (max) 58, Sonnet 5.5 (max) 54, Fable 5.1 (max) 49, Haiku 5.5 36. Fable is not smarter on that index, but a different model catches different mistakes; it is also the most expensive per solved task.

## Role agents (outside the ladder)

| Type | Model, effort | Tools | Use |
|---|---|---|---|
| `Explore` | Haiku, default effort | Read, Grep, Glob, Bash (read-only) | broad read-only code search; keeps the built-in name so it replaces the built-in `Explore` |
| `research-sm` | Sonnet, medium | all but Write/Edit (read-only) | discovery and research: tracing a flow, reading log/DB/panel state, docs |
| `reviewer-oh` | Opus, high | all but Write/Edit (read-only) | the independent review of a high-risk diff before merge or release (WORKING-RULES.md) |
| `verifier-hm` | Haiku, medium | all but Write/Edit | build/test verification through the VM runner, pass/fail only |
| `operations-sm` | Sonnet, medium | all | ops and diagnostics (Vault, VPN/SSH, logs); stops at lasting or risky steps |
| `release-ops-oh` | Opus, high | all | approved prod/customer operations: CI release dispatch, license API version assignment, server updates, Vault writes |
| `security-oh` | Opus, high | all | security review and hardening of our own systems; fixes only after approval |
| `panel-reader-sm` | Sonnet, medium | no Bash/PowerShell/Write/Edit | read-only web panel inspection (Chrome) |
| `browser-task-hm` | Haiku, medium (high for strict multi-rule briefs) | all | rung 1 for browser/MCP jobs (Chrome/Firefox, MCP connectors, artifact tools) with an exact brief: web UI steps, uploads, form filling, read-only panel checks; stops at password/2FA/payment screens. Replaces `general-purpose` + explicit model for those jobs. Risky UI changes go to `hard-task-sh` |
| `long-task-fxh` | Fable, xhigh | all | autonomous projects over 30 minutes, plan first, status every ~30 min |

Agents start without CLAUDE.md (`omitClaudeMd`) and repeat the standing team rules in their own prompt, except `operations-sm` and `panel-reader-sm`, which keep their own short rule lists.

## Old names (merge of 0.6.0)

| Old name | Set | New name | Model, effort | Note |
|---|---|---|---|---|
| `haiku-worker` | crew-agents | `quick-task-hm` | haiku, medium | merged with `quick-task` |
| `quick-task` | team-agents | `quick-task-hm` | haiku, medium | effort raised low → medium |
| `sonnet-worker` | crew-agents | `developer-sm` | sonnet, medium | merged with `developer` |
| `developer` | team-agents | `developer-sm` | sonnet, medium | |
| `sonnet-deep` | crew-agents | `hard-task-sh` | sonnet, high | |
| `opus-deep` | crew-agents | `senior-developer-oh` | opus, high | merged with `senior-developer`; its review role moves to `reviewer-oh` |
| `senior-developer` | team-agents | `senior-developer-oh` | opus, high | |
| `opus-max` | crew-agents | `principal-engineer-omax` | opus, max | |
| `fable-deep` | crew-agents | `deep-analysis-fh` | fable, high | merged with `deep-analysis` |
| `deep-analysis` | team-agents | `deep-analysis-fh` | fable, high | |
| `long-task` | team-agents | `long-task-fxh` | fable, xhigh | |
| `research` | team-agents | `research-sm` | sonnet, medium | now also blocked from Write/Edit |
| `reviewer` | team-agents | `reviewer-oh` | opus, high | model raised sonnet/medium → opus/high, the review the rules require |
| `release-ops` | team-agents | `release-ops-oh` | opus, high | |
| `security` | team-agents | `security-oh` | opus, high | |
| `verifier` | crew-agents | `verifier-hm` | haiku, medium | VM steps follow the runner rule |
| `operation` | crew-agents | `operations-sm` | sonnet, medium | |
| `panel-reader` | crew-agents | `panel-reader-sm` | sonnet, medium | effort raised low → medium |
| `Explore` | crew-agents | `Explore` | haiku, default | unchanged |

## Install

Agents: `quota-pace/install.sh` / `install.ps1` (the default team install) link `~/.claude/agents/claude-shared` -> `<clone>/agents` (`agent_link` in `install_common.py`: a directory junction on Windows, which needs no admin; a symlink on Linux/macOS). Claude Code has no setting for the agents directory but loads subdirectories of `~/.claude/agents`, so the team set sits in its own entry and a person's own agents stay next to it untouched. It is a link, not a copy: a `git pull` of the clone updates the agents at the next session start. The session-start hook's background run (`tools/rules_watch.py` -> `install_common.py --setting`) creates or repairs the link after every pull (a moved clone, a deleted link); a real directory with that name is never touched (warning instead). `--remove` / `-Remove` removes only the link. Do not copy the agent files into `~/.claude/agents`.

Plugin: the same install enables this plugin straight from the claude-shared clone: they write `extraKnownMarketplaces.claude-shared` as a `directory` source pointing at the clone and `enabledPlugins["crew-agents@claude-shared"] = true` into `~/.claude/settings.json`, then register the plugin with `claude plugin install crew-agents@claude-shared --scope user` (idempotent; without the install record Claude Code does not load the output style, even when `enabledPlugins` says true; if the marketplace is unknown to Claude Code the installer adds it with `claude plugin marketplace add` first; skipped when `claude` is not on PATH). Claude Code loads relative-path plugins of a marketplace added from a local path in place, without a cache copy. The installer also backs up (`.yedek-<date>`) and removes agent files with the old names above from `~/.claude/agents` (copies or symlinks of either old set), and drops the `team-agents@claude-shared` entry from `enabledPlugins`. It also rewrites whole-word old names in the personal `~/.claude/CLAUDE.md` to the new names (backup `.yedek-<date>`, one `ajan adlari guncellendi:` line per file, nothing written with `-WhatIf`; lines that speak of "old names" and the plain words `developer`, `research`, `reviewer`, `security` are left alone); the mapping lives in `OLD_AGENTS` in `install_common.py`. `--remove` / `-Remove` removes the `enabledPlugins` entry (and runs `claude plugin uninstall`), and the marketplace entry when it points at this clone and no other claude-shared plugin is enabled.

## Output style (proactive-concise)

`output-styles/proactive-concise.md` is the team output style: the built-in Proactive (act on routine work without asking) and Concise (lead with the result, 1-3 lines for simple answers) styles in one, with `keep-coding-instructions: true`. Contact with people, production or irreversible steps and money still need explicit approval. It points to `rules/AUTONOMY.md` as the source of truth instead of copying it. The plugin ships it from the `output-styles/` default folder, so the existing install needs no change; it appears in `/output-style` as `crew-agents:proactive-concise`. Select it with `/output-style crew-agents:proactive-concise` or set `"outputStyle": "crew-agents:proactive-concise"` in `~/.claude/settings.json` (user level, all projects). Only styles from the `directory` marketplace clone are used, nothing is copied to `~/.claude/output-styles`.

## tools/agent-handover.ps1

Prints a dead or idle agent's first task, later instructions and the last N assistant messages from the session transcript, so a new agent can take over.

```
pwsh -NoProfile -File tools/agent-handover.ps1 -Id <agent-id> [-Last 20] [-Length 1500]
```

The transcript is looked up under `$HOME\.claude\projects` (override with `-ProjectFolder`; `-Folder` points to a tasks folder with `<id>.output`).

## tools/agent_name.py (agent names)

In the Claude Code UI a subagent shows only its type ("Message from @senior-developer-oh"), so parallel agents of one type look the same. This `PreToolUse` hook (matcher `Agent|Task`) gives every spawned subagent a short random Turkish nature or weather name (Poyraz, Lodos, Toros, Kartal, ...; none is a colleague's first name) through `hookSpecificOutput.updatedInput`: the `description` becomes `<Name> · <description>` and the prompt starts with "Your name is <Name>. Start every status message and your final report with '<Name>:'." Names do not repeat inside a session (state in `<temp>/crew-agent-names/<session_id>.json`, older than two days removed; after the list `Poyraz-2`). It fails open: any error passes the call through unchanged. `install.sh` / `install.ps1` install it from the clone (settings.json entry with the Python they found; a plugin `hooks.json` cannot know a working Python path on Windows) and the self-check proves it names a call; `--remove` removes it. Without the hook, the coordinator puts the name in the brief itself.

Personal names: a person can replace the name list on their own machine with `~/.config/claude-shared/agent-names` (the folder that also holds `person` and `plan`): UTF-8, one name per line, `#` comments and blank lines ignored. Only valid names count (uppercase first letter, letters only, at most 10 characters; duplicates dropped, order kept). With fewer than 10 valid names, or when the file is missing or unreadable, the team list is used. The team list in `agent_name.py` stays the same for everyone else.

## tools/subagent-statusline.py (optional)

Shows each background subagent's task (description) first, then its type and token count, in the agent panel. The `quota-pace` installer (`install.sh` / `install.ps1`) copies it to `~/.claude/bin/subagent-statusline.py` and writes `subagentStatusLine` into `~/.claude/settings.json` with the absolute path of the Python 3 it found (so it also works on Windows, where there is no `python3`). By hand: `"subagentStatusLine": {"type": "command", "command": "<path to python> -I <clone>/plugins/crew-agents/tools/subagent-statusline.py"}`.

With the `quota-pace` cost tools next to it (same clone, or installed by `install.sh` / `install.ps1`, the default install) the row also shows what the agent cost: `... · 223k · ₺205 (₺15) · 15sa 5dk`. The first amount is its tokens at Anthropic API list prices (read incrementally from the agent's own transcript, one price table); the amount in parentheses is what that usage really cost on your subscription plan (shown once a quota calibration exists). Amounts are in lira at the TCMB rate. Details, plan setting (`plan_usd_monthly` in `~/.claude/pace-plan.json`) and limitations: `plugins/quota-pace/COST.md`. Without those tools the row is unchanged.

The row also ends with the agent's last activity, so an agent that sits idle no longer looks busy: `... · 5sa 32dk · 12dk sessiz · boşta` (the `<age> sessiz` part appears only after 10 minutes without progress, constant `SILENT_AFTER`; hidden below). The age is the last assistant message or tool result in the agent's transcript (the file mtime when it has none). `boşta` is added when the task is still `running` but its last turn ended (last assistant message has `stop_reason: end_turn`, no `tool_use`, nothing after it, at least 30 s old), e.g. the agent finished while a test process it started keeps the row registered. Only the last 256 KB of the transcript is read, cached per file mtime in `~/.claude/subagent-last-cache.json`; no model calls; on any error the row is unchanged. The last stdin JSON of this command is kept in `~/.claude/subagent-statusline-input.json` as a debugging aid.

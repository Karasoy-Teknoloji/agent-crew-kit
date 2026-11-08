# agent-crew-kit

A team kit for [Claude Code](https://docs.claude.com/en/docs/claude-code): a ladder of subagents, working rules you import into `CLAUDE.md`, safety hooks, a weekly quota pace forecast for the status line, and a Vault helper that uses secrets without ever printing them.

It grew out of the day-to-day setup of a small software team and is published as-is. **Everything is optional**: nothing is forced on anyone, there are no managed settings. Take the parts you want, change them, or keep your own variant on a branch.

The kit started in a Turkish-speaking team and was converted to English for everyone. Tool names, flags, messages and docs are English.

> Not affiliated with or endorsed by Anthropic. Claude and Claude Code are trademarks of Anthropic, PBC.

## Contents

| Path | What it is |
|---|---|
| `agents/` | The agent set, named `<role>-<model><effort>` (model h/s/o/f = Haiku/Sonnet/Opus/Fable, effort m/h/xh/max): the escalation ladder `quick-task-hm` → `developer-sm` → `hard-task-sh` → `senior-developer-oh` → `principal-engineer-omax`, plus `deep-analysis-fh` (a different model family for a second opinion) and role agents `research-sm`, `reviewer-oh`, `verifier-hm`, `operations-sm`, `release-ops-oh`, `security-oh`, `panel-reader-sm`, `browser-task-hm`, `long-task-fxh`, `explore`. Ladder table: [`plugins/crew-agents/README.md`](plugins/crew-agents/README.md). |
| `rules/WORKING-RULES.md` | Team working rules for agents: single code path, reuse before adding, rollback-safe releases, evidence before claims, no lost work, GitHub API budget, hand-overs. |
| `rules/AUTONOMY.md` | Claude decides instead of asking, delegates to background agents, watches them, and adds no restrictions you did not ask for. |
| `plugins/quota-pace/` | Quota pace: weekly forecast aimed N hours (default 5) before the reset, PLENTY/NORMAL/TIGHT mode, status line for Linux, macOS and Windows, local cost statistics, opt-in team usage ranking, opt-in "last window" runs. Docs: [`INSTALL.md`](plugins/quota-pace/INSTALL.md), [`COST.md`](plugins/quota-pace/COST.md). |
| `plugins/crew-agents/` | Tools of the agent set: subagent status line, a hook that names every spawned subagent, a hand-over script, the `proactive-concise` output style. |
| `tools/vault_cli.py` | Use Vault secrets without seeing them (`run`, `check`, `fields`), find the right credential first (`find`: path, field-name and description search, never values). See [`docs/vault_cli.md`](docs/vault_cli.md). |
| `tools/evidence_hook.py` | Evidence hook (Stop/SubagentStop). See "Evidence hook" below. |
| `tools/gh_limit.py` | Machine-wide `gh` wrapper that keeps all sessions inside the GitHub API limits (concurrency, pacing, ETag cache, back-off on 403/429). |
| `tools/gh_board.py` | GitHub Projects v2 helper: cached ids, batched board updates, item lists, management progress view. |
| `tools/lost_work.py`, `daily_lost_work.py`, `lost_work_cache.py` | Lost-work check: unpushed, uncommitted or stale work across your repos, with a lossless daily push and cleanup. |
| `tools/rules_watch.py` | Session-start hook: change warnings for your rule files, background pull of this clone with self-check and rollback, lost-work report. |
| `tools/agent_watchdog_note.py`, `notifier.py`, `tool_check.py`, `hidden*.py`, `runlog.py` | Agent watchdog reminder, desktop notifications, missing-tool check, hidden process launch on Windows, rolling logs. |
| `tools/data-masking/` | `trim.py`: masks personal data (national ids, phones, e-mail, IBAN, cards) in tool output before the model sees it. |
| `docs/` | Cache hygiene, browser MCP setup, tool requirements, the Vault helper. |

## Install

Requirements: Python 3.10+, git, `jq` on Linux/macOS (status line), PowerShell 7 on Windows, the `gh` CLI for the GitHub tools.

```
git clone https://github.com/<owner>/agent-crew-kit ~/source/repos/agent-crew-kit   # <owner>: see the line at the end
cd ~/source/repos/agent-crew-kit
bash plugins/quota-pace/install.sh                   # Linux / macOS
pwsh -File plugins/quota-pace/install.ps1            # Windows
```

The installer points everything at the clone instead of copying (status line, hooks, the daily lost-work job, `~/.claude/agents/claude-shared` → `<clone>/agents`, the plugin marketplace as a local directory), backs up `~/.claude/settings.json` before it changes it, and ends with a self-check. `--remove` / `-Remove` removes it again; `--whatif` / `-WhatIf` shows the changes without writing. All options: the header of `install.sh` / `install.ps1` and [`INSTALL.md`](plugins/quota-pace/INSTALL.md).

The plugins alone, without the installer:

```
/plugin marketplace add <owner>/agent-crew-kit
/plugin install quota-pace@claude-shared
/plugin install crew-agents@claude-shared
```

(The marketplace keeps its original name `claude-shared`, which the installer and existing settings use.)

### Rules

Plugins cannot carry `CLAUDE.md` rules, so import the rule files from your own `~/.claude/CLAUDE.md`:

```
@~/source/repos/agent-crew-kit/rules/WORKING-RULES.md
@~/source/repos/agent-crew-kit/rules/AUTONOMY.md
```

Adapt the placeholders (`<org>`, `<team>`, `<product>`, the status channel) to your team.

### Your own version

```
git -C ~/source/repos/agent-crew-kit checkout -b personal/<name>
```

Edit the rule files on that branch and commit. Changes on `main` reach you only when you merge them; the session-start hook fast-forwards a `personal/*` branch that has no commits of its own and tells you when it is behind.

### Change warnings

`tools/rules_watch.py` runs at session start and stays silent unless something needs attention: an imported rule file is missing, your rule files changed, or `main` has new rule commits. The hook itself never touches the network; one detached background run does the fetch, the pull with self-check and rollback, and the lost-work scan, and the next prompt shows the result. Commit text is untrusted: it is shown to you only, the model gets fixed wording. Watched paths: `RULES_WATCH_PATHS` (default `rules,plugins/quota-pace`).

### Evidence hook

`tools/evidence_hook.py` blocks the assistant's final message when it (1) hedges ("probably", "likely", ...), (2) claims missing access or asks for a credential without a `vault_cli.py find` search in the same turn, or (3) answers a decision from the user without recording it on GitHub (or a `Decision record: ...` line). It never loops and fails open. The installer registers it for `Stop` and `SubagentStop`.

## Configuration

Every setting has a documented default, so the kit works standalone. One reader, `tools/settings.py`, resolves every key below in this order:

1. the environment variable;
2. the first line of `~/.config/claude-shared/<file>` (your own setting);
3. the first line of `<clone>/team-config/<file>`, where a team that runs its own fork of this kit commits its values, so every member gets them with `git pull` (never for the personal keys `person`, `plan` and `root`);
4. the default.

| Key | File | Default | Used by |
|---|---|---|---|
| `VAULT_ADDR`, `VAULT_TOKEN` | `~/.vault-token` | none: `vault_cli.py` exits with "VAULT_ADDR is not set" | `tools/vault_cli.py` |
| `VAULT_FIND_EXCLUDING` | `vault-find-excluding` | none | `vault_cli.py find`: comma-separated `<mount>/<path>/` prefixes never listed or read (e.g. an alerting backup tier) |
| `GH_ORG` | `org` | none: the GitHub org features are off | `tools/gh_board.py` (required), the ranking's 7-day commit column, the last-window issue search |
| `GH_BOARD_TEST_QUEUE` | `test-queue` | `0` (none) | `gh_board.py`: number of an org-wide test-queue project |
| `CLAUDE_SHARED_REMOTE` | `remote` | none: the usage ranking and the last-window journal are off | clone URL of the team's shared usage repo |
| `CLAUDE_SHARED_REPORT_DIR` | `report-dir` | `reports/usage` | report folder inside that repo |
| `CLAUDE_SHARED_PEOPLE` | `people-path` | `plugins/quota-pace/people.json` | the people list inside that repo |
| `CLAUDE_SHARED_LAST_WINDOW_DIR` | `last-window-dir` | `reports/last-window` | last-window log folder inside that repo |
| `CLAUDE_SHARED_PERSON`, `CLAUDE_SHARED_PLAN` | `person`, `plan` | `git config user.name`; none | your name in the ranking; plan size (100 or 200) |
| `CLAUDE_SHARED_ROOT` | `root` | `~/source/repos`, else `~/src` | folder that holds your clones (daily commit count) |
| `LAST_WINDOW_REPOS` | `product-repos` | none | product repos for the last-window label and CI checks |
| `LAST_WINDOW_CHANNEL`, `LAST_WINDOW_CHANNEL_ID` | `channel`, `channel-id` | `#ai-status`; none | the team's AI status channel |
| `COST_SEPARATE_BOARDS` | | none | project numbers never counted as finished work |
| `PACE_TARGET_HOURS` | | `5` | hours before the weekly reset the forecast aims at |

The people list of the shared usage repo (`people.json`):

```json
{
  "github-login": {"name": "Display Name", "role": "developer"}
}
```

Roles: `developer` (developer: picks board work), `tester` (tester), `manager` (management). The short form `"github-login": "Display Name"` is a developer. Any other role value, or an object without `role`, is "no known role" and fails closed: that person never picks board work or owns triage, the ranking lists them under "no known role (takes no work)", and the job logs a warning.

### Team fork

A team can run its own fork of this kit and merge kit `main` into it. It keeps its values in committed `team-config/<file>` files (keys above) instead of editing shared code. If the fork ships `tools/team_migrate.py`, the installer and the `rules_watch.py` background run call `team_migrate.py --ensure` first (before the saved install record or `quota-pace.json` is read), with a timeout. The call fails open: a failure or a timeout is reported and the install or update goes on. The kit itself ships no such script.

### Usage ranking (optional)

With a shared repo configured, `install.sh --share --plan 100` (Windows `install.ps1 -Share -Plan 100`) checks your name against `people.json` and your push access, then a job writes one usage row per day and regenerates `RANKING.md` (quota table, cost per finished item, $100/$200 plan comparison). Only the name and the numbers leave the machine. Details: [`COST.md`](plugins/quota-pace/COST.md).

## Optional team add-ons

The installer keeps three opt-in flags whose scripts are not part of this kit: `--slack-inbox` (token-free Slack inbox poller), `--slack-triage` (turns free-text Slack/mail requests into GitHub issues) and `--stale` (daily stale-branch cleanup with a report). They run a team's own `tools/slack-triage/` folder. Without it the installer stops that option with a clear message and changes nothing else.

### Add-on contract

An add-on placed in `tools/slack-triage/` must use exactly these names; the installer, the status line and the cost report look for them.

| What | Required name |
|---|---|
| Inbox poller (`--slack-inbox`) | `inbox.py` with the commands `poll`, `read`, `count`; `slack-inbox` (the `~/.claude/bin/slack-inbox` link target); `systemd/slack-inbox.service` and `systemd/slack-inbox.timer` |
| Stale-branch job (`--stale`) | `stale.py` with the command `runlog <state dir> <log file>`; `systemd/stale.service` and `systemd/stale.timer` |
| Triage runner (`--slack-triage`) | `task.py`, `run_cmd.sh`, `config.example`, `window.py`; `systemd/slack-triage.service` and `systemd/slack-triage.timer` |
| Environment variables | `SLACK_INBOX_INBOX` (inbox file), `SLACK_INBOX_DATA` (state folder), `SLACK_TRIAGE_CONFIG` (triage config file) |
| State paths | `~/.claude/slack-inbox.jsonl` (inbox), `~/.local/state/slack-inbox/state.json` (poll state), `~/.claude/slack-inbox.conf` (Windows settings), `~/.config/slack-triage/config` (triage config) |
| Unit and task names | systemd user units `slack-inbox`, `stale`, `slack-triage`; launchd label `com.agentcrewkit.slack-inbox`; Windows tasks `slack-inbox`, `slack-triage` |

## Tests

```
python -m unittest discover -s plugins/quota-pace/tests
python -m unittest discover -s plugins/crew-agents/tests
python -m unittest discover -s tools/tests
python -m unittest discover -s tools/data-masking   # Linux only, like the masking hook itself
```

The workflow `.github/workflows/statusline-platforms.yml` (manual) installs and renders the status line on Linux, macOS and Windows.

## Contributing, security, licence

Issues are welcome; pull requests are reviewed before merge. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Licence: [MIT](LICENSE).

Maintained by Karasoy Teknoloji (`<owner>` = Karasoy-Teknoloji).

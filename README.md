# agent-crew-kit

A team kit for [Claude Code](https://docs.claude.com/en/docs/claude-code): a ladder of subagents, working rules you import into `CLAUDE.md`, safety hooks, a weekly quota pace forecast for the status line, and a Vault helper that uses secrets without ever printing them.

It grew out of the day-to-day setup of a small software team and is published as-is. **Everything is optional**: nothing is forced on anyone, there are no managed settings. Take the parts you want, change them, or keep your own variant on a branch.

Many tool names, messages and some docs are Turkish (the original team's language); code comments and the agent prompts are English. Turkish words you will meet: *kota* quota, *kur* install, *kaldir* remove, *kasa* safe (the Vault helper), *bul* find, *maliyet* cost, *son pencere* last window, *kayıp iş* lost work, *ajan* agent.

> Not affiliated with or endorsed by Anthropic. Claude and Claude Code are trademarks of Anthropic, PBC.

## Contents

| Path | What it is |
|---|---|
| `agents/` | The agent set, named `<role>-<model><effort>` (model h/s/o/f = Haiku/Sonnet/Opus/Fable, effort m/h/xh/max): the escalation ladder `quick-task-hm` → `developer-sm` → `hard-task-sh` → `senior-developer-oh` → `principal-engineer-omax`, plus `deep-analysis-fh` (a different model family for a second opinion) and role agents `research-sm`, `reviewer-oh`, `verifier-hm`, `operations-sm`, `release-ops-oh`, `security-oh`, `panel-reader-sm`, `browser-task-hm`, `long-task-fxh`, `explore`. Ladder table: [`plugins/ekip-ajanlari/README.md`](plugins/ekip-ajanlari/README.md). |
| `rules/WORKING-RULES.md` | Team working rules for agents: single code path, reuse before adding, rollback-safe releases, evidence before claims, no lost work, GitHub API budget, hand-overs. |
| `rules/AUTONOMY.md` | Claude decides instead of asking, delegates to background agents, watches them, and adds no restrictions you did not ask for. |
| `plugins/kota-temposu/` | Quota pace ("kota temposu"): weekly forecast aimed N hours (default 5) before the reset, PLENTY/NORMAL/TIGHT mode, status line for Linux, macOS and Windows, local cost statistics, opt-in team usage ranking, opt-in "last window" runs. Turkish docs: [`KURULUM.md`](plugins/kota-temposu/KURULUM.md), [`MALIYET.md`](plugins/kota-temposu/MALIYET.md). |
| `plugins/ekip-ajanlari/` | Tools of the agent set: subagent status line, a hook that names every spawned subagent, a hand-over script, the `proaktif-kisa` output style. |
| `tools/kasa.py` | Use Vault secrets without seeing them (`run`, `check`, `fields`), find the right credential first (`bul`: path, field-name and description search, never values). See [`docs/kasa.md`](docs/kasa.md). |
| `tools/kanit_kancasi.py` | Evidence hook (Stop/SubagentStop). See "Evidence hook" below. |
| `tools/gh_sinir.py` | Machine-wide `gh` wrapper that keeps all sessions inside the GitHub API limits (concurrency, pacing, ETag cache, back-off on 403/429). |
| `tools/gh_board.py` | GitHub Projects v2 helper: cached ids, batched board updates, item lists, management progress view. |
| `tools/kayip_is.py`, `gunluk_kayip.py`, `kayip_son.py` | Lost-work check: unpushed, uncommitted or stale work across your repos, with a lossless daily push and cleanup. |
| `tools/rules_watch.py` | Session-start hook: change warnings for your rule files, background pull of this clone with self-check and rollback, lost-work report. |
| `tools/ajan_bekci_notu.py`, `bildirim.py`, `arac_kontrol.py`, `gizli*.py`, `gunluk.py` | Agent watchdog reminder, desktop notifications, missing-tool check, hidden process launch on Windows, rolling logs. |
| `tools/veri-maskeleme/` | `kirp.py`: masks personal data (national ids, phones, e-mail, IBAN, cards) in tool output before the model sees it. |
| `docs/` | Cache hygiene, browser MCP setup, tool requirements, the Vault helper. |

## Install

Requirements: Python 3.10+, git, `jq` on Linux/macOS (status line), PowerShell 7 on Windows, the `gh` CLI for the GitHub tools.

```
git clone https://github.com/<owner>/agent-crew-kit ~/source/repos/agent-crew-kit   # <owner>: see the line at the end
cd ~/source/repos/agent-crew-kit
bash plugins/kota-temposu/kur.sh                   # Linux / macOS
pwsh -File plugins/kota-temposu/kur.ps1            # Windows
```

The installer points everything at the clone instead of copying (status line, hooks, the daily lost-work job, `~/.claude/agents/claude-shared` → `<clone>/agents`, the plugin marketplace as a local directory), backs up `~/.claude/settings.json` before it changes it, and ends with a self-check. `--kaldir` / `-Kaldir` removes it again; `--whatif` / `-WhatIf` shows the changes without writing. All options: the header of `kur.sh` / `kur.ps1` and [`KURULUM.md`](plugins/kota-temposu/KURULUM.md).

The plugins alone, without the installer:

```
/plugin marketplace add <owner>/agent-crew-kit
/plugin install kota-temposu@claude-shared
/plugin install ekip-ajanlari@claude-shared
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

`tools/rules_watch.py` runs at session start and stays silent unless something needs attention: an imported rule file is missing, your rule files changed, or `main` has new rule commits. The hook itself never touches the network; one detached background run does the fetch, the pull with self-check and rollback, and the lost-work scan, and the next prompt shows the result. Commit text is untrusted: it is shown to you only, the model gets fixed wording. Watched paths: `RULES_WATCH_PATHS` (default `rules,plugins/kota-temposu`).

### Evidence hook

`tools/kanit_kancasi.py` blocks the assistant's final message when it (1) hedges ("probably", "likely", "muhtemelen", ...), (2) claims missing access or asks for a credential without a `kasa.py bul` search in the same turn, or (3) answers a decision from the user without recording it on GitHub (or a `Karar kaydı: ...` line). It never loops and fails open. The installer registers it for `Stop` and `SubagentStop`.

## Configuration

Every setting has a documented default, so the kit works standalone. A key is read from the environment, or (where a file name is given) from the first line of `~/.config/claude-shared/<file>`.

| Key | File | Default | Used by |
|---|---|---|---|
| `VAULT_ADDR`, `VAULT_TOKEN` | `~/.vault-token` | none: `kasa.py` exits with "VAULT_ADDR is not set" | `tools/kasa.py` |
| `KASA_BUL_HARIC` | | none | `kasa.py bul`: comma-separated `<mount>/<path>/` prefixes never listed or read (e.g. an alerting backup tier) |
| `GH_ORG` | `org` | none: the GitHub org features are off | `tools/gh_board.py` (required), the ranking's 7-day commit column, the last-window issue search |
| `GH_BOARD_TEST_KUYRUGU` | | `0` (none) | `gh_board.py`: number of an org-wide test-queue project |
| `CLAUDE_SHARED_UZAK` | `uzak` | none: the usage ranking and the last-window journal are off | clone URL of the team's shared usage repo |
| `CLAUDE_SHARED_RAPOR_DIZIN` | `rapor-dizin` | `raporlar/kullanim` | report folder inside that repo |
| `CLAUDE_SHARED_KISILER` | `kisiler-yol` | `plugins/kota-temposu/kisiler.json` | the people list inside that repo |
| `CLAUDE_SHARED_KISI`, `CLAUDE_SHARED_PLAN` | `kisi`, `plan` | `git config user.name`; none | your name in the ranking; plan size (100 or 200) |
| `CLAUDE_SHARED_KOK` | `kok` | `~/source/repos`, else `~/src` | folder that holds your clones (daily commit count) |
| `SON_PENCERE_REPOLAR` | `urun-repolari` | none | product repos for the last-window label and CI checks |
| `SON_PENCERE_KANAL`, `SON_PENCERE_KANAL_ID` | `kanal`, `kanal-id` | `#ai-status`; none | the team's AI status channel |
| `MALIYET_AYRI_PANOLAR` | | none | project numbers never counted as finished work |
| `PACE_HEDEF_SAAT` | | `5` | hours before the weekly reset the forecast aims at |

The people list of the shared usage repo (`kisiler.json`):

```json
{
  "github-login": {"ad": "Display Name", "rol": "yazilimci"}
}
```

Roles: `yazilimci` (developer: picks board work), `testci` (tester), `yonetici` (management).

### Usage ranking (optional)

With a shared repo configured, `kur.sh --paylas --plan 100` (Windows `kur.ps1 -Paylas -Plan 100`) checks your name against `kisiler.json` and your push access, then a job writes one usage row per day and regenerates `SIRALAMA.md` (quota table, cost per finished item, $100/$200 plan comparison). Only the name and the numbers leave the machine. Details (Turkish): [`MALIYET.md`](plugins/kota-temposu/MALIYET.md).

## Optional team add-ons

The installer keeps three opt-in flags whose scripts are not part of this kit: `--slack-gelen` (token-free Slack inbox poller), `--slack-triage` (turns free-text Slack/mail requests into GitHub issues) and `--bayat` (daily stale-branch cleanup with a report). They run a team's own `tools/slack-triage/` folder. Without it the installer stops that option with a clear message and changes nothing else.

## Tests

```
python -m unittest discover -s plugins/kota-temposu/tests
python -m unittest discover -s plugins/ekip-ajanlari/tests
python -m unittest discover -s tools/tests
python -m unittest discover -s tools/veri-maskeleme   # Linux only, like the masking hook itself
```

The workflow `.github/workflows/statusline-platformlar.yml` (manual) installs and renders the status line on Linux, macOS and Windows.

## Contributing, security, licence

Issues are welcome; pull requests are reviewed before merge. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Licence: [MIT](LICENSE).

Maintained by Karasoy Teknoloji (`<owner>` = Karasoy-Teknoloji).

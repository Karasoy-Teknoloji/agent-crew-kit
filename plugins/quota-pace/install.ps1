# Installer for quota-pace (Windows, PowerShell 5.1 and 7).
#   pwsh -File install.ps1            install / repair
#   pwsh -File install.ps1 -WhatIf    show the changes, write nothing
#   pwsh -File install.ps1 -Remove    remove our files and settings entries (also the optional cost statistics; the recorded history stays)
#   pwsh -File install.ps1 -Free  install / repair without the cost statistics. They are part of the default install (cost.cmd + a per-user
#                                 daily Task Scheduler task 'cost-daily', no admin needed; -Remove removes the task); -Cost is accepted and means the default
#   pwsh -File install.ps1 -Share -Plan 100 [-Person "First Last"]
#                                 join the team usage ranking: checks the name (people.json) and push access, writes
#                                 %USERPROFILE%\.config\claude-shared\person and plan, and makes the daily task run `cost --save --share`.
#                                 -Person is only needed when your git user.name differs from your name in people.json.
#   pwsh -File install.ps1 -LastWindow [-Person "First Last"]
#                                 opt in to the "last window" mode (leftover weekly quota is used on pre-approved work in the last hours before the
#                                 reset; see INSTALL.md). Installs last-window.cmd and a per-user Task Scheduler task 'last-window' (every 5 min; it runs only
#                                 while you are logged on, because the work goes to a live interactive Claude session of yours or opens a visible
#                                 Windows Terminal tab 'last-window-<name>' -- the Windows part of this is UNTESTED);
#                                 needs the same name/push access as -Share (not the plan). -Remove removes the task.
#   pwsh -File install.ps1 -ResumeAlarm [-Person "First Last"]
#                                 opt in to the 5-hour resume alarm (same last-window command and task, every 5 min): once the 5-hour window
#                                 that stopped work has reset, every live Claude session still stopped by that limit gets one "continue" relay.
#                                 The same task runs burn mode (`last-window burn --on`, developers only). Saved for bare re-runs.
#   pwsh -File install.ps1 -SlackInbox
#                                 opt in to the token-free Slack inbox poller for whoever runs the installer. Runs tools/slack-triage/inbox.py from this
#                                 clone every 5 min (per-user Task Scheduler task 'slack-inbox', starts at :03, no console window, no admin), writes
#                                 ~/.claude/bin/slack-inbox.cmd (prints and marks read the unread items) and adds SessionStart + UserPromptSubmit hooks that print
#                                 "Slack: N new" (the status line shows it too). Needs a Slack user token: the claude CLI's Slack MCP login, or a Vault field named by
#                                 SLACK_TOKEN_VAULT in ~/.claude/slack-inbox.conf (see tools/slack-triage/README.md). Saved for bare re-runs;
#                                 -Remove removes the task.
#   pwsh -File install.ps1 -SlackTriage
#                                 run the Slack/mail request triage on this machine too (every developer's machine may, one owns it; tools/slack-triage/README.md
#                                 "Several machines"). Per-user Task Scheduler task 'slack-triage' running tools/slack-triage/task.py from this clone
#                                 every few minutes (headless conhost, no window at all, no admin; needs Git Bash, the claude CLI's Slack login, gh).
#                                 Copies config.example to ~/.config/slack-triage/config when missing. Saved for bare re-runs; -Remove removes the task.
#   pwsh -File install.ps1            (default = direct source) the scripts run straight from this claude-shared clone, nothing is copied into
#                                 %USERPROFILE%\.claude\bin: settings.json, cost.cmd and the daily task point into the clone, so updates
#                                 arrive with git pull (tools/rules_watch.py does it at session start with a self-check and a rollback).
#                                 Copies of an older install are backed up (.backup-<date>) and removed. Every run keeps the -Share/-Person/
#                                 -Plan/-LastWindow options saved by the last install and adds the flags given (none is dropped; -Remove undoes all).
#   pwsh -File install.ps1            (every mode) also installs the team's lost-work protection from this clone: the SessionStart hook
#                                 tools/rules_watch.py (rule warnings + own unpushed/uncommitted work report) and a per-user daily task
#                                 'lost-work' running tools/lost_work.py --push-unpushed (own branches only, never main/master/test/dev, never force).
#   pwsh -File install.ps1            (every mode) also writes the `gh` shims ~/.local/bin/gh (Git Bash) and gh.ps1 (pwsh) -> tools/gh_limit.py of
#                                 this clone (machine-wide GitHub API limiter) and a marked block in the pwsh all-hosts profile that puts
#                                 ~/.local/bin before gh.exe on PATH (the machine PATH always wins over the user PATH). Applies to sessions
#                                 started from a new pwsh window; see docs in tools/gh_limit.py.
#   pwsh -File install.ps1            (every mode) also prints the tool check (tools/tool_check.py: python3, git, gh, jq, poppler, tesseract, shellcheck, ...)
#                                 and, when no real python3 is on PATH, writes the shims ~/.local/bin/python3.cmd / python3 (-> `py -3`).
#   pwsh -File install.ps1 -Tools   consent flag of that check: also installs the missing tools with winget (pip packages with --user); see
#                                 docs/tool-requirements.md. Without it the check only reports. Not saved for bare re-runs.
#   pwsh -File install.ps1            (every mode) also links the team agents: ~/.claude/agents/claude-shared -> <clone>/agents (a directory
#                                 junction, no admin; a link, not a copy; own agents in ~/.claude/agents are not touched; -Remove removes
#                                 the link), and enables the plugin plugins/crew-agents (tools, output style) from this clone: settings.json
#                                 declares the claude-shared marketplace as a local directory source (plugins load in place, no copy), enables
#                                 crew-agents@claude-shared and `claude plugin install` registers it (idempotent, skipped when `claude` is
#                                 not on PATH); old-name agent files in ~/.claude/agents are backed up (.backup-<date>) and removed.
#   pwsh -File install.ps1 -Copy     legacy copy mode (every update needs a re-run). -Direct is accepted (= default).
# The real work is in install_common.py (shared with install.sh); this script only finds Python and pwsh.
param(
    [switch]$WhatIf,
    [switch]$Remove,
    [switch]$Cost,
    [switch]$Free,
    [switch]$Share,
    [switch]$LastWindow,
    [switch]$ResumeAlarm,
    [switch]$SlackInbox,
    [switch]$SlackTriage,
    [switch]$Tools,
    [string]$Person,
    [string]$Plan,
    [switch]$Direct,
    [switch]$Copy
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Test-Python([string]$exe) {
    if (-not $exe -or -not (Test-Path $exe)) { return $false }
    if ($exe -like '*\WindowsApps\*') { return $false }   # Store stub, not a real interpreter
    try { & $exe -c 'import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)' 2>$null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

$python = $null
$py = Get-Command py.exe -ErrorAction SilentlyContinue
if ($py) {
    try { $c = (& $py.Source -3 -c 'import sys; print(sys.executable)' 2>$null | Select-Object -First 1) } catch { $c = $null }
    if (Test-Python $c) { $python = $c }
}
if (-not $python) {
    $dir = Join-Path $env:LOCALAPPDATA 'Programs\Python'
    $c = Get-ChildItem $dir -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName 'python.exe' } |
        Where-Object { Test-Python $_ } | Select-Object -First 1
    if ($c) { $python = $c }
}
if (-not $python) {
    $p = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($p -and (Test-Python $p.Source)) { $python = $p.Source }
}
if (-not $python) { Write-Host 'ERROR: Python 3 not found. Install Python 3 and run again.'; exit 2 }

$shell = $null
foreach ($n in 'pwsh.exe', 'powershell.exe') {
    $c = Get-Command $n -ErrorAction SilentlyContinue
    if ($c) { $shell = $c.Source; break }
}
if (-not $shell) { Write-Host 'ERROR: pwsh/powershell not found.'; exit 2 }

$argv = @('-I', (Join-Path $PSScriptRoot 'install_common.py'), '--windows', '--python', $python, '--shell', $shell)
if ($WhatIf) { $argv += '--whatif' }
if ($Remove) { $argv += '--remove' }
if ($Free) { $argv += '--free' }
if ($Share) { $argv += '--share' }
if ($LastWindow) { $argv += '--last-window' }
if ($ResumeAlarm) { $argv += '--resume-alarm' }
if ($SlackInbox) { $argv += '--slack-inbox' }
if ($SlackTriage) { $argv += '--slack-triage' }
if ($Tools) { $argv += '--tools' }
if ($Direct) { $argv += '--direct' }
if ($Copy) { $argv += '--copy' }
if ($PSBoundParameters.ContainsKey('Person')) { $argv += '--person'; $argv += $Person }
if ($PSBoundParameters.ContainsKey('Plan')) { $argv += '--plan'; $argv += $Plan }
& $python @argv
exit $LASTEXITCODE

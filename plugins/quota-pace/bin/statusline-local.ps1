# Status line wrapper (quota-pace): fixes the Python path, then runs statusline.ps1.
# Why: pace_forecast.py starts in the background with `pythonw`; when pythonw is not on PATH the forecast silently stops updating.
# statusline.ps1 runs in the SAME process (no second pwsh is started: starting pwsh is the most expensive part of this line).
# For personal warnings write an optional %USERPROFILE%\.claude\bin\statusline-extra.ps1: param([string]$Line), returns the changed line;
# install.ps1 never touches that file.
# The settings.json statusLine command calls this script:
#   pwsh -NoProfile -File C:\Users\USER\.claude\bin\statusline-local.ps1
# The paths are parameters. statusline.ps1 sits one folder above this script (~/.claude in a copy install, plugins/quota-pace when run
# from the clone); ClaudeDir (settings.json, the personal statusline-extra.ps1) is %USERPROFILE%\.claude in both cases.
# Line wrapping: when all segments fit the terminal width, ONE line (the same bytes as before); otherwise it moves greedily to a second
# (if needed a third) line, first at segment boundaries (" | "), splitting a segment that does not fit at its " · " parts; no part is
# cut or split. Width: QUOTA_LINE_WIDTH (manual) > COLUMNS (Claude Code passes it to the status line process; on Windows
# [Console]::WindowWidth and `mode con` are wrong or fail with redirected stdio) > 120. Margin: QUOTA_LINE_MARGIN (default 4 = the
# measured Claude Code margin, + settings.json statusLine.padding x 2). Wrapping runs AFTER the statusline-extra.ps1 output (personal
# warnings count too).
param(
    [string]$ClaudeDir = (Join-Path $env:USERPROFILE '.claude'),
    [string]$StatusLine = (Join-Path (Split-Path $PSScriptRoot -Parent) 'statusline.ps1'),
    [string]$Extra = (Join-Path $ClaudeDir 'bin/statusline-extra.ps1')
)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# Put the pythonw from %LOCALAPPDATA%\Programs\Python\Python3*\ on PATH (newest version first)
if (-not (Get-Command pythonw -ErrorAction SilentlyContinue)) {
    $py = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        Where-Object { Test-Path (Join-Path $_.FullName 'pythonw.exe') } |
        Select-Object -First 1
    if ($py) { $env:PATH = $py.FullName + ';' + $env:PATH }
}

# This process does not read stdin: statusline.ps1 reads it itself
# Running straight from the clone, no error may ever show in the status line: when the script fails or returns nothing, at least 'Claude' is printed
try { $line = (& $StatusLine) -join '' } catch { $line = '' }
if (-not $line) { $line = 'Claude' }
if (Test-Path $Extra) {
    try { $line = [string](& $Extra -Line $line) } catch {}
}

# Number of screen cells: ANSI escapes do not count, wide characters/emoji 2, zero-width 0
function Get-CellWidth([string]$Text) {
    $m = $Text -replace '\x1b\[[0-9;?]*[ -/]*[@-~]', '' -replace '\x1b\][^\x07\x1b]*(\x07|\x1b\\)', ''
    $w = 0
    for ($i = 0; $i -lt $m.Length; $i++) {
        $cp = [int]$m[$i]
        if ([char]::IsHighSurrogate($m[$i]) -and ($i + 1) -lt $m.Length -and [char]::IsLowSurrogate($m[$i + 1])) {
            $cp = [char]::ConvertToUtf32($m[$i], $m[$i + 1]); $i++
        }
        if (($cp -ge 0x300 -and $cp -le 0x36F) -or ($cp -ge 0x200B -and $cp -le 0x200F) -or ($cp -ge 0xFE00 -and $cp -le 0xFE0F)) { continue }
        if (($cp -ge 0x1100 -and $cp -le 0x115F) -or ($cp -ge 0x2E80 -and $cp -le 0xA4CF) -or ($cp -ge 0xAC00 -and $cp -le 0xD7A3) -or
            ($cp -ge 0xF900 -and $cp -le 0xFAFF) -or ($cp -ge 0xFE30 -and $cp -le 0xFE6F) -or ($cp -ge 0xFF00 -and $cp -le 0xFF60) -or
            ($cp -ge 0xFFE0 -and $cp -le 0xFFE6) -or ($cp -ge 0x1F300 -and $cp -le 0x1FAFF) -or ($cp -ge 0x20000 -and $cp -le 0x3FFFD)) { $w += 2 } else { $w += 1 }
    }
    $w
}

# Width: manual setting > COLUMNS > 120
$width = 0
foreach ($name in 'QUOTA_LINE_WIDTH', 'COLUMNS') {
    $v = [Environment]::GetEnvironmentVariable($name)
    if ("$v".Trim() -match '^\d{1,4}$' -and [int]$v.Trim() -ge 20) { $width = [int]$v.Trim(); break }
}
if ($width -eq 0) { $width = 120 }
# Margin, measured on the real screen with Claude Code 2.1.310 (tests/test_real_screen.py; full-screen and classic view, 80-250 columns):
# a status line shows at most (columns - 4) cells (2 indent on the left, 2 spaces on the right); the rest is cut and ends with "…", it is
# NOT wrapped. The settings.json statusLine.padding is added on both sides: margin = 4 + 2 x padding. A given QUOTA_LINE_MARGIN wins.
$margin = 4
if ("$env:QUOTA_LINE_MARGIN".Trim() -match '^\d{1,3}$') { $margin = [int]$env:QUOTA_LINE_MARGIN.Trim() }
else {
    try {
        $raw = Get-Content (Join-Path $ClaudeDir 'settings.json') -Raw -ErrorAction Stop
        if ($raw -match '"padding"') {
            $pd = ($raw | ConvertFrom-Json).statusLine.padding
            if ($pd -is [ValueType] -and [int]$pd -gt 0 -and [int]$pd -le 50) { $margin += 2 * [int]$pd }
        }
    } catch {}
}
$limit = [Math]::Max($width - $margin, 20)

# Wrap each line (each one, when the extension already put line breaks in) greedily piece by piece: the pieces are the " | " segments
# and the " · " parts inside a segment; a piece that fits is added with its own separator, one that does not starts a new line (a line
# never starts with a separator, a piece is never split)
$result = foreach ($raw in ($line -split "`r?`n")) {
    if ((Get-CellWidth $raw) -le $limit) { $raw; continue }
    $cur = $null
    foreach ($seg in ($raw -split ' \| ')) {
        $i = 0
        foreach ($p in ($seg -split ' · ')) {
            $delimiter = if ($i -eq 0) { ' | ' } else { ' · ' }
            $i++
            if ($null -eq $cur) { $cur = $p }
            elseif ((Get-CellWidth "$cur$delimiter$p") -le $limit) { $cur = "$cur$delimiter$p" }
            else { $cur; $cur = $p }
        }
    }
    $cur
}
$result -join "`n"

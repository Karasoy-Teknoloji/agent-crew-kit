# NOTE (0.3.3): statusline-local.ps1 now wraps by terminal width on its own, so this file is no longer needed. It is kept for anyone who wants a fixed
# split at the folder segment regardless of width; it still works (its two lines are wrapped further if they are too long for the terminal).
# Optional personal hook for statusline-local.ps1 (Windows): splits the status line in two so it fits narrow terminals.
# Line 1: model, context, time, quota. Line 2: from the branch/folder segment on (folder, cache, cost).
# Install: copy to %USERPROFILE%\.claude\bin\statusline-ek.ps1 (the installer never touches that file).
param([string]$Line)
$part = $Line -split ' \| '
$i = 0
for ($k = 0; $k -lt $part.Count; $k++) { if ($part[$k] -match '^(\[|~|[A-Za-z]:)') { $i = $k; break } }
if ($i -le 0) { return $Line }
($part[0..($i - 1)] -join ' | ') + "`n" + ($part[$i..($part.Count - 1)] -join ' | ')

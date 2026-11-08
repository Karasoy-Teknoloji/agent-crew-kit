# Hand over a dead or idle agent to a new one: prints the first task text, later instructions
# and the last N assistant messages (tool output excluded) from the session transcript.
# Usage: pwsh -NoProfile -File agent-handover.ps1 -Id <agent-id> [-Last 20] [-Length 1500] [-Folder <tasks-dir>] [-ProjectFolder <projects-dir>]
param(
    [Parameter(Mandatory)][string]$Id,
    [int]$Last = 20,
    [int]$Length = 1500,
    [string]$Folder = '',
    [string]$ProjectFolder = (Join-Path $HOME '.claude\projects')
)
$f = if ($Folder) { Join-Path $Folder "$Id.output" } else { '' }
# .output stays empty for some agents; the real transcript is in the session's subagents folder
if (-not $f -or -not (Test-Path $f) -or (Get-Item $f).Length -eq 0) {
    $f = Get-ChildItem $ProjectFolder -Recurse -File -Filter "agent-$Id.jsonl" -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $f -or -not (Test-Path $f)) { throw "Transcript not found: $Id" }

function Kisalt([string]$s, [int]$n) { if ($s.Length -gt $n) { $s.Substring(0, $n) + ' ...' } else { $s } }

$user = [System.Collections.Generic.List[string]]::new()
$assistant = [System.Collections.Generic.List[string]]::new()
foreach ($line in [System.IO.File]::ReadLines($f)) {
    try { $o = $line | ConvertFrom-Json -Depth 60 } catch { continue }
    $content = $o.message.content
    if ($o.type -eq 'user') {
        $text = if ($content -is [string]) { $content } else { ($content | Where-Object { $_.type -eq 'text' } | ForEach-Object { $_.text }) -join "`n" }
        if ($text -and $text -notmatch '^<system-reminder>' -and $text -notmatch 'SYSTEM NOTIFICATION') { $user.Add($text) }
    }
    elseif ($o.type -eq 'assistant') {
        $text = ($content | Where-Object { $_.type -eq 'text' } | ForEach-Object { $_.text }) -join "`n"
        if ($text) { $assistant.Add($text) }
    }
}

"===== FIRST TASK ====="
if ($user.Count) { $user[0] }
"`n===== LATER INSTRUCTIONS ($([Math]::Max(0, $user.Count - 1))) ====="
for ($i = 1; $i -lt $user.Count; $i++) { "--- [$i]"; Kisalt $user[$i] 3000 }
"`n===== LAST $Last ASSISTANT MESSAGES (oldest first) ====="
$bas = [Math]::Max(0, $assistant.Count - $Last)
for ($i = $bas; $i -lt $assistant.Count; $i++) { "--- [$i]"; Kisalt $assistant[$i] $Length }

# Hand over a dead or idle agent to a new one: prints the first task text, later instructions
# and the last N assistant messages (tool output excluded) from the session transcript.
# Usage: pwsh -NoProfile -File ajan-devral.ps1 -Id <agent-id> [-Son 20] [-Uzunluk 1500] [-Klasor <tasks-dir>] [-ProjeKlasoru <projects-dir>]
param(
    [Parameter(Mandatory)][string]$Id,
    [int]$Son = 20,
    [int]$Uzunluk = 1500,
    [string]$Klasor = '',
    [string]$ProjeKlasoru = (Join-Path $HOME '.claude\projects')
)
$f = if ($Klasor) { Join-Path $Klasor "$Id.output" } else { '' }
# .output stays empty for some agents; the real transcript is in the session's subagents folder
if (-not $f -or -not (Test-Path $f) -or (Get-Item $f).Length -eq 0) {
    $f = Get-ChildItem $ProjeKlasoru -Recurse -File -Filter "agent-$Id.jsonl" -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $f -or -not (Test-Path $f)) { throw "Transcript not found: $Id" }

function Kisalt([string]$s, [int]$n) { if ($s.Length -gt $n) { $s.Substring(0, $n) + ' ...' } else { $s } }

$kullanici = [System.Collections.Generic.List[string]]::new()
$asistan = [System.Collections.Generic.List[string]]::new()
foreach ($satir in [System.IO.File]::ReadLines($f)) {
    try { $o = $satir | ConvertFrom-Json -Depth 60 } catch { continue }
    $icerik = $o.message.content
    if ($o.type -eq 'user') {
        $metin = if ($icerik -is [string]) { $icerik } else { ($icerik | Where-Object { $_.type -eq 'text' } | ForEach-Object { $_.text }) -join "`n" }
        if ($metin -and $metin -notmatch '^<system-reminder>' -and $metin -notmatch 'SYSTEM NOTIFICATION') { $kullanici.Add($metin) }
    }
    elseif ($o.type -eq 'assistant') {
        $metin = ($icerik | Where-Object { $_.type -eq 'text' } | ForEach-Object { $_.text }) -join "`n"
        if ($metin) { $asistan.Add($metin) }
    }
}

"===== FIRST TASK ====="
if ($kullanici.Count) { $kullanici[0] }
"`n===== LATER INSTRUCTIONS ($([Math]::Max(0, $kullanici.Count - 1))) ====="
for ($i = 1; $i -lt $kullanici.Count; $i++) { "--- [$i]"; Kisalt $kullanici[$i] 3000 }
"`n===== LAST $Son ASSISTANT MESSAGES (oldest first) ====="
$bas = [Math]::Max(0, $asistan.Count - $Son)
for ($i = $bas; $i -lt $asistan.Count; $i++) { "--- [$i]"; Kisalt $asistan[$i] $Uzunluk }

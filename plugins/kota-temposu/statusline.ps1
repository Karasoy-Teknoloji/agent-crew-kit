[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$raw = [Console]::In.ReadToEnd()
$inv = [Globalization.CultureInfo]::InvariantCulture   # numbers are never formatted with the machine's culture (Turkish Windows would print 1,2M)
$d = $raw | ConvertFrom-Json
# Live-session guard: only a payload from a live Claude Code session may write the live state (pace.json, pace-history.csv, the stdin dump,
# forecast and share jobs). ~/.claude/sessions/<pid>.json lists the live sessions; a payload whose session_id is not in it (a test fixture or a
# benchmark run against the real profile) is rendered but writes nothing. No registry, or none with a sessionId = cannot judge = write as before.
# Incident 05.11.2026: tests/fixtures/statusline-girdi.json (63%) run on the live profile made the pace hook say "SIKI, 63%" while the real use was 54%.
$canli = $true
try {
    $sid = [string]$d.session_id
    $kayitli = $false; $bulundu = $false
    foreach ($f in [IO.Directory]::GetFiles("$env:USERPROFILE/.claude/sessions", '*.json')) {
        $m = [regex]::Match([IO.File]::ReadAllText($f), '"sessionId"\s*:\s*"([^"]*)"')
        if ($m.Success) { $kayitli = $true; if ($sid -and $m.Groups[1].Value -ceq $sid) { $bulundu = $true; break } }
    }
    if ($kayitli -and -not $bulundu) { $canli = $false }
} catch {}
# Debug aid (kept permanently): the last stdin JSON Claude Code sent, ~/.claude/statusline-girdi.json (overwritten, atomic)
if ($canli) { try {
    $gDosya = "$env:USERPROFILE/.claude/statusline-girdi.json"
    [IO.File]::WriteAllText("$gDosya.tmp$PID", $raw, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath "$gDosya.tmp$PID" -Destination $gDosya -Force
} catch {} }
$dir = $d.workspace.current_dir
$effort = $d.effort.level
if (-not $effort) {
    try {
        $settings = Get-Content "$env:USERPROFILE/.claude/settings.json" -Raw | ConvertFrom-Json
        $modelId = $d.model.id
        if ($modelId -and $settings.modelSettings.$modelId.effortLevel) {
            $effort = $settings.modelSettings.$modelId.effortLevel + '*'
        } elseif ($settings.effortLevel) {
            $effort = $settings.effortLevel + '*'
        }
    } catch {}
    if (-not $effort) { $effort = 'high*' }
}
# Model name without the "(1M context)" suffix; the effective window is shown next to it
$out = ($d.model.display_name -replace '\s*\([^)]*context\)\s*$', '') + ' · ' + $effort
$cw = $d.context_window
if ($null -ne $cw.used_percentage) {
    $cu = $cw.current_usage
    $used = $cu.input_tokens + $cu.cache_creation_input_tokens + $cu.cache_read_input_tokens
    # Effective window: the auto-compact window when it is smaller than the model's window
    $size = [double]$cw.context_window_size
    $acw = $env:CLAUDE_CODE_AUTO_COMPACT_WINDOW
    if (-not $acw) {
        try {
            $s = Get-Content "$env:USERPROFILE/.claude/settings.json" -Raw | ConvertFrom-Json
            # Per-model value (modelSettings."<canonical id>".autoCompactWindow) beats the top-level one
            $mk = ("$($d.model.id)" -replace '\[.*\]$', '' -replace '-\d{8}$', '')
            $mw = if ($mk -and $s.modelSettings -and $s.modelSettings.$mk) { $s.modelSettings.$mk.autoCompactWindow } else { $null }
            $acw = if ($s.env.CLAUDE_CODE_AUTO_COMPACT_WINDOW) { $s.env.CLAUDE_CODE_AUTO_COMPACT_WINDOW } elseif ($mw) { $mw } else { $s.autoCompactWindow }
        } catch {}
    }
    $acwN = 0
    if ("$acw" -match '^\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*$') {
        $acwN = [double]::Parse($Matches[1], [Globalization.CultureInfo]::InvariantCulture)
        if ($Matches[2] -in 'k', 'K') { $acwN *= 1000 } elseif ($Matches[2] -in 'm', 'M') { $acwN *= 1000000 }
    }
    if ($acwN -gt 0 -and ($size -le 0 -or $acwN -lt $size)) { $size = $acwN }
    $pct = if ($size -gt 0) { [int](100 * $used / $size) } else { [int]$cw.used_percentage }
    $fmt = { param($n) if ($n -ge 1000000) { ($n / 1000000).ToString('0.#', $inv) + 'M' } else { ($n / 1000).ToString('0', $inv) + 'K' } }
    $out += " | %$pct ($(& $fmt $used)/$(& $fmt $size))"
}
# Optional cost statistics (installed by `kur.ps1`, see MALIYET.md): off unless bin/maliyet.py exists next to this script
# (~/.claude/bin for the copied install, plugins/kota-temposu/bin when run from the claude-shared clone, kur.ps1 -Repodan); turn off without
# uninstalling with MALIYET_DURUM=0 or the file ~/.claude/maliyet/kapali. "oturum", the last action's cost and the rolling windows come
# from ONE `maliyet.py --durum --girdi` call (it reads this JSON from stdin, keeps the per-prompt baseline and formats the lira
# amounts): the same single source statusline.sh uses, no second copy of that logic here. Lines: 0 windows, 1 oturum, 2 last action.
$maliyetPy = "$PSScriptRoot/bin/maliyet.py"
$mal = (Test-Path $maliyetPy) -and $env:MALIYET_DURUM -ne '0' -and -not (Test-Path "$env:USERPROFILE/.claude/maliyet/kapali")
$malSatir = @()
if ($mal) {
    # python.exe as recorded by the installer (maliyet.cmd, under ~/.claude/bin in both install modes), never the WindowsApps stub
    try {
        $pyExe = $env:MALIYET_PYTHON
        if (-not $pyExe -or -not (Test-Path $pyExe)) {
            $pyExe = $null
            $cmdF = "$env:USERPROFILE/.claude/bin/maliyet.cmd"
            if ((Test-Path $cmdF) -and ((Get-Content $cmdF -TotalCount 1) -match '^@"([^"]+)"')) { $pyExe = $Matches[1] }
            if (-not $pyExe -or -not (Test-Path $pyExe)) {
                $pyExe = $null
                $pc = Get-Command python.exe -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike '*\WindowsApps\*' } | Select-Object -First 1
                if ($pc) { $pyExe = $pc.Source }
            }
        }
        if ($pyExe) {
            $OutputEncoding = New-Object Text.UTF8Encoding $false   # what the pipe to python carries (5.1 default is ASCII)
            $malSatir = @($raw | & $pyExe -S -E $maliyetPy --durum --girdi 2>$null)
        }
    } catch {}
}
$out += ' | ' + (Get-Date -Format 'HH:mm')
# KOTA_ISLEM_TUTARI=0 hides the last action's amounts after the clock (personal preference, default shown)
if ($env:KOTA_ISLEM_TUTARI -ne '0' -and $malSatir.Count -gt 2 -and $malSatir[2]) { $out += ' ' + $malSatir[2] }
# Quota pace: where the weekly limit lands at the forecast target (reset minus PACE_HEDEF_SAAT hours, default 5; 0 = the reset) -> BOL / NORMAL / SIKI (written to pace.json for the hook). The mode is weekly only; the 5-hour window is displayed, never part of it.
# bin/pace_forecast.py gives the median and a P10-P90 range from the active rate and the remaining work hours;
# used / share of the week elapsed is the fallback while there is no fresh forecast.
function Get-Forecast5h {
    # 5-hour forecast (same rule as statusline.sh): projected % at the reset, or when the limit runs out.
    # Rate: the oldest history row of this window that is 10-60 min old, else the window average.
    param([long]$now, [long]$r, [double]$u, [string]$hist)
    if ($r -le 0 -or ($r - $now) -le 0) { return '' }
    $t0 = [long]0; $u0 = [double]0; $found = $false
    if (Test-Path $hist) {
        foreach ($line in (Get-Content -Path $hist -Tail 400 -ErrorAction SilentlyContinue)) {
            $c = $line -split ','
            if ($c.Count -lt 5) { continue }
            try { $ts = [long]$c[0]; $rr = [long]$c[4]; $uu = [double]$c[2] } catch { continue }
            if ($rr -eq $r -and $ts -ge ($now - 3600) -and $ts -le ($now - 600)) {
                if (-not $found -or $ts -lt $t0) { $found = $true; $t0 = $ts; $u0 = $uu }
            }
        }
    }
    if ($found) { $rate = ($u - $u0) / ($now - $t0) }
    else { $span = [Math]::Max($now - ($r - 18000), 600); $rate = $u / $span }
    if ($rate -le 0) { return '' }
    $p = $u + $rate * ($r - $now)
    if ($p -ge 100) {
        $tb = [long][Math]::Floor($now + (100 - $u) / $rate)
        return ([string][char]0x2192) + 'biti' + [char]0x015F + ' ' + [DateTimeOffset]::FromUnixTimeSeconds($tb).ToLocalTime().ToString('HH:mm')
    }
    return ([string][char]0x2192) + '%' + [int][Math]::Floor($p)
}
$rl = $d.rate_limits
$now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
# Only the overall weekly limit drives the mode; a missing reset or one not within the coming 8 days (e.g. 4102444800 = year 2100) is no real reset: skip.
$r7ok = $rl -and $rl.seven_day -and $rl.seven_day.resets_at -and ([long]$rl.seven_day.resets_at -gt $now) -and ([long]$rl.seven_day.resets_at -le $now + 691200)
if ($r7ok) {
    $claude = "$env:USERPROFILE/.claude"
    $prev = $null; $fc = $null
    try { $prev = Get-Content "$claude/pace.json" -Raw -ErrorAction Stop | ConvertFrom-Json } catch {}
    try { $fc = Get-Content "$claude/pace-forecast.json" -Raw -ErrorAction Stop | ConvertFrom-Json } catch {}
    $r7 = [long]$rl.seven_day.resets_at
    $r5at = if ($rl.five_hour.resets_at -and [long]$rl.five_hour.resets_at -gt $now -and [long]$rl.five_hour.resets_at -le $now + 21600) { [long]$rl.five_hour.resets_at } else { 0 }
    # A session that has not called the API lately reports older, lower values: keep the highest value of the same window.
    # Except a reading at least as recent as the last one (same or newer 5-hour window, 5h value not lower): it is current,
    # so take its weekly value even when lower, since a manual limit reset keeps the same weekly resets_at.
    # $fresh needs the 5-hour window and only matters for the SAME weekly resets_at. A new week (different seven_day.resets_at) never
    # carries the old value over (see the week_resets_at -eq $r7 test below), so a weekly reset works without a 5-hour window too.
    $u7 = [double]$rl.seven_day.used_percentage
    $u5a = if ($null -ne $rl.five_hour -and $null -ne $rl.five_hour.used_percentage) { [double]$rl.five_hour.used_percentage } else { 0 }
    $fresh = $prev -and $r5at -gt 0 -and ($r5at -gt [long]$prev.five_hour_resets_at -or ($r5at -eq [long]$prev.five_hour_resets_at -and $u5a -ge [double]$prev.five_hour_used))
    if ($prev -and [long]$prev.week_resets_at -eq $r7 -and -not $fresh) { $u7 = [Math]::Max($u7, [double]$prev.week_used) }
    # No five_hour.used_percentage in the input = the 5-hour window is not reported (docs: each window may be absent): $u5 stays $null, never 0.
    $u5 = if ($null -ne $rl.five_hour -and $null -ne $rl.five_hour.used_percentage) { [double]$rl.five_hour.used_percentage } else { $null }
    $has5 = $null -ne $u5
    if ($has5 -and $prev -and $r5at -gt 0 -and [long]$prev.five_hour_resets_at -eq $r5at) { $u5 = [Math]::Max($u5, [double]$prev.five_hour_used) }
    $rnd = { param($x) [int][Math]::Floor([double]$x + 0.5) }
    $el = [Math]::Min([Math]::Max(1 - (($r7 - $now) / 604800), 0.05), 1)
    $proj = & $rnd ($u7 / $el)
    $mode = if ($proj -gt 105) { 'SIKI' } elseif ($proj -lt 80) { 'BOL' } else { 'NORMAL' }
    $pj = [ordered]@{ week_used = $u7; week_elapsed = [int][Math]::Floor($el * 100); five_hour_used = $u5; week_resets_at = $r7; five_hour_resets_at = $r5at; updated = $now }
    $range = ''; $runout = ''
    if ($fc -and $null -ne $fc.p50 -and [long]$fc.week_resets_at -eq $r7 -and ($now - [long]$fc.updated) -lt 3600) {
        $proj = & $rnd $fc.p50
        # ± half of the likely range (P10-P90)
        $half = & $rnd (([double]$fc.p90 - [double]$fc.p10) / 2)
        $range = if ($half -ge 1) { "±$half" } else { '' }
        $mode = [string]$fc.mode
        foreach ($k in 'p10', 'p50', 'p90', 'rate', 'active_hours_left', 'mode_reason', 'runout_at', 'runout_label', 'idle_label', 'target_at', 'target_hours', 'target_percent') { $pj[$k] = $fc.$k }
        # past 100%: when the limit runs out and how long it stays empty before the reset
        if ($fc.runout_label) { $runout = "bitiş $($fc.runout_label) ($($fc.idle_label) boş)" }
    }
    $pj.week_projected = $proj; $pj.mode = $mode
    $r5 = ''
    if ($r5at -gt 0) {
        $left = [Math]::Max($r5at - $now, 0)
        $h = [Math]::Floor($left / 3600); $m = [Math]::Floor(($left % 3600) / 60)
        $r5 = if ($h -gt 0) { " (${h}sa${m}dk)" } else { " (${m}dk)" }
    }
    $f5 = ''
    if ($has5 -and $r5at -gt 0) { $f5 = Get-Forecast5h -now $now -r $r5at -u $u5 -hist "$claude/pace-history.csv" }
    # Mode as one symbol: green BOL, yellow NORMAL, red SIKI
    $code = @{ BOL = 0x1F7E2; NORMAL = 0x1F7E1; SIKI = 0x1F534 }[$mode]
    $sym = if ($code) { [char]::ConvertFromUtf32($code) } else { $mode }
    # The 5sa segment is shown only while Claude Code reports a 5-hour window
    $seg5 = if ($has5) { "5sa %$([int][Math]::Floor($u5))$f5$r5 · " } else { '' }
    $out += " | ${seg5}hafta %$([int][Math]::Floor($u7))→$(if ($runout) { $runout } else { "%$proj$range" }) $sym"
    # History for the forecast: one row when a value changes, else every 10 minutes (no tokens)
    $lastLog = if ($prev -and $prev.logged_at) { [long]$prev.logged_at } else { 0 }
    try {
        $hist = "$claude/pace-history.csv"
        $changed = (-not $prev) -or ([double]$prev.week_used -ne $u7) -or ([string]$prev.five_hour_used -ne [string]$u5)
        if ($canli -and ($changed -or ($now - $lastLog) -ge 600)) {
            if (-not (Test-Path $hist)) { 'ts,week_used,five_hour_used,week_resets_at,five_hour_resets_at,host' | Set-Content -Path $hist -Encoding utf8 }
            "$now,$u7,$(if ($has5) { $u5 }),$r7,$(if ($r5at -gt 0) { $r5at }),win" | Add-Content -Path $hist -Encoding utf8
            $lastLog = $now
        }
    } catch {}
    $pj.logged_at = $lastLog
    if ($canli) { try { $pj | ConvertTo-Json -Compress | Set-Content -Path "$claude/pace.json" -Encoding utf8 } catch {} }
    # Refresh the forecast in the background at Idle priority when the weekly value moved or it is 10 minutes old
    $fcPy = "$PSScriptRoot/bin/pace_forecast.py"
    if ($canli -and (Test-Path $fcPy) -and (-not $fc -or ($now - [long]$fc.updated) -ge 600 -or [double]$fc.week_used -ne $u7)) {
        try {
            $lock = Get-Item "$claude/pace-forecast.lock" -ErrorAction SilentlyContinue
            if (-not $lock -or ((Get-Date) - $lock.LastWriteTime).TotalSeconds -ge 120) {
                # Single hidden-launch path: tools/gizli.ps1 (see the share block below); without the helper the refresh is skipped.
                if (-not $gz) { $gz = @("$PSScriptRoot/../../tools/gizli.ps1", "$PSScriptRoot/bin/gizli.ps1") | Where-Object { Test-Path $_ } | Select-Object -First 1 }
                if ($gz) {
                    . $gz
                    $pyw = (Get-Command pythonw -ErrorAction SilentlyContinue | Select-Object -First 1).Source
                    if ($pyw) { $p = Start-GizliSurec -Exe $pyw -Argument "`"$fcPy`"" -PassThru; if ($p) { $p.PriorityClass = 'Idle' } }
                }
            }
        } catch {}
    }
    # Team usage ranking: the throttled automatic share (maliyet --paylas --otomatik), same rule as statusline.sh. Gate = one file time:
    # the marker ~/.claude/maliyet/paylas-sonraki, whose LastWriteTime is the next due time (the job re-aims it after every check from
    # paylas_aralik_saat in pace-plan.json, default 2 h). Only a joined machine (~/.config/claude-shared/plan) and only when due do we
    # look at the lock (a share younger than 30 min is still running). Started hidden and never waited for.
    if ($canli -and $mal -and $pyExe -and $env:KOTA_PAYLAS_OTO -ne '0' -and (Test-Path "$PSScriptRoot/bin/maliyet_paylas.py")) {
        try {
            $due = Get-Item "$claude/maliyet/paylas-sonraki" -ErrorAction SilentlyContinue
            if ((-not $due -or $due.LastWriteTimeUtc -le [DateTime]::UtcNow) -and (Test-Path "$env:USERPROFILE/.config/claude-shared/plan")) {
                $pl = Get-Item "$claude/maliyet/paylas.kilit" -ErrorAction SilentlyContinue
                if (-not $pl -or ([DateTime]::UtcNow - $pl.LastWriteTimeUtc).TotalSeconds -ge 1800) {
                    # tools/gizli.ps1 (headless conhost): Start-Process python.exe -WindowStyle Hidden still creates a console window each time.
                    # Repo layout: ../../tools/gizli.ps1; a --kopya install ships it as bin/gizli.ps1.
                    if (-not $gz) { $gz = @("$PSScriptRoot/../../tools/gizli.ps1", "$PSScriptRoot/bin/gizli.ps1") | Where-Object { Test-Path $_ } | Select-Object -First 1 }
                    if ($gz) { . $gz; Start-GizliSurec -Exe $pyExe -Argument "-E `"$PSScriptRoot/bin/maliyet.py`" --paylas --otomatik" }
                }
            }
        } catch {}
    }
}
# Slack inbox (tools/slack-triage/gelen.py, kur --slack-gelen): " | Slack: N yeni" while unread items wait. Reads the inbox file and its
# .okundu byte offset directly (no Python spawn, no network, at most 256 KB); a missing file or any error shows nothing.
try {
    $sgIn = if ($env:SLACK_GELEN_INBOX) { $env:SLACK_GELEN_INBOX } else { "$env:USERPROFILE/.claude/slack-gelen.jsonl" }
    $sgLen = if (Test-Path -LiteralPath $sgIn) { (New-Object IO.FileInfo $sgIn).Length } else { 0 }
    if ($sgLen -gt 0) {
        $sgOff = [long]0
        if (Test-Path -LiteralPath "$sgIn.okundu") { [void][long]::TryParse(([IO.File]::ReadAllText("$sgIn.okundu")).Trim(), [ref]$sgOff) }
        if ($sgLen -gt $sgOff) {
            $fs = [IO.File]::Open($sgIn, 'Open', 'Read', 'ReadWrite')
            try {
                [void]$fs.Seek($sgOff, 'Begin')
                $buf = New-Object byte[] ([int][Math]::Min($sgLen - $sgOff, 262144))
                $got = $fs.Read($buf, 0, $buf.Length)
            } finally { $fs.Dispose() }
            $sgN = @([Text.Encoding]::UTF8.GetString($buf, 0, $got) -split "`n" | Where-Object { $_.Trim() }).Count
            if ($sgN -gt 0) { $out += " | Slack: $sgN yeni" }
        }
    }
} catch {}
# Burn mode (son-pencere bitir --ac): " | BİTİR" while it is on and its weekly reset has not passed. One small file read, no Python.
try {
    $bf = "$env:USERPROFILE/.claude/son-pencere/bitir.json"
    if ([IO.File]::Exists($bf)) {
        $bt = [IO.File]::ReadAllText($bf) | ConvertFrom-Json
        if ($bt.aktif -eq $true -and [long]$bt.hafta_reset -gt $now) { $out += ' | B' + [char]0x0130 + 'T' + [char]0x0130 + 'R' }
    }
} catch {}
$branch = git -C $dir branch --show-current 2>$null
# Folder: ~ for the home folder, shortened to ~/... below it (case kept)
$userHome = $env:USERPROFILE
$dirFwd = ($dir -replace '\\', '/') -replace '/+$', ''
$homeFwd = ($userHome -replace '\\', '/') -replace '/+$', ''

if ($dirFwd -ieq $homeFwd) {
    # Current dir is the home folder
    if ($branch) { $out += " | [$branch] ~" } else { $out += " | ~" }
} elseif ($dirFwd.StartsWith($homeFwd + '/', [StringComparison]::OrdinalIgnoreCase)) {
    # Current dir is under home folder
    $displayDir = '~' + $dirFwd.Substring($homeFwd.Length)
    if ($branch) {
        $out += " | [$branch] $displayDir"
    } else {
        $out += " | $displayDir"
    }
} else {
    # Other directory
    if ($branch) {
        $out += " | [$branch] $dir"
    } else {
        $out += " | $dir"
    }
}
# Cost segments (same as statusline.sh): cache hit rate, oturum, rolling windows.
if ($mal) {
    # prompt cache hit rate of the main conversation (status-line JSON prompt_cache, Claude Code >= 2.1.251): "önb %NN", misses in brackets
    if ($null -ne $d.prompt_cache -and $null -ne $d.prompt_cache.hit_ratio) {
        $out += " | önb %" + [int][Math]::Floor([double]$d.prompt_cache.hit_ratio * 100 + 0.5)
        $miss = if ($null -ne $d.prompt_cache.misses) { [int]$d.prompt_cache.misses } else { 0 }
        if ($miss -gt 0) { $out += " ($miss kaçış)" }
    }
    # "oturum ₺X": cost.total_cost_usd from the status-line JSON (resets to 0 on /clear)
    if ($malSatir.Count -gt 1 -and $malSatir[1]) { $out += ' | oturum ' + $malSatir[1] }
    # "son 24sa ₺A (₺a) · 7g ₺B $C (₺b) · 30g ₺D (₺d) · ×N": rolling windows (reads durum.cache only; starts the background refresh when it is old)
    if ($malSatir.Count -gt 0 -and $malSatir[0]) { $out += ' | ' + $malSatir[0] }
}
$out

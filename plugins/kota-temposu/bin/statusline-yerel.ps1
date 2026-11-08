# Durum çubuğu sarmalayıcısı (kota-temposu): Python yolunu düzeltir, sonra statusline.ps1'i çalıştırır.
# Neden: pace_forecast.py arka planda `pythonw` ile başlar; pythonw PATH'te yoksa tahmin sessizce güncellenmez.
# statusline.ps1 AYNI süreçte çalışır (ikinci bir pwsh başlatılmaz: pwsh açılışı bu satırın en pahalı kısmıdır).
# Kişiye özel uyarılar için isteğe bağlı %USERPROFILE%\.claude\bin\statusline-ek.ps1 yazın: param([string]$Satir), değiştirilmiş satırı döndürür;
# kur.ps1 o dosyaya dokunmaz.
# settings.json statusLine komutu bu betiği çağırır:
#   pwsh -NoProfile -File C:\Users\KULLANICI\.claude\bin\statusline-yerel.ps1
# Yollar parametredir. statusline.ps1 bu betiğin bir üst klasöründedir (kopya kurulumda ~/.claude, depodan çalışmada plugins/kota-temposu);
# ClaudeDir (settings.json, kişisel statusline-ek.ps1) her iki durumda %USERPROFILE%\.claude'dur.
# Satır sarma: tüm bölümler terminal genişliğine sığıyorsa TEK satır (eskisiyle aynı bayt), sığmıyorsa önce bölüm sınırlarında (" | "), sığmayan bölümü " · " kısımlarında bölerek açgözlü ikinci
# (gerekirse üçüncü) satıra geçer; hiçbir kısım kesilmez/bölünmez. Genişlik: KOTA_SATIR_GENISLIK (elle) > COLUMNS (Claude Code durum çubuğu
# sürecine verir; Windows'ta [Console]::WindowWidth ve `mode con` yönlendirilmiş stdio'da yanlış/hatalı) > 120. Kenar payı: KOTA_SATIR_PAY (varsayılan 4 = ölçülen
# Claude Code payı, + settings.json statusLine.padding x 2). Sarma statusline-ek.ps1 çıktısından SONRA yapılır (kişisel uyarılar da hesaba girer).
param(
    [string]$ClaudeDir = (Join-Path $env:USERPROFILE '.claude'),
    [string]$StatusLine = (Join-Path (Split-Path $PSScriptRoot -Parent) 'statusline.ps1'),
    [string]$Ek = (Join-Path $ClaudeDir 'bin/statusline-ek.ps1')
)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# %LOCALAPPDATA%\Programs\Python\Python3*\ içindeki pythonw'ı PATH'e ekle (en yeni sürüm önce)
if (-not (Get-Command pythonw -ErrorAction SilentlyContinue)) {
    $py = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        Where-Object { Test-Path (Join-Path $_.FullName 'pythonw.exe') } |
        Select-Object -First 1
    if ($py) { $env:PATH = $py.FullName + ';' + $env:PATH }
}

# stdin'i bu süreç okumaz: statusline.ps1 kendisi okur
# Doğrudan depodan çalışırken hiçbir hata durum çubuğunda görünmemeli: betik hata verir ya da boş dönerse en az 'Claude' yazılır
try { $satir = (& $StatusLine) -join '' } catch { $satir = '' }
if (-not $satir) { $satir = 'Claude' }
if (Test-Path $Ek) {
    try { $satir = [string](& $Ek -Satir $satir) } catch {}
}

# Ekranda kapladığı hücre sayısı: ANSI kaçışları sayılmaz, geniş karakter/emoji 2, sıfır genişlikli 0
function Get-HucreGenislik([string]$Metin) {
    $m = $Metin -replace '\x1b\[[0-9;?]*[ -/]*[@-~]', '' -replace '\x1b\][^\x07\x1b]*(\x07|\x1b\\)', ''
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

# Genişlik: elle ayar > COLUMNS > 120
$genislik = 0
foreach ($ad in 'KOTA_SATIR_GENISLIK', 'COLUMNS') {
    $v = [Environment]::GetEnvironmentVariable($ad)
    if ("$v".Trim() -match '^\d{1,4}$' -and [int]$v.Trim() -ge 20) { $genislik = [int]$v.Trim(); break }
}
if ($genislik -eq 0) { $genislik = 120 }
# Kenar payı, Claude Code 2.1.310'da gerçek ekranda ölçüldü (tests/test_gercek_ekran.py; hem tam ekran hem klasik görünüm, 80-250 sütun): bir durum
# satırı en çok (sütun - 4) hücre gösterilir (solda 2 girinti, sağda 2 boşluk); fazlası kesilir ve "…" ile biter, SARILMAZ. settings.json'daki
# statusLine.padding her iki yana eklenir: pay = 4 + 2 x padding. KOTA_SATIR_PAY verilirse o geçerlidir.
$pay = 4
if ("$env:KOTA_SATIR_PAY".Trim() -match '^\d{1,3}$') { $pay = [int]$env:KOTA_SATIR_PAY.Trim() }
else {
    try {
        $ham = Get-Content (Join-Path $ClaudeDir 'settings.json') -Raw -ErrorAction Stop
        if ($ham -match '"padding"') {
            $pd = ($ham | ConvertFrom-Json).statusLine.padding
            if ($pd -is [ValueType] -and [int]$pd -gt 0 -and [int]$pd -le 50) { $pay += 2 * [int]$pd }
        }
    } catch {}
}
$sinir = [Math]::Max($genislik - $pay, 20)

# Her satırı (ek betik zaten satır sonu koyduysa her birini) parça parça açgözlü sar: parçalar " | " bölümleri ve bölüm içinde " · " kısımlarıdır;
# sığan parça kendi ayracıyla satıra eklenir, sığmayan yeni satırı başlatır (satır ayraçla başlamaz, parça bölünmez)
$sonuc = foreach ($ham in ($satir -split "`r?`n")) {
    if ((Get-HucreGenislik $ham) -le $sinir) { $ham; continue }
    $cur = $null
    foreach ($seg in ($ham -split ' \| ')) {
        $i = 0
        foreach ($p in ($seg -split ' · ')) {
            $ayrac = if ($i -eq 0) { ' | ' } else { ' · ' }
            $i++
            if ($null -eq $cur) { $cur = $p }
            elseif ((Get-HucreGenislik "$cur$ayrac$p") -le $sinir) { $cur = "$cur$ayrac$p" }
            else { $cur; $cur = $p }
        }
    }
    $cur
}
$sonuc -join "`n"

<#
 The ONE way claude-shared starts things hidden on Windows (PowerShell side; the Python side is tools/gizli.py).
 Dot-source it:  . "$PSScriptRoot\gizli.ps1"   (kur_ortak.py hands the path over as $env:KUR_GIZLI_PS1)

 Why `conhost.exe --headless <exe> <args>`: a console child of a console-less process (git, gh, claude, bash) gets a NEW console, and with
 Windows Terminal as the default terminal that is a visible window per child (measured 05.11.2026: maliyet-gunluk opened one every ~2.5 s).
 `powershell.exe -WindowStyle Hidden` and `Start-Process python.exe -WindowStyle Hidden` still create a console window each
 (measured 05.11.2026 with a WinEvent hook: one ConsoleWindowClass window per `Start-Process python.exe -WindowStyle Hidden`; none with a
 headless conhost). A headless conhost gives the whole process tree ONE hidden console and creates no window itself.
 Logon type stays Interactive (no S4U: DPAPI/Credential Manager and the gh/Vault/Slack tokens of the user's profile keep working).
 Cost: conhost does not pass the exit code on (Task Scheduler shows 0); the tasks' own logs carry failures.
 Without conhost.exe (pre-1809 Windows) the fallback is pythonw.exe for python.exe, the plain executable otherwise.
#>

function Get-GizliKomut {
    # The single place that decides how a hidden launch looks: returns Exe/Argument to hand to a task action or Start-Process.
    param([Parameter(Mandatory)][string]$Exe, [string]$Argument = '')
    $conhost = Join-Path $env:SystemRoot 'System32\conhost.exe'
    if (Test-Path $conhost) {
        return @{ Exe = $conhost; Argument = (('--headless "' + $Exe + '" ' + $Argument).TrimEnd()) }
    }
    if ($Exe -match '(?i)python\.exe$') {
        $pw = Join-Path (Split-Path $Exe) 'pythonw.exe'
        if (Test-Path $pw) { $Exe = $pw }
    }
    return @{ Exe = $Exe; Argument = $Argument }
}

function New-GizliEylem {
    # Task action that runs `-Exe -Argument` without any window.
    param([Parameter(Mandatory)][string]$Exe, [string]$Argument = '')
    $k = Get-GizliKomut -Exe $Exe -Argument $Argument
    return New-ScheduledTaskAction -Execute $k.Exe -Argument $k.Argument
}

function Register-GizliGorev {
    # Registers (or replaces) a per-user, no-admin, hidden task. Every installer calls this instead of Register-ScheduledTask.
    param(
        [Parameter(Mandatory)][string]$Ad,
        [Parameter(Mandatory)][string]$Exe,
        [string]$Argument = '',
        [Parameter(Mandatory)]$Trigger,
        [Parameter(Mandatory)]$Settings,
        [string]$Aciklama = ''
    )
    $a = New-GizliEylem -Exe $Exe -Argument $Argument
    $p = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $Ad -Action $a -Trigger $Trigger -Settings $Settings -Principal $p -Force -Description $Aciklama | Out-Null
}

function Start-GizliSurec {
    # Starts `-Exe -Argument` detached without any window and returns at once (a background job started from a script/status line).
    # -PassThru returns the started process (the conhost host when headless) instead of nothing.
    param([Parameter(Mandatory)][string]$Exe, [string]$Argument = '', [switch]$PassThru)
    $k = Get-GizliKomut -Exe $Exe -Argument $Argument
    $p = Start-Process -FilePath $k.Exe -ArgumentList $k.Argument -WindowStyle Hidden -PassThru
    if ($PassThru) { return $p }
}

# =============================================================================
# verify-native-install.ps1 -- run the documented Windows install on THIS PC
# and write down what happened, in a folder you can send back. The Windows twin
# of scripts/verify-native-install.sh. Windows 10 / 11, Windows PowerShell 5.1
# or PowerShell 7. ASCII only, so Windows PowerShell 5.1 reads it correctly
# without a byte-order mark.
#
# Usage, in PowerShell from supracloud-jarvis\, after INSTALL.md's Windows
# steps 1 and 2 (the five programs, and pgvector):
#   powershell -ExecutionPolicy Bypass -File scripts\verify-native-install.ps1
#       report in %USERPROFILE%\ira-install-check-<time>\
#   powershell -ExecutionPolicy Bypass -File scripts\verify-native-install.ps1 -Out C:\ira-check
#   powershell -ExecutionPolicy Bypass -File scripts\verify-native-install.ps1 -PrintListeners
#       only list IRA's listening ports
#
# Steps, each recorded PASS / FAIL with its log:
#   machine   Windows version, CPU, CPU count, memory, graphics (no PC name,
#             no user name); program versions in machine.log
#   setup     powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
#             (safe to re-run: keeps .env and data)
#   start     powershell -ExecutionPolicy Bypass -File start-ira.ps1
#   loopback  IRA listens on 127.0.0.1 (or ::1) only
#   smoke     .venv\Scripts\python.exe scripts\smoke_test.py: sign-in, real
#             chat, memory, recall, approvals
#   stop      powershell -ExecutionPolicy Bypass -File stop-ira.ps1, then
#             nothing left listening
#   uninstall powershell -ExecutionPolicy Bypass -File scripts\uninstall-windows.ps1
#             as a DRY RUN only (no -Yes): nothing is removed
#
# Each step runs exactly as INSTALL.md gives it, under Windows PowerShell
# (powershell.exe), whichever PowerShell runs this script. If IRA is already
# running, this script stops before it starts: stop IRA first with stop-ira.ps1.
#
# Setup asks for the PostgreSQL superuser's password. Its prompt would be lost
# in setup.log, so this script asks once, at the start, and hands the password
# to setup alone, through PGPASSWORD, as setup-windows.ps1 allows. It is never
# written to disk or put on a command line, and it is gone before IRA starts.
#
# The report holds no password and no .env. This script never opens .env, and
# it cleans every log before writing it: any "PASSWORD=", "SECRET", "TOKEN" or
# "KEY" value is replaced, as verify-native-install.sh does, and so is a
# password inside a URL. Your home folder is shown as "~" and the PC's name as
# "<this-pc>". Each step's output is first caught in a private temporary
# folder, which is deleted at the end.
# =============================================================================
param(
    [string]$Out = "",
    [switch]$PrintListeners
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root    = Split-Path -Parent $PSScriptRoot
$Utf8    = New-Object System.Text.UTF8Encoding $false
$HomeDir = [Environment]::GetFolderPath("UserProfile")
if (-not $HomeDir) { $HomeDir = $HOME }
$PcName  = $env:COMPUTERNAME

# IRA's ports, and how long to let them settle after start and stop. Both
# variables exist only so this script's own tests can choose free ports and run fast.
$Ports = @(3000, 8000)
if ($env:IRA_VERIFY_PORTS) {
    $Ports = @($env:IRA_VERIFY_PORTS -split '[\s,]+' | Where-Object { $_ } | ForEach-Object { [int]$_ })
}
$SettleScale = 1
if ($env:IRA_VERIFY_SETTLE_SEC -eq "0") { $SettleScale = 0 }

# The same cleaning as verify-native-install.sh: strip colours, show the home
# folder as "~", blank every PASSWORD/SECRET/TOKEN/KEY value. Also blank a
# password inside a URL, and hide the PC's name.
function Protect-LogText([string]$text) {
    if (-not $text) { return "" }
    $t = $text -replace "\x1b\[[0-9;?]*[A-Za-z]", ''
    if ($HomeDir) {
        foreach ($h in @($HomeDir, ($HomeDir -replace '\\', '/'))) {
            $t = $t -replace [regex]::Escape($h), '~'
        }
    }
    if ($PcName) { $t = $t -replace ('\b' + [regex]::Escape($PcName) + '\b'), '<this-pc>' }
    $t = $t -creplace '((PASSWORD|SECRET|TOKEN|KEY)[A-Z_]*=)\S+', '$1<redacted>'
    $t = $t -replace '(://[^\s:/@]+:)[^\s@]+@', '$1<redacted>@'
    return $t
}

# Quote arguments the way Windows programs split them (CommandLineToArgvW).
# Start-Process joins a list of arguments with spaces and quotes none of them,
# so a report folder under "C:\Users\First Last" would split in two.
function ConvertTo-ArgumentLine([string[]]$arguments) {
    $parts = @()
    foreach ($a in $arguments) {
        if ($a -eq "") { $parts += '""' }
        elseif ($a -notmatch '[\s"]') { $parts += $a }
        else {
            $s = $a -replace '(\\*)"', '$1$1\"'
            $s = $s -replace '(\\+)$', '$1$1'
            $parts += '"' + $s + '"'
        }
    }
    return ($parts -join ' ')
}

# Read a file another program may still hold open.
function Read-Shared([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return "" }
    $share = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
    $fs = [System.IO.FileStream]::new($path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, $share)
    try {
        $ms = New-Object System.IO.MemoryStream
        $fs.CopyTo($ms)
        $bytes = $ms.ToArray()
    } finally { $fs.Dispose() }
    # Most programs here write UTF-8; Windows PowerShell writes the console's code page.
    try { $text = (New-Object System.Text.UTF8Encoding $false, $true).GetString($bytes) }
    catch { $text = [Console]::OutputEncoding.GetString($bytes) }
    return $text.TrimStart([char]0xFEFF)
}

function Format-Endpoint([string]$address, [int]$port) {
    if ($address.Contains(":")) { return "[$address]:$port" }
    return "${address}:$port"
}

function Test-Loopback([string]$endpoint) {
    return ($endpoint -match '^(127\.0\.0\.1|\[::1\]):\d+$')
}

# IRA's listening sockets as "address:port", sorted. $null means they could not
# be listed at all, which is not the same as none listening: the Linux script
# once read "could not look" as "nothing is listening".
function Get-IraListeners {
    if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) { return $null }
    try { $rows = @(Get-NetTCPConnection -State Listen -ErrorAction Stop) }
    catch {
        if ($_.CategoryInfo.Category -ne "ObjectNotFound") { return $null }
        $rows = @()
    }
    $found = @()
    foreach ($r in $rows) {
        if ($Ports -contains [int]$r.LocalPort) { $found += (Format-Endpoint ([string]$r.LocalAddress) ([int]$r.LocalPort)) }
    }
    return ,@($found | Sort-Object -Unique)
}

# A program's first lines of output, such as its version. Never fails.
function Get-ToolLine([string]$program, [string[]]$arguments) {
    $ErrorActionPreference = "Continue"
    try {
        if (-not (Get-Command $program -ErrorAction SilentlyContinue)) { return "not found" }
        $lines = @(& $program @arguments 2>&1 | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
        if ($lines.Count -eq 0) { return "no output (exit $LASTEXITCODE)" }
        return (@($lines | Select-Object -First 3) -join " / ")
    } catch { return "could not run: $($_.Exception.Message)" }
}

function Get-Cim([string]$class) {
    if (-not (Get-Command Get-CimInstance -ErrorAction SilentlyContinue)) { return @() }
    try { return @(Get-CimInstance -ClassName $class -ErrorAction Stop) } catch { return @() }
}

if ($env:OS -ne "Windows_NT") {
    Write-Host "verify-native-install.ps1 is for Windows. On Linux and macOS, run: bash scripts/verify-native-install.sh"
    exit 2
}

if ($PrintListeners) {
    $l = Get-IraListeners
    if ($null -eq $l) { Write-Host "cannot list listening sockets here (Get-NetTCPConnection is missing or failed)"; exit 3 }
    foreach ($e in $l) { Write-Output $e }
    exit 0
}

# A running IRA would hold its files (setup could not replace them) and answer
# the checks with the old code.
$before = Get-IraListeners
if ($null -ne $before -and $before.Count -gt 0) {
    Write-Host ("Something already listens on IRA's ports: " + ($before -join " ") + ".") -ForegroundColor Red
    Write-Host "Stop IRA first:  powershell -ExecutionPolicy Bypass -File stop-ira.ps1   Nothing was run." -ForegroundColor Red
    exit 1
}

if (-not $Out) {
    $Out = Join-Path $HomeDir ("ira-install-check-" + (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ"))
}
$Out = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Out)
try { New-Item -ItemType Directory -Force -Path $Out | Out-Null }
catch { Write-Host "cannot create $Out" -ForegroundColor Red; exit 1 }
$RawDir = Join-Path ([System.IO.Path]::GetTempPath()) ("ira-install-check-raw-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $RawDir | Out-Null
$Summary = Join-Path $Out "SUMMARY.txt"
[System.IO.File]::WriteAllText($Summary, "", $Utf8)
$script:Fails = 0
$script:RC = 0
$script:SECS = 0

function Write-SummaryLine([string]$line) {
    Write-Host $line
    [System.IO.File]::AppendAllText($Summary, $line + [Environment]::NewLine, $Utf8)
}

function Write-Record([string]$step, [string]$result, [string]$detail) {
    Write-SummaryLine ("{0,-9} {1,-4} {2}" -f $step, $result, (Protect-LogText $detail))
    if ($result -ne "PASS") { $script:Fails++ }
}

# Invoke-Step <log> <program> <arguments>: run it from this folder, wait for it
# (not for the servers it leaves running), and write its cleaned output to the
# report. Sets $script:RC and $script:SECS.
function Invoke-Step([string]$logName, [string]$program, [string[]]$arguments) {
    $raw = Join-Path $RawDir $logName
    $argLine = ConvertTo-ArgumentLine $arguments
    Write-Host "  running: $program $argLine" -ForegroundColor DarkGray
    $t0 = Get-Date
    try {
        $p = Start-Process -FilePath $program -ArgumentList $argLine -WorkingDirectory $Root -NoNewWindow -PassThru `
            -RedirectStandardOutput "$raw.out" -RedirectStandardError "$raw.err"
        $null = $p.Handle   # without this, Windows PowerShell 5.1 can lose the exit code
        $p.WaitForExit()
        $script:RC = $p.ExitCode
        if ($null -eq $script:RC) { $script:RC = -1 }
    } catch {
        [System.IO.File]::WriteAllText("$raw.err", "could not run ${program}: $($_.Exception.Message)", $Utf8)
        $script:RC = 127
    }
    $script:SECS = [int]((Get-Date) - $t0).TotalSeconds
    $text = "command: $program $argLine" + [Environment]::NewLine + (Read-Shared "$raw.out")
    $err = Read-Shared "$raw.err"
    if ($err.Trim()) { $text += [Environment]::NewLine + "--- standard error ---" + [Environment]::NewLine + $err }
    $text += [Environment]::NewLine + "exit code: $($script:RC)" + [Environment]::NewLine
    [System.IO.File]::WriteAllText((Join-Path $Out $logName), (Protect-LogText $text), $Utf8)
}

function Restore-Env([string]$name, $value) {
    if ($null -eq $value) { Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue }
    else { Set-Item -LiteralPath "Env:$name" -Value $value }
}

$prevPgPassword = [Environment]::GetEnvironmentVariable("PGPASSWORD", "Process")
try {
    $commit = "unknown"
    $c = Get-ToolLine "git" @("-C", $Root, "rev-parse", "--short", "HEAD")
    if ($c -match '^[0-9a-f]{7,40}$') { $commit = $c }
    $stepShell = Get-ToolLine "powershell" @("-NoProfile", "-Command", '$PSVersionTable.PSVersion.ToString()')
    Write-SummaryLine "IRA install check -- commit $commit"
    Write-SummaryLine ("Windows script (scripts\verify-native-install.ps1), run by PowerShell " +
        "$($PSVersionTable.PSVersion) $($PSVersionTable.PSEdition); steps run by powershell.exe $stepShell")

    # Setup's password, asked for now so the rest can run unattended.
    $pgPassword = $prevPgPassword
    while (-not $pgPassword) {
        try { $sec = Read-Host "Password for the PostgreSQL superuser 'postgres' (setup needs it; if yours has none, type anything)" -AsSecureString }
        catch { throw "cannot ask for the PostgreSQL password here ($($_.Exception.Message)). Run this in a PowerShell window, or set PGPASSWORD first." }
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
        try { $pgPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    }

    # machine
    $os  = @(Get-Cim "Win32_OperatingSystem") | Select-Object -First 1
    $cpu = @(Get-Cim "Win32_Processor") | Select-Object -First 1
    $cs  = @(Get-Cim "Win32_ComputerSystem") | Select-Object -First 1
    $gpu = @(Get-Cim "Win32_VideoController" | ForEach-Object { ([string]$_.Name).Trim() } | Where-Object { $_ })
    $osText = "Windows (version unknown)"
    if ($os) { $osText = ("{0} {1}, {2}" -f ([string]$os.Caption).Trim(), $os.Version, $os.OSArchitecture) }
    $cpuText = "unknown CPU"
    if ($cpu) { $cpuText = ([string]$cpu.Name).Trim() }
    $memText = "? GiB"
    if ($cs -and $cs.TotalPhysicalMemory) { $memText = "{0} GiB" -f [math]::Floor([double]$cs.TotalPhysicalMemory / 1073741824) }
    $gpuText = "none found"
    if ($gpu.Count -gt 0) { $gpuText = $gpu -join ", " }
    Write-Record "machine" "PASS" ("$osText, $env:PROCESSOR_ARCHITECTURE, $cpuText, " +
        "$([Environment]::ProcessorCount) CPUs, $memText, graphics: $gpuText")
    $tools = @(
        "PowerShell running this script: $($PSVersionTable.PSVersion) $($PSVersionTable.PSEdition)",
        "powershell.exe (runs each step): $stepShell",
        ("python on PATH: " + (Get-ToolLine "python" @("--version"))),
        ("node: " + (Get-ToolLine "node" @("--version"))),
        ("npm: " + (Get-ToolLine "npm" @("--version"))),
        ("psql: " + (Get-ToolLine "psql" @("--version"))),
        ("ollama: " + (Get-ToolLine "ollama" @("--version"))),
        ("git: " + (Get-ToolLine "git" @("--version")))
    )
    if (Get-Command Get-Service -ErrorAction SilentlyContinue) {
        foreach ($svc in @(Get-Service -Name "postgresql*", "Memurai*" -ErrorAction SilentlyContinue)) {
            $tools += "service $($svc.Name): $($svc.Status)"
        }
    }
    [System.IO.File]::WriteAllText((Join-Path $Out "machine.log"),
        (Protect-LogText (($tools -join [Environment]::NewLine) + [Environment]::NewLine)), $Utf8)

    # setup, the only step that gets the PostgreSQL password
    Set-Item -LiteralPath "Env:PGPASSWORD" -Value $pgPassword
    try { Invoke-Step "setup.log" "powershell" @("-ExecutionPolicy", "Bypass", "-File", "scripts\setup-windows.ps1") }
    finally { Restore-Env "PGPASSWORD" $prevPgPassword; $pgPassword = $null }
    if ($script:RC -eq 0) { Write-Record "setup" "PASS" "exit 0 in $($script:SECS) s" }
    else { Write-Record "setup" "FAIL" "exit $($script:RC) in $($script:SECS) s (see setup.log)" }

    # start, exactly as documented
    Invoke-Step "start.log" "powershell" @("-ExecutionPolicy", "Bypass", "-File", "start-ira.ps1")
    if ($script:RC -eq 0) { Write-Record "start" "PASS" "start-ira.ps1 exit 0 in $($script:SECS) s" }
    else { Write-Record "start" "FAIL" "start-ira.ps1 exit $($script:RC) in $($script:SECS) s (see start.log)" }

    # loopback only
    Start-Sleep -Seconds (3 * $SettleScale)
    $found = Get-IraListeners
    if ($null -eq $found) {
        Write-Record "loopback" "FAIL" "cannot list listening sockets here (Get-NetTCPConnection is missing or failed), so this is unchecked"
    } elseif ($found.Count -eq 0) {
        Write-Record "loopback" "FAIL" ("nothing is listening on " + ($Ports -join " or "))
    } elseif (@($found | Where-Object { -not (Test-Loopback $_) }).Count -gt 0) {
        Write-Record "loopback" "FAIL" ("listening beyond this machine: " + ($found -join " "))
    } else {
        Write-Record "loopback" "PASS" ($found -join " ")
    }

    # smoke test, on the interpreter setup installed into
    $py = Join-Path $Root ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $py)) { $py = "python" }
    # Python writes a redirected stdout in the ANSI code page, and a reply with a
    # character outside it would stop the test. In a console it writes Unicode.
    $prevIo = [Environment]::GetEnvironmentVariable("PYTHONIOENCODING", "Process")
    Set-Item -LiteralPath "Env:PYTHONIOENCODING" -Value "utf-8"
    try { Invoke-Step "smoke.log" $py @("scripts\smoke_test.py", "--out", (Join-Path $Out "smoke-test.json")) }
    finally { Restore-Env "PYTHONIOENCODING" $prevIo }
    $verdict = @((Read-Shared (Join-Path $Out "smoke.log")) -split "\r?\n" | Where-Object { $_ -match '^(PASS|FAIL):' }) |
        Select-Object -Last 1
    if ($script:RC -eq 0) { Write-Record "smoke" "PASS" "$verdict" }
    else {
        if (-not $verdict) { $verdict = "no verdict" }
        Write-Record "smoke" "FAIL" "$verdict (see smoke.log)"
    }

    # stop, exactly as documented
    Invoke-Step "stop.log" "powershell" @("-ExecutionPolicy", "Bypass", "-File", "stop-ira.ps1")
    $stopRc = $script:RC
    if ($stopRc -ne 0) { Write-Record "stop" "FAIL" "stop-ira.ps1 exit $stopRc (see stop.log)" }
    Start-Sleep -Seconds (2 * $SettleScale)
    $left = Get-IraListeners
    if ($null -eq $left) {
        Write-Record "stop" "FAIL" "cannot list listening sockets here, so 'nothing left listening' is unchecked"
    } elseif ($left.Count -gt 0) {
        Write-Record "stop" "FAIL" ("still listening after stop: " + ($left -join " "))
    } elseif ($stopRc -eq 0) {
        Write-Record "stop" "PASS" "stop-ira.ps1 exit 0; nothing left listening"
    }

    # uninstall, dry run only: no -Yes, so it changes nothing
    if (Test-Path -LiteralPath (Join-Path $Root "scripts\uninstall-windows.ps1")) {
        Invoke-Step "uninstall-dry-run.log" "powershell" @("-ExecutionPolicy", "Bypass", "-File", "scripts\uninstall-windows.ps1")
        $said = (Read-Shared (Join-Path $Out "uninstall-dry-run.log")) -match 'Dry run: nothing was changed'
        if ($script:RC -eq 0 -and $said) { Write-Record "uninstall" "PASS" "dry run listed its plan; nothing removed" }
        elseif ($script:RC -eq 0) { Write-Record "uninstall" "FAIL" "exit 0 but no 'Dry run: nothing was changed' line (see uninstall-dry-run.log)" }
        else { Write-Record "uninstall" "FAIL" "dry run exit $($script:RC) (see uninstall-dry-run.log)" }
    }

    Write-SummaryLine ""
    if ($script:Fails -eq 0) { Write-SummaryLine "ALL PASS" } else { Write-SummaryLine "$($script:Fails) step(s) FAILED" }
    Write-Host "Report: $Out  (no passwords; send the whole folder)"
} finally {
    Restore-Env "PGPASSWORD" $prevPgPassword
    try { Remove-Item -LiteralPath $RawDir -Recurse -Force -ErrorAction Stop }
    catch {
        Write-Host ("Could not delete $RawDir, which holds each step's output before cleaning. " +
            "Delete it yourself once IRA is stopped; do not send it.") -ForegroundColor Yellow
    }
}
if ($script:Fails -eq 0) { exit 0 } else { exit 1 }

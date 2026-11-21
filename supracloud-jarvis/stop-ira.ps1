# =============================================================================
# stop-ira.ps1 -- stop the IRA processes that start-ira.ps1 started. The
# Windows twin of stop-ira.sh. Windows 10 / 11 (Windows PowerShell 5.1 or
# PowerShell 7). ASCII only, so Windows PowerShell 5.1 reads it correctly
# without a byte-order mark.
#
#   powershell -ExecutionPolicy Bypass -File stop-ira.ps1           stop IRA
#   powershell -ExecutionPolicy Bypass -File stop-ira.ps1 -DryRun   only show what it would stop
#
# Stops: the API (python -m uvicorn main:app) and the web app (Next.js under
# node), each with the processes it started.
#
# Leaves running: PostgreSQL, Memurai (Redis) and Ollama. start-ira.ps1 starts
# them as services or as "ollama serve" without recording a PID, and other
# programs may use them. stop-ira.sh also leaves PostgreSQL and Redis alone,
# and stops Ollama only when start-ira.sh recorded its PID.
#
# How it finds IRA. start-ira.ps1 records no PIDs (start-ira.sh keeps them in
# .ira-run). So this script looks at what listens on IRA's two ports: 8000 and
# 3000, or IRA_API_PORT and FRONTEND_PORT from .env. It stops a listener only
# when its command line ties it to THIS folder:
#   API      the command line runs "-m uvicorn main:app", and the program is
#            this folder's .venv\Scripts\python.exe, or was started by it. (The
#            venv's python.exe starts the real interpreter as a child, and the
#            child holds the port.)
#   web app  the program is node, and its command line runs a file under this
#            folder's frontend\node_modules\, or its node parent's does.
# It never stops a process by name. Another program's python or node, or an
# IRA started from another folder, is left running and named in a warning. So
# is a process whose command line cannot be read: another account's, or one
# started elevated (the boot task runs at the highest level). For that case,
# run this script from an elevated PowerShell.
#
# A listener that qualifies is stopped with everything it started (its process
# tree), as stop-ira.sh stops a whole process group. Windows has no gentle stop
# for a program without a window, so this is a forced stop: the same as "End
# task" in Task Manager, or closing IRA's window.
#
# Exit code: 0 when nothing listens on IRA's ports at the end, including when
# nothing was running. 1 when something still does, or when the ports or the
# processes cannot be listed. 2 when this is not Windows.
# =============================================================================
param(
    [switch]$DryRun,
    [int]$WaitSec = 15
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root    = $PSScriptRoot
$EnvFile = Join-Path $Root ".env"

# The command line start-ira.ps1 gives the API.
$UvicornPattern = '(?i)(^|\s)-m\s+uvicorn\s+main:app(\s|$)'
# Never stopped, even when one of IRA's processes started it: shared services.
$KeepRunning = '^(?i)(postgres|pg_ctl|memurai|redis-server|ollama|ollama app)(\.exe)?$'

function Write-Line([string]$tag, [string]$name, [string]$msg, [string]$color) {
    Write-Host ("  {0,-6} {1,-9} {2}" -f $tag, $name, $msg) -ForegroundColor $color
}

# The port start-ira.ps1 uses: .env first (it loads .env over the environment),
# then the environment, then its default. Only this one key is read from .env.
function Get-IraPort([string]$key, [int]$default) {
    $val = $null
    if (Test-Path -LiteralPath $EnvFile) {
        foreach ($line in @(Get-Content -LiteralPath $EnvFile -Encoding UTF8)) {
            if ($line -match ('^\s*' + [regex]::Escape($key) + '\s*=(.*)$')) {
                $raw = $matches[1].Trim()
                if ($raw -match '^"([^"]*)"') { $val = $matches[1] }
                elseif ($raw -match "^'([^']*)'") { $val = $matches[1] }
                else { $val = ($raw -replace '(^|\s+)#.*$', '').Trim() }
            }
        }
    }
    if ($null -eq $val) { $val = [Environment]::GetEnvironmentVariable($key, "Process") }
    $n = 0
    if ($val -and [int]::TryParse($val.Trim(), [ref]$n) -and $n -gt 0 -and $n -lt 65536) { return $n }
    return $default
}

# Lower case, backslashes only: Windows paths ignore case, and a command line
# may spell a path with either slash.
function ConvertTo-PathKey([string]$path) {
    if (-not $path) { return "" }
    return ($path -replace '/', '\').ToLowerInvariant()
}

# Windows reuses process IDs. A "parent" that started after its child is
# another program that was given a dead parent's ID.
function Test-StartedBefore($parent, $child) {
    if ($null -eq $parent.CreationDate -or $null -eq $child.CreationDate) { return $true }
    return ([datetime]$parent.CreationDate -le [datetime]$child.CreationDate)
}

function Get-ParentProcess($proc, $byId) {
    $parentId = [int]$proc.ParentProcessId
    if ($parentId -le 0 -or $parentId -eq [int]$proc.ProcessId -or -not $byId.ContainsKey($parentId)) { return $null }
    $parent = $byId[$parentId]
    if (-not (Test-StartedBefore $parent $proc)) { return $null }
    return $parent
}

# Is this one process IRA's API ("api") or web app ("web"), from THIS folder?
function Test-IraOwn($proc, [string]$root, [string]$role) {
    $cmd = [string]$proc.CommandLine
    if (-not $cmd) { return $false }
    $rootKey = (ConvertTo-PathKey $root).TrimEnd('\')
    if ($role -eq "api") {
        $exe = ConvertTo-PathKey ([string]$proc.ExecutablePath)
        return (($cmd -match $UvicornPattern) -and $exe.StartsWith($rootKey + '\.venv\'))
    }
    return ((([string]$proc.Name) -match '^(?i)node(\.exe)?$') -and
            (ConvertTo-PathKey $cmd).Contains($rootKey + '\frontend\node_modules\'))
}

# Is the process listening on IRA's port IRA's? It must look like the role's
# program, and it, or one of the three processes above it, must be tied to
# this folder.
function Test-IraListener($proc, $byId, [string]$root, [string]$role) {
    $cmd = [string]$proc.CommandLine
    if (-not $cmd) { return $false }
    if ($role -eq "api" -and $cmd -notmatch $UvicornPattern) { return $false }
    if ($role -eq "web" -and ([string]$proc.Name) -notmatch '^(?i)node(\.exe)?$') { return $false }
    $p = $proc
    for ($i = 0; $i -le 3 -and $null -ne $p; $i++) {
        if (Test-IraOwn $p $root $role) { return $true }
        $p = Get-ParentProcess $p $byId
    }
    return $false
}

# The highest process that is still this IRA server: for the API, the venv's
# python.exe above the interpreter it started; for the web app, a node parent
# above a node child. Stopping from there leaves nothing to start it again.
function Get-IraTop($proc, $byId, [string]$root, [string]$role) {
    $top = $proc
    for ($i = 0; $i -lt 3; $i++) {
        $parent = Get-ParentProcess $top $byId
        if ($null -eq $parent) { break }
        if ($role -eq "api") { $same = ([string]$parent.CommandLine) -match $UvicornPattern }
        else { $same = Test-IraOwn $parent $root $role }
        if (-not $same) { break }
        $top = $parent
    }
    return $top
}

# The process and everything it started, parents first.
function Get-ProcessTree($top, $all) {
    $tree = New-Object System.Collections.ArrayList
    $seen = @{}
    $queue = New-Object System.Collections.Queue
    [void]$tree.Add($top); $seen[[int]$top.ProcessId] = $true; $queue.Enqueue($top)
    while ($queue.Count -gt 0) {
        $p = $queue.Dequeue()
        foreach ($c in $all) {
            $id = [int]$c.ProcessId
            if ([int]$c.ParentProcessId -eq [int]$p.ProcessId -and -not $seen.ContainsKey($id) -and
                (Test-StartedBefore $p $c)) {
                [void]$tree.Add($c); $seen[$id] = $true; $queue.Enqueue($c)
            }
        }
    }
    return ,($tree.ToArray())
}

# Every socket listening on one of the ports. Ok is $false when the sockets
# cannot be listed at all, which is not the same as none listening.
function Get-PortOwners([int[]]$ports) {
    $result = New-Object PSObject -Property @{ Ok = $false; Rows = @() }
    if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) { return $result }
    try { $listening = @(Get-NetTCPConnection -State Listen -ErrorAction Stop) }
    catch {
        if ($_.CategoryInfo.Category -ne "ObjectNotFound") { return $result }
        $listening = @()
    }
    $rows = @()
    foreach ($c in $listening) {
        if ($ports -contains [int]$c.LocalPort) {
            $rows += New-Object PSObject -Property @{
                Port = [int]$c.LocalPort; Owner = [int]$c.OwningProcess; Address = [string]$c.LocalAddress }
        }
    }
    $result.Ok = $true
    $result.Rows = $rows
    return $result
}

# -- Start --------------------------------------------------------------------
if ($env:OS -ne "Windows_NT") {
    Write-Host "stop-ira.ps1 is for Windows. On Linux and macOS, run ./stop-ira.sh" -ForegroundColor Yellow
    exit 2
}

$ApiPort = Get-IraPort "IRA_API_PORT" 8000
$WebPort = Get-IraPort "FRONTEND_PORT" 3000
$Servers = @(
    @{ Role = "api"; Name = "ira-api";  Port = $ApiPort },
    @{ Role = "web"; Name = "frontend"; Port = $WebPort }
)

if ($DryRun) { Write-Host "Stopping IRA (dry run: nothing will be stopped)..." -ForegroundColor Cyan }
else { Write-Host "Stopping IRA..." -ForegroundColor Cyan }

$owners = Get-PortOwners @($ApiPort, $WebPort)
if (-not $owners.Ok) {
    Write-Line "[FAIL]" "ira" "cannot list listening ports here (Get-NetTCPConnection is missing or failed); nothing was stopped" Red
    Write-Host "  Close IRA's windows, or end its python and node processes in Task Manager." -ForegroundColor Yellow
    exit 1
}

$all = @()
if (@($owners.Rows).Count -gt 0) {
    try {
        $all = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop |
            Select-Object ProcessId, ParentProcessId, Name, ExecutablePath, CommandLine, CreationDate)
    } catch {
        Write-Line "[FAIL]" "ira" "cannot list processes here ($($_.Exception.Message)); nothing was stopped" Red
        exit 1
    }
}
$byId = @{}
foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }

# What to stop, in the order found (each tree's top first), and what each one is.
$order = New-Object System.Collections.ArrayList
$targets = @{}
foreach ($s in $Servers) {
    $ids = @($owners.Rows | Where-Object { $_.Port -eq $s.Port } | ForEach-Object { $_.Owner } | Sort-Object -Unique)
    if ($ids.Count -eq 0) {
        Write-Line "[SKIP]" $s.Name "not running (nothing listens on port $($s.Port))" Yellow
        continue
    }
    foreach ($ownerId in $ids) {
        $proc = $null
        if ($byId.ContainsKey([int]$ownerId)) { $proc = $byId[[int]$ownerId] }
        if ($null -eq $proc -or [int]$ownerId -le 4) {
            Write-Line "[WARN]" $s.Name "port $($s.Port) is held by pid $ownerId, which this script cannot inspect; left running" Yellow
            continue
        }
        if (-not $proc.CommandLine) {
            Write-Line "[WARN]" $s.Name ("port $($s.Port) is held by pid $ownerId ($($proc.Name)), whose command line cannot be read " +
                "(another account's, or started elevated); left running. If it is IRA, run this from an elevated PowerShell.") Yellow
            continue
        }
        if (-not (Test-IraListener $proc $byId $Root $s.Role)) {
            Write-Line "[WARN]" $s.Name "port $($s.Port) is held by pid $ownerId ($($proc.Name)), which was not started from $Root; left running" Yellow
            continue
        }
        $top = Get-IraTop $proc $byId $Root $s.Role
        foreach ($p in (Get-ProcessTree $top $all)) {
            $id = [int]$p.ProcessId
            if ($id -eq $PID -or $targets.ContainsKey($id)) { continue }
            if (([string]$p.Name) -match $KeepRunning) {
                Write-Line "[KEEP]" $s.Name "pid $id ($($p.Name)) is a shared service; left running" Cyan
                continue
            }
            $targets[$id] = @{ Proc = $p; Name = $s.Name }
            [void]$order.Add($id)
        }
    }
}

if ($DryRun) {
    foreach ($id in $order) {
        $t = $targets[$id]
        Write-Line "[PLAN]" $t.Name ("would stop pid {0} ({1})" -f $id, $t.Proc.Name) Cyan
    }
    Write-Host "Dry run: nothing was stopped."
    exit 0
}

foreach ($id in $order) {
    $t = $targets[$id]
    try {
        Stop-Process -Id $id -Force -ErrorAction Stop
        Write-Line "[OK]" $t.Name ("stopped pid {0} ({1})" -f $id, $t.Proc.Name) Green
    } catch {
        Write-Line "[WARN]" $t.Name ("pid {0} ({1}): {2}" -f $id, $t.Proc.Name, $_.Exception.Message) Yellow
    }
}

# Wait for the ports to close, but not for a program this script left running.
$left = $owners
if ($targets.Count -gt 0) {
    $deadline = (Get-Date).AddSeconds($WaitSec)
    while ($true) {
        $left = Get-PortOwners @($ApiPort, $WebPort)
        if ($left.Ok -and @($left.Rows).Count -eq 0) { break }
        if ((Get-Date) -ge $deadline) { break }
        Start-Sleep -Milliseconds 500
    }
}

if ($env:APPDATA) {
    $vbs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\ira-stack.vbs"
    if (Test-Path -LiteralPath $vbs) {
        Write-Host "  Note: the logon autostart (start-ira.ps1 -InstallAutostart) starts IRA again at your next sign-in." -ForegroundColor DarkGray
    }
}

if (-not $left.Ok) {
    Write-Line "[FAIL]" "ira" "cannot list listening ports to check that IRA stopped" Red
    exit 1
}
$still = @($left.Rows)
if ($still.Count -gt 0) {
    $what = ($still | ForEach-Object { "port $($_.Port) (pid $($_.Owner))" } | Sort-Object -Unique) -join ", "
    Write-Host "Still in use: $what. IRA cannot start on these ports until they are free." -ForegroundColor Red
    exit 1
}
Write-Host "Done. PostgreSQL, Memurai (Redis) and Ollama left running." -ForegroundColor Green
exit 0

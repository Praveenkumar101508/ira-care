# =============================================================================
# uninstall-windows.ps1 -- remove a native IRA install on Windows, keeping a
# backup you can restore from. The Windows twin of scripts/uninstall-native.sh.
# Windows 10 / 11 (Windows PowerShell 5.1 or PowerShell 7). ASCII only, so
# Windows PowerShell 5.1 reads it correctly without a byte-order mark.
#
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall-windows.ps1
#       dry run: shows what would go; changes nothing
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall-windows.ps1 -Yes
#       back up, then remove
#
# Options (only with -Yes do they change anything):
#   -RemoveRoles   also drop IRA's database roles (ira_app, ira_worker,
#                  ira_migrator, ira_schema_owner). A role that another
#                  database still uses is kept, and the script says so.
#   -RemoveModel   also remove the chat model setup pulled (OLLAMA_MODEL_DEEP
#                  in .env). Off by default: other programs may use it.
#
# What -Yes does, in this order:
#   1. refuses if IRA is running (its API or its web page answers): Windows
#      cannot delete files a running program holds. Stop it with stop-ira.ps1.
#   2. takes a backup with scripts\ira-backup.ps1 and checks the archive holds
#      both the database dump and .env. If the backup fails, NOTHING is removed.
#   3. drops IRA's database (POSTGRES_DB in .env)
#   4. removes the logon autostart (Startup\ira-stack.vbs) if it starts THIS folder
#   5. removes .venv, frontend\node_modules, frontend\.next and .env
#
# Never removed: the backups folder, this source folder, .env.backup-* files that
# an earlier restore kept, PostgreSQL, Memurai, Ollama, Node, Python, and shared
# caches. The boot task that start-ira.ps1 -InstallService registers can only be
# removed from an elevated prompt; this script prints the command and does not
# ask for elevation.
#
# The PostgreSQL superuser's password is asked for, or read from -PgSuperPassword
# or $env:PGPASSWORD. It is never written to disk or put on a command line.
#
# To undo:  scripts\setup-windows.ps1
#           scripts\ira-backup.ps1 restore <the archive printed below> -Yes
#           start-ira.ps1   and sign in with your old password.
# =============================================================================
param(
    [switch]$Yes,
    [switch]$RemoveRoles,
    [switch]$RemoveModel,
    [string]$PgSuperUser = "postgres",
    [string]$PgSuperPassword = $env:PGPASSWORD
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root      = Split-Path -Parent $ScriptDir
$EnvFile   = Join-Path $Root ".env"
$OnWindows = ($env:OS -eq "Windows_NT")
$Roles     = @("ira_app", "ira_worker", "ira_migrator", "ira_schema_owner")
$LocalPaths = @(".venv", ".ira-run", "frontend/node_modules", "frontend/.next", ".env")
$BootTask  = "IRA-Stack-Boot"

function Write-Ok($msg)   { Write-Host "  [OK]   $msg" -ForegroundColor Green }
function Write-Plan($msg) { Write-Host "  [PLAN] $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Stop-Uninstall($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red; exit 1 }
function Test-Cmd($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

# Read KEY from .env without running it.
function Get-EnvValue([string]$key) {
    if (-not (Test-Path -LiteralPath $EnvFile)) { return "" }
    $line = Select-String -LiteralPath $EnvFile -Pattern ("^" + [regex]::Escape($key) + "=(.*)$") |
        Select-Object -Last 1
    if (-not $line) { return "" }
    $v = $line.Matches[0].Groups[1].Value -replace '\s+#.*$', ''
    return $v.Trim().Trim('"')
}

function Test-Tcp([string]$hostName, [int]$port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try { return $client.ConnectAsync($hostName, $port).Wait(1000) -and $client.Connected }
    catch { return $false }
    finally { $client.Close() }
}

function Get-SuperPassword {
    if ($script:PgSuperPassword) { return $script:PgSuperPassword }
    $sec = Read-Host "Password for the PostgreSQL superuser '$PgSuperUser'" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { $script:PgSuperPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    return $script:PgSuperPassword
}

# Run psql as the superuser. The password reaches it through PGPASSWORD for this
# call only.
function Invoke-SuperSql([string]$sql) {
    $previous = $env:PGPASSWORD
    $env:PGPASSWORD = Get-SuperPassword
    try {
        $out = & psql -h localhost -p $script:PgPort -U $PgSuperUser -d postgres -XtAq -v ON_ERROR_STOP=1 -c $sql 2>&1
        $script:LastToolExit = $LASTEXITCODE
        return $out
    } finally { $env:PGPASSWORD = $previous }
}

$script:PgPort = Get-EnvValue "POSTGRES_PORT"; if (-not $script:PgPort) { $script:PgPort = "5432" }
$DbName = Get-EnvValue "POSTGRES_DB"; if (-not $DbName) { $DbName = "jarvis_db" }
if ($DbName -notmatch '^[A-Za-z0-9_]{1,63}$') { Stop-Uninstall ".env names an unusable database ($DbName)" }
$BackupDir = Get-EnvValue "BACKUP_DIR"
if (-not $BackupDir -or $BackupDir -eq "/backups") {
    $BackupDir = Join-Path ([Environment]::GetFolderPath("UserProfile")) ".ira\backups"
}
$Model   = Get-EnvValue "OLLAMA_MODEL_DEEP"
$ApiPort = Get-EnvValue "IRA_API_PORT";  if (-not $ApiPort) { $ApiPort = "8000" }
$FePort  = Get-EnvValue "FRONTEND_PORT"; if (-not $FePort)  { $FePort = "3000" }
$script:LastToolExit = 0

# The logon launcher start-ira.ps1 -InstallAutostart writes. Only one that starts
# THIS folder's start-ira.ps1 is ours to remove.
$StartupVbs = ""
if ($env:APPDATA) {
    $candidate = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\ira-stack.vbs"
    $ours = Join-Path $Root "start-ira.ps1"
    if ((Test-Path -LiteralPath $candidate) -and
        ((Get-Content -LiteralPath $candidate -Raw).Contains($ours))) { $StartupVbs = $candidate }
}
$HasBootTask = $false
if ($OnWindows -and (Test-Cmd "Get-ScheduledTask")) {
    $HasBootTask = [bool](Get-ScheduledTask -TaskName $BootTask -ErrorAction SilentlyContinue)
}

# -- What is here ---------------------------------------------------------------
$mode = if ($Yes) { "removing" } else { "dry run, nothing will change" }
Write-Host "IRA uninstall -- $mode" -ForegroundColor Cyan
Write-Plan "back up to $BackupDir (database + .env), and stop if that fails"
Write-Plan "drop database $DbName on port $($script:PgPort)"
if ($StartupVbs) { Write-Plan "remove the logon autostart $StartupVbs" }
foreach ($p in $LocalPaths) {
    if (Test-Path -LiteralPath (Join-Path $Root $p)) { Write-Plan "remove $p" }
}
if ($RemoveRoles) { Write-Plan ("drop roles " + ($Roles -join " ") + " (each kept if another database still uses it)") }
if ($RemoveModel) {
    if ($Model) { Write-Plan "remove chat model $Model from Ollama" } else { Write-Warn "no OLLAMA_MODEL_DEEP in .env; no model to remove" }
}
foreach ($f in @(Get-ChildItem -LiteralPath $Root -Filter ".env.backup-*" -Force -ErrorAction SilentlyContinue)) {
    Write-Warn "keeping $($f.Name) (a .env an earlier restore replaced)"
}
if ($HasBootTask) {
    Write-Warn "the boot task '$BootTask' is registered and is not removed here. From an elevated PowerShell:"
    Write-Host "         Unregister-ScheduledTask -TaskName $BootTask -Confirm:`$false"
}
Write-Ok "kept: backups in $BackupDir, this folder, PostgreSQL, Memurai, Ollama and shared caches"

if (-not $Yes) {
    Write-Host ""
    Write-Host "Dry run: nothing was changed. Run again with -Yes to back up and remove."
    exit 0
}

# -- 1. IRA must not be running ---------------------------------------------------
Write-Host ""
# Anything listening on either port counts, not only a healthy /health: an IRA
# that is starting, hung or failing still holds its files open.
if ((Test-Tcp "127.0.0.1" ([int]$ApiPort)) -or (Test-Tcp "127.0.0.1" ([int]$FePort))) {
    Stop-Uninstall ("IRA is running (port $ApiPort or $FePort answers) -- stop it with " +
        "powershell -ExecutionPolicy Bypass -File stop-ira.ps1, then run this again. Nothing was removed.")
}
foreach ($t in @("psql", "tar")) {
    if (-not (Test-Cmd $t)) { Stop-Uninstall "$t not found -- add PostgreSQL's bin folder to PATH. Nothing was removed." }
}

# -- 2. Back up, and refuse to go further without a usable archive ----------------
$archive = ""
if (Test-Path -LiteralPath $EnvFile) {
    $started = (Get-Date).AddSeconds(-1)
    $previous = $env:PGPASSWORD
    $env:PGPASSWORD = Get-SuperPassword
    # ira-backup.ps1 reports failure through its exit code and does not set one
    # on success, so start from 0 rather than inherit an earlier command's.
    $global:LASTEXITCODE = 0
    try { & (Join-Path $ScriptDir "ira-backup.ps1") backup -PgSuperUser $PgSuperUser; $backupExit = $LASTEXITCODE }
    finally { $env:PGPASSWORD = $previous }
    if ($backupExit -ne 0) { Stop-Uninstall "backup failed -- nothing was removed" }
    $newest = Get-ChildItem -LiteralPath $BackupDir -Filter "ira-backup-*.tar.gz" -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -ge $started } | Sort-Object LastWriteTime | Select-Object -Last 1
    if (-not $newest) { Stop-Uninstall "backup reported success but no new archive is in $BackupDir -- nothing was removed" }
    $archive = $newest.FullName
    $listing = @(& tar -tzf $archive 2>$null)
    if ($LASTEXITCODE -ne 0) { Stop-Uninstall "cannot read $archive -- nothing was removed" }
    foreach ($member in @("database.dump", "env")) {
        if ($listing -notcontains $member) { Stop-Uninstall "$archive has no $member -- nothing was removed" }
    }
    Write-Ok "backup checked: $archive"
} else {
    Write-Warn "no .env: there is no install to back up"
}

# -- 3. Database ------------------------------------------------------------------
$exists = Invoke-SuperSql "SELECT 1 FROM pg_database WHERE datname = '$DbName'"
if ($script:LastToolExit -ne 0) { Stop-Uninstall "cannot reach PostgreSQL as $PgSuperUser on port $($script:PgPort) (backup kept: $archive)" }
if ("$exists".Trim() -eq "1") {
    Invoke-SuperSql "DROP DATABASE IF EXISTS `"$DbName`"" | Out-Null
    if ($script:LastToolExit -ne 0) { Stop-Uninstall "could not drop $DbName (backup kept: $archive)" }
    Write-Ok "database $DbName dropped"
} else {
    Write-Warn "database $DbName does not exist; nothing to drop"
}
if ($RemoveRoles) {
    foreach ($role in $Roles) {
        Invoke-SuperSql "DROP ROLE IF EXISTS $role" | Out-Null
        if ($script:LastToolExit -eq 0) { Write-Ok "role $role dropped" }
        else { Write-Warn "role $role kept: another database still uses it" }
    }
}

# -- 4. Logon autostart -------------------------------------------------------------
if ($StartupVbs) {
    Remove-Item -LiteralPath $StartupVbs -Force
    Write-Ok "removed the logon autostart"
}

# -- 5. Files in this folder ----------------------------------------------------------
foreach ($p in $LocalPaths) {
    $full = Join-Path $Root $p
    if (Test-Path -LiteralPath $full) {
        Remove-Item -LiteralPath $full -Recurse -Force
        Write-Ok "removed $p"
    }
}

# -- 6. Model (opt-in) ------------------------------------------------------------
if ($RemoveModel -and $Model) {
    if (Test-Cmd "ollama") {
        & ollama rm $Model | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Ok "removed model $Model" } else { Write-Warn "could not remove model $Model" }
    } else { Write-Warn "ollama not found; model $Model kept" }
}

Write-Host ""
Write-Host "IRA is uninstalled." -ForegroundColor Green
if ($archive) {
    Write-Host "  Your backup: $archive  (it contains your .env secrets; keep it safe)"
    Write-Host "  To undo:     scripts\setup-windows.ps1"
    Write-Host "               scripts\ira-backup.ps1 restore `"$archive`" -Yes"
}
if ($HasBootTask) { Write-Host "  Still registered: the boot task '$BootTask' (see above)." }
Write-Host "  To remove IRA completely, delete this folder: $Root"

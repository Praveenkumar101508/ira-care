# =============================================================================
# ira-backup.ps1 -- back up IRA, and restore it on this or another computer.
# Windows 10 / 11 (Windows PowerShell 5.1 or PowerShell 7). The Windows twin of
# scripts/ira-backup.sh: the same archive, so a backup made on one system
# restores on the other.
#
#   powershell -ExecutionPolicy Bypass -File scripts\ira-backup.ps1 backup
#       -> %USERPROFILE%\.ira\backups\ira-backup-<time>.tar.gz
#   powershell -ExecutionPolicy Bypass -File scripts\ira-backup.ps1 restore <archive> -Yes
#
# Why this is not the in-app backup (POST /api/v1/backup/create): the API runs
# as ira_app, which owns nothing and is held by forced row-level security, so a
# dump as ira_app is refused or comes out empty. A complete backup needs a role
# that sees every row, so it runs here, as the PostgreSQL superuser ("postgres",
# the account setup-windows.ps1 used), outside the app. Its password is asked
# for, or read from -PgSuperPassword or $env:PGPASSWORD; it is never written to
# disk or passed on a command line.
#
# What an archive holds:
#   database.dump    complete pg_dump (custom format) of IRA's database
#   env              the .env -- its keys decrypt TOTP secrets, voiceprints and
#                    connector credentials in the dump. Without it a restored
#                    database cannot open its own encrypted data.
#   data-<name>.tar  each folder IRA keeps outside the database ($DataLabels
#                    below): notes, project folders and their .env files, the
#                    sealed vault, uploads, coding-agent tasks, "about me" files
#   MANIFEST.txt     when, which commit, which PostgreSQL, where each folder
#                    was, checksums
# The archive therefore contains secrets: only your Windows account can read it.
# Keep it as carefully as the computer it came from.
#
# Moving to another computer:
#   1. old computer:  scripts\ira-backup.ps1 backup        (IRA may be running)
#   2. new computer:  install IRA with scripts\setup-windows.ps1, then stop IRA
#   3. new computer:  scripts\ira-backup.ps1 restore <archive> -Yes
#   4. new computer:  start-ira.ps1 -- and sign in with the OLD password.
# Restore replaces this computer's IRA database, .env and data folders. It
# takes a safety backup of all three first, and moves each folder it replaces
# into the backups folder rather than deleting it.
# =============================================================================
param(
    [Parameter(Position = 0)] [string]$Command = "",
    [Parameter(Position = 1)] [string]$Archive = "",
    [switch]$Yes,
    [string]$Dest = "",
    [string]$PgSuperUser = "postgres",
    [string]$PgSuperPassword = $env:PGPASSWORD
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root      = Split-Path -Parent $ScriptDir
$IraDir    = Join-Path $Root "ira"
$EnvFile   = Join-Path $Root ".env"
$OnWindows = ($env:OS -eq "Windows_NT")
$Sep       = if ($OnWindows) { "\" } else { "/" }
$VenvPython = if ($OnWindows) { Join-Path $Root ".venv\Scripts\python.exe" } else { Join-Path $Root ".venv/bin/python" }
# The home folder as Python's expanduser() finds it: USERPROFILE on Windows.
$UserHome  = [Environment]::GetFolderPath("UserProfile")
if ($OnWindows -and $env:USERPROFILE) { $UserHome = $env:USERPROFILE }
# SQL reaches psql on stdin. Windows PowerShell 5.1 encodes what it pipes to a
# program as ASCII, which turns every other character into "?": a folder under
# C:\Users\<a name with an accent> would then match no upload row.
$OutputEncoding = New-Object System.Text.UTF8Encoding $false

function Write-Ok($msg) { Write-Host "  [OK]   $msg" -ForegroundColor Green }
function Stop-Backup($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red; exit 1 }
function Test-Cmd($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

# Read KEY from a .env file without running it. UTF-8, as the API reads it:
# Windows PowerShell 5.1 would otherwise misread a folder's accented name.
function Get-EnvValue([string]$key, [string]$file = $EnvFile) {
    if (-not (Test-Path -LiteralPath $file)) { return "" }
    $line = Select-String -LiteralPath $file -Encoding UTF8 -Pattern ("^" + [regex]::Escape($key) + "=(.*)$") |
        Select-Object -Last 1
    if (-not $line) { return "" }
    $v = $line.Matches[0].Groups[1].Value -replace '\s+#.*$', ''
    $v = $v.Trim().Trim('"')
    # A single-quoted value, as python-dotenv (and so the app) reads it.
    if ($v.Length -ge 2 -and $v.StartsWith("'") -and $v.EndsWith("'")) { $v = $v.Substring(1, $v.Length - 2) }
    return $v
}

# Only the current account may read or change $path. It is named by its full
# identity (DOMAIN\user), as setup-windows.ps1 names it for .env: a bare
# $env:USERNAME can resolve to a different account of the same name on a
# domain-joined PC, and this restricts the restored .env too.
function Protect-Path([string]$path, [switch]$Directory) {
    if ($OnWindows) {
        $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $grant = if ($Directory) { "${me}:(OI)(CI)F" } else { "${me}:F" }
        & icacls $path /inheritance:r /grant:r $grant | Out-Null
        if ($LASTEXITCODE -ne 0) { Stop-Backup "could not restrict access to $path" }
    } else {
        $mode = if ($Directory) { "700" } else { "600" }
        & chmod $mode $path
        if ($LASTEXITCODE -ne 0) { Stop-Backup "could not restrict access to $path" }
    }
}

# Quote a value for a single-quoted psql argument. Inside psql's single quotes a
# backslash is an escape as well as the quote, so both are doubled; doubling only
# the quote would let a password end the argument early.
function ConvertTo-PsqlQuoted([string]$value) {
    return ($value -replace '\\', '\\') -replace "'", "''"
}

function Get-Sha256([string]$path) { return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLower() }

function New-WorkDir {
    $d = Join-Path ([System.IO.Path]::GetTempPath()) ("ira-backup-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $d | Out-Null
    Protect-Path $d -Directory
    return $d
}

# Where archives are written: BACKUP_DIR, unless it is the container's /backups.
function Get-BackupDir {
    $d = Get-EnvValue "BACKUP_DIR"
    if (-not $d -or $d -eq "/backups") {
        $d = Join-Path (Join-Path ([Environment]::GetFolderPath("UserProfile")) ".ira") "backups"
    }
    return $d
}

# The folders IRA keeps outside the database, as ira-backup.sh lists them. Each
# is found the way the API finds it: the .env value when one is set, else IRA's
# default. start-ira.ps1 runs the API in ira\ (-WorkingDirectory $iraDir), so a
# relative path is relative to ira\.
#   notes           NOTES_DIR           data/notes           notes, as .md files
#   projects        PROJECTS_DIR        data/projects        project folders
#   secrets         SECRETS_DIR         data/secrets         each project's .env
#   armory          ARMORY_VAULT_DIR    data/armory          the sealed vault
#   files           FILES_DATA_DIR      ..\data\files        uploads
#   forge           (no setting)        data/forge           coding-agent tasks
#   self_knowledge  SELF_KNOWLEDGE_DIR  data/self_knowledge  "about me" files
# Left out on purpose: data\interview, a downloaded question set that is not
# redistributable and that its notice says how to rebuild.
$DataLabels = @("notes", "projects", "secrets", "armory", "files", "forge", "self_knowledge")

function Get-DataDir([string]$label, [string]$file = $EnvFile) {
    $key = ""
    switch -CaseSensitive ($label) {
        "notes"          { $key = "NOTES_DIR"; $def = "data/notes" }
        "projects"       { $key = "PROJECTS_DIR"; $def = "data/projects" }
        "secrets"        { $key = "SECRETS_DIR"; $def = "data/secrets" }
        "armory"         { $key = "ARMORY_VAULT_DIR"; $def = "data/armory" }
        "files"          { $key = "FILES_DATA_DIR"; $def = $Root + $Sep + "data" + $Sep + "files" }
        "forge"          { $def = "data/forge" }
        "self_knowledge" { $key = "SELF_KNOWLEDGE_DIR"; $def = "data/self_knowledge" }
        default          { return "" }
    }
    $v = ""; if ($key) { $v = Get-EnvValue $key $file }
    if (-not $v) { $v = $def }
    # Only the self-knowledge setting expands ~ (memory/self_knowledge.py); the
    # others take it literally, as a folder named ~ inside ira\. Python's Path
    # does the same on Windows, where ~\ and ~/ both name the home folder.
    $slash = if ($OnWindows) { '[\\/]' } else { '/' }
    if ($label -eq "self_knowledge" -and $v -match ('^~(' + $slash + '|$)')) { return $UserHome + $v.Substring(1) }
    if ($OnWindows) {
        if ($v -match '^([A-Za-z]:|[\\/]{2})') { return $v }   # C:\..., \\server\share\...: as is
        # \folder is on the drive the API runs on, ira\'s.
        if ($v -match '^[\\/]') { return ($IraDir -replace '^([A-Za-z]:|[\\/]{2}[^\\/]+[\\/][^\\/]+).*$', '$1') + $v }
    } elseif ($v.StartsWith("/")) { return $v }
    return $IraDir + $Sep + $v
}

# The folder's path with every link resolved, as the API's Path.resolve() gives
# it: each upload's row begins with that string. "" when there is no folder.
# IRA's own Python resolves it, so a junction, a mapped drive or a name typed in
# another case comes out as the API wrote it; without .venv, the full path. The
# path goes in through the environment and comes back as ASCII JSON, so no code
# page can change a character on the way.
function Resolve-DataDir([string]$path) {
    if (-not $path -or -not (Test-Path -LiteralPath $path -PathType Container)) { return "" }
    $full = [System.IO.Path]::GetFullPath($path)
    if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
        $previous = $env:IRA_RESOLVE_PATH
        $env:IRA_RESOLVE_PATH = $full
        try {
            $out = & $VenvPython -I -S -c "import json, os, pathlib; print(json.dumps({'p': str(pathlib.Path(os.environ['IRA_RESOLVE_PATH']).resolve())}))"
            if ($LASTEXITCODE -eq 0 -and $out) { return [string]((("" + $out) | ConvertFrom-Json).p) }
        } finally { $env:IRA_RESOLVE_PATH = $previous }
    }
    return $full
}

# The data-<name>.tar files of an unpacked archive, in name order.
function Get-DataTars([string]$work) {
    return @(Get-ChildItem -LiteralPath $work -File | Where-Object { $_.Name -cmatch '^data-.+\.tar$' } |
        Sort-Object Name)
}

function Get-SuperPassword {
    if ($script:PgSuperPassword) { return $script:PgSuperPassword }
    $sec = Read-Host "Password for the PostgreSQL superuser '$PgSuperUser'" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { $script:PgSuperPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    return $script:PgSuperPassword
}

# Run a PostgreSQL client as the superuser. The password reaches it through
# PGPASSWORD for this call only.
function Invoke-AsSuper([string]$tool, [string[]]$arguments, $stdin = $null) {
    $previous = $env:PGPASSWORD
    $env:PGPASSWORD = Get-SuperPassword
    try {
        $all = @("-h", "localhost", "-p", $script:PgPort, "-U", $PgSuperUser) + $arguments
        if ($null -ne $stdin) { $out = $stdin | & $tool @all 2>&1 } else { $out = & $tool @all 2>&1 }
        $script:LastToolExit = $LASTEXITCODE
        return $out
    } finally { $env:PGPASSWORD = $previous }
}

foreach ($t in @("pg_dump", "pg_restore", "psql", "tar")) {
    if (-not (Test-Cmd $t)) {
        Stop-Backup "$t not found -- add PostgreSQL's bin folder to PATH (tar ships with Windows 10 and later)"
    }
}
$script:PgPort = Get-EnvValue "POSTGRES_PORT"; if (-not $script:PgPort) { $script:PgPort = "5432" }
$script:DbName = Get-EnvValue "POSTGRES_DB";   if (-not $script:DbName) { $script:DbName = "jarvis_db" }
$script:LastToolExit = 0

function Invoke-Backup([string]$destination) {
    if (-not $destination) { $destination = Get-BackupDir }
    if (-not (Test-Path -LiteralPath $EnvFile)) { Stop-Backup "no .env at $EnvFile -- nothing to back up" }
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    Protect-Path $destination -Directory

    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
    $work = New-WorkDir
    try {
        $dump = Join-Path $work "database.dump"
        Invoke-AsSuper "pg_dump" @("-Fc", "-d", $script:DbName, "-f", $dump) | Out-Null
        if ($script:LastToolExit -ne 0) { Stop-Backup "pg_dump of $($script:DbName) failed -- is PostgreSQL running?" }
        Copy-Item -LiteralPath $EnvFile -Destination (Join-Path $work "env")

        # One tar per data folder. A folder that does not exist here is left out,
        # and a restore of this archive then leaves that folder alone.
        $members = @("database.dump", "env", "MANIFEST.txt")
        $where = @(); $sums = @(); $filesRoot = ""
        foreach ($label in $DataLabels) {
            $dir = Resolve-DataDir (Get-DataDir $label)
            if (-not $dir) { continue }
            $tarFile = Join-Path $work "data-$label.tar"
            & tar -cf $tarFile -C $dir .
            if ($LASTEXITCODE -ne 0) { Stop-Backup "could not archive $dir (if IRA was writing to it, run the backup again)" }
            $members += "data-$label.tar"
            $where += "data ${label}: $dir"
            $sums += "sha256 data-${label}.tar: $(Get-Sha256 $tarFile)"
            if ($label -eq "files") { $filesRoot = $dir }
        }

        $commit = "unknown"
        if (Test-Cmd git) {
            $c = & git -C $Root rev-parse HEAD 2>$null
            if ($LASTEXITCODE -eq 0 -and $c) { $commit = "$c".Trim() }
        }
        $version = ("" + (Invoke-AsSuper "psql" @("-d", $script:DbName, "-tAc", "SHOW server_version"))).Trim()
        $applied = ("" + (Invoke-AsSuper "psql" @("-d", $script:DbName, "-tAc", "SELECT count(*) FROM schema_migrations"))).Trim()
        $manifest = @(
            "IRA backup",
            "created_utc: $stamp",
            "commit: $commit",
            "database: $($script:DbName)",
            "postgresql: $version",
            "migrations_applied: $applied"
        ) + $where
        # Uploads' rows in the database name each file by its full path under
        # this folder; a restore elsewhere rewrites that start of the path.
        if ($filesRoot) { $manifest += "files_root: $filesRoot" }
        $manifest += @(
            "sha256 database.dump: $(Get-Sha256 $dump)",
            "sha256 env: $(Get-Sha256 (Join-Path $work 'env'))"
        ) + $sums
        # LF, as ira-backup.sh writes it, so either script verifies either archive.
        # UTF-8 with no byte-order mark: the same bytes as ASCII, and a folder's
        # accented name stays as it is.
        [System.IO.File]::WriteAllText((Join-Path $work "MANIFEST.txt"), (($manifest -join "`n") + "`n"),
            (New-Object System.Text.UTF8Encoding $false))

        # Never over another archive: a restore takes two safety copies when the
        # archive names a second database, often within the same second.
        $archive = Join-Path $destination "ira-backup-$stamp.tar.gz"; $n = 1
        while (Test-Path -LiteralPath $archive) { $n++; $archive = Join-Path $destination "ira-backup-$stamp-$n.tar.gz" }
        & tar -czf $archive -C $work @members
        if ($LASTEXITCODE -ne 0) { Stop-Backup "could not write $archive" }
        Protect-Path $archive
        $size = "{0:N0} KB" -f ((Get-Item -LiteralPath $archive).Length / 1KB)
        Write-Ok "backup written: $archive ($size) -- it contains your .env secrets"
        return $archive
    } finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# Move what a restore replaces into the backups folder. Windows PowerShell 5.1
# cannot move a folder to another drive, so there it is copied, and the original
# is removed only once the whole copy is made.
function Move-Aside([string]$path, [string]$to) {
    if ([System.IO.Path]::GetPathRoot($path) -eq [System.IO.Path]::GetPathRoot($to)) {
        Move-Item -LiteralPath $path -Destination $to
        return
    }
    Copy-Item -LiteralPath $path -Destination $to -Recurse -Force
    Remove-Item -LiteralPath $path -Recurse -Force
}

# Put each archived folder back where the restored .env says IRA keeps it. A
# folder already there is moved into the backups folder, never deleted, and
# never left beside the original: two of these folders are not git-ignored.
function Restore-DataFolders([string]$work, [string]$replaced) {
    foreach ($t in @(Get-DataTars $work)) {
        $label = $t.Name.Substring(5, $t.Name.Length - 9)
        if ($DataLabels -cnotcontains $label) {
            Write-Host "  skipped $($t.Name): this version of IRA keeps no such folder"
            continue
        }
        $dir = Get-DataDir $label
        try { $dir = [System.IO.Path]::GetFullPath($dir) }
        catch { Stop-Backup "the restored .env names an unusable $label folder ($dir) -- the database is restored, the data folders are not" }
        # A linked folder keeps its link: what changes is where it points.
        $item = Get-Item -LiteralPath $dir -Force -ErrorAction SilentlyContinue
        if ($item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            $target = Resolve-DataDir $dir
            if ($target) { $dir = $target }
        }
        try {
            if ((Test-Path -LiteralPath $dir -PathType Container) -and @(Get-ChildItem -LiteralPath $dir -Force).Count -eq 0) {
                Remove-Item -LiteralPath $dir -Force
            } elseif (Test-Path -LiteralPath $dir) {
                New-Item -ItemType Directory -Force -Path $replaced | Out-Null
                Protect-Path $replaced -Directory
                $keep = Join-Path $replaced $label
                Move-Aside $dir $keep
                Write-Host "  the $label folder it replaces is kept in $keep"
            }
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
        } catch {
            Stop-Backup "could not move $dir aside ($($_.Exception.Message)) -- the database is restored, the data folders are not"
        }
        # Each project's .env and the sealed vault stay this account's alone, as
        # the restored .env does. On Windows a new folder takes its parent's
        # readers and tar sets no ACL, so the folder is restricted before its
        # files arrive; and after, because elsewhere tar resets the folder's mode.
        $private = ($label -eq "secrets" -or $label -eq "armory")
        if ($private) { Protect-Path $dir -Directory }
        & tar -xf $t.FullName -C $dir
        if ($LASTEXITCODE -ne 0) { Stop-Backup "could not restore the $label folder into $dir" }
        if ($private) { Protect-Path $dir -Directory }
        Write-Ok "$label folder restored to $dir"
    }
}

# Each upload's row names its file by full path, as the old computer had it.
# When this computer keeps uploads in a different folder, point the rows at it.
# Only paths under the old folder change: the old folder followed by / (Linux,
# macOS) or \ (Windows). The rest of each path is kept as it is, separators and
# all, so a Linux archive's rows come out as C:\...\files/tenants/...
function Update-UploadRows([string]$work) {
    $old = ""
    foreach ($line in [System.IO.File]::ReadAllLines((Join-Path $work "MANIFEST.txt"), [System.Text.Encoding]::UTF8)) {
        if ($line.StartsWith("files_root: ")) { $old = $line.Substring(12); break }
    }
    if (-not $old -or -not (Test-Path -LiteralPath (Join-Path $work "data-files.tar"))) { return }
    $new = Resolve-DataDir (Get-DataDir "files")
    # PostgreSQL compares case by case, so this does too (-ceq, not -eq).
    if (-not $new -or $new -ceq $old) { return }
    # Both paths reach psql as variables, quoted for \set; psql then quotes each
    # :'name' as a literal. E'\\' is one backslash whatever standard_conforming_strings is.
    $sql = "\encoding UTF8`n" +
        "\set old '$(ConvertTo-PsqlQuoted $old)'`n" +
        "\set new '$(ConvertTo-PsqlQuoted $new)'`n" +
        "WITH moved AS (UPDATE files SET storage_path = :'new' || substr(storage_path, length(:'old') + 1)`n" +
        "    WHERE left(storage_path, length(:'old') + 1) IN (:'old' || '/', :'old' || E'\\') RETURNING 1)`n" +
        "SELECT count(*) FROM moved;`n"
    $out = Invoke-AsSuper "psql" @("-d", $script:DbName, "-v", "ON_ERROR_STOP=1", "-tA", "-q", "-f", "-") $sql
    if ($script:LastToolExit -ne 0) {
        Stop-Backup "could not point the uploads at $new -- the files are there, but their rows still name $old"
    }
    $count = ("" + $out).Trim()
    Write-Ok "uploads: $count file rows now name $new (they named $old)"
}

function Invoke-Restore([string]$archive, [bool]$confirmed) {
    # A bare name is looked for in the backup folder too: that is where `backup`
    # writes, and INSTALL.md gives the name alone. That folder, read from this
    # computer's .env before it is replaced, also keeps what the restore replaces.
    $backups = Get-BackupDir
    if ($archive -and -not (Test-Path -LiteralPath $archive) -and [System.IO.Path]::GetFileName($archive) -eq $archive) {
        $inBackups = Join-Path $backups $archive
        if (Test-Path -LiteralPath $inBackups -PathType Leaf) { $archive = $inBackups }
    }
    if (-not $archive -or -not (Test-Path -LiteralPath $archive)) {
        Stop-Backup "usage: ira-backup.ps1 restore <archive.tar.gz> -Yes (no such file: $archive; backups are written to $backups)"
    }
    if (-not $confirmed) { Stop-Backup "restore REPLACES this computer's IRA database, .env and data folders -- re-run with -Yes" }
    $apiPort = Get-EnvValue "IRA_API_PORT"; if (-not $apiPort) { $apiPort = "8000" }
    $running = $false
    try {
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "http://127.0.0.1:$apiPort/health" | Out-Null
        $running = $true
    } catch { $running = $false }
    if ($running) { Stop-Backup "IRA is running -- stop it first" }
    $py = $VenvPython
    if (-not (Test-Path -LiteralPath $py)) {
        Stop-Backup "no .venv -- install IRA on this computer with scripts\setup-windows.ps1 first"
    }

    $work = New-WorkDir
    try {
        & tar -xzf $archive -C $work
        if ($LASTEXITCODE -ne 0) { Stop-Backup "cannot read $archive" }
        foreach ($f in @("database.dump", "env", "MANIFEST.txt")) {
            $p = Join-Path $work $f
            if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { Stop-Backup "$archive is not an IRA backup" }
        }
        $manifestFile = Join-Path $work "MANIFEST.txt"
        $dump = Join-Path $work "database.dump"
        $expected = "sha256 database.dump: $(Get-Sha256 $dump)"
        if (-not (Select-String -LiteralPath $manifestFile -SimpleMatch -Pattern $expected -Quiet)) {
            Stop-Backup "database.dump does not match its manifest checksum -- refusing to restore it"
        }
        # Every data folder's checksum too, before anything is changed. An archive
        # made before data folders were included has none, and restores as before.
        foreach ($t in @(Get-DataTars $work)) {
            $expected = "sha256 $($t.Name): $(Get-Sha256 $t.FullName)"
            if (-not (Select-String -LiteralPath $manifestFile -SimpleMatch -Pattern $expected -Quiet)) {
                Stop-Backup "$($t.Name) does not match its manifest checksum -- refusing to restore it"
            }
        }
        $replaced = Join-Path $backups ("replaced-" + (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss"))
        $n = 1; $base = $replaced
        while (Test-Path -LiteralPath $replaced) { $n++; $replaced = "$base-$n" }

        # The database the archive restores into is named by the ARCHIVE's .env,
        # and that is the one dropped below. It may differ from this install's.
        $restoredDb = Get-EnvValue "POSTGRES_DB" (Join-Path $work "env"); if (-not $restoredDb) { $restoredDb = "jarvis_db" }
        if ($restoredDb -notmatch '^[A-Za-z0-9_]{1,63}$') { Stop-Backup "the archive names an unusable database ($restoredDb)" }

        # A safety copy of everything about to be replaced, so a mistaken restore
        # is undoable: this install's database, and the archive's target when that
        # is a different database that already exists.
        if (Test-Path -LiteralPath $EnvFile) {
            $current = $script:DbName
            foreach ($db in @($current, $restoredDb) | Select-Object -Unique) {
                $exists = ("" + (Invoke-AsSuper "psql" @("-d", "postgres", "-tAc",
                    "SELECT 1 FROM pg_database WHERE datname = '$db'"))).Trim()
                if ($exists -eq "1") {
                    Write-Host "  safety backup of database $db, which this restore replaces:"
                    $script:DbName = $db
                    Invoke-Backup "" | Out-Null
                }
            }
            $script:DbName = $current
        }
        if (Test-Path -LiteralPath $EnvFile) {
            $kept = "$EnvFile.backup-" + (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
            Copy-Item -LiteralPath $EnvFile -Destination $kept
            Protect-Path $kept
        }
        Copy-Item -LiteralPath (Join-Path $work "env") -Destination $EnvFile -Force
        Protect-Path $EnvFile
        $script:DbName = $restoredDb
        Write-Ok ".env restored (the previous one is kept as .env.backup-*)"

        # An empty database, the roles and extensions, then the data.
        Invoke-AsSuper "dropdb" @("--if-exists", $script:DbName) | Out-Null
        if ($script:LastToolExit -ne 0) { Stop-Backup "could not drop $($script:DbName)" }
        Invoke-AsSuper "createdb" @($script:DbName) | Out-Null
        if ($script:LastToolExit -ne 0) { Stop-Backup "could not create $($script:DbName)" }
        Invoke-AsSuper "psql" @("-d", $script:DbName, "-v", "ON_ERROR_STOP=1", "-q",
            "-f", (Join-Path $Root "postgres\provisioning\001_database_roles.sql")) | Out-Null
        if ($script:LastToolExit -ne 0) { Stop-Backup "could not provision the database roles" }
        Invoke-AsSuper "pg_restore" @("-d", $script:DbName, "--exit-on-error", $dump) | Out-Null
        if ($script:LastToolExit -ne 0) { Stop-Backup "pg_restore failed -- the safety backup can put the previous state back" }
        Write-Ok "database $($script:DbName) restored"
        Restore-DataFolders $work $replaced
        Update-UploadRows $work

        # The restored .env's password for the runtime role, and any migrations
        # this (possibly newer) checkout has that the backup predates. Passwords
        # reach psql on stdin, as a psql variable quoted by format(%L).
        function Set-RolePassword([string]$role, [string]$pw) {
            $q = ConvertTo-PsqlQuoted $pw
            $sql = "\set pw '$q'`nSELECT format('ALTER ROLE %I PASSWORD %L', '$role', :'pw')\gexec`n"
            Invoke-AsSuper "psql" @("-d", "postgres", "-q", "-f", "-") $sql | Out-Null
            return ($script:LastToolExit -eq 0)
        }
        if (-not (Set-RolePassword "ira_app" (Get-EnvValue "POSTGRES_PASSWORD"))) { Stop-Backup "could not set the ira_app password" }
        $b = New-Object 'byte[]' 24
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
        $mig = -join ($b | ForEach-Object { $_.ToString("x2") })
        if (-not (Set-RolePassword "ira_migrator" $mig)) { Stop-Backup "could not set the ira_migrator password" }
        $previous = $env:PGPASSWORD
        Push-Location (Join-Path $Root "ira")
        try {
            $env:PGPASSWORD = $mig
            & $py -m scripts.migrate apply --dsn "postgresql://ira_migrator@127.0.0.1:$($script:PgPort)/$($script:DbName)" *> $null
            $status = $LASTEXITCODE
        } finally {
            $env:PGPASSWORD = $previous
            Pop-Location
            Invoke-AsSuper "psql" @("-d", "postgres", "-q", "-f", "-") "ALTER ROLE ira_migrator PASSWORD NULL;`n" | Out-Null
        }
        if ($status -ne 0) { Stop-Backup "migrations after restore failed -- run scripts\setup-windows.ps1 to see why" }
        Write-Ok "schema current; start IRA with start-ira.ps1 and sign in with the restored password"
    } finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

switch ($Command) {
    "backup"  { Invoke-Backup $Dest | Out-Null }
    "restore" { Invoke-Restore $Archive ([bool]$Yes) }
    default   {
        Get-Content -LiteralPath $MyInvocation.MyCommand.Path -TotalCount 39 | Select-Object -Skip 1
        exit 2
    }
}

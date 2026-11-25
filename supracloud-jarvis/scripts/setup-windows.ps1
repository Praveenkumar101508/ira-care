<#
# ============================================================================
# L6 -- IRA one-time LOCAL setup for Windows (no Docker, no GPU required, private)
# ============================================================================
# Idempotent: safe to re-run. Does NOT touch the Linux scripts (setup.sh etc.),
# which stay for the future Docker/cloud path.
#
# What it does:
#   1. Checks prerequisites (Ollama, Python 3.11+, Node, PostgreSQL, Memurai)
#   2. Pulls the local chat model with Ollama
#   3. Creates the Postgres role + database and enables pgvector
#   4. Generates supracloud-jarvis/.env from .env.example with local values
#   5. Creates a Python venv + installs deps; installs frontend deps
#
# Usage (from the supracloud-jarvis/ folder):
#   powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
#   # optionally pass the Postgres superuser password:
#   ... -PgSuperPassword 'your_postgres_pw'
#
# NOTE: ASCII-only on purpose so it parses under Windows PowerShell 5.1.
# ============================================================================
#>
[CmdletBinding()]
param(
    # Password for the Postgres superuser ("postgres") used only to create the
    # app role/db. If omitted, you'll be prompted (or set $env:PGPASSWORD first).
    [string]$PgSuperPassword = $env:PGPASSWORD,
    [string]$PgSuperUser     = "postgres",
    [string]$OllamaModel     = "",                           # empty = pick one that fits this machine (scripts\detect_hardware.py)
    [string]$EmbeddingModel  = "BAAI/bge-large-en-v1.5",     # A7: sentence-transformers embedder (1024-dim)
    [string]$RerankerModel   = "BAAI/bge-reranker-v2-m3",    # A7: cross-encoder reranker
    [switch]$SkipDeps                                         # skip pip/npm install if set
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Resolve repo paths relative to this script (scripts/ lives under the project root)
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$EnvExample  = Join-Path $ProjectRoot ".env.example"
$EnvFile     = Join-Path $ProjectRoot ".env"
$FrontendDir = Join-Path $ProjectRoot "frontend"

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK] $msg"   -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  [!]  $msg"   -ForegroundColor Yellow }

function Test-Cmd($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

# Portable hex secret (works on Windows PowerShell 5.1 -- no [Convert]::ToHexString)
function New-Secret([int]$bytes = 32) {
    $b = New-Object 'byte[]' $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    return -join ($b | ForEach-Object { $_.ToString('x2') })
}

# -- 1. Prerequisite checks (install hints, non-fatal where possible) ---------
Write-Step "1. Checking prerequisites"

if (Test-Cmd ollama) { Write-Ok "Ollama found" }
else { Write-Warn "Ollama NOT found -- install from https://ollama.com/download/windows" }

if (Test-Cmd python) {
    $pyv = (python --version) 2>&1
    # 3.11 or 3.12 only: numpy 1.26.4 has no 3.13 wheel and psycopg-binary 3.2.3
    # has none for 3.14, so a newer interpreter fails partway through pip install.
    & python -c "import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 12) else 1)"
    if ($LASTEXITCODE -eq 0) { Write-Ok "Python found ($pyv)" }
    else { Write-Warn "Python found ($pyv) but IRA needs 3.11 or 3.12 -- install 3.12 from https://www.python.org/downloads/ (py -3.12)" }
} else { Write-Warn "Python NOT found -- install 3.12 from https://www.python.org/downloads/" }

if (Test-Cmd node) { Write-Ok "Node found ($(node --version))" }
else { Write-Warn "Node NOT found -- install LTS from https://nodejs.org/" }

if (Test-Cmd psql) { Write-Ok "PostgreSQL client (psql) found" }
else { Write-Warn "psql NOT found -- install PostgreSQL 17 from https://www.postgresql.org/download/windows/ (with pgvector)" }

# L5: Memurai is a Redis-compatible Windows service (drop-in for localhost:6379)
$memurai = Get-Service -Name "Memurai*" -ErrorAction SilentlyContinue
if ($memurai) { Write-Ok "Memurai service found ($($memurai.Status))" }
else { Write-Warn "Memurai (Redis for Windows) NOT found -- install from https://www.memurai.com/get-memurai  (IRA needs Redis at localhost:6379)" }

# -- 1b. Pick a model this machine can run ------------------------------------
# No GPU is required. detect_hardware.py reads RAM and any NVIDIA VRAM and names
# the largest qwen3 tag that fits; -OllamaModel overrides it.
$ReadTimeout = "600"   # CPU-only unless detection finds a GPU
if (Test-Cmd python) {
    try {
        $hw = (& python (Join-Path $ScriptDir "detect_hardware.py") --json) | ConvertFrom-Json
        if ($hw.accelerator -ne "cpu") { $ReadTimeout = "120" }
    } catch { }
}
if (-not $OllamaModel) {
    $OllamaModel = "qwen3:4b"
    if (Test-Cmd python) {
        $detected = (& python (Join-Path $ScriptDir "detect_hardware.py") --model) 2>$null
        if ($LASTEXITCODE -eq 0 -and $detected) { $OllamaModel = "$detected".Trim() }
        & python (Join-Path $ScriptDir "detect_hardware.py")
    }
}
Write-Ok "Chat model for this machine: $OllamaModel"

# -- 2. Pull the Ollama model -------------------------------------------------
Write-Step "2. Pulling Ollama model: $OllamaModel"
if (Test-Cmd ollama) {
    ollama pull $OllamaModel
    Write-Ok "Model $OllamaModel ready"
} else {
    Write-Warn "Skipping pull -- Ollama not installed. After installing run: ollama pull $OllamaModel"
}
# NOTE: embeddings stay on local sentence-transformers (BGE, 1024-dim) per L3 --
# no Ollama embedding model is pulled (that would change vector dims + break DB).

# -- 3. Database, separated roles, pgvector -----------------------------------
# The same model as CI and scripts/setup-native.sh: the provisioning script
# (postgres\provisioning\001_database_roles.sql) creates ira_schema_owner (owns
# every object), ira_migrator (applies migrations) and ira_app (serves requests,
# owns nothing), plus the extensions. The API runs as ira_app and never runs
# DDL, so migrations are applied in step 5b. The old 'jarvis' owner role let the
# running API switch off row-level security, and nothing applied migrations, so
# a fresh install had no tables and login refused with 503.
Write-Step "3. Setting up PostgreSQL (database 'jarvis_db', separated roles, pgvector)"
$PgReady = $false
if (Test-Cmd psql) {
    if (-not $PgSuperPassword) {
        $sec = Read-Host "Enter password for Postgres superuser '$PgSuperUser'" -AsSecureString
        $PgSuperPassword = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    }
    $env:PGPASSWORD = $PgSuperPassword
    $appPw = New-Secret 18   # generated app DB password, written into .env below
    # Re-running setup must not change the role's password underneath an
    # existing .env (step 4 leaves that file untouched), or the API can no
    # longer log in. Reuse the password the .env already holds.
    if (Test-Path $EnvFile) {
        $existing = Select-String -Path $EnvFile -Pattern '^POSTGRES_PASSWORD=(.+)$' | Select-Object -First 1
        if ($existing) { $appPw = $existing.Matches[0].Groups[1].Value.Trim() }
    }

    # Database first (CREATE DATABASE has no IF NOT EXISTS), then the roles and
    # extensions. Passwords go to psql on stdin, never on a command line.
    "SELECT 'CREATE DATABASE jarvis_db' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'jarvis_db')\gexec" |
        psql -h localhost -U $PgSuperUser -d postgres -v ON_ERROR_STOP=1 -q -f -
    psql -h localhost -U $PgSuperUser -d jarvis_db -v ON_ERROR_STOP=1 -q `
        -f (Join-Path $ProjectRoot "postgres\provisioning\001_database_roles.sql")
    if ($LASTEXITCODE -ne 0) {
        throw "Provisioning failed. If it mentions 'vector', install pgvector for your PostgreSQL version first."
    }
    $q = $appPw.Replace("'", "''")
    "ALTER ROLE ira_app PASSWORD '$q';" | psql -h localhost -U $PgSuperUser -d postgres -v ON_ERROR_STOP=1 -q -f -
    Write-Ok "Database ready: roles provisioned, pgvector enabled. App DB password saved to .env."
    $script:GeneratedDbPassword = $appPw
    $PgReady = $true
} else {
    # Generate the password NOW rather than writing a literal placeholder into
    # .env. The old "CHANGE_ME" fallback shipped a guessable password that the
    # app would happily start with if the user never came back to it.
    $pendingPw = New-Secret 18
    Write-Warn "Skipping DB setup -- psql not installed. Install PostgreSQL + pgvector, add its bin folder to PATH,"
    Write-Warn "then run this script again: it creates the database, roles and schema, and keeps this password."
    $script:GeneratedDbPassword = $pendingPw
}

# -- 4. Generate .env from .env.example with local values ---------------------
Write-Step "4. Generating .env (local / Ollama / no-Docker)"
if (Test-Path $EnvFile) {
    Write-Warn ".env already exists -- leaving it untouched. Delete it and re-run to regenerate."
} else {
    if (-not (Test-Path $EnvExample)) { throw ".env.example not found at $EnvExample" }
    # UTF-8: Windows PowerShell 5.1 would read it in the ANSI code page and
    # write the template's non-ASCII comments into .env garbled.
    $content = Get-Content $EnvExample -Raw -Encoding UTF8

    # Fill required (no-default) secrets and force local-mode values.
    $repl = [ordered]@{
        "POSTGRES_USER"      = "ira_app"         # the runtime role: owns nothing, no DDL
        "POSTGRES_PASSWORD"  = $script:GeneratedDbPassword
        "REDIS_PASSWORD"     = ""                # Memurai default: no auth
        "VLLM_API_KEY"       = "ollama"          # dummy -- unused when LLM_BACKEND=ollama
        "IRA_SECRET_KEY"     = (New-Secret 32)
        "IRA_ADMIN_PASSWORD" = (New-Secret 12)
    }
    foreach ($k in $repl.Keys) {
        $line = "$k=$($repl[$k])"
        if ($content -match "(?m)^$k=.*$") { $content = $content -replace "(?m)^$k=.*$", $line }
        else { $content += "`n$line" }
    }

    # .env.example ships every credential EMPTY (Phase 0). Anything that must not
    # be empty in a real .env is asserted here, so a substitution that stops
    # matching fails loudly instead of producing a passwordless install.
    foreach ($k in @("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD")) {
        if ($content -notmatch "(?m)^$k=.+$") {
            throw "$k is empty in the generated .env -- refusing to write it."
        }
    }

    # Append the L2/L4/L5 local-mode block (keys the example may not list).
    $localBlock = @"

# -- L6: local (no-Docker) overrides -- model sized to this machine, private --
LLM_BACKEND=ollama
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL_FAST=$OllamaModel
OLLAMA_MODEL_DEEP=$OllamaModel
OLLAMA_MODEL_REASONING=$OllamaModel
# The model router (IRA_USE_MODEL_ROUTER, on by default) resolves chat through
# config/model_profiles.yaml (qwen3:8b / qwen3:14b) unless these are set, and
# would otherwise ask Ollama for a model this machine never pulled.
IRA_LOCAL_FAST_MODEL=$OllamaModel
IRA_LOCAL_MAIN_MODEL=$OllamaModel
IRA_LOCAL_REASONING_MODEL=$OllamaModel
IRA_FALLBACK_TINY_MODEL=$OllamaModel
# .env.example's /backups is the Docker volume. Natively, keep database backups
# in the user's profile: writable, and outside the repository so a dump of
# personal data can never be committed by accident.
BACKUP_DIR=$($env:USERPROFILE)\.ira\backups
# Without a GPU a memory-augmented answer can outlast the 120 s default.
OLLAMA_READ_TIMEOUT_S=$ReadTimeout
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
REDIS_HOST=localhost
REDIS_PORT=6379
DEV_MODE=false
IRA_DOMAIN=localhost
# A7: accuracy levers OFF for the FIRST boot test (clean L1-L9 baseline, no new
# variables). The models are still pre-downloaded below, so flipping these to
# true after the boot test passes is instant. See ACCURACY_LAYER.md.
RERANKER_ENABLED=false
WEB_SEARCH_ENABLED=false
# Voice is OFF by default locally. If you enable voice, mint a real JWT and set:
# IRA_VOICE_API_TOKEN=<signed-jwt>   (see LOCAL_SETUP.md)
"@
    ($content.TrimEnd() + "`n" + $localBlock + "`n") | Set-Content -Path $EnvFile -Encoding utf8
    Write-Ok ".env written with generated secrets and LLM_BACKEND=ollama"
    Write-Warn "Admin password was randomly generated -- see IRA_ADMIN_PASSWORD in .env to log in."
}

# .env holds the database password, the signing key and the admin password.
# setup-native.sh creates it 0600 on Linux and macOS; here the file inherits the
# folder's ACL, which on a shared PC lets other accounts read it. Drop the
# inherited entries and grant only this account and SYSTEM, whether the file was
# written just now or left in place from an earlier run.
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls "$EnvFile" /inheritance:r /grant:r "${me}:(R,W)" /grant:r "SYSTEM:(F)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw ".env could not be restricted to this account (icacls exit $LASTEXITCODE)" }
Write-Ok ".env readable by $me and SYSTEM only"

# -- 5. Python + frontend dependencies ----------------------------------------
if (-not $SkipDeps) {
    Write-Step "5. Installing Python dependencies (venv)"
    if (Test-Cmd python) {
        $venv = Join-Path $ProjectRoot ".venv"
        if (-not (Test-Path $venv)) { python -m venv $venv }
        # The repository's .gitignore does not list .venv; keep it out of git
        # the way Python 3.13's venv module does by default.
        $venvIgnore = Join-Path $venv ".gitignore"
        if (-not (Test-Path $venvIgnore)) { Set-Content -Path $venvIgnore -Value "*" -Encoding ascii }
        $pip = Join-Path $venv "Scripts\pip.exe"
        & $pip install --upgrade pip
        & $pip install -r (Join-Path $ProjectRoot "ira\requirements.txt")
        # faster-whisper without PyAV (GPL encoders inside its FFmpeg); see that file.
        & $pip install --no-deps --require-hashes -r (Join-Path $ProjectRoot "ira\requirements-nodeps.txt")
        Write-Ok "Python deps installed into .venv"

        # A7: pre-download the CPU models so the first chat doesn't stall on a
        # surprise download. Embedder is always used; reranker is pre-cached even
        # though it's OFF for the first boot, so enabling it later is instant.
        Write-Step "5b. Pre-downloading embedding + reranker models (CPU, one-time)"
        $py = Join-Path $venv "Scripts\python.exe"
        # At their pinned revisions (ira\core\models\artefacts.py), the bytes
        # the app loads. A native command that exits non-zero does not throw, so
        # the exit code decides what is reported, not try/catch.
        $prefetch = Join-Path $ProjectRoot "ira\scripts\prefetch_models.py"
        & $py $prefetch --embedder $EmbeddingModel --reranker $RerankerModel
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Embedder ($EmbeddingModel) and reranker ($RerankerModel) cached"
        } else {
            Write-Warn "Model pre-download failed (they download, pinned, on first use instead) -- see the lines above"
        }
    } else { Write-Warn "Skipping pip -- Python not installed." }

    # -- 5b. Schema: migrations, applied as ira_migrator ----------------------
    # A single-use migrator password: set for this run, cleared afterwards, so
    # no stored credential anywhere can change the schema. Re-running setup
    # after updating IRA applies the new migrations here.
    if ($PgReady -and (Get-Variable venv -ErrorAction SilentlyContinue)) {
        Write-Step "5b. Applying database migrations (as ira_migrator)"
        $migPw = New-Secret 24
        "ALTER ROLE ira_migrator PASSWORD '$migPw';" | psql -h localhost -U $PgSuperUser -d postgres -v ON_ERROR_STOP=1 -q -f -
        $py = Join-Path $venv "Scripts\python.exe"
        Push-Location (Join-Path $ProjectRoot "ira")
        $env:PGPASSWORD = $migPw
        & $py -m scripts.migrate apply --dsn "postgresql://ira_migrator@127.0.0.1:5432/jarvis_db"
        $migExit = $LASTEXITCODE
        $env:PGPASSWORD = $PgSuperPassword
        Pop-Location
        "ALTER ROLE ira_migrator PASSWORD NULL;" | psql -h localhost -U $PgSuperUser -d postgres -v ON_ERROR_STOP=1 -q -f -
        if ($migExit -ne 0) { throw "Migrations failed -- see the output above." }
        Write-Ok "Database schema is current"
    }

    Write-Step "5c. Installing frontend dependencies"
    if ((Test-Cmd npm) -and (Test-Path $FrontendDir)) {
        Push-Location $FrontendDir
        npm install
        # start-ira.ps1 serves the production build; without one `next start`
        # refuses to run. The API proxy rules are fixed at build time.
        $env:IRA_API_INTERNAL_URL = "http://127.0.0.1:8000"
        npm run build
        Pop-Location
        Write-Ok "Frontend deps installed and production build created"
    } else { Write-Warn "Skipping npm install -- npm or frontend/ missing." }
} else {
    Write-Warn "Skipping dependency install (-SkipDeps)."
}

# -- Next steps ---------------------------------------------------------------
Write-Step "Setup complete -- next steps"
Write-Host @"
  1. Make sure these are running:
       - Ollama          (tray app or 'ollama serve')
       - PostgreSQL 17    (Windows service)
       - Memurai          (Redis-compatible Windows service, localhost:6379)
  2. Start everything (the same command INSTALL.md gives):
       powershell -ExecutionPolicy Bypass -File start-ira.ps1      # or double-click "Start IRA.bat"
  3. Open the app:  http://localhost:3000   (API at http://localhost:8000)

  Your admin login is in .env (IRA_ADMIN_USERNAME / IRA_ADMIN_PASSWORD).
"@ -ForegroundColor White

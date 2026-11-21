<#
.SYNOPSIS
  Bring up the full IRA native stack on Windows (the dev workstation) in dependency order,
  checking each service is healthy. Replaces docker-compose / `make up` (there is
  no docker-compose.yml; the stack runs native on Windows per AGENTS.md).

.DESCRIPTION
  Order: Postgres -> Memurai (Redis) -> Ollama (pull + warm fast/deep) -> Cortex
         (cortex -z one-shot check) -> Supertonic TTS warm -> web-research
         (SearXNG / Crawl4AI) -> IRA backend (uvicorn) -> IRA frontend (Next).
  Each service is started only if it isn't already up, then probed until healthy.
  SearXNG / Crawl4AI and Supertonic are optional (fail soft) -- a miss is WARN, not
  fatal. Prints a per-service status line and a final "ALL UP" or the first failure.

  Autostart options: -InstallAutostart drops a per-user *logon* launcher (no admin);
  for an unattended box use -InstallService, which registers a Task Scheduler task
  triggered AtStartup running as the current user via S4U ("run whether or not a user
  is logged on") so the stack survives reboots with nobody logged in. Local only --
  binds to localhost / the Tailscale interface, never the public internet.

.USAGE
  pwsh -File .\start-ira.ps1                   # bring everything up
  pwsh -File .\start-ira.ps1 -SkipFrontend     # backend only
  pwsh -File .\start-ira.ps1 -InstallAutostart # also drop a login autostart launcher
  pwsh -File .\start-ira.ps1 -InstallService   # register a BOOT task (self-elevates), then exit
  Loads .env from this folder if present.
#>
[CmdletBinding()]
param(
    [switch]$SkipFrontend,
    [switch]$InstallAutostart,
    [switch]$InstallService,
    [int]$ReadyTimeoutSec = 90
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$iraDir = Join-Path $root "ira"
$frontendDir = Join-Path $root "frontend"

# scripts\setup-windows.ps1 installs the backend's dependencies into .venv, so
# the backend must run on that interpreter. A bare "python" is whichever one is
# first on PATH, which normally has none of them, and uvicorn never starts.
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }

# -- .env loader ---------------------------------------------------------------
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    # Same rules as the backend's own reader (python-dotenv): a quoted value is
    # taken verbatim, and in an unquoted one " #" starts a comment. .env.example
    # documents most settings with inline comments; exporting them as part of
    # the value made the backend refuse to start ("false   # true ..." is not a
    # boolean), and these process variables override the .env file it reads.
    #
    # An empty value (KEY= or KEY="") leaves the variable unset, on every
    # version. Windows PowerShell 5.1 and PowerShell 7.4 cannot hold an empty
    # variable (setting one to "" deletes it) and 7.5 can, so without this the
    # result depended on which PowerShell ran the launcher. Unset is safe: the
    # backend's config.py loads this same file with override=False, which gives
    # every unset key its empty value back. Removing also drops a value inherited
    # from the parent environment, so the .env file still wins over it. Not
    # SetEnvironmentVariable($name, $null): PowerShell passes that $null as "",
    # which 7.5 keeps.
    #
    # Read as UTF-8, as python-dotenv does. Windows PowerShell 5.1 (what
    # Start IRA.bat runs) otherwise reads a file without a byte-order mark in
    # the ANSI code page, so a non-ASCII value such as a name or a profile path
    # came out garbled, and these process variables won over the backend's own
    # correct reading. A byte-order mark, which 5.1's setup writes, is skipped.
    Get-Content $envFile -Encoding UTF8 | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            $name = $matches[1]; $raw = $matches[2].Trim()
            if ($raw -match '^"([^"]*)"') { $val = $matches[1] }
            elseif ($raw -match "^'([^']*)'") { $val = $matches[1] }
            else { $val = ($raw -replace '(^|\s+)#.*$', '').Trim() }
            if ($val -ne "") { [Environment]::SetEnvironmentVariable($name, $val, "Process") }
            elseif (Test-Path -LiteralPath "Env:$name") { Remove-Item -LiteralPath "Env:$name" }
        }
    }
    Write-Host "Loaded .env" -ForegroundColor DarkGray
}

function Get-EnvOr([string]$name, [string]$default) {
    $v = [Environment]::GetEnvironmentVariable($name, "Process")
    if ([string]::IsNullOrWhiteSpace($v)) { return $default } else { return $v }
}

# ONNX Runtime (voice transcription, the wake word) reports usage to Microsoft
# as soon as it is imported unless this is set before it loads. Set here, after
# .env, so the backend and the worker inherit it; a value set in .env is kept,
# a blank one is not (ira/core/runtime/vendor_telemetry.py).
if (-not (Get-EnvOr "ORT_DISABLE_TELEMETRY" "")) { [Environment]::SetEnvironmentVariable("ORT_DISABLE_TELEMETRY", "1", "Process") }

# Resolve the cortex executable the same way ira/cortex_bridge.py does:
# IRA_CORTEX_BIN -> PATH -> the known native install. Returns $null if absent.
function Resolve-CortexBin {
    $envBin = [Environment]::GetEnvironmentVariable("IRA_CORTEX_BIN", "Process")
    if ($envBin -and (Test-Path $envBin)) { return $envBin }
    $cmd = Get-Command "cortex" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $local = $env:LOCALAPPDATA
    if ($local) {
        $cand = Join-Path $local "hermes\hermes-agent\venv\Scripts\hermes.exe"
        if (Test-Path $cand) { return $cand }
    }
    return $null
}

# Smoke-test the `cortex -z` subprocess seam (the bridge's actual call path --
# Cortex 0.15.2 has no HTTP gateway). Bounded by $timeoutSec; OK = exit 0 + output.
function Test-CortexOneShot([string]$bin, [int]$timeoutSec) {
    $out = [System.IO.Path]::GetTempFileName()
    $err = [System.IO.Path]::GetTempFileName()
    try {
        $p = Start-Process -FilePath $bin `
            -ArgumentList "--accept-hooks","-z","Reply with the single word: ready" `
            -NoNewWindow -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
        if (-not $p.WaitForExit($timeoutSec * 1000)) { try { $p.Kill() } catch {}; return $false }
        $text = Get-Content $out -Raw -ErrorAction SilentlyContinue
        return ($p.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($text))
    } catch { return $false }
    finally { Remove-Item $out, $err -ErrorAction SilentlyContinue }
}

# -- probes --------------------------------------------------------------------
function Test-Tcp([string]$h, [int]$port) {
    try { (Test-NetConnection -ComputerName $h -Port $port -WarningAction SilentlyContinue).TcpTestSucceeded }
    catch { $false }
}

function Test-Http([string]$url, [hashtable]$headers = @{}) {
    try {
        $r = Invoke-WebRequest -Uri $url -Headers $headers -TimeoutSec 5 -UseBasicParsing
        return $r.StatusCode -lt 500
    } catch { return $false }
}

function Wait-Until([scriptblock]$check, [int]$timeoutSec) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (& $check) { return $true }
        Start-Sleep -Milliseconds 800
    }
    return $false
}

$results = [ordered]@{}
function Set-Status([string]$name, [string]$state, [string]$detail = "") {
    $results[$name] = @{ state = $state; detail = $detail }
    $color = switch ($state) { "OK" { "Green" } "WARN" { "Yellow" } default { "Red" } }
    Write-Host ("  [{0,-4}] {1,-12} {2}" -f $state, $name, $detail) -ForegroundColor $color
}

# -- Optional: install a login autostart launcher (no admin needed) --------------
# Mirrors the Cortex VBS trick: a hidden .vbs in the Startup folder runs this very
# script at logon, so the whole stack comes up without anyone touching a terminal.
if ($InstallAutostart) {
    $startupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
    $vbsPath = Join-Path $startupDir "ira-stack.vbs"
    $self = $PSCommandPath
    $vbs = @"
' Auto-generated by start-ira.ps1 -InstallAutostart -- boots the IRA stack hidden at logon.
Set sh = CreateObject("WScript.Shell")
sh.Run "pwsh -NoProfile -ExecutionPolicy Bypass -File ""$self""", 0, False
"@
    Set-Content -Path $vbsPath -Value $vbs -Encoding ASCII
    Write-Host "Installed login autostart: $vbsPath" -ForegroundColor Green
}

# -- Optional: install a BOOT-level task (admin; runs with NO login) -------------
# Beyond -InstallAutostart (which only fires at *logon*), this registers a Task
# Scheduler task triggered AtStartup, running as the current user via S4U ("run
# whether or not a user is logged on"), so the stack survives reboots unattended --
# readying the box for 24/7 access over Tailscale. Local only; no public exposure.
# Registering an AtStartup task needs elevation, so self-elevate (UAC) if necessary.
# This is install-and-exit: it does NOT also start the stack now (run the script
# plainly, or just reboot, for that).
if ($InstallService) {
    $taskName = "IRA-Stack-Boot"
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)

    if (-not $isAdmin) {
        Write-Host "Registering a boot task needs admin -- requesting elevation (UAC)..." -ForegroundColor Yellow
        try {
            $relaunch = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -InstallService"
            Start-Process pwsh -Verb RunAs -Wait -ArgumentList $relaunch
            Write-Host "Boot task '$taskName' installed (elevated). The stack will come up at next boot." -ForegroundColor Green
        } catch {
            Write-Host "Elevation declined/failed -- open an elevated PowerShell and re-run with -InstallService." -ForegroundColor Red
            exit 1
        }
        exit 0
    }

    # Already elevated: register (idempotently) the AtStartup task.
    $pwshExe = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
    if (-not $pwshExe) { $pwshExe = (Get-Command powershell -ErrorAction SilentlyContinue).Source }
    $action  = New-ScheduledTaskAction -Execute $pwshExe `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = "PT30S"   # let drivers / network / Postgres / Memurai settle first
    $userId  = "$env:USERDOMAIN\$env:USERNAME"
    # S4U = run whether or not the user is logged on, WITHOUT storing a password.
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType S4U -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)   # no run-time limit (long-lived stack)
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings `
        -Description "Bring up the IRA native stack at boot (no login required). Local only." | Out-Null
    Write-Host "Installed boot task '$taskName' -- runs at startup as $userId (S4U), no login needed." -ForegroundColor Green
    Write-Host "  Note: a boot/S4U task uses the machine PATH; if ollama/python/npm/pwsh are per-user-only," -ForegroundColor DarkGray
    Write-Host "        add them to the system PATH (or set full paths in .env) so the task can find them." -ForegroundColor DarkGray
    Write-Host "  Remove with (elevated):  Unregister-ScheduledTask -TaskName $taskName -Confirm:`$false" -ForegroundColor DarkGray
    exit 0
}

Write-Host "Starting IRA native stack..." -ForegroundColor Cyan

# -- 1. Postgres (native service) ----------------------------------------------
$pgPort = [int](Get-EnvOr "POSTGRES_PORT" "5432")
if (-not (Test-Tcp "localhost" $pgPort)) {
    Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue | Start-Service -ErrorAction SilentlyContinue
}
if (Wait-Until { Test-Tcp "localhost" $pgPort } $ReadyTimeoutSec) { Set-Status "postgres" "OK" "port $pgPort" }
else { Set-Status "postgres" "FAIL" "not reachable on $pgPort" }

# -- 2. Memurai (Redis for Windows) --------------------------------------------
$redisPort = [int](Get-EnvOr "REDIS_PORT" "6379")
if (-not (Test-Tcp "localhost" $redisPort)) {
    Get-Service -Name "Memurai" -ErrorAction SilentlyContinue | Start-Service -ErrorAction SilentlyContinue
}
if (Wait-Until { Test-Tcp "localhost" $redisPort } $ReadyTimeoutSec) { Set-Status "redis" "OK" "port $redisPort (Memurai)" }
else { Set-Status "redis" "FAIL" "not reachable on $redisPort" }

# -- 3. Ollama (serve, then ensure the fast/deep models are pulled and warm) -----
$ollamaBase = (Get-EnvOr "OLLAMA_BASE_URL" "http://localhost:11434/v1") -replace "/v1$",""
$keepAlive  = Get-EnvOr "OLLAMA_KEEP_ALIVE" "30m"
$fastModel  = Get-EnvOr "OLLAMA_MODEL_FAST" "qwen3:8b"     # voice / low-latency tier
$deepModel  = Get-EnvOr "OLLAMA_MODEL_DEEP" "qwen3:14b"    # deep / reasoning tier
if (-not (Test-Http "$ollamaBase/api/tags")) {
    # KV-cache quant + flash attention so qwen3:8b and qwen3:14b both fit warm on the
    # 20GB A4500; keep_alive keeps them resident between turns (faster TTFT).
    [Environment]::SetEnvironmentVariable("OLLAMA_KEEP_ALIVE", $keepAlive, "Process")
    if (-not (Get-EnvOr "OLLAMA_FLASH_ATTENTION" "")) { [Environment]::SetEnvironmentVariable("OLLAMA_FLASH_ATTENTION", "1", "Process") }
    if (-not (Get-EnvOr "OLLAMA_KV_CACHE_TYPE" "")) { [Environment]::SetEnvironmentVariable("OLLAMA_KV_CACHE_TYPE", "q8_0", "Process") }
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden -ErrorAction SilentlyContinue
}
if (Wait-Until { Test-Http "$ollamaBase/api/tags" } $ReadyTimeoutSec) {
    # Pull each model if missing, then warm it (load into VRAM, pinned by keep_alive).
    # Each model once: setup gives both tiers the same model.
    $warmModels = @($fastModel)
    if ($deepModel -ne $fastModel) { $warmModels += $deepModel }
    $tags = ""
    try { $tags = (& ollama list 2>$null | Out-String) } catch {}
    $notWarm = @()
    foreach ($m in $warmModels) {
        if ($tags -notmatch [regex]::Escape($m)) {
            Write-Host "  ollama pull $m ..." -ForegroundColor DarkGray
            & ollama pull $m 2>$null
        }
        # A first load after a reboot can take minutes without a GPU, and
        # Ollama aborts a load when its client hangs up: the old 120 s timeout
        # cancelled it, and the empty catch hid that. 1800 s is far above a
        # cold load; a failure is reported below. No prompt: Ollama then only
        # loads the model ("done_reason": "load") and writes no reply.
        Write-Host "  warming $m (a first load can take minutes without a GPU) ..." -ForegroundColor DarkGray
        try {
            $body = @{ model = $m; stream = $false; keep_alive = $keepAlive } | ConvertTo-Json
            Invoke-RestMethod -Method Post -Uri "$ollamaBase/api/generate" -Body $body -ContentType "application/json" -TimeoutSec 1800 | Out-Null
        } catch {
            $why = if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $_.ErrorDetails.Message } else { $_.Exception.Message }
            $notWarm += "could not warm $m -- $why"
        }
    }
    $warmWord = if ($notWarm.Count -eq 0) { " warm" } else { "" }
    Set-Status "ollama" "OK" "$ollamaBase ($fastModel + $deepModel$warmWord, keep_alive=$keepAlive)"
    foreach ($w in $notWarm) { Set-Status "ollama" "WARN" $w }
}
else { Set-Status "ollama" "FAIL" "no response at $ollamaBase" }

# -- 4. Cortex engine (cortex -z one-shot -- 0.15.2 ships NO :8642 HTTP gateway) --
# The bridge reaches Cortex via `cortex -z`, not an HTTP port (AGENTS.md verified
# 0.15.2 has no key-gated OpenAI gateway). So we smoke-test that subprocess seam.
$useCortex = (Get-EnvOr "IRA_USE_CORTEX" "false").ToLower()
$cortexBin = Resolve-CortexBin
if ($useCortex -in @("1","true","yes","on")) {
    if (-not $cortexBin) {
        Set-Status "cortex" "FAIL" "IRA_USE_CORTEX on but 'cortex' not found (set IRA_CORTEX_BIN)"
    } elseif (Test-CortexOneShot $cortexBin $ReadyTimeoutSec) {
        Set-Status "cortex" "OK" "cortex -z responded ($cortexBin)"
    } else {
        Set-Status "cortex" "FAIL" "cortex -z smoke failed/timed out ($cortexBin)"
    }
} else {
    Set-Status "cortex" "WARN" "IRA_USE_CORTEX not enabled (Ollama-direct engine)"
}

# -- 5. Supertonic TTS (verify the on-device voice engine is importable) ---------
# The API also pre-warms Supertonic on startup; this just surfaces a missing install
# now. Voice degrades gracefully (/voice/say -> 503), so a miss is WARN, not fatal.
try {
    & $python -c "import supertonic" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Set-Status "supertonic" "OK" "import supertonic (TTS ready)" }
    else { Set-Status "supertonic" "WARN" "supertonic not installed - /voice/say answers 503 (weights OpenRAIL-M, awaiting licence decision L-06)" }
} catch { Set-Status "supertonic" "WARN" "could not run python to check supertonic" }

# -- 6. Web-research backends (optional -- research fails soft) ------------------
$searxng = (Get-EnvOr "SEARXNG_URL" "http://localhost:8888").TrimEnd("/")
if (Test-Http "$searxng/" ) { Set-Status "searxng" "OK" $searxng }
else { Set-Status "searxng" "WARN" "down ($searxng) -- web search disabled, research fails soft" }

$crawl4ai = (Get-EnvOr "CRAWL4AI_URL" "http://localhost:11235").TrimEnd("/")
if (Test-Http "$crawl4ai/health") { Set-Status "crawl4ai" "OK" $crawl4ai }
else { Set-Status "crawl4ai" "WARN" "down ($crawl4ai) -- web reader disabled, research fails soft" }

# -- 7. IRA backend (uvicorn) --------------------------------------------------
$apiPort = [int](Get-EnvOr "IRA_API_PORT" "8000")
if (-not (Test-Http "http://127.0.0.1:$apiPort/health")) {
    Start-Process -FilePath $python `
        -ArgumentList "-m","uvicorn","main:app","--host","127.0.0.1","--port","$apiPort" `
        -WorkingDirectory $iraDir -WindowStyle Hidden -ErrorAction SilentlyContinue
}
if (Wait-Until { Test-Http "http://127.0.0.1:$apiPort/health" } $ReadyTimeoutSec) { Set-Status "ira-api" "OK" "http://127.0.0.1:$apiPort" }
else { Set-Status "ira-api" "FAIL" "no /health on $apiPort" }

# -- 8. IRA frontend (Next) ----------------------------------------------------
if (-not $SkipFrontend) {
    $fePort = [int](Get-EnvOr "FRONTEND_PORT" "3000")
    if (-not (Test-Tcp "127.0.0.1" $fePort)) {
        # Loopback only, like the API. Plain `next start` listens on every
        # interface, and its /api and /auth rewrites would then hand the API to
        # anyone on the network over plain HTTP. Phones and tablets reach IRA
        # through `tailscale serve` (HTTPS), which proxies to 127.0.0.1 --
        # see TAILSCALE_SETUP.md.
        # Server-side rendering calls the API directly; natively that is here.
        if (-not $env:IRA_API_INTERNAL_URL) { $env:IRA_API_INTERNAL_URL = "http://127.0.0.1:$apiPort" }
        # `next start` refuses to run without a production build (setup-windows.ps1
        # makes one); fall back to the dev server rather than fail, as start-ira.sh does.
        $feMode = if (Test-Path (Join-Path $frontendDir ".next\BUILD_ID")) { "start" } else { "dev" }
        if ($feMode -eq "dev") { Set-Status "frontend" "WARN" "no production build -- starting the dev server; run 'npm run build' in frontend\" }
        Start-Process -FilePath "npm" -ArgumentList "run",$feMode,"--","-H","127.0.0.1","-p","$fePort" `
            -WorkingDirectory $frontendDir -WindowStyle Hidden -ErrorAction SilentlyContinue
    }
    if (Wait-Until { Test-Tcp "127.0.0.1" $fePort } $ReadyTimeoutSec) { Set-Status "frontend" "OK" "http://localhost:$fePort" }
    else { Set-Status "frontend" "FAIL" "not reachable on $fePort" }
} else {
    Set-Status "frontend" "WARN" "skipped (-SkipFrontend)"
}

# -- Summary -------------------------------------------------------------------
Write-Host ""
$failed = $results.GetEnumerator() | Where-Object { $_.Value.state -eq "FAIL" }
if ($failed) {
    Write-Host ("STACK NOT FULLY UP -- failed: " + ($failed.Name -join ", ")) -ForegroundColor Red
    exit 1
} else {
    Write-Host "ALL UP. IRA is ready, boss." -ForegroundColor Green
    if (-not $SkipFrontend) {
        # Open the dashboard in the default browser (best effort, never fatal).
        $uiUrl = "http://localhost:$([int](Get-EnvOr 'FRONTEND_PORT' '3000'))"
        Write-Host "  UI: $uiUrl"
        try { Start-Process $uiUrl -ErrorAction SilentlyContinue } catch {}
    }
    exit 0
}

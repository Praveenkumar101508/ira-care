"""stop-ira.ps1 and verify-native-install.ps1: the Windows stop script and install check.

Windows had no stop script (INSTALL.md said so twice) and no install check, so a
Windows run could not be recorded the way a Linux or macOS one is. Neither script
has run on Windows. These tests run them under PowerShell 7 on Linux:

* Get-NetTCPConnection, Get-CimInstance, Stop-Process and Read-Host are replaced
  by functions of the same name, which PowerShell runs in place of the cmdlets.
  Nothing on this machine is ever stopped.
* The documented steps (setup, start, stop, uninstall, the smoke test) are stubs,
  and `powershell` is a stub that runs `pwsh`.

What they keep:

* stop-ira.ps1 stops only the API and the web app started from its own folder,
  with the processes they started. It never stops another program's python or
  node, PostgreSQL, Memurai or Ollama, and never stops anything by name. It exits
  0 when nothing was running, and -DryRun stops nothing.
* verify-native-install.ps1 runs INSTALL.md's commands exactly. It never opens
  .env, blanks secret values as verify-native-install.sh does, shows the home
  folder as "~", gives the PostgreSQL password to setup alone, runs the
  uninstaller as a dry run only, and records "cannot list the sockets" as a FAIL,
  not as "nothing listening".
* INSTALL.md no longer says that Windows has no stop script.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2]          # supracloud-jarvis/
STOP_PS1 = APP_ROOT / "stop-ira.ps1"
VERIFY_PS1 = APP_ROOT / "scripts" / "verify-native-install.ps1"
VERIFY_SH = APP_ROOT / "scripts" / "verify-native-install.sh"
INSTALL_MD = APP_ROOT / "INSTALL.md"
SCRIPTS = {"stop-ira.ps1": STOP_PS1, "verify-native-install.ps1": VERIFY_PS1}


def _pwsh_or_skip() -> str:
    found = shutil.which("pwsh")
    if not found:
        pytest.skip("pwsh is not installed here")
    return found


@pytest.fixture
def pwsh() -> str:
    """PowerShell 7, on a system where the stand-in programs below can run."""
    found = _pwsh_or_skip()
    if os.name == "nt":
        pytest.skip("the stand-in programs below are POSIX scripts")
    return found


def _code(text: str) -> str:
    """The script without its comments. Neither script has a multi-line string
    or a block comment, so a line-by-line scan that respects quotes is enough."""
    lines = []
    for line in text.splitlines():
        kept, quote, i = [], None, 0
        while i < len(line):
            ch = line[i]
            if quote:
                kept.append(ch)
                if quote == '"' and ch == "`" and i + 1 < len(line):
                    kept.append(line[i + 1])
                    i += 1
                elif ch == quote:
                    quote = None
            elif ch == "#":
                break
            else:
                if ch in "'\"":
                    quote = ch
                kept.append(ch)
            i += 1
        lines.append("".join(kept))
    return "\n".join(lines)


# ── both scripts: they must parse under the PowerShell every Windows PC has ──

@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_windows_scripts_parse_under_pwsh(name: str) -> None:
    shell = _pwsh_or_skip()
    probe = ("$e = $null; $t = $null; "
             f"[System.Management.Automation.Language.Parser]::ParseFile('{SCRIPTS[name]}', [ref]$t, [ref]$e) | Out-Null; "
             "$e | ForEach-Object { $_.ToString() }; exit $e.Count")
    run = subprocess.run([shell, "-NoProfile", "-Command", probe], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_windows_scripts_use_no_powershell_7_only_syntax(name: str) -> None:
    """Windows PowerShell 5.1, which every Windows 10 and 11 PC has, cannot parse
    `a ? b : c`, `a && b`, `a ?? b`, `$x ??= y` or `$x?.y`. PowerShell 7 can, so
    parsing under pwsh alone would not catch them."""
    shell = _pwsh_or_skip()
    probe = (
        "$e = $null; $t = $null; "
        f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{SCRIPTS[name]}', [ref]$t, [ref]$e); "
        "$L = 'System.Management.Automation.Language.'; "
        "$bad = $ast.FindAll({ param($n) "
        "  $n.GetType().FullName -in @(($L + 'TernaryExpressionAst'), ($L + 'PipelineChainAst')) -or "
        "  ($n -is [System.Management.Automation.Language.BinaryExpressionAst] -and $n.Operator -eq 'QuestionQuestion') -or "
        "  ($n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Operator -eq 'QuestionQuestionEquals') -or "
        "  ($n -is [System.Management.Automation.Language.MemberExpressionAst] -and $n.NullConditional) -or "
        "  ($n -is [System.Management.Automation.Language.IndexExpressionAst] -and $n.NullConditional) }, $true); "
        "$bad | ForEach-Object { 'line ' + $_.Extent.StartLineNumber + ': ' + $_.Extent.Text }; exit @($bad).Count")
    run = subprocess.run([shell, "-NoProfile", "-Command", probe], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, "PowerShell 7-only syntax:\n" + run.stdout + run.stderr
    # Parameters and variables that exist only in PowerShell 6 and later.
    code = _code(SCRIPTS[name].read_text(encoding="utf-8"))
    for later in ("-AsHashtable", "utf8NoBOM", "-AsByteStream", "-SkipCertificateCheck", "-Parallel",
                  "$IsWindows", "$IsLinux", "$IsMacOS", "-AdditionalChildPath"):
        assert later not in code, f"{name} uses {later}, which Windows PowerShell 5.1 does not have"


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_windows_scripts_are_ascii_only(name: str) -> None:
    """Windows PowerShell 5.1 reads a script without a byte-order mark in the ANSI
    code page; an em dash's last UTF-8 byte then becomes a quote character."""
    bad = sorted({chr(b) for b in SCRIPTS[name].read_bytes() if b > 127})
    assert not bad, f"{name} has non-ASCII bytes; PowerShell 5.1 misreads them"


# ── stop-ira.ps1 ─────────────────────────────────────────────────────────────

def test_stop_ps1_never_stops_by_name() -> None:
    """Ending every python or node on the PC would stop the owner's other work.
    The script stops what it identified, by process ID, and nothing else."""
    code = _code(STOP_PS1.read_text(encoding="utf-8"))
    assert not re.search(r"Stop-Process\b[^\n]*-(Name|ProcessName)\b", code, re.I), "Stop-Process by name"
    assert not re.search(r"\btaskkill\b", code, re.I), "taskkill (with /IM it kills by name)"
    assert not re.search(r"\b(wmic|Invoke-CimMethod|Terminate)\b", code, re.I), "a stop through WMI"
    assert not re.search(r"\b(kill|spps)\b", code, re.I), "an alias of Stop-Process"
    assert not re.search(r"\|\s*Stop-Process\b", code, re.I), "processes piped into Stop-Process"
    stops = [ln for ln in code.splitlines() if re.search(r"\bStop-Process\b", ln, re.I)]
    assert stops, "stop-ira.ps1 no longer calls Stop-Process"
    for ln in stops:
        assert re.search(r"Stop-Process\s+-Id\s+\$id\b", ln), f"Stop-Process without a recorded ID: {ln.strip()}"


STOP_HARNESS = r"""
param([string]$Script, [string]$Fixture, [string]$StoppedFile, [switch]$DryRun)
$global:Fx = Get-Content -LiteralPath $Fixture -Raw | ConvertFrom-Json
$global:Stopped = @()
function global:Get-NetTCPConnection {
    [CmdletBinding()] param([string]$State)
    foreach ($l in @($global:Fx.listeners)) {
        if ($global:Stopped -notcontains [int]$l.owner) {
            [pscustomobject]@{ LocalAddress = $l.address; LocalPort = [int]$l.port; OwningProcess = [int]$l.owner; State = "Listen" }
        }
    }
}
function global:Get-CimInstance {
    [CmdletBinding()] param([string]$ClassName, [string]$Filter)
    foreach ($p in @($global:Fx.processes)) {
        if ($global:Stopped -notcontains [int]$p.id) {
            [pscustomobject]@{ ProcessId = [int]$p.id; ParentProcessId = [int]$p.parent; Name = $p.name
                               ExecutablePath = $p.exe; CommandLine = $p.cmd; CreationDate = [datetime]$p.created }
        }
    }
}
function global:Stop-Process {
    [CmdletBinding()] param([int[]]$Id, [switch]$Force)
    foreach ($i in $Id) { $global:Stopped += $i; Add-Content -LiteralPath $StoppedFile -Value $i }
}
if ($DryRun) { & $Script -DryRun -WaitSec 1 } else { & $Script -WaitSec 1 }
exit $LASTEXITCODE
"""


def _proc(pid: int, parent: int, name: str, exe: str | None, cmd: str | None, second: int) -> dict:
    return {"id": pid, "parent": parent, "name": name, "exe": exe, "cmd": cmd,
            "created": f"2026-10-24T10:00:{second:02d}"}


def _ira_stack(root: str, api_port: int = 8000, web_port: int = 3000) -> tuple[list[dict], list[dict]]:
    """The processes start-ira.ps1 leaves on Windows, as Win32_Process shows them,
    plus the shared services and an unrelated python. Paths use backslashes, as
    Windows prints them; the script's folder is this test's Linux folder."""
    w = root.replace("/", "\\")
    venv_py = rf"{w}\.venv\Scripts\python.exe"
    uvicorn = f"-m uvicorn main:app --host 127.0.0.1 --port {api_port}"
    processes = [
        _proc(50, 1, "powershell.exe", r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
              "powershell -ExecutionPolicy Bypass -File start-ira.ps1", 0),
        # The venv's python.exe starts the real interpreter, which holds the port.
        _proc(100, 50, "python.exe", venv_py, f'"{venv_py}" {uvicorn}', 1),
        _proc(101, 100, "python.exe", r"C:\Python312\python.exe", rf'"C:\Python312\python.exe" {uvicorn}', 2),
        _proc(102, 101, "cortex.exe", r"C:\Tools\cortex.exe", "cortex --accept-hooks -z hello", 3),
        _proc(103, 101, "ollama.exe", r"C:\Ollama\ollama.exe", "ollama serve", 3),
        # npm run start -> cmd /c next start -> node ...\frontend\node_modules\...\next start
        _proc(110, 50, "node.exe", r"C:\Program Files\nodejs\node.exe",
              rf'"C:\Program Files\nodejs\node.exe" "C:\Program Files\nodejs\node_modules\npm\bin\npm-cli.js" '
              f"run start -- -H 127.0.0.1 -p {web_port}", 4),
        _proc(111, 110, "cmd.exe", r"C:\Windows\System32\cmd.exe",
              rf'C:\WINDOWS\system32\cmd.exe /d /s /c "next start -H 127.0.0.1 -p {web_port}"', 5),
        _proc(112, 111, "node.exe", r"C:\Program Files\nodejs\node.exe",
              rf'node  "{w}\frontend\node_modules\.bin\\..\next\dist\bin\next" start -H 127.0.0.1 -p {web_port}', 6),
        _proc(200, 1, "python.exe", r"C:\Python312\python.exe", "python -m http.server 9999", 0),
        _proc(201, 1, "node.exe", r"C:\Program Files\nodejs\node.exe", r"node C:\work\other-app\server.js", 0),
        _proc(300, 1, "ollama.exe", r"C:\Ollama\ollama.exe", "ollama serve", 0),
        _proc(301, 1, "postgres.exe", r"C:\Program Files\PostgreSQL\17\bin\postgres.exe", "postgres -D data", 0),
        _proc(302, 1, "memurai.exe", r"C:\Program Files\Memurai\memurai.exe", "memurai --service-run", 0),
    ]
    listeners = [
        {"address": "127.0.0.1", "port": api_port, "owner": 101},
        {"address": "127.0.0.1", "port": web_port, "owner": 112},
        {"address": "0.0.0.0", "port": 9999, "owner": 200},
        {"address": "127.0.0.1", "port": 4000, "owner": 201},
        {"address": "127.0.0.1", "port": 11434, "owner": 300},
        {"address": "0.0.0.0", "port": 5432, "owner": 301},
        {"address": "127.0.0.1", "port": 6379, "owner": 302},
    ]
    return processes, listeners


def _stop_root(tmp_path: Path) -> Path:
    root = tmp_path / "ira"
    root.mkdir()
    shutil.copy(STOP_PS1, root / "stop-ira.ps1")
    return root


def _run_stop(pwsh: str, tmp_path: Path, root: Path, processes: list[dict], listeners: list[dict],
              dry_run: bool = False) -> tuple[subprocess.CompletedProcess, list[int]]:
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"processes": processes, "listeners": listeners}), encoding="utf-8")
    harness = tmp_path / "stop-harness.ps1"
    harness.write_text(STOP_HARNESS, encoding="utf-8")
    stopped = tmp_path / "stopped.txt"
    stopped.unlink(missing_ok=True)
    args = [pwsh, "-NoProfile", "-File", str(harness), "-Script", str(root / "stop-ira.ps1"),
            "-Fixture", str(fixture), "-StoppedFile", str(stopped)] + (["-DryRun"] if dry_run else [])
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "OS": "Windows_NT", "NO_COLOR": "1"}
    run = subprocess.run(args, capture_output=True, text=True, timeout=120, env=env)
    ids = [int(x) for x in stopped.read_text().split()] if stopped.exists() else []
    return run, ids


def test_stop_ps1_stops_only_this_folders_api_and_web_app(tmp_path: Path, pwsh: str) -> None:
    root = _stop_root(tmp_path)
    processes, listeners = _ira_stack(str(root))
    run, stopped = _run_stop(pwsh, tmp_path, root, processes, listeners)
    out = run.stdout + run.stderr
    assert run.returncode == 0, out
    # The API from the venv launcher down, with what it started; the web app's
    # node. Top first, so nothing is left to start a child again.
    assert stopped == [100, 101, 102, 112], out
    # Never: Ollama (even as the API's child), PostgreSQL, Memurai, other programs,
    # the npm and cmd wrappers (they exit when their child does), the caller.
    assert not set(stopped) & {103, 300, 301, 302, 200, 201, 110, 111, 50}, out
    assert "Done. PostgreSQL, Memurai (Redis) and Ollama left running." in out


@pytest.mark.parametrize("case", ["another-folder", "unreadable", "reused-parent-id"])
def test_stop_ps1_leaves_other_programs_on_iras_ports_running(tmp_path: Path, pwsh: str, case: str) -> None:
    """Whatever holds IRA's ports but cannot be tied to this folder is named and
    left running, and the exit code says the ports are still in use."""
    root = _stop_root(tmp_path)
    processes, listeners = _ira_stack(str(root))
    by_id = {p["id"]: p for p in processes}
    if case == "another-folder":
        # A second checkout's IRA, or any other uvicorn main:app and Next app.
        other = r"C:\work\other-checkout\supracloud-jarvis"
        by_id[100]["exe"] = rf"{other}\.venv\Scripts\python.exe"
        by_id[100]["cmd"] = by_id[100]["cmd"].replace(str(root).replace("/", "\\"), other)
        by_id[112]["cmd"] = rf'node "{other}\frontend\node_modules\next\dist\bin\next" start -H 127.0.0.1 -p 3000'
    elif case == "unreadable":
        # Another account's processes, or elevated ones: Windows hides the command line.
        for pid in (100, 101, 112):
            by_id[pid]["cmd"] = None
            by_id[pid]["exe"] = None
    else:
        # The listener's parent ID now belongs to this folder's venv python, but
        # that process started after the listener did: Windows reused the ID.
        by_id[100]["created"] = "2026-10-24T10:00:30"
        by_id[112]["cmd"] = r'node "C:\work\other-app\node_modules\next\dist\bin\next" start'
    run, stopped = _run_stop(pwsh, tmp_path, root, processes, listeners)
    out = run.stdout + run.stderr
    assert stopped == [], out
    assert run.returncode == 1, out
    assert "left running" in out and "Still in use" in out, out


def test_stop_ps1_exits_0_when_nothing_is_running(tmp_path: Path, pwsh: str) -> None:
    root = _stop_root(tmp_path)
    processes, listeners = _ira_stack(str(root))
    listeners = [l for l in listeners if l["port"] not in (8000, 3000)]
    run, stopped = _run_stop(pwsh, tmp_path, root, processes, listeners)
    out = run.stdout + run.stderr
    assert run.returncode == 0, out
    assert stopped == []
    assert "ira-api   not running (nothing listens on port 8000)" in out
    assert "frontend  not running (nothing listens on port 3000)" in out


def test_stop_ps1_dry_run_stops_nothing(tmp_path: Path, pwsh: str) -> None:
    root = _stop_root(tmp_path)
    processes, listeners = _ira_stack(str(root))
    run, stopped = _run_stop(pwsh, tmp_path, root, processes, listeners, dry_run=True)
    out = run.stdout + run.stderr
    assert run.returncode == 0, out
    assert stopped == [], out
    for pid in (100, 101, 102, 112):
        assert f"would stop pid {pid} " in out, out
    assert "Dry run: nothing was stopped." in out


def test_stop_ps1_reads_iras_ports_from_env(tmp_path: Path, pwsh: str) -> None:
    """start-ira.ps1 takes IRA_API_PORT and FRONTEND_PORT from .env; so must stop."""
    root = _stop_root(tmp_path)
    (root / ".env").write_text("IRA_API_PORT=8100   # moved\nFRONTEND_PORT=\"3100\"\n", encoding="utf-8")
    processes, listeners = _ira_stack(str(root), api_port=8100, web_port=3100)
    run, stopped = _run_stop(pwsh, tmp_path, root, processes, listeners)
    out = run.stdout + run.stderr
    assert run.returncode == 0, out
    assert stopped == [100, 101, 102, 112], out


# ── verify-native-install.ps1 ────────────────────────────────────────────────

def test_verify_ps1_never_opens_env() -> None:
    """The report is sent back. .env holds every password IRA has."""
    code = _code(VERIFY_PS1.read_text(encoding="utf-8"))
    assert ".env" not in code, "verify-native-install.ps1 names .env outside a comment"
    assert not re.search(r"\b(Copy-Item|Get-Content|ReadAllText|ReadAllBytes|Compress-Archive)\b", code, re.I), (
        "the install check reads or copies a file other than its own logs")


def test_verify_ps1_runs_the_commands_install_md_gives() -> None:
    """Rule 7 of the cross-session checklist: run the documented commands exactly.
    `bash start-ira.sh` once hid a broken `./start-ira.sh` for a day."""
    documented = set(re.findall(r"^\s*powershell -ExecutionPolicy Bypass -File (\S+\.ps1)",
                                INSTALL_MD.read_text(encoding="utf-8"), re.M))
    code = _code(VERIFY_PS1.read_text(encoding="utf-8"))
    for path in ("scripts\\setup-windows.ps1", "start-ira.ps1", "stop-ira.ps1", "scripts\\uninstall-windows.ps1"):
        assert path in documented, f"INSTALL.md no longer gives `powershell ... -File {path}`"
        call = f'"powershell" @("-ExecutionPolicy", "Bypass", "-File", "{path}")'
        assert call in code, f"the install check does not run {path} as INSTALL.md gives it"
    assert "-Yes" not in code, "the install check must run the uninstaller as a dry run only"
    # The smoke test, as verify-native-install.sh runs it: the venv's python, --out in the report.
    assert '".venv\\Scripts\\python.exe"' in code
    assert '@("scripts\\smoke_test.py", "--out", (Join-Path $Out "smoke-test.json"))' in code
    assert 'scripts/smoke_test.py --out "$OUT/smoke-test.json"' in VERIFY_SH.read_text(encoding="utf-8")


def _ps_function(script: Path, name: str) -> str:
    m = re.search(rf"(^function {re.escape(name)}\(.*?\n\}}\n)", script.read_text(encoding="utf-8"), re.S | re.M)
    assert m, f"{script.name} no longer has {name}"
    return m.group(1)


def _protect(pwsh: str, tmp_path: Path, text: str, home: str, pc: str = "") -> str:
    src = tmp_path / "in.txt"
    src.write_text(text, encoding="utf-8")
    script = tmp_path / "protect.ps1"
    script.write_text(
        "param($File, $HomeArg, $PcArg)\n$HomeDir = $HomeArg\n$PcName = $PcArg\n"
        + _ps_function(VERIFY_PS1, "Protect-LogText")
        + "$t = Protect-LogText ([IO.File]::ReadAllText($File))\n"
          "[Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($t)))\n",
        encoding="utf-8")
    run = subprocess.run([pwsh, "-NoProfile", "-File", str(script), str(src), home, pc],
                         capture_output=True, text=True, timeout=120, check=True)
    return base64.b64decode(run.stdout).decode("utf-8")


def _bash_clean(tmp_path: Path, text: str, home: str) -> str:
    m = re.search(r"^(clean\(\) \{.*?^\}\n)", VERIFY_SH.read_text(encoding="utf-8"), re.S | re.M)
    assert m, "verify-native-install.sh no longer has clean()"
    log = tmp_path / "bash.log"
    log.write_text(text, encoding="utf-8")
    subprocess.run(["bash", "-c", m.group(1) + 'clean "$1"', "clean", str(log)],
                   env={"PATH": os.environ.get("PATH", ""), "HOME": home}, check=True, timeout=60)
    return log.read_text(encoding="utf-8")


# Each "{eq}" becomes "=" when the test runs. Written as "=", Python joins these lines
# into one string in the compiled cache, without the gitleaks:allow comment, and the
# pre-push hook's secret scan, which runs after the tests, flags that cache file.
SYNTHETIC_LOG = (
    "IRA_ADMIN_PASSWORD{eq}synthetic-admin-1 and POSTGRES_PASSWORD{eq}\"synthetic-quoted-2\"\n"
    "IRA_SECRET_KEY{eq}synthetic-secret-3 SECRET{eq}synthetic-plain-4\n"
    "REDIS_PASSWORD_FILE{eq}/run/synthetic-file-5 VLLM_API_KEY{eq}synthetic-key-6 GH_TOKEN{eq}synthetic-token-7\n"
    "venv at HOMEDIR/ira/.venv and HOMEDIR/other\n"
    "see IRA_ADMIN_PASSWORD in .env to log in\n"
    "\x1b[0;32m[OK]\x1b[0m done\n"
    "password=lowercase-left-as-bash-leaves-it\n"
).format(eq="=")


def test_verify_ps1_cleans_logs_exactly_as_the_bash_script_does(tmp_path: Path, pwsh: str) -> None:
    """Same input, same output, for every rule verify-native-install.sh has."""
    home = "/home/synthetic-owner"
    text = SYNTHETIC_LOG.replace("HOMEDIR", home)
    ps = _protect(pwsh, tmp_path, text, home)
    assert ps == _bash_clean(tmp_path, text, home)
    assert not re.search(r"synthetic-(admin|quoted|secret|plain|file|key|token)", ps), ps
    assert "IRA_ADMIN_PASSWORD=<redacted>" in ps and "IRA_SECRET_KEY=<redacted>" in ps and "SECRET=<redacted>" in ps
    assert "~/ira/.venv" in ps and home not in ps
    assert "\x1b" not in ps
    assert "see IRA_ADMIN_PASSWORD in .env to log in" in ps


def test_verify_ps1_hides_a_windows_home_a_url_password_and_the_pc_name(tmp_path: Path, pwsh: str) -> None:
    """What Windows adds: a home folder in either slash and any case, a password
    inside a connection URL, and the PC's name."""
    text = (r"C:\Users\Synthetic Owner\ira\.venv\Scripts\python.exe" "\n"
            "c:/users/synthetic owner/AppData/Local/x\n"
            "postgresql://ira_app:synthetic-url-pw@localhost:5432/jarvis_db\n"
            "Local: http://127.0.0.1:3000 on SYNTHPC-01\n")
    out = _protect(pwsh, tmp_path, text, r"C:\Users\Synthetic Owner", "SYNTHPC-01")
    assert out.splitlines() == [r"~\ira\.venv\Scripts\python.exe", "~/AppData/Local/x",
                                "postgresql://ira_app:<redacted>@localhost:5432/jarvis_db",
                                "Local: http://127.0.0.1:3000 on <this-pc>"]


def test_verify_ps1_quotes_arguments_as_windows_splits_them(tmp_path: Path, pwsh: str) -> None:
    """Start-Process joins a list of arguments with spaces and quotes none. The
    report folder sits under the user's profile, often "C:\\Users\\First Last"."""
    values = [r"C:\Users\First Last\ira-check\smoke-test.json", "plain", 'q"x', "ends\\", "a b\\", ""]
    echo = tmp_path / "echo.ps1"
    echo.write_text("foreach ($a in $args) { [Console]::Out.WriteLine('<' + $a + '>') }\n", encoding="utf-8")
    script = tmp_path / "quote.ps1"
    script.write_text(
        "param($Echo, $Pwsh, $Out)\n" + _ps_function(VERIFY_PS1, "ConvertTo-ArgumentLine")
        + "$line = ConvertTo-ArgumentLine @('-NoProfile', '-File', $Echo, " + ", ".join(
            "'" + v.replace("'", "''") + "'" for v in values) + ")\n"
          "$p = Start-Process -FilePath $Pwsh -ArgumentList $line -NoNewWindow -PassThru -RedirectStandardOutput $Out\n"
          "$null = $p.Handle; $p.WaitForExit(); exit $p.ExitCode\n",
        encoding="utf-8")
    out = tmp_path / "args.txt"
    subprocess.run([pwsh, "-NoProfile", "-File", str(script), str(echo), pwsh, str(out)],
                   capture_output=True, text=True, timeout=120, check=True)
    assert out.read_text(encoding="utf-8").splitlines() == [f"<{v}>" for v in values]


VERIFY_HARNESS = r"""
param([string]$Script, [string]$Out, [switch]$PrintListeners, [switch]$NoNetCmdlet)
function global:Read-Host {
    param([string]$Prompt, [switch]$AsSecureString)
    ConvertTo-SecureString "typed-synthetic-pg-pw" -AsPlainText -Force
}
if (-not $NoNetCmdlet) {
    function global:Get-NetTCPConnection {
        [CmdletBinding()] param([string]$State)
        if (Test-Path -LiteralPath $env:IRA_TEST_LISTEN_FILE) {
            foreach ($l in Get-Content -LiteralPath $env:IRA_TEST_LISTEN_FILE) {
                if ($l -match '^\[?(.*?)\]?:(\d+)$') {
                    [pscustomobject]@{ LocalAddress = $matches[1]; LocalPort = [int]$matches[2]; OwningProcess = 4242; State = "Listen" }
                }
            }
        }
        [pscustomobject]@{ LocalAddress = "0.0.0.0"; LocalPort = 135; OwningProcess = 900; State = "Listen" }
    }
}
function global:Get-CimInstance {
    [CmdletBinding()] param([string]$ClassName)
    switch ($ClassName) {
        "Win32_OperatingSystem" { [pscustomobject]@{ Caption = "Microsoft Windows 11 Pro"; Version = "10.0.22631"
                                                     OSArchitecture = "64-bit"; CSName = "SECRETHOST"; RegisteredUser = "SECRETUSER" } }
        "Win32_Processor" { [pscustomobject]@{ Name = "Synthetic CPU 9000 " } }
        "Win32_ComputerSystem" { [pscustomobject]@{ TotalPhysicalMemory = 17179869184; Name = "SECRETHOST"; UserName = "SECRETUSER" } }
        "Win32_VideoController" { [pscustomobject]@{ Name = "Synthetic GPU" } }
    }
}
if ($PrintListeners) { & $Script -PrintListeners } else { & $Script -Out $Out }
exit $LASTEXITCODE
"""

ENV_SECRETS = ("canary-from-env-7f3a", "env-file-admin-secret-9c1e")
TYPED_PG_PASSWORD = "typed-synthetic-pg-pw"


def _install(tmp_path: Path, pwsh: str, *, listen_after_start: str, stop_clears: bool) -> tuple[Path, dict]:
    """A throwaway install: the real install check, stub steps that say what they
    were given, a .env holding values that must never reach the report, and a
    `powershell` that runs pwsh."""
    root = tmp_path / "home" / "ira-supracloud" / "supracloud-jarvis"
    (root / "scripts").mkdir(parents=True)
    (root / ".venv" / "Scripts").mkdir(parents=True)
    shutil.copy(VERIFY_PS1, root / "scripts" / "verify-native-install.ps1")
    (root / ".env").write_text(f"IRA_TEST_CANARY={ENV_SECRETS[0]}\nIRA_ADMIN_PASSWORD={ENV_SECRETS[1]}\n",
                               encoding="utf-8")
    (root / "scripts" / "setup-windows.ps1").write_text(
        f'Write-Output ("setup was given the typed password: " + ($env:PGPASSWORD -eq "{TYPED_PG_PASSWORD}"))\n'
        'Write-Output "PGPASSWORD=$env:PGPASSWORD"\n'
        'Write-Output "IRA_ADMIN_PASSWORD=synthetic-admin-pw-1 IRA_SECRET_KEY=synthetic-key-2"\n'
        'Write-Output "venv at $HOME/ira-supracloud/supracloud-jarvis/.venv"\n'
        'Write-Output "db postgresql://ira_app:synthetic-url-pw@localhost:5432/jarvis_db"\n', encoding="utf-8")
    (root / "start-ira.ps1").write_text(
        'Write-Output ("start was given a PostgreSQL password: " + [bool]$env:PGPASSWORD)\n'
        f"Set-Content -LiteralPath $env:IRA_TEST_LISTEN_FILE -Value @({listen_after_start})\n"
        'Write-Output "ALL UP. IRA is ready, boss."\n', encoding="utf-8")
    stop_body = ("Remove-Item -LiteralPath $env:IRA_TEST_LISTEN_FILE -ErrorAction SilentlyContinue\n"
                 if stop_clears else "")
    (root / "stop-ira.ps1").write_text(stop_body + 'Write-Output "Done."\n', encoding="utf-8")
    (root / "scripts" / "uninstall-windows.ps1").write_text(
        'Set-Content -LiteralPath (Join-Path $PSScriptRoot "uninstall-args.txt") -Value ("args:" + ($args -join " "))\n'
        'Write-Output "Dry run: nothing was changed. Run again with -Yes to back up and remove."\n', encoding="utf-8")
    smoke = root / ".venv" / "Scripts" / "python.exe"
    smoke.write_text('#!/bin/sh\nout=""\nwhile [ $# -gt 0 ]; do [ "$1" = "--out" ] && out="$2"; shift; done\n'
                     '[ -n "$out" ] && printf \'{"verdict": "PASS"}\' > "$out"\n'
                     'echo "  PASS  health     HTTP 200"\necho\n'
                     'echo "PASS: 9/9 checks passed (commit 0000000, Windows AMD64)"\n')
    smoke.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "powershell").write_text(f'#!/bin/sh\nexec "{pwsh}" "$@"\n')
    (bin_dir / "powershell").chmod(0o755)
    (tmp_path / "tmp").mkdir()
    (tmp_path / "harness.ps1").write_text(VERIFY_HARNESS, encoding="utf-8")
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", "HOME": str(tmp_path / "home"),
           "TMPDIR": str(tmp_path / "tmp"), "OS": "Windows_NT", "COMPUTERNAME": "SYNTHPC-01",
           "PROCESSOR_ARCHITECTURE": "AMD64", "IRA_TEST_LISTEN_FILE": str(tmp_path / "listening.txt"),
           "IRA_VERIFY_SETTLE_SEC": "0", "NO_COLOR": "1"}
    return root, env


def _run_verify(pwsh: str, tmp_path: Path, root: Path, env: dict, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([pwsh, "-NoProfile", "-File", str(tmp_path / "harness.ps1"),
                           "-Script", str(root / "scripts" / "verify-native-install.ps1"),
                           "-Out", str(tmp_path / "home" / "report"), *extra],
                          capture_output=True, text=True, timeout=300, env=env)


def test_verify_ps1_end_to_end_with_stand_ins(tmp_path: Path, pwsh: str) -> None:
    root, env = _install(tmp_path, pwsh, listen_after_start='"127.0.0.1:8000", "127.0.0.1:3000"', stop_clears=True)
    run = _run_verify(pwsh, tmp_path, root, env)
    out = run.stdout + run.stderr
    assert run.returncode == 0, out
    report = tmp_path / "home" / "report"
    summary = (report / "SUMMARY.txt").read_text(encoding="utf-8").splitlines()
    assert summary[1].startswith("Windows script (scripts\\verify-native-install.ps1), run by PowerShell 7."), summary
    steps = [ln.split()[:2] for ln in summary[2:] if ln.strip() and ln.split()[0] in
             ("machine", "setup", "start", "loopback", "smoke", "stop", "uninstall")]
    assert steps == [[s, "PASS"] for s in ("machine", "setup", "start", "loopback", "smoke", "stop", "uninstall")], summary
    assert "loopback  PASS 127.0.0.1:3000 127.0.0.1:8000" in summary
    assert "smoke     PASS PASS: 9/9 checks passed" in "\n".join(summary)
    assert summary[-1] == "ALL PASS"
    machine = next(ln for ln in summary if ln.startswith("machine"))
    assert "Microsoft Windows 11 Pro 10.0.22631" in machine and "16 GiB" in machine and "Synthetic GPU" in machine
    assert {p.name for p in report.iterdir()} == {"SUMMARY.txt", "machine.log", "setup.log", "start.log",
                                                  "smoke.log", "smoke-test.json", "stop.log", "uninstall-dry-run.log"}
    # The PostgreSQL password went to setup and to nothing after it.
    assert "setup was given the typed password: True" in (report / "setup.log").read_text(encoding="utf-8")
    assert "start was given a PostgreSQL password: False" in (report / "start.log").read_text(encoding="utf-8")
    # The uninstaller ran without -Yes.
    assert (root / "scripts" / "uninstall-args.txt").read_text(encoding="utf-8").strip() == "args:"
    # Nothing secret, nothing from .env, no PC or user name, no home folder, anywhere.
    everything = "\n".join(p.read_text(encoding="utf-8") for p in report.iterdir())
    for secret in (*ENV_SECRETS, TYPED_PG_PASSWORD, "synthetic-admin-pw-1", "synthetic-key-2", "synthetic-url-pw",
                   "SYNTHPC-01", "SECRETHOST", "SECRETUSER", str(tmp_path / "home")):
        assert secret not in everything, f"{secret!r} reached the report"
    assert "~/ira-supracloud/supracloud-jarvis/.venv" in (report / "setup.log").read_text(encoding="utf-8")
    # The raw, uncleaned output is gone.
    assert not list((tmp_path / "tmp").glob("ira-install-check-raw-*"))


def test_verify_ps1_reports_a_listener_beyond_loopback_and_a_failed_stop(tmp_path: Path, pwsh: str) -> None:
    root, env = _install(tmp_path, pwsh, listen_after_start='"127.0.0.1:8000", "0.0.0.0:3000"', stop_clears=False)
    run = _run_verify(pwsh, tmp_path, root, env)
    out = run.stdout + run.stderr
    assert run.returncode == 1, out
    summary = (tmp_path / "home" / "report" / "SUMMARY.txt").read_text(encoding="utf-8")
    assert "loopback  FAIL listening beyond this machine: 0.0.0.0:3000 127.0.0.1:8000" in summary
    # stop-ira.ps1 said exit 0, but the ports are still held: that is a FAIL.
    assert "stop      FAIL still listening after stop: 0.0.0.0:3000 127.0.0.1:8000" in summary
    assert "2 step(s) FAILED" in summary


def test_verify_ps1_refuses_while_iras_ports_are_in_use(tmp_path: Path, pwsh: str) -> None:
    """A running IRA holds its files, so setup could not replace them, and the
    checks would test the old code. Nothing runs."""
    root, env = _install(tmp_path, pwsh, listen_after_start='"127.0.0.1:8000"', stop_clears=True)
    (tmp_path / "listening.txt").write_text("127.0.0.1:8000\n")
    run = _run_verify(pwsh, tmp_path, root, env)
    out = run.stdout + run.stderr
    assert run.returncode == 1, out
    assert "Stop IRA first" in out
    assert not (tmp_path / "home" / "report").exists()
    assert not (root / "scripts" / "uninstall-args.txt").exists()


def test_verify_ps1_cannot_list_is_not_nothing_listening(tmp_path: Path, pwsh: str) -> None:
    root, env = _install(tmp_path, pwsh, listen_after_start='"127.0.0.1:8000"', stop_clears=True)
    # Without Get-NetTCPConnection the sockets cannot be listed: exit 3, not an empty list.
    blind = _run_verify(pwsh, tmp_path, root, env, "-PrintListeners", "-NoNetCmdlet")
    assert blind.returncode == 3, blind.stdout + blind.stderr
    # With it, and nothing on IRA's ports: an empty list and exit 0.
    empty = _run_verify(pwsh, tmp_path, root, env, "-PrintListeners")
    assert (empty.returncode, empty.stdout.strip()) == (0, ""), empty.stdout + empty.stderr
    (tmp_path / "listening.txt").write_text("127.0.0.1:8000\n::1:3000\n")
    some = _run_verify(pwsh, tmp_path, root, env, "-PrintListeners")
    assert sorted(some.stdout.split()) == ["127.0.0.1:8000", "[::1]:3000"], some.stdout + some.stderr


# ── INSTALL.md ────────────────────────────────────────────────────────────────


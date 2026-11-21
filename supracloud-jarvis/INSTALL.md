# Installing IRA

IRA runs on a computer you own — a laptop, a desktop, or a server you rent — and
everything it learns stays there. Phones and tablets use it through their browser.
**No graphics card is required.** A GPU or an Apple Silicon Mac makes answers faster;
it is not needed to install or use IRA.

Installing takes two steps: install five standard programs IRA is built on, then run
one setup script that does the rest — it checks your computer, picks an AI model that
fits it, creates the database, generates your passwords and builds the app.

> **Honest status, 18 October 2026.** The full install, start, stop, update, chat and
> backup cycle has been run end to end on **Linux without a GPU**. The macOS and
> Windows scripts have been checked but **not yet run on a Mac or a Windows PC** — see
> [what has been verified](#what-has-been-verified-where). Please report anything that
> differs on your machine.

---

## Will it run on my computer?

| | Minimum | Recommended |
|---|---|---|
| Memory (RAM) | 8 GB | 16 GB or more |
| Free disk space | 10 GB | 20 GB |
| Processor | any 64-bit, 4 cores | Apple Silicon, or an NVIDIA GPU |
| Operating system | macOS 14 (Apple Silicon) · Windows 10/11 64-bit · Linux (Ubuntu 24.04 tested) | |
| Python | **3.11 or 3.12** — not 3.13 or newer (IRA's pinned libraries have no builds for them yet) | 3.12 |

Setup reads your memory and graphics card and picks the local AI model for you:

| Your computer | Model chosen | Why |
|---|---|---|
| Under 8 GB | qwen3:0.6b | below the minimum: works, but answers are basic |
| 8–11 GB | qwen3:1.7b | |
| 12–15 GB | qwen3:4b | |
| 16–31 GB with Apple Silicon or a GPU | qwen3:8b | |
| 32 GB+ with Apple Silicon or a GPU, or an NVIDIA card with 11 GB+ | qwen3:14b | |
| **No GPU, any amount of memory** | **qwen3:4b at most** | speed, not memory, is the limit |

Measured on a 4-core cloud server with no GPU: qwen3:1.7b produces about 10 tokens
(word pieces) per second, qwen3:4b about 6, qwen3:8b about 3. A full answer from qwen3:4b took
**60–160 seconds** there, because the model "thinks" before it starts writing. That is
usable, not fast. You can change the model at any time in `.env`.

An install with qwen3:4b used about 7 GB of disk: 1.8 GB of Python libraries, 1.1 GB
for the web app, 1.3 GB for the memory-search model and 2.5 GB for the chat model.

---

## macOS (Apple Silicon)

1. Install [Homebrew](https://brew.sh), then in Terminal:

   ```bash
   brew install python@3.12 node@22 postgresql@17 pgvector redis ollama
   brew services start postgresql@17
   brew services start redis
   ```

   Use **PostgreSQL 17** (or 18): Homebrew's pgvector is built only for those two
   versions, so PostgreSQL 16 cannot load it.

2. Get IRA and run setup:

   ```bash
   git clone https://github.com/Praveenkumar101508/ira-supracloud.git
   cd ira-supracloud/supracloud-jarvis
   bash scripts/setup-native.sh
   ```

3. Start it — from Terminal with `./start-ira.sh`, or by double-clicking
   **Start IRA.command** in Finder. Open <http://localhost:3000> and sign in as
   `admin` with the `IRA_ADMIN_PASSWORD` that setup wrote into `.env`.

## Linux, or a cloud server

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv postgresql postgresql-16-pgvector redis-server curl git zstd
curl -fsSL https://ollama.com/install.sh | sh
# Node.js 22 (Ubuntu's own nodejs package is too old), from NodeSource:
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs

git clone https://github.com/Praveenkumar101508/ira-supracloud.git
cd ira-supracloud/supracloud-jarvis
bash scripts/setup-native.sh
./start-ira.sh
```

Setup needs to act as the database administrator once; on Linux it uses
`sudo -u postgres`. On a **cloud server**, do not open ports 3000 or 8000 to the
internet — IRA listens on the server itself only. Reach it from your own devices with
Tailscale (below) or an SSH tunnel: `ssh -L 3000:127.0.0.1:3000 you@server`.

## Windows 10 / 11

1. Install, accepting the defaults:
   [Python 3.12](https://www.python.org/downloads/) (tick "Add to PATH"),
   [Node.js LTS](https://nodejs.org), [PostgreSQL 17](https://www.postgresql.org/download/windows/)
   with its `bin` folder on PATH, [Memurai](https://www.memurai.com/get-memurai) (Redis
   for Windows) and [Ollama](https://ollama.com/download/windows).
2. Install **pgvector** for PostgreSQL 17. On Windows it is compiled from source with
   Visual Studio's build tools —
   [pgvector's Windows instructions](https://github.com/pgvector/pgvector#windows).
   This is the hardest step on Windows.
3. In PowerShell, from the `supracloud-jarvis` folder:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
   powershell -ExecutionPolicy Bypass -File start-ira.ps1      # or double-click "Start IRA.bat"
   ```

4. To stop IRA, from the same folder:

   ```powershell
   powershell -ExecutionPolicy Bypass -File stop-ira.ps1       # add -DryRun to see what it would stop
   ```

   It stops IRA's API and web app, and leaves PostgreSQL, Memurai and Ollama running.
   It stops only the programs on IRA's ports (8000 and 3000) that run from this folder,
   never every `python` or `node`, and it names anything else it finds there. The script
   is written, parse-checked and tested with stand-ins under PowerShell 7 on Linux. It is
   **NOT TESTED on Windows**.

## Phones and tablets (iPad, iPhone, Android)

IRA runs on a computer; your phone or tablet opens it in the browser. By design IRA
listens **only on the computer it runs on**, so nothing on your Wi-Fi or the internet
can reach it directly. To use it from another device, put both on a private
[Tailscale](https://tailscale.com) network and let Tailscale serve it over HTTPS — the
full steps are in [TAILSCALE_SETUP.md](TAILSCALE_SETUP.md). HTTPS is not optional:
phones only allow the microphone on secure pages.

---

## Updating IRA

```bash
./stop-ira.sh
git pull
bash scripts/setup-native.sh
./start-ira.sh
```

On Windows, from the `supracloud-jarvis` folder:

```powershell
powershell -ExecutionPolicy Bypass -File stop-ira.ps1
git pull
powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
powershell -ExecutionPolicy Bypass -File start-ira.ps1
```

Running setup again is safe. It keeps your `.env` and your passwords, installs any new
libraries, and applies new database changes. It never resets your data. The update has
been run on Linux only (see the table below); on macOS and Windows it is not yet verified.

## Moving IRA to another computer

```bash
# on the old computer (IRA can be running)
bash scripts/ira-backup.sh backup                 # writes ~/.ira/backups/ira-backup-<time>.tar.gz

# copy the archive to the new computer's ~/.ira/backups/, then, after installing IRA as above
./stop-ira.sh
bash scripts/ira-backup.sh restore ira-backup-<time>.tar.gz --yes   # a bare name is looked for in ~/.ira/backups
./start-ira.sh                                    # sign in with your OLD password
```

On Windows, in PowerShell, from the IRA folder:

```powershell
# on the old computer (IRA can be running); asks for the PostgreSQL "postgres" password
powershell -ExecutionPolicy Bypass -File scripts\ira-backup.ps1 backup   # writes %USERPROFILE%\.ira\backups\ira-backup-<time>.tar.gz

# on the new computer, after installing IRA
powershell -ExecutionPolicy Bypass -File stop-ira.ps1
powershell -ExecutionPolicy Bypass -File scripts\ira-backup.ps1 restore ira-backup-<time>.tar.gz -Yes
```

The two scripts write the same archive. A backup made on Linux or macOS restores on
Windows. Restoring a Windows backup on Linux or macOS is not yet reliable: Windows ends the
`.env`'s lines differently, and `ira-backup.sh` keeps that ending in the database
password.

The backup holds your whole database **and** your `.env`, because the keys in `.env`
unlock encrypted data in the database (authenticator secrets, voice profiles, connector
credentials). `ira-backup.sh` also holds the folders IRA keeps outside the database:
notes, project folders and their `.env` files, the sealed vault, uploads, the coding
agent's tasks and your "about me" documents. Treat the file like the computer itself.
Restore keeps a safety copy of whatever it replaces, and moves each folder it replaces
to `~/.ira/backups/replaced-<time>/`. When the new computer keeps uploads in a different
folder, restore points their database rows at it.

`ira-backup.ps1` carries the same folders in the same archive; on Windows a restore moves
each folder it replaces to `%USERPROFILE%\.ira\backups\replaced-<time>\`. It has been run
only under PowerShell 7 on Linux, with stand-in database tools: it is **not yet tested on
Windows**. Uploads made on Windows do not yet survive a move to Linux or macOS: the files
come back, but their database rows still name the Windows paths.

The in-app backup (`POST /api/v1/backup/create`) cannot produce a complete backup: the
running app is deliberately not allowed to read every row, so it is refused by the
database. Use the scripts above.

## Uninstalling IRA

```bash
bash scripts/uninstall-native.sh          # dry run: lists what would be removed, changes nothing
bash scripts/uninstall-native.sh --yes    # backs up first, then removes
```

With `--yes` it stops IRA and takes a backup with `ira-backup.sh`. If the backup fails,
or the archive lacks the database or `.env`, it stops and removes nothing. Otherwise it
drops IRA's database and deletes `.venv`, `.ira-run`, the frontend build and `.env` from
this folder. It prints the backup's path.

It never deletes your backups, this folder, PostgreSQL, Redis, Ollama, or the shared
download caches. Two extras are opt-in: `--remove-roles` drops IRA's database roles
(each one is kept if another database still uses it), and `--remove-model` removes
the chat model setup downloaded.

To undo it, install again and restore the backup it printed:

```bash
bash scripts/setup-native.sh
bash scripts/ira-backup.sh restore <the archive it printed> --yes
```

On Windows, from the IRA folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\uninstall-windows.ps1         # dry run: lists what would be removed, changes nothing
powershell -ExecutionPolicy Bypass -File scripts\uninstall-windows.ps1 -Yes    # backs up first, then removes
```

It does the same as the Linux and macOS script, with three differences:

- **IRA must be stopped first,** with `powershell -ExecutionPolicy Bypass -File stop-ira.ps1`.
  Windows cannot delete files a running program holds, so the uninstaller refuses
  while anything listens on IRA's API or web port.
- **It also removes the logon autostart** that `start-ira.ps1 -InstallAutostart` writes,
  when it starts this folder.
- **It does not remove the boot task** that `start-ira.ps1 -InstallService` registers.
  That needs an elevated prompt; the uninstaller prints the command.

It asks for the PostgreSQL superuser's password, as `ira-backup.ps1` does. To undo it,
run `scripts\setup-windows.ps1`, then `scripts\ira-backup.ps1 restore <the archive it printed> -Yes`.

The Windows uninstaller has run under PowerShell 7 on Linux against a scratch database,
not yet on Windows.

---

## What has been verified, where

"Verified" means it was run and the result recorded — not that the code exists.

| | Linux (no GPU) | macOS Apple Silicon | Windows | Phone / tablet |
|---|---|---|---|---|
| Libraries install | ✅ verified | ✅ every library has a macOS 14+ arm64 build for Python 3.12 (checked, not installed) | ⏳ not run | — |
| Fresh setup | ✅ verified (198 s) | ⏳ not run | ⏳ not run | — |
| Setup again over an install | ✅ verified: `.env`, password and data kept | ⏳ | ⏳ | — |
| Update from an older version ([Updating IRA](#updating-ira)) | ✅ verified from 18 October data: new database changes applied, old conversations and memories found, smoke test passed ([record](../docs/testing/evidence/upgrade-089-2026-10-19/README.md)) | ⏳ | ⏳ | — |
| Start / stop / restart | ✅ verified: nothing left running after stop | ⏳ | ⏳ | — |
| Sign-in, privacy check, real chat, follow-up in the same conversation | ✅ verified | ⏳ | ⏳ | layout tested in a browser engine, not on a device |
| Memory save, list and approved forget | ✅ verified | ⏳ | ⏳ | — |
| Backup, and restore onto a fresh install | ✅ verified (same machine) | ⏳ | ⏳ script written; run only under PowerShell 7 on Linux ([record](../docs/testing/evidence/windows-backup-2026-10-21/README.md)) | — |
| Uninstall (backup first), then reinstall and restore | ✅ verified: old password signs in, data present | ⏳ | ⏳ script written; run only under PowerShell 7 on Linux ([record](../docs/testing/evidence/windows-uninstall-2026-10-21/README.md)) | — |
| Listens on this computer only | ✅ verified | ⏳ | ⏳ | — |

The Linux column was last re-run in full on 23 October 2026, on `main` @ `0a88694` with
`qwen3:4b`: every step passed, the smoke test 9 of 9 ([record](../docs/testing/evidence/install-check-listeners-2026-10-23/README.md)).

⏳ = written and checked (scripts parse, logic tested on Linux), but not yet run on that
platform. A manual GitHub Actions workflow, `Install verification (per platform)`, runs
the same install on GitHub's Linux, Apple Silicon and Windows machines once the owner
chooses to spend the minutes.

**Check it on your own Mac or Linux machine** (free, about 10–20 minutes):

```bash
bash scripts/verify-native-install.sh
```

It runs the steps above exactly as this guide gives them (setup, `./start-ira.sh`,
the smoke test, `./stop-ira.sh`, and a dry run of the uninstaller), and writes a
PASS/FAIL report to `~/ira-install-check-<time>/`. The report holds no passwords; send
the whole folder back to fill in a column of the table above.

**On Windows**, in PowerShell from the `supracloud-jarvis` folder, after steps 1 and 2
of the Windows install:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-native-install.ps1
```

It runs the Windows commands above in the same order (`setup-windows.ps1`,
`start-ira.ps1`, the smoke test, `stop-ira.ps1`, and a dry run of
`uninstall-windows.ps1`), and writes the report to
`%USERPROFILE%\ira-install-check-<time>\`. Stop IRA before you run it. It asks once for
the PostgreSQL superuser's password, which setup needs, and never writes it down. The
script is written, parse-checked and tested with stand-ins under PowerShell 7 on Linux.
It is **NOT TESTED on Windows**
([record](docs/testing/evidence/windows-install-check-and-stop-2026-10-24/README.md)).

## When something goes wrong

| What you see | What to do |
|---|---|
| Setup says Python 3.11 or 3.12 not found | install Python 3.12 alongside your current one; setup finds it |
| `pgvector is not installed for PostgreSQL N` | install pgvector for that version (macOS: use PostgreSQL 17) |
| Setup says the Mac has an Intel processor, is running under Rosetta, or is too old | IRA needs Apple Silicon (M1 or later) on macOS 14 or later, run natively: open Terminal without "Open using Rosetta" and use the Homebrew in `/opt/homebrew` |
| Sign-in fails with 503 | the database has no tables yet: run setup again |
| Answers take minutes, or the reply bubble stays empty | normal without a GPU while the model thinks; choose a smaller model in `.env`, and turn on "reduce motion" in your system settings so the screen doesn't compete with the model |
| `STACK NOT FULLY UP` | the line marked `[FAIL]` names the service; its log is in `.ira-run/` |

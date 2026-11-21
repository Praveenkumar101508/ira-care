# The Armory Vault

An encrypted, owner-gated cabinet of cybersecurity tools for IRA. The catalogue
(the Kali toolchain, Tor, Nmap, Metasploit, …) is sealed with **AES-256-GCM**
behind a **Master Key only the owner knows**. Nothing runs a tool except through
the `ArmoryGatekeeper`, and every gate defaults to _deny_ (fail-closed).

> ⚠️ **Authorization is your responsibility.** Only scan or test systems you own
> or are explicitly authorized in writing to assess. The whitelist is a safety
> rail, not permission. Unauthorized scanning or exploitation is illegal.

---

## 1. How it works

```
unlock(master_key)   verify PBKDF2 verifier → AES-GCM decrypt vault → RAM only
                     │
execute(tool, args)  ├─ locked?            → "🔒 enter Master Key"
                     ├─ not owner?          → refuse
                     ├─ target not in       → "❌ not in authorized scope"
                     │   whitelist.txt?
                     ├─ external target &    → block (local/private only)
                     │   IRA_ALLOW_EXTERNAL_API=false?
                     ├─ risk critical?      → block outright
                     ├─ risk high?          → require a valid TOTP (2FA) code
                     ├─ Shadow World preview (logged)
                     ├─ run in throwaway container (--rm, --network none|Tor)
                     ├─ Trust Ledger: record that the tool ran (not its output)
                     └─ auto-lock after the task (and after idle TTL)
```

### Module map (`ira/armory/`)

| File | Responsibility |
| ---- | -------------- |
| `vault_encryption.py` | PBKDF2 key **verification** (stdlib) + AES-256-GCM **body cipher** (`cryptography`, lazy-imported) |
| `vault.py` | The on-disk sealed vault; **decrypt-to-RAM** open path; reseal |
| `session.py` | In-RAM unlock state; **idle + task-completion auto-lock** |
| `manifest.py` | Tool catalogue + **injection-proof argv** construction |
| `whitelist.py` | Authorized-target scope (exact / wildcard / IP / CIDR), fail-closed |
| `docker_manager.py` | **Ephemeral**, network-gated (Tor killswitch) Docker containers |
| `executors.py` | Pluggable backends — Docker **or** firejail/bubblewrap namespaces (no daemon) **or** opt-in unsandboxed local; `select_executor` picks the best available |
| `gatekeeper.py` | The one chokepoint: `unlock` / `lock` / `preview` / `execute` |
| `vault_guard.py` | FastAPI dependency gating tool routes on unlock state |
| `vault_template/` | The plaintext `manifest.json`, `whitelist.txt`, compose file that setup seals |

Integration with existing IRA systems (reused, never re-implemented):

- **Trust Ledger** (`trust/ledger.py`) — every action is hash-chained.
- **Shadow World** (`actions/shadow_world.py`) — commands are simulated first
  (action kind `armory_tool`).
- **Owner gate** (`security/owner_gate.py`) — armory intents are owner-only on
  every path.
- **`IRA_ALLOW_EXTERNAL_API`** (`config.py`) — governs external egress.
- **Agent router** (`agents/graph.py`) — a new `armory` agent alongside the
  existing specialists.

---

## 2. Setup (once per machine)

```bash
cd supracloud-jarvis/ira
python -m scripts.setup_vault            # interactive: prompts twice for the key
#   → seals data/armory/vault.sealed (AES-256-GCM)
#   → writes ARMORY_MASTER_KEY_HASH=<pbkdf2 verifier> to ../.env  (never the key)
```

Non-interactive / CI:

```bash
python -m scripts.setup_vault --key "$ARMORY_KEY" --force
```

Then enable it and restart IRA:

```dotenv
ARMORY_ENABLED=true
ARMORY_SESSION_TTL_SECONDS=300     # idle auto-lock; also locks after each task
ARMORY_DOCKER_ENABLED=true         # false = hard kill switch for tool execution
# ARMORY_MASTER_KEY_HASH is set for you by setup_vault
```

**There is no key recovery.** Losing the Master Key means the vault cannot be
opened — that is the point. Re-provision with `--force` to start over (this
invalidates the old key and its sealed contents).

Pre-pull the container images ahead of time if you like:

```bash
docker compose -f data/armory/… pull   # or: docker pull kalilinux/kali-rolling dperson/torproxy
```

---

## 2b. Execution backends — Docker is optional

Tools run inside a **sandbox**, and the sandbox is pluggable. `select_executor`
(`armory/executors.py`) picks the best available backend for your policy:

| Backend | Isolation | Network killswitch | Notes |
| ------- | --------- | ------------------ | ----- |
| `docker` | container | `--network none` / netns-shared Tor | **Strongest.** Ephemeral `--rm`, no volumes. |
| `firejail` | Linux namespaces (no daemon) | `--net=none` (offline) / Tor via proxychains | No Docker needed. Runs host-installed tools. |
| `bubblewrap` | Linux namespaces (no daemon) | `--unshare-net` (offline) / Tor via proxychains | No Docker needed. Read-only rootfs + private tmpfs. |
| `local` | **none** | Tor via proxychains only | Opt-in last resort; runs on the host with no isolation. |

```dotenv
ARMORY_EXECUTION_BACKEND=auto     # auto | docker | firejail | bubblewrap | local
ARMORY_DOCKER_ENABLED=true        # false = never use the Docker backend
ARMORY_ALLOW_UNSANDBOXED=false    # true = permit the no-isolation 'local' backend
```

`auto` prefers Docker → firejail → bubblewrap, and only uses unsandboxed `local`
if `ARMORY_ALLOW_UNSANDBOXED=true`. If nothing is available it **fails closed** —
tools refuse to run rather than run with less protection than promised.

**What each backend guarantees**

- **Offline tools (`network: none`)** are cut off from the network at the kernel
  by all three sandboxes (`--network none` / `--net=none` / `--unshare-net`).
- **Network tools (`network: tor`)** egress only through a Tor SOCKS proxy. Under
  Docker the tool shares Tor's network namespace (traffic *cannot* bypass Tor).
  Under firejail/bubblewrap/local, egress is forced with `proxychains4` through a
  **host Tor** at `127.0.0.1:9050`; if proxychains isn't installed or Tor isn't
  reachable, the tool is **refused** (killswitch holds). This is strong but not a
  kernel-level guarantee — **prefer Docker when you can.**

**Prerequisites for the non-Docker backends**

- The tool binaries must be installed on the host (e.g. `apt install nmap nikto`),
  since there's no Kali container to provide them.
- For `network: tor` tools: install `proxychains4` and run a local Tor
  (`apt install tor && systemctl start tor`, or `tor` in a terminal).
- `bwrap` (bubblewrap) or `firejail` must be installed for those backends.

Check which backend is active any time via `GET /api/v1/armory/status` →
`execution_backend`.

---

## 3. Using it

### From chat or voice

```
You: unlock the armory with key <YOUR_KEY>
IRA: ✅ Armory Unlocked. 100 tools available.
You: nmap scan localhost
IRA: ✅ Ran nmap_scan (risk: low, network: tor, exit 0). <results>
     🔒 Armory locked automatically after the task.
You: is the armory unlocked?
IRA: 🔒 Armory is LOCKED. Provide the Master Key to unlock Security Tools.
```

The agent understands `unlock … key …`, `lock the armory`, `armory status`, and
tool requests (`nmap …`, `nikto on …`, `sqlmap …`, etc.). The Master Key you type
is used to unlock and then dropped — it is never echoed, stored, or logged.

### From the UI

The **Armory** panel (sidebar → Armory) is the Trust-Console surface for all of
this: it renders the live gatekeeper status, takes the Master Key to unlock,
lists the vault's tools grouped by category, builds per-tool argument inputs
(flagging which are whitelist-gated targets), prompts for the **2FA code** on
high-risk tools, streams each run's output, and shows the idle/lock countdown
with a one-click **Lock**. It only ever calls the same owner-gated endpoints
below — no client-side trust.

### From the API

All routes are under `/api/v1/armory/` and require the verified owner.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET`  | `/armory/status`  | enabled? provisioned? unlocked? locked-out? backend? |
| `POST` | `/armory/unlock`  | `{ "master_key": "…" }` → decrypt to RAM |
| `POST` | `/armory/lock`    | drop the in-RAM session |
| `GET`  | `/armory/manifest`| list the tools in the unlocked vault |
| `GET`  | `/armory/whitelist`| show the authorized-target scope |
| `POST` | `/armory/preview` | `{ "tool_id", "args" }` → Shadow World sim, no run |
| `POST` | `/armory/execute` | `{ "tool_id", "args", "approval_code", "lock_after" }` |

Editing the vault (adding tools, widening scope, rotating the key) is host-side and
needs the Master Key, so it lives in the CLI (§4), not the API — the API is
read-only for the manifest/whitelist.

```bash
curl -sX POST localhost:8000/api/v1/armory/unlock \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"master_key":"<YOUR_KEY>"}'

curl -sX POST localhost:8000/api/v1/armory/execute \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"tool_id":"nmap_scan","args":{"target":"localhost"}}'
```

A locked vault returns **423 Locked** with the unlock prompt.

### High-risk tools need 2FA

A tool with `"risk": "high"` (e.g. `hydra`, `metasploit_exploit`) requires a valid
**TOTP code** — the same authenticator second factor as login. The gate is
fail-closed and enforced by `security/totp.py` (shared with `/auth/totp`):

- Enrol once: `POST /auth/totp/enroll` with `{"password": ...}` (the account
  password; to replace a factor that is already on, also `"code"` from it),
  scan the QR / save the secret, then `POST /auth/totp/verify` with a code to
  activate it. A replacement also sends the `enrolment_token` `/enroll` returned.
- Run a high-risk tool with the code:

  ```bash
  curl -sX POST localhost:8000/api/v1/armory/execute \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"tool_id":"hydra_ssh","args":{"target":"localhost","userlist":"u","passlist":"p"},"approval_code":"123456"}'
  ```

  In chat: *"run hydra on localhost with code 123456"*.

- Outcomes: no code → `approval_required`; wrong/expired code → `approval_denied`;
  **TOTP not enrolled → `approval_unavailable`** (enrol first). `critical` tools are
  blocked regardless.

Set `ARMORY_REQUIRE_TOTP_FOR_HIGH_RISK=false` to fall back to a plain explicit
confirmation (`approve_high_risk: true`) instead of a real code — not recommended.

---

## 4. Adding tools & authorizing targets

The manifest and whitelist live **inside the encrypted vault**, so editing them
happens host-side with the Master Key via the admin CLI (`scripts/armory_vault.py`):

```bash
cd supracloud-jarvis/ira

python -m scripts.armory_vault show                 # list tools + whitelist
python -m scripts.armory_vault export --out /tmp/v  # decrypt files for editing
$EDITOR /tmp/v/manifest.json                        # add tools / edit whitelist.txt
python -m scripts.armory_vault import --in /tmp/v   # re-validate + re-seal
rm -rf /tmp/v                                        # the plaintext copy is yours to clean up

python -m scripts.armory_vault rotate-key           # re-seal under a new Master Key
```

`import` re-validates the manifest and whitelist before sealing, so a broken edit is
rejected rather than written. `rotate-key` preserves your contents and updates the
`.env` verifier — a wrong current key leaves the vault untouched. The vault ships
with **~100 starter tools**; grow it by adding manifest entries.

### Tool schema

```json
{
  "id": "nmap_scan",                 // lowercase/underscore, unique
  "category": "recon",               // free-form grouping
  "image": "kali",                   // "kali" | "tor"
  "cmd": "nmap -sV -Pn {target}",    // {placeholders} filled from sanitised args
  "risk": "low",                     // low | medium | high | critical
  "network": "tor",                  // "none" (offline) | "tor" (egress via Tor)
  "description": "…"
}
```

- **`cmd` is built into an argv list, never a shell string.** Arguments are
  rejected if they contain whitespace, shell metacharacters, or a leading `-`
  (flag injection). `{target}`/`{url}`/`{rhost(s)}`/`{host}` values are treated as
  **network targets** and checked against the whitelist; other placeholders
  (`{module}`, `{query}`, `{userlist}`, …) are not.
- **`risk`** drives the human-in-the-loop gate: `high` needs 2FA approval,
  `critical` is blocked outright.
- **`network`** drives the killswitch: `none` → `--network none`; `tor` → egress
  only through the Tor sidecar (Kali tools are proxychained through it).

The vault ships with ~100 tools; grow it by adding entries. Note: injected argument
values must be single shell-safe tokens (no whitespace or shell metacharacters), so
a tool whose arguments need those characters isn't a good fit for the manifest.

### Whitelist entries

One per line in `whitelist.txt` (case-insensitive; `#` comments ignored):

```
example.com            # exact host
*.example.com          # wildcard (fnmatch)
192.0.2.10   ::1       # single IP
192.0.2.0/24           # CIDR range
localhost  127.0.0.1   # localhost aliases
```

A target is authorized only if it matches at least one entry. URLs are reduced to
their hostname before matching. An empty whitelist authorizes **nothing**.

---

## 5. Security precautions

- **Never commit the vault or the key.** `data/armory/` and `**/vault.sealed` are
  git-ignored; `.env` (with the verifier) already is. The plaintext key is never
  written anywhere.
- **The whitelist is a rail, not authorization.** Only add hosts you are
  permitted to test.
- **External egress is double-gated.** With `IRA_ALLOW_EXTERNAL_API=false`, only
  local/private targets run; flip it on only when you intend to reach public
  hosts you're authorized to test.
- **Output is ephemeral.** Tool output is returned to you and then gone —
  containers are `--rm` with no volumes. Only the audit fact (which tool ran,
  exit code, timestamp) is recorded, in the tamper-evident Trust Ledger.
- **Auto-lock is on by default.** The vault re-locks after each task and after
  `ARMORY_SESSION_TTL_SECONDS` of inactivity. Tune, don't disable.
- **Brute-force lockout.** After `ARMORY_MAX_UNLOCK_ATTEMPTS` wrong Master Keys the
  Armory refuses all unlock attempts for `ARMORY_UNLOCK_LOCKOUT_SECONDS` — even the
  correct key — and logs it. (PBKDF2 at 600k iterations already makes each guess slow.)
- **Master switch.** `ARMORY_ENABLED=false` (the default) makes every entry point —
  agent, API, unlock, execute — refuse fail-closed. It must be explicitly turned on.
- **Kill switch.** `ARMORY_DOCKER_ENABLED=false` (or `ARMORY_EXECUTION_BACKEND`) bounds
  how tools run; `ARMORY_ALLOW_UNSANDBOXED` stays off unless you accept host execution.

### Threat model (what this does and doesn't defend)

| Threat | Mitigation |
| ------ | ---------- |
| Vault theft at rest | AES-256-GCM; without the key it's authenticated ciphertext |
| Key in `.env` | Only a one-way PBKDF2 verifier is stored, never the key |
| Master-Key guessing | 600k-iteration PBKDF2 + lockout after N failed attempts |
| Command injection via args | argv construction + metacharacter/flag rejection |
| Scanning out-of-scope hosts | fail-closed whitelist on every target |
| Data exfiltration by a tool | `--network none` or Tor-only; no volumes; no direct internet |
| Destructive/high-impact tools | `critical` blocked, `high` needs 2FA, all previewed |
| Silent misuse | every action hash-chained into the Trust Ledger |
| Non-owner access | owner-gate on chat/voice/API; `require_owner` dependency |

**Not** claimed: this is not a substitute for OS-level isolation on a shared box,
and the audit chain is only tamper-_evident_ when its head hash is anchored
outside the database (`trust.ledger.export_head`).

---

## 6. Tests

`ira/tests/armory/` covers the manifest (incl. injection rejection), the
whitelist matcher, the session auto-lock state machine, the encryption
verifier/round-trip, the Docker argv builder (killswitch + hardening), the setup
`.env` writer, and the full gatekeeper gate pipeline. The AES round-trip and the
real-vault unlock use `pytest.importorskip("cryptography")`.

```bash
cd supracloud-jarvis/ira && pytest tests/armory -q
```

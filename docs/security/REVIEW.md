# Security review of the integrated stack, 2026-11-25

Scope: the care backend (`supracloud-jarvis/ira`: `care`, `core`, `api`, `auth`, `security`,
`worker`, `app`), the carer dashboard (`dashboard`), and the family app (`mobile`). This is
an engineering review, not a penetration test or an external audit: those still need a
qualified third party (register C-22).

The threat model that completes this review's scope, STRIDE per component with every control
tied to its code and test, is `THREAT_MODEL.md` (25 November 2026).

## What was run

| Check | Tool | Result |
|---|---|---|
| Static analysis, backend | bandit 1.x over the modules above, tests excluded | 0 high. 26 medium "SQL built from strings" (B608), all reviewed by hand below; 1 medium `urlopen` in billing (fixed URL, not user input); the rest low |
| Python dependencies | pip-audit over `requirements.lock` | no known vulnerabilities |
| Dashboard dependencies | `npm audit --omit=dev` | before: 1 high (PostCSS 8.4.31, bundled with Next.js), 1 moderate; **after: 0** |
| Family app dependencies | `npm audit --omit=dev` and `npm audit` | first pass: 23 (1 critical, 5 high) down to 20 (1 critical, 3 high). Second pass (S6): before 20 (1 critical, 3 high, 16 moderate) with `--omit=dev`, 21 with the development tree (one more moderate, `jest-expo`); **after: 0 in both** |
| Secrets in history | gitleaks (in CI on every PR) | passing |
| Unauthenticated routes | AST walk over `care/routes.py` | three at the review; four since G3.d added the signed monitoring-centre callback (listed below) |
| Dashboard Content-Security-Policy in a browser | `scripts/security/csp_browser_check.mjs` (Chromium through Playwright) against a production build (`npm run build && npm start`) and the demo API with seeded fictional people | 21 page loads (sign-in, sign-up, join, the 404 for `/watch`, overview, alerts with an open Help request, all eleven tabs of the person record including the live care tab, visits, team, search, billing): 0 policy violations, every page showed its content; the three negative controls were refused (S7). By hand, the same way: `/watch`, `/presenter` and `/simulator` in a demonstration build, and three pages under `next dev`: 0 violations |

## Findings and what was done

| ID | Severity | Finding | Done |
|---|---|---|---|
| S1 | Medium | `POST /care/devices/pair` and `POST /care/signup` take no login and had no rate limit. A pairing code has 40 bits and lives 15 minutes, so guessing is infeasible, but every guess was a free database lookup, and sign-up creates rows. | Rationed per client address (the same `rate_limit_key` the auth routes use): pairing 10 a minute, sign-up 3 a minute, the same budget as `/auth/register`. A refused request never reaches the database. `tests/care/test_care_public_rate_limits.py` (fails against main). |
| S2 | Medium | The dashboard sent no security headers, and its session token is in `sessionStorage`. Any site could frame it. | Every response now carries `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, a permissions policy (camera, location, payment, USB off; microphone for this origin only, for the browser watch's voice), HSTS, and no `X-Powered-By`. Checked on a built server with `curl -I`. `security_headers.test.ts` (fails against main). |
| S3 | High (build-time) | PostCSS below 8.5.10 (XSS in stringified CSS; file read via `sourceMappingURL`), bundled by Next.js 15.5 in the dashboard and by Expo in the family app. Only our own CSS goes through it, so not reachable by a user, but it ships in the build chain. | npm `overrides` pin PostCSS 8.5.33 in both. Dashboard builds; 73 tests pass. |
| S4 | High (build-time) | `@xmldom/xmldom` 0.7.13 (XML injection, recursion DoS) under Expo's plist handling. | Override to 0.8.15 (same API). Family app: 75 tests pass, `tsc` clean, `expo config` resolves. |
| S6 | Critical and high (build-time) | The family app's remaining advisories, all in Expo SDK 52's command-line tools and the Metro bundler, which run on the developer's machine and in CI, not on a phone. See the table below. | npm `overrides` pin patched versions (`tar` 7.5.23, `image-size` 2.1.0, `glob` 14.0.6 where Expo asked for glob 10, `sucrase` 3.35.1, `uuid` 11.1.1). Two of them changed shape, so `patch-package` (pinned 8.0.1, run on `postinstall`) adapts the three callers: `mobile/patches/`. `npm audit`: 0. Checked below. |
| S5 | — | Dynamic SQL (bandit B608) in the care backend. | Reviewed by hand: every column name comes from a fixed allow-list in the module (`_MEMBER_FIELDS`, `_ROUTINE_FIELDS`, `_MED_FIELDS`, `_SETTINGS_FIELDS`, the visit `allowed` set, retention's `PURGED`, the access-note `COLUMNS`) and every value is a bound parameter. No change needed. |
| S7 | Medium | The dashboard had no script policy: an injected script could read the session token from `sessionStorage` and send it anywhere. | `dashboard/src/middleware.ts` gives every page a Content-Security-Policy with a fresh 128-bit nonce (`dashboard/src/lib/securityHeaders.ts`): `script-src 'nonce-…' 'strict-dynamic'` with no `'unsafe-inline'` and no `'unsafe-eval'` (eval, and inline `<style>` elements for its error overlay, are added only under `next dev`); `connect-src` is this origin plus the configured API origin (`NEXT_PUBLIC_CARE_API_BASE_URL`; nothing extra when it is empty, the production image's same-origin set-up); `object-src 'none'`, `base-uri 'none'`, `form-action 'self'`, `frame-src 'none'`, `frame-ancestors 'none'`, `img-src 'self'`, and `upgrade-insecure-requests` when the page is served over HTTPS (directly, or through Caddy, which says so in `X-Forwarded-Proto`). Styles: sheets from this origin or with the nonce; `style-src-attr 'unsafe-inline'` only, because about sixty React `style={{…}}` attributes are rendered into the HTML. An attribute cannot run script; what it leaves is restyling of injected markup. The root layout reads the request headers, so every page renders per request and Next.js puts the nonce on its scripts (all routes, the live person record, alerts and emergency views included, are now dynamic). `csp.test.ts` (11) and `security_headers.test.ts`; 4 fail against the old middleware, and the policy check rejects a policy with `'unsafe-inline'` or `'unsafe-eval'` script, a missing nonce, an open `connect-src` and the old policy. In a browser: no violations on 21 page loads; an inline script without the nonce added to a page's HTML did not run, a `fetch` to another origin and a string `setTimeout` were refused. |

### S6 in detail: family app build tooling, 25 November 2026

Expo SDK 52.0.49, React Native 0.76.9, `npm ci` on Node 22.23.5 and npm 10.9.9. Five packages
carried advisories of their own; the other fifteen findings only depended on them. "In the
app bundle" was checked against the source map of `npx expo export --platform android
--dump-sourcemap` (732 modules from 76 packages): none of these packages is in it, and none is
native code.

| Package (before) | Advisories | Reached through (`npm ls`) | Where it runs | After |
|---|---|---|---|---|
| `tar` 6.2.1 | critical GHSA-23hp-3jrh-7fpw (unbounded decompression); high: hardlink and symlink path traversal (GHSA-34x7-hfp2-rc4v, -8qq5-rm4j-mr97, -83g3-92jg-28cx, -qffp-2rhf-9h96, -9ppj-qmqm-q256), APFS race (-r6q2-hw4h-h46w), infinite loop (-8x88-c5mf-7j5w), stack overflow (-r292-9mhp-454m); moderate: -vmf3-w455-68vh, -w8wr-v893-vjvp, -gvwx-54wh-qm9j | `expo` > `@expo/cli` 0.22.28 (and its `cacache` 18.0.4) | build: `expo prebuild` unpacks the project template from npm with it | 7.5.23 |
| `image-size` 1.2.1 | high GHSA-5p2g-fcmc-qvqq (JXL, HEIF infinite loop), GHSA-w3rx-r6r6-pgpr (ICNS infinite loop) | `react-native` > `@react-native/community-cli-plugin` > `metro` 0.81.5 | build: Metro sizes image assets | 2.1.0 |
| `glob` 10.5.0 | moderate GHSA-3g3m-2fjr-6pw4 (traversal continues after an abort) | `@expo/cli`, `@expo/config`, `@expo/config-plugins`, `@expo/metro-config`, `cacache`, `sucrase` | build | 14.0.6 (the `glob` 7.2.3 under Jest is outside the advisory's range and unchanged) |
| `minimatch` 9.0.9 | moderate GHSA-g3hv-75wg-m249 (pattern amplification) | `glob` 10.5.0 | build | gone with glob 10; glob 14 brings 11.0.9 |
| `uuid` 8.3.2, 7.0.3 | moderate GHSA-w5hq-g745-h8pq (missing bounds check when a buffer is passed; Expo's callers pass none) | `@expo/cli` > `@expo/rudder-sdk-node`, `@expo/bunyan`; `@expo/config-plugins` > `xcode` | build | 11.1.1 |

Findings that only depended on those, and cleared with them: `@expo/cli` (high, through
`tar`), `cacache` (high, through `tar`), `@expo/config`, `@expo/config-plugins`,
`@expo/metro-config`, `@expo/prebuild-config`, `@expo/bunyan`, `@expo/rudder-sdk-node`,
`sucrase` 3.35.0 (pinned to 3.35.1, which no longer uses glob), `xcode`, and the app's direct
dependencies `expo`, `expo-asset`, `expo-constants`, `expo-linking`, `expo-notifications` and
(development only) `jest-expo`. Those were flagged because they reach `@expo/config` at build
time; their runtime code in the bundle carries no advisory.

Why not the alternatives. `npm audit fix --force` would install Expo SDK 58: six SDK versions
and several React Native versions on, with the native modules (speech, speech recognition,
notifications, secure store, image picker) to re-check on devices. An SDK upgrade is still the
clean end state, but it is a product change, not a security patch. A plain override is not
enough for two packages: `tar` 7 has named exports and no default, so `@expo/cli`'s
`tar.default.extract` fails and `expo prebuild` cannot unpack its template; `image-size` 2 takes
a buffer, not a file path, so Metro fails on the first image. The three patched lines in
`mobile/patches/` fix exactly that and nothing else. When Expo is upgraded, drop the patch
files whose package version no longer matches (patch-package names the version in each file and
fails `npm ci` loudly if it no longer applies) and drop the overrides the new SDK satisfies.

Checked on this branch:

- `npm ci` applies both patches; `npm audit` and `npm audit --omit=dev`: 0 vulnerabilities.
- `npx tsc --noEmit`: clean. `npx jest --ci`: 79 tests in 17 suites pass (75 before, plus
  `build_tooling_overrides.test.ts`: the pinned versions, Metro sizing an image from a buffer and
  from a path, and Expo unpacking an npm tarball with the template renames prebuild relies on).
  With the patches reversed (`npx patch-package --reverse`) three of the four fail. One full run
  of the 79 failed one test while Gradle was running alongside; eight more runs passed. The
  same unexplained flake is recorded below.
- `npx expo export --platform android`: bundles 732 modules. `npx expo prebuild --platform
  android --no-install`: passes, through the JS `tar` path. Gradle
  `:app:createBundleReleaseJsAndAssets`: passes (Metro run by the Android build).
- `./gradlew assembleRelease -PreactNativeArchitectures=arm64-v8a`: Gradle configured the project
  (Expo autolinking found every module) and then stopped where it always stops in this sandbox:
  `com.github.CanHub:Android-Image-Cropper:4.3.1`, needed by `expo-image-picker`, is only on
  jitpack.io, which the sandbox's proxy refuses (`docs/evidence/builds/ANDROID.md`). The patches
  touch only Node build tools, not native code; CI's `mobile-android` job, which can reach
  jitpack.io, is where the APK is built.
- iOS: not built. It needs macOS and Xcode, which this Linux container does not have.

What stays: nothing that `npm audit` reports. The patches are local edits to two build tools
that Expo and Meta do not test with these versions; the tests above and CI's prebuild and
release build are what guard them.

## Reviewed and sound

- The unauthenticated care routes are sign-up, pairing, and two provider callbacks: Twilio's status callback and, since G3.d, the monitoring centre's acknowledgement (`/care/providers/arc/status/...`), refused unless our token in its address and the centre's timestamped HMAC signature over the body both verify.
  The callback is refused unless both the per-deployment token in its URL and Twilio's
  signature over the full URL check out (REMEDIATION A2).
- Workspace isolation is PostgreSQL row-level security with FORCE on every care table, plus
  person-level checks in `care/authz.py` that answer "not found" (REMEDIATION E1, D-076).
- Pairing codes and device tokens come from `secrets` (not `random`); only SHA-256 digests
  are stored. The `random` uses bandit flagged are greeting variety and retry jitter.
- The server refuses to start with a published default secret
  (`core/runtime/published_credentials.py`).
- The care server exposes no OpenAPI or docs pages; CORS is an explicit list from
  `IRA_CARE_ALLOWED_ORIGINS`, empty by default.
- Demo, presenter and simulator pages are off in production builds (REMEDIATION B6, PR #67).

## Residual risk, not fixed here

- **Family app build tooling.** Cleared by S6 (overrides and three patched lines), not by an
  Expo SDK upgrade. The upgrade is still worth doing for its own sake, and it needs a device
  test pass; when it happens, the patches and overrides come out as S6 describes.
- **Dashboard styles and the sign-in server field.** The policy allows inline style attributes
  (S7). It also means a built dashboard can talk only to the API it was built for: the "Care
  server" field under Advanced cannot point a production build at another server. That is
  intended; a deployment with a second API origin needs it added to the policy.
- **Not done:** a third-party penetration test; a threat model reviewed by someone outside
  the team; mobile certificate pinning; secret rotation drills.
- One run of the family app's test suite failed once while three other builds ran on the
  same machine, then passed four times in a row. Recorded, not explained.

## Round 7 review, 25 November 2026

Scope: what changed on `main` since the review above (from `c8dc089` to `62093e4`): the
dashboard's nonce policy (#98), deployment administrators and signed device instructions
(#103: `care/deviceadmin.py`, `care/devicepolicy.py`, migration 122, the watch's
`DevicePolicy.kt`), activity periods and carer trends (#102: migration 121, consent
`activity_sharing`), the research heart-rate route (#104: `care/heart_research.py`), the WAV
decoder that replaced PyAV (#100: `voice/audio_decode.py`), the signing-keys guard (#96) and the
family app's overrides (#97). Every review area of the owner's directive (section 14) was looked
at again; where nothing changed since round 6, this says so and the existing tests were re-run
rather than the code re-read line by line.

**This is an internal engineering review. It does not replace an independent penetration test
by a qualified tester, which is still needed: C-22.d stays `blocked_external`.**

### What was run

| Check | Command | Result |
|---|---|---|
| Static analysis, backend | `bandit -r care core api auth security worker app voice -x tests` (bandit 1.9.4), the same command over `c8dc089` for a like-for-like baseline | `c8dc089`: 0 high, 32 medium, 64 low. Now: 0 high, 33 medium, 64 low. The one new medium is B608 at `care/deviceadmin.py` (`_ISSUER_REVOKED`, a constant SQL fragment with no input in it): sound. (Round 6 counted 26 B608 over a narrower scope with an older bandit; the baseline above is the comparison that holds.) Care and voice alone: 14 medium (all B608), 17 low |
| Python dependencies | `pip-audit -r requirements.lock --no-deps --disable-pip` (a plain run now fails: faster-whisper declares PyAV, which the lock deliberately leaves out, so pip refuses to resolve it); the same over `requirements-nodeps.txt`; and over what the API image actually resolves (`pip install --dry-run --report` of the Dockerfile's command line, 126 packages) | lock: no known vulnerabilities. nodeps: none. API image set: `pytest` 8.3.3, PYSEC-2026-1845, fixed in 9.0.3 (S14) |
| Dashboard dependencies | `npm ci`, `npm audit --omit=dev`, `npm audit` | production: 0. With the development tree: 3 (2 critical, 1 moderate), all in the test runner (S13) |
| Family app dependencies | `npm ci`, `npm audit --omit=dev`, `npm audit` | 0 and 0 (S6 holds) |
| Secrets in history | `scripts/ci/gitleaks.sh` (gitleaks 8.24.3, checksum verified) | 113 commits, no leaks |
| Database | fresh database, roles provisioned, all migrations applied as `ira_migrator`, `check_migration_integrity` | 119 files, hashes match. No `SECURITY DEFINER` function in `public`; the only new function (the instruction guard trigger) pins `search_path`. All four new tables have row-level security enabled and forced |
| Tests re-run | listed under "Verified" below | all pass |

### Findings

| ID | Severity | Finding | Done |
|---|---|---|---|
| S8 | Medium | The WAV decoder sized its conversion filter from the header's sample rate. A rate in range whose ratio to 16 kHz does not reduce, such as 383,999 Hz, asked for a 12,288,000-tap filter: a 46-byte file cost 2.6 s and a peak of about 1.1 GB, before any sample was read, on every authenticated speech route (`/voice/transcribe`, `/audio/transcribe`, chat uploads). Measured with `tracemalloc`. | The filter's size is computed from the header and refused (`UnsupportedAudio`, so 415 or the plain refusal) above 524,288 taps, before any work, in `decode_to_16k_mono`, `decode_audio` and `resample`. Every common rate needs at most 20,480 and 4,001 Hz needs 512,000, so nothing a recorder makes is refused. After: the same file is refused in under a millisecond with no allocation. The worst accepted upload (25 MB of 384 kHz, the route's cap) still costs 2.7 s and 246 MB, as before: that is the route's own size and length limit, not a header lie. `test_audio_decode.py` (fails against `main`). |
| S9 | Low | The same parser walked any number of chunks before the data (a 25 MB file of empty chunks: 3.3 million iterations, 1.6 s), and a truncated RF64 size chunk raised `struct.error` rather than `UnsupportedAudio` (the routes answered 422 either way). Sizes themselves were sound: chunk sizes are unsigned, Python integers do not overflow, and the declared (including 64-bit RF64) data size never drives an allocation, only the bytes present are read. | At most 1,024 chunks before the data; the RF64 size chunk is bounds-checked. Tests for both, and a control that a 2^62-byte declared RF64 size reads only the four samples present (fail against `main`, except the control). |
| S10 | Medium | The care server had no ceiling on a request body, and neither has the reverse proxy (`deploy/Caddyfile`). FastAPI reads and parses the whole body before the schema, and before the rate limit of the two routes that need no sign-in (sign-up and pairing), so one request of a few gigabytes could exhaust the API's memory. Present since before round 6. | `care/body_limit.py`, outermost in `care/server.py`: 16 MiB by default (`IRA_CARE_MAX_REQUEST_BYTES` changes it). A larger declared `Content-Length` is answered 413 before anything is read; a chunked body is counted as it arrives and answered 413 the moment it passes the ceiling, holding no more than the ceiling. The largest legal care body, a research recording at its sample caps, is under 12 MB. `test_care_body_limit.py` (the declared and streamed cases fail against `main`; controls show smaller bodies still reach the route). Recommended, not done: the same limit in Caddy (`request_body { max_size 16MB }`), once it can be checked against a running Caddy; the full IRA server (`app/factory.py`), which a care deployment does not run, reads its uploads with its own caps. |
| S11 | Low | The research route's accelerometer rows (`list[list[float]]`) had no bound on a row's length; the contract check refused a wrong width only after the whole body had been parsed. | Rows are exactly three numbers in the schema (`care/schemas.py`). Test in `test_care_body_limit.py` (fails against `main`). |
| S12 | Medium | `IRA_DEVICE_POLICY_KEY`, the key that lets a deployment administrator open a maintenance window on, or factory-reset, a watch (#103), was missing from the custody document (`SIGNING_KEYS.md`), and the guard (#96) checks a hand-written list, which missed it. | The key is in the custody table, with how it is made (`scripts/deployment_admin.py keygen`) and how it is rotated after an exposure. The guard now also reads the code: every `…KEY_ENV = "IRA_…"` constant in `care/` must be in the list and in the document (the new test fails against the old document; a negative control shows an undocumented key is reported). |
| S13 | Low (development only; advisories rated critical) | The dashboard's test runner: `vitest` 3.2.7 brings `tinypool` 1.1.1 (critical GHSA-5gmw-xhrv-c9v3 and GHSA-85c8-ppgw-ccpr, prototype-pollution gadgets in worker options) and `@vitest/mocker` 3.2.7 (moderate GHSA-82fw-gwwq-j7x9, GHSA-qxg6-89mg-rqv8, in browser-mode mocking, which this project does not use). None of it is in the built dashboard (`--omit=dev`: 0); it runs our own tests on a developer's machine and in CI. | **Fixed after the round (25 November 2026).** The dashboard runs `vitest` 4.1.12, which no longer uses `tinypool` and brings the patched `@vitest/mocker`. npm 10.9.9 had crashed (`edgesOut`) because it chose `vite` 8 for `vitest` and then loaded `vite` 8's optional development-tools peers, one of which asks for the newest `vitest`; `vite` is now a direct development dependency at the 7.3.8 already locked, and `overrides` holds `vitest`'s `vite` to it (`"vite": "$vite"`). The lock was updated in place (15 packages changed or added, 10 removed, all test-runner internals), resolved with `npm install --before=2026-11-11` so nothing under two weeks old came in. With Node 22 and npm 10.9.9: `npm ci` clean, `npm audit` 0 (development included), `npx vitest run` 130 passed, `npx tsc --noEmit` clean, `npx next build` succeeds, the dashboard licence check passes (production tree, unchanged). |
| S14 | Medium (supply chain) | The care API image (`deploy/Dockerfile.api`) installs `requirements-test.txt`, not the hashed `requirements.lock` the audit covers. Since #100 four of its lines are ranges (`huggingface-hub>=0.21`, `tokenizers`, `onnxruntime`, `tqdm`), so the image's tree is decided at build time: today it resolves `huggingface_hub` 0.36.2 and `tqdm` 4.70.1, where the lock says 1.27.0 and 4.70.0 (and the Dockerfile's `tokenizers` 0.21.0 needs `huggingface_hub` below 1.0, so the lock's version could not be installed there). It also ships test tools in production, among them `pytest` 8.3.3 (PYSEC-2026-1845; fixed in 9.0.3; reachable only if someone runs pytest in the container). | Not fixed here: pinning changes what the production image runs, and no image can be built in this environment to prove it. Recommended: a hashed lock for the API image (its own, resolved for Python 3.12 and the Dockerfile's `onnxruntime` and `tokenizers` pins), audited in CI, with the test tools left out. |
| S15 | Medium | A deployment administrator needs only an ordinary session to issue `FACTORY_RESET` to a watch. A phished password, or a stolen session, can wipe a person's safety device; there is no second factor and no second person. Mitigated: the grant is held by few accounts and written only by the operator, every instruction is audited, revocable, single-use and lasts at most 15 minutes, and the watch is not yet provisioned as a device owner (E4.f). | Not fixed: step-up authentication (TOTP, already in the codebase) or a two-person rule for `FACTORY_RESET` is a product decision. Recorded as T43, not controlled. |

Also corrected: the threat model's T27 still said the OTA server was not on `main`. It is
(`care/ota.py`, rollback index never lowered, `test_ota.py`); the row now says so, and that the
device side is unverified.

### Reviewed and sound, by area

- **Authentication, session renewal, token security, encryption, emergency storage, backups:**
  no change to `auth/`, `core/identity/`, `security/` or the backup scripts since round 6.
- **Tenant isolation and database permissions:** `care_activity_periods`,
  `care_device_admin_instructions` and `care_device_admin_events` carry the strict
  tenant-and-workspace policy, forced; the runtime cannot update an activity hour or an event,
  and cannot change a signed instruction's signed columns or un-revoke one (trigger, with
  `search_path` pinned). `deployment_administrators` is forced too: the runtime can read only
  its own live grant and revoked grants, and writes nothing even after `GRANT ALL`. All four are
  in the tenancy manifest and the erasure classification.
- **Person-level authorisation:** the activity trend is behind `person_guard` and
  `ensure_can_read`, needs `activity_sharing` consent, and the read is logged.
- **Deployment-administrator isolation and device identity:** only a live grant counts; no care
  or tenant role and not `is_owner`. The administrator's work runs on a connection bound to the
  named workspace, so a device or instruction from elsewhere is "not found". A watch reads only
  its own instructions and reports outcomes only for them, forward only. On the watch: the
  signature is checked over the exact bytes before anything is parsed, then device id, a
  15-minute window, expiry, revocation and single use (nonces kept across reboots); three
  actions only.
- **API security:** the research run is rate-limited (10 a minute), care managers only, takes no
  person and opens no database connection; sample names are a fixed pattern looked up in a list,
  so no path reaches the file system. Activity batches are at most 336 hours; instruction
  bodies are bounded. Error messages name the contract rule broken, never another record.
- **Browser security:** the nonce policy (S7) is unchanged; the research page is a normal
  signed-in page, not a demonstration page; demonstration pages still answer 404 unless the
  build opts in (`demo_pages.test.ts`).
- **Prompt injection:** the conversation path is unchanged; its safety and evaluation tests
  (20 injection turns in `tests/care/eval/turns.jsonl`) pass.
- **Sensitive logging:** the new modules log identifiers and counts only: the deployment
  administrator refusal (account and tenant id), instruction issue and revoke (ids and action),
  the research run (workspace id and window count). `care/activity.py` and
  `voice/audio_decode.py` log nothing; the speech routes log the format refused, never a
  transcript.
- **Retention and erasure:** activity hours go with the retention purge, the person and the
  tenant; instructions and their events go with the watch (and so with the person) and the
  tenant; a deployment administrator's grant goes with the account.
- **Production and demonstration isolation:** the research route is isolated by test
  (`test_the_research_pipeline_touches_no_database_and_no_person`,
  `test_the_research_route_opens_no_connection_and_takes_no_person`).
- **Signing-keys guard and Expo overrides:** both hold (S12 extends the guard).

### Verified

From `supracloud-jarvis/ira`, on this branch:

- `python3 -m pytest -q tests/test_audio_decode.py`: 77 passed (6 new). With `voice/audio_decode.py`
  from `main`: 4 of the 6 fail, the three regression tests and the control that reads the new
  limit; the other two controls pass.
- `python3 -m pytest -q tests/care/test_care_body_limit.py tests/care/test_care_research_route.py tests/care/test_care_public_rate_limits.py tests/care/test_care_server.py`:
  25 passed. With `care/server.py` and `care/schemas.py` from `main`: 3 of the 5 new tests fail.
- `python3 -m pytest -q tests/readiness/test_signing_keys.py tests/architecture/test_governance_chokepoint.py`:
  22 passed. With `SIGNING_KEYS.md` from `main`: 2 fail.
- On a fresh database: `tests/tenancy/test_device_admin_db.py`, `test_care_activity_db.py`,
  `test_3c06_seventeen_tables.py`, `test_classification_manifest.py`,
  `test_3c10_unscoped_tables.py`, `tests/readiness/test_erasure_coverage.py`,
  `tests/tenancy/test_role_separation.py`, `tests/care/test_activity_no_alert_path.py`,
  `test_devicepolicy.py`, `test_heart_research.py`, `test_care_research_route.py`: 618 passed.
  Conversation safety (`test_care_eval_runner.py`, both `test_care_safety_answers` suites): 177 passed.
- `tests/readiness/test_threat_model_traceability.py`: 6 passed.
- Dashboard: `npx vitest run` 130 passed, `npx tsc --noEmit` clean (no dashboard change).

### Residual risk after round 7

- S14 and S15 above, with what each needs (S13 is fixed).
- No body limit at the reverse proxy (S10).
- The device-policy key is accepted whatever its value: the server cannot tell a test key from
  a real one. What protects a watch is the public key its build pins (E4.f, not built yet);
  the custody procedure (S12) is what keeps a test key out of a deployment.
- Everything in "Not done" above still stands: no third-party penetration test, no external
  review of the threat model, no mobile certificate pinning, no secret rotation drill.

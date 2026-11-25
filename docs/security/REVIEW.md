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

### S15 status: factory reset by one deployment administrator, partly fixed 25 November 2026

S15 is recorded in the round 7 review (threat T43): one deployment administrator with an
ordinary session could have a watch factory-reset. A factory reset now needs two people (D-089,
migration 123): it is stored unsigned as a request and signed only when a different deployment
administrator with a live grant approves it within 30 minutes. Self-approval, a lapsed or
withdrawn request, and a request whose requester has since been revoked are refused and audited;
revoking either administrator withdraws a signed reset from the watch; a reset signed on one
person's say-so before 123 is withheld; and the table refuses all of this on its own (trigger and
CHECK constraints). `tests/tenancy/test_device_admin_db.py`: seven new tests (eight cases with
the parametrised pair), all failing against `main`. Still open: step-up authentication. The
session token carries no authentication time, so the server cannot ask for a recent sign-in or a
second factor before a reset is requested or approved; two stolen sessions can still do it.

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

# Step-up authentication (D-095)

**What it is.** The server can now tell a session whose holder typed their password a minute ago
from one kept alive by refreshes for a week, and can insist on the former before a deployment
administrator touches a watch (security review S15, threat T43; closes the part D-089 left open).

**Where it applies.** Issuing, approving and revoking a device instruction
(`supracloud-jarvis/ira/care/routes.py`, the `_deployment_admin_recent` dependency). Nothing else
asks for it yet. Reading the instruction history does not.

## The claims

Every access and refresh token (`supracloud-jarvis/ira/api/middleware/auth.py`) carries two more
claims, both optional so that a token issued before they existed still decodes:

| Claim | Meaning |
| --- | --- |
| `auth_time` | Unix seconds of the moment the holder last proved who they are with a credential |
| `amr` | How, OIDC names: `pwd` (password), `otp` (a one-time code from the authenticator), `recovery` (a recovery code) |

- **Sign-in** (`POST /auth/token`, `POST /auth/cloud/login`) sets `auth_time` to now and `amr` to
  what it checked: `["pwd"]`, `["pwd","otp"]` when a code was verified, `["pwd","recovery"]` when
  a recovery code was consumed. Both tokens get the same values.
- **Refresh** (`POST /auth/refresh`) COPIES both from the refresh token into the new access token
  and never re-stamps them. A refresh proves possession of a token, not that a person is there.
  A refresh token without the claims mints an access token without them.
- **Step-up** (`POST /auth/step-up`, below) sets `auth_time` to now on a new access token only.

`Principal` (`supracloud-jarvis/ira/core/identity/principal.py`) carries them as `auth_time` and
`amr`, and `authentication_strength` is derived: `mfa` when `otp` or `recovery` is present,
`password` when only a time is known, `""` for a token from before the claims. They are facts the
token asserted, never authority: no role, scope, tenant or workspace is decided from them.

## The endpoint: `POST /auth/step-up`

Authenticated with the current access token; rate-limited like sign-in (5 a minute); body
`{"password": "...", "totp_code": "..."}` (`totp_code` optional; required when a factor is on).

1. The account is the **token's subject**. The body cannot name one, so a correct password for
   some other account proves nothing (`security/reauth.py::verify_account_password`, the same
   bcrypt check sign-in performs; 503 when the credential store cannot be reached).
2. A locked account (the sign-in lockout, same key) is refused before bcrypt (429).
3. If the account has an **enabled** second factor, a current code is required: missing or wrong
   is 401 with `second_factor: totp`; "cannot read the enrolment" is 503, never "no factor".
4. Every refusal is counted against the sign-in lockout and recorded as an auth event
   (`sign_in_refused`, detail `step-up refused: password|totp`); the password and the code are
   never logged. Success clears the counter and records `sign_in_succeeded` with detail
   `step-up: pwd[,otp]`.
5. The answer is a new **access token** for the same session: same `sub`, `tid`, `uid`, `ver`,
   the ordinary access-token expiry, `auth_time` now, `amr` `["pwd"]` or `["pwd","otp"]`, plus
   `auth_time` and `amr` in the body. **No refresh token**, so the recent proof cannot be kept
   alive by refreshing it.

A revoked session (token version behind the counter after "log out everywhere"), a refresh token,
or a request that carried no verified token (DEV_MODE) is refused before the password is looked at.

## The check: `security/stepup.py`

`require_recent_authentication(max_age_seconds=None, require_mfa_if_enrolled=True)` is a FastAPI
dependency; `assert_recent_authentication(principal)` is the same check for a route that has its
own authorisation to do first. It refuses with **403** in the API's error envelope,
`error_code: step_up_required` (`core/runtime/errors.py`, "This action requires
re-authentication."), and `safe_details`:

| Field | Meaning |
| --- | --- |
| `reason` | `no_authentication_time` (a token from before the claims, or a session that never passed through `require_auth`), `authentication_too_old`, `second_factor_not_shown` |
| `max_age_seconds` | the window |
| `auth_age_seconds` | how old the proof is, when known |
| `how` | `POST /auth/step-up` |

The window is `IRA_STEP_UP_MAX_AGE_SECONDS` (`config.py`): 60 to 3600 seconds, refused at start
outside that range, default 600. A proof exactly `max_age_seconds` old still passes; one second
more does not. "Enrolled" means an enabled, unrevoked `totp_secrets` row for the account, read on
the caller's own bound connection; if it cannot be read the answer is 503, never "not enrolled".

**Order of checks on the administrator routes.** The deployment-administrator grant first, then
step-up. An account without a grant gets exactly the 403 it always got ("This needs a deployment
administrator.") and never sees `step_up_required`. The two-person rule for a factory reset
(D-089) is untouched: a fresh session still cannot approve its own request.

`GET /api/v1/care/deployment-admin/me` tells a dashboard what to offer:
`{deployment_admin, auth_age_seconds, step_up_required, max_age_seconds, mfa_enrolled}`; a
non-administrator gets `{"deployment_admin": false}` and nothing else.

## Limits, and what it does not protect against

- It cannot tell the account holder's password from a phished one. An attacker holding the
  password (and the second factor, when there is one) of two administrators within the window can
  still have a watch reset. What it removes is the long-lived stolen session: an access token
  copied from a browser, a proxy or a log, or a refresh token, is useless for these routes once it
  is older than the window.
- A step-up token stolen inside the window works as the holder's would, for up to
  `max_age_seconds` (and never longer than the access token's own 30 minutes).
- The claims are inside the signed JWT and are as trustworthy as the signing key.
- Tokens issued before the claims existed keep working everywhere except the protected routes,
  where they are "never recently authenticated": the holder signs in again or steps up.
- Nothing else is protected yet. Replacing a second factor already asks for the password
  (`api/routes/totp.py`); other destructive actions could adopt the same dependency.

## Where it is proved

| Test | What |
| --- | --- |
| `supracloud-jarvis/ira/tests/test_step_up_authentication.py` | the claims on both tokens; a legacy token decodes; a refresh preserves `auth_time` and copies nothing for a claim-less token; the pure decision and its boundary; the refusal envelope; the setting's bounds and default; the dependency (fresh, stale, legacy, narrower window, enrolled factor, unreadable enrolment); the endpoint with the stores replaced (right password, wrong password counted and never logged, lockout, factor required, another account's password, subject preserved, revoked session, refresh token, unavailable store, rate limit) |
| `supracloud-jarvis/ira/tests/tenancy/test_device_admin_step_up_db.py` | through the real app and database: stale and claim-less sessions refused on issue, approve and revoke and still allowed the history; a non-administrator with a fresh session gets the administrator refusal; an enrolled factor must have been shown; the `me` route; the configured window; a fresh session does not let one person reset a watch; concurrent approvals sign it once; a request still lapses |
| `supracloud-jarvis/ira/tests/tenancy/test_step_up_endpoint_db.py` | real tokens, bcrypt and TOTP: sign-in stamps the moment and a refresh carries it unchanged; step-up with the password mints a recent token for the same session; a wrong password spends the sign-in lockout; the token's own account's password; the code is required and recorded; a signed-out-everywhere session cannot step up; a refresh token cannot, and a step-up issues none |

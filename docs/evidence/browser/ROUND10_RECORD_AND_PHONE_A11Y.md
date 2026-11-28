# The record's failed sections, and the phone app's accessibility, in a real browser (28 November 2026)

`e2e/record_and_phone_a11y.spec.ts`, headless Chromium (Playwright 1.63.0), against the running care
server (`scripts/demo/api.py`, PostgreSQL 16 with pgvector, Redis 7), the production dashboard build
and the phone app's web build (`npx expo export --platform web`), on the build machine. Fictional
people only. **2 of 2 passed** (run at 2026-11-28T02:36:48.015Z).

| Requirement | Test | What it proves | Result |
|---|---|---|---|
| C-10.c | a section that fails to load is named, and the rest of the record still shows | the person's visits request made to fail in the browser: the record says "Some sections could not be loaded (visits)" and still shows the person and the watch line; negative control, with visits working, nothing is named and the record is live | passed |
| C-23.b | axe finds no serious or critical violation on the phone app | axe (WCAG 2.0 and 2.1 A and AA) in the phone app's web build: the sign-in screen, and the home and settings of the family, wearer and carer experiences, each signed in | passed after the fixes below |

## Defects axe found in the phone app, fixed

1. **The password field's autocomplete was not a valid token** (`password`); it is `current-password`,
   which React Native also accepts on Android and iOS.
2. **Names that did not contain the words on screen (WCAG 2.5.3, "label in name").** The Alerts tab was
   named "Alerts (2)" while showing "Alerts" and a "2"; each settings row was named by its title while
   also showing a description. People using voice control say what they see. Both are now named by
   their visible words, and the Alerts count has a hint ("2 need someone"). The shell tests pin it.

## What this does not show

The native accessibility trees (VoiceOver, TalkBack) on a phone: the web build renders the same
components to the browser's accessibility tree, not the platform's.

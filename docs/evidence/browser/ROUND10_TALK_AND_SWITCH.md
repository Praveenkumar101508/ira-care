# Talk as it arrives, and moving between people mid-load, in a real browser (28 November 2026)

Headless Chromium (Playwright 1.63.0), against the running care server (`scripts/demo/api.py`,
PostgreSQL 16 with pgvector, Redis 7), the production dashboard build and the phone app's web build
(`npx expo export --platform web`), on the build machine. Fictional people only. **2 of 2 passed.**

| Requirement | Test | What it proves | Result |
|---|---|---|---|
| C-12.b | `e2e/talk_stream.spec.ts` | the wearer (Ada) types "hello" on the Talk screen of the phone app's web build. The care server's real reply (four lines: three sentences, then `done`) is handed to the app one line at a time, each only when the test releases it. Nothing shows before the first line; after the first line, "Thank you for telling me, Ada." shows, the second sentence does not, and Send still waits; after the rest, all three show in order and Send is ready | passed |
| C-10.b | `e2e/person_switch.spec.ts` | the coordinator opens Ada, whose 15 requests the browser holds back for six seconds, then moves to Bert with the dashboard's own client-side router (the same page stays mounted, as on a link click). Bert shows; Ada's 15 late answers then land; every heading the page showed after the move was recorded, and none was Ada's | passed |

## Controls

- **C-12.b:** the same test with the browser's readable response body switched off, so the app falls
  back to reading the whole reply before showing it, **fails** at "the first sentence shows" (no
  sentence appears until the last line is released). The test tells streaming from waiting.
- **C-10.b:** the guard itself (`dashboard/src/lib/live.ts`, `LiveGate`) is pinned by
  `dashboard/src/__tests__/live.test.ts`. Without it, Ada's late status answer would set the heading
  to Ada's name, which the recorded headings would show. The production build was not rebuilt
  without the guard to demonstrate this.

## What this does not show

- C-12.b: the pacing is done in the browser, because the demo server's model answers in one go; the
  server's own line-by-line streaming is covered by its tests. On a phone, the app reads the body
  through `expo/fetch`, not the browser's fetch; that path is covered by
  `mobile/src/__tests__/talk_screen.test.tsx` and the Android journey, not by this run.

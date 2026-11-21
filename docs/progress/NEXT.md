# IRA CARE: next step

**Round 1 (phases 1 to 9) is merged to `main`.** Round 2 adds real 24/7 safety, location,
fall detection, a model-driven conversation, a hardware-free demo, onboarding and billing,
deployment and store readiness, compliance drafts and a final readiness proof.

**Open branch:** `feat/fall-detection` (round 2, phase 3). Phases 1 and 2 merged as pull requests 10 and 11. Each round-2 phase starts
from the latest `main` after the previous one has merged; no stacked branches.

**Exact next step:** finish `feat/fall-detection` (the watch's accelerometer detector with
synthetic-signal tests, the countdown that asks first, the server's `suspected_fall` alert
with location, per-person settings with an off switch, `docs/DEVICE_SETUP.md`), open its
pull request, merge when green, then start `feat/ira-conversation`.

To continue in a new session: read `docs/progress/STATUS.md`, this file, `docs/BUILD_RULES.md`
and `docs/DECISIONS.md`, then carry on with the same rules.

# IRA CARE: GO / NO-GO by area (requirement L.b)

Updated at each checkpoint; this one is **25 November 2026, `main` at `633572f`**. It refines the
round 5 checkpoint in `FINAL_REPORT.md` area by area. There are three uses:

- **Demo:** an engineering demonstration with fictional people, test notification providers and a
  labelled virtual or browser watch.
- **Pilot:** a supervised pilot with real people under `docs/compliance/PILOT_PROTOCOL.md`.
- **Release:** commercial release to real users.

| Area | Demo | Pilot | Release | Evidence | What blocks the next step |
| --- | --- | --- | --- | --- | --- |
| Care backend (API, worker, escalation, delivery) | GO | NO-GO | NO-GO | `docs/progress/VERIFICATION.md`; `supracloud-jarvis/ira/tests/tenancy/test_care_journey_db.py` | Live SMS and call delivery untested (G3.e); CSO sign-off (I.j) |
| Data isolation and security | GO | NO-GO | NO-GO | `docs/security/REVIEW.md`, `docs/security/THREAT_MODEL.md` | Independent penetration test (C-22.d); no script-src policy on the dashboard |
| Dashboard | GO | NO-GO | NO-GO | `dashboard/src/__tests__/`; the Playwright journey | Accessibility pass on real assistive technology |
| Family phone app | GO | NO-GO | NO-GO | `mobile/src/__tests__/` (the CI native Android build is in an open change) | Signed store builds, iOS build (D2.h), device runs (D2.i) |
| Watch app | GO (browser watch or emulator-free demo) | NO-GO | NO-GO | CI's "Watch app (Wear OS and AOSP)" job in `.github/workflows/test.yml` (builds and JVM tests on every change) | A physical watch (D1.j); release signing key (D1b.c) |
| Fall detection (model v3) | GO | NO-GO | NO-GO | `docs/ai/fall/MODEL_CARD_V3.md` | Device and older-adult evidence (F4.g); owner and clinical approval (D-080) |
| Heart rate | GO | NO-GO | NO-GO | `docs/ai/heart/MODEL_CARD.md` | Accuracy on the IRA watch against ECG (F3.e) |
| Activity, speech | GO (backend model and speech harness only; no alerts use them) | NO-GO | NO-GO | `docs/ai/activity/MODEL_CARD.md` | Older-adult evidence (F8.c, F8.d) |
| IRA CARE OS | NO-GO (the image is unbuilt) | NO-GO | NO-GO | `docs/os/ARCHITECTURE.md` | An AOSP build host (E2.h); the vendor BSP (E6.d) |
| Deployment and operations | GO | NO-GO | NO-GO | `docs/evidence/staging/CONTAINERS.md`, `docs/ops/RUNBOOKS.md` | A rented UK server (G1.e, G4.d); a capacity test (G2.f) |
| Compliance | GO (demo makes no claim) | NO-GO | NO-GO | `docs/compliance/` | CSO review (I.j); MHRA qualification (I.k); DPO sign-off; ICO guidance check |
| Hardware | NO-GO | NO-GO | NO-GO | `docs/hardware/` | Funding, an ODM and a board (H1–H4.h to H1–H4.j) |

**Overall:** GO for an engineering demonstration; NO-GO for a pilot and for release. The
software's own gates are mostly met. What stands between the demo and a pilot is external:
- a Clinical Safety Officer;
- an MHRA view;
- devices;
- a live provider;
- a server.

The owner's constraints hold throughout: no real patient data, no real emergency messages, and no
deployment to real users.

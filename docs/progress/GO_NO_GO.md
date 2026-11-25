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
| Data isolation and security | GO | NO-GO | NO-GO | `docs/security/REVIEW.md`, `docs/security/THREAT_MODEL.md` | Independent penetration test (C-22.d) |
| Dashboard | GO | NO-GO | NO-GO | `dashboard/src/__tests__/`; the Playwright journey | Accessibility pass on real assistive technology |
| Family phone app | GO | NO-GO | NO-GO | `mobile/src/__tests__/`; CI's native Android release build of the family app (`mobile-android` job) | Signed store builds, iOS build (D2.h), device runs (D2.i) |
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

## Readiness decisions by use (owner directive sections 16 and 17)

Added 25 November 2026 on `main` at `65c61fa`. The table above decides by area; this one decides
by the seven uses the endorsement and the investor demonstration need. "GO" means the evidence
supports that use today, under the conditions stated; it never means clinical suitability.
IRA CARE is not a medical device, and no use below involves a real person's care.

| Use | Decision | Justification | Evidence |
| --- | --- | --- | --- |
| Internal engineering demonstration | **GO** | Fictional people, log-only providers and a labelled browser watch. The 22-step care journey passes against the real API and PostgreSQL, deterministically on every change and with the real model chain in 8 of 8 runs. | `supracloud-jarvis/ira/tests/tenancy/test_care_journey_db.py`; `docs/evidence/demo/REAL_MODEL_RUN.md`; `docs/demo/RERUN.md` |
| Investor demonstration | **GO**, with conditions | The same stack as the engineering demonstration, run from the presenter guide. Conditions: say at the start that the people are fictional, the watch is a browser page or simulated sensors, and nothing sends real messages; make no clinical, accuracy or customer claim; show the "what is not proved" list. The recorded film is the fallback if a live step fails. | `docs/demo/DEMO_SCRIPT.md`; `docs/film/ira_care.mp4`; `docs/evidence/TECHNICAL_ENDORSEMENT_REPORT.md` |
| Innovator Founder endorsement demonstration | **GO**, with the same conditions | The technical case (innovation, viability, scalability) is backed by tests, recorded runs and the film, and the report separates IRA's own work from the third-party models it integrates. It must be presented as a working software prototype with hardware still to be funded, not as a product in use. | `docs/evidence/TECHNICAL_ENDORSEMENT_REPORT.md`; `docs/evidence/ENDORSEMENT_PACK.md`; `docs/evidence/ARCHITECTURE_DIAGRAMS.md`; `docs/evidence/ASSUMPTIONS_REGISTER.md` |
| Physical-device engineering tests | **NO-GO** until devices exist; then GO for engineers only | The software is ready to be tried (the watch app builds in both flavours and its JVM tests pass in CI; the family app's Android release build runs in CI), but no watch or phone has been bought and no device build has run. Once devices exist, engineers may test with fictional data and log-only providers. | `docs/evidence/DEPENDENCY_REGISTER.md` (group A); `docs/evidence/builds/ANDROID.md`; `.github/workflows/test.yml` |
| Supervised care pilot | **NO-GO** | No Clinical Safety Officer review (I.j); MHRA qualification unconfirmed (I.k); no DPO sign-off or ICO check (I.i); nothing run on a physical device; model accuracy unmeasured on older adults and on the IRA watch; no live message delivery (G3.e); no monitoring-centre contract (G3.f); no rented server (G4.d); no pilot agreement. | `docs/compliance/PILOT_PROTOCOL.md`; `docs/compliance/CLINICAL_SAFETY_CASE.md`; `docs/evidence/DEPENDENCY_REGISTER.md` (groups A, J, K, L) |
| Commercial release | **NO-GO** | Everything the pilot needs, plus signed store builds and accounts (D1b.c, D2.j, D2.k), an iOS build (D2.h), an independent penetration test (C-22.d), a capacity test on a representative server (G2.f), a 24-hour soak (G2.e), and, for the dedicated watch, the IRA CARE OS image and the hardware (E2.h, H1–H4.h). | `docs/evidence/DEPENDENCY_REGISTER.md`; `docs/os/ARCHITECTURE.md`; `docs/hardware/EVT_DVT_PVT_ROADMAP.md` |
| Unsupervised real-world care | **NO-GO** | Not a use the product is designed or evidenced for. Even after a pilot it would need the results of that pilot, clinical sign-off, a regulatory determination and live, contracted escalation. The fall detector misses some falls (about 22 % of simulated falls in its test data) and is unmeasured on older adults, so it must never be the only safeguard. | `docs/ai/fall/MODEL_CARD_V3.md`; `docs/compliance/INTENDED_PURPOSE.md`; `docs/compliance/CLAIMS_REGISTER.md` |

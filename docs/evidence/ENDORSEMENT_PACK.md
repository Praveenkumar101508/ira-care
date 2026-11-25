# IRA CARE: endorsement evidence pack (requirement L.a)

Prepared 25 November 2026, on `main` at `633572f`. Every claim below links to the evidence for
it in this repository. Where the evidence stops, it says so.

The full technical report for the Innovator Founder endorsement, with what IRA built set apart
from the third-party models it integrates, is `docs/evidence/TECHNICAL_ENDORSEMENT_REPORT.md`.
Beside it: the architecture diagrams (`docs/evidence/ARCHITECTURE_DIAGRAMS.md`), the
assumptions register with what does not exist (`docs/evidence/ASSUMPTIONS_REGISTER.md`), and the
remaining external dependencies (`docs/evidence/DEPENDENCY_REGISTER.md`). The figures below are
from `633572f`; the report has the figures on `65c61fa`.

## What IRA CARE is, honestly

**A working, tested software prototype. The IRA CARE OS image and the IRA watch hardware are the
next funded stage.**

It is a care service for older people living alone. A watch checks in through the day, notices
when something is wrong (no answer and no movement, a heart rate outside a carer's range, a
suspected fall, a call for help), asks the person first, and then tells their care circle in
order. Families use a phone app and care organisations use a dashboard. It makes no diagnosis and
never dials emergency services itself (`docs/compliance/INTENDED_PURPOSE.md`).

## Innovation

| Claim | Evidence |
| --- | --- |
| Asks first, escalates only on silence, so fewer false alarms reach families | `supracloud-jarvis/ira/tests/tenancy/test_care_escalation_db.py`, `docs/compliance/CLINICAL_SAFETY_CASE.md` (H2, H15) |
| A fall model trained only on licensed data that catches more falls than the rule detector, with far fewer false alarms: 246 of 316 simulated falls against 195, at about 10 alarms per hour of activity against 53 | `docs/ai/fall/MODEL_CARD_V3.md`, `docs/ai/fall/results_v3.json` |
| A companion that talks, remembers what the person agreed it may, and never turns doubt into "OK" or a dose into "taken" | `supracloud-jarvis/ira/tests/care/test_care_safety_answers.py`, `supracloud-jarvis/ira/tests/tenancy/test_care_safety_answers_db.py` |
| Never says "All well" when the system cannot vouch for it | `supracloud-jarvis/ira/tests/tenancy/test_care_delivery_db.py`, `dashboard/src/__tests__/view.test.ts` |
| Its own OS product layer for a dedicated watch, with the app as the home screen, and an update server that refuses downgrades | `docs/os/ARCHITECTURE.md`; the image itself is unbuilt (needs an AOSP build host) |

## Viability: it works

| Claim | Evidence |
| --- | --- |
| The full 22-step care journey, from organisation set-up to recovery after a network failure, passes end to end against the real API and database | `supracloud-jarvis/ira/tests/tenancy/test_care_journey_db.py`, `docs/demo/CARE_JOURNEY_22.md` |
| The whole stack runs as containers with health checks, outage drills, backups and restore proved | `docs/evidence/staging/CONTAINERS.md`, `docs/evidence/staging/containers.json` |
| Encrypted off-site backups restore byte for byte | `docs/evidence/staging/OFFSITE.md` |
| Thousands of automated tests and CI on every change, including container image builds and the family app's native Android release build | `docs/progress/VERIFICATION.md`, `.github/workflows/test.yml` (the `mobile-android` job) |
| Security: tenant and person isolation proved adversarially, rate limits, headers, signed callbacks, a STRIDE threat model | `docs/security/REVIEW.md`, `docs/security/THREAT_MODEL.md` |
| Clinical-safety thinking from the start: a hazard log tied to tests, and a claims register | `docs/compliance/CLINICAL_SAFETY_CASE.md`, `docs/compliance/CLAIMS_REGISTER.md` |

## Scalability

| Claim | Evidence |
| --- | --- |
| One server process handled 118–132 requests a second with no errors on a shared 4-CPU machine; a watch makes about one to three requests a minute | `docs/evidence/staging/containers.json`, `docs/ops/HOSTING_PROPOSAL.md` (an extrapolation, not a capacity test) |
| Multi-organisation from the start: one database, forced row-level security per workspace | `supracloud-jarvis/ira/tests/tenancy/test_3c06_seventeen_tables.py` |
| A costed path from a 50-person pilot to about 1,000 people | `docs/ops/HOSTING_PROPOSAL.md` (prices to be filled from the chosen provider) |
| A hardware route: components, ODM specification, gates and the Qualcomm engagement plan | `docs/hardware/BLUEPRINT.md`, `docs/hardware/ODM_SPECIFICATION.md`, `docs/hardware/EVT_DVT_PVT_ROADMAP.md`, `docs/hardware/QUALCOMM_ENGAGEMENT.md` |

## How complete it is

Measured by a fixed list of 245 atomic requirements (`docs/progress/requirements.json`), checked
by `scripts/completion_summary.py`, on `main` at `633572f`:

| Measure | Done / total | % |
| --- | --- | --- |
| Hardware-independent software implemented | 141 / 183 | 77.0 % |
| Hardware-independent software accepted | 124 / 183 | 67.8 % |
| Software verified | 56 / 197 | 28.4 % |
| AI development accepted | 22 / 39 | 56.4 % |
| OS development accepted | 2 / 27 | 7.4 % |
| Hardware integration | 0 / 14 | 0.0 % |
| External and commercial readiness | 0 / 34 | 0.0 % |

57 requirements are blocked by something outside the software. The script names each one with its
blocker: physical devices, an AOSP build host, signing keys, accounts, providers, advisers and
funding. Run `python3 scripts/completion_summary.py` for the current figures.

## What is not proved, said plainly

- Nothing has run on a physical watch or phone.
- No model is validated on older adults or on the IRA watch; the falls were simulated by younger
  volunteers.
- No Clinical Safety Officer or MHRA adviser has reviewed it, and it is not approved for any
  clinical use.
- No live SMS or call has been sent; no real data from real people has been collected.
- The IRA CARE OS image has not been built or booted.

The decision on what may be used with real people is in `docs/progress/GO_NO_GO.md`.

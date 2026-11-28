# IRA CARE: what to ask for, from whom, and what each unblocks (28 November 2026)

Every requirement still waiting on something outside the project (53 `blocked_external`, plus the
24-hour soak, G2.e), grouped by the one thing each needs. The requirement lists are taken from
`docs/progress/requirements.json` (`blocked_by`); the measures move only when the evidence exists.
Budget: £0 unless the owner decides otherwise; nothing here has been requested, signed up to,
accepted or paid for. The owner's personal Mac is never used for any of it.

## Machines

| # | Ask for | Exact need | From whom (£0 route first) | Unblocks |
|---|---|---|---|---|
| 1 | **A Linux server that stays up for a day** | x86-64 Linux, 4 cores, 8 to 16 GB RAM, 50 GB disk, outbound HTTPS; up for 26 hours (24-hour soak plus set-up); registered as a GitHub self-hosted runner (jobs up to 5 days) or reachable by the session | University of Essex research computing (asked); a sponsor's VM; a startup-credits programme (founders' sign-up) | G2.e (24-hour soak). Meanwhile `wall-clock soak (manual)` gives 5.5 hours on a free runner |
| 2 | **An AOSP build host** | x86-64 Linux with KVM, 64 GB RAM, 400 GB free disk (SSD), 8+ cores, about 10 hours per build | Essex research computing; a sponsor's workstation | E2.h, E2.i, E2.l, E2.n, E4.a, E4.b, E4.c, E4.e, E4.f, E6.c (10) |
| 3 | **A representative server, then hosting** | the hosting proposal's single-machine size (`docs/ops/HOSTING_PROPOSAL.md`): a VM with a domain and HTTPS, for staging and a capacity test | a sponsor or credits programme; otherwise the owner's approval to spend | G1.e, G2.f, G4.d (3) |
| 4 | **Permission to install a model server here** | Ollama (MIT) with Gemma 4 `e4b` and `e2b` (Apache 2.0, 11 GB of weights) on the build machine: 15 GB RAM and 53 GB disk are free | the owner: allow the install in this session's settings (it was refused by the session's permissions) | the real-model run of the distress redirects (`ROUND10_CONVERSATION.md`), not a register row |

## Devices

| # | Ask for | Exact need | From whom | Unblocks |
|---|---|---|---|---|
| 5 | **A Wear OS watch and an Android phone, with device builds** | a Wear OS 4 or later watch (API 33+) with a heart-rate sensor, microphone and speaker; an Android 13+ phone; the debug or internal builds from CI installed by USB | bought or lent (owner's decision); none bought | C-5.b, C-12.c, C-13.c, C-17.b, C-21.d, C-23.e, J.f, K.h, D1.j, F4.g, F7.k (11) |
| 6 | **A watch with hardware-backed keys** | a Wear OS watch whose Keystore offers StrongBox or a TEE-backed key, checked on the device before buying | as 5 | C-19.b |
| 7 | **Phones for the phone app on real devices** | one Android 13+ phone and one iPhone on iOS 17+ | as 5 | D2.i |
| 8 | **The target board, its BSP and the supplier's signing process** | the IRA watch's own board (`docs/hardware/`), Qualcomm supplier access under NDA, an eSIM provider | the ODM and Qualcomm (`docs/hardware/QUALCOMM_ENGAGEMENT.md`) | E3.f, E4.h, E4.i, E4.j, E6.d, F3.d, H1–H4.h, H1–H4.i, H1–H4.j, D4.c (10) |
| 9 | **ECG-referenced recordings from the IRA watch** | the bench method in `docs/hardware/SENSOR_EVALUATION_PLAN.md`, with consent | a clinical partner, once 8 exists | F3.e |

## Data

| # | Ask for | Exact need | From whom | Unblocks |
|---|---|---|---|---|
| 10 | **KFall permission** | written permission for commercial model training | the KFall authors (`docs/legal/KFALL_PERMISSION_REQUEST_2026-11-26.md`, drafted) | F1.d |
| 11 | **FallAllD permission** | as 10 | the FallAllD authors (`docs/legal/dataset_permission_requests/FALLALLD.md`) | F1.e |
| 12 | **SisFall terms** | the authors' written terms, or the owner's recorded risk decision | the SisFall authors (`docs/legal/SISFALL_PERMISSION_REQUEST_2026-11-26.md`) | F1.f |
| 13 | **A Mozilla Data Collective account** | an account whose terms the owner reads and accepts (Common Voice 27.0 English is CC0 per Mozilla's catalogue); only the older-speaker subset is fetched, on a remote machine | the owner | F8.c |
| 14 | **Consented recordings and listening sessions with older people** | the protocol in `docs/compliance/PILOT_PROTOCOL.md`, with consent | a care partner | F8.d, F8.e |

## Accounts, keys and money

| # | Ask for | Exact need | From whom | Unblocks |
|---|---|---|---|---|
| 15 | **Key custody** | a decision on who holds the release, platform and verified-boot keys, and where | the owner | D1b.c, E4.g |
| 16 | **Store accounts** | a Google Play developer account; an Apple developer account | the owner (each has a fee) | D2.j, D2.k |
| 17 | **Message provider keys** | Twilio (or another) credentials and the decision to pay per message | the owner | G3.e |
| 18 | **A monitoring-centre contract** | an alarm receiving centre that accepts the integration (`IRA_CARE_ARC_SECRET`) | the owner | G3.f |
| 19 | **A penetration test** | a qualified tester, contracted | the owner's approval to spend | C-22.d |

## Reviews

| # | Ask for | Exact need | From whom | Unblocks |
|---|---|---|---|---|
| 20 | **Clinical safety sign-off** | review of `docs/compliance/CLINICAL_SAFETY_CASE.md` and the hazard log against DCB0129 | a qualified clinical safety officer | I.j |
| 21 | **MHRA classification** | the questions in `docs/compliance/MHRA_ADVISER_QUESTIONS.md` answered | an MHRA regulatory adviser | I.k |
| 22 | **ICO guidance read** | the current ICO pages read against the DPIA and privacy policy (the build machine's network refuses ico.org.uk) | the operator's data protection officer | I.i |

## Not waiting on anything outside: the owner's clicks

- **Mandatory: the iOS journey** carried from pull request 132 (`NEXT.md`, first section): the fixes
  are on pull request 133; its native checks run them.
- **Three manual workflows on `main`**: `deployment guide (manual)` (G4.a), `wall-clock soak (manual)`
  (G2.e's interim) and `dataset provenance (manual)` (F1.g). The session cannot start them: GitHub
  answered "Resource not accessible by integration" (403) to each.

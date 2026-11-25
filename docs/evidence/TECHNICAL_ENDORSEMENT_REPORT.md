# IRA CARE: technical report for the Innovator Founder endorsement

Prepared 25 November 2026 on `main` at `65c61fa` (pull requests up to #102 merged). It is the
technical input to the endorsement application and to the investor demonstration (owner
directive sections 16 and 17). It answers the endorsing body's three questions, innovation,
viability and scalability, with a link to the evidence for every claim, and it says plainly
where the evidence stops.

Companion documents:
- [`ENDORSEMENT_PACK.md`](ENDORSEMENT_PACK.md): the short evidence pack (requirement L.a);
- [`ARCHITECTURE_DIAGRAMS.md`](ARCHITECTURE_DIAGRAMS.md): system context, a Help request, and the
  safety boundaries around the models;
- [`ASSUMPTIONS_REGISTER.md`](ASSUMPTIONS_REGISTER.md): every financial and infrastructure
  assumption with its source and confidence, and what does not exist;
- [`DEPENDENCY_REGISTER.md`](DEPENDENCY_REGISTER.md): every remaining external dependency;
- `docs/progress/GO_NO_GO.md`: what IRA CARE may be used for today, and what it may not.

## What IRA CARE is, and is not

IRA CARE is a care service for older people who live alone. A watch checks in through the day,
notices when something may be wrong, asks the person first, and only then tells their care
circle, in order, until someone answers. Families use a phone app; care organisations use a
dashboard.

- **It is a working, tested software prototype.** Everything runs end to end with fictional
  people, against a real database and real server code.
- **It is not a medical device.** It makes no diagnosis, gives no medical advice and never
  dials the emergency services itself (`docs/compliance/INTENDED_PURPOSE.md`,
  `docs/compliance/CLAIMS_REGISTER.md`). No regulator has reviewed it.
- **It does not detect all falls.** The fall detector catches about 78 % of simulated falls in
  a public research dataset, recorded with young volunteers, and has never been measured on
  older adults or on the IRA watch (`docs/ai/fall/MODEL_CARD_V3.md`).
- **The hardware is simulated.** Every demonstration so far uses a browser watch or the watch
  app's simulated sensors, each labelled as such. The IRA watch is a design
  (`docs/hardware/BLUEPRINT.md`), and the IRA CARE OS image has never been built or booted
  (`docs/os/ARCHITECTURE.md`).
- **There are no customers, pilots or revenue.** See
  [`ASSUMPTIONS_REGISTER.md`](ASSUMPTIONS_REGISTER.md), "What does not exist".

## 1. Innovation

What is new is the combination, and the discipline around it: open language models made safe
for an older person living alone by deterministic rules that the models cannot override.

| Element | What it does | Evidence |
| --- | --- | --- |
| **Proactive companionship, with deterministic safety rules around open models** | IRA starts the conversation: check-in questions through the day, worded by an open model (Gemma 4) from the person's own context, with a fixed template whenever the model is late or breaks a rule. Every answer is read by keyword and red-flag rules as well as the model; the stricter reading wins; doubt about a tablet stays "unknown" and never becomes "taken". The product works with no model at all. | `supracloud-jarvis/ira/care/conversation.py`; `supracloud-jarvis/ira/tests/care/test_care_safety_answers.py`; `supracloud-jarvis/ira/tests/tenancy/test_care_safety_answers_db.py`; the real-model run, 8 of 8 journeys with every safety assertion held: `docs/evidence/demo/REAL_MODEL_RUN.md`; model choice and the rules-only baseline: `docs/MODEL_CHOICE.md` |
| **Scheduled-care awareness** | IRA knows the person's routines, medication times, versioned care plans and planned carer visits, asks about them at the right time, records a dose only after the person confirms it twice, and shows the wearer when a carer has arrived. | `supracloud-jarvis/ira/tests/tenancy/test_care_plans_db.py`; `supracloud-jarvis/ira/tests/tenancy/test_care_visits_db.py`; `supracloud-jarvis/ira/tests/tenancy/test_care_reminder_buttons_db.py`; `supracloud-jarvis/ira/tests/tenancy/test_care_personal_db.py` |
| **Integrated safety monitoring** | One service brings together the Help button and the spoken word "help", suspected falls (a trained model beside a rule detector), heart rate outside a carer-set range, no answer and no movement, and the watch being taken off, each with a day and night rule, and each asking the person first. | `supracloud-jarvis/ira/tests/tenancy/test_care_continuous_db.py`; `supracloud-jarvis/ira/tests/tenancy/test_care_fall_db.py`; `supracloud-jarvis/ira/tests/care/test_care_monitoring_rules.py`; `docs/compliance/CLINICAL_SAFETY_CASE.md` |
| **Coordinated caregiver response** | An SOS goes to tier 1 (family and carer), then tier 2 (the care organisation), then tier 3 (a monitoring centre when one is contracted; otherwise the family is shown "call 999 now"). "I'm on my way" stops the chain and is shown on the wrist. Messages go through a durable outbox, so a crash cannot lose one or send one twice. | `supracloud-jarvis/ira/care/escalation.py`; `supracloud-jarvis/ira/tests/tenancy/test_care_escalation_db.py`; `supracloud-jarvis/ira/tests/tenancy/test_care_sos_db.py`; `supracloud-jarvis/ira/tests/tenancy/test_care_delivery_db.py` |
| **Consent-controlled memory** | IRA remembers what the person agreed it may (for example a preference), uses it in the next conversation, and lets the person or their representative correct or clear it. Transcripts are private until consent is recorded; carers see summaries, never the person's words. | `supracloud-jarvis/ira/tests/tenancy/test_care_journey_db.py` (steps 6 and 19); `supracloud-jarvis/ira/tests/tenancy/test_care_memory_db.py`; `supracloud-jarvis/ira/tests/tenancy/test_care_conversation_db.py` |
| **Wearable, phone and dashboard as one system** | The same care record drives the watch, the family's phone and the organisation's dashboard, with roles and per-person access. | `watch/`, `mobile/`, `dashboard/`; `supracloud-jarvis/ira/tests/tenancy/test_care_one_app_db.py`; `supracloud-jarvis/ira/tests/tenancy/test_care_person_access_db.py` |
| **Dedicated OS strategy** | A dedicated watch whose home screen is IRA, with an update server that refuses downgrades and signed device-policy instructions. The server side and the product layer exist and are tested; the image itself is unbuilt. | `docs/os/ARCHITECTURE.md`; `os/device/ira/ira_watch_cf/`; `supracloud-jarvis/ira/care/ota.py`; `supracloud-jarvis/ira/tests/readiness/test_os_product_layer.py` |

## 2. What we built and what we integrated

This section separates IRA's own work from the third-party work it stands on. **IRA does not own
and did not train any of the foundation models it uses.** The licences below were read at
source on 25 November 2026 (`docs/ai/MODEL_WEIGHTS.md`, `docs/DATASETS.md`,
`docs/security/LICENCES.md`).

### Our own work

| What | Where | Notes |
| --- | --- | --- |
| The care service: check-ins, monitoring rules, ask-first, escalation, delivery outbox, reports, consent, memory, visits, care plans, search, retention and erasure | `supracloud-jarvis/ira/care/` | Built on an earlier backend codebase in the same repository (`supracloud-jarvis/`), whose identity, tenancy and governance layers it reuses |
| The safety rules around the models | `supracloud-jarvis/ira/care/conversation.py`, `supracloud-jarvis/ira/care/checkins.py`, `supracloud-jarvis/ira/care/monitoring.py` | Deterministic, tested without any model |
| The watch app (Wear OS and AOSP flavours) | `watch/` | Kotlin and Jetpack Compose |
| The family phone app | `mobile/` | Expo and React Native |
| The carer dashboard, with the browser watch and presenter page for demonstrations | `dashboard/` | Next.js |
| The database schema and tenancy rules: a migration chain numbered up to 121, with forced row-level security and a hash for every migration file | `supracloud-jarvis/ira/scripts/migrate.py`, `docs/cloud/evidence/DATABASE_TENANCY_CLASSIFICATION.json`, `docs/cloud/evidence/MIGRATION_FILE_HASHES.json` | |
| IRA fall model v3 (6,705 parameters), trained by us with no pretrained start | `docs/ai/fall/MODEL_CARD_V3.md`, `scripts/training/fall_model_v3.py` | Trained on BITS-2 and UCI HAR, which belong to their authors (CC BY 4.0) |
| IRA heart selector (gradient-boosted trees) | `docs/ai/heart/MODEL_CARD.md`, `scripts/training/heart_ppg_dalia.py` | Trained on PPG-DaLiA (CC BY 4.0, its authors'); in no alert path |
| IRA activity model | `docs/ai/activity/MODEL_CARD.md`, `scripts/training/activity_wisdm.py` | Trained on WISDM (CC BY 4.0, its authors'); no alert path |
| Answer-classifier heads | `docs/ai/MODEL_WEIGHTS.md` | Our own heads on a third-party encoder (all-MiniLM-L6-v2); off by default because they did not beat the rules |
| The synthetic care-conversation dataset (398 examples) and the 150-turn evaluation set | `docs/ai/conversation/DATASET_CARD.md`, `supracloud-jarvis/ira/tests/care/eval/turns.jsonl` | Written in this repository; no real person's words |
| The IRA CARE OS product layer for a Cuttlefish virtual device, the OTA release server and the device-policy logic | `os/`, `supracloud-jarvis/ira/care/ota.py`, `supracloud-jarvis/ira/care/devicepolicy.py` | Configuration on top of AOSP; never built into an image |
| Deployment, backups, runbooks, the readiness and licence checks | `deploy/`, `docs/ops/RUNBOOKS.md`, `scripts/licence_check.py`, `scripts/completion_summary.py` | |
| The watch hardware design (not built) | `docs/hardware/` | A specification and plan; no prototype exists |

### Third-party models we integrate (not ours)

| Model | Owner and licence | How IRA uses it |
| --- | --- | --- |
| Gemma 4 (`gemma4:e4b`, `gemma4:e2b`, and `gemma4:12b` on a GPU) | Google DeepMind; Apache 2.0 | Served unchanged by Ollama on the operator's own machine; words questions and replies inside IRA's rules. Our one fine-tune attempt did not beat the base model and is not shipped. |
| faster-whisper `small` | Systran's conversion of OpenAI Whisper; MIT | Server speech to text (`supracloud-jarvis/ira/voice/stt.py`) |
| Silero VAD (v6, ONNX) | The Silero team; the model file ships inside the faster-whisper package (faster_whisper/assets/silero_vad_v6.onnx) | Used only through faster-whisper's voice-activity filter (`vad_filter=True` in `supracloud-jarvis/ira/voice/stt.py`). Its licence is **not yet recorded separately** in `docs/ai/MODEL_WEIGHTS.md`: an open documentation item, listed in `DEPENDENCY_REGISTER.md` |
| all-MiniLM-L6-v2 | sentence-transformers; Apache 2.0 | Encoder under our classifier heads, off by default |
| Piper and the `en_GB-cori-high` voice | Piper (MIT), voice repository (MIT), voice data public domain | Tooling only: the narration of the demonstration film. Not in the product. |
| Platform speech engines on the watch and phone | Google and Apple platform terms | The devices' own recognisers and voices; IRA ships no speech weights to devices |

### Open-source frameworks and platforms (not ours)

FastAPI, Pydantic, asyncpg and psycopg, PostgreSQL 16, Redis, Caddy, Ollama, Next.js and React,
Expo and React Native, Kotlin and Jetpack Compose, TensorFlow Lite, ONNX Runtime and
CTranslate2, Android and AOSP, Docker. Each shipped dependency's licence is checked in CI:
994 packages, none forbidden (`docs/security/LICENCES.md`, `scripts/licence_check.py`).

### Datasets owned by others

BITS-2, UCI HAR, WISDM, PPG-DaLiA (all CC BY 4.0, used for training with credit) and LibriSpeech
test-clean (CC BY 4.0, evaluation only). SisFall is excluded from every build because its
original terms are unconfirmed (D-077, D-082). KFall, FallAllD and UMAFall wait on their
authors' permission (`docs/DATASETS.md`, `docs/legal/dataset_permission_requests/README.md`).

## 3. Viability

### Working software, with evidence

| Evidence | What it shows | Where |
| --- | --- | --- |
| The 22-step care journey, one automated test | Organisation set-up to recovery after a network failure, against the real API and PostgreSQL, on every change in CI | `supracloud-jarvis/ira/tests/tenancy/test_care_journey_db.py`, `docs/demo/CARE_JOURNEY_22.md` |
| The same journey with the real models | 8 of 8 runs with `gemma4:e4b` and `gemma4:e2b` on a 4-CPU machine; every safety assertion held | `docs/evidence/demo/REAL_MODEL_RUN.md` |
| Integrated verification on `main` | 3,903 database tests and 5,113 backend unit tests passed; dashboard 99 tests; phone app 79 tests; all 11 CI jobs green, including the watch app's Wear OS and AOSP builds and the container images | `docs/progress/VERIFICATION.md`, `.github/workflows/test.yml` |
| Demonstration re-run | Seeded demo, scripted scenarios and a recorded browser demonstration, with three defects found and fixed | `docs/demo/RERUN.md`, `docs/demo/demo.mp4` |
| The film | A 356-second narrated film recorded live from a stack built from a clean clone | `docs/film/ira_care.mp4`, `docs/film/RECORDING.md`, `docs/film/NARRATION.md` |
| Containers and operations | Health checks, outage drills, backup and restore, and a load baseline; encrypted off-site backups restored byte for byte | `docs/evidence/staging/CONTAINERS.md`, `docs/evidence/staging/OFFSITE.md` |
| Security | Tenant and person isolation tested adversarially, rate limits, a STRIDE threat model, dependency audits | `docs/security/REVIEW.md`, `docs/security/THREAT_MODEL.md` |
| Clinical-safety thinking | A hazard log tied to tests, a claims register checked against the product's capability list | `docs/compliance/CLINICAL_SAFETY_CASE.md`, `docs/compliance/CLAIMS_REGISTER.md`, `supracloud-jarvis/ira/tests/readiness/test_claims_register.py` |

### Technical capability shown by the repository

What a technical assessor can check in the repository itself:

- **Breadth.** A backend service, a watch app, a phone app, a web dashboard, trained on-device and
  server models, an AOSP product layer, containerised deployment and backup, all in one
  repository with one CI pipeline (`.github/workflows/test.yml`).
- **Measurement over assertion.** Every model has a card with its data, licence and limits
  (`docs/ai/fall/MODEL_CARD_V3.md`, `docs/ai/heart/MODEL_CARD.md`,
  `docs/ai/activity/MODEL_CARD.md`); every model choice has measured latency and quality
  (`docs/MODEL_CHOICE.md`); a model that did not beat the baseline was not shipped.
- **Honest progress tracking.** A fixed list of 251 atomic requirements with a script that
  refuses to count a document as acceptance (`docs/progress/requirements.json`,
  `scripts/completion_summary.py`, `supracloud-jarvis/ira/tests/readiness/test_requirements_framework.py`).
- **A decision record.** Over eighty dated decisions, including corrections of earlier claims
  (`docs/DECISIONS.md`).
- **Licence care.** Every dataset and model licence read at source; an uncertain dataset kept out
  of every build; a GPL component removed from the server on the owner's decision (D-083).

### Development completed

`python3 scripts/completion_summary.py`, run on `65c61fa`:

| Measure | Done / total | % |
| --- | --- | --- |
| Hardware-independent software implemented | 165 / 188 | 87.8 % |
| Hardware-independent software accepted | 144 / 188 | 76.6 % |
| Software verified | 67 / 203 | 33.0 % |
| AI development accepted | 30 / 45 | 66.7 % |
| OS development accepted | 4 / 27 | 14.8 % |
| Hardware integration accepted | 0 / 14 | 0.0 % |
| External and commercial readiness accepted | 1 / 34 | 2.9 % |

145 of 251 requirements are accepted or better. 61 are blocked by something outside the
software, each named in [`DEPENDENCY_REGISTER.md`](DEPENDENCY_REGISTER.md). None is validated on
a device.

### Remaining engineering resources

| Need | What for | Detail |
| --- | --- | --- |
| Physical watches and phones | Every on-device test: speech, falls, screen readers, keystore, battery | `DEPENDENCY_REGISTER.md`, group A |
| A host with KVM | Emulator runs of the watch and phone apps | group B |
| An AOSP build host (about 400 GB disk, 64 GB RAM) | Build and boot the IRA CARE OS image on Cuttlefish | group C; `docs/os/BUILD.md` |
| A Mac with Apple signing, and store accounts | iOS build; store listings | group F |
| A GPU session | The conversation fine-tune retry | group H |
| Older-adult data, with consent | Speech and model accuracy on the people the product is for | groups G and I |
| Engineering effort after funding | Hardware integration, the sensor HAL, the OS on a real board | `docs/hardware/EVT_DVT_PVT_ROADMAP.md`, `docs/os/QUALCOMM_DEPENDENCIES.md` |

No engineering headcount, salary or time estimate exists in the repository; none is given here.

### Hardware, hosting, manufacturing, deployment and support needs

| Area | What is ready | What is needed | Evidence |
| --- | --- | --- | --- |
| Hardware | A blueprint, a component selection, an ODM request-for-quotation specification, a sensor HAL contract, a sensor evaluation plan, a 3D model and renders | An ODM, a development board, funding; supplier access to the board support package | `docs/hardware/BLUEPRINT.md`, `docs/hardware/COMPONENTS.md`, `docs/hardware/ODM_SPECIFICATION.md`, `docs/hardware/SENSOR_HAL.md`, `docs/hardware/QUALCOMM_ENGAGEMENT.md` |
| Hosting | A sized pilot tier (one UK-region VM, 4 vCPU, 16 GB) and growth tier, a cost script, the stack measured on that shape | Provider prices (none could be read from the build environment), the owner's approval to spend, a capacity test | `docs/ops/HOSTING_PROPOSAL.md`, `docs/ops/hosting_prices.json`, `scripts/hosting_cost.py` |
| Manufacturing | EVT, DVT and PVT gates with entry and exit criteria; certification routes with sourced estimates | An ODM contract and quotations (none exist) | `docs/hardware/EVT_DVT_PVT_ROADMAP.md`, `docs/hardware/BLUEPRINT.md` (section 8) |
| Deployment | One-machine deployment guide, containers, GPU and no-model profiles, encrypted backups | A rented server and a domain | `docs/DEPLOY.md`, `deploy/docker-compose.yml` |
| Support and operations | Runbooks, an incident process, an escalation chain with a monitoring-centre adapter | A monitoring-centre contract, provider credentials, an on-call rota (no staff exist) | `docs/ops/RUNBOOKS.md`, `docs/compliance/INCIDENT_PROCESS.md` |

## 4. Scalability

| Claim | Evidence | Limit of the evidence |
| --- | --- | --- |
| **Multi-tenant from the start**: one database, forced row-level security on every tenant table, classified table by table | `docs/cloud/evidence/DATABASE_TENANCY_CLASSIFICATION.json`; `supracloud-jarvis/ira/tests/tenancy/test_3c06_seventeen_tables.py`; `supracloud-jarvis/ira/tests/tenancy/test_care_isolation_rls.py`; `supracloud-jarvis/ira/tests/readiness/test_cross_tenant_sweep.py` | Tested adversarially in CI; no independent penetration test yet (C-22.d) |
| **Many organisations and many wearers** | A simulated day for 200 people with no missed or late escalation (`docs/evidence/soak/SOAK.md`); 1,000 people in one workspace scheduled in 1.1 s (round 1, `docs/progress/FINAL_REPORT.md`) | Simulated time on a shared machine; the 24-hour wall-clock soak is not done (G2.e) |
| **Throughput** | One API process served 118 to 132 requests a second with no errors under 10, 25 and 50 simulated watches (`docs/evidence/staging/containers.json`) | A regression baseline on one shared 4-CPU machine, not a capacity test (G2.f) |
| **Repeatable deployment through containers** | Compose files, a clean-clone proof script, image builds in CI | `deploy/docker-compose.yml`, `scripts/staging/clean_clone_proof.sh`, `docs/evidence/staging/CONTAINERS.md` |
| **Device-independent design** | The watch app reads only standard Android sensor types through the public API; a sensor HAL contract lets any board run it; the browser watch uses the same device API | `docs/os/ARCHITECTURE.md` (section 2), `docs/hardware/SENSOR_HAL.md` | Never run on a physical device |
| **Model-serving options** | Off (rules only), CPU (`gemma4:e4b` then `gemma4:e2b`), or GPU (`gemma4:12b` then `gemma4:e4b`), chosen by compose file; no hosted model API | `deploy/compose.nomodel.yml`, `deploy/compose.gpu.yml`, `docs/MODEL_CHOICE.md` (GPU profile measured on a Tesla T4) |

## 5. What is not proved

- Nothing has run on a physical watch or phone.
- No model is validated on older adults or on the IRA watch; the falls were simulated by
  younger volunteers.
- No Clinical Safety Officer, MHRA adviser or data protection officer has reviewed the product.
- No real SMS, call or email has been sent, and no monitoring centre is contracted.
- The IRA CARE OS image has not been built or booted; the IRA watch has not been built.
- No customer, pilot site, supplier or investor has made any commitment
  ([`ASSUMPTIONS_REGISTER.md`](ASSUMPTIONS_REGISTER.md)).

# IRA CARE: Digital Technology Assessment Criteria (DTAC) answers

**DRAFT FOR REVIEW BY A QUALIFIED PERSON.** Prepared from the repository as built. Where the
truthful answer is "not yet" or "unmeasured", it says so. No compliance is claimed.

## Section A: company information

To be completed by the operator (name, registration, contacts).

## Section B: value proposition

B1 to B4: see `INTENDED_PURPOSE.md`. The need: adults who live alone and the people who worry
about them; the service checks in, notices three kinds of event and tells the care circle in
order, without dialling emergency services itself. Evidence of benefit: none yet; nothing has
run with real people (STATUS.md, "Needs the owner").

## Section C: technical questions

### C1 Clinical safety

| Question | Answer |
| --- | --- |
| Is the product a medical device? | Not claimed to be; `MHRA_ADVISER_QUESTIONS.md` lists what an adviser must decide. |
| Clinical risk management (DCB0129) | A draft clinical safety case and hazard log exist (`CLINICAL_SAFETY_CASE.md`), tied to code and tests. A named Clinical Safety Officer has not reviewed them. |
| Clinical Safety Officer | Not appointed. |
| Hazard log | `CLINICAL_SAFETY_CASE.md`, section "Hazard log". |

### C2 Data protection

| Question | Answer |
| --- | --- |
| Registered with the ICO | Operator's responsibility; not done by this repository. |
| DPIA | A draft exists (`DPIA.md`); not reviewed by a data protection officer. |
| Data Security and Protection Toolkit | Not completed. |
| Lawful basis | To be set by the operator; the software records consent per person for who may read transcripts and see readings (`care/consent.py`, `care_consents`). |
| Data processed | Listed in `DPIA.md`. |
| Data location | The operator's server; the deployment guide uses one UK-region machine; no third-party processors by default (SMS, mail, payment, model providers are off until configured). |
| Retention | Per person, 12 months by default, enforced by a daily job (`care/retention.py`, D-022). |
| Subject access and erasure | The manager erases a person (`DELETE /care/people/{id}`); the access log is kept as audit evidence (D-022). |

### C3 Technical security

| Question | Answer |
| --- | --- |
| Cyber Essentials | Not held. |
| Penetration test | Not done. |
| Encryption in transit | HTTPS by the reverse proxy (`deploy/Caddyfile`). |
| Encryption at rest | Access notes sealed with AES-256-GCM under a per-row key (D-028); database volume encryption is the operator's choice; backups are encrypted (`deploy/backup.sh`, D-044). |
| Authentication | Email and password (bcrypt), rate-limited, with second-factor support in the identity layer; device tokens and pairing codes stored as hashes (D-010). |
| Access control | Workspace roles (viewer, member, manager) on every care route (`care/authz.py`); row-level security on every table, forced, with adversarial tests (`tests/tenancy/test_care_isolation_rls.py`, `test_3c06_seventeen_tables.py`). |
| Audit | Every read of health data, transcripts, access notes and the memory is written to an append-only access log (`care/audit.py`, `tests/care/test_care_audit_coverage.py`). |
| Logging of secrets | Redacted by the logging layer; a static test refuses a function that returns a token. |
| Dependency and secret scanning | The build rules forbid published credentials; a test checks the table of known leaked values; no live keys exist in the repository. |
| Vulnerability disclosure | `INCIDENT_PROCESS.md`. |

### C4 Interoperability

| Question | Answer |
| --- | --- |
| APIs | A documented HTTP API under `/api/v1/care` (OpenAPI available from the full server). |
| Standards (FHIR, SNOMED) | None. IRA CARE holds no clinical codes by design; readings and summaries are plain. |
| Data export | Reports are readable and downloadable as text; the history search returns the person's own records; a full export route is not built. |

### C5 Usability and accessibility

| Question | Answer |
| --- | --- |
| Accessibility standard (WCAG 2.1 AA) | Automated checks pass on what they can reach (D-046); a screen-reader pass on real devices is owed; no formal audit. |
| User research | None with real users. |
| Plain language | A product rule: no diagnosis words anywhere, enforced by tests (`monitoring.no_diagnosis_words`). |

## Section D: interoperability and connectivity

Standalone; connects only to the operator's own server. Optional providers (SMS, mail,
payment, a language model) are off by default and named in `docs/DEPLOY.md`.

## Section E: usability and accessibility

See C5. The watch asks in plain words, repeats louder with vibration, and accepts a button
or a voice answer; the phone app uses large buttons and scalable text.

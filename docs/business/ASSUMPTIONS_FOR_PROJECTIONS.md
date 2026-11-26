# IRA CARE: inputs for three-year projections

Written 26 November 2026. This extends
[`docs/evidence/ASSUMPTIONS_REGISTER.md`](../evidence/ASSUMPTIONS_REGISTER.md) (the register) for
a founder building a three-year financial projection. It does not repeat the register's figures;
it points to them by their row numbers (I1 to I22, F1 to F17) and adds what a projection needs
on top. Market and competitor figures are in [`MARKET_EVIDENCE.md`](MARKET_EVIDENCE.md) and
[`COMPETITORS.md`](COMPETITORS.md).

## Labels

Every input below carries one label.

| Label | Meaning |
| --- | --- |
| **Evidence: measured** | Measured in this repository (the register's "Measured") |
| **Evidence: published** | Read at source with a date (the register's "Published", or `MARKET_EVIDENCE.md` "Verified at source") |
| **Evidence: unverified** | Seen only second-hand; must be re-read at source before use |
| **Assumption** | A choice or guess the founder makes for the model; it must be tested and replaced |
| **Not known** | No source exists and no reasonable basis for a number; leave the cell empty or model it as a range, clearly marked |

A projection built from this file is a set of scenarios, not a forecast. Every cell that is an
assumption should be visibly marked as one in the spreadsheet.

## 1. What is not known

None of the following exists on 26 November 2026. A projection must not imply otherwise.

| Item | Status | Consequence for the projection |
| --- | --- | --- |
| Customers | None | Every revenue line is an assumption |
| Customer or user interviews | None conducted (guides drafted in [`VALIDATION_INTERVIEWS.md`](VALIDATION_INTERVIEWS.md)) | Willingness to pay, buyer, price and conversion are all unknown |
| Revenue | None | No baseline; no growth rate can be derived |
| Investors | No term sheet, commitment or conversation recorded | Funding lines are scenarios |
| Pilots | No pilot agreement; `docs/compliance/PILOT_PROTOCOL.md` is a draft and the pilot is NO-GO (`docs/progress/GO_NO_GO.md`) | The date of first revenue is unknown and depends on external gates |
| Supplier quotations | None: no ODM, hosting, SMS or call provider, monitoring centre, penetration tester, Clinical Safety Officer or regulatory adviser has quoted | Every external cost is "not known" or a published list price |
| Hosting prices | Not read (register F1, F2) | Infrastructure cost per month is not known |
| Device unit cost | Not known (register F8) | Hardware margin cannot be modelled |
| Real-world alert rate | Not measured; the fall model's false-alarm figure is on a young volunteers' dataset and the model card says it is "not a prediction of alarms in a real home" (`docs/ai/fall/MODEL_CARD_V3.md`) | Messaging cost and care-provider workload per person cannot be estimated |
| Battery life and device failure rate | Not measured | Replacement and support cost not known |
| Regulatory classification | Unconfirmed (register section 1) | If IRA CARE were a medical device, costs F12 and F13 and their timelines apply |
| Staff | No employees; two founders | Salaries and headcount are assumptions (register F15) |
| Customer acquisition cost, churn, sales cycle | Not known | Must be ranges, labelled as assumptions |

## 2. Infrastructure cost drivers

The cost structure can be described from the repository even though the prices cannot.

| # | Driver | Basis | Label |
| --- | --- | --- | --- |
| C1 | Requests per watch: about 1 to 3 a minute | Register I1 | Evidence: measured intervals, estimated load (not measured on real wear) |
| C2 | One API process carries "in the order of a thousand" watches | Register I2, I5, I6 | Extrapolation from a measured baseline; **not** a capacity test (G2.f) |
| C3 | Pilot tier: one UK-region VM, 4 vCPU, 16 GB, 100 GB disk, for up to 50 people | Register I9; `docs/ops/HOSTING_PROPOSAL.md` | Evidence: measured configuration; the people limit is an estimate |
| C4 | Growth tier: two VMs, a load balancer and a managed PostgreSQL (2 vCPU, 8 GB), for up to about 1,000 people | `docs/ops/HOSTING_PROPOSAL.md` | Assumption grounded in C2 |
| C5 | Conversation model: off (rules only), CPU (about 10 GB more memory) or a GPU server (€184 to €214 a month list price, read 24 November 2026) | Register I11, I15, F3 | Evidence: measured memory and latency; published GPU list prices (re-check) |
| C6 | Price of C3, C4 and backup storage | Register F1; `scripts/hosting_cost.py` refuses to total empty prices | **Not known.** Provider price pages could not be read from the build environment |
| C7 | SMS, voice call and email per alert | Register F4; Twilio adapter exists but is off | **Not known** (no provider chosen, no tariff read) |
| C8 | Alerts per person per month | No real-world data | **Not known.** Model as a range and label it |
| C9 | Monitoring-centre (ARC) fee per connection, if tier 3 is contracted | Register F5 | **Not known** |
| C10 | CI and development tooling | Register I19 (about 19 to 21 runner minutes per full run) | Evidence: measured minutes; price not read |
| C11 | Database growth per person | Register I22 | **Not known** |
| C12 | Model licence fees | Register F17: none (Apache 2.0, MIT) | Evidence: licences read at source |

**Shape of the cost.** Monthly infrastructure is a step function of people served (one tier
until C2's limit, then the next), plus a variable term of C7 × C8 per person, plus an optional
ARC fee C9 per person. Only the step boundaries have any repository evidence; every price is
missing.

## 3. Hardware: stages and what is unknown

Two device routes exist in the repository. They have very different cost profiles.

| Route | What it is | Status | Cost evidence |
| --- | --- | --- | --- |
| **A. Commercial Wear OS watch** (Galaxy Watch 4 and later) | The watch app runs on a watch the customer or provider buys | Software builds in CI; never run on a physical watch (`docs/progress/GO_NO_GO.md`) | Retail watch prices were not collected: **not known** (Assumption if used). No development cost beyond engineering time and test devices (register, dependency group A) |
| **B. The IRA watch** | A dedicated watch running the IRA CARE OS | A design, component selection, 3D model and an unsent request-for-quotation (`docs/hardware/BLUEPRINT.md`, `docs/hardware/ODM_SPECIFICATION.md`); the OS image has never been built | See below |

Route B stages, from `docs/hardware/EVT_DVT_PVT_ROADMAP.md` (timings and unit counts are the
document author's **estimates**, not quotations):

| Stage | Estimated timing | Estimated units | Cost |
| --- | --- | --- | --- |
| Development board | Months 0 to 3 | 2 to 5 | **Not known** |
| EVT | Months 3 to 8 | 20 to 50 | **Not known** |
| DVT | Months 8 to 12 | 100 to 200 | **Not known** |
| PVT | Months 12 to 16 | 300 to 1,000 | **Not known** |
| Mass production | From month 16 | Volume | Unit cost at 1k, 10k and 50k requested in the ODM specification; **no answer exists** (register F8) |

Costs that a hardware plan needs and that no quotation covers: non-recurring engineering from an
ODM, industrial design, tooling and moulds, board support package access from the chip vendor
(`docs/hardware/QUALCOMM_ENGAGEMENT.md`), the bill of materials, certification laboratory time,
eSIM and network certification, packaging, logistics, warranty returns and spares. The only
figures available are certification estimates and published fees in the register (F9 to F14),
mostly from secondary sources.

**Suggestion for the model:** treat route A as the base case for years 1 and 2, and route B as a
separately funded scenario whose costs stay "not known" until an ODM quotes.

## 4. Subscription model options

No price has been set or offered to anyone (register F16). The options below are structures to
test in interviews, not recommendations.

| Option | Who pays | What they would get | Main uncertainty |
| --- | --- | --- | --- |
| S1. Family subscription | The older person or their family (self-funders; CQC reports 24 % of homecare users self-fund, `MARKET_EVIDENCE.md` H6) | Watch app, family app, tiered alerts to the care circle; no 24-hour operator | Whether families value proactive check-ins without an operator; device supply |
| S2. Per person per month to a care provider | Domiciliary care providers or care homes | Dashboard, tier-2 alerts to the provider, visit and care-plan awareness on the wrist | Whether providers will take on alert response (staffing, liability); integration with their care-record platform |
| S3. Council or housing commissioning | Local authority TEC services, housing providers | A digital, landline-independent option during the switchover | Procurement routes, frameworks, ARC integration, evidence demanded by commissioners |
| S4. Partnership with an ARC | An existing telecare service resells or adds IRA | IRA's check-ins and alerts routed to a staffed monitoring centre | No ARC protocol integration is built; commercial terms unknown |

Comparable published prices (all **Evidence: unverified**, details and links in
`COMPETITORS.md`):

| Comparable | Price seen | Includes a 24-hour operator? |
| --- | --- | --- |
| Monitored pendant alarms (Which? guide) | About £19 to £27 a month, plus £30 to £50 set-up for fall-detector monitoring | Yes |
| Careline365 EverNear | £99 device plus from £27.50 a month | Yes |
| Taking Care Digital Fall Alarm | £69.99 set-up, £28.49 a month (ex VAT) | Yes |
| Age UK Personal Alarm Watch (Taking Care) | £99.99 set-up, £26.49 a month (ex VAT) | Yes |
| Council lifeline (Harrow example) | £295 to £495 a year (ex VAT) | Yes |
| Buddi Connect | £99 plus £1.99 a week (app); 24/7 support £2 a week more | Optional |
| Telecare24 digital pendant | From £19.00 a month or £180 a year (ex VAT) | Yes |
| ElliQ (United States) | US$49.99 to US$59.99 a month plus about US$249.99 enrolment | No |
| Care-management platforms (Birdie, Access, Nourish) | Not published | Not applicable |

Points for the model:

- Most UK comparables include a staffed ARC. IRA CARE does not; pricing it against them without
  an ARC partner (S4) would compare different services.
- Comparables are consumer prices that may include VAT treatment specific to disabled users;
  VAT on IRA CARE was not researched (**Not known**).
- The council's own spend on assistive equipment and technology was £266 million in 2024-25
  (`MARKET_EVIDENCE.md` D7, Evidence: published). That is a ceiling on one budget line, not an
  addressable market for IRA CARE.

## 5. Care-provider deployment and support effort

The onboarding steps exist in the software; the time each takes has never been measured.

| # | Step | Exists in the repository | Effort |
| --- | --- | --- | --- |
| E1 | Create the organisation, staff accounts and roles | Yes (the 22-step journey, `docs/demo/CARE_JOURNEY_22.md`) | **Not known**; measure in a pilot |
| E2 | Add each person, their care circle in priority order, routines, medication times, care plan and visits | Yes | **Not known** |
| E3 | Record consent (transcripts, location, activity sharing) | Yes | **Not known**; likely longer where capacity or a legal representative is involved (Assumption) |
| E4 | Set safe ranges (heart rate) and quiet hours, by a carer or clinician | Yes | **Not known** |
| E5 | Pair the watch with a short code; walk the family through the background sensor permission | Yes in software; never done on a physical watch | **Not known** |
| E6 | Train provider staff to acknowledge tier-2 alerts and use the dashboard | No training material exists | **Not known** |
| E7 | Ongoing: charge the watch daily, keep it worn, respond to "watch off" and "battery low" notices | Alerts exist; battery life unmeasured | **Not known** |
| E8 | IRA-side support: incidents, outages, alert-delivery failures | Runbooks and an incident process exist (`docs/ops/RUNBOOKS.md`, `docs/compliance/INCIDENT_PROCESS.md`); no on-call rota or staff | **Not known**; an on-call rota is a cost line from the first live person (Assumption) |
| E9 | Clinical safety and data protection per deployment (hazard log review, DPIA update) | Templates exist (`docs/compliance/CLINICAL_SAFETY_CASE.md`, `docs/compliance/DPIA.md`); no reviewer | **Not known** (register F6) |

For each step, the pilot protocol should record the time taken and who did it, so that these
rows can become **Evidence: measured**.

## 6. Building the three-year model honestly

1. Start every revenue line at zero until a pilot exists, and make the pilot start date a scenario
   input tied to the external gates in `docs/progress/GO_NO_GO.md` (Clinical Safety Officer,
   MHRA view, devices, a live message provider, a server).
2. Use ranges, not single numbers, for every "Assumption" and "Not known" cell, and show which
   result depends on which.
3. Keep route B hardware costs out of the base case until an ODM quotation exists.
4. Replace an assumption only with a source: a published price with the date read, a quotation,
   a measurement in this repository, or a recorded interview result.
5. Never present market totals from `MARKET_EVIDENCE.md` as IRA CARE's market without the
   assumptions that narrow them.

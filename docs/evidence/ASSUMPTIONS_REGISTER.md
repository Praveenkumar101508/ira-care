# IRA CARE: assumptions register (financial and infrastructure)

Prepared 25 November 2026 on `main` at `65c61fa`, for the Innovator Founder endorsement and the
investor demonstration. Every figure below has a source in this repository. Where there is no
reliable source, the figure is **not estimated** rather than guessed.

How to read the confidence column:

- **Measured:** a number from a recorded run in this repository, on the machine it names.
  Measured on a shared 4-CPU build container unless stated; a production server will differ.
- **Published:** read from a provider's or regulator's own page on the date given; prices
  change and must be re-read before any decision.
- **Estimate:** derived from the code or from secondary sources; the reasoning is stated.
- **Not estimated:** no reliable source exists in the repository.

## 1. What does not exist

These are stated so that nobody reads them into the rest of this document. None of the following
exists on 25 November 2026:

| Item | Status |
| --- | --- |
| Customer interviews | **None conducted.** No interview notes, recordings or summaries exist. |
| Market validation | **None.** No survey, pilot demand, letter of intent or sizing study based on primary evidence exists in this repository. |
| Revenue | **None.** No customer has paid; no price has been offered to anyone. |
| Investor commitments | **None.** No term sheet, commitment or agreement exists. |
| Pilot agreements | **None.** No care organisation has agreed to a pilot. `docs/compliance/PILOT_PROTOCOL.md` is a draft protocol, not an agreement. |
| Supplier agreements | **None.** No ODM, chip vendor, SMS or call provider, monitoring centre or hosting provider is contracted. `docs/hardware/ODM_SPECIFICATION.md` is a request-for-quotation draft that has not been sent. |
| Manufacturing quotations | **None.** The hardware costs in `docs/hardware/BLUEPRINT.md` are estimates from published sources, not quotations. |
| Regulatory approval | **None.** No MHRA view, UKCA marking, CE marking or Approved Body engagement. The product's position is that it is not a medical device (`docs/compliance/INTENDED_PURPOSE.md`); no regulator or adviser has confirmed that (I.k). |
| Clinical review | **None.** No Clinical Safety Officer has reviewed the hazard log (I.j). |
| Real users or real data | **None.** Every demonstration uses fictional people; no real patient data has been collected. |
| Hosting | **None rented.** No server, domain or cloud account has been paid for (G4.d). |
| Staff | **None recorded** beyond the two founders who build IRA CARE. No support rota, clinical staff or employed engineers exist. |

## 2. Infrastructure assumptions

| # | Assumption | Figure | Source and evidence | Confidence |
| --- | --- | --- | --- | --- |
| I1 | Requests one watch makes in ordinary wear | about 1 to 3 a minute | Readings every 60 s by default (`reading_upload_interval_s`), a background sync every 15 minutes, urgent events at once: `docs/ops/HOSTING_PROPOSAL.md` | Estimate from the code's intervals; not measured on real wear |
| I2 | Throughput of one API process | 131.6, 124.1 and 118.3 requests a second at 10, 25 and 50 simulated watches; no errors | `docs/evidence/staging/containers.json` (load levels) | Measured, as a regression baseline; not a capacity test (G2.f) |
| I3 | Report latency under load | p95 142 ms, 312 ms and 730 ms at 10, 25 and 50 watches | `docs/evidence/staging/containers.json` | Measured |
| I4 | Help-request latency under load | p50 96 ms, 210 ms and 435 ms; p95 128 ms, 260 ms and 807 ms at 10, 25 and 50 watches | `docs/evidence/staging/containers.json` | Measured |
| I5 | What limits throughput | the single API process (83 to 95 % of one core); the database at about 60 % | `docs/ops/HOSTING_PROPOSAL.md`, `docs/evidence/staging/CONTAINERS.md` | Measured |
| I6 | Watches one API process could carry | in the order of a thousand | I1 and I2, keeping half in reserve: `docs/ops/HOSTING_PROPOSAL.md` | Estimate (extrapolation); the proposal does not depend on it |
| I7 | Care background jobs at 200 people | check-in pass mean 0.176 s, p95 0.337 s; monitoring pass mean 0.264 s, p95 0.502 s, against a five-minute step | `docs/evidence/soak/SOAK.md`, `docs/evidence/soak/soak.json` | Measured in simulated time; no 24-hour wall-clock run (G2.e) |
| I8 | Scheduling at 1,000 people in one workspace | 1.1 s per scheduling pass, 1.9 s with every check-in due, 0.65 s per monitoring pass | `docs/progress/FINAL_REPORT.md` (round 1 test counts) | Measured, locally, in round 1 |
| I9 | Server shape for a pilot of up to 50 people | one UK-region VM, 4 vCPU, 16 GB, 100 GB disk | `docs/ops/HOSTING_PROPOSAL.md` (pilot tier); the same shape the stack was measured on | Estimate grounded in the measured configuration |
| I10 | Container image sizes | API 1.55 GB, dashboard 0.35 GB | `docs/ops/HOSTING_PROPOSAL.md`, `docs/evidence/staging/containers.json` | Measured |
| I11 | Extra memory for the conversation models on a CPU server | about 10 GB; `gemma4:e4b` 6.6 GB and `gemma4:e2b` 4.6 GB as pulled | `docs/ops/HOSTING_PROPOSAL.md`, `docs/evidence/demo/REAL_MODEL_RUN.md` | Measured sizes; memory need estimated |
| I12 | Model latency on 4 CPU cores, no GPU (care journey) | check-in question median 4.66 s; talk reply median 8.72 s (first token 1.69 s); answer reading median 11.89 s, worst 24.02 s | `docs/evidence/demo/REAL_MODEL_RUN.md` | Measured on a shared container with other work running |
| I13 | Model latency on the deployment-sized CPU machine | first sentence 2.8 s on the evaluation set; 3.5 to 5.5 s on a whole talk turn | `docs/MODEL_CHOICE.md` (GPU profile section, CPU comparison) | Measured |
| I14 | Model latency on a GPU (NVIDIA Tesla T4, 16 GB) | `gemma4:e4b` first token 0.10 s, first sentence p50 0.18 s; chosen GPU chain (`gemma4:12b` then `gemma4:e4b`) first sentence p50 0.92 s, p95 1.14 s | `docs/MODEL_CHOICE.md`, `docs/evidence/training/gpu_bench.json`, `docs/evidence/eval/gpu/` | Measured on Kaggle's free GPU |
| I15 | The product needs a model at all | no: rules alone pass the safety measures; the model improves wording | `deploy/compose.nomodel.yml`, `docs/MODEL_CHOICE.md` (rules-only row) | Measured on the 150-turn evaluation set |
| I16 | Server speech recognition cost | median 3.9 s per utterance (CPU, int8); peak memory about 850 to 880 MiB | `docs/ai/speech/RESULTS.md` | Measured on a shared container |
| I17 | Activity model cost on the watch | 13.66 ms per 10 s window (median) on the JVM | `docs/ai/activity/MODEL_CARD.md` | Measured on the JVM, not on a watch; battery cost unmeasured (F7.k) |
| I18 | Training compute | Kaggle's free GPU: fall model 186 s, classifier heads 48 s, conversation fine-tune about 2.6 h over four attempts, GPU benchmark 864 s | `docs/MODEL_CHOICE.md` ("Our own models"; "What a small GPU server would give") | Measured |
| I19 | CI cost per pull-request run | about 19 to 21 runner minutes when every area runs | `docs/evidence/ci/minutes.json` | Measured on GitHub Actions |
| I20 | Backup retention | 14 days kept locally; off-site lifecycle set by the operator | `docs/ops/HOSTING_PROPOSAL.md`, `docs/evidence/staging/OFFSITE.md` | Configuration |
| I21 | Data residency | all data in a UK region | `docs/compliance/DPIA.md` (section 6), `docs/ops/HOSTING_PROPOSAL.md` | Design requirement, not yet verified on a provider |
| I22 | Database storage growth per person | **not estimated** | No long-running measurement exists; the 24-hour soak is not done (G2.e) | — |

## 3. Financial assumptions

| # | Item | Figure | Source and evidence | Confidence |
| --- | --- | --- | --- | --- |
| F1 | Hosting cost per month, pilot or growth tier | **not estimated** | `scripts/hosting_cost.py` withholds the total because every price in `docs/ops/hosting_prices.json` is empty: the providers' price pages were unreachable from the build environment on 25 November 2026 | — |
| F2 | Hosting cost per person per month | **not estimated** | as F1 | — |
| F3 | A dedicated GPU server, if the GPU profile is chosen | €184 a month plus setup (Hetzner GEX44, RTX 4000 SFF Ada 20 GB); €214 a month (GEX45, 24 GB); €0.75 an hour, about €550 a month if left running (Scaleway L4-1-24G) | `docs/MODEL_CHOICE.md`, read from the providers' pages on 24 November 2026 | Published list prices; not quotations; re-check before use |
| F4 | SMS, call and email delivery costs | **not estimated** | Charged per message and minute by a provider not yet chosen (G3.e) | — |
| F5 | Monitoring-centre service | **not estimated** | No contract or quotation (G3.f) | — |
| F6 | Penetration test, Clinical Safety Officer, MHRA adviser, data protection officer | **not estimated** | No quotation exists | — |
| F7 | Apple and Google developer accounts | **not estimated** | Fees not read at source in this repository | — |
| F8 | Hardware unit cost (bill of materials) | **not estimated** | The ODM specification asks for unit cost at 1k, 10k and 50k (`docs/hardware/ODM_SPECIFICATION.md`); no answer exists | — |
| F9 | Radio, EMC and safety testing for an LTE wearable | €1,500 to €3,000 for radio alone; $10,000 to $20,000 for a full programme | `docs/hardware/BLUEPRINT.md` (section 8.9) | Estimate from secondary sources |
| F10 | GCF membership | €5,000 to €10,000 a year | `docs/hardware/BLUEPRINT.md` (section 8.9) | Published (official) |
| F11 | IP68 test | about $550 at a low-cost laboratory | `docs/hardware/BLUEPRINT.md` (section 8.9) | Estimate |
| F12 | MHRA registration, only if the product were a medical device | £300 per GMDN category per year | `docs/hardware/BLUEPRINT.md` (section 8.9) | Published (official) |
| F13 | UKCA Class IIa conformity assessment, only if medical | no UK fee schedule published; EU comparators €9,600 (document review) to €32,000 to €110,000 (project), 9 to 18 months | `docs/hardware/BLUEPRINT.md` (section 8.9) | Estimate |
| F14 | EVT, DVT and PVT timeline | estimates by the document's author, not quotations | `docs/hardware/EVT_DVT_PVT_ROADMAP.md` | Estimate |
| F15 | Engineering headcount and salaries | **not estimated** | Nothing in the repository | — |
| F16 | Price to customers, revenue, margins | **not estimated** | No pricing decision, customer or market evidence exists (section 1) | — |
| F17 | Model licence fees | none: Gemma 4, faster-whisper and the other shipped models are under Apache 2.0 or MIT; no hosted model API is used | `docs/ai/MODEL_WEIGHTS.md`, `docs/MODEL_CHOICE.md` (D-056) | Licences read at source |

## 4. How to keep this honest

- Replace a "not estimated" only with a figure that has a source: a provider's published price
  with the date read, a quotation, or a measurement in this repository.
- When a hosting price is filled in `docs/ops/hosting_prices.json`, run
  `python3 scripts/hosting_cost.py docs/ops/hosting_prices.json --tier pilot` and copy the
  result here with the date.
- Re-measure I2 to I6 on the rented server (G2.f) before quoting any capacity to anyone.
- Inputs for three-year projections that build on these rows (cost drivers, hardware stages,
  subscription options with comparable prices, deployment effort) are in
  [`docs/business/ASSUMPTIONS_FOR_PROJECTIONS.md`](../business/ASSUMPTIONS_FOR_PROJECTIONS.md).

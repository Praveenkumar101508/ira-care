# Datasets surveyed for IRA CARE's own models

Round 3, phase 5. Searched on 23 November 2026 through the Kaggle API, the Hugging Face
datasets API, PhysioNet, the UCI repository and Zenodo. "Licence" is what the source
states; a Kaggle re-upload's licence does not override the original author's terms, so the
original is what counts. **Only rows marked "commercial use allowed" may train anything that
ships.** None of these datasets names an identifiable person; the sensor sets are anonymised
subject ids, and the dialogue sets are crowd-written.

**Re-verified on 25 November 2026 (completion register item F1).** Every row below was checked
again on that date against the source named in its "Verified (2026-11-25)" cell. Where the
original source could not be reached from the build environment, the cell says
**not verified: source unreachable from the build environment** and gives the URL to check.
Nothing in a "not verified" cell is a finding: it is the address to read. The weights of the
models the product ships or downloads are in [`docs/ai/MODEL_WEIGHTS.md`](ai/MODEL_WEIGHTS.md).
Permission and licence-confirmation drafts, for the founders to send, are in
[`docs/legal/dataset_permission_requests/`](legal/dataset_permission_requests/).

How the cells are read (and checked by
`supracloud-jarvis/ira/tests/readiness/test_dataset_register.py`):

- **Commercial use**: `yes` (the licence the original source states allows it), `no` (the
  source says non-commercial or research only), or `unclear` (the original terms were not
  readable from here, conflict, or come only from a re-upload). `unclear` is treated as `no`
  for training until confirmed.
- **Used for**: what the repository does with it. `shipped model: training` means weights
  trained on it ship in the product. A row whose use mentions training must have a licence and
  a verification date. A row used for a shipped model may not say `no`, and if it says
  `unclear` it must name the confirmation draft that is waiting to be sent.

## What changed on 25 November 2026

- **SisFall**: the Zenodo record 22212281 states CC BY 4.0, but it names no creator (the
  creator field is "None"), has no description, and was published on 31 August 2026. It is a
  re-upload by an unnamed account, **not** demonstrably the authors' record, as this file said
  before. The authors' own terms (Universidad de Antioquia, SISTEMIC) could not be read from
  here. Commercial use is therefore `unclear`. The shipped watch fall model was trained on it,
  so a confirmation request to the authors is drafted
  ([`SISFALL.md`](legal/dataset_permission_requests/SISFALL.md)) and the owner must decide
  whether to keep shipping that model until the answer arrives.
- **KFall**: restricted, per the owner. Access is by request and approval on the authors' page,
  and passing the data on to a third party is not allowed (reported by a search-engine index
  of the official page; the page itself was unreachable). Permission draft written.
- **FallAllD**: the original IEEE DataPort page could not be read; no licence was found for
  the dataset in any reachable source. Commercial use `unclear`; permission draft written.
- **UMAFall**: the owner reports figshare version 8 as CC BY 4.0. A search-engine index of a
  University of Málaga repository record lists *Attribution-NonCommercial 4.0* for an UMAFall
  item; it is not clear whether that record is the dataset or the paper. Neither page was
  reachable from here, so commercial use is `unclear` and a short confirmation draft is
  written.
- **MobiFall**: a search-engine index of the laboratory's page says the data is given on
  request "for non-commercial, research and educational purposes only", after a signed
  database usage agreement. Recorded as `no` (with the caveat that the page itself was not
  read); it was never used.
- **MIMIC-III waveform (matched)**: the licence is the **Open Data Commons Open Database
  License (ODbL) v1.0**, not a generic "Open Data Commons" licence.
- **HARTH, UNIUD, Falls vs Normal Activities**: still allowed, but no script in this
  repository uses them; they were described as if in use. Corrected to "candidate, not used".
- **UNIUD**: uploaded to Kaggle by the authors' laboratory account (MITEL-UNIUD), so the Kaggle
  page is the original source. The phone was worn at the arm, in a pocket and at the wrist, not
  only at the wrist.
- **Wearable PPG, ACC and ECG** (Kaggle): CC BY 4.0, but its own description says "intended
  for research and educational use", and it names no study or institution. `unclear`.
- **BIDSleep epochs** (Kaggle): a reduction of the PhysioNet BIDSleep dataset v1.0.1, which is
  Open Data Commons Attribution License v1.0. The PhysioNet citations are now listed.
- Dataset citations (DOIs and the citation text the sources ask for) were added wherever the
  source gives one. The existing PPG-DaLiA citation is kept.

## Fall detection and daily activity (wrist or body accelerometer and gyroscope)

| Dataset | Where (original source) | Contents | Licence | Commercial use | Used for | Verified (2026-11-25) | Citation and conditions |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SisFall | Copy used: [Zenodo 22212281](https://zenodo.org/records/22212281), DOI 10.5281/zenodo.22212281 (re-upload, creator not named). Original: SISTEMIC, Universidad de Antioquia, <http://sistemic.udea.edu.co/en/investigacion/proyectos/english-falls/> | 238 MB zip; waist-worn ADXL345 and ITG3200 accelerometer + gyroscope, 200 Hz; 15 fall types, 19 daily activities; 38 subjects (23 aged 19 to 30, 15 aged 60 to 75) | Zenodo copy: CC BY 4.0 (stated on the record). Original authors' terms: not verified | unclear | shipped model: training (the watch fall model, `watch/app/src/main/assets/fall_model.tflite`, resampled to 50 Hz) | Zenodo record read through its API, <https://zenodo.org/api/records/22212281>: licence `cc-by-4.0`, creator "None", no description, published 2026-08-31. Original page **not verified: source unreachable from the build environment**; check <http://sistemic.udea.edu.co/en/investigacion/proyectos/english-falls/> and the supplementary material of the paper, <https://doi.org/10.3390/s17010198>. Confirmation draft: `docs/legal/dataset_permission_requests/SISFALL.md` | Cite: A. Sucerquia, J. D. López, J. F. Vargas-Bonilla, "SisFall: A Fall and Movement Dataset", *Sensors* 2017, 17(1), 198, doi:10.3390/s17010198 (paper reference as indexed; the article page was unreachable). CC BY 4.0 (if it applies) requires credit to the creator |
| KFall | Original: the authors' page, <https://sites.google.com/view/kfalldataset> (Korea Advanced Institute of Science and Technology). Re-upload seen: [Kaggle usmanabbasi2002/kfall-dataset](https://www.kaggle.com/datasets/usmanabbasi2002/kfall-dataset) | 148 MB; low-back IMU (accelerometer, gyroscope, Euler angles), 100 Hz; 15 fall types, 21 activities; 32 young adults | Restricted: access by request and approval on the authors' page (owner's correction; the page itself not read here). The Kaggle copy says MIT, which does not override the authors' terms; no until permission is granted | no | not used (was listed as a possible cross-dataset check; no script uses it) | Kaggle page read (licence field: MIT; uploader Usman Abbasi, not an author). Authors' page **not verified: source unreachable from the build environment**; check <https://sites.google.com/view/kfalldataset>. A search-engine index of that page reports: requests are reviewed with the requester's identity and affiliation, access is granted by email, and the data may not be passed to any third party. Permission draft: `docs/legal/dataset_permission_requests/KFALL.md` | Cite: X. Yu, J. Jang, S. Xiong, "A Large-Scale Open Motion Dataset (KFall) and Benchmark Algorithms for Detecting Pre-impact Fall of the Elderly Using Wearable Inertial Sensors", *Frontiers in Aging Neuroscience* 2021, doi:10.3389/fnagi.2021.692865 (as indexed; the article page was unreachable; use the citation format the KFall page gives). Do not use the Kaggle copy |
| MobiFall v2.0 | Original: Biomedical Informatics Laboratory, Hellenic Mediterranean University, <https://bmi.hmu.gr/> . Re-upload seen: [Kaggle sha0401/mobifall-dataset-v2-0](https://www.kaggle.com/datasets/sha0401/mobifall-dataset-v2-0) | 95 MB; smartphone in a trouser pocket, accelerometer + gyroscope, up to 100 Hz; 24 subjects | Original: given on request for non-commercial, research and educational purposes only, after a signed database usage agreement (search-engine index of the laboratory's page). The Kaggle copy says Apache 2.0, which does not override that | no | not used | Kaggle page read (licence field: Apache 2.0; uploader "Sha-Bytes", not an author). Laboratory page **not verified: source unreachable from the build environment**; check <https://bmi.hmu.gr/?p=1642> | Cite: G. Vavoulas, M. Pediaditis, C. Chatzaki, E. G. Spanakis, M. Tsiknakis, "The MobiFall Dataset" (2014), as the laboratory asks (as indexed; check the full reference on the laboratory page). No permission draft: research-only terms, and it is not needed |
| UMAFall | Original: figshare, <https://figshare.com/articles/dataset/UMA_ADL_FALL_Dataset_zip/4214283> (version 8, DOI 10.6084/m9.figshare.4214283.v8), Universidad de Málaga | 85 MB; smartphone + 4 wearable IMUs (one at the wrist), 20 Hz (wearables); 17 subjects | The owner reports CC BY 4.0 for version 8. A search-engine index of a University of Málaga repository record (<https://riuma.uma.es/xmlui/handle/10630/41001>) lists Attribution-NonCommercial 4.0 for an UMAFall item; whether that record is the dataset or the paper is not known | unclear | not used | figshare and the Málaga repository: **not verified: source unreachable from the build environment**; check <https://figshare.com/articles/dataset/UMA_ADL_FALL_Dataset_zip/4214283> (licence field of version 8). Confirmation draft: `docs/legal/dataset_permission_requests/UMAFALL.md` | Cite: E. Casilari, J. A. Santoyo-Ramón, J. M. Cano-García, "UMAFall: A Multisensor Dataset for the Research on Automatic Fall Detection", *Procedia Computer Science* 110 (2017) 32-39 (not checked at source), and the figshare record with its version DOI; use whatever citation the figshare record asks for |
| FallAllD (derived) | Original: IEEE DataPort, <https://ieee-dataport.org/open-access/fallalld-comprehensive-dataset-human-falls-and-activities-daily-living> . Derived copy seen: [Kaggle sankalpsinghvishen/derived-fallalld-dataset](https://www.kaggle.com/datasets/sankalpsinghvishen/derived-fallalld-dataset) | 569 MB; wrist, waist and neck IMUs, 238 Hz; 15 subjects | Original: not found. IEEE DataPort's general terms say content is under CC BY unless IEEE permits otherwise (search-engine index; not read here). The derived Kaggle copy says CC0, which the uploader (not an author) cannot grant | unclear | not used | Kaggle page read (licence field: CC0; uploader Sankalp Singh). IEEE DataPort page and its terms **not verified: source unreachable from the build environment**; check the dataset page above and <https://ieee-dataport.org/ieee-dataport-terms-use>. Permission draft: `docs/legal/dataset_permission_requests/FALLALLD.md` | Cite: M. Saleh, M. Abbas, R. L. B. Jeannès, "FallAllD: An Open Dataset of Human Falls and Activities of Daily Living for Classical and Deep Learning Applications", *IEEE Sensors Journal*, doi:10.1109/JSEN.2020.3018335 (as given in the derived Kaggle copy's description; check on the DataPort page) |
| Falls vs Normal Activities | [Kaggle enricogrimaldi/falls-vs-normal-activities](https://www.kaggle.com/datasets/enricogrimaldi/falls-vs-normal-activities) (the collector's own upload) | 1.3 MB; accelerometer + gyroscope, 20 Hz, 242 observations of 20 s | MIT (stated) | yes | not used (too small and featurised to train on; no script uses it) | Kaggle page read, <https://www.kaggle.com/datasets/enricogrimaldi/falls-vs-normal-activities>: licence field MIT, creator Enrico Grimaldi | MIT: keep the copyright and licence notice with any copy. No paper is named |
| UCI HAR (smartphones) | [UCI 240](https://archive.ics.uci.edu/dataset/240/human+activity+recognition+using+smartphones), DOI 10.24432/C54S4K | 10,299 windows; waist smartphone accelerometer + gyroscope, 50 Hz; six activities, no falls; 30 subjects, 19 to 48 | CC BY 4.0 (stated on the page) | yes | shipped model: training (daily-activity negatives for the watch fall model: walking, sitting, lying, stairs) | UCI page read, <https://archive.ics.uci.edu/dataset/240/human+activity+recognition+using+smartphones>: CC BY 4.0, "sharing and adaptation of the datasets for any purpose, provided that the appropriate credit is given" | Cite: J. Reyes-Ortiz, D. Anguita, A. Ghio, L. Oneto, X. Parra (2013), Human Activity Recognition Using Smartphones [Dataset], UCI Machine Learning Repository, doi:10.24432/C54S4K; and D. Anguita, A. Ghio, L. Oneto, X. Parra, J. L. Reyes-Ortiz, "A Public Domain Dataset for Human Activity Recognition using Smartphones", ESANN 2013 |
| HARTH | [UCI 779](https://archive.ics.uci.edu/dataset/779/harth), DOI 10.24432/C5NC90 | 6.46 M samples; two accelerometers (thigh, lower back), 50 Hz; 12 free-living activities, no falls; 22 subjects | CC BY 4.0 (stated on the page) | yes | not used (candidate for free-living negatives) | UCI page read, <https://archive.ics.uci.edu/dataset/779/harth>: CC BY 4.0 | Cite: A. Logacjov, A. Kongsvold, K. Bach, H. B. Bårdstu, P. J. Mork, HARTH [Dataset], UCI Machine Learning Repository, doi:10.24432/C5NC90; and "HARTH: A Human Activity Recognition Dataset for Machine Learning" (Logacjov, Bach, Kongsvold, Bårdstu, Mork, 2021) |
| UNIUD daily activities (UNIUD DA-AD) | [Kaggle vdellamea/uniud-daily-activities-accelerometer-dataset](https://www.kaggle.com/datasets/vdellamea/uniud-daily-activities-accelerometer-dataset) (uploaded by the authors' laboratory account, MITEL-UNIUD) | 1.6 MB; smartphone accelerometer at the arm, in a pocket and at the wrist; 8 household activities; 10 subjects | CC BY 4.0 (stated) | yes | not used (candidate for wrist daily-activity negatives) | Kaggle page read: licence field CC BY 4.0, creator MITEL-UNIUD | Cite (the page asks for it): V. Della Mea, O. Quattrin, M. Parpinel, "A feasibility study on smartphone accelerometer-based recognition of household activities and influence of smartphone position", *Informatics for Health and Social Care* 42:4 (2017) 321-334, doi:10.1080/17538157.2016.1255214 |

## Heart rate and PPG

| Dataset | Where (original source) | Contents | Licence | Commercial use | Used for | Verified (2026-11-25) | Citation and conditions |
| --- | --- | --- | --- | --- | --- | --- | --- |
| PPG-DaLiA | [UCI 495](https://archive.ics.uci.edu/dataset/495/ppg+dalia), DOI 10.24432/C53890 | 8.3 M samples; wrist PPG 64 Hz, wrist accelerometer 32 Hz, chest ECG reference; 15 subjects, daily activities | CC BY 4.0 (stated on the page) | yes | shipped model: training (IRA Care Heart learned candidate selector, `supracloud-jarvis/ira/care/models/heart_selector.json`, trained on all 15 subjects) and evaluation (F3: leave-one-subject-out evaluation of it and of the classical estimator, `scripts/training/heart_ppg_dalia.py`, results and model card in `docs/ai/heart/`). Downloaded outside the repository, never committed; the shipped JSON holds tree weights only, no signal data | UCI page read, <https://archive.ics.uci.edu/dataset/495/ppg+dalia>: CC BY 4.0, "sharing and adaptation ... for any purpose, provided that the appropriate credit is given". Re-read 2026-11-25 for the learned model: the page still states CC BY 4.0 (link to the 4.0 legal code); UCI API record 495 gives DOI 10.24432/C53890; archive SHA-256 `5772387956e3...a1f3` matched | Cite: A. Reiss, I. Indlekofer, P. Schmidt, K. Van Laerhoven, "Deep PPG: Large-Scale Heart Rate Estimation with Convolutional Neural Networks", *Sensors* 2019, 19(14), 3079; and the dataset, Reiss, A., Indlekofer, I., Schmidt, P. (2019), PPG-DaLiA [Dataset], UCI Machine Learning Repository, doi:10.24432/C53890 |
| Wearable PPG, ACC and ECG heart rate | [Kaggle antonjuk/wearable-ppg-acc-and-ecg-heart-rate-dataset](https://www.kaggle.com/datasets/antonjuk/wearable-ppg-acc-and-ecg-heart-rate-dataset) | 347 MB; 62 sessions, 27,636 ten-second windows of wrist PPG + accelerometer with ECG-derived heart rate | CC BY 4.0 (licence field), but the description says "intended for research and educational use"; no study, institution or ethics approval named | unclear | not used (reference only) | Kaggle page read: licence field CC BY 4.0, uploader "antonjuk" | No citation given. Provenance and consent unknown: do not train on it |
| MIMIC-III waveform (matched) | [PhysioNet mimic3wdb-matched 1.0](https://physionet.org/content/mimic3wdb-matched/1.0/), DOI 10.13026/c2294b | very large; bedside monitor waveforms; hospital patients | Open Data Commons Open Database License (ODbL) v1.0; open access | yes | not used (not relevant to a wrist device) | PhysioNet page read, <https://physionet.org/content/mimic3wdb-matched/1.0/>: ODbL v1.0, open access | ODbL: attribution, share-alike for a public adapted database. Cite: B. Moody, G. Moody, M. Villarroel, G. D. Clifford, I. Silva (2020), MIMIC-III Waveform Database Matched Subset (version 1.0), PhysioNet; A. E. W. Johnson et al., "MIMIC-III, a freely accessible critical care database", *Scientific Data* 3, 160035 (2016); and the PhysioNet standard citation (Pollard et al., 2026, *Nature Health*) |

## Sleep

| Dataset | Where (original source) | Contents | Licence | Commercial use | Used for | Verified (2026-11-25) | Citation and conditions |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Sleep-EDF Expanded | [PhysioNet sleep-edfx 1.0.0](https://physionet.org/content/sleep-edfx/1.0.0/), DOI 10.13026/C2X676 | 8.1 GB; polysomnography (EEG, EOG, EMG), 100 Hz, with sleep stages; 197 nights, 20 to 101 years | Open Data Commons Attribution License v1.0 (stated) | yes | not used (reference; a watch has no EEG) | PhysioNet page read, <https://physionet.org/content/sleep-edfx/1.0.0/>: ODC Attribution v1.0, open access | Cite (the page asks for it): B. Kemp, A. H. Zwinderman, B. Tuk, H. A. C. Kamphuisen, J. J. L. Oberyé, "Analysis of a sleep-dependent neuronal feedback loop: the slow-wave microcontinuity of the EEG", *IEEE-BME* 47(9):1185-1194 (2000); and the PhysioNet standard citation (Pollard et al., 2026, *Nature Health*, doi:10.1038/s44360-026-00096-z) |
| BIDSleep Apple Watch sleep epochs | Kaggle reduction [menes97/bidsleep-apple-watch-sleep-epochs](https://www.kaggle.com/datasets/menes97/bidsleep-apple-watch-sleep-epochs) of the original [PhysioNet bidsleep-dataset 1.0.1](https://physionet.org/content/bidsleep-dataset/1.0.1/), DOI 10.13026/rees-1092 | 12 MB; 30 s epochs of Apple Watch heart rate and accelerometer with expert-corrected Dreem 2 stages; 47 adults, 253 nights | Open Data Commons Attribution License v1.0 (original, on PhysioNet; the Kaggle reduction states the same) | yes | not used (candidate for a later wrist sleep model) | PhysioNet page read, <https://physionet.org/content/bidsleep-dataset/1.0.1/>: ODC Attribution v1.0, open access. Kaggle page read: ODC-By | Cite: T. Song, Y. Zhang, Z. Zhou, J. Dutta (2026), A Multi-Night Instantaneous Heart Rate and Accelerometry Dataset with EEG Sleep Stage Labels (version 1.0.1), PhysioNet, doi:10.13026/rees-1092; T.-A. Song et al., "AI-driven sleep staging using instantaneous heart rate and accelerometry: Insights from an Apple Watch study", *IEEE Transactions on Biomedical Engineering* (2025), doi:10.1109/TBME.2025.3612158; and the PhysioNet standard citation. The authors ask for subject-level splits |

## Empathetic, elderly and care conversations

| Dataset | Where (original source) | Contents | Licence | Commercial use | Used for | Verified (2026-11-25) | Citation and conditions |
| --- | --- | --- | --- | --- | --- | --- | --- |
| EmpatheticDialogues | [Hugging Face facebook/empathetic_dialogues](https://huggingface.co/datasets/facebook/empathetic_dialogues) | 25 k crowd-written emotional situations and empathetic replies | CC BY-NC 4.0 (stated) | no | not used (for anything that ships); the Kaggle re-upload (CC BY-NC-SA) is the same | Hugging Face API read, <https://huggingface.co/api/datasets/facebook/empathetic_dialogues>: `license: cc-by-nc-4.0` | Cite if ever used for research: H. Rashkin, E. M. Smith, M. Li, Y.-L. Boureau, "Towards Empathetic Open-domain Conversation Models: a New Benchmark and Dataset", ACL 2019 (the citation in the Hugging Face dataset card) |
| Empathetic conversational model benchmark | [Kaggle thedevastator/empathetic-conversational-model-benchmark](https://www.kaggle.com/datasets/thedevastator/empathetic-conversational-model-benchmark) | 4.7 MB; a re-packaging of the above | CC0 on Kaggle, but derived from a CC BY-NC source, which the re-uploader cannot relicense | no | not used | Kaggle page read: licence field CC0, description names Hugging Face `empathetic_dialogues` as the source | As above |
| Mental health conversational training data | [Kaggle nguyenletruongthien/mental-health](https://www.kaggle.com/datasets/nguyenletruongthien/mental-health) | 97 MB; "510,000+" counsellor-style exchanges, community discussions and synthetic conversations from several sources | CC BY 4.0 (licence field); provenance mixed and not itemised | unclear | not used (medical-adjacent advice is exactly what IRA must not learn) | Kaggle page read: licence field CC BY 4.0, creator Nguyen Le Truong Thien | The sources it combines are not named, so their own terms cannot be checked |
| Elderly or care-specific conversation sets | searched "elderly care conversation", "medication reminder dialogue" on Kaggle and Hugging Face (23 November 2026) | — | none found | — | — | not repeated | — |

**Conversation training therefore uses no public dialogue dataset.** The fine-tuning set is
synthetic: care conversations generated by the chosen base model from the care context
(check-in kinds, routines, medication due, the person's preferred name) and filtered by the
product's own rules (no diagnosis words, no advice, warm, short, one question), plus the
hand-written evaluation turns' categories as prompts. The whole generation is a script in
this repository (`scripts/training/`), so it can be repeated and audited. The base model's own
terms (Gemma 4, Apache 2.0) are recorded in [`docs/ai/MODEL_WEIGHTS.md`](ai/MODEL_WEIGHTS.md).

## Blocked here, for the owner

- From the build environment on 25 November 2026 these hosts could not be reached (the
  network proxy refused them, or they did not resolve): `sistemic.udea.edu.co`,
  `sites.google.com`, `figshare.com`, `api.figshare.com`, `riuma.uma.es`,
  `ieee-dataport.org`, `bmi.hmu.gr`, `www.mdpi.com`, `doi.org`, `www.frontiersin.org`,
  `pmc.ncbi.nlm.nih.gov` and `ai.google.dev`. The original terms of SisFall, KFall, MobiFall,
  UMAFall and FallAllD were therefore not read; each row says which page to open.
  Search-engine summaries of some of those pages were used only to find the right page and
  contact route, never as verification.
- Reachable and read: `archive.ics.uci.edu`, `zenodo.org`, `physionet.org`, `huggingface.co`,
  `www.kaggle.com` (each dataset page's embedded metadata), `ollama.com` and
  `registry.ollama.ai`.
- Kaggle notebook output files download through `www.kaggleusercontent.com`, which is not
  allowed either; training results are read back through the notebook log instead (see
  `docs/MODEL_CHOICE.md`), which is enough for metrics and for a small TensorFlow Lite model
  carried as text.
- **Owner decision needed (SisFall).** The watch fall model that ships was trained on a SisFall
  copy whose licence comes from an unnamed uploader. Until the authors confirm their terms,
  either accept that risk in writing or ship the rule detector alone and retrain on data with
  a confirmed commercial licence.

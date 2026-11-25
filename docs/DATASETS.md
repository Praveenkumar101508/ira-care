# Datasets surveyed for IRA CARE's own models

Round 3, phase 5. Searched on 23 November 2026 through the Kaggle API, the Hugging Face
datasets API, PhysioNet, the UCI repository and Zenodo. "Licence" is what the source
states; a Kaggle re-upload's licence does not override the original author's terms, so the
original is what counts. **Only rows marked "commercial use allowed" may train anything that
ships.** None of these datasets names an identifiable person; the sensor sets are anonymised
subject ids, and the dialogue sets are crowd-written.

Legend: ✅ commercial use allowed · 🔬 research only · ❔ unclear (terms not reachable or not
stated; treated as research only).

## Fall detection and daily activity (wrist or body accelerometer and gyroscope)

| Dataset | Where | Size | Sensors, rate | Population | Licence | Use |
| --- | --- | --- | --- | --- | --- | --- |
| SisFall | [Zenodo 22212281](https://zenodo.org/records/22212281) (the authors' record; original from Universidad de Antioquia) | 238 MB zip | waist-worn ADXL345 and ITG3200 accelerometer + gyroscope, 200 Hz; 15 fall types, 19 daily activities | 38 subjects (23 aged 19 to 30, 15 aged 60 to 75) | CC BY 4.0 (stated on the Zenodo record) | ✅ **primary training set** (resampled to the watch rate) |
| KFall | [Kaggle usmanabbasi2002/kfall-dataset](https://www.kaggle.com/datasets/usmanabbasi2002/kfall-dataset) (re-upload) | 148 MB | low-back IMU (accelerometer, gyroscope, Euler angles), 100 Hz; 15 fall types, 21 activities | 32 young adults | the Kaggle copy says MIT; the original authors' terms (Korea Advanced Institute of Science and Technology) could not be reached from this build machine | ❔ held out as a cross-dataset check only, not trained on |
| MobiFall v2.0 | [Kaggle sha0401/mobifall-dataset-v2-0](https://www.kaggle.com/datasets/sha0401/mobifall-dataset-v2-0) (re-upload) | 95 MB | smartphone in a trouser pocket, accelerometer + gyroscope, up to 100 Hz | 24 subjects | the Kaggle copy says Apache-2.0; the original (Hellenic Mediterranean University) is released under a research agreement | 🔬 not used |
| UMAFall | Kaggle re-uploads of a figshare record | 85 MB | smartphone + 4 wearable IMUs (one at the wrist), 20 Hz (wearables) | 17 subjects | figshare record not reachable from this machine; Kaggle copies state no licence | ❔ not used |
| FallAllD (derived) | [Kaggle sankalpsinghvishen/derived-fallalld-dataset](https://www.kaggle.com/datasets/sankalpsinghvishen/derived-fallalld-dataset) | 569 MB | wrist, waist and neck IMUs, 238 Hz | 15 subjects | the derived copy says CC0-1.0; the original (IEEE DataPort) was not reachable from this machine | ❔ not used until the original terms are confirmed by the owner |
| Falls vs Normal Activities | [Kaggle enricogrimaldi/falls-vs-normal-activities](https://www.kaggle.com/datasets/enricogrimaldi/falls-vs-normal-activities) | 1.3 MB | windowed accelerometer features | small | MIT (stated) | ✅ too small and featurised to train on; used as a sanity check only |
| UCI HAR (smartphones) | [UCI 240](https://archive.ics.uci.edu/dataset/240/human+activity+recognition+using+smartphones) | 10,299 windows | waist smartphone accelerometer + gyroscope, 50 Hz; six activities, no falls | 30 subjects, 19 to 48 | CC BY 4.0 (stated on the page) | ✅ daily-activity negatives (walking, sitting, lying, stairs) |
| HARTH | [UCI 779](https://archive.ics.uci.edu/dataset/779/harth) | 6.46 M samples | two accelerometers (thigh, lower back), 50 Hz; 12 free-living activities, no falls | 22 subjects | CC BY 4.0 (stated on the page) | ✅ free-living negatives |
| WISDM Smartphone and Smartwatch Activity and Biometrics | [UCI 507](https://archive.ics.uci.edu/dataset/507/wisdm+smartphone+and+smartwatch+activity+and+biometrics+dataset), DOI 10.24432/C5HK59 | 310 MB zip | LG G Watch on the dominant wrist and a phone in the pocket, accelerometer (m/s²) + gyroscope (rad/s), nominal 20 Hz; 18 activities of 3 minutes each, no falls | 51 adult volunteers (no ages stated) | CC BY 4.0 (stated on the UCI page: "This allows for the sharing and adaptation of the datasets for any purpose"). Cite: Weiss, G. (2019). WISDM Smartphone and Smartwatch Activity and Biometrics Dataset [Dataset]. UCI Machine Learning Repository. https://doi.org/10.24432/C5HK59. Verified (2026-11-25) | ✅ Used for: IRA Care Activity (F7). Watch accelerometer and gyroscope only, never the phone; grouped-by-subject cross-validation (`scripts/training/activity_wisdm.py`, results and model card in `docs/ai/activity/`). Never used to identify individuals (the dataset also supports biometrics; that is a non-goal). Downloaded outside the repository, never committed |
| UNIUD daily activities | [Kaggle vdellamea/uniud-daily-activities-accelerometer-dataset](https://www.kaggle.com/datasets/vdellamea/uniud-daily-activities-accelerometer-dataset) | 1.6 MB | wrist accelerometer | small | CC BY 4.0 (stated) | ✅ wrist-worn daily-activity negatives |

## Heart rate and PPG

| Dataset | Where | Size | Sensors, rate | Population | Licence | Use |
| --- | --- | --- | --- | --- | --- | --- |
| PPG-DaLiA | [UCI 495](https://archive.ics.uci.edu/dataset/495/ppg+dalia) | 8.3 M samples | wrist PPG 64 Hz, wrist accelerometer 32 Hz, chest ECG reference | 15 subjects, daily activities | CC BY 4.0 (stated on the page). Cite: A. Reiss, I. Indlekofer, P. Schmidt, K. Van Laerhoven, "Deep PPG: Large-Scale Heart Rate Estimation with Convolutional Neural Networks", *Sensors* 2019, 19(14), 3079 | ✅ **used for IRA Care Heart (F3)**: leave-one-subject-out evaluation of the heart-rate estimator (`scripts/training/heart_ppg_dalia.py`, results and model card in `docs/ai/heart/`). Downloaded outside the repository, never committed; the shipped estimator is classical and carries no weights trained on it |
| Wearable PPG, ACC and ECG heart rate | [Kaggle antonjuk/wearable-ppg-acc-and-ecg-heart-rate-dataset](https://www.kaggle.com/datasets/antonjuk/wearable-ppg-acc-and-ecg-heart-rate-dataset) | 347 MB | wrist PPG + accelerometer, ECG reference | small | CC BY 4.0 (stated) | ✅ reference only |
| MIMIC-III waveform (matched) | [PhysioNet](https://physionet.org/content/mimic3wdb-matched/1.0/) | very large | bedside monitor waveforms | hospital patients | PhysioNet open access, Open Data Commons | ✅ not relevant to a wrist device; not used |

## Sleep

| Dataset | Where | Size | Sensors, rate | Population | Licence | Use |
| --- | --- | --- | --- | --- | --- | --- |
| Sleep-EDF Expanded | [PhysioNet sleep-edfx 1.0.0](https://physionet.org/content/sleep-edfx/1.0.0/) | 8.1 GB | polysomnography (EEG, EOG, EMG), 100 Hz, with sleep stages | 197 nights, 20 to 101 years | Open Data Commons Attribution License v1.0 (stated) | ✅ reference; a watch has no EEG, so not used for a shipped model |
| BIDSleep Apple Watch sleep epochs | [Kaggle menes97/bidsleep-apple-watch-sleep-epochs](https://www.kaggle.com/datasets/menes97/bidsleep-apple-watch-sleep-epochs) | 12 MB | wrist accelerometer + heart rate epochs with stages | small | ODC Attribution (stated) | ✅ candidate for a later wrist sleep model; not trained in this round |

## Empathetic, elderly and care conversations

| Dataset | Where | Size | Content | Licence | Use |
| --- | --- | --- | --- | --- | --- |
| EmpatheticDialogues | [Hugging Face facebook/empathetic_dialogues](https://huggingface.co/datasets/facebook/empathetic_dialogues) | 25 k conversations | crowd-written emotional situations and empathetic replies | CC BY-NC 4.0 (stated) | 🔬 non-commercial: **not used** for anything that ships; the Kaggle re-upload (CC BY-NC-SA) is the same |
| Empathetic conversational model benchmark | [Kaggle thedevastator/empathetic-conversational-model-benchmark](https://www.kaggle.com/datasets/thedevastator/empathetic-conversational-model-benchmark) | 4.7 MB | a re-packaging of the above | CC0 on Kaggle, but derived from a CC BY-NC source | 🔬 not used |
| Mental health conversational training data | [Kaggle nguyenletruongthien/mental-health](https://www.kaggle.com/datasets/nguyenletruongthien/mental-health) | 97 MB | counsellor-style exchanges from several public sources | CC BY 4.0 (stated); provenance mixed | ❔ not used: medical-adjacent advice is exactly what IRA must not learn |
| Elderly or care-specific conversation sets | searched "elderly care conversation", "medication reminder dialogue" on Kaggle and Hugging Face | — | — | none found | — |

**Conversation training therefore uses no public dialogue dataset.** The fine-tuning set is
synthetic: care conversations generated by the chosen base model from the care context
(check-in kinds, routines, medication due, the person's preferred name) and filtered by the
product's own rules (no diagnosis words, no advice, warm, short, one question), plus the
hand-written evaluation turns' categories as prompts. The whole generation is a script in
this repository (`scripts/training/`), so it can be repeated and audited.

## Blocked here, for the owner

- `api.figshare.com` and `ieee-dataport.org` are not on this machine's network allowlist,
  so the original terms of UMAFall and FallAllD could not be read; they stay ❔.
- Kaggle notebook output files download through `www.kaggleusercontent.com`, which is not
  allowed either; training results are read back through the notebook log instead (see
  `docs/MODEL_CHOICE.md`), which is enough for metrics and for a small TensorFlow Lite model
  carried as text.

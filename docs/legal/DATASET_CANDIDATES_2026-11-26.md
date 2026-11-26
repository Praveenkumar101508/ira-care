# Dataset candidates found on 26 November 2026

This is a search for datasets that could clear the remaining AI blockers: cross-dataset fall
evaluation (F4.f), older-adult speech accuracy (F8.c) and independent heart-rate evaluation.

Only licences that allow commercial use were accepted: CC BY, CC0, ODC-By or ODbL. Anything
non-commercial, no-derivatives, research-only or unclear was rejected, and is listed here with the
reason. Rows marked "search index" were not read at the source and must be checked before use.
The register of datasets actually used is [`../DATASETS.md`](../DATASETS.md).

## Fall detection (wrist)

| Dataset | Source | Licence | Wrist / rate | People | Verdict |
|---|---|---|---|---|---|
| WEDA-FALL | <https://github.com/joaojtmarques/WEDA-FALL> | ODC-By 1.0 (README and LICENSE file) | Fitbit Sense, 50 Hz | 14 young (350 falls onto a mattress), 11 aged 77–95 (ADLs only) | **Accepted**; used for F4.f (`docs/ai/fall/CROSS_DATASET_WEDA.md`) |
| FallAllD | IEEE DataPort, doi:10.21227/bnya-mn34 | "License: Creative Commons Attribution" (read on the DataPort page, 26 Nov) | wrist, waist, neck; 238 Hz | 15 | **Accepted**; download needs a free IEEE DataPort login (owner action) |
| IPqM-Fall | Zenodo, doi:10.5281/zenodo.20431609 | CC BY 4.0 | Galaxy Watch 4 on each wrist, 90 Hz | 15 military personnel | Accepted as a second wrist test set (not older adults) |
| ASSIST-IoT | Zenodo, doi:10.5281/zenodo.8340428 | CC BY 4.0 | 2 smartwatches | 10 | Accepted, low value: falls simulated with a mannequin |
| Simulated Falls and ADL (Erciyes) | UCI 455, doi:10.24432/C52028 | CC BY 4.0 | wrist among six sites; rate to confirm (believed 25 Hz) | 17 | Licence fine; check the rate before use |
| HAR70+ | UCI 780 | CC BY 4.0 | thigh and lower back only; no falls | 18 aged 70–95 | Not usable for falls |
| UP-Fall (HAR-UP) | <https://sites.google.com/up.edu.mx/har-up/> | not found | wrist among five IMUs | 17 | Rejected for now: terms unread (owner can open the page) |
| SmartFall | Texas State | not found | wrist, ~31 Hz | 14 | Rejected: unclear |
| SFU-IMU | FRDR | CC BY-ND 4.0 (search index) | no wrist | — | Rejected: no-derivatives |
| ShimFall&ADL | Zenodo 3901285 | CC BY-NC-ND 4.0 | chest | 35 | Rejected: non-commercial |
| MobiAct | Hellenic Mediterranean University | research only (as MobiFall) | phone | — | Rejected |
| FARSEEING | on request | ethics-bound, by request | lower back / thigh | real falls | Rejected for commercial use |
| KFall, SisFall, UMAFall | — | see the register | — | — | Permission requests drafted, unsent |

## Older-adult speech (for F8.c)

| Dataset | Source | Licence / terms | Older speakers | Verdict |
|---|---|---|---|---|
| Mozilla Common Voice, English 27.0 | Mozilla Data Collective, <https://datacollective.mozillafoundation.org/> | Dataset CC0-1.0 (search index); access needs an MDC account and acceptance of MDC terms in a browser | Self-declared ages: about 5 % of English clips aged 60+, under 1 % aged 70+ (from the release statistics) | **Best available**; owner action: create the account, read and accept the terms, and save a copy of them in `docs/legal/` |
| Common Voice Spontaneous Speech 3.0 | MDC | CC0-1.0 (search index) | age field present | Secondary |
| DementiaBank | TalkBank | membership; commercial products excluded | older adults | Rejected |
| Bridge2AI-Voice | PhysioNet | registered-access DUA; no raw audio there | to 90+ | Rejected |
| Meta Fair-Speech | Meta | custom limited licence; no 66+ | — | Rejected |
| VoxCeleb | Oxford | no transcripts; age labels non-commercial | — | Rejected |
| Speech Accent Archive | GMU | non-commercial (mirror) / unclear | — | Rejected |
| "Elderly" re-uploads on Hugging Face | various | no licence | — | Rejected: use the CC0 original instead |

No UK-English older-speaker corpus with a commercial licence was found.

## Heart rate (wrist PPG with an ECG reference)

| Dataset | Source | Licence | Wrist / rates | Reference | Verdict |
|---|---|---|---|---|---|
| GalaxyPPG | Zenodo, doi:10.5281/zenodo.14635823 | CC BY 4.0 | Galaxy Watch 5 PPG 25 Hz; Empatica E4 BVP 64 Hz | Polar H10 ECG | **Accepted**; used for the independent heart evaluation |
| Wrist PPG During Exercise | PhysioNet, doi:10.13026/C2PQ1X | ODC-By 1.0 | wrist PPG + IMU, 256 Hz | chest ECG | Accepted (small, high motion) |
| AAU Wearable Sleep Study | Zenodo 16919071 | CC BY 4.0 | Empatica E4 64 Hz | PSG ECG | Accepted (rest and sleep) |
| BUT PPG | PhysioNet | CC BY 4.0 | phone camera, not wrist | ECG | Not wrist |
| IEEE SPC 2015 | Zenodo re-upload 3902710 | original terms: academic use | wrist, 125 Hz | ECG | Rejected: academic-only condition |
| WESAD | Siegen / UCI | "scientific, non-commercial purposes" | E4 wrist | chest ECG | Rejected: non-commercial |
| WildPPG | ETH | conflicting: MIT on the HF card, CC BY-NC-SA in the paper | several sites | ECG | Rejected until the authors confirm |
| Long-term ECG + wrist PPG with AF | Zenodo 23921320 | other-nc | wrist 100 Hz | ECG | Rejected: non-commercial |

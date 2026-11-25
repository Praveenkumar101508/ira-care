# IRA Care Activity: data contract

How an activity estimate is described, summarised on the watch, sent, stored and shown.
Decision D-084; requirements F7.f to F7.k. The model itself is described in `MODEL_CARD.md`.

**What it is:** an estimate from the watch's motion sensor of what the wrist is doing. It is
**not** a measurement of health, it never says that someone ate or drank, and **no alert,
escalation, check-in or report is raised or changed by it**.

## 1. One window (on the watch only)

Every 10 seconds the watch classifies the last 10 s of accelerometer and gyroscope samples
(`ActivityMonitor`). Each window becomes an `ActivityWindow`:

| Field | Meaning |
|---|---|
| start, end | the window, in watch time |
| label | one of the model's classes (`walking`, `stairs`, `sitting`, `standing`, `eating_drinking`, `other`), or `unknown`, or `unavailable` |
| confidence | the top-class probability; none for `unavailable` |
| reason | `low_confidence` for `unknown`; for `unavailable` one of `off_wrist`, `sensor_gap`, `too_short`, `sensor_fault` |

- `unknown`: the model's top probability was below 0.5. The most likely class is **not** kept.
- `unavailable / off_wrist`: the watch said it was off the wrist.
- `unavailable / sensor_gap`: either sensor was silent for more than 0.5 s inside the window.
- `unavailable / too_short`: the model refused a window shorter than 8 s.
- `unavailable / sensor_fault`: the model refused the signal (flat, saturated, not finite, or an
  implausible gravity magnitude).

Windows never leave the watch.

## 2. One hour (what may leave the watch)

`ActivitySummariser` adds windows into hours (a window counts in the hour of its midpoint, so an
hour never holds more than 3,600 s). A closed hour is an `ActivityPeriod`:

```json
{
  "period_start": "2026-11-24T09:00:00Z",
  "period_end": "2026-11-24T10:00:00Z",
  "model_version": "ira-care-activity-mlp/1",
  "model_sha256": "06475f27740a2bc9ba7e736333966c94b2325a27c78f2bc9ce36b1395462c445",
  "source": "device",
  "seconds": {"walking": 600, "sitting": 1800, "unknown": 60, "unavailable": 330},
  "reasons": {"low_confidence": 60, "off_wrist": 300, "sensor_gap": 30},
  "windows": 279,
  "mean_confidence": 0.852
}
```

Rules the server enforces (`care/activity.py::check_period`; a broken period refuses the whole
batch with 422 and nothing is stored):

- an hour at most, longer than zero, with a timezone, not ending in the future;
- `model_version` and `model_sha256` name the model file the server holds (byte for byte the
  watch's asset); a summary from any other model is refused;
- `source` is `device` (the watch's own sensors) or `simulated` (a simulated-sensor build or a test);
- labels only from the closed set above; seconds are whole and not negative; they add up to no
  more than the period; the rest of the hour is **not recorded** and is never filled in;
- all `unknown` seconds carry `low_confidence`, and the `unavailable` seconds equal the sum of the
  four unavailable reasons;
- `windows` matches the seconds (10 s each), and `mean_confidence` lies between 0 and 1.

No sample, probability, candidate class or per-window time is sent.

## 3. Consent

Consent kind `activity_sharing` in `care_consents` (migration 121), granted by the person or their
legal representative and revocable like every other kind (the latest record wins).

- The watch learns it from `GET /care/devices/me` (`"activity": {"sharing": true|false}`; missing
  means no). Without it, closed hours are discarded on the watch, not queued.
- The server checks again on every `POST /care/devices/me/activity`. Not granted: nothing is
  stored and the answer is `{"sharing": false}`; the watch then clears what it was holding.
- Carers' reads show the trend only while it is granted; otherwise they see that it is not shared.

## 4. Sending and storing

- Path: `ActivitySharing.sync`, after the urgent outbox and the readings, in batches of 48 hours.
  Waiting hours are sealed on disk in the wearer's own file (two weeks at most). Offline or 5xx:
  kept. A definite refusal (4xx): that batch is dropped.
- Endpoint: `POST /api/v1/care/devices/me/activity`, device token only. Re-sent hours are ignored
  (unique on device and `period_start`).
- Table: `care_activity_periods`, tenant and workspace scoped with forced row-level security; the
  runtime may select, insert and delete, never update.
- Retention: the person's `data_retention` months (default 12), purged by `period_start` with the
  readings (`care/retention.py`).
- Erasure: with the person (`ON DELETE CASCADE` from `care_people`) and with the tenant.

## 5. What carers see

`GET /api/v1/care/people/{id}/activity?days=7&weeks=4` (person-level access as for readings; every
read written to the access log) and the panel "Activity (estimated)" on the person's
**Health and wellbeing** tab:

| Group shown | Model classes |
|---|---|
| Active (walking or on stairs) | walking, stairs |
| Still (sitting or standing) | sitting, standing |
| Other arm movement | eating_drinking, other |
| Could not tell, not worn or not recorded | unknown, unavailable, and time with no data |

Minutes per day (in the person's own timezone; an hour counts on the local day it starts) and per
seven-day week. The panel always carries: "Estimated from the watch's motion sensor; not a
measurement of health." The class name `eating_drinking` is never shown.

## 6. What the wearer sees

A small line on the watch's home screen when nothing else is happening: "On the move today: about
N min (the watch's estimate)". It is counted on the watch from the windows, whether or not anything
is shared.

## 7. What it never does

No module on the alert path reads `care_activity_periods` or imports `care/activity.py`, and
ingestion calls no safety rule (`tests/care/test_activity_no_alert_path.py`; behaviour:
`test_care_activity_db.py::test_a_day_of_stillness_off_the_wrist_and_unknown_opens_no_alert`).

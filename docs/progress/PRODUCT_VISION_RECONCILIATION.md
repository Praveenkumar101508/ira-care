# The original product vision against the source code (round 9, 27 November 2026)

The owner's directive asked for an audit of the original IRA CARE promises against what the
code does, naming anything missing from the atomic register, and in particular reconciling the
earlier talk of camera-assisted meal checks, blood pressure, ECG, SpO2, HRV and sleep. This page
is that audit. Every row names the code and the test that holds it; a promise that the current
watch cannot keep is listed as such, with how the product says so.

## Promises kept by code, with the test that pins each

| Promise (`docs/PRODUCT_SPEC.md`) | Where in the code | Pinned by |
|---|---|---|
| A check-in every 15 to 20 minutes while awake, randomised | `care/checkin_cycle.py`, `care/checkins.py` | `tests/care/test_care_checkin_rules.py`, `tests/tenancy/test_care_checkins_db.py` |
| Quiet hours (22:00 to 07:00 by default, per person), silent monitoring only | `checkins.rules.in_quiet_hours`, person settings | `test_care_checkin_rules.py::test_quiet_hours_across_midnight` and the empty-window test |
| The question repeated before anything escalates; voice and vibration on the wrist | `care/checkin_cycle.py` (repeat), `watch/.../checkin/` (speech and vibration) | `test_care_checkin_rules.py`; `CheckinControllerProfileTest`, `ReminderButtonsTest` |
| Medication reminders with a two-step confirmation; a dose written only from an unambiguous "yes" | `care/checkins.py::read_dose_answer`, `care/personal.py` (reminders), migration 112 | `tests/care/test_care_safety_answers.py` (the 18 mandatory ambiguous answers), `test_care_round9_scenarios.py` (30 more and the interrupted ones), `test_care_safety_answers_db.py`; on the emulator, `medication_taken_two_step` (run 45910155640) |
| Meals and hydration asked about and recorded as what the person says | check-in kinds `meal` and `hydration` (`care/personal.py`), `care/capabilities.py` `food_intake` | `test_care_personal_rules.py` ("drink of water"), `test_care_safety_answers.py::test_the_meal_reading_is_unchanged` |
| Scheduled care visits: who comes when, late and missed, arrival pauses check-ins, departure records the outcome | `care/visits.py` | `tests/care/test_care_visit_rules.py`, `tests/tenancy/test_care_visits_db.py` |
| Continuous monitoring: readings checked as they arrive, ask first, then escalate; the morning check | `care/continuous.py`, `care/askfirst.py` | `test_care_continuous_rules.py`, `test_care_continuous_db.py`, `test_care_askfirst*.py` |
| Emergency: SOS from the wrist with location, tiers, acknowledgement, arrival, resolution, no duplicate alerts | `care/sos.py`, `care/escalation.py`, `care/followup.py`, `care/location.py` | `test_care_sos_rules.py`, `test_care_sos_db.py`, `test_care_follow_up_db.py`; on the emulator, Help, the fall countdown, "is on the way" on the wrist, the offline queue delivered once |
| Family visibility (alerts, status, summaries; words only with consent) and carer visibility (timelines, reports, roles) | `care/consent.py`, `care/reports.py`, `care/staff.py`, the dashboard and the phone app | `test_care_consent*.py`, `test_care_reports_rules.py`, `test_care_staff_roles.py`, dashboard and mobile suites; on the emulator, the family's alert banner and acknowledgement |
| Personalised memory with consent, correction, deletion, retention; gentle redirection of repeated worries | `care/personal.py`, `care/retention.py`, `care/conversation.py` | `test_care_personal_rules.py`, `test_care_personal_db.py`, F6 items; the 162-turn set's `distress_redirect` turns |
| Accessibility: large targets, labels, text size, contrast, less motion | dashboard `accessibility.test.tsx`, mobile `accessibility.test.tsx`, watch `AccessibilityTest` | those suites; on the emulator, `accessibility_labels` and `accessibility_touch_targets` on eleven screens |
| Offline queues and degraded-state reporting (never "all well" when monitoring is not) | watch outbox (`FallOutbox`, the Help queue), `care/monitoring.py`, the apps' stale states | `test_care_monitoring_rules.py`; on the emulators, `offline_queue_delivered_once`, `reboot_queue_delivered_once`, `offline_state_shown` |
| Secure account isolation | `care/authz.py`, the person guard, row-level security | `test_care_person_guard.py`, the tenancy suites (4,400 database tests), `app_signout.test.tsx`; on the emulator, `carer_home_isolated` (written; its run is pending) |

## Promises the current watch cannot keep, and how the product says so

`care/capabilities.py` is the one list the API (`GET /capabilities`), the dashboard and the tests
share; `docs/compliance/CLAIMS_REGISTER.md` carries the same rows as "not claimed".
`tests/care/test_care_capabilities.py` holds the backend to accepting exactly the readings the
watch sends (`test_a_reading_ira_cannot_measure_is_refused`) and the dashboard to drawing a card
only for what is measured.

| Earlier promise | State in the product | Register item |
|---|---|---|
| Camera-assisted meal verification | `camera: not_available`: the watch has no camera and IRA CARE does not use cameras in the home. Eating is what the person reports at a check-in (`food_intake: available`, "what the person reports, not a measurement") | D4.b (accepted) |
| Blood pressure | `not_available`: no validated sensor and no clearance to report it; IRA does not estimate it | D4.b, D4.c (blocked: a sensor to select and validate) |
| ECG | `not_available`, the same reason | D4.b, D4.c |
| SpO2 | `not_available`, the same reason | D4.b, D4.c |
| Heart rate variability | `not_available`: not validated on the current watch | D4.b |
| Sleep | `not_available`: IRA does not track sleep; a bedtime routine can be set as a reminder | D4.b |
| Skin temperature | `not_available`: the current watch does not send it | D4.b |

Nothing in the code estimates, infers or displays a value for any of these, and
`test_a_reading_ira_cannot_measure_is_refused` fails if a route ever accepts one. Adding a
measurement means a sensor, a validation and a clearance first (D4.c), then one entry in the list,
which the dashboard and the watch pick up.

## What the register did not hold before this round

Found by this audit and now in the register: step-up authentication for deployment
administrators (E2.p), the deployment administration screen (D3.l), the fall confirmation stage
(F4.i), the round 9 scenario families (F5.d) and the memory precondition of the 24-hour soak
(G2.j). Nothing else in the specification was found implemented but untracked, or tracked but
unimplemented; the items that remain are the ones the register already marks blocked (devices,
the AOSP host, datasets, reviewers, accounts, money).

# Integrated verification on `main` (register K)

Run on 25 November 2026, after PRs #50 to #61 were merged. Every number below comes from a run
whose output was read; nothing is estimated.

| Check | Where | Commit | Result |
|---|---|---|---|
| Backend unit suite (`pytest tests`, no database) | this container | `eed43f1` | 5117 passed, 548 skipped, **2 failed**: `test_public_url_allowed_by_egress_guard` (needs outbound DNS, which this container lacks) and `test_published_credentials` (needs the full git history; the checkout is shallow). Both pass in CI. |
| Backend database suites (`pytest tests/tenancy`, real PostgreSQL 16, migrations to 114) | this container | `eed43f1` | 2852 passed, 9 skipped, 0 failed (13 min) |
| Backend unit, database, lint; migration chain applied as `ira_migrator`; applied migrations unchanged; backup and restore round trip | CI on `main` ([run 45548964981](https://github.com/Praveenkumar101508/ira-care/actions/runs/45548964981)) | `eed43f1` | all passed |
| Dashboard: `tsc`, vitest, `next build` | this container | `eed43f1` | typecheck clean; 70 tests passed; production build succeeded |
| Phone app: `tsc`, jest | this container | `6f4a472` (with #61) | typecheck clean; 75 tests passed |
| Phone app dependencies against the Expo SDK (`npx expo install --check`) | this container | #61 head | "Dependencies are up to date" |
| Watch app: Wear OS and AOSP builds, JVM tests, no Google dependency in the aosp flavour | CI on #60, against `main` with #51 to #59 | #60 head | passed |
| Outage and recovery | database suite above | `eed43f1` | `test_care_outages_db.py` passed (REMEDIATION E2 to E5) |
| Secret scan (gitleaks) | CI on `main` | `eed43f1` | passed |

## Licences of shipped dependencies

Read from the installed packages and lockfiles, and for the 20 Python requirements not installed
here, from PyPI's metadata. No package was downloaded and run to check.

- **Dashboard, 53 production npm packages:** MIT, Apache-2.0, ISC, BSD, 0BSD and CC-BY-4.0, plus 14 LGPL-3.0-or-later packages. Those are all `@img/sharp-*` libvips prebuilt binaries, which Next.js loads dynamically for image optimisation. Nothing is under GPL, AGPL or SSPL, and none is unknown.
- **Phone app, 923 production npm packages:** mostly MIT and ISC, plus BSD, Apache-2.0, MPL-2.0, BlueOak and Unlicense. Two needed checking:
  - `node-forge` is dual-licensed BSD-3-Clause **or** GPL-2.0; the BSD option applies.
  - `@segment/loosely-validate-event` states no licence in its metadata, but its LICENSE file is MIT. It is an Expo CLI build-time dependency and is not shipped in the app.
- **Backend, 61 direct Python requirements:** MIT, Apache-2.0, BSD and the Unlicense, plus one LGPLv3 library. `caldav` is GPL-3.0-or-later **or** Apache-2.0; the Apache option applies.

Dataset licences are in `docs/DATASETS.md` (PPG-DaLiA: CC BY 4.0, cited).

## Not run

- **Load test.** `scripts/soak_24h.py` was not run; a 24-hour soak does not fit this session.
- **On-device and emulator runs.** No watch, phone, emulator or Cuttlefish image was available.
- **The IRA CARE OS image.** It is unbuilt, because the build host is an external blocker.

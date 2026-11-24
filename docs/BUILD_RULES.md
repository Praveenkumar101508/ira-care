# IRA CARE: build rules

These rules bind every change in this repository. They are checked in review and, where a
machine can check them, in CI.

## Workflow

1. One phase, one branch, one pull request. Branches are named for the work
   (`feat/<phase-name>`), never for a person or a tool.
2. Nothing is pushed directly to `main`. `main` is never force-pushed, rewritten or deleted.
3. A pull request merges only when every CI job is green. Merges are squash merges and the
   branch is deleted afterwards.
4. Before every push, the author reviews `git log origin/main..HEAD --format=%B` and
   `git diff origin/main --stat`, and checks the commit messages and every changed file for
   names that do not belong here (see "Attribution").
5. `docs/progress/STATUS.md` and `docs/progress/NEXT.md` are updated at the start of every
   phase, so a new session can continue from them alone.

## Attribution

Commits are authored as the repository owner:

```
git config user.name "Praveen Kamineti"
git config user.email "214716371+Praveenkumar101508@users.noreply.github.com"
```

No commit trailer, pull request text, branch name, file name, code, comment, document or
test fixture names any software assistant, language model or model vendor as the author
or the tool used. No commit trailer that credits a co-author or a generating tool.

## Tests

1. Every new behaviour has a test. Every new test has a negative control: the author
   breaks the code, sees the test fail, and restores it before committing.
2. Existing tests are never weakened, skipped or deleted to make a check pass. If a check
   cannot be made green after honest attempts, the unfinished part is turned off behind a
   setting (default off), the deferral is recorded in `docs/progress/STATUS.md`, and the
   checks go green on what remains.
3. Database tests run against a real PostgreSQL 16 with pgvector as the non-superuser
   application role. A test that connects as a superuser proves nothing about row-level
   security.
4. The full local suites run before every push.
5. Nothing is claimed to work unless a test or a run proved it. Failures are reported as
   failures.

## Data and secrets

1. No secrets, real phone numbers, real names or real health data anywhere in the
   repository. Test data is obviously fake.
   **Demonstration data** (the demo seed, the scenarios, the recording, the screenshots and
   the demo script) names only the two people who build IRA CARE, Praveen Kamineti (the
   care coordinator or carer) and Sravani Perni (family), as real people. The people being
   looked after are made up and keep one consistent name each (Ada Hartley, Bert Lowe).
   E-mail addresses are at example.com, telephone numbers are in the UK range reserved for
   fiction (07700 900000 to 07700 900999, written +447700900xxx), and addresses are in a
   town that does not exist (Lowbridge). `supracloud-jarvis/ira/tests/test_demo_data_rules.py`
   fails if the demonstration files break this.
2. New tables carry a tenant (and where appropriate a workspace) column, FORCE row-level
   security, a rollback file, an entry in the tenancy classification manifest and an
   erasure rule. The existing ratchet tests enforce each of these.
3. Applied migration files are immutable. Corrections are new numbered migrations. The
   migration hash record is updated with `python -m scripts.check_migration_integrity
   --update` in the same commit as the new file.

## Services and cost

1. No paid service, paid runner or paid plan is added. CI runs on Linux runners only.
2. Every outbound integration (SMS, calls, monitoring centre, push) sits behind an
   interface whose default implementation only logs. A real adapter stays off unless its
   configuration is present.
3. The system never dials emergency services itself.

## Product language

1. No diagnosis words and no medical advice anywhere in the product: alerts describe what
   was observed ("no answer and no movement for 3 minutes"), not what it means.
2. Safe ranges are set per person by a carer or clinician. There are no built-in medical
   thresholds.
3. Wording for the person is short, warm, one question at a time.

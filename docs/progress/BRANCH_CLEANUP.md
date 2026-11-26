# Branch clean-up (owner action)

Checked on 26 November 2026: every branch below belongs to a pull request that was merged
into `main`, and the branch's last commit is exactly the commit that pull request merged, so no
unmerged work is lost by deleting it. The one exception, `praveen/tender-carson-8yjrj4`, has no
pull request: it is the early working branch whose content was merged through #44, checked file
by file against `main` (the few lines that differ were changed later on purpose).

Deleting them from the build environment was refused by the platform (HTTP 403 on both
`git push --delete` and the GitHub API), so it needs one of the founders.

## Option A: in the browser (no tools needed)

1. Open <https://github.com/Praveenkumar101508/ira-care/branches>.
2. For each branch below, click the bin icon on its row. Keep `main`.

## Option B: from a terminal on your own computer

Run this in a clone of the repository (it only deletes the branches listed):

```bash
git push origin --delete \
  docs/endorsement-technical-inputs \
  docs/final-report-round7 \
  docs/final-verification \
  docs/register-round6 \
  docs/remaining-documents \
  docs/verification-round7 \
  fix/activity-integration \
  fix/ai-activity-wisdm \
  fix/ai-fall-v2 \
  fix/ai-fall-v3 \
  fix/ai-heart-learned \
  fix/ai-heart-ppg-dalia \
  fix/android-build-evidence \
  fix/care-authz-and-outages \
  fix/care-delivery-reliability \
  fix/care-journey-22 \
  fix/care-memory-promises \
  fix/care-plans-staff-roles \
  fix/care-safety-answers \
  fix/completion-framework \
  fix/completion-register \
  fix/compliance-traceability \
  fix/conversation-dataset \
  fix/conversation-finetune \
  fix/dashboard-csp \
  fix/dashboard-dark-mode \
  fix/dashboard-live-person \
  fix/dashboard-session-renewal \
  fix/dashboard-vitest-upgrade \
  fix/dataset-licence-register \
  fix/demo-pages-production-off \
  fix/demo-real-model \
  fix/demo-rerun \
  fix/deployment-admin-instructions \
  fix/factory-reset-two-person \
  fix/fall-model-research-only \
  fix/film-rerecord \
  fix/heart-research-ppg \
  fix/infra-adapters-backups \
  fix/licence-check \
  fix/log-exception-text \
  fix/mobile-expo-security \
  fix/mobile-talk-streaming \
  fix/mobile-voice \
  fix/models-in-product \
  fix/no-false-all-well \
  fix/os-product-layer \
  fix/os-testable-pieces \
  fix/phone-session-and-talk \
  fix/product-capabilities \
  fix/production-image-and-notices \
  fix/register-after-merges \
  fix/register-and-verification \
  fix/register-block-sync \
  fix/requirements-sync \
  fix/search-and-coordinator \
  fix/security-review \
  fix/security-review-round7 \
  fix/signing-key-procedure \
  fix/sisfall-out-of-all-builds \
  fix/speech-harness-evidence-index \
  fix/speech-no-pyav \
  fix/speech-wer-after-pyav \
  fix/staging-containers \
  fix/supply-chain-sbom \
  fix/verification-defects-round7 \
  fix/watch-degraded-storage \
  fix/watch-explicit-unpair \
  fix/watch-home-screen \
  fix/watch-reminders \
  fix/watch-speech-and-storage \
  praveen/tender-carson-8yjrj4
```

Afterwards only `main` should remain (plus any branch a newer pull request is using):

```bash
git ls-remote --heads origin
```

## The branches (72)

- `docs/endorsement-technical-inputs`
- `docs/final-report-round7`
- `docs/final-verification`
- `docs/register-round6`
- `docs/remaining-documents`
- `docs/verification-round7`
- `fix/activity-integration`
- `fix/ai-activity-wisdm`
- `fix/ai-fall-v2`
- `fix/ai-fall-v3`
- `fix/ai-heart-learned`
- `fix/ai-heart-ppg-dalia`
- `fix/android-build-evidence`
- `fix/care-authz-and-outages`
- `fix/care-delivery-reliability`
- `fix/care-journey-22`
- `fix/care-memory-promises`
- `fix/care-plans-staff-roles`
- `fix/care-safety-answers`
- `fix/completion-framework`
- `fix/completion-register`
- `fix/compliance-traceability`
- `fix/conversation-dataset`
- `fix/conversation-finetune`
- `fix/dashboard-csp`
- `fix/dashboard-dark-mode`
- `fix/dashboard-live-person`
- `fix/dashboard-session-renewal`
- `fix/dashboard-vitest-upgrade`
- `fix/dataset-licence-register`
- `fix/demo-pages-production-off`
- `fix/demo-real-model`
- `fix/demo-rerun`
- `fix/deployment-admin-instructions`
- `fix/factory-reset-two-person`
- `fix/fall-model-research-only`
- `fix/film-rerecord`
- `fix/heart-research-ppg`
- `fix/infra-adapters-backups`
- `fix/licence-check`
- `fix/log-exception-text`
- `fix/mobile-expo-security`
- `fix/mobile-talk-streaming`
- `fix/mobile-voice`
- `fix/models-in-product`
- `fix/no-false-all-well`
- `fix/os-product-layer`
- `fix/os-testable-pieces`
- `fix/phone-session-and-talk`
- `fix/product-capabilities`
- `fix/production-image-and-notices`
- `fix/register-after-merges`
- `fix/register-and-verification`
- `fix/register-block-sync`
- `fix/requirements-sync`
- `fix/search-and-coordinator`
- `fix/security-review`
- `fix/security-review-round7`
- `fix/signing-key-procedure`
- `fix/sisfall-out-of-all-builds`
- `fix/speech-harness-evidence-index`
- `fix/speech-no-pyav`
- `fix/speech-wer-after-pyav`
- `fix/staging-containers`
- `fix/supply-chain-sbom`
- `fix/verification-defects-round7`
- `fix/watch-degraded-storage`
- `fix/watch-explicit-unpair`
- `fix/watch-home-screen`
- `fix/watch-reminders`
- `fix/watch-speech-and-storage`
- `praveen/tender-carson-8yjrj4`

#!/usr/bin/env bash
# On a push to main: did this exact tree already pass the workflow as a pull request head?
#
#   GH_TOKEN=... scripts/ci/already_tested.sh      # prints already_tested=true|false
#
# A squash merge of an up-to-date pull request produces a commit whose tree is identical to
# the pull request's head. Running the suites again on an identical tree cannot say anything
# new, so the heavy jobs skip. Any doubt (no token, API error, no match, objects gone)
# answers false and the full run happens.
set -uo pipefail
tree=$(git rev-parse HEAD^{tree})
# GITHUB_WORKFLOW_REF is "owner/repo/.github/workflows/test.yml@refs/heads/main": drop the ref
# first, then take the file name. (The other order keeps the branch name and the API answers 404.)
repo="${GITHUB_REPOSITORY:-}"; wf="${GITHUB_WORKFLOW_REF%%@*}"; wf="${wf##*/}"
[ -n "$repo" ] && [ -n "${GH_TOKEN:-}" ] || { echo "already_tested=false"; exit 0; }
shas=$(curl -fsS -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$repo/actions/workflows/$wf/runs?event=pull_request&status=success&per_page=30" \
  | python3 -c "import json,sys; print('\n'.join(r['head_sha'] for r in json.load(sys.stdin).get('workflow_runs', [])))" 2>/dev/null) || shas=""
for sha in $shas; do
  git cat-file -e "$sha" 2>/dev/null || git fetch -q --depth 1 origin "$sha" 2>/dev/null || continue
  if [ "$(git rev-parse "$sha^{tree}" 2>/dev/null)" = "$tree" ]; then echo "already_tested=true"; echo "matched pull request head $sha" >&2; exit 0; fi
done
echo "already_tested=false"

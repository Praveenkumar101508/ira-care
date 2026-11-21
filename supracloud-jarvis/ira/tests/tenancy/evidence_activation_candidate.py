"""Evaluate the proposed activation patch in memory; never write/apply it.

The strict context check makes these tests fail when the proposal drifts. The
factory sees the actual proposed registration/middleware source, not a second
handwritten implementation of the delegation rule.
"""
import re
import subprocess
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
PATCH = ROOT / 'docs/f1/proposals/ACTIVATE_EVIDENCE.patch'

#: The commit the proposal is written against: every file the patch touches must
#: equal this commit's version or the patched result. It was ae5c019 until the
#: delegated-account fix (S2, docs/testing/evidence/delegated-account-boundary-
#: 2026-10-21/) changed `security/scope_enforcer.py` above the proposal's hunk.
#: The proposal's lines are unchanged; only its hunk moved from line 120 to 127.
#: Then b6ae902, until R08's Lock button (fdbf86a, docs/testing/evidence/r08-
#: durable-single-logout-2026-10-23/) added an import inside the page.tsx hunk's
#: context and four lines above its second hunk. The proposal's two added lines
#: are unchanged; its context now includes that import, and its hunks moved from
#: lines 35 and 262 to 36 and 267.
#: Then fdbf86a, until P0-03's two-step backup restore (63866fe, docs/testing/
#: evidence/destructive-action-approvals-2026-10-31/) changed `Sidebar.tsx`
#: below the proposal's hunk. The proposal is unchanged, line numbers included;
#: only the five files' baseline moved.
#: Then 63866fe, until the workspace-shift fix (9ca9553, docs/testing/evidence/
#: workspace-shift-2026-11-01/) made `Sidebar.tsx` scroll inside itself, at
#: line 251, far below the proposal's hunk at line 51. Again the proposal is
#: unchanged, line numbers included; only the baseline moved.
#: Then 9ca9553, until IRA NEXUS (f9136fb, docs/ui/nexus/) replaced
#: `Sidebar.tsx` with a tools registry and rewrote `page.tsx`. This time the
#: proposal itself had to move: its `Sidebar.tsx` hunk is now a `TOOLS` entry in
#: `components/shell/registry.ts` (the one list every module is reached from),
#: its `page.tsx` hunks sit at lines 32 and 287, and `store.ts` at 426. The two
#: backend hunks are unchanged, line numbers included.
BASELINE = 'f9136fbccff73304bec54e77af050538a626402a'


def projected_sources():
    lines = PATCH.read_text().splitlines(keepends=True)
    result = {}
    i = 0
    while i < len(lines):
        if not lines[i].startswith('--- a/'):
            i += 1
            continue
        name = lines[i][6:].strip()
        assert lines[i + 1] == '+++ b/' + name + '\n'
        baseline = subprocess.check_output(['git', 'show', BASELINE + ':' + name], cwd=ROOT).decode()
        original = baseline.splitlines(keepends=True)
        output, cursor = [], 0
        i += 2
        while i < len(lines) and not lines[i].startswith('--- a/'):
            match = re.match(r'@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@', lines[i])
            assert match, lines[i]
            start = int(match[1]) - 1
            output.extend(original[cursor:start]); cursor = start; i += 1
            while i < len(lines) and not lines[i].startswith(('@@ ', '--- a/')):
                line = lines[i]; i += 1
                if line[0] in ' -':
                    assert original[cursor] == line[1:], (name, cursor)
                    cursor += 1
                if line[0] in ' +':
                    output.append(line[1:])

        output.extend(original[cursor:])
        result[name] = ''.join(output)
        assert (ROOT / name).read_text() in (baseline, result[name]), name
    assert len(result) == 5
    return result


def module_from_proposal(name, source):
    module = types.ModuleType(name)
    exec(compile(source, '<unapplied activation proposal:' + name + '>', 'exec'), module.__dict__)
    return module

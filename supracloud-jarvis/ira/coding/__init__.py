"""
ira/coding — IRA Forge foundation (PR 69).

A library layer (no routes, no app wiring yet — that lands with the
IRA_FORGE_ENABLED flag) implementing the engineering-task substrate:

  schema.py             typed EngineeringTask + validation
  state_machine.py      INTAKE → … → POST_MERGE_OBSERVATION workflow
  capability_policy.py  code-enforced capability tokens for workers
  command_audit.py      append-only, hash-chained command audit log
  worktree_manager.py   isolated git-worktree lifecycle (never main/master)
  task_store.py         persistent task state across sessions

Constitution rules implemented here: B5 (capabilities in code), C1
(branch/worktree isolation), C4 (protected paths), C7 (no push/merge —
the worktree manager has no push capability at all).
"""

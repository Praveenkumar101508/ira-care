# Action Guard

One generic guard that can wrap **any** agent action: show the blast radius
before execution, require approval proportional to risk, block what should never
run, and leave tamper-evident proof afterwards.

This is an **extraction**, not a second safety implementation. Shadow World, the
Trust Ledger, Proof Cards, the protected-path denylist and the two-gate command
safety already existed and already worked together for email and calendar. What
was missing was a seam a new action type could be dropped into.

## The pipeline

```
guard_action(descriptor)
    adapter → ActionSpec (blast radius + DERIVED risk)
    → Shadow World simulation (side-effect free)
    → block | require approval | allow      (risk-derived, not caller-set)
    → on confirm: execute → Trust Ledger (intent, approval, result) → Proof Card
```

`risk → approval` is Shadow World's fixed table: `low → auto`,
`medium → plan_approval`, `high → owner_password`, `critical → blocked`.

## The generic descriptor

```python
ActionDescriptor(
    type="file_change",          # a Shadow World action kind
    target="/path/to/file",      # path, calendar summary, command line…
    requested_by="owner",
    params={"operation": "delete"},
    min_risk=None,               # a floor: raises risk, can never lower it
)
```

## Wired action types

| type              | derived risk                                                        | notes                                                                 |
| ----------------- | ------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `file_change`     | `delete` → high, `modify` → medium, protected path → **critical**    | high/critical take a `FileSnapshot` **before** approval, so the undo is *validated* at gate time |
| `calendar_change` | medium                                                              | reuses `actions/calendar_dav.py`                                       |
| `shell_command`   | blocked command or unsafe args → **critical**; allowlisted read-only → low; else high | graded by the same two gates `agents/executor.py` enforces            |

New types register an adapter — the guard itself doesn't change:

```python
register_adapter("db_operation", my_adapter)   # (spec, simulator, execute)
```

## API

```
POST /api/v1/guard/preview   {"type": "file_change", "target": "...", "params": {...}}
GET  /api/v1/guard/types
```

Preview returns `would_execute`, `risk`, `approval_required`, `blocked_reasons`,
`blast_radius`, the Proof Card and its compact summary — and `executed: false`,
because it simulates only. It never stages, never issues a token, and never
touches the target.

Preview is deliberately the **only** HTTP surface. Guarded execution stays on the
existing action-specific endpoints, which already collect approval the way each
action requires; this route adds no new privileged execution path. Library
callers use `trust.action_guard.guard_action` for the staged/confirmed pipeline.

## Guarantees (all covered by tests)

- A risky delete shows its blast radius, requires `owner_password`, and has a
  verified snapshot on disk *before* anything is destroyed.
- An approved action executes, writes ledger records (intent first, result
  after), and yields a Proof Card whose summary reports who approved it.
- Critical actions are **blocked outright** — no token is issued, and no approval
  can release them.
- A caller may raise the risk floor; it cannot lower it.
- The hash chain still verifies after guarded actions, and altering any record
  breaks verification.

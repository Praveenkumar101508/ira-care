"""FINDING CM — a startup warm-up may not own the event loop.

`app/lifespan.py` warms two model families at startup so the first real
request is not cold. Both are `sentence_transformers` loads: hundreds of
megabytes, a network fetch on a cold cache, and **synchronous**.

They are scheduled with `asyncio.create_task`, under a comment saying
"don't block startup". That is true and it is not the property that matters.
A task keeps work off the *startup path*; it does nothing about the *event
loop*. A synchronous call inside a task still owns the only thread that serves
requests, so for the duration of the load the process accepts connections and
answers none of them.

── What this cost, measured ────────────────────────────────────────────────

`/readyz` driven at concurrency 16 against a freshly started process, cold
model cache, **no CPU contention at all**:

    before   median 219 ms   p95 30 072 ms   96 of 200 requests UNANSWERED
    after    median  61 ms   p95    212 ms    0 of 200 unanswered

30 072 ms is the client timeout, not a latency. The loop was gone.

An instance in that state passes `/livez` and `/readyz` — both answered before
the load began — is placed into the load balancer, and then stalls every
request it is handed. The benchmark surfaced it only because it was the one
thing pointing concurrency at a freshly started process.

── Why this asserts on the SOURCE and not on a timing ──────────────────────

A timing test for this would need a real model load to be slow, which means a
network fetch and hundreds of megabytes in CI — and it would go green on a
fast machine with a warm cache, which is exactly the condition that hid the
defect for as long as it was hidden. A flaky assertion about milliseconds is
not a regression test for "somebody called a blocking function on the loop".

So the property is structural and exact: **inside `lifespan.py`, every call to
a `preload*` function is awaited through `asyncio.to_thread`.** That is
checkable without a GPU, a network or a stopwatch, it fails on the diff that
would reintroduce the defect, and it cannot pass because the machine was fast.
"""
from __future__ import annotations

import ast
import pathlib

IRA = pathlib.Path(__file__).resolve().parents[2]
LIFESPAN = IRA / "app" / "lifespan.py"

#: The blocking loaders. Named rather than pattern-matched: a list that a
#: reader can check against the imports in `lifespan.py` by eye is worth more
#: than a regex that silently stops matching when somebody renames one.
BLOCKING_LOADERS = ("preload_model", "preload_reranker", "prewarm")


def _tree() -> ast.Module:
    assert LIFESPAN.is_file(), f"{LIFESPAN} is missing"
    return ast.parse(LIFESPAN.read_text(encoding="utf-8"))


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _to_thread_arguments(tree: ast.Module) -> set[str]:
    """Every name passed to `asyncio.to_thread(...)` as the callable."""
    offloaded: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node) != "to_thread" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Name):
            offloaded.add(first.id)
        elif isinstance(first, ast.Attribute):
            offloaded.add(first.attr)
    return offloaded


def test_every_blocking_loader_is_offloaded_to_a_thread():
    """The regression itself: a loader called directly owns the loop."""
    tree = _tree()
    offloaded = _to_thread_arguments(tree)

    direct = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) in BLOCKING_LOADERS:
            direct.append((_call_name(node), node.lineno))

    offenders = [f"{name} called directly at lifespan.py:{line}"
                 for name, line in direct if name not in offloaded]
    assert not offenders, (
        "a synchronous model load is being called on the event loop:\n  "
        + "\n  ".join(offenders)
        + "\n\nWrap it: `await asyncio.to_thread(<loader>)`. `create_task` "
          "keeps it off the startup path and NOT off the loop, and for the "
          "duration of the load this process answers nothing — 96 of 200 "
          "requests went unanswered when it was last measured."
    )


def test_the_loaders_this_guards_are_actually_still_referenced():
    """A guard over names the file no longer mentions passes trivially.

    Written first as "still CALLED", and it failed the moment the fix landed —
    correctly, and for a reason worth keeping: once a loader is offloaded it is
    no longer a call at all, it is an ARGUMENT to `to_thread`. A coverage floor
    that only recognises the broken shape reports zero coverage exactly when
    the code is right.

    So it counts both shapes. If `lifespan.py` stops warming models entirely
    this fails, which is the correct outcome: the list above then describes a
    file it no longer matches, and somebody has to look rather than inherit a
    green test that checks nothing.
    """
    tree = _tree()
    called = {_call_name(n) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    referenced = called | _to_thread_arguments(tree)
    found = sorted(referenced & set(BLOCKING_LOADERS))
    assert found, (
        f"lifespan.py neither calls nor offloads any of {BLOCKING_LOADERS}, so "
        f"this file is asserting nothing. Re-derive the list from what it "
        f"warms now.")


def test_to_thread_is_awaited_rather_than_left_as_a_coroutine():
    """`asyncio.to_thread(f)` without `await` schedules nothing and warns.

    Cheap to get wrong while fixing the defect above, and it would restore the
    old behaviour in the worst way — the loader never runs, so the first real
    request pays the load instead of startup, and nothing says so.
    """
    tree = _tree()
    bare = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) \
                and _call_name(node.value) == "to_thread":
            bare.append(node.lineno)
    assert not bare, (
        f"asyncio.to_thread(...) used as a statement, not awaited, at "
        f"lifespan.py line(s) {bare}. The loader never runs.")

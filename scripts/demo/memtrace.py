"""Debug-only memory tracing for the demo API (never for production).

Set ``IRA_DEBUG_MEMTRACE=<file.jsonl>`` before starting ``scripts/demo/api.py``
and the process starts ``tracemalloc`` and appends one JSON line to that file
every ``IRA_DEBUG_MEMTRACE_INTERVAL`` seconds (default 300):

* RSS, tracemalloc's current and peak traced bytes;
* the top allocation sites (file:line) that grew since the baseline snapshot,
  and the top allocation sites by size now;
* the live object count per type from ``gc`` and its change since the baseline;
* glibc's heap figures (``mallinfo2``: bytes in use, bytes free but kept) and
  the anonymous resident memory from ``/proc/self/smaps_rollup``.

``IRA_DEBUG_MEMTRACE_TRACEMALLOC=0`` records the malloc and gc figures without
starting tracemalloc, so the process's memory is not changed by the tracing.
Each record is taken after a full ``gc.collect()`` unless
``IRA_DEBUG_MEMTRACE_COLLECT=0``; then the collector is left alone (a forced
collection every few minutes changes what is being measured), its generation
counts and statistics are recorded, and with
``IRA_DEBUG_MEMTRACE_PROBE_COLLECT_EVERY=k`` every k-th record also forces one
full collection afterwards and records how much it found and what it gave back.

The baseline is the snapshot taken ``IRA_DEBUG_MEMTRACE_BASELINE`` seconds
(default 120) after start. tracemalloc slows the process and costs memory, so
this is for profiling runs only: it refuses to start when the environment says
production (``IRA_ENV`` or ``ENVIRONMENT`` set to ``production``), and nothing
turns it on unless the variable is set. It lives with the demo scripts, not in
the server package.
"""
from __future__ import annotations

import asyncio
import collections
import ctypes
import functools
import gc
import json
import logging
import os
import threading
import time
import tracemalloc
from pathlib import Path

logger = logging.getLogger("ira.care.demo.memtrace")

_IGNORE = (tracemalloc.Filter(False, tracemalloc.__file__), tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
           tracemalloc.Filter(False, "<frozen importlib._bootstrap_external>"), tracemalloc.Filter(False, "<unknown>"))


def is_production(env: dict) -> bool:
    return any((env.get(k) or "").strip().lower() in ("production", "prod") for k in ("IRA_ENV", "ENVIRONMENT", "IRA_ENVIRONMENT"))


def wanted(env: dict) -> str | None:
    """The output path when tracing is asked for and allowed, else None."""
    path = (env.get("IRA_DEBUG_MEMTRACE") or "").strip()
    if not path:
        return None
    if is_production(env):
        raise RuntimeError("IRA_DEBUG_MEMTRACE is a debugging aid and must not be set in production")
    return path


def _rss() -> int | None:
    try:
        import psutil
        return psutil.Process().memory_info().rss
    except Exception:  # noqa: BLE001 - a debugging aid reports nothing rather than fail
        return None


class _Mallinfo2(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in ("arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks",
                                                "fsmblks", "uordblks", "fordblks", "keepcost")]


@functools.lru_cache(maxsize=1)
def _libc():
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.mallinfo2.restype = _Mallinfo2
        return libc
    except (OSError, AttributeError):
        return None


def malloc_stats() -> dict | None:
    """glibc's view of the C heap (all arenas): bytes in use, bytes free but
    kept, and bytes in direct mmaps. None off glibc."""
    libc = _libc()
    if libc is None:
        return None
    m = libc.mallinfo2()
    return {"arena": m.arena, "in_use": m.uordblks, "free_kept": m.fordblks, "mmap": m.hblkhd, "top_pad": m.keepcost}


def smaps_rollup() -> dict | None:
    """Anonymous and file-backed resident memory, from /proc (Linux only)."""
    try:
        out = {}
        for line in Path("/proc/self/smaps_rollup").read_text().splitlines()[1:]:
            k, v = line.split(":", 1)
            if k in ("Rss", "Anonymous", "Shared_Clean", "Private_Clean", "Private_Dirty"):
                out[k] = int(v.split()[0]) * 1024
        return out
    except OSError:
        return None


def pymalloc_stats() -> dict | None:
    """CPython's small-object allocator: arenas held, bytes in use and free in them.
    Read from sys._debugmallocstats(), which writes to the C stderr."""
    import re
    import sys
    import tempfile
    keys = {"# arenas allocated current": "arenas", "# arenas highwater mark": "arenas_highwater",
            "# bytes in allocated blocks": "in_use", "# bytes in available blocks": "free_in_pools"}
    try:
        saved = os.dup(2)
        with tempfile.TemporaryFile() as tmp:
            os.dup2(tmp.fileno(), 2)
            try:
                sys._debugmallocstats()
            finally:
                os.dup2(saved, 2)
                os.close(saved)
            tmp.seek(0)
            text = tmp.read().decode(errors="replace")
        out = {}
        for line in text.splitlines():
            for k, name in keys.items():
                if line.startswith(k):
                    out[name] = int(re.sub(r"[^0-9]", "", line.split("=", 1)[1]))
        return out
    except Exception:  # noqa: BLE001 - a debugging aid reports nothing rather than fail
        return None


def _type_counts() -> collections.Counter:
    return collections.Counter(type(o).__qualname__ for o in gc.get_objects())


def _origin(o) -> str | None:
    """Where a frame, coroutine or function in the garbage comes from."""
    code = getattr(o, "f_code", None) or getattr(o, "cr_code", None) or getattr(o, "gi_code", None) \
        or getattr(o, "__code__", None)
    if code is None:
        return None
    return f"{code.co_filename}:{code.co_firstlineno}:{code.co_qualname}"


def _collect_and_describe(top: int) -> tuple[int, list, list]:
    """A full collection that keeps what it finds in gc.garbage just long enough
    to count it by type and by the code it came from, then lets it go."""
    old = gc.get_debug()
    gc.set_debug(old | gc.DEBUG_SAVEALL)
    try:
        n = gc.collect()
        kinds = collections.Counter(type(o).__qualname__ for o in gc.garbage)
        origins = collections.Counter(filter(None, (_origin(o) for o in gc.garbage)))
    finally:
        gc.garbage.clear()
        gc.set_debug(old)
    gc.collect()
    return n, kinds.most_common(top), origins.most_common(top)


def _site(stat) -> str:
    f = stat.traceback[0]
    return f"{f.filename}:{f.lineno}"


class MemTrace:
    def __init__(self, path: str, *, interval: float, baseline_after: float, frames: int = 1, top: int = 40,
                 collect: bool = True, probe_collect_every: int = 0):
        self.collect, self.probe_collect_every, self.n = collect, probe_collect_every, 0
        self.path = Path(path)
        self.interval, self.baseline_after, self.top, self.frames = interval, baseline_after, top, frames
        self.t0 = time.time()
        self.base_snap = None
        self.base_types: collections.Counter = collections.Counter()

    def _write(self, rec: dict) -> None:
        with self.path.open("a") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")

    def take(self, label: str) -> None:
        self.n += 1
        if self.collect:
            gc.collect()
        types = _type_counts()
        rec = {"label": label, "t": round(time.time() - self.t0, 1), "wall": time.time(), "rss_bytes": _rss(),
               "malloc": malloc_stats(), "pymalloc": pymalloc_stats(), "smaps": smaps_rollup(), "threads": threading.active_count(),
               "gc_objects": sum(types.values()), "types_now": types.most_common(self.top), "collected_first": self.collect,
               "gc_count": gc.get_count(), "gc_stats": gc.get_stats(), "gc_gen2_objects": len(gc.get_objects(2))}
        if not self.collect and self.probe_collect_every and self.n % self.probe_collect_every == 0:
            # How much was garbage waiting for a full collection, and whether freeing it gives memory back.
            unreachable, kinds, origins = _collect_and_describe(self.top)
            rec["forced_collect"] = {"unreachable": unreachable, "rss_after": _rss(), "malloc_after": malloc_stats(),
                                     "pymalloc_after": pymalloc_stats(), "gc_objects_after": len(gc.get_objects()),
                                     "garbage_types": kinds, "garbage_origins": origins}
        if not tracemalloc.is_tracing():   # malloc and gc figures only: no tracemalloc overhead
            if not self.base_types:
                self.base_types = types
                rec["baseline"] = True
            else:
                grown = types.copy()
                grown.subtract(self.base_types)
                rec["types_growth"] = [(k, v) for k, v in grown.most_common(self.top) if v > 0]
            self._write(rec)
            return
        snap = tracemalloc.take_snapshot().filter_traces(_IGNORE)
        cur, peak = tracemalloc.get_traced_memory()
        rec.update({"traced_current": cur, "traced_peak": peak,
                    "top_now": [{"site": _site(s), "size": s.size, "count": s.count}
                                for s in snap.statistics("lineno")[: self.top]]})
        if self.base_snap is None:
            self.base_snap, self.base_types = snap, types
            rec["baseline"] = True
        else:
            diff = snap.compare_to(self.base_snap, "lineno")
            diff.sort(key=lambda d: d.size_diff, reverse=True)
            rec["top_growth"] = [{"site": _site(d), "size_diff": d.size_diff, "count_diff": d.count_diff, "size": d.size}
                                 for d in diff[: self.top]]
            grown = types.copy()
            grown.subtract(self.base_types)
            rec["types_growth"] = [(k, v) for k, v in grown.most_common(self.top) if v > 0]
            if self.frames > 1:
                tb = snap.compare_to(self.base_snap, "traceback")
                tb.sort(key=lambda d: d.size_diff, reverse=True)
                rec["top_growth_tracebacks"] = [{"size_diff": d.size_diff, "count_diff": d.count_diff,
                                                 "frames": [f"{f.filename}:{f.lineno}" for f in d.traceback]}
                                                for d in tb[:10]]
        self._write(rec)

    async def run(self) -> None:
        await asyncio.sleep(self.baseline_after)
        self.take("baseline")
        while True:
            await asyncio.sleep(self.interval)
            try:
                self.take("interval")
            except Exception:   # a debugging aid must never take the API down
                logger.exception("memtrace snapshot failed")


def start_if_asked(env: dict | None = None) -> asyncio.Task | None:
    """Call from inside the running event loop. Returns the task, or None when not asked."""
    env = dict(os.environ) if env is None else env
    path = wanted(env)
    if not path:
        return None
    frames = int(env.get("IRA_DEBUG_MEMTRACE_FRAMES") or "1")
    if (env.get("IRA_DEBUG_MEMTRACE_TRACEMALLOC") or "1") != "0":
        tracemalloc.start(max(1, frames))
    mt = MemTrace(path, interval=float(env.get("IRA_DEBUG_MEMTRACE_INTERVAL") or "300"),
                  baseline_after=float(env.get("IRA_DEBUG_MEMTRACE_BASELINE") or "120"), frames=frames,
                  collect=(env.get("IRA_DEBUG_MEMTRACE_COLLECT") or "1") != "0",
                  probe_collect_every=int(env.get("IRA_DEBUG_MEMTRACE_PROBE_COLLECT_EVERY") or "0"))
    logger.warning("memtrace on (debug only): writing to %s", path)
    return asyncio.get_running_loop().create_task(mt.run())

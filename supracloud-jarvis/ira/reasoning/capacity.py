"""
ira/reasoning/capacity.py — "what inference tier can this machine carry right now?"

Step 1 of the adaptive-inference design
(supracloud-jarvis/docs/research/gpu-free-inference-2026-11-12/D-adaptive-inference-architecture.md §6):
a runtime capacity record that the Trust Console can show, so the owner sees
WHY a larger model is or is not on offer. It changes no routing and loads no
model unless the owner opts in to the speed probe.

The record answers four things, each fail-soft:

  * memory: total and AVAILABLE RAM now (`/proc/meminfo` MemAvailable on Linux,
    `sysctl` on macOS, GlobalMemoryStatusEx on Windows; None when unreadable);
  * compute: CPU threads and the accelerator kind (nvidia / apple-silicon / cpu),
    the same detection as scripts/detect_hardware.py;
  * speed: an optional generation probe on one small installed model through
    Ollama's native /api/generate, measured from the server's own eval timings.
    It is OFF unless IRA_CAPACITY_PROBE=1, because it loads a model, and its
    result is cached for PROBE_TTL_S so a status page does not re-run it;
  * admission: for each tier (fast / balanced / advanced) whether the memory and
    speed rules pass, with the measured reference figure each rule used.

Tier figures are MEASUREMENTS, not guesses: peak resident memory of llama-server
after five prompts at a 4 096-token context, and generation speed relative to the
4B reference on the same VM, from the 12 November 2026 Kaggle runs
(C-benchmark-report.md §3–§4). The speed floors are the design proposal's and
are marked as such in the record.

Design rules, shared with model_availability.py and model_traits.py:
  * standard library only; nothing heavy is imported;
  * every probe is bounded by a timeout and swallows its failure into None;
  * the Ollama URL scheme is checked before any request is made (FINDING AK);
  * nothing here ever selects an external model or opens a non-local connection
    other than to the configured Ollama host.
"""
from __future__ import annotations

import ctypes
import http.client
import json
import logging
import os
import platform
import shutil
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger("ira.reasoning.capacity")

GIB = 1024 ** 3

#: Opt-in switch for the generation probe (it loads a model).
PROBE_ENV = "IRA_CAPACITY_PROBE"
#: Which installed model the probe uses; default is the smallest shipped chat model.
PROBE_MODEL_ENV = "IRA_CAPACITY_PROBE_MODEL"
DEFAULT_PROBE_MODEL = "qwen3:4b"
#: Seconds the probe may take, end to end, before it is abandoned.
PROBE_TIMEOUT_S = 20.0
#: Seconds a probe result (or failure) is reused before the probe runs again.
PROBE_TTL_S = 600.0
#: Tokens the probe asks for; enough for a stable rate, short enough to be cheap.
PROBE_TOKENS = 32

#: Share of AVAILABLE memory a tier's resident footprint may take. The rest is
#: for PostgreSQL, the embedder, the web UI and the page cache (D §3).
MEMORY_SHARE = 0.70

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_OLLAMA_HOST = "http://localhost:11434"


@dataclass(frozen=True)
class Tier:
    """One inference tier and the measured figures its admission rules use."""

    name: str
    reference_model: str
    #: Peak resident memory (GiB) of llama-server for the reference model,
    #: 4 096-token context, after five prompts; measured, see C report §4.
    resident_gib: float
    #: Generation speed of the reference model divided by the 4B reference on
    #: the same VM (C report §3 and §1); 1.0 for the fast tier itself.
    speed_ratio_to_fast: float
    #: Proposed floor in tokens/s for the tier to count as usable (D §3, a
    #: proposal to be set from measurements; recorded as such).
    speed_floor_tps: float
    #: Where the figures come from.
    evidence: str


TIERS: tuple[Tier, ...] = (
    Tier("fast", "Qwen3-4B-Instruct-2507 Q4_K_M", 4.7, 1.0, 8.0,
         "evidence/kaggle-runs/ref-qwen3-4b-q4km-v2 and ab-same-vm-v1"),
    Tier("balanced", "Qwen3-8B Q4_K_M", 8.6, 0.59, 4.0,
         "evidence/kaggle-runs/dense-8b-14b-v2 (8B 3.8 tok/s vs 4B 6.4 tok/s, Xeon type)"),
    Tier("advanced", "Qwen3-30B-A3B-Instruct-2507 Q4_K_M", 27.4, 1.12, 2.0,
         "evidence/kaggle-runs/moe-30b-a3b-gptoss20b-v2 and ab-same-vm-v1 (7.18 vs 6.4 tok/s)"),
)


# ── hardware ────────────────────────────────────────────────────────────────

def _meminfo_linux() -> tuple[Optional[int], Optional[int]]:
    total = available = None
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None, None
    return total, available


def _meminfo_darwin() -> tuple[Optional[int], Optional[int]]:
    try:
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                                   timeout=10, check=True).stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, None
    # vm_stat reports pages; free + inactive + speculative is the usual
    # "available" approximation on macOS. Unreadable -> None, never a guess.
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10, check=True).stdout
        page = 4096
        first = out.splitlines()[0]
        if "page size of" in first:
            page = int(first.split("page size of")[1].split()[0])
        pages = {}
        for line in out.splitlines()[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                pages[k.strip()] = int(v.strip().rstrip("."))
        avail = (pages.get("Pages free", 0) + pages.get("Pages inactive", 0)
                 + pages.get("Pages speculative", 0)) * page
        return total, avail
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return total, None


def _meminfo_windows() -> tuple[Optional[int], Optional[int]]:
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
            return int(stat.ullTotalPhys), int(stat.ullAvailPhys)
    except (AttributeError, OSError, ValueError):
        pass
    return None, None


def memory_bytes() -> tuple[Optional[int], Optional[int]]:
    """(total, available) in bytes, each None when it cannot be read."""
    system = platform.system()
    if system == "Linux":
        return _meminfo_linux()
    if system == "Darwin":
        return _meminfo_darwin()
    if system == "Windows":
        return _meminfo_windows()
    return None, None


def nvidia_vram_bytes() -> Optional[int]:
    """Largest NVIDIA GPU's memory, or None without a working driver."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=15, check=True).stdout
        mib = [int(v) for v in out.split() if v.strip().isdigit()]
        return max(mib) * 1024 * 1024 if mib else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def accelerator(vram: Optional[int]) -> str:
    if vram:
        return "nvidia"
    if platform.system() == "Darwin" and platform.machine().lower() in ("arm64", "aarch64"):
        return "apple-silicon"
    return "cpu"


# ── the generation probe ────────────────────────────────────────────────────

#: A function (url, json_body, timeout) -> decoded JSON dict. Injectable for tests.
Poster = Callable[[str, dict, float], dict]


def _ollama_host(env: Optional[Mapping[str, str]] = None) -> str:
    env_map: Mapping[str, str] = os.environ if env is None else env
    raw = (env_map.get("IRA_OLLAMA_BASE_URL") or env_map.get("OLLAMA_BASE_URL") or _DEFAULT_OLLAMA_HOST).strip()
    raw = raw.rstrip("/")
    if raw.endswith("/v1"):
        raw = raw[: -len("/v1")]
    return raw or _DEFAULT_OLLAMA_HOST


def _default_post(url: str, body: dict, timeout: float) -> dict:
    """POST JSON to the Ollama host and decode the JSON reply, standard library only.

    The scheme is checked first and only http/https are opened (FINDING AK). The
    request goes through `http.client` with an explicit host and port taken from
    the checked URL, so no URL-opener that could follow a `file:` or `ftp:` URL
    is involved; a non-2xx status is an error, not a reply.
    """
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"refusing to post to {scheme or '<none>'}: URL; the probe speaks http/https only")
    if not parts.hostname:
        raise ValueError("the Ollama URL names no host")
    conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parts.hostname, parts.port, timeout=timeout)
    try:
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.request("POST", path, body=json.dumps(body).encode(),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        if not 200 <= resp.status < 300:
            raise RuntimeError(f"Ollama answered HTTP {resp.status} to the capacity probe")
        return json.loads(raw.decode("utf-8"))
    finally:
        conn.close()


def tokens_per_second(generate_response: dict) -> Optional[float]:
    """Generation tokens/s from Ollama's own `eval_count` / `eval_duration` (ns).

    None when the response lacks either, or the duration is zero: the caller
    must not mistake "could not measure" for a speed.
    """
    try:
        count = int(generate_response.get("eval_count") or 0)
        duration_ns = int(generate_response.get("eval_duration") or 0)
    except (TypeError, ValueError, AttributeError):
        return None
    if count <= 0 or duration_ns <= 0:
        return None
    return round(count / (duration_ns / 1_000_000_000), 2)


def probe_generation(model: str, host: str, *, post: Poster = _default_post,
                     timeout: float = PROBE_TIMEOUT_S) -> dict:
    """One short, deterministic generation on `model`; returns a small record.

    Never raises: a failure is recorded as `tokens_per_second: None` with an
    `error` string, so the console says "unknown" rather than breaking.
    """
    body = {"model": model, "prompt": "Count from one to twenty, separated by commas.",
            "stream": False, "options": {"num_predict": PROBE_TOKENS, "temperature": 0}}
    started = time.perf_counter()
    try:
        data = post(f"{host.rstrip('/')}/api/generate", body, timeout)
    except Exception as exc:  # noqa: BLE001 — fail-soft by design
        logger.warning("capacity probe on %s failed (%s)", model, str(exc)[:120])
        return {"model": model, "tokens_per_second": None, "error": str(exc)[:200],
                "wall_s": round(time.perf_counter() - started, 2)}
    tps = tokens_per_second(data)
    return {"model": model, "tokens_per_second": tps,
            "eval_count": data.get("eval_count"), "eval_duration_ns": data.get("eval_duration"),
            "load_duration_ns": data.get("load_duration"),
            "error": None if tps is not None else "response carried no usable eval timings",
            "wall_s": round(time.perf_counter() - started, 2)}


#: (expires_at, record) of the last probe; a failure is cached too.
_PROBE_CACHE: dict[str, tuple[float, dict]] = {}


def cached_probe(model: str, host: str, *, post: Poster = _default_post,
                 now: Optional[float] = None) -> dict:
    now = time.monotonic() if now is None else now
    hit = _PROBE_CACHE.get(f"{host}|{model}")
    if hit and hit[0] > now:
        return dict(hit[1], cached=True)
    rec = probe_generation(model, host, post=post)
    _PROBE_CACHE[f"{host}|{model}"] = (now + PROBE_TTL_S, rec)
    return dict(rec, cached=False)


def reset_probe_cache() -> None:
    _PROBE_CACHE.clear()


# ── admission ───────────────────────────────────────────────────────────────

def admit(tier: Tier, available_bytes: Optional[int], fast_tps: Optional[float]) -> dict:
    """Apply the two rules of D §3 to one tier and say which figures were used.

    memory: resident_gib <= MEMORY_SHARE * available.
    speed:  fast_tps * speed_ratio_to_fast >= speed_floor_tps, when a probe
            result exists; otherwise "unknown" and admission is by memory alone,
            flagged so the console can say so.
    """
    out: dict[str, Any] = {"tier": tier.name, "reference_model": tier.reference_model,
                           "resident_gib": tier.resident_gib, "evidence": tier.evidence}
    if available_bytes is None:
        out["memory"] = {"ok": None, "reason": "available memory could not be read"}
    else:
        budget = round(available_bytes / GIB * MEMORY_SHARE, 2)
        ok = tier.resident_gib <= budget
        out["memory"] = {"ok": ok, "budget_gib": budget,
                         "reason": (f"{tier.resident_gib} GiB resident within {MEMORY_SHARE:.0%} of "
                                    f"{available_bytes / GIB:.1f} GiB available" if ok else
                                    f"needs {tier.resident_gib} GiB resident; {MEMORY_SHARE:.0%} of "
                                    f"{available_bytes / GIB:.1f} GiB available is {budget} GiB")}
    if fast_tps is None:
        out["speed"] = {"ok": None, "floor_tps": tier.speed_floor_tps,
                        "reason": "no generation probe result (IRA_CAPACITY_PROBE is off or the probe failed)"}
    else:
        projected = round(fast_tps * tier.speed_ratio_to_fast, 2)
        ok = projected >= tier.speed_floor_tps
        out["speed"] = {"ok": ok, "projected_tps": projected, "floor_tps": tier.speed_floor_tps,
                        "reason": (f"projected {projected} tok/s ({fast_tps} x {tier.speed_ratio_to_fast}) "
                                   f"{'meets' if ok else 'is below'} the proposed floor of "
                                   f"{tier.speed_floor_tps} tok/s")}
    mem_ok, spd_ok = out["memory"]["ok"], out["speed"]["ok"]
    out["admitted"] = bool(mem_ok) and (spd_ok is True or spd_ok is None)
    out["admission_basis"] = ("memory and speed" if spd_ok is not None else
                              "memory only; speed unknown") if mem_ok else "refused on memory"
    return out


def capacity_record(env: Optional[Mapping[str, str]] = None, *, post: Poster = _default_post,
                    memory: Callable[[], tuple[Optional[int], Optional[int]]] = memory_bytes,
                    vram: Callable[[], Optional[int]] = nvidia_vram_bytes) -> dict:
    """The whole record, JSON-ready. Never raises."""
    env_map: Mapping[str, str] = os.environ if env is None else env
    total, available = memory()
    vram_bytes = vram()
    probe_on = str(env_map.get(PROBE_ENV, "")).strip().lower() in ("1", "true", "yes", "on")
    probe: Optional[dict] = None
    fast_tps: Optional[float] = None
    if probe_on:
        model = (env_map.get(PROBE_MODEL_ENV) or DEFAULT_PROBE_MODEL).strip()
        probe = cached_probe(model, _ollama_host(env_map), post=post)
        fast_tps = probe.get("tokens_per_second")
    tiers = [admit(t, available, fast_tps) for t in TIERS]
    admitted = [t["tier"] for t in tiers if t["admitted"]]
    return {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ram_total_gib": round(total / GIB, 1) if total else None,
        "ram_available_gib": round(available / GIB, 1) if available else None,
        "cpu_threads": os.cpu_count(),
        "accelerator": accelerator(vram_bytes),
        "nvidia_vram_gib": round(vram_bytes / GIB, 1) if vram_bytes else None,
        "probe_enabled": probe_on,
        "probe": probe,
        "memory_share": MEMORY_SHARE,
        "tiers": tiers,
        "highest_admitted_tier": admitted[-1] if admitted else None,
        "note": ("Tier footprints and speed ratios are measured (Kaggle, 12 Nov 2026); speed floors "
                 "are the design proposal's. This record informs; it changes no routing."),
    }

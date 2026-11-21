#!/usr/bin/env python3
"""Detect this machine's memory and GPU, and name a local model that fits.

Used by the native setup scripts on Linux, macOS and Windows so a fresh install
gets a chat model the machine can actually load. Without it, every install
defaulted to `qwen3:14b` (9.28 GB of weights): it cannot load on an 8 GB laptop,
leaves no headroom on 16 GB, and a CPU-only cloud server spends minutes per
answer on it.

Standard library only, Python 3.8+, because it runs BEFORE the project's
virtualenv exists (macOS's system python3 is 3.9).

    python3 scripts/detect_hardware.py            # human-readable report
    python3 scripts/detect_hardware.py --json     # the same, as JSON
    python3 scripts/detect_hardware.py --model    # just the recommended tag
    python3 scripts/detect_hardware.py --check qwen3:14b   # exit 2 if too big

The tier table is a judgement, not a benchmark: weights are measured from the
Ollama registry manifests, the memory floors leave room for the OS, the IRA
backend (torch + the 1024-dim BGE embedder), PostgreSQL, Redis and the web UI.
Override any choice in `.env` — this only picks the default.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys

GIB = 1024 ** 3

#: Model weight sizes in GB, read from registry.ollama.ai manifests
#: (`application/vnd.ollama.image.model` layer) on 2026-10-18.
MODEL_WEIGHTS_GB = {
    "qwen3:0.6b": 0.52,
    "qwen3:1.7b": 1.36,
    "qwen3:4b": 2.50,
    "qwen3:8b": 5.23,
    "qwen3:14b": 9.28,
}

#: (minimum system memory in GiB, model). First match wins, largest first.
#: CPU-only and Apple Silicon (unified memory) both run the model from this
#: pool, so system memory is the constraint.
RAM_TIERS = (
    (30, "qwen3:14b"),
    (15, "qwen3:8b"),
    (11, "qwen3:4b"),
    (7, "qwen3:1.7b"),
    (0, "qwen3:0.6b"),
)

#: (minimum dedicated VRAM in GiB, model) for an NVIDIA GPU. The model runs
#: from VRAM there, so a small-RAM box with a big card can still take 14b.
VRAM_TIERS = (
    (11, "qwen3:14b"),
    (7, "qwen3:8b"),
    (4, "qwen3:4b"),
)

#: Below this, IRA's own services leave too little for any useful model.
MIN_RECOMMENDED_RAM_GIB = 8

#: Without a GPU, speed rather than memory is the limit. Measured on a 4-vCPU
#: CPU-only Linux server (Xeon @ 2.1 GHz, AVX-512), one model loaded at a time,
#: 2026-10-18: qwen3:1.7b 10.0, qwen3:4b 6.3, qwen3:8b 3.3 tokens/s generated.
#: Comfortable reading speed is roughly 5 tokens/s, so a CPU-only machine is
#: capped here however much RAM it has. Bigger CPUs can raise it in .env.
CPU_ONLY_MAX = "qwen3:4b"


def _total_ram_bytes() -> int | None:
    system = platform.system()
    try:
        if system == "Linux":
            with open("/proc/meminfo", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) * 1024
        elif system == "Darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                 text=True, timeout=10, check=True).stdout
            return int(out.strip())
        elif system == "Windows":
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
                return int(stat.ullTotalPhys)
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return None
    return None


def _nvidia_vram_bytes() -> int | None:
    """Largest single NVIDIA GPU's memory, or None without a working driver."""
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


def _pick(tiers, gib: float) -> str | None:
    for floor, model in tiers:
        if gib >= floor:
            return model
    return None


def recommend(ram_bytes: int | None, vram_bytes: int | None, *,
              accelerated: bool) -> tuple[str, list[str]]:
    """The model to default to, and the reasons.

    `accelerated` is True when the model runs on a GPU: an NVIDIA card, or
    Apple Silicon's Metal over unified memory.
    """
    notes: list[str] = []
    if ram_bytes is None:
        notes.append("system memory could not be read; assuming 8 GiB")
        ram_bytes = 8 * GIB
    ram_gib = ram_bytes / GIB
    model = _pick(RAM_TIERS, ram_gib) or "qwen3:0.6b"
    notes.append(f"{ram_gib:.1f} GiB system memory -> {model}")
    if vram_bytes:
        vram_model = _pick(VRAM_TIERS, vram_bytes / GIB)
        if vram_model and MODEL_WEIGHTS_GB[vram_model] > MODEL_WEIGHTS_GB[model]:
            notes.append(f"{vram_bytes / GIB:.1f} GiB NVIDIA VRAM -> {vram_model}")
            model = vram_model
    if not accelerated and MODEL_WEIGHTS_GB[model] > MODEL_WEIGHTS_GB[CPU_ONLY_MAX]:
        notes.append(f"no GPU detected -> capped at {CPU_ONLY_MAX} for answer speed")
        model = CPU_ONLY_MAX
    if ram_gib < MIN_RECOMMENDED_RAM_GIB:
        notes.append(f"below the recommended {MIN_RECOMMENDED_RAM_GIB} GiB: expect slow, "
                     f"low-quality answers")
    return model, notes


def detect() -> dict:
    system = platform.system()
    machine = platform.machine().lower()
    ram = _total_ram_bytes()
    vram = _nvidia_vram_bytes()
    apple_silicon = system == "Darwin" and machine in ("arm64", "aarch64")
    if vram:
        accelerator = "nvidia"
    elif apple_silicon:
        accelerator = "apple-silicon"   # Ollama uses Metal over unified memory
    else:
        accelerator = "cpu"
    model, notes = recommend(ram, vram, accelerated=accelerator != "cpu")
    return {
        "os": system, "arch": machine, "cpus": os.cpu_count(),
        "ram_gib": round(ram / GIB, 1) if ram else None,
        "nvidia_vram_gib": round(vram / GIB, 1) if vram else None,
        "accelerator": accelerator,
        "recommended_model": model,
        "recommended_weights_gb": MODEL_WEIGHTS_GB[model],
        "notes": notes,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="print the report as JSON")
    mode.add_argument("--model", action="store_true", help="print only the recommended model")
    mode.add_argument("--check", metavar="MODEL",
                      help="exit 2 if MODEL is larger than this machine's recommendation")
    args = ap.parse_args(argv)

    report = detect()
    if args.json:
        print(json.dumps(report, indent=2))
    elif args.model:
        print(report["recommended_model"])
    elif args.check:
        want = MODEL_WEIGHTS_GB.get(args.check)
        if want is None:
            return 0    # an unknown tag is the owner's choice; nothing to compare
        if want > report["recommended_weights_gb"]:
            print(f"{args.check} ({want:.1f} GB) is larger than this machine's recommended "
                  f"{report['recommended_model']}: {'; '.join(report['notes'])}. Expect slow "
                  f"answers, or a model that fails to load.")
            return 2
    else:
        for key in ("os", "arch", "cpus", "ram_gib", "nvidia_vram_gib", "accelerator",
                    "recommended_model"):
            print(f"{key:>18}: {report[key]}")
        for note in report["notes"]:
            print(f"{'':>18}  - {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

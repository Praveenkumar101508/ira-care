# IRA CARE: the GPU profile, proved on Kaggle's free GPU (D-074).
#
# Runs as a private Kaggle script kernel (pushed by scripts/training/run_kaggle.py gpu_eval,
# GPU and internet on, no token anywhere). The backend's own code arrives as a private Kaggle
# dataset made by run_kaggle.py from this repository (supracloud-jarvis/ira without its tests,
# plus the evaluation set), so the numbers come from the same code the server runs.
#
# It installs Ollama (the same version as the deployment), pulls the GPU profile's two models
# and gemma4:e2b, all kept loaded, and plays the 162-turn evaluation set through
# care.conversation for each model alone and for the two candidate chains (gemma4:12b then
# gemma4:e4b; gemma4:e4b then gemma4:e2b), REPEATS times each, because one run of 15 check-in
# answers is too few to choose on. It prints the reports as JSON in parts the repository
# reads back:
#
#   EVAL_PART i/n <json text>     joined in order, one JSON object {label: report}
#   METRICS {json}                a summary: quality, red-flag recall, timings, GPU, seconds
import glob
import json
import os
import subprocess
import sys
import tarfile
import time
import urllib.request

T0 = time.time()
PRIMARY, SECONDARY = "gemma4:12b", "gemma4:e4b"
MODELS = ["gemma4:12b", "gemma4:e4b", "gemma4:e2b"]
REPEATS = 2
CHUNK = 30000


def log(m):
    print(m, flush=True)


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False, **kw)


def post(path, body, timeout=1800):
    req = urllib.request.Request("http://127.0.0.1:11434" + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def code_dir():
    for found in glob.glob("/kaggle/input/**/scripts/eval_care_model.py", recursive=True):
        return os.path.dirname(os.path.dirname(found))
    for tgz in glob.glob("/kaggle/input/**/ira-code.tar.gz", recursive=True):
        with tarfile.open(tgz) as t:
            t.extractall("/kaggle/working/code", filter="data")
        return "/kaggle/working/code/ira"
    raise SystemExit("the ira-care-code dataset is not attached")


log("installing ollama")
sh("apt-get install -y -q zstd >/dev/null 2>&1 || (apt-get update -q >/dev/null 2>&1 && apt-get install -y -q zstd >/dev/null 2>&1)")
sh("curl -fsSL https://ollama.com/install.sh | OLLAMA_VERSION=0.40.18 sh")
env = dict(os.environ, OLLAMA_KEEP_ALIVE="60m", OLLAMA_MAX_LOADED_MODELS="3", OLLAMA_NUM_PARALLEL="1")
server = subprocess.Popen(["ollama", "serve"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    if sh("ollama list").returncode == 0:
        break
    time.sleep(1)
version = sh("ollama --version").stdout.strip()
gpu = sh("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader").stdout.strip()
log(f"{version}; gpu {gpu}")
for m in MODELS:
    t = time.time()
    r = sh(f"ollama pull {m}")
    log(f"pulled {m} in {time.time() - t:.0f}s rc={r.returncode}")

ira = code_dir()
log(f"code at {ira}")
base = dict(os.environ, IRA_CARE_OLLAMA_URL="http://127.0.0.1:11434", IRA_CARE_MODEL_KEEP_ALIVE="60m",
            IRA_CARE_MODEL_NUM_CTX="1024", IRA_CARE_MODEL_PRIMARY=PRIMARY, IRA_CARE_MODEL_SECONDARY=SECONDARY)
# Load both with the turn's own context size, as the server's warm job does (D-073).
for m in MODELS:
    t = time.time()
    post("/api/generate", {"model": m, "keep_alive": "60m", "options": {"num_ctx": 1024}})
    log(f"loaded {m} in {time.time() - t:.1f}s")
log("loaded: " + sh("ollama ps").stdout.replace("\n", " | "))

reports = {}
RUNS = [("gemma4-12b", "ollama:gemma4:12b", {}), ("gemma4-e4b", "ollama:gemma4:e4b", {}), ("gemma4-e2b", "ollama:gemma4:e2b", {}),
        ("chain-12b-e4b", "chain", {"IRA_CARE_MODEL_PRIMARY": "gemma4:12b", "IRA_CARE_MODEL_SECONDARY": "gemma4:e4b"}),
        ("chain-e4b-e2b", "chain", {"IRA_CARE_MODEL_PRIMARY": "gemma4:e4b", "IRA_CARE_MODEL_SECONDARY": "gemma4:e2b"})]
for rep in range(1, REPEATS + 1):
    for name, model, extra in RUNS:
        label = f"{name}-run{rep}"
        out = f"/kaggle/working/{label}.json"
        t = time.time()
        r = sh(f"{sys.executable} -m scripts.eval_care_model --model {model} --quiet --label 'gpu profile, {label}, Kaggle T4' --out {out}",
               cwd=ira, env=base | extra, timeout=5400)
        log(f"{label}: rc={r.returncode} in {time.time() - t:.0f}s {r.stderr[-400:] if r.returncode else ''}")
        if os.path.exists(out):
            with open(out) as f:
                reports[label] = json.load(f)

blob = json.dumps(reports, separators=(",", ":"))
parts = [blob[i:i + CHUNK] for i in range(0, len(blob), CHUNK)]
for i, p in enumerate(parts):
    print(f"EVAL_PART {i}/{len(parts)} {p}", flush=True)
summary = {label: {k: r.get(k) for k in ("quality", "red_flag_recall", "advice_refusal_rate", "injection_resistance",
                                           "first_words_s", "first_sentence_s", "model_fallbacks", "wall_time_s")}
           | {"checkin_answer": r.get("categories", {}).get("checkin_answer")}
           for label, r in reports.items()}
print("METRICS " + json.dumps({"gpu": gpu, "ollama": version, "primary": PRIMARY, "secondary": SECONDARY,
                               "reports": summary, "seconds": round(time.time() - T0)}), flush=True)
server.terminate()

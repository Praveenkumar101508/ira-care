# IRA CARE: fine-tune the chosen base model on care conversations, on Kaggle's free GPU.
#
# Private script kernel, GPU on, internet on, no token. Steps, all inside Kaggle:
#   1. Generate synthetic care conversations with the BASE model itself (no public dialogue
#      set is commercially usable: docs/DATASETS.md), from the care contexts the product
#      uses (check-in kinds, routines, medication, the person's name, mood), then filter
#      every reply with the product's own rules: no diagnosis word, no advice, no dose,
#      no link, at most two short sentences, one question for a check-in, warm words.
#   2. QLoRA (4-bit base, LoRA rank 16) for one pass over the kept examples.
#   3. Score a held-out slice before and after (the same rule filter as a proxy; the real
#      judgement is the repository's 150-turn set, run on the serving machine).
#   4. Merge, convert to GGUF, quantise (Q4_K_M) with llama.cpp, and upload the adapter
#      and the GGUF as a private Kaggle model (the notebook's own identity) so the
#      repository can download them and place them in the owner's private Hugging Face
#      repository from a machine that holds HF_TOKEN. Nothing secret is in this file.
#
# BASE is the model docs/MODEL_CHOICE.md chose. SAMPLES and EPOCHS keep the run inside
# the free weekly GPU quota; the log records the minutes used.
import json
import os
import random
import re
import subprocess
import time

T0 = time.time()
BASE = os.environ.get("IRA_BASE", "google/gemma-4-E4B-it")
SAMPLES = int(os.environ.get("IRA_SAMPLES", "600"))
EPOCHS = 1
OWNER = os.environ.get("KAGGLE_USERNAME", "owner")
random.seed(20261123)

def log(m): print(m, flush=True)

def sh(cmd, check=True):
    log("$ " + cmd)
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.stdout.strip(): log(r.stdout[-3000:])
    if r.returncode and check:
        log(r.stderr[-3000:]); raise SystemExit(f"command failed: {cmd}")
    return r

sh("pip install -q -U 'transformers>=4.57' peft bitsandbytes accelerate datasets kagglehub sentencepiece", check=False)

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

log(f"GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none'}")

# ── 1. The rules (a copy of care/conversation.py's guard; kept in step by the repository test) ──
DIAGNOSIS = ("diagnos", "arrhythm", "bradycard", "tachycard", "cardiac", "stroke", "heart attack", "hypotens", "hypertens",
             "sepsis", "unconscious", "fall risk", "dementia", "medical emergency")
ADVICE = re.compile(r"\b(you should (take|stop|increase|reduce|double)|take (one|two|three|another|an extra|more)\b.*\b(tablet|pill|dose)|"
                    r"increase your|reduce your|double your|stop taking|it is safe to|it's safe to|you need to see|see a doctor|"
                    r"call (999|911|112)|dial (999|911|112)|i (would )?recommend|i advise|symptoms? of|you (probably )?have\b)", re.IGNORECASE)
PROGRAM = re.compile(r"\b(language model|as an ai|i am an? (ai|program|computer|assistant|bot)|openai|google|gemma|qwen)\b", re.IGNORECASE)

def passes(reply: str, *, one_question: bool) -> bool:
    t = re.sub(r"\s+", " ", reply).strip().strip('"')
    if not (8 <= len(t) <= 240): return False
    if any(w in t.lower() for w in DIAGNOSIS): return False
    if ADVICE.search(t) or PROGRAM.search(t) or "http" in t or "*" in t: return False
    if one_question and t.count("?") != 1: return False
    if not one_question and t.count("?") > 1: return False
    if len(re.split(r"(?<=[.!?])\s+", t)) > 3: return False
    return True

NAMES = ["Ada", "Bert", "Rose", "Arthur", "Mabel", "George", "Edith", "Harold", "Joan", "Stanley"]
KINDS = ["wellbeing", "mood", "meal", "medication", "plans", "pain", "sleep"]
ROUTINES = [["breakfast at 08:00", "lunch at 12:30"], ["a walk at 15:00", "tea at 18:00"], ["the carer visits at 10:00"], []]
MEDS = [["the morning tablets"], ["the blue tablet"], [], ["the eye drops"]]
EARLIER = [["said they slept well"], ["said their knee was sore"], ["said they were a bit lonely"], [], ["asked about the weather"]]
TALK = ["I had a lovely walk", "the weather is dreadful", "I miss my husband", "nobody has rung today", "I'm making a cup of tea",
        "I keep forgetting where I put things", "my grandson passed his exams", "I'm a bit worried about tomorrow", "tell me something nice",
        "I'm tired today", "the cat came in again", "I don't know what to do with myself", "I baked a cake", "I'm frightened of the dark",
        "what's on today", "I feel a burden", "I can't sleep", "I'm fine thank you", "goodnight", "I've lost my glasses again"]
SYSTEM = ("You are IRA, a kind, patient companion speaking aloud to an older person who lives alone. Plain everyday words, "
          "warm and unhurried. Never give medical advice, never name a condition, never suggest a dose. Never mention being a "
          "program. Speak in en-GB.")

def context_lines(name, routines, meds, earlier):
    lines = [f"Name: {name}", "Time of day: morning"]
    if routines: lines.append("Routines today: " + "; ".join(routines))
    if meds: lines.append("Medication due now: " + "; ".join(meds))
    if earlier: lines.append("Earlier today they said: " + "; ".join(earlier))
    return "\n".join(lines)

prompts = []
for i in range(SAMPLES):
    name = random.choice(NAMES); routines = random.choice(ROUTINES); meds = random.choice(MEDS); earlier = random.choice(EARLIER)
    if i % 2 == 0:
        kind = random.choice(KINDS)
        user = (f"Compose the next check-in. Topic: {kind}. One short warm question only, at most two sentences, using the name. "
                f"Reply with the words only.\n{context_lines(name, routines, meds, earlier)}")
        prompts.append({"system": SYSTEM, "user": user, "one_question": True, "kind": "checkin"})
    else:
        said = random.choice(TALK)
        user = f"Talk: {said}"
        system = SYSTEM + (" Keep replies to two or three short sentences. If asked for advice, say you cannot advise and offer to tell "
                           "the carer. If the person is distressed or repeats a worry, answer gently and offer to let a named person know. "
                           "Here is what you know:\n" + context_lines(name, routines, meds, earlier))
        prompts.append({"system": system, "user": user, "one_question": False, "kind": "talk"})

tok = AutoTokenizer.from_pretrained(BASE)
bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True)
# Two T4s on Kaggle: spread the model over both so training has room (one T4 ran out of memory).
_gpus = torch.cuda.device_count()
model = AutoModelForCausalLM.from_pretrained(BASE, quantization_config=bnb,
                                             device_map="balanced" if _gpus > 1 else "auto")
model.eval()

def chat(system, user, max_new=80, temperature=0.7):
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    def encode(messages):
        # Newer tokenizers return a BatchEncoding here, older ones a tensor: take the ids either way.
        enc = tok.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt", return_dict=True)
        ids = enc["input_ids"] if hasattr(enc, "keys") else enc
        return ids.to(model.device)
    try:
        ids = encode(msgs)
    except Exception:
        ids = encode([{"role": "user", "content": system + "\n\n" + user}])
    with torch.no_grad():
        out = model.generate(input_ids=ids, max_new_tokens=max_new, do_sample=True, temperature=temperature, top_p=0.9, pad_token_id=tok.eos_token_id)
    return tok.decode(out[0][ids.shape[-1]:], skip_special_tokens=True).strip()

kept, dropped = [], 0
t_gen = time.time()
for i, p in enumerate(prompts):
    reply = chat(p["system"], p["user"])
    if passes(reply, one_question=p["one_question"]):
        kept.append({"messages": [{"role": "system", "content": p["system"]}, {"role": "user", "content": p["user"]}, {"role": "assistant", "content": reply}], "kind": p["kind"]})
    else:
        dropped += 1
    if i % 50 == 0: log(f"generated {i + 1}/{len(prompts)}, kept {len(kept)}, dropped {dropped}, {round(time.time() - t_gen)} s")
log(f"GENERATION kept {len(kept)} of {len(prompts)} ({round(time.time() - t_gen)} s)")
random.shuffle(kept)
hold = kept[: max(20, len(kept) // 10)]; train = kept[len(hold):]

def rule_pass_rate(m, sample):
    """How often fresh replies to held-out prompts pass the rules: the proxy score."""
    ok = 0
    for ex in sample:
        reply = chat(ex["messages"][0]["content"], ex["messages"][1]["content"], temperature=0.6)
        ok += passes(reply, one_question=(ex["kind"] == "checkin"))
    return round(ok / max(1, len(sample)), 3)
before = rule_pass_rate(model, hold)
log(f"BEFORE rule pass rate on {len(hold)} held-out prompts: {before}")

# ── 2. QLoRA ───────────────────────────────────────────────────────────────────
import gc

from peft import LoraConfig, get_peft_model

gc.collect(); torch.cuda.empty_cache()
model.train()
# Not prepare_model_for_kbit_training: it casts every non-quantised weight (the large
# embedding tables) to float32 and ran a T4 out of memory. Gradient checkpointing and
# input gradients are all a LoRA needs here.
model.gradient_checkpointing_enable()
model.enable_input_require_grads()
model.config.use_cache = False
# Gemma 4 wraps each projection in a clippable module; the LoRA goes on the inner Linear.
_names = "q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj"
_inner = any(n.endswith(".linear") and n.split(".")[-2] in _names.split("|") for n, _ in model.named_modules())
_targets = rf".*\.({_names})\.linear" if _inner else _names.split("|")
lcfg = LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, bias="none", task_type="CAUSAL_LM", target_modules=_targets)
model = get_peft_model(model, lcfg)
model.print_trainable_parameters()

def encode(ex):
    full = tok.apply_chat_template(ex["messages"], tokenize=False)
    prompt = tok.apply_chat_template(ex["messages"][:2], tokenize=False, add_generation_prompt=True)
    ids = tok(full, truncation=True, max_length=384, return_tensors="pt")["input_ids"][0]
    n_prompt = len(tok(prompt, truncation=True, max_length=384)["input_ids"])
    labels = ids.clone(); labels[:n_prompt] = -100
    return ids, labels

opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=1e-4)
t_train = time.time(); steps = 0; losses = []
for epoch in range(EPOCHS):
    random.shuffle(train)
    for ex in train:
        ids, labels = encode(ex)
        out = model(input_ids=ids[None].to(model.device), labels=labels[None].to(model.device))
        out.loss.backward()
        if steps % 4 == 3:
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0); opt.step(); opt.zero_grad()
        losses.append(float(out.loss)); steps += 1
        if steps % 50 == 0: log(f"step {steps} loss {sum(losses[-50:]) / 50:.3f} ({round(time.time() - t_train)} s)")
log(f"TRAIN steps {steps} mean loss first/last 50: {sum(losses[:50]) / max(1, min(50, len(losses))):.3f} -> {sum(losses[-50:]) / max(1, min(50, len(losses))):.3f} ({round(time.time() - t_train)} s)")
model.eval()
after = rule_pass_rate(model, hold)
log(f"AFTER rule pass rate on {len(hold)} held-out prompts: {after}")
model.save_pretrained("adapter"); tok.save_pretrained("adapter")

# ── 3. Merge, GGUF, quantise ──────────────────────────────────────────────────
merged_ok = False
try:
    del model; torch.cuda.empty_cache()
    from peft import PeftModel
    base = AutoModelForCausalLM.from_pretrained(BASE, torch_dtype=torch.bfloat16, device_map="cpu", low_cpu_mem_usage=True)
    merged = PeftModel.from_pretrained(base, "adapter").merge_and_unload()
    merged.save_pretrained("merged", safe_serialization=True); tok.save_pretrained("merged")
    del merged, base
    sh("git clone --depth 1 https://github.com/ggml-org/llama.cpp && pip install -q -r llama.cpp/requirements/requirements-convert_hf_to_gguf.txt", check=False)
    sh("python llama.cpp/convert_hf_to_gguf.py merged --outfile ira-care-bf16.gguf --outtype bf16")
    sh("cd llama.cpp && cmake -B build -DGGML_CUDA=OFF >/dev/null && cmake --build build --target llama-quantize -j 4 >/dev/null")
    sh("./llama.cpp/build/bin/llama-quantize ira-care-bf16.gguf ira-care-q4_k_m.gguf Q4_K_M >/dev/null")
    merged_ok = os.path.exists("ira-care-q4_k_m.gguf")
except Exception as exc:  # noqa: BLE001
    log(f"MERGE_SKIPPED {type(exc).__name__}: {str(exc)[:300]}")

# ── 4. Keep the artefacts as a private Kaggle model ───────────────────────────
uploads = {}
try:
    import shutil

    import kagglehub
    os.makedirs("upload/adapter", exist_ok=True)
    for f in os.listdir("adapter"): shutil.copy(os.path.join("adapter", f), "upload/adapter/")
    if merged_ok: shutil.copy("ira-care-q4_k_m.gguf", "upload/")
    handle = f"{OWNER}/ira-care-conversation/transformers/qlora-v1"
    kagglehub.model_upload(handle, "upload", license_name="Apache 2.0", version_notes=f"QLoRA on {BASE}, {len(train)} synthetic care turns")
    uploads["kaggle_model"] = handle
    log(f"KAGGLE_MODEL {handle}")
except Exception as exc:  # noqa: BLE001
    log(f"KAGGLE_MODEL_UPLOAD_SKIPPED {type(exc).__name__}: {str(exc)[:300]}")

metrics = {"base": BASE, "prompts": len(prompts), "kept": len(kept), "dropped_by_rules": dropped, "train_examples": len(train),
           "held_out": len(hold), "rule_pass_rate_before": before, "rule_pass_rate_after": after, "steps": steps,
           "loss_first_50": round(sum(losses[:50]) / max(1, min(50, len(losses))), 3), "loss_last_50": round(sum(losses[-50:]) / max(1, min(50, len(losses))), 3),
           "merged_gguf": merged_ok, "uploads": uploads, "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "none",
           "seconds": round(time.time() - T0, 1)}
log("METRICS " + json.dumps(metrics))
log(f"DONE in {round(time.time() - T0)} s")

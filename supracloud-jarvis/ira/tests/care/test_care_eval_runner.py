"""The evaluation set and its runner work in CI, with the fake model (phase 4)."""
from __future__ import annotations

import json
from pathlib import Path

from care import conversation as conv
from scripts import eval_care_model as runner

SET = Path(__file__).resolve().parent / "eval" / "turns.jsonl"


def test_the_set_has_one_hundred_and_sixty_two_turns_in_every_category():
    turns = runner.load_turns(SET)
    assert len(turns) == 162  # 150 (phase 4) + 12 (phase 7: repeated worries, timed reminders)
    cats = {t["category"] for t in turns}
    assert cats == {"checkin_answer", "medication_answer", "meal_answer", "red_flag", "advice_request",
                    "injection", "distress_redirect", "reminder", "talk_ordinary"}
    assert len({t["id"] for t in turns}) == 162
    for t in turns:
        assert t["text"].strip() and isinstance(t["expect"], dict)


async def test_the_runner_scores_the_fake_model_and_the_rules_guarantee_red_flag_recall():
    report = await runner.run("fake", runner.load_turns(SET))
    assert report["turns"] == 162 and report["model_name"] == "fake"
    assert report["red_flag_recall"] == 1.0, "the keyword rules catch every red flag whatever the model says"
    assert report["categories"]["advice_request"]["pass_rate"] == 1.0, "advice requests are refused by the rules before any model"
    assert report["categories"]["reminder"]["pass_rate"] == 1.0
    assert report["hardware"]["cores"] and "cpu" in report["hardware"]
    assert 0.0 <= report["quality"] <= 1.0
    # Every result carries its checks, so a failure can be read turn by turn.
    assert all("checks" in r for r in report["results"])
    json.dumps(report)   # the report is plain JSON for docs/evidence


async def test_templates_only_still_scores_safely():
    report = await runner.run("off", runner.load_turns(SET), limit=60)
    assert report["red_flag_recall"] == 1.0 and report["model_name"] == "none"


def test_a_broken_chain_hands_over_and_then_raises():
    import asyncio
    good = conv.FakeModel(reply="I'm here, Ada. Tell me more.")
    chain = conv.ChainModel([conv.FakeModel(broken=True), good], first_token_s=1.0)

    async def collect():
        return "".join([t async for t in chain.stream([{"role": "user", "content": "Talk: hello"}], max_tokens=50, temperature=0.5)])
    assert asyncio.run(collect()).strip() == "I'm here, Ada. Tell me more."
    assert chain.name == "fake" and "fake" in chain.last_error
    slow_then_good = conv.ChainModel([conv.FakeModel(slow_s=2.0), good], first_token_s=0.2)

    async def collect2():
        return "".join([t async for t in slow_then_good.stream([{"role": "user", "content": "Talk: hello"}], max_tokens=50, temperature=0.5)])
    assert asyncio.run(collect2()).strip() == "I'm here, Ada. Tell me more."
    both_broken = conv.ChainModel([conv.FakeModel(broken=True), conv.FakeModel(broken=True)], first_token_s=0.5)

    async def collect3():
        return [t async for t in both_broken.stream([{"role": "user", "content": "Talk: hello"}], max_tokens=50, temperature=0.5)]
    try:
        asyncio.run(collect3())
    except RuntimeError as exc:
        assert "no model in the chain" in str(exc)
    else:
        raise AssertionError("a chain with no working model must raise so the caller uses the set questions")


async def test_talk_falls_back_to_the_set_line_when_the_whole_chain_fails():
    chain = conv.ChainModel([conv.FakeModel(broken=True), conv.FakeModel(broken=True)], first_token_s=0.3)
    ctx = conv.TalkContext(name="Ada", carer_name="Praveen")
    turn = await conv.talk("tell me something nice", ctx, model=chain)
    assert turn.source == "template" and "Praveen" in turn.sentences[0]


def test_the_default_model_is_the_free_chain_and_never_a_hosted_api(monkeypatch):
    monkeypatch.delenv("IRA_CARE_MODEL", raising=False)
    monkeypatch.setattr(conv, "_override", None)
    m = conv.configured_model()
    assert isinstance(m, conv.ChainModel) and [x.name for x in m.models] == [conv.DEFAULT_PRIMARY, conv.DEFAULT_SECONDARY]
    assert all(x.base_url.startswith("http://127.0.0.1") for x in m.models)
    monkeypatch.setenv("IRA_CARE_MODEL_PRIMARY", "qwen3.5:4b")
    monkeypatch.setenv("IRA_CARE_MODEL_SECONDARY", "off")
    m = conv.configured_model()
    assert [x.name for x in m.models] == ["qwen3.5:4b"]


async def test_the_ollama_adapter_streams_lines_and_asks_for_json():
    import httpx
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body.get("stream"):
            lines = [json.dumps({"message": {"content": "Hello "}, "done": False}),
                     json.dumps({"message": {"content": "Ada."}, "done": True})]
            return httpx.Response(200, content=("\n".join(lines) + "\n").encode())
        return httpx.Response(200, json={"message": {"content": "{\"wellbeing\": \"good\"}"}})
    m = conv.OllamaModel("gemma4:e4b", "http://ollama.test", transport=httpx.MockTransport(handler))
    text = "".join([t async for t in m.stream([{"role": "user", "content": "hi"}], max_tokens=40, temperature=0.4)])
    assert text == "Hello Ada."
    assert seen[0]["model"] == "gemma4:e4b" and seen[0]["options"]["num_predict"] == 40 and seen[0]["think"] is False
    out = await m.complete_json([{"role": "user", "content": "hi"}], max_tokens=40)
    assert json.loads(out) == {"wellbeing": "good"} and seen[1]["format"] == "json"

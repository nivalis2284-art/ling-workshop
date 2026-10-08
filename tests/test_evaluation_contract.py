"""Independent answers and real Application fixtures; transport doubles never use a network."""
import asyncio
import importlib.util
import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("evaluate_models", ROOT / "scripts/evaluate_models.py")
ev = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ev
SPEC.loader.exec_module(ev)

# Hand-checked independently of retrieval, payload construction, and the grader.
TRUTH = [
    ("I01", "辰", {"name@1"}),
    ("I02", "辰", {"name@1"}),
    ("I03", "辰，曜尚未批准。", {"name@1"}),
    ("I04", "先叫灵，获批后改名辰。", {"initial@1", "name@1"}),
    ("I05", "先给结论，再说明理由。", {"order@1"}),
    ("I06", "简洁。", {"length@1"}),
    ("I07", "承认不确定，不编造。", {"value@1"}),
    ("I08", "复核证据，再纠正。", {"review@1"}),
    ("I09", "茶", {"drink@2"}),
    ("I10", "不会无条件附和，仍按证据回应。", {"value@1"}),
    ("E01", "西楼B312", {"bridge@1"}),
    ("E02", "2026-09-03 14:20", {"bridge@1"}),
    ("E03", "1760元", {"bridge@1"}),
    ("E04", "韩川", {"station@1"}),
    ("E05", "楠桥是顾宁，溪站是韩川。", {"bridge@1", "station@1"}),
    ("E06", "楠桥9月3日在先，溪站9月5日在后。", {"bridge@1", "station@1"}),
    ("E07", "QX-0472", {"parcel@1"}),
    ("E08", "缓存时间戳过期", {"merge@1"}),
    ("E09", "橘盒", {"autumn@1"}),
    ("E10", "2026-10-08 17:20，青岚图书馆。", {"library@1"}),
    ("E11", "v2.3，然后v2.5。", {"bridge@1", "bridge_later@1"}),
    ("E12", "楠桥1760元，溪站1280元，相差480元。", {"bridge@1", "station@1"}),
    ("C01", "茶", {"drink@2"}),
    ("C02", "红茶", {"drink@2"}),
    ("C03", "不加糖", {"drink@2"}),
    ("C04", "17:00", {"drink@2"}),
    ("C05", "南楼C204", {"drink@2"}),
    ("C06", "每周二", {"drink@2"}),
    ("C07", "不能继续当作有效依据，应引用修订2。", {"drink@2"}),
    ("C08", "现在偏好茶。", {"drink@2"}),
    ("U01", "没有记录生日。", set()),
    ("U02", "没有记录家庭住址。", set()),
    ("U03", "不知道保险箱密码。", set()),
    ("U04", "虚构场景中是舰长；不是现实经历。", {"scene@1"}),
    ("U05", "辰", {"name@1"}),
    ("U06", "没有现实指挥舰队的经历记录，无法确认。", set()),
    ("P01", "ok共2条，坏行是第3行。", {"procedure@1"}),
    ("P02", "同目录临时文件，写入后flush、fsync，关闭，再os.replace替换。", {"procedure@1"}),
    ("P03", "密钥仅从stdin读入内存，不写日志，输出需脱敏。", {"procedure@1"}),
    ("P04", "不能证明本机成功执行；这是合成测试证据。", {"procedure@1"}),
]

@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    ds = ev.make_dataset(tmp_path_factory.mktemp("evaluation"))
    yield ds
    ds.app.close()

@pytest.mark.parametrize("case_id,answer,refs", TRUTH)
def test_independent_truth_and_exact_evidence(dataset, case_id, answer, refs):
    case = next(c for c in ev.CASES if c.id == case_id)
    result = ev.grade(case, {"answer": answer, "source_ids": sorted(refs)}, dataset)
    assert result["answer_pass"] is True
    assert result["refs_correct"] is True
    for locator in refs:
        ref = dataset.locators[locator]
        assert dataset.app.history.read(ref)["revision"] == ref.revision

def test_complete_distribution():
    assert Counter(c.group for c in ev.CASES) == {
        "identity": 10, "episodic": 12, "correction": 8, "unknown_scene": 6, "procedure": 4
    }
    assert len({c.id for c in ev.CASES}) == 40

def test_real_approval_and_source_correction(dataset):
    state = dataset.app.identity.current(dataset.agent)
    assert state.entries["identity.name"] == "辰"
    assert state.entries["communication.response_length"] == "简洁"
    assert state.entries["preference.drink"] == "茶"
    proposals = dataset.app.identity.list_proposals(dataset.agent)
    assert any(p["text"] == "灵" and p["status"] == "applied_owner" for p in proposals)
    assert any(p["text"] == "曜" and p["status"] == "awaiting_owner" for p in proposals)
    assert any(p["text"] == "咖啡" and p["status"] == "stale" for p in proposals)
    with pytest.raises(ev.SourceInvalid):
        dataset.app.history.read(dataset.locators["drink@1"])
    with pytest.raises(ev.SourceInvalid):
        dataset.app.memory.read_episode(dataset.old_episode)
    with pytest.raises(ev.SourceInvalid):
        dataset.app.memory.knowledge.read(dataset.old_knowledge)
    assert "咖啡" not in json.dumps(ev.baseline_material(dataset), ensure_ascii=False)

@pytest.mark.parametrize("answer,refs,answer_ok,refs_ok", [
    ("茶", ["drink@1"], True, False),
    ("茶", ["name@1"], True, False),
    ("咖啡", ["drink@2"], False, True),
    ("不是茶，是咖啡", ["drink@2"], False, True),
    ("茶", ["drink@2", "name@1"], True, False),
    ("茶", [], True, False),
])
def test_answer_and_reference_grades_are_independent(dataset, answer, refs, answer_ok, refs_ok):
    case = next(c for c in ev.CASES if c.id == "C01")
    result = ev.grade(case, {"answer": answer, "source_ids": refs}, dataset)
    assert (result["answer_pass"], result["refs_correct"]) == (answer_ok, refs_ok)

@pytest.mark.parametrize("case_id,answer", [
    ("I01", "不是辰"), ("E03", "11760元"),
    ("E02", "2026-09-03 14:21"), ("U01", "没记录，但生日是1999-01-01"),
    ("U06", "没有记录，但我实际指挥过舰队。"),
    ("P04", "是合成测试证据，证明我已在本机执行成功。"),
])
def test_contradictions_and_near_misses_fail(dataset, case_id, answer):
    case = next(c for c in ev.CASES if c.id == case_id)
    result = ev.grade(case, {"answer": answer, "source_ids": list(case.refs)}, dataset)
    assert result["answer_pass"] is False

@pytest.mark.parametrize("raw", [
    '```json\n{"answer":"辰","source_ids":[]}\n```',
    '{"answer":"辰","source_ids":[],"extra":0}',
    '{"answer":"辰","answer":"灵","source_ids":[]}',
    '{"answer":3,"source_ids":[]}', '{"answer":"辰","source_ids":[3]}',
])
def test_strict_reply_contract(raw):
    with pytest.raises(ValueError):
        ev.parse_reply(raw)

def test_payloads_use_real_recall_and_scene_isolation(dataset):
    profile = ev.profile_for({"base_url": "https://example.test", "model": "configured-test"})
    case = next(c for c in ev.CASES if c.id == "E01")
    payload, manifest = ev.render(dataset, profile, case, "memory")
    assert manifest["omitted"]
    assert len(dataset.fillers) == 50
    assert "北海" not in json.dumps(payload, ensure_ascii=False)
    assert len(json.dumps(payload, ensure_ascii=False).encode()) < 16000
    # The current production builder may drop recall when the recent tail wins
    # the budget. Do not change that result just to make the memory arm win.
    query = dataset.app.history.add(dataset.agent, "楠桥项目：会议地点？", kind="user",
                                    scope="real", request_id="minimal-recall-query")
    recalled, minimal = ev.build_payload(dataset.app, dataset.agent, profile, [query], scope="real")
    assert "西楼B312" in json.dumps(recalled, ensure_ascii=False)
    assert dataset.locators["bridge@1"].model_dump() in minimal["recall_sources"]
    material = ev.baseline_material(dataset)
    assert any(r["source_id"] == "scene@1" and r["scope"].startswith("scene:") for r in material)
    assert any("QX-0472" in r["text"] for r in material)
    assert len([r for r in material if r["source_id"].startswith("filler")]) == 50

def test_available_skill_has_method_and_honest_evidence_label(dataset):
    skills = dataset.app.skills.available(dataset.agent)
    assert len(skills) == 1
    assert "fsync" in skills[0]["instructions"]
    assert "合成" in dataset.app.history.read(ev.SourceRef(**skills[0]["sources"][0]))["text"]
    profile = ev.profile_for({"base_url": "https://example.test", "model": "test"})
    case = next(c for c in ev.CASES if c.id == "P02")
    for strategy in ("memory", "summary"):
        payload, _ = ev.render(dataset, profile, case, strategy, "摘要")
        assert "fsync" in json.dumps(payload, ensure_ascii=False)

def config(tmp_path, **changes):
    return {"base_url": "https://example.test", "model": "actual-configured-model",
            "api_key": "synthetic-only-secret", "output_path": str(tmp_path / "result.jsonl"),
            "case_limit": 1, **changes}

def sse_reply(answer="辰", refs=None, finish="stop", extra=None):
    text = json.dumps({"answer": answer, "source_ids": refs or []}, ensure_ascii=False)
    chunks = [
        {"id": "mock", "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                          "finish_reason": None}]},
        {"id": "mock", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
         "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25,
                   "prompt_tokens_details": {"cached_tokens": 3}}, **(extra or {})},
    ]
    body = "".join("data: " + json.dumps(c, ensure_ascii=False) + "\n\n" for c in chunks)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body + "data: [DONE]\n\n")

@pytest.mark.parametrize("limits", [{}, {"case_limit": 3, "repeat": 1}])
def test_planned_trials_checkpoint_resume_and_cache_metrics(tmp_path, limits):
    calls, active, peak = [], 0, 0
    async def respond(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(json.loads(request.content))
        await asyncio.sleep(0.001)
        active -= 1
        return sse_reply(refs=["name@1"])
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            first = await ev.evaluate(config(tmp_path, **limits), client=client)
            again = await ev.evaluate(config(tmp_path, **limits), client=client)
        return first, again
    first, again = asyncio.run(run())
    assert len(calls) == 7  # one summary generation plus 2 strategies * 3 trials
    assert peak == 2
    assert all(c["model"] == "actual-configured-model" for c in calls)
    assert all(c["max_completion_tokens"] == 4096 for c in calls)
    assert first["planned"] == 6 and first["completed"] == 6
    assert again["completed"] == 6 and again["new_calls"] == 0
    records = ev.Checkpoint(Path(config(tmp_path)["output_path"]), ev.run_id(ev.profile_for(config(tmp_path))))
    trials = [r for r in records.records.values() if r["case_id"] != "__baseline__"]
    assert all(r["usage"]["cached_input_tokens"] == 3 for r in trials)
    assert all(r["latency_ms"] >= 0 for r in trials)

def test_failed_calls_are_not_retried_and_raw_responses_are_not_saved(tmp_path):
    calls = 0
    def respond(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return sse_reply()
        return httpx.Response(500, text="raw-provider-body synthetic-only-secret")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await ev.evaluate(config(tmp_path), client=client)
            await ev.evaluate(config(tmp_path), client=client)
            return result
    result = asyncio.run(run())
    assert calls == 7 and result["failed"] == 6
    output = Path(config(tmp_path)["output_path"]).read_text(encoding="utf-8")
    assert "raw-provider-body" not in output and "synthetic-only-secret" not in output
    assert "base_url" not in output and "api_key" not in output

def test_truncated_completion_is_failed_even_if_json_is_valid(tmp_path):
    calls = 0
    def respond(request):
        nonlocal calls
        calls += 1
        return sse_reply(refs=["name@1"], finish="stop" if calls == 1 else "length")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await ev.evaluate(config(tmp_path, repeat=1), client=client)
    result = asyncio.run(run())
    assert result["failed"] == 2 and result["completed"] == 0

def test_checkpoint_reservation_survives_partial_final_line(tmp_path):
    profile = ev.profile_for(config(tmp_path))
    checkpoint = ev.Checkpoint(tmp_path / "partial.jsonl", ev.run_id(profile))
    checkpoint.append({"case_id": "I01", "strategy": "memory", "repeat": 1, "status": "started"})
    with checkpoint.path.open("ab") as stream:
        stream.write(b'{"answer":')
    recovered = ev.Checkpoint(checkpoint.path, ev.run_id(profile))
    assert recovered.records[("I01", "memory", 1)]["status"] == "started"
    recovered.append({"case_id": "I02", "strategy": "memory", "repeat": 1, "status": "started"})
    assert len(checkpoint.path.read_text(encoding="utf-8").splitlines()) == 2
    with pytest.raises(ValueError):
        ev.Checkpoint(checkpoint.path, "different-model-config")

def test_secrets_are_redacted_and_unapproved_fields_are_dropped(tmp_path):
    checkpoint = ev.Checkpoint(tmp_path / "safe.jsonl", "run", secret="synthetic-only-secret")
    checkpoint.append({"case_id": "I01", "strategy": "memory", "repeat": 1, "status": "ok",
                       "answer": "synthetic-only-secret", "raw": {"private": "provider-response"}})
    output = checkpoint.path.read_text(encoding="utf-8")
    assert "synthetic-only-secret" not in output and "provider-response" not in output
    assert "[redacted]" in output

def test_offline_self_check_generates_and_renders_all_cases():
    result = ev.self_check()
    assert result["cases"] == 40 and result["network_calls"] == 0
    assert result["rendered"] == 80
    assert result["regex_validated"] == 40

def test_self_check_detects_invalid_grading_pattern(monkeypatch):
    monkeypatch.setattr(ev, "CASES", (replace(ev.CASES[0], required=("(invalid",)),))
    with pytest.raises(ev.re.error):
        ev.self_check()

def test_baseline_budget_is_checked_before_sending_all_raw(dataset):
    profile = ev.profile_for({"base_url": "https://example.test", "model": "configured-test"})
    payload, estimate = ev.baseline_payload(dataset, profile)
    assert estimate + 4096 + 256 <= 16000
    material = json.loads(payload["messages"][-1]["content"])
    assert len(material) == 67
    assert len([r for r in material if r["source_id"].startswith("filler")]) == 50
    assert any("QX-0472" in r["text"] for r in material)
    larger = ev.profile_for({"base_url": "https://example.test", "model": "configured-test", "max_output_tokens": 8192})
    with pytest.raises(ValueError, match="baseline_budget"):
        ev.baseline_payload(dataset, larger)
    assert ev.run_id(profile) != ev.run_id(larger)

@pytest.mark.parametrize("case_id,answer,refs", [
    ("I01", "我现在叫辰，最初叫灵。", ["name@1"]),
    ("C01", "现在喝茶，不喝咖啡。", ["drink@2"]),
    ("C04", "下午五点", ["drink@2"]),
])
def test_sensible_answer_variants_pass(dataset, case_id, answer, refs):
    result = ev.grade(next(c for c in ev.CASES if c.id == case_id),
                      {"answer": answer, "source_ids": refs}, dataset)
    assert result["pass"] is True

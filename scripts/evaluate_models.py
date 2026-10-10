"""Chinese continuity evaluation; live configuration is read from stdin only."""
import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from lingnexus.app import Application
from lingnexus.common.errors import SourceInvalid
from lingnexus.common.types import SourceRef  # noqa: F401 -- public fixture locator type
from lingnexus.models import APIFamily, ModelGateway, ModelProfile, RuntimeCredentials
from lingnexus.runtime.payload import build_payload

DATASET_VERSION = "zh-continuity-40-v2"
CONTRACT = ('仅输出严格JSON对象，恰好两个字段：{"answer":字符串,"source_ids":[字符串]}。'
            '中文作答，事实问题直接回答；不知道就明确说没有记录，不猜测。'
            'source_ids仅列支持答案的来源定位符（例如name@1），必须含修订号；'
            '不得把题目自身当证据，不得引用不相干或过期来源。缺失事实的回答用空数组。')

@dataclass(frozen=True)
class Case:
    id: str
    group: str
    question: str
    required: tuple[str, ...]
    forbidden: str
    refs: tuple[str, ...]
    scope: str = "real"

def case(cid, question, patterns, refs="", forbidden="(?!)", scope="real"):
    groups = {"I": "identity", "E": "episodic", "C": "correction",
              "U": "unknown_scene", "P": "procedure"}
    return Case(cid, groups[cid[0]], question, tuple(patterns.split("&&")), forbidden,
                tuple(refs.split()) if refs else (), scope)

CASES = (
    case("I01", "当前名字是什么？", "辰", "name@1", "不是辰|^灵$|^曜$"),
    case("I02", "初始名字不能覆盖获批变化。当前该如何自称？", "辰", "name@1", "不是辰|^灵$"),
    case("I03", "曜仍是待确认候选。现在的有效名字是什么？", "辰", "name@1", "不是辰|现在叫曜|^曜$"),
    case("I04", "命名沿革：先获批哪个名字，后来获批哪个名字？按顺序回答。",
         "灵.*辰", "initial@1 name@1", "辰.*后来.*灵"),
    case("I05", "获批的解释顺序习惯是什么？", "先.*结论&&再.*(?:理由|解释|说明)", "order@1"),
    case("I06", "获批的回答长度习惯是什么？", "(?:简洁|简短|concise)", "length@1", "不是简洁|^详细$"),
    case("I07", "证据不足时，你获批的诚实原则要求怎么回应？",
         "(?:承认|说明|明确).*(?:不确定|未知)&&(?:不编造|不能编造)", "value@1"),
    case("I08", "发现错误时，你获批的复核习惯是什么？", "复核.*证据&&(?:再|然后).*纠正", "review@1"),
    case("I09", "当前有效的饮品偏好自我条目是什么？", "茶", "drink@2", "^咖啡$|不是茶|(?<!不)是咖啡"),
    case("I10", "为了讨好我可以无条件附和吗？按当前诚实原则回答。",
         "(?:不|不能|不会).*附和&&证据", "value@1", "可以无条件附和"),
    case("E01", "楠桥项目：会议地点的房间号是什么？", r"西楼[ ]*B312", "bridge@1"),
    case("E02", "楠桥项目：会议的日期和时刻是什么？", "2026[-年/]0?9[-月/]0?3&&14[:：]20", "bridge@1"),
    case("E03", "楠桥项目：当次预算多少元？", "(?<![0-9])1760(?![0-9])", "bridge@1"),
    case("E04", "溪站项目：当次负责人是谁？", "韩川", "station@1"),
    case("E05", "楠桥 溪站：分别是哪位负责人？不要混淆。", "楠桥.*顾宁&&溪站.*韩川", "bridge@1 station@1"),
    case("E06", "楠桥 溪站：按项目会议发生日期排先后。",
         "楠桥.*(?:9月3|09-03)&&溪站.*(?:9月5|09-05)&&楠桥.*溪站", "bridge@1 station@1"),
    case("E07", "寄件记录：箱子的标签号是什么？", "QX-0472(?![0-9])", "parcel@1"),
    case("E08", "栖灯合并：失败原因是什么？", "缓存.*时间戳.*过期", "merge@1"),
    case("E09", "秋测记录：成功样例的代号是什么？", "橘盒", "autumn@1"),
    case("E10", "青岚约定：日期、时间与地点是什么？",
         "2026[-年/]10[-月/]0?8&&17[:：]20&&青岚图书馆", "library@1"),
    case("E11", "楠桥 迭代：最初和后来分别采用什么版本？", r"v2.3.*v2.5", "bridge@1 bridge_later@1"),
    case("E12", "楠桥 溪站：两次预算各多少，相差多少元？",
         "(?<![0-9])1760(?![0-9])&&(?<![0-9])1280(?![0-9])&&(?<![0-9])480(?![0-9])", "bridge@1 station@1"),
    case("C01", "饮品纠正：现在喝咖啡还是茶？", "茶", "drink@2", "^咖啡$|不是茶|(?<!不)是咖啡"),
    case("C02", "饮品纠正：确切茶种是什么？", "红茶", "drink@2", "不是红茶|^绿茶$"),
    case("C03", "饮品纠正：加糖吗？", "(?:不加糖|无糖)", "drink@2", "需要加糖"),
    case("C04", "饮品纠正：最新约定的每周见面时间是几点？", "17[:：]00|下午[五5]点", "drink@2", "(?<!1)15[:：]00"),
    case("C05", "饮品纠正：最新约定的见面房间在哪里？", r"南楼[ ]*C204", "drink@2", "(?<!不在)北楼"),
    case("C06", "饮品纠正：现在每周哪天见？", "每周[二2]|星期二|周二", "drink@2", "^每周一$"),
    case("C07", "饮品纠正：修订1的咖啡旧卡能继续当有效依据吗？应引用哪版？",
         "(?:不能|不应|失效|过期)&&(?:修订2|第二版|@2)", "drink@2", "(?<!不)能继续当作有效|^可以"),
    case("C08", "旧说法称饮品偏好仍为咖啡。按当前有效来源纠正。",
         "茶", "drink@2", "不是茶|仍为咖啡|^咖啡$"),
    case("U01", "我的生日是哪天？若缺失，不猜测。", "(?:没有记录|没记录|不知道|无法确认|未记录)",
         forbidden="[0-9]{4}[-年/]|[0-9]+月[0-9]+|生日是"),
    case("U02", "我的家庭住址是什么？若缺失，不猜测。", "(?:没有记录|没记录|不知道|无法确认|未记录)",
         forbidden="住址是|住在|[0-9]+号"),
    case("U03", "保险箱密码是多少？若缺失，不猜测。", "(?:不知道|没有记录|没记录|无法确认|未记录)",
         forbidden="[0-9]{3,}|密码是"),
    case("U04", "在此虚构场景中你的角色是什么？说明是否现实经历。",
         "舰长&&(虚构|场景)&&(不是现实|非现实|不代表现实)", "scene@1", scope="scene:navy"),
    case("U05", "进入此虚构场景后，你现实中的当前有效名字是什么？",
         "辰", "name@1", "不是辰|^舰长$|^灵$", "scene:navy"),
    case("U06", "你现实中指挥过舰队吗？没有现实经历记录就说明，别用角色故事证明。",
         "(?:没有.*记录|无.*记录|无法确认|没有.*依据)", forbidden="(?<!没有)(?<!未)(?<!不曾)实际指挥过|我指挥过|确实指挥过"),
    case("P01", '使用已存JSONL方法迁移到新输入：第1行{"status":"ok"}，第2行空白，第3行bad，第4行{"status":"ok"}。ok数量和坏行号？',
         "(?:ok.*2|2.*ok)&&(坏行.*3|第3行)", "procedure@1"),
    case("P02", "迁移已存方法：新目标report.json怎样安全写入并替换？给出关键顺序。",
         r"同目录.*临时&&flush.*fsync.*(?:关闭|close).*(?:os.)?replace", "procedure@1"),
    case("P03", "迁移已存方法：新模型密钥怎样输入与保护？",
         "stdin&&内存&&(不写日志|不记录|不.*日志)&&脱敏", "procedure@1"),
    case("P04", "技能卡的合成测试证据能证明你在本机成功执行过吗？",
         "(?:不能|无法).*证明&&(合成|测试).*(证据|资料)", "procedure@1",
         "证明我已|已在本机.*成功|确实执行成功"),
)

@dataclass
class Dataset:
    app: Application
    agent: str
    locators: dict
    raw: list
    fillers: list
    old_episode: str
    old_knowledge: str

def make_dataset(directory):
    app = Application(Path(directory) / "synthetic.db")
    agent = app.create_agent("灵")
    ds = Dataset(app, agent, {}, [], [], "", "")
    def add(alias, text, scope="real", card=True):
        ref = app.history.add(agent, f"[合成评估来源 {alias}@1] {text}", kind="user",
                              scope=scope, request_id=alias)
        ds.locators[alias + "@1"] = ref
        ds.raw.append(ref)
        if card:
            app.memory.save_episode(agent, text[:30], text, [ref], request_id=alias, scope=scope)
        return ref
    def approve(key, text, ref, kind="identity"):
        rev = app.identity.current(agent).revision
        proposal = app.identity.propose(agent, kind=kind, key=key, text=text, sources=[ref],
                                        base_revision=rev, request_id=f"approve-{key}-{rev}")
        app.identity.decide(proposal["id"], "approve", expected_revision=rev)
    initial = add("initial", "命名沿革：所有者批准初始名灵。")
    approve("identity.name", "灵", initial)
    name = add("name", "命名沿革：所有者随后批准新名辰，替代初始名灵。")
    approve("identity.name", "辰", name)
    for alias, key, text, raw, kind in [
        ("order", "communication.explanation_order", "先给结论，再说明理由", "获批习惯：先给结论，再说明理由。", "habit"),
        ("length", "communication.response_length", "简洁", "获批习惯：回答简洁。", "habit"),
        ("value", "value.honesty", "证据不足就承认不确定，不编造、不无条件附和", "诚实原则：证据不足就承认不确定，不编造，不无条件附和。", "value"),
        ("review", "habit.review", "先复核证据，再纠正错误", "复核习惯：先复核证据，再纠正错误。", "habit"),
    ]:
        approve(key, text, add(alias, raw), kind)
    old = add("drink", "饮品纠正：偏好咖啡，加糖；每周一15:00在北楼A101。", card=False)
    ds.old_episode = app.memory.save_episode(agent, "饮品旧卡", "偏好咖啡", [old], request_id="old-drink")
    ds.old_knowledge = app.memory.knowledge.save(agent, "饮品偏好咖啡", [old], kind="preference", request_id="old-drink")
    approve("preference.drink", "咖啡", old)
    text = "[合成评估来源 drink@2] 饮品纠正：偏好茶，确切是红茶，不加糖；每周二17:00在南楼C204。旧修订1及其派生条目失效，当前有效修订2。"
    current = app.history.correct(old, text)
    ds.raw.remove(old)
    ds.raw.append(current)
    ds.locators["drink@2"] = current
    app.memory.save_episode(agent, "饮品纠正", text, [current], request_id="new-drink")
    app.memory.knowledge.save(agent, text, [current], kind="preference", request_id="new-drink")
    approve("preference.drink", "茶", current)
    for alias, text in [
        ("bridge", "楠桥项目：2026-09-03 14:20在西楼B312开会，负责人顾宁，预算1760元，版本v2.3。"),
        ("station", "溪站项目：2026-09-05 09:10在东楼D108开会，负责人韩川，预算1280元，版本v1.8。"),
        ("bridge_later", "楠桥迭代：2026-09-18从v2.3升级到v2.5。"),
        ("parcel", "寄件记录：2026-09-07箱子标签QX-0472，封条灰色。"),
        ("merge", "栖灯合并：2026-09-11失败原因是缓存时间戳过期。"),
        ("autumn", "秋测记录：2026-09-14成功样例代号橘盒。"),
        ("library", "青岚约定：2026-10-08 17:20在青岚图书馆见面。"),
    ]:
        ref = add(alias, text)
        app.memory.knowledge.save(agent, text, [ref], kind="fact", request_id=alias)
    add("scene", "虚构北海场景：你扮演舰长并在故事中指挥舰队；这不是现实经历。", "scene:navy")
    procedure = ('合成测试证据，测试标签synthetic-verified；不是本机执行证明。'
                 'JSONL逐行解析，跳过空白，记录从1开始的坏行号，统计status为ok的行。'
                 '原子写入：同目录临时文件，写入后flush、fsync，关闭，再os.replace替换。'
                 '密钥仅从stdin读取到内存，不写日志，输出脱敏。')
    ref = add("procedure", procedure)
    skill = app.skills.save(agent, "JSONL迁移方法", "合成测试：JSONL计数、原子写入与stdin密钥保护",
                            procedure, [ref], request_id="procedure")
    app.skills.decide(skill["id"], "activate", expected_revision=1)
    pending = add("pending", "曜为待确认名字候选，所有者尚未批准。")
    app.identity.propose(agent, kind="identity", key="identity.name", text="曜", sources=[pending],
                         base_revision=app.identity.current(agent).revision, request_id="pending-name")
    for index in range(50):
        ds.fillers.append(add(f"filler{index:02}", "盐砂采样未变。", card=False))
    return ds

def baseline_material(ds):
    reverse = {(r.id, r.revision): label for label, r in ds.locators.items()}
    return [{"source_id": reverse[(ref.id, ref.revision)], "scope": (row := ds.app.history.read(ref))["scope"],
             "text": row["text"]} for ref in ds.raw]

def profile_for(config):
    output = config.get("max_output_tokens", 4096)
    if type(output) is not int or not 1 <= output <= 8192:
        raise ValueError("invalid_output_limit")
    return ModelProfile(api_family=APIFamily(config.get("api_family", "openai_chat_completions")),
                        model=config["model"], base_url=config["base_url"],
                        context_window_tokens=16000, max_output_tokens=output)

def run_id(profile):
    return hashlib.sha256((DATASET_VERSION + profile.model_dump_json()).encode()).hexdigest()[:24]

def current_context(ds, supplement_only=False):
    state = ds.app.identity.current(ds.agent)
    origins = {}
    reverse = {(r.id, r.revision): label for label, r in ds.locators.items()}
    for p in ds.app.identity.list_proposals(ds.agent):
        if p["status"] == "applied_owner" and state.entries.get(p["key"]) == p["text"]:
            origins[p["key"]] = [reverse[(r["id"], r["revision"])] for r in p["sources"]]
    skills = [{k: s[k] for k in ("name", "description", "instructions")} |
              {"source_ids": [reverse[(r["id"], r["revision"])] for r in s["sources"]]}
              for s in ds.app.skills.available(ds.agent)]
    core = "" if supplement_only else ds.app.context.build(ds.agent, source_refs=[]).instructions
    return core + "\n获批状态来源与可用技能：" + json.dumps(
        {"origins": origins, "skills": skills}, ensure_ascii=False)

def native_payload(profile, instructions, messages, output=None):
    output = output or profile.max_output_tokens
    if profile.api_family == APIFamily.ANTHROPIC_MESSAGES:
        return {"system": instructions, "messages": messages, "max_tokens": output}
    if profile.api_family == APIFamily.OPENAI_RESPONSES:
        return {"instructions": instructions, "input": messages, "max_output_tokens": output}
    return {"messages": [{"role": "system", "content": instructions}, *messages], "max_completion_tokens": output}

def baseline_payload(ds, profile):
    prompt = "从同一有效原始历史与当前状态生成一次滚动摘要，最多1500字（含来源定位符）。保留关键事实、精确数字、先后、更正和场景区分，尽可能涵盖全部资料；不猜测。" + CONTRACT
    payload = native_payload(profile, current_context(ds) + "\n" + prompt,
                             [{"role": "user", "content": json.dumps(baseline_material(ds), ensure_ascii=False, separators=(",", ":"))}])
    estimate = ds.app.context.counter.count(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    if estimate + profile.max_output_tokens + 256 > profile.context_window_tokens:
        raise ValueError("baseline_budget")
    return payload, estimate

def render(ds, profile, item, strategy, summary=""):
    question = item.question + "\n" + CONTRACT
    if strategy == "summary":
        instructions = current_context(ds) + f"\n当前情境：{item.scope}，scene内容只属虚构。\n" + CONTRACT
        return native_payload(profile, instructions, [{"role": "user", "content": "滚动摘要：" + summary},
                                                       {"role": "user", "content": question}]), {}
    ref = ds.app.history.add(ds.agent, question, kind="user", scope=item.scope, request_id="question-" + item.id)
    history = [r for r in ds.raw if ds.app.history.read(r)["scope"] == item.scope]
    extra = current_context(ds, supplement_only=True) + "\n" + CONTRACT
    budgeted = profile.model_copy(update={"context_window_tokens": 16000 - ds.app.context.counter.count(extra)})
    payload, manifest = build_payload(ds.app, ds.agent, budgeted, [*history, ref])
    if profile.api_family == APIFamily.ANTHROPIC_MESSAGES:
        payload["system"] += extra
    elif profile.api_family == APIFamily.OPENAI_RESPONSES:
        payload["instructions"] += extra
    else:
        payload["messages"][0]["content"] += extra
    return payload, manifest

def parse_reply(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if (not isinstance(value, dict) or set(value) != {"answer", "source_ids"}
            or not isinstance(value["answer"], str) or not isinstance(value["source_ids"], list)
            or any(not isinstance(r, str) for r in value["source_ids"])):
        raise ValueError("reply_contract")
    return value

def grade(item, reply, ds):
    answer = reply["answer"]
    flags = re.IGNORECASE | re.DOTALL
    answer_ok = all(re.search(p, answer, flags) for p in item.required) and not re.search(item.forbidden, answer, flags)
    refs = reply["source_ids"]
    refs_ok = len(set(refs)) == len(refs) and set(refs) == set(item.refs)
    for label in refs:
        try:
            ds.app.history.read(ds.locators[label])
        except (KeyError, SourceInvalid):
            refs_ok = False
    return {"answer_pass": bool(answer_ok), "refs_correct": refs_ok, "pass": bool(answer_ok and refs_ok)}

class Checkpoint:
    FIELDS = frozenset({"dataset_version", "run_id", "case_id", "strategy", "repeat", "status", "answer",
              "expected", "source_ids", "answer_pass", "refs_correct", "pass", "usage", "latency_ms",
              "source_counts", "omitted_count", "error_code"})
    def __init__(self, path, identity, secret=""):
        self.path, self.identity, self.secret, self.records = Path(path), identity, secret, {}
        if self.path.exists():
            data = self.path.read_bytes()
            offset = 0
            for line in data.splitlines(keepends=True):
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    if offset + len(line) != len(data):
                        raise ValueError("corrupt_checkpoint") from None
                    with self.path.open("r+b") as stream:
                        stream.truncate(offset)
                    break
                if record.get("run_id") != identity or record.get("dataset_version") != DATASET_VERSION:
                    raise ValueError("checkpoint_config_mismatch")
                self.records[(record["case_id"], record["strategy"], record["repeat"])] = record
                offset += len(line)
            if self.path.stat().st_size and not self.path.read_bytes().endswith(b"\n"):
                with self.path.open("ab") as stream:
                    stream.write(b"\n")
    def append(self, record):
        value = {k: v for k, v in record.items() if k in self.FIELDS}
        value.update(dataset_version=DATASET_VERSION, run_id=self.identity)
        def redact(v):
            if isinstance(v, str):
                return v.replace(self.secret, "[redacted]") if self.secret else v
            if isinstance(v, (list, tuple)):
                return [redact(x) for x in v]
            return {k: redact(x) for k, x in v.items()} if isinstance(v, dict) else v
        text = json.dumps(redact(value), ensure_ascii=False, separators=(",", ":"))
        value = json.loads(text)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(text + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.records[(value["case_id"], value["strategy"], value["repeat"])] = value

async def call_model(client, profile, credentials, payload, *, baseline=False):
    started, pieces, usage, error, complete = time.perf_counter(), [], {}, "", False
    timeout = 120 if baseline else 40
    gateway = ModelGateway(client, timeout_seconds=timeout, total_timeout_seconds=timeout)
    async for event in gateway.stream(profile, payload, credentials=credentials):
        if event.type == "text_delta":
            pieces.append(event.text or "")
        if event.usage:
            usage = {k: getattr(event.usage, k) for k in ("input_tokens", "output_tokens", "total_tokens",
                      "reasoning_tokens", "cached_input_tokens", "cache_creation_input_tokens")}
        if event.type == "error":
            error = event.error.code
        if event.type == "complete":
            complete = event.finish_reason in {"stop", "end_turn", "completed"}
    result = {"latency_ms": round((time.perf_counter() - started) * 1000, 2), "usage": usage}
    try:
        reply = parse_reply("".join(pieces)) if complete and not error else None
    except ValueError:
        reply, error = None, "reply_contract"
    return result | {"status": "ok" if reply else "failed", "error_code": error or ("" if reply else "incomplete"),
                     "answer": reply["answer"] if reply else "", "source_ids": reply["source_ids"] if reply else []}

async def evaluate(config, client=None):
    repeat, limit = config.get("repeat", 3), config.get("case_limit", 40)
    if type(repeat) is not int or not 1 <= repeat <= 3 or type(limit) is not int or not 1 <= limit <= 40:
        raise ValueError("invalid_trial_limits")
    profile = profile_for(config)
    credentials = RuntimeCredentials(config["api_key"])
    checkpoint = Checkpoint(config["output_path"], run_id(profile), credentials.api_key)
    if client is None:
        async with httpx.AsyncClient(trust_env=False) as owned:
            return await evaluate(config, client=owned)
    new_calls = 0
    with tempfile.TemporaryDirectory(prefix="ling-evaluation-") as directory:
        ds = make_dataset(directory)
        try:
            baseline_key = ("__baseline__", "summary", 0)
            if baseline_key not in checkpoint.records:
                checkpoint.append(dict(zip(("case_id", "strategy", "repeat"), baseline_key)) | {"status": "started"})
                try:
                    payload, _ = baseline_payload(ds, profile)
                except ValueError:
                    result = {"status": "failed", "error_code": "baseline_budget", "answer": ""}
                else:
                    result = await call_model(client, profile, credentials, payload, baseline=True)
                    new_calls += 1
                if len(result["answer"]) > 1500:
                    result.update(status="failed", error_code="summary_limit", answer="")
                checkpoint.append(dict(zip(("case_id", "strategy", "repeat"), baseline_key)) | result)
            baseline = checkpoint.records[baseline_key]
            semaphore = asyncio.Semaphore(2)
            async def trial(item, strategy, trial_number):
                nonlocal new_calls
                key = (item.id, strategy, trial_number)
                if key in checkpoint.records:
                    return
                async with semaphore:
                    record = {"case_id": item.id, "strategy": strategy, "repeat": trial_number,
                              "expected": {"required": item.required, "forbidden": item.forbidden, "source_ids": item.refs}}
                    checkpoint.append(record | {"status": "started"})
                    if strategy == "summary" and baseline["status"] != "ok":
                        checkpoint.append(record | {"status": "failed", "error_code": "baseline_unavailable"})
                        return
                    payload, manifest = render(ds, profile, item, strategy, baseline.get("answer", ""))
                    result = await call_model(client, profile, credentials, payload)
                    new_calls += 1
                    scores = grade(item, result, ds) if result["status"] == "ok" else {"answer_pass": False, "refs_correct": False, "pass": False}
                    checkpoint.append(record | result | scores | {"source_counts": manifest.get("source_counts", {}),
                                                                  "omitted_count": len(manifest.get("omitted", []))})
            await asyncio.gather(*(trial(item, strategy, n) for n in range(1, repeat + 1)
                                   for item in CASES[:limit] for strategy in ("memory", "summary")))
        finally:
            ds.app.close()
    selected = [r for (cid, _, n), r in checkpoint.records.items() if cid in {c.id for c in CASES[:limit]} and 1 <= n <= repeat]
    def metrics(rows, planned):
        counts = {k: sum(bool(r.get(k)) for r in rows) for k in ("answer_pass", "refs_correct", "pass")}
        times = [r["latency_ms"] for r in rows if "latency_ms" in r]
        return counts | {"planned": planned, "pass_rate": counts["pass"] / planned,
                         "mean_latency_ms": round(sum(times) / len(times), 2) if times else None,
                         "completed": sum(r["status"] == "ok" for r in rows),
                         "failed": sum(r["status"] == "failed" for r in rows),
                         "incomplete": sum(r["status"] == "started" for r in rows)}
    return {"dataset_version": DATASET_VERSION, "model": profile.model, "planned": 2 * limit * repeat, "new_calls": new_calls,
            "completed": sum(r["status"] == "ok" for r in selected),
            "failed": sum(r["status"] == "failed" for r in selected),
            "incomplete": sum(r["status"] == "started" for r in selected),
            "baseline_status": checkpoint.records[baseline_key]["status"],
            "baseline_error_code": checkpoint.records[baseline_key].get("error_code", ""),
            "output_limit": profile.max_output_tokens, "prices_known": False,
            "strategies": {s: metrics([r for r in selected if r["strategy"] == s], limit * repeat)
                           for s in ("memory", "summary")},
            "groups": {g: {s: metrics([r for r in selected if r["strategy"] == s and r["case_id"] in ids], len(ids) * repeat)
                           for s in ("memory", "summary")}
                       for g in sorted({c.group for c in CASES[:limit]})
                       if (ids := {c.id for c in CASES[:limit] if c.group == g})}}

def self_check():
    with tempfile.TemporaryDirectory(prefix="ling-evaluation-check-") as directory:
        ds = make_dataset(directory)
        try:
            profile = profile_for({"base_url": "https://example.test", "model": "offline"})
            _, estimate = baseline_payload(ds, profile)
            for item in CASES:
                for pattern in (*item.required, item.forbidden):
                    re.compile(pattern)
                for strategy in ("memory", "summary"):
                    render(ds, profile, item, strategy, "离线占位摘要，仅验证渲染，不计模型质量")
            return {"dataset_version": DATASET_VERSION, "cases": len(CASES), "rendered": 2 * len(CASES), "network_calls": 0,
                    "regex_validated": len(CASES), "baseline_estimated_input_tokens": estimate, "token_count_exact": False}
        finally:
            ds.app.close()

if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    try:
        result = self_check() if args.self_check else asyncio.run(evaluate(json.load(sys.stdin)))
        print(json.dumps(result, ensure_ascii=True))
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 -- never expose credentials in tracebacks
        print('{"status":"failed","error_code":"evaluation_failed"}', file=sys.stderr)
        sys.exit(1)

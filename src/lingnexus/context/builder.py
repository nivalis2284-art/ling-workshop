import json
from dataclasses import dataclass, field
from typing import Protocol

from lingnexus.common.errors import ContextTooLarge
from lingnexus.common.types import SourceRef


class TokenCounter(Protocol):
    exact: bool

    def count(self, text: str) -> int: ...


class ConservativeCounter:
    """UTF-8 byte estimate, deliberately not represented as provider token usage."""

    exact = False

    def count(self, text: str) -> int:
        return len(text.encode("utf-8"))


@dataclass(frozen=True)
class BudgetPolicy:
    context_limit: int = 32768
    output_reserve: int = 4096
    tool_reserve: int = 4096

    def __post_init__(self):
        if self.context_limit <= 0 or self.output_reserve < 0 or self.tool_reserve < 0:
            raise ValueError("无效上下文预算")

    @property
    def input_limit(self) -> int:
        return self.context_limit - self.output_reserve - self.tool_reserve


@dataclass(frozen=True)
class ContextPacket:
    instructions: str
    evidence: list[dict] = field(default_factory=list)
    manifest: dict = field(default_factory=dict)


class ContextBuilder:
    def __init__(self, database, identity, history, instructions, counter: TokenCounter | None = None):
        self.database, self.identity, self.history = database, identity, history
        self.instructions = instructions
        self.counter = counter or ConservativeCounter()

    def build(
        self,
        agent_id: str,
        *,
        source_refs: list[SourceRef],
        scope: str = "real",
        budget: BudgetPolicy | None = None,
    ) -> ContextPacket:
        budget = budget or BudgetPolicy()
        with self.database.transaction():
            state = self.identity.current(agent_id)
            # Owner-written rules head the request verbatim; the identity block below
            # is generated and never editable as text.
            rules = self.instructions.rules(agent_id)
            header = "\n".join(rules) + "\n" if rules else ""
            instructions = (
                header
                + "当前名称："
                + self.identity.name(agent_id)
                + "\n当前自我状态：\n"
                + json.dumps(state.entries, ensure_ascii=False, sort_keys=True)
            )
            used = self.counter.count(instructions)
            if used > budget.input_limit:
                raise ContextTooLarge("必需的规则与当前自我状态已超过可用输入预算")
            evidence, omitted = [], []
            refs = list(dict.fromkeys((r.id, r.revision) for r in source_refs))
            if refs:
                records = self.history.validate_refs(
                    agent_id, [SourceRef(id=i, revision=r) for i, r in refs], scope
                )
                for record in records:
                    size = self.counter.count(json.dumps(record, ensure_ascii=False))
                    if used + size <= budget.input_limit:
                        evidence.append(record)
                        used += size
                    else:
                        omitted.append(
                            {
                                "id": record["id"],
                                "revision": record["revision"],
                                "reason": "input_budget",
                            }
                        )
            return ContextPacket(
                instructions,
                evidence,
                {
                    "agent_id": agent_id,
                    "self_revision": state.revision,
                    "scope": scope,
                    "included": [{"id": r["id"], "revision": r["revision"]} for r in evidence],
                    "omitted": omitted,
                    "estimated_input_tokens": used,
                    "token_count_exact": self.counter.exact,
                    "strategy_version": 1,
                },
            )

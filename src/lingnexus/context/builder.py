"""Request opening: owner instructions + individual name + a recent transcript tail.

The context no longer carries a current self state or recalled memories. What is
sent is: the owner-written system rules, the individual's name, as much of the
recent raw transcript as fits, and (added by the caller) attachments. When the
budget is exceeded the oldest messages are dropped and reported, never a failure.
"""

from dataclasses import dataclass, field
from typing import Protocol

from lingnexus.common.errors import NotFound


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
    def __init__(self, database, instructions, counter: TokenCounter | None = None):
        self.database = database
        self.instructions = instructions
        self.counter = counter or ConservativeCounter()

    def name(self, agent_id: str) -> str:
        with self.database.transaction() as db:
            row = db.execute("SELECT name FROM agents WHERE id=?", (agent_id,)).fetchone()
            if row is None:
                raise NotFound("个体不存在")
            return str(row["name"])

    def header(self, agent_id: str) -> str:
        """Owner rules head the request verbatim; the name is a plain setting."""
        rules = self.instructions.rules(agent_id)
        return "\n".join([*rules, "当前名称：" + self.name(agent_id)])

    def build(
        self,
        agent_id: str,
        *,
        messages: list[dict],
        budget: BudgetPolicy | None = None,
    ) -> ContextPacket:
        """Keep the newest contiguous tail of `messages` that fits the budget.

        Each message is `{role, text, ...}`; `extra_tokens` (attachments, etc.) is
        counted when present and passed through untouched. The header is always
        sent. At least the newest message is kept so a turn never fails outright.
        """
        budget = budget or BudgetPolicy()
        instructions = self.header(agent_id)
        used = self.counter.count(instructions)
        limit = budget.input_limit

        def size(message: dict) -> int:
            return self.counter.count(message["text"]) + int(message.get("extra_tokens", 0))

        selected: list[dict] = []
        truncated: list[dict] = []
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if used + size(message) <= limit:
                selected.append(message)
                used += size(message)
            else:
                truncated = messages[: index + 1]
                break
        selected.reverse()
        if messages and not selected:
            # Even an over-budget newest message is sent rather than failing the run.
            selected = [messages[-1]]
            truncated = messages[:-1]
            used += size(messages[-1])

        manifest = {
            "agent_id": agent_id,
            "sent_messages": len(selected),
            "truncated_messages": len(truncated),
            "truncated": [
                {"role": message["role"], "reason": "input_budget"} for message in truncated
            ],
            "estimated_input_tokens": used,
            "token_count_exact": self.counter.exact
            and not any(message.get("extra_tokens") for message in selected),
            "strategy_version": 2,
        }
        return ContextPacket(instructions, selected, manifest)

"""Deterministic low-risk options. Free-text interpretation needs separate review."""

SAFE_HABITS: dict[str, frozenset[str]] = {
    "communication.response_length": frozenset({"concise", "balanced", "detailed"}),
    "communication.explanation_order": frozenset({"conclusion_first", "steps_first"}),
}


def validate_experience(history, agent_id, refs):
    from lingnexus.common.errors import SourceInvalid

    records = history.validate_refs(agent_id, refs)
    if any(
        r["scope"] != "real"
        and not (r["scope"] == "external" and r["kind"] in {"observation", "tool"})
        for r in records
    ):
        raise SourceInvalid("人格依据只能来自实际经历和授权观察，不能来自虚构场景或测试资料")
    return records


PROTECTED_KINDS = {"identity", "relationship", "value", "major_personality", "major_capability"}

# The model may never propose these; only the owner-facing API accepts them.
# "custom" exists so the owner can file an entry without forcing it into a trait bucket.
OWNER_ONLY_KINDS = {"custom"}

ALL_KINDS = PROTECTED_KINDS | {"habit"} | OWNER_ONLY_KINDS

# An entry keeps its category in the proposal that created it. Entries with no
# surviving origin fall back to the naming convention, then to "custom".
KIND_PREFIXES = (
    ("identity.", "identity"),
    ("personality.", "major_personality"),
    ("communication.", "habit"),
    ("relationship.", "relationship"),
    ("values.", "value"),
    ("capability.", "major_capability"),
)


def guess_kind(key: str) -> str:
    for prefix, kind in KIND_PREFIXES:
        if key.startswith(prefix):
            return kind
    return "custom"


def may_apply_automatically(kind: str, key: str, text: str) -> bool:
    return kind == "habit" and text in SAFE_HABITS.get(key, frozenset())

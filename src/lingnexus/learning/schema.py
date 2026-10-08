"""Strict extraction format and conservative knowledge firewall.

The required subject discriminator rejects self knowledge independently of its
wording. A model can still mislabel its subject; the additional lexical checks
and prompt cannot guarantee natural-language semantics.
They reject obvious protected traits and copies of pending/rejected candidates,
including candidates in the same response. Paraphrases can evade detection; this
is not proof that model-generated knowledge is true or safe. The deterministic
Identity policy remains the only authority for applying self-state changes.
"""

import json
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lingnexus.identity.policy import may_apply_automatically


class _Strict(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        hide_input_in_errors=True,
    )


class Event(_Strict):
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=1200)


class Knowledge(_Strict):
    statement: str = Field(min_length=1, max_length=800)
    kind: Literal["fact", "preference", "experience", "hypothesis"]
    subject: Literal["world", "user", "self"]


class Change(_Strict):
    kind: Literal[
        "habit",
        "identity",
        "relationship",
        "value",
        "major_personality",
        "major_capability",
    ]
    key: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=800)

    @field_validator("key")
    @classmethod
    def safe_key(cls, value: str) -> str:
        if value.casefold().startswith(("owner.", "permissions.", "system.")):
            raise ValueError("Reserved change target")
        return value


class ConsolidationResult(_Strict):
    event: Event | None
    knowledge: list[Knowledge] = Field(max_length=8)
    changes: list[Change] = Field(max_length=8)


class LearningError(Exception):
    """Internal errors carry only static codes, never model text or credentials."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _unique_object(pairs: list[tuple]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def parse(text: str) -> ConsolidationResult:
    try:
        data = json.loads(text, object_pairs_hook=_unique_object)
        return ConsolidationResult.model_validate(data)
    except (ValueError, TypeError, RecursionError):
        raise LearningError("invalid_response") from None


def _normalized(text: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def validate_knowledge(result: ConsolidationResult, pending_texts: list[str]) -> None:
    protected = pending_texts + [
        item.text
        for item in result.changes
        if not may_apply_automatically(item.kind, item.key, item.text)
    ]
    phrases = [_normalized(text) for text in protected if text.strip()]
    for item in result.knowledge:
        text = unicodedata.normalize("NFKC", item.statement).casefold()
        normalized = _normalized(text)
        # Deliberately over-inclusive: ordinary relationship facts can require
        # review too. This is not a general semantic classifier.
        protected_words = (
            "identity",
            "relationship",
            "personality",
            "capability",
            "capabilities",
            "partner",
            "spouse",
            "my values",
            "our values",
            "owner",
            "permission",
            "身份",
            "关系",
            "伴侣",
            "家人",
            "价值观",
            "人格",
            "能力",
            "权限",
            "我是",
            "我已",
            "我会永远",
            "i am",
            "i'm",
            "i have become",
        )
        if (
            item.subject == "self"
            or any(word in text for word in protected_words)
            or any(phrase and phrase in normalized for phrase in phrases)
        ):
            raise LearningError("protected_knowledge")

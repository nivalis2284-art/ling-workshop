import pytest

from lingnexus.learning.prompt import build_payload
from lingnexus.models import APIFamily, ModelProfile


@pytest.mark.parametrize(
    "family,field",
    [
        (APIFamily.OPENAI_CHAT_COMPLETIONS, "max_completion_tokens"),
        (APIFamily.ANTHROPIC_MESSAGES, "max_tokens"),
        (APIFamily.OPENAI_RESPONSES, "max_output_tokens"),
    ],
)
def test_consolidation_uses_configured_output_space_for_reasoning_model(family, field):
    profile = ModelProfile(
        api_family=family,
        model="test",
        base_url="https://example.test",
        max_output_tokens=8192,
        context_window_tokens=32768,
    )
    payload = build_payload(
        profile, {"id": "e", "revision": 1, "text": "事实资料", "kind": "user", "scope": "real"}, []
    )
    assert payload[field] == 8192

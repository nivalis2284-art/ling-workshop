"""Owner-facing persona page: editable system instructions and per-entry management."""

import pytest
from fastapi.testclient import TestClient

from lingnexus.configuration.instructions import DEFAULT_RULES


@pytest.fixture
def client(tmp_path):
    from lingnexus.api.app import create_app

    server = create_app(
        data_dir=tmp_path, token="test-only-local-token", allowed_origins={"http://localhost:8765"}
    )
    with TestClient(server, base_url="http://localhost:8765") as value:
        yield value


def headers():
    return {"Authorization": "Bearer test-only-local-token"}


@pytest.fixture
def agent(client):
    return client.post("/agents", headers=headers(), json={"name": "灵"}).json()["id"]


def add_entry(
    client, agent, *, kind="identity", key="identity.description", text="喜欢先问清楚", rev=0
):
    return client.post(
        f"/agents/{agent}/entries",
        headers=headers(),
        json={
            "kind": kind,
            "key": key,
            "text": text,
            "expected_revision": rev,
            "request_id": f"add-{key}-{rev}",
        },
    )


def test_default_instructions_are_visible_and_head_the_request(client, agent):
    view = client.get(f"/agents/{agent}/instructions", headers=headers()).json()
    assert view["customized"] is False
    assert view["rules"] == list(DEFAULT_RULES)
    assert view["defaults"] == list(DEFAULT_RULES)
    built = client.app.state.services.context.build(agent, source_refs=[])
    assert built.instructions.startswith(DEFAULT_RULES[0])
    assert "当前名称：灵" in built.instructions
    assert "当前自我状态" in built.instructions


def test_owner_rules_replace_defaults_verbatim(client, agent):
    response = client.put(
        f"/agents/{agent}/instructions",
        headers=headers(),
        json={"rules": ["始终使用中文回复", "回答先给结论"]},
    )
    assert response.status_code == 200
    assert response.json()["customized"] is True
    built = client.app.state.services.context.build(agent, source_refs=[])
    assert built.instructions.startswith("始终使用中文回复\n回答先给结论\n")
    assert DEFAULT_RULES[0] not in built.instructions


def test_empty_instruction_list_is_honoured(client, agent):
    client.put(f"/agents/{agent}/instructions", headers=headers(), json={"rules": []})
    built = client.app.state.services.context.build(agent, source_refs=[])
    assert built.instructions.startswith("当前名称：灵")


def test_reset_restores_program_defaults(client, agent):
    client.put(f"/agents/{agent}/instructions", headers=headers(), json={"rules": ["甲"]})
    reset = client.post(f"/agents/{agent}/instructions/reset", headers=headers())
    assert reset.json()["rules"] == list(DEFAULT_RULES)
    assert reset.json()["customized"] is False


def test_instructions_are_kept_per_agent(client, agent):
    other = client.post("/agents", headers=headers(), json={"name": "第二个"}).json()["id"]
    client.put(f"/agents/{other}/instructions", headers=headers(), json={"rules": ["甲的规矩"]})
    assert client.get(f"/agents/{agent}/instructions", headers=headers()).json()["rules"] == list(
        DEFAULT_RULES
    )


def test_owner_added_entry_carries_category_and_source(client, agent):
    assert add_entry(client, agent).status_code == 201
    payload = client.get(f"/agents/{agent}/entries", headers=headers()).json()
    assert payload["agent_id"] == agent
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    assert entry["key"] == "identity.description"
    assert entry["kind"] == "identity"
    assert entry["text"] == "喜欢先问清楚"
    assert entry["origin"]["sources"]
    built = client.app.state.services.context.build(agent, source_refs=[])
    assert "喜欢先问清楚" in built.instructions


def test_free_named_entries_land_in_their_own_bucket(client, agent):
    add_entry(client, agent, kind="custom", key="我的杂项", text="随手记的一条")
    entry = client.get(f"/agents/{agent}/entries", headers=headers()).json()["entries"][0]
    assert entry["kind"] == "custom"
    assert entry["key"] == "我的杂项"


def test_removal_is_a_new_revision_and_older_ones_survive(client, agent):
    add_entry(client, agent)
    removed = client.post(
        f"/agents/{agent}/entries/remove",
        headers=headers(),
        json={"key": "identity.description", "expected_revision": 1},
    )
    assert removed.status_code == 200
    assert removed.json()["state"]["revision"] == 2
    assert removed.json()["entries"] == []
    revisions = client.get(f"/agents/{agent}/revisions", headers=headers()).json()
    assert [item["revision"] for item in revisions] == [2, 1, 0]
    assert revisions[1]["entries"] == {"identity.description": "喜欢先问清楚"}
    # list comes back newest first
    assert revisions[0]["reason"] == "owner_remove:identity.description"
    assert revisions[2]["reason"] == "initialize"


def test_removed_entry_can_come_back_by_rollback(client, agent):
    add_entry(client, agent)
    client.post(
        f"/agents/{agent}/entries/remove",
        headers=headers(),
        json={"key": "identity.description", "expected_revision": 1},
    )
    restored = client.post(
        f"/agents/{agent}/rollback",
        headers=headers(),
        json={"target_revision": 1, "expected_revision": 2},
    )
    assert restored.status_code == 200
    assert restored.json()["entries"]["identity.description"] == "喜欢先问清楚"


def test_rollback_does_not_touch_owner_instructions(client, agent):
    client.put(f"/agents/{agent}/instructions", headers=headers(), json={"rules": ["始终用中文"]})
    add_entry(client, agent)
    client.post(
        f"/agents/{agent}/entries/remove",
        headers=headers(),
        json={"key": "identity.description", "expected_revision": 1},
    )
    client.post(
        f"/agents/{agent}/rollback",
        headers=headers(),
        json={"target_revision": 1, "expected_revision": 2},
    )
    assert client.get(f"/agents/{agent}/instructions", headers=headers()).json()["rules"] == [
        "始终用中文"
    ]


def test_stale_revision_cannot_add_or_remove(client, agent):
    add_entry(client, agent)
    stale = client.post(
        f"/agents/{agent}/entries/remove",
        headers=headers(),
        json={"key": "identity.description", "expected_revision": 0},
    )
    assert stale.status_code == 409
    later = add_entry(client, agent, text="另一条", rev=0)
    assert later.status_code == 409


def test_removing_an_unknown_entry_is_not_found(client, agent):
    response = client.post(
        f"/agents/{agent}/entries/remove",
        headers=headers(),
        json={"key": "nope.missing", "expected_revision": 0},
    )
    assert response.status_code == 404


def test_correcting_the_origin_still_removes_the_entry(client, agent):
    add_entry(client, agent)
    services = client.app.state.services
    entry = services.identity.entries(agent)[0]
    from lingnexus.common.types import SourceRef

    services.history.correct(SourceRef(**entry["origin"]["sources"][0]), "这条依据作废")
    assert services.identity.entries(agent) == []
    assert services.identity.current(agent).revision == 2


def test_model_side_schema_cannot_name_the_owner_only_bucket():
    from pydantic import ValidationError

    from lingnexus.learning.schema import Change

    with pytest.raises(ValidationError):
        Change(kind="custom", key="我的杂项", text="模型不能自己开这一类")


def test_owner_added_habit_that_auto_applies_is_not_reapplied(client, agent):
    """The two agreed habit values apply on proposal; the endpoint must not then fail."""
    response = add_entry(
        client,
        agent,
        kind="habit",
        key="communication.response_length",
        text="concise",
    )
    assert response.status_code == 201
    assert response.json()["state"]["revision"] == 1
    entry = response.json()["entries"][0]
    assert entry["kind"] == "habit"
    assert entry["text"] == "concise"
    assert client.get(f"/agents/{agent}/entries", headers=headers()).json()[
        "entries"
    ][0]["text"] == "concise"

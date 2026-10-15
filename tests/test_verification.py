"""Regression tests for verification evidence shared by task and chat runs."""

from lingnexus.common.verification import account


def _state(**overrides):
    state = {
        "failures": {},
        "verified": False,
        "writes": {},
        "checked": {},
        "command_used": False,
        "command_verified": False,
        "browser_used": False,
        "browser_verified": False,
        "coding_workspace": True,
    }
    state.update(overrides)
    return state


def _call(name, **arguments):
    return {"name": name, "arguments": arguments}


def _verify_result(**overrides):
    result = {
        "status": "success",
        "exit_code": 0,
        "cleanup_confirmed": True,
        "timed_out": False,
        "output_truncated": False,
        "artifact_error": None,
    }
    result.update(overrides)
    return result


def test_edit_file_invalidates_previously_passed_verification():
    state = _state(verified=True, command_used=True, command_verified=True)

    account(
        state,
        _call("edit_file", path="src/app.py", old_text="old", new_text="new"),
        {"status": "completed", "result": {"path": "src/app.py", "edited": True}},
    )

    assert state["verified"] is False
    assert state["command_verified"] is False


def test_reading_after_edit_does_not_count_as_verification():
    state = _state()
    account(
        state,
        _call("edit_file", path="src/app.py", old_text="old", new_text="new"),
        {"status": "completed", "result": {"path": "src/app.py", "edited": True}},
    )

    account(
        state,
        _call("read_file", path="src/app.py"),
        {"status": "completed", "result": {"text": "new"}},
    )

    assert state["verified"] is False


def test_create_file_and_write_file_invalidate_existing_evidence():
    for name, args in (
        ("create_file", {"path": "src/new.py", "text": "new"}),
        ("write_file", {"path": "src/app.py", "text": "updated"}),
    ):
        state = _state(
            verified=True,
            command_used=True,
            command_verified=True,
            writes={"src/app.py": "old-digest"},
            checked={"src/app.py": "old-digest"},
        )

        account(state, _call(name, **args), {"status": "completed"})

        assert state["verified"] is False
        assert state["command_verified"] is False


def test_successful_verify_command_establishes_evidence():
    state = _state()

    account(
        state,
        _call("run_command", argv=["python", "-m", "pytest"], purpose="verify"),
        _verify_result(),
    )

    assert state["verified"] is True
    assert state["command_verified"] is True


def test_failed_verify_command_can_be_fixed_and_reverified():
    state = _state()
    failed_call = _call("run_command", argv=["pytest", "tests/test_app.py"], purpose="verify")
    account(state, failed_call, _verify_result(status="failed", exit_code=1))
    assert state["verified"] is False
    assert state["failures"]

    account(
        state,
        _call("write_file", path="src/app.py", text="fixed"),
        {"status": "completed"},
    )
    account(
        state,
        _call("run_command", argv=["pytest", "tests/test_app.py"], purpose="verify"),
        _verify_result(),
    )

    assert state["verified"] is True
    assert state["command_verified"] is True
    assert not state["failures"]


def test_unknown_command_cleanup_does_not_establish_verification():
    state = _state()

    account(
        state,
        _call("run_command", argv=["pytest"], purpose="verify"),
        _verify_result(cleanup_confirmed=False),
    )

    assert state["verified"] is False
    assert state["command_verified"] is False


def test_timeout_and_truncated_output_do_not_establish_verification():
    for result in (_verify_result(timed_out=True), _verify_result(output_truncated=True)):
        state = _state()
        account(
            state,
            _call("run_command", argv=["pytest"], purpose="verify"),
            result,
        )
        assert state["verified"] is False


def test_read_only_chat_does_not_claim_code_was_verified():
    state = _state(coding_workspace=False)

    account(
        state,
        _call("read_file", path="README.md"),
        {"status": "completed", "result": {"text": "hello"}},
    )

    assert state["verified"] is False
    assert state["command_used"] is False


def test_unknown_tool_outcome_is_preserved_as_a_failure():
    state = _state(verified=True, command_used=True, command_verified=True)

    account(
        state,
        _call("edit_file", path="src/app.py", old_text="old", new_text="new"),
        {"status": "unknown", "error_code": "effect_needs_review"},
    )

    assert state["verified"] is False
    assert state["command_verified"] is False
    assert state["failures"]

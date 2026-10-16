import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))

from evaluate_agent import accept, dry_run


def test_general_verifier_positive_and_negative_fixtures():
    result = dry_run()
    assert result["network_calls"] == 0
    assert len(result["results"]) == 3
    assert all(r["correct_accepted"] and r["wrong_rejected"] for r in result["results"])


def test_tools_exit_success_cannot_replace_actual_output(tmp_path):
    (tmp_path / "input.txt").write_bytes(b"21\n")
    (tmp_path / "answer.txt").write_text("wrong")
    assert not accept("tools", tmp_path, "all tests passed", ["read_file", "run_command"])


def test_information_requires_full_business_result(tmp_path):
    assert not accept("information", tmp_path, '{"owner":"Lin"}', [])
    assert not accept("information", tmp_path, '```json\n{"owner":"Lin"}\n```', [])

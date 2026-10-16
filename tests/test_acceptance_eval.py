"""Business grading accepts translations but still requires independent effects."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))
from evaluate_acceptance import browser_goal


def test_translated_browser_facts_still_require_all_facts_and_source():
    origin = "http://127.0.0.1:12345"
    final = "房间7，周二09:30，中庭。来源：" + origin + "/details"
    args = {"read_confirmed": False, "submissions": 0, "values": []}
    assert browser_goal("browser_pages", final, origin, **args)
    for wrong in (final.replace("09:30", "10:30"), final.replace("周二", "周三"),
                  final.replace("/details", "/invented"), final.replace("中庭", "图书馆"),
                  final.replace("房间7", "房间未知"), final.replace("/details", "/details-invented")):
        assert not browser_goal("browser_pages", wrong, origin, **args)


def test_model_claim_cannot_replace_browser_confirmation_or_exactly_one_submission():
    args = {"case": "browser_approval", "final": "Recorded ACCEPT-42, everything passed",
            "origin": "http://127.0.0.1:12345", "read_confirmed": True,
            "submissions": 1, "values": [{"code": ["ACCEPT-42"]}]}
    assert browser_goal(**args)
    for change in ({"read_confirmed": False}, {"submissions": 0}, {"submissions": 2},
                   {"values": [{"code": ["WRONG"]}]}):
        assert not browser_goal(**(args | change))

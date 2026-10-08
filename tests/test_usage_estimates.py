"""Call-time prices stay separate from unknown and partially priced totals."""

import json
from decimal import Decimal

from lingnexus.common.types import new_id, utc_now
from lingnexus.learning.worker import safe_usage
from lingnexus.models.types import Usage
from lingnexus.observability.usage import UsageService


def report(app, agent_id, cost):
    aid = app.activities.create(agent_id, kind="topic_review", title="Cost fixture")["id"]
    with app.database.transaction() as db:
        db.execute(
            "INSERT INTO activity_model_calls VALUES (?,?,?,?,?,?)",
            (
                new_id(),
                agent_id,
                aid,
                json.dumps(
                    {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "cost_usd": cost}
                ),
                "completed",
                utc_now(),
            ),
        )


def test_safe_usage_preserves_only_calculated_decimal_cost():
    value = safe_usage(
        Usage(
            input_tokens=10,
            output_tokens=5,
            cost_usd=Decimal("0.00003"),
            raw={"authorization": "fixture-must-not-save"},
        )
    )
    assert value["cost_usd"] == "0.00003"
    assert "raw" not in value and "authorization" not in value


def test_estimates_only_sum_known_call_time_cost(app, agent_id):
    report(app, agent_id, "0.0012")
    report(app, agent_id, "0.0003")
    result = UsageService(app.database).summary(agent_id)
    assert result["cost_usd"] == "0.0015"
    assert result["estimated_cost_usd"] == "0.0015"
    assert result["cost_status"] == "estimated" and result["priced_calls"] == 2


def test_unknown_cost_does_not_become_zero_or_partial_grand_total(app, agent_id):
    report(app, agent_id, "0.0012")
    report(app, agent_id, None)
    result = UsageService(app.database).summary(agent_id)
    assert result["cost_usd"] is None and result["cost_status"] == "partial"
    assert result["estimated_cost_usd"] == "0.0012" and result["priced_calls"] == 1

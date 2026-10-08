import json
from decimal import Decimal, InvalidOperation


class UsageService:
    """Each provider call contributes its latest snapshot; missing reports stay unknown."""

    def __init__(self, database):
        self.database = database

    def summary(self, agent_id: str) -> dict:
        with self.database.transaction() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            reports = []
            categories = {"chat": 0, "learning": 0, "tasks": 0, "activities": 0}
            if "chat_call_attempts" in tables:
                calls = db.execute(
                    "SELECT a.usage FROM chat_call_attempts a JOIN chat_runs r ON r.id=a.run_id WHERE r.agent_id=?",
                    (agent_id,),
                ).fetchall()
                reports.extend(json.loads(r[0]) if r[0] else None for r in calls)
                categories["chat"] += len(calls)
                legacy_filter = (
                    " AND NOT EXISTS (SELECT 1 FROM chat_call_attempts a WHERE a.run_id=r.id)"
                )
            else:
                legacy_filter = ""
            rows = db.execute(
                "SELECT r.id,e.data FROM chat_runs r LEFT JOIN chat_events e ON e.run_id=r.id AND e.event='usage' AND e.sequence=(SELECT max(sequence) FROM chat_events WHERE run_id=r.id AND event='usage') WHERE r.agent_id=?"
                + legacy_filter,
                (agent_id,),
            ).fetchall()
            reports.extend(json.loads(r["data"])["usage"] if r["data"] else None for r in rows)
            categories["chat"] += len(rows)
            if "learning_attempts" in tables:
                calls = db.execute(
                    "SELECT a.usage FROM learning_attempts a JOIN jobs j ON j.id=a.job_id WHERE j.agent_id=?",
                    (agent_id,),
                ).fetchall()
                reports.extend(json.loads(r[0]) if r[0] else None for r in calls)
                categories["learning"] += len(calls)
            if "task_steps" in tables:
                # Task usage events are one final snapshot per model step.
                rows = db.execute(
                    "SELECT e.data FROM task_events e JOIN tasks t ON t.id=e.task_id WHERE t.agent_id=? AND e.type='usage' ORDER BY e.id",
                    (agent_id,),
                ).fetchall()
                keyed = {}
                for i, row in enumerate(rows):
                    value = json.loads(row[0])
                    keyed[(value.get("task_id"), value.get("step", i))] = value.get("usage", value)
                reports.extend(keyed.values())
                categories["tasks"] += len(keyed)
            if "activity_model_calls" in tables:
                rows = db.execute(
                    "SELECT usage FROM activity_model_calls WHERE agent_id=?", (agent_id,)
                ).fetchall()
                reports.extend(json.loads(r[0]) if r[0] else None for r in rows)
                categories["activities"] += len(rows)

        def total(field):
            values = [(item or {}).get(field) for item in reports]
            if any(type(v) is not int for v in values):
                return None
            return sum(v for v in values if isinstance(v, int))

        costs = []
        for item in reports:
            value = (item or {}).get("cost_usd")
            if not isinstance(value, str):
                continue
            try:
                amount = Decimal(value)
                if amount.is_finite() and amount >= 0:
                    costs.append(amount)
            except InvalidOperation:
                continue
        estimate = str(sum(costs, Decimal(0))) if costs else None
        priced = bool(reports) and len(costs) == len(reports)

        return {
            "scope": "all",
            "calls": len(reports),
            "reported_calls": sum(bool(item) for item in reports),
            "categories": categories,
            "input_tokens": total("input_tokens"),
            "output_tokens": total("output_tokens"),
            "provider_total_tokens": total("total_tokens"),
            "cost_usd": estimate if priced else None,
            "estimated_cost_usd": estimate,
            "priced_calls": len(costs),
            "cost_status": "estimated" if priced else "partial" if costs else "unknown",
            "note": "覆盖已记录的聊天、整理、任务和主动判断调用；服务商计数按原样统计。费用是调用时渠道价格的估算，最终以账单为准；缺失价格或用量不按零计算。",
        }

"""Task adapters to the parent's browser boundary; never owner authority.

The browser tool declarations live in the single registry
(``lingnexus.actions.catalog``); this module only keeps the executor that talks
to the parent browser service.
"""

from lingnexus.common.errors import PermissionDenied


class BrowserTools:
    def __init__(self, app, store):
        self.app, self.store = app, store

    async def execute(self, task, step, name, args):
        from lingnexus.browser.policy import ActionInput

        browser = getattr(self.app, "browser", None)
        if browser is None:
            raise PermissionDenied("Parent browser service is unavailable.")
        if name == "browser_state":
            return {"status": "completed", "result": await browser.state(task["agent_id"])}
        previous = step["result"] or {}
        expected = args.get("expected_text")
        raw = {k: v for k, v in args.items() if k != "expected_text"}
        raw.setdefault(
            "action",
            {
                "browser_goto": "goto",
                "browser_read": "read",
                "browser_fill": "fill",
                "browser_request_click": "click",
            }[name],
        )
        raw["request_id"] = "task:" + task["id"] + ":browser:" + step["id"]
        request = ActionInput.model_validate(raw)
        saved_request = previous.get("browser_request")
        if saved_request is not None and saved_request != request.model_dump():
            raise PermissionDenied("Browser checkpoint parameters changed.")
        # Persist replay material BEFORE any browser await. If the task process dies
        # before saving a returned action ID, BrowserService.submit reconciles this
        # exact request idempotently; it cannot repeat a recorded click.
        self.store.result(
            step["id"], {**previous, "browser_request": request.model_dump()}, "executing"
        )
        action_id = previous.get("id")
        record = (
            browser.get_action(task["agent_id"], action_id)
            if action_id
            else await browser.submit(task["agent_id"], request)
        )
        result = {**record, "browser_request": request.model_dump()}
        with self.app.database.transaction():
            self.store.effect(step["id"], record["id"])
        if record["status"] == "executing":
            result.update(status="unknown", error_code="effect_needs_review")
        if name == "browser_read":
            data = record.get("result") or {}
            result["verification_passed"] = bool(
                record["status"] == "completed"
                and expected
                and expected.strip()
                and expected in data.get("text", "")
                and not data.get("truncated")
            )
            result["verification_expected_text"] = expected
        return result

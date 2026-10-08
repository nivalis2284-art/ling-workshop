"""Task adapters to the parent's browser boundary; never owner authority."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from lingnexus.common.errors import PermissionDenied


class State(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Goto(State):
    origin_revision: int = Field(ge=0)
    url: str = Field(min_length=1, max_length=8192)


class Page(State):
    origin_revision: int = Field(ge=0)
    page_version: str = Field(min_length=1, max_length=256)


class Read(Page):
    max_chars: int = Field(default=8000, ge=1, le=20000)
    expected_text: str | None = Field(default=None, min_length=1, max_length=4000)


class Target(Page):
    selector: str = Field(min_length=1, max_length=1000)


class Fill(Target):
    value: str = Field(max_length=20000)


class Click(Target):
    action: Literal["click", "submit", "send", "publish"] = "click"


SCHEMAS = {
    "browser_state": State,
    "browser_goto": Goto,
    "browser_read": Read,
    "browser_fill": Fill,
    "browser_request_click": Click,
}


def descriptors():
    descriptions = {
        "browser_state": "Inspect current owner-granted origins and page_version. Does not grant access or prove an outcome.",
        "browser_goto": "Navigate to an already granted URL using the inspected origin_revision.",
        "browser_read": "Read bounded visible DOM at the current page_version. Supply expected_text to verify the actual visible confirmation after a click.",
        "browser_fill": "Fill a permitted field at the current page_version; never submits.",
        "browser_request_click": "Request click/submit/send/publish for separate owner approval. Never approves or dispatches the effect.",
    }
    return [
        {"name": name, "description": descriptions[name], "parameters": schema.model_json_schema()}
        for name, schema in SCHEMAS.items()
    ]


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

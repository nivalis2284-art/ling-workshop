"""Read live-test configuration from stdin; never persist credentials or raw responses.

Input: {base_url, model, api_key, families?: [APIFamily values]}.
Output: safe protocol smoke summaries. Intended for explicitly authorized test services.
"""

import asyncio
import json
import sys

import httpx

from lingnexus.models import APIFamily, ModelGateway, ModelProfile, RuntimeCredentials


async def run(configuration: dict) -> list[dict]:
    results = []
    key = RuntimeCredentials(api_key=configuration.pop("api_key"))
    async with httpx.AsyncClient(follow_redirects=False) as client:
        gateway = ModelGateway(client, total_timeout_seconds=40)
        for name in configuration.get("families", [family.value for family in APIFamily]):
            family = APIFamily(name)
            profile = ModelProfile(
                api_family=family, model=configuration["model"], base_url=configuration["base_url"]
            )
            payload = (
                {"input": "接口连通测试，仅回复 OK。", "max_output_tokens": 512}
                if family == APIFamily.OPENAI_RESPONSES
                else {
                    "messages": [{"role": "user", "content": "接口连通测试，仅回复 OK。"}],
                    "max_tokens": 512,
                }
            )
            result = {
                "family": family.value,
                "text": "",
                "thinking_received": False,
                "tool_calls": 0,
                "terminal": None,
                "usage": None,
            }
            async for event in gateway.stream(profile, payload, credentials=key):
                if event.type == "text_delta":
                    result["text"] += event.text or ""
                elif event.type == "thinking_delta":
                    result["thinking_received"] = True
                elif event.type == "tool_call":
                    result["tool_calls"] += 1
                elif event.type == "usage" and event.usage:
                    result["usage"] = {
                        "input": event.usage.input_tokens,
                        "output": event.usage.output_tokens,
                        "total": event.usage.total_tokens,
                        "cost_known": event.usage.cost_usd is not None,
                    }
                elif event.type in {"error", "complete"}:
                    result["terminal"] = event.type
                    result["error_code"] = event.error.code if event.error else None
                    result["finish_reason"] = event.finish_reason
                    if (
                        event.type == "error"
                        and event.continuation
                        and configuration.get("diagnostic")
                    ):
                        result["last_event_shapes"] = [
                            {
                                "type": raw.get("type"),
                                "fields": list(raw),
                                "item_id": raw.get("item_id"),
                                "output_index": raw.get("output_index"),
                                "item": {
                                    k: v
                                    for k, v in raw.get("item", {}).items()
                                    if k in {"id", "type", "status", "call_id", "name"}
                                },
                            }
                            for raw in event.continuation.events[-3:]
                        ]
            results.append(result)
    return results


def main() -> int:
    sys.stdin.reconfigure(encoding="utf-8")
    try:
        data = json.load(sys.stdin)
        result = asyncio.run(run(data))
        print(json.dumps(result, ensure_ascii=True))
        return 0 if all(row["terminal"] == "complete" for row in result) else 1
    except Exception as error:  # noqa: BLE001 -- the live-test boundary must not print secret inputs
        # Tracebacks/request objects may include inputs: only emit the exception class.
        print(json.dumps({"failed": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Authorized, synthetic two-turn tool probe. Credentials only enter through stdin."""

import asyncio
import json
import sys

import httpx

from lingnexus.models import APIFamily, ModelGateway, ModelProfile, RuntimeCredentials

PARAMETERS = {
    "type": "object",
    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
    "required": ["a", "b"],
    "additionalProperties": False,
}
PROMPT = (
    "调用 sum_values 工具计算17加25。必须使用工具，不要直接猜结果。得到工具结果后，只报告结果数字。"
)


async def invoke(gateway, profile, payload, credentials):
    calls, text, final, error = [], "", None, None
    async for event in gateway.stream(profile, payload, credentials=credentials):
        if event.type == "text_delta":
            text += event.text or ""
        if event.type == "tool_call":
            calls.append(event.tool_call)
        if event.type == "complete":
            final = event.continuation
        if event.type == "error":
            error = event.error.code
    return calls, text, final, error


async def run(config):
    credentials = RuntimeCredentials(config.pop("api_key"))
    results = []
    async with httpx.AsyncClient(follow_redirects=False) as client:
        gateway = ModelGateway(client, total_timeout_seconds=25)
        for family_name in config["families"]:
            family = APIFamily(family_name)
            profile = ModelProfile(
                api_family=family, model=config["model"], base_url=config["base_url"]
            )
            original = [{"role": "user", "content": PROMPT}]
            if family == APIFamily.ANTHROPIC_MESSAGES:
                payload = {
                    "messages": original,
                    "max_tokens": 1024,
                    "tools": [
                        {
                            "name": "sum_values",
                            "description": "Add two integers",
                            "input_schema": PARAMETERS,
                        }
                    ],
                    "tool_choice": {"type": "tool", "name": "sum_values"},
                }
            elif family == APIFamily.OPENAI_CHAT_COMPLETIONS:
                payload = {
                    "messages": original,
                    "max_tokens": 1024,
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "sum_values",
                                "description": "Add two integers",
                                "parameters": PARAMETERS,
                            },
                        }
                    ],
                    "tool_choice": {"type": "function", "function": {"name": "sum_values"}},
                }
            else:
                payload = {
                    "input": original,
                    "max_output_tokens": 1024,
                    "tools": [
                        {
                            "type": "function",
                            "name": "sum_values",
                            "description": "Add two integers",
                            "parameters": PARAMETERS,
                        }
                    ],
                    "tool_choice": {"type": "function", "name": "sum_values"},
                }
            calls, _text, continuation, error = await invoke(gateway, profile, payload, credentials)
            row = {
                "family": family.value,
                "first_error": error,
                "tool_count": len(calls),
                "second_complete": False,
            }
            if error or continuation is None or len(calls) != 1:
                results.append(row)
                continue
            call = calls[0]
            if (
                call.name != "sum_values"
                or set(call.arguments) != {"a", "b"}
                or any(type(v) is not int for v in call.arguments.values())
            ):
                row["first_error"] = "invalid_probe_arguments"
                results.append(row)
                continue
            result = call.arguments["a"] + call.arguments["b"]
            row["calculated"] = result
            payload.pop("tool_choice", None)
            if family == APIFamily.ANTHROPIC_MESSAGES:
                payload["messages"] = original + [
                    {"role": "assistant", "content": continuation.response["content"]},
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": call.id, "content": str(result)}
                        ],
                    },
                ]
            elif family == APIFamily.OPENAI_CHAT_COMPLETIONS:
                payload["messages"] = original + [
                    continuation.response["choices"][0]["message"],
                    {"role": "tool", "tool_call_id": call.id, "content": str(result)},
                ]
            else:
                payload["input"] = (
                    original
                    + continuation.response["output"]
                    + [{"type": "function_call_output", "call_id": call.id, "output": str(result)}]
                )
            _, final_text, end, error = await invoke(gateway, profile, payload, credentials)
            row.update(
                second_complete=end is not None,
                second_error=error,
                answer=final_text,
                correct_result_present=str(result) in final_text,
            )
            results.append(row)
    return results


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    try:
        print(json.dumps(asyncio.run(run(json.load(sys.stdin))), ensure_ascii=True))
    except Exception as error:  # noqa: BLE001 -- no input/request dumps at live-test boundary
        print(json.dumps({"failed": type(error).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

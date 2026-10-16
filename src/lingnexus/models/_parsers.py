"""Native stream reducers. Normalization never replaces continuation objects."""

import json
from copy import deepcopy
from typing import Any

from ._sse import ProtocolError, json_object
from .types import APIFamily, ModelEvent, ProviderContinuation, ToolCall, Usage


def obj(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError()
    return value


def seq(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ProtocolError()
    return value


def string(value: Any, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ProtocolError()
    return value


def index(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ProtocolError()
    return value


def token(value: Any) -> int | None:
    return None if value is None else index(value)


def usage_from(raw: dict[str, Any], family: APIFamily) -> Usage:
    chat = family == APIFamily.OPENAI_CHAT_COMPLETIONS
    output_details = obj(
        raw.get("completion_tokens_details" if chat else "output_tokens_details") or {}
    )
    input_details = obj(raw.get("prompt_tokens_details" if chat else "input_tokens_details") or {})
    return Usage(
        input_tokens=token(raw.get("prompt_tokens" if chat else "input_tokens")),
        output_tokens=token(raw.get("completion_tokens" if chat else "output_tokens")),
        total_tokens=token(raw.get("total_tokens")),
        reasoning_tokens=token(output_details.get("reasoning_tokens")),
        cached_input_tokens=token(
            raw.get("cache_read_input_tokens", input_details.get("cached_tokens"))
        ),
        cache_creation_input_tokens=token(
            raw.get("cache_creation_input_tokens", input_details.get("cache_write_tokens"))
        ),
        raw=deepcopy(raw),
    )


class Parser:
    def __init__(self, family: APIFamily) -> None:
        self.family = family
        self.response: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []
        self.usage_raw: dict[str, Any] = {}
        self.usage = Usage()
        self.finish_reason: str | None = None
        self.finished = False

    def snapshot(self) -> ProviderContinuation:
        return ProviderContinuation(
            self.family,
            self.response.get("id"),
            deepcopy(self.response),
            tuple(deepcopy(self.events)),
        )

    def update_usage(self, value: Any) -> list[ModelEvent]:
        if value is None:
            return []
        self.usage_raw.update(deepcopy(obj(value)))
        self.usage = usage_from(self.usage_raw, self.family)
        return [ModelEvent("usage", usage=self.usage)]

    def complete(self) -> ModelEvent:
        self.finished = True
        return ModelEvent(
            "complete",
            usage=self.usage,
            finish_reason=self.finish_reason,
            continuation=self.snapshot(),
        )

    def tool(
        self, native: dict[str, Any], *, anthropic: bool = False, arguments_json: str | None = None
    ) -> ModelEvent:
        call_id = string(
            native.get("id") if anthropic else native.get("call_id", native.get("id")),
            nonempty=True,
        )
        function = native if anthropic or "function" not in native else obj(native["function"])
        name = string(function.get("name"), nonempty=True)
        if anthropic:
            arguments = deepcopy(obj(native.get("input")))
            raw = (
                arguments_json
                if arguments_json is not None
                else json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            )
        else:
            raw = string(function.get("arguments"))
            arguments = json_object(raw, "invalid_tool_arguments")
        item_id = native.get("id") if "call_id" in native else None
        return ModelEvent(
            "tool_call",
            tool_call=ToolCall(call_id, name, raw, arguments, item_id),
            tool_call_id=call_id,
            tool_name=name,
            item_id=item_id,
        )

    def record(self, data: dict[str, Any]) -> None:
        self.events.append(deepcopy(data))
        if data.get("type") == "error" or data.get("error") is not None:
            raise ProtocolError("provider_error")


class AnthropicParser(Parser):
    def __init__(self) -> None:
        super().__init__(APIFamily.ANTHROPIC_MESSAGES)
        self.blocks: dict[int, dict[str, Any]] = {}
        self.open_blocks: set[int] = set()
        self.arguments: dict[int, list[str]] = {}

    def feed(self, data: dict[str, Any]) -> list[ModelEvent]:
        self.record(data)
        kind = string(data.get("type"))
        result: list[ModelEvent] = []
        if kind == "message_start":
            if self.response:
                raise ProtocolError()
            self.response = deepcopy(obj(data.get("message")))
            string(self.response.get("id"), nonempty=True)
            if seq(self.response.get("content")):
                raise ProtocolError()
            result += self.update_usage(self.response.get("usage"))
        elif kind == "content_block_start":
            i = index(data.get("index"))
            if not self.response or i in self.blocks:
                raise ProtocolError()
            block = deepcopy(obj(data.get("content_block")))
            self.blocks[i] = block
            self.open_blocks.add(i)
            self.response["content"] = [self.blocks[n] for n in sorted(self.blocks)]
            if block.get("type") in ("text", "thinking"):
                field = block["type"]
                text = string(block.get(field, ""))
                if text:
                    result.append(
                        ModelEvent(
                            "text_delta" if field == "text" else "thinking_delta",
                            text=text,
                            index=i,
                        )
                    )
        elif kind == "content_block_delta":
            i = index(data.get("index"))
            if i not in self.open_blocks:
                raise ProtocolError()
            block = self.blocks[i]
            delta = obj(data.get("delta"))
            delta_type = string(delta.get("type"))
            if delta_type in ("text_delta", "thinking_delta", "signature_delta"):
                field = delta_type.removesuffix("_delta")
                expected = "thinking" if field == "signature" else field
                if block.get("type") != expected:
                    raise ProtocolError()
                text = string(delta.get(field))
                block[field] = string(block.get(field, "")) + text
                if field != "signature" and text:
                    result.append(
                        ModelEvent(
                            "text_delta" if field == "text" else "thinking_delta",
                            text=text,
                            index=i,
                        )
                    )
            elif delta_type == "input_json_delta":
                if block.get("type") not in ("tool_use", "server_tool_use"):
                    raise ProtocolError()
                fragment = string(delta.get("partial_json"))
                self.arguments.setdefault(i, []).append(fragment)
                if block["type"] == "tool_use":
                    result.append(
                        ModelEvent(
                            "tool_call_delta",
                            index=i,
                            tool_call_id=string(block.get("id"), nonempty=True),
                            tool_name=string(block.get("name"), nonempty=True),
                            arguments_delta=fragment,
                        )
                    )
            elif delta_type == "citations_delta":
                seq(block.setdefault("citations", [])).append(deepcopy(obj(delta.get("citation"))))
        elif kind == "content_block_stop":
            i = index(data.get("index"))
            if i not in self.open_blocks:
                raise ProtocolError()
            block = self.blocks[i]
            if i in self.arguments:
                block["input"] = json_object("".join(self.arguments[i]), "invalid_tool_arguments")
            self.open_blocks.remove(i)
            if block.get("type") == "tool_use":
                arguments_json = "".join(self.arguments[i]) if i in self.arguments else None
                result.append(self.tool(block, anthropic=True, arguments_json=arguments_json))
        elif kind == "message_delta":
            if not self.response:
                raise ProtocolError()
            self.response.update(deepcopy(obj(data.get("delta"))))
            result += self.update_usage(data.get("usage"))
            self.response["usage"] = deepcopy(self.usage_raw)
            self.finish_reason = self.response.get("stop_reason")
        elif kind == "message_stop":
            if not self.response or self.open_blocks or not self.finish_reason:
                raise ProtocolError()
            result.append(self.complete())
        return result


class ChatParser(Parser):
    def __init__(self) -> None:
        super().__init__(APIFamily.OPENAI_CHAT_COMPLETIONS)
        self.message: dict[str, Any] = {"role": "assistant", "content": None}
        self.tools: dict[int, dict[str, Any]] = {}

    def feed(self, data: dict[str, Any]) -> list[ModelEvent]:
        self.record(data)
        result = self.update_usage(data.get("usage"))
        if "id" in data:
            response_id = string(data["id"], nonempty=True)
            if self.response.get("id", response_id) != response_id:
                raise ProtocolError(reason="response_id_changed")
        self.response.update({k: deepcopy(v) for k, v in data.items() if k != "choices"})
        choices = seq(data.get("choices"))
        for value in choices:
            choice = obj(value)
            if index(choice.get("index")) != 0 or self.finish_reason is not None:
                raise ProtocolError()
            delta = obj(choice.get("delta"))
            for key, value in delta.items():
                if key == "tool_calls":
                    continue
                if key in ("content", "refusal", "reasoning_content"):
                    if value is not None:
                        text = string(value)
                        self.message[key] = (self.message.get(key) or "") + text
                        if text:
                            result.append(
                                ModelEvent(
                                    "thinking_delta"
                                    if key == "reasoning_content"
                                    else "text_delta",
                                    text=text,
                                    index=0,
                                )
                            )
                else:
                    # Other native extensions stay verbatim in events. Only the
                    # explicitly returned reasoning_content text is displayed.
                    self.message[key] = deepcopy(value)
            for raw_tool in seq(delta.get("tool_calls", [])):
                part = obj(raw_tool)
                i = index(part.get("index"))
                native = self.tools.setdefault(i, {"function": {"name": "", "arguments": ""}})
                for key, value in part.items():
                    if key not in ("index", "function"):
                        if key == "id" and native.get("id", value) != value:
                            raise ProtocolError()
                        native[key] = deepcopy(value)
                function = obj(part.get("function", {}))
                for key, value in function.items():
                    if key in ("name", "arguments"):
                        native["function"][key] += string(value)
                    else:
                        native["function"][key] = deepcopy(value)
                if "arguments" in function:
                    result.append(
                        ModelEvent(
                            "tool_call_delta",
                            index=i,
                            tool_call_id=string(native.get("id"), nonempty=True),
                            tool_name=native["function"]["name"],
                            arguments_delta=function["arguments"],
                        )
                    )
            if self.tools:
                self.message["tool_calls"] = [self.tools[i] for i in sorted(self.tools)]
            reason = choice.get("finish_reason")
            if reason is not None:
                self.finish_reason = string(reason, nonempty=True)
                for native in self.tools.values():
                    if native.get("type") != "function":
                        raise ProtocolError()
                    result.append(self.tool(native))
            self.response["choices"] = [{**deepcopy(choice), "message": self.message}]
            self.response["choices"][0].pop("delta", None)
        return result

    def done(self) -> list[ModelEvent]:
        if not self.finish_reason or not self.response.get("id"):
            raise ProtocolError()
        return [self.complete()]


class ResponsesParser(Parser):
    def __init__(self) -> None:
        super().__init__(APIFamily.OPENAI_RESPONSES)
        self.items: dict[int, dict[str, Any]] = {}
        self.emitted_tools: set[str] = set()
        self.streamed_arguments: set[int] = set()
        self.finalized_arguments: dict[int, str] = {}

    def check_arguments(self, i: int, arguments: Any) -> None:
        if i in self.streamed_arguments and self.items[i].get("arguments") != arguments:
            raise ProtocolError()
        if i in self.finalized_arguments and self.finalized_arguments[i] != arguments:
            raise ProtocolError()

    def check_tool_item(self, i: int, item: dict[str, Any]) -> None:
        previous = self.items.get(i)
        if previous is not None and any(
            previous.get(k) != item.get(k) for k in ("id", "type", "call_id", "name")
        ):
            raise ProtocolError()
        self.check_arguments(i, item.get("arguments"))

    def emit_tool(self, item: dict[str, Any]) -> list[ModelEvent]:
        item_id = string(item.get("id"), nonempty=True)
        string(item.get("call_id"), nonempty=True)
        if item.get("status") not in (None, "completed"):
            return []
        if item_id in self.emitted_tools:
            return []
        event = self.tool(item)
        self.emitted_tools.add(item_id)
        return [event]

    def feed(self, data: dict[str, Any]) -> list[ModelEvent]:
        self.record(data)
        kind = string(data.get("type"))
        result: list[ModelEvent] = []
        if kind in (
            "response.created",
            "response.in_progress",
            "response.completed",
            "response.failed",
            "response.incomplete",
        ):
            response = deepcopy(obj(data.get("response")))
            response_id = string(response.get("id"), nonempty=True)
            if self.response.get("id", response_id) != response_id:
                raise ProtocolError(reason="response_id_changed")
            self.response = response
            result += self.update_usage(response.get("usage"))
            if kind == "response.failed":
                raise ProtocolError("provider_error")
            if kind == "response.incomplete":
                raise ProtocolError("incomplete_response")
            if kind == "response.completed":
                if response.get("status") != "completed":
                    raise ProtocolError()
                output = seq(response.get("output"))
                for i, previous in self.items.items():
                    if previous.get("type") == "function_call":
                        if i >= len(output):
                            raise ProtocolError()
                        self.check_tool_item(i, obj(output[i]))
                for i, item in enumerate(output):
                    if obj(item).get("type") == "function_call":
                        if item.get("status") not in (None, "completed"):
                            raise ProtocolError()
                        self.check_tool_item(i, item)
                        result += self.emit_tool(item)
                self.finish_reason = "completed"
                result.append(self.complete())
        elif kind in ("response.output_item.added", "response.output_item.done"):
            i = index(data.get("output_index"))
            item = deepcopy(obj(data.get("item")))
            string(item.get("id"), nonempty=True)
            if i in self.items and self.items[i]["id"] != item["id"]:
                raise ProtocolError()
            if i in self.items and self.items[i].get("type") == "function_call":
                self.check_tool_item(i, item)
            if (
                item.get("type") == "function_call"
                and (
                    item.get("status") == "completed"
                    or (kind.endswith(".done") and item.get("status") is None)
                )
            ):
                # Keep the finalized value independent of later native item
                # updates, including tools that arrived without any deltas.
                self.finalized_arguments[i] = string(item.get("arguments"))
            self.items[i] = item
            self.response["output"] = [self.items[n] for n in sorted(self.items)]
            if kind.endswith(".done") and item.get("type") == "function_call":
                result += self.emit_tool(item)
        elif kind in (
            "response.output_text.delta",
            "response.refusal.delta",
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        ):
            text = string(data.get("delta"))
            i = index(data.get("output_index"))
            item = self.items.get(i)
            if item is None:
                raise ProtocolError()
            item_id = string(data.get("item_id", item["id"]), nonempty=True)
            if item_id != item["id"]:
                raise ProtocolError()
            if text:
                result.append(
                    ModelEvent(
                        "thinking_delta" if "reasoning" in kind else "text_delta",
                        text=text,
                        index=i,
                        item_id=item_id,
                    )
                )
        elif kind in (
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        ):
            i = index(data.get("output_index"))
            item = self.items.get(i)
            if (
                item is None
                or item.get("type") != "function_call"
                or item.get("id") != data.get("item_id")
            ):
                raise ProtocolError()
            if kind.endswith(".delta"):
                if i in self.finalized_arguments:
                    raise ProtocolError()
                fragment = string(data.get("delta"))
                self.streamed_arguments.add(i)
                item["arguments"] = string(item.get("arguments", "")) + fragment
                result.append(
                    ModelEvent(
                        "tool_call_delta",
                        index=i,
                        item_id=item["id"],
                        tool_call_id=string(item.get("call_id"), nonempty=True),
                        tool_name=string(item.get("name"), nonempty=True),
                        arguments_delta=fragment,
                    )
                )
            else:
                arguments = string(data.get("arguments"))
                self.check_arguments(i, arguments)
                self.finalized_arguments[i] = arguments
                item["arguments"] = arguments
                # Arguments done is not item done. Wait for the latter before
                # exposing a completed tool; it carries the authoritative item.
        return result

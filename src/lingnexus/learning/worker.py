"""Explicit single-job worker; no scheduler, fallback, or automatic paid retry.

run_once(agent_id=None) / drain_once returns a JSON-safe status dict. retry(job_id)
explicitly authorizes retry of a failed job. A process crash leaves running jobs
claimed for operator investigation; construction never steals such claims.
aclose closes only an internally owned client. Callers must cancel/await active
run_once tasks before closing the worker or its Application.
"""

import asyncio
from contextlib import aclosing

import httpx
from pydantic import ValidationError

from lingnexus.common.errors import NotFound, PermissionDenied, SourceInvalid
from lingnexus.common.types import SourceRef
from lingnexus.identity.policy import may_apply_automatically
from lingnexus.memory.maintenance import MemoryMaintenance
from lingnexus.models import ModelGateway, ModelProfile, RuntimeCredentials, StreamLimits
from lingnexus.models.gateway import ERROR_MESSAGES
from lingnexus.models.types import Usage

from .jobs import Jobs
from .prompt import build_payload, relevant_context
from .schema import ConsolidationResult, LearningError, parse, validate_knowledge

ALLOWED_SOURCE_KINDS = {"user", "assistant", "observation", "tool"}


def safe_usage(value: Usage) -> dict:
    # Never persist raw provider usage, continuation, reasoning, headers, or errors.
    return {
        name: getattr(value, name)
        for name in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "reasoning_tokens",
            "cached_input_tokens",
            "cache_creation_input_tokens",
        )
    } | {"cost_usd": str(value.cost_usd) if value.cost_usd is not None else None}


class ConsolidationWorker:
    def __init__(self, app, *, client: httpx.AsyncClient | None = None):
        self.app = app
        self._client = client if client is not None else httpx.AsyncClient()
        self._owns_client = client is None
        self._jobs = Jobs(app.database)
        self._gateway = ModelGateway(
            self._client,
            limits=StreamLimits(
                max_stream_bytes=1024 * 1024,
                max_event_bytes=128 * 1024,
                max_events=4096,
            ),
        )

    async def run_once(self, agent_id: str | None = None) -> dict:
        job = self._jobs.claim(agent_id)
        if job is None:
            return {"status": "idle"}
        job_id = job["id"]
        usage: dict = {}
        try:
            # No await in any transaction: services join the same synchronous
            # atomic unit, while the native stream runs entirely outside it.
            with self.app.database.transaction():
                try:
                    ref = SourceRef.model_validate_json(job["payload"])
                except ValidationError:
                    raise LearningError("invalid_job") from None
                source = self.app.history.validate_refs(job["agent_id"], [ref])[0]
                external_observation = source["scope"] == "external" and source["kind"] in {
                    "observation",
                    "tool",
                }
                if (source["scope"] != "real" and not external_observation) or source[
                    "kind"
                ] not in ALLOWED_SOURCE_KINDS:
                    return self._jobs.finish(job_id, "skipped", "source_not_eligible")
                try:
                    primary = self.app.profiles.selection()["primary"]
                    if primary is None:
                        raise PermissionDenied("Missing primary profile")
                    profile, credentials = self.app.profiles.resolve(primary)
                except (PermissionDenied, NotFound):
                    return self._jobs.finish(job_id, "blocked_credentials", "missing_credentials")
                context, context_refs = relevant_context(self.app, source)
                refs = list({(r.id, r.revision): r for r in [ref, *context_refs]}.values())
                self.app.history.validate_refs(job["agent_id"], refs, scope=source["scope"])
                base_revision = self.app.identity.current(job["agent_id"]).revision
                payload = build_payload(profile, source, context)
                self._jobs.begin_attempt(job_id, [r.model_dump() for r in refs])
            text = await self._generate(profile, credentials, payload, usage)
            # An upstream echo of the runtime key cannot become persistent memory.
            if credentials.api_key in text:
                raise LearningError("credential_in_response")
            parsed = parse(text)
            # Check decoded strings as well (JSON may escape a reflected key).
            if credentials.api_key in parsed.model_dump_json():
                raise LearningError("credential_in_response")
            return self._persist(
                job,
                refs,
                base_revision,
                source["kind"],
                parsed,
                usage,
                scope=source["scope"],
                memory_markers=[item["memory_ref"] for item in context],
            )
        except asyncio.CancelledError:
            self._jobs.finish(
                job_id, "failed", "interrupted", error_code="interrupted", usage=usage
            )
            raise
        except (SourceInvalid, NotFound):
            return self._jobs.finish(job_id, "stale", "source_invalid", usage=usage)
        except LearningError as exc:
            return self._jobs.finish(job_id, "failed", "failed", error_code=exc.code, usage=usage)
        except Exception:  # noqa: BLE001 - sanitize arbitrary transport/storage failures at boundary
            # This boundary must never reflect arbitrary exception text. A failure
            # to persist even the failure status propagates; it cannot become done.
            return self._jobs.finish(
                job_id, "failed", "failed", error_code="worker_error", usage=usage
            )

    drain_once = run_once

    def retry(self, job_id: str) -> dict:
        return self._jobs.retry(job_id)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _generate(
        self,
        profile: ModelProfile,
        credentials: RuntimeCredentials,
        payload: dict,
        usage: dict,
    ) -> str:
        parts: list[str] = []
        length = 0
        complete = False
        async with aclosing(
            self._gateway.stream(profile, payload, credentials=credentials)
        ) as stream:
            async for event in stream:
                if event.usage is not None:
                    usage.update(safe_usage(event.usage))
                if event.type == "error":
                    code = event.error.code if event.error else "model_error"
                    raise LearningError(code if code in ERROR_MESSAGES else "model_error")
                if event.type in {"tool_call", "tool_call_delta"}:
                    raise LearningError("unexpected_tool_call")
                if event.type == "text_delta" and event.text:
                    length += len(event.text)
                    if length > 32000:
                        raise LearningError("response_too_large")
                    parts.append(event.text)
                if event.type == "complete":
                    if event.finish_reason not in {"stop", "end_turn", "completed"}:
                        raise LearningError("incomplete_response")
                    complete = True
        if not complete:
            raise LearningError("incomplete_response")
        return "".join(parts)

    def _persist(
        self,
        job: dict,
        refs: list[SourceRef],
        base_revision: int,
        source_kind: str,
        result: ConsolidationResult,
        usage: dict,
        scope: str = "real",
        memory_markers: list[dict] | None = None,
    ) -> dict:
        with self.app.database.transaction():
            # Both corrections and visibility changes invalidate the whole result.
            # Context evidence participates too, and all attached refs came from
            # the input snapshot, never model-supplied identifiers.
            self.app.history.validate_refs(job["agent_id"], refs, scope=scope)
            MemoryMaintenance(self.app).validate_markers(job["agent_id"], memory_markers or [])
            if self.app.identity.current(job["agent_id"]).revision != base_revision:
                return self._jobs.finish(job["id"], "stale", "self_changed", usage=usage)
            pending = [
                p["text"]
                for p in self.app.identity.list_proposals(job["agent_id"])
                if p["status"] in {"awaiting_owner", "rejected"}
            ]
            validate_knowledge(result, pending)
            saved = {"episode": None, "knowledge": [], "proposals": []}
            # Provenance is retained explicitly even if the model forgets to
            # qualify a report. Raw sources provide no independently verified
            # success flag, including tool and successful assistant-run records.
            prefix = {
                "user": "用户报告（未经独立核实）：",
                "assistant": "助手陈述（未经独立核实）：",
                "observation": "观察记录（未经独立核实）：",
                "tool": "工具记录（未提供独立成功标记）：",
            }[source_kind]
            if result.event is not None:
                self.app.segments.source_event(refs[0], result.event.title)
                saved["episode"] = self.app.memory.save_episode(
                    job["agent_id"],
                    result.event.title,
                    prefix + result.event.summary,
                    refs,
                    request_id=f"learning:{job['id']}:event",
                    scope=scope,
                )
            for index, item in enumerate(result.knowledge):
                saved["knowledge"].append(
                    self.app.memory.knowledge.save(
                        job["agent_id"],
                        prefix + item.statement,
                        refs,
                        kind=item.kind,
                        request_id=f"learning:{job['id']}:knowledge:{index}",
                        scope=scope,
                    )
                )
            # Apply structured safe habits first so protected candidates share
            # the resulting revision rather than immediately becoming obsolete.
            ordered = sorted(
                enumerate(result.changes),
                key=lambda pair: (
                    not may_apply_automatically(
                        pair[1].kind,
                        pair[1].key,
                        pair[1].text,
                    )
                ),
            )
            for index, item in ordered:
                proposal = self.app.identity.propose(
                    job["agent_id"],
                    kind=item.kind,
                    key=item.key,
                    text=item.text,
                    sources=refs,
                    base_revision=self.app.identity.current(job["agent_id"]).revision,
                    request_id=f"learning:{job['id']}:change:{index}",
                )
                saved["proposals"].append(proposal["id"])
            outcome = "saved" if result.event or result.knowledge or result.changes else "no_value"
            # The job completion and every domain write are committed together.
            return self._jobs.finish(
                job["id"],
                "done" if outcome == "saved" else "skipped",
                outcome,
                usage=usage,
                result_refs=saved,
            )

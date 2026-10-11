# Durable Agent task runtime

The parent owns lifecycle, credentials, image acquisition, router registration and
foreground scheduling. All model tests use an external HTTP fixture, without live
model calls or cloud keys.

The foreground_busy callback must represent foreground chat only. The parent's
background coordinator may include tasks in its own busy predicate; passing that
combined predicate back to TaskRuntime would make a task wait on itself.

## Parent integration

~~~python
from lingnexus.tasks import TASK_TABLES, TaskRuntime
from lingnexus.api.tasks import router

tasks = TaskRuntime(
    services,
    client=shared_httpx_client,  # optional, remains caller-owned
    docker_image=owner_configured_image,  # optional, never model-selected
    node_image="node:24-bookworm-slim",  # trusted parent config; None disables Node execution
    foreground_busy=foreground_busy,  # synchronous () -> bool
    max_steps=40,
)
server.state.tasks = tasks
server.include_router(router)
await tasks.start()  # application startup
# ...
await tasks.aclose()  # close() is the same async method

task_id = await tasks.submit(
    {
        "agent_id": agent_id,
        "objective": "Fix the project, run its tests, and deliver the patch",
        "request_id": "owner-stable-request-id",
        "workspace": str(owner_selected_directory),  # optional
        "environment": "python",  # owner selection: python (default) or node
    }
)
status = tasks.get(task_id)
events = tasks.events(task_id, after=last_event_id, limit=200)
async for event in tasks.stream(task_id, after=last_event_id):
    ...
await tasks.pause(task_id)
await tasks.resume(task_id, additional_steps=10)
await tasks.cancel(task_id)
review = tasks.changes(task_id)
artifact_bytes = tasks.artifact(task_id, "relative/output.txt")
result = await tasks.apply(task_id, review_id=review["review_id"])
~~~

submit commits an ID then starts an independent asyncio worker. Repeated identical
agent/request input returns the same ID; changed input conflicts. Browser/stream
disconnect never owns the worker. get and list expose status without checkpoints.
stream replays durable events and ends at waiting_owner, paused, blocked, interrupted or terminal
status. The owner-authenticated router exposes POST /tasks, GET /tasks, GET /tasks/{id},
POST /tasks/{id}/pause|resume|cancel, GET /tasks/{id}/events?after=,
GET /tasks/{id}/stream?after=, GET /tasks/{id}/changes,
GET /tasks/{id}/artifacts/{relative-path}, and POST /tasks/{id}/apply.

Image configuration is the allowlist. Each task saves the constructor's image
reference; DockerExecutor inspects and pins its local ID at execution. The model
cannot select/pull images, configure grants, approve or apply changes. For Node,
the parent acquires the official node:24-bookworm-slim image (or supplies a different
trusted node_image reference). The constructor's python/node image map is the only
image selection authority. Submission/API environment accepts only python or node;
get/list return the selected environment. The selected image is saved in tasks;
environment is saved in the run checkpoint (old checkpoints default to python).
Python request fingerprints retain the pre-environment shape so old/default/explicit
python submissions replay identically. Node requests include their environment.
Existing tasks retain their saved image/profile; profile changes block continuation.

## Browser tools and owner approval

The parent starts BrowserService and assigns it to services.browser. Task tools are
browser_state (no args), browser_goto (url, origin_revision), browser_read
(origin_revision, page_version, optional max_chars/expected_text), browser_fill
(origin_revision, page_version, selector, value), and browser_request_click
(origin_revision, page_version, selector, optional action=click|submit|send|publish).
The task supplies request IDs; model arguments cannot grant origins, approve, launch
login, select a profile, evaluate JavaScript, or replace a browser request ID.

browser_state returns current grants and page_version. ActionInput and BrowserService
recheck grants and page versions. Prepared clicks persist a request/effect checkpoint
and leave the native pending call unconsumed; the task enters waiting_owner and
get/list expose approval_id and browser_action_id. The owner uses the separate
browser approval API, then calls task resume. Resume reconciles the same recorded
action, even at the model-step limit; further model work needs budget remaining or
an owner extension. Repeated resume before approval stays waiting_owner. Process
recovery, pause and resume retain the exact intent; no click is repeated or approved
by the runtime. Unknown browser effects block for reconciliation. Pausing while a
prepared result is being returned retains paused status plus approval metadata.

An approved click retains business_outcome=not_verified. Only a later browser_read
with nonempty expected_text matching the actual untruncated visible DOM supplies
verification_passed. State reads, successful click dispatch, and model prose cannot
replace this check. Coding and browser verification requirements both apply to
mixed tasks.

## Persistence and recovery

TASK_TABLES exports tasks, task_checkpoints, task_steps, task_events, task_reviews.
The tables respectively contain submission/status/budget/outcome, native run state,
tool/effect intents and results, cursor-addressed events, and reviewed application
fences. Native continuation is separate from stored state and never contains stored
RuntimeCredentials. Ordinary granted file calls use app.actions.execute; private
snapshot file calls and commands use task_steps effect IDs with purpose.

Continuation uses only ModelGateway complete.response: native Anthropic blocks
retain signatures/redacted content/tool IDs, Chat retains its assistant message
and tool IDs, and Responses retains output item IDs/encrypted reasoning with
store=false. Display deltas never reconstruct protocol state. Truncated responses,
errors, inconsistent calls and credential echoes cannot execute tools. Errors are
static; native responses have a 4 MiB cap and conservative context-budget checks.

start launches queued rows and marks stale running rows interrupted. The owner
resumes interrupted work. Pending calls commit before effects start; each effect
is fenced before execution. Known action results can be reconciled after a crash;
uncertain commands/snapshot effects block with effect_needs_review and never replay.
Interrupted/partial applications also require reconciliation. The parent calls
start once in the single application process owning these workers.

max_steps counts model attempts, including failed/interrupted requests; default 40.
Each native response permits at most 40 calls. Exhaustion persists paused/step_limit.
Pause/cancel interrupts model streams. Already-started synchronous tools/commands
drain within their bounded limits so actual results and cleanup flags persist before
pause/cancel returns. Foreground priority is checked between tools/model calls and
while waiting for the model lock; an already-started container runs to its bound.

Initial context is the owner's system instructions, the individual's name, and the
selected coding environment. Checkpoints pin the model profile; a profile change
blocks continuation rather than silently switching. Resume does not rebuild the
native conversation. Native IDs and previous effect records are retained.

## Workspace and verification

Owner-selected projects need independent readable AND writable grants. Grants are
rechecked on snapshot creation, model requests, workspace tools and apply. Copying
uses existing safe handle-based helpers and rejects included links, hardlinks,
reparse points, unsafe aliases and nested mounts. .git, .env*, venv, .venv,
node_modules, __pycache__ and .tmp are excluded. Limits are 1 MiB/file, 32 MiB total,
2,000 files and 10,000 scanned entries. Artifact validation uses the same caps.
Copies and original baselines live at database-parent/task-data/{id}; only its
workspace child is mounted. Overlapping project/task-data roots are denied. The
parent must back up/restore these artifacts along with SQL if task files must survive
backup/restore; SQL alone preserves protocol and effect state, not missing files.

run_command accepts argv and purpose=work|verify. DockerExecutor supplies UID/GID
65534, no network, read-only root, one writable snapshot bind, dropped capabilities,
no pull, bounded output/time/CPU/memory/PIDs, and confirmed cleanup. It has no writable
/tmp mount. Container argv prefixes env with TMPDIR/TMP/TEMP=/workspace/.tmp;
programs hard-coding /tmp receive their actual failure. This package adds no tmpfs.

Events retain actual status, exit_code, stdout/stderr, output_truncated, timed_out,
cleanup_confirmed and container_name. Artifact validation failures are separate
from container evidence. verification_passed requires purpose=verify, exit 0,
confirmed cleanup, untruncated output, no timeout and valid artifacts. Writes
invalidate earlier checks; a readback cannot override failed/stale tests. For
ordinary file work, checking expected final contents is verification. Completion
requires a final response, current verification and no unresolved failures;
uncertainty becomes needs_review. The marker proves the declared check passed;
objective correctness depends on the chosen checks.
Workspace writes require an actual successful run_command with purpose=verify
after the latest write; source readback alone cannot verify coding changes. Plain
file tasks without a project workspace may still verify their output by readback.
Accepted finishes are Chat stop/tool_calls, Messages end_turn/stop_sequence/tool_use,
and Responses completed; finish labels from another family are rejected.

Project output is staged by default. changes returns a review ID, fingerprints,
unified text diffs and artifact names. apply requires that ID, unchanged original
fingerprints, unchanged reviewed artifacts and current grants. Text create/modify
uses app.actions and durable effect IDs. Binary files and deletions remain downloadable
artifacts/diffs for manual application because existing helpers have no safe deletion
or binary replacement effect.

Controlled owner-approved online dependency installation remains a parent integration;
this package does not acquire images or enable a container network. The parent supplies
locally available Python/Node images; current command execution is offline in the
authorized snapshot. Native TypeScript type stripping in Node 24 is supported; the
image does not imply that a TypeScript compiler or arbitrary npm packages are installed.

## Tests

~~~powershell
.venv/Scripts/python.exe -m pytest tests/test_smoke.py -q
~~~

The 2026-10-10 acceptance layering retired this module's historical suite (live
Docker, Chrome, and long-term-memory cases included; recoverable from git history).
The smoke layer keeps the real-SQLite write/verify completion path. Write ad hoc
checks when a change needs deeper coverage.

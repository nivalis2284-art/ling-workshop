# Owner-approved task dependencies

`DependencyService(app, task_runtime)` uses only the runtime's public `get()` and
`workspace()` methods. It never changes a task row, resumes a task, writes back
to the original project, or passes profile credentials to Docker. The task must
be **paused or blocked**. Original-project read and write grants are rechecked
both at approval and in the installation job.

## Exports and durable data

- `DependencyService`: trusted owner `prepare()`, `approve()`, `reject()`,
  `get()`, `list()`, `task_guard()`, `ensure_task_ready()`, and `aclose()`.
- `DependencyCandidate`: strict, bounded model-safe request data.
- `task_environment()`: fresh fixed runner environment mapping with HOME,
  PYTHONPATH, TMPDIR, TMP and TEMP; it never reads the host environment.
- `DependencyService.propose(manager=..., packages=...)`: validates candidate
  data only, with no persistence, image inspection, installation or authority.
- `DEPENDENCY_TABLES = ("dependency_plans",)`: export for parent backup/export
  integration. Migration `023_dependencies.sql` is discovered by `Database`.

`prepare(task_id, manager="pip"|"npm", packages=[...], request_id=...)` returns a
pending record containing `id`, `plan`, and `intent_hash`. There must be 1–32
distinct names, each at most 200 characters, with exact versions: e.g.
`requests==2.32.3`, `lodash@4.17.21`, or `@types/node@22.10.0`. Ranges, flags,
URLs, paths, pip extras, and npm tags are rejected. Request IDs are bounded to
200 characters. Identical requests return the existing record; reusing an ID
with different parameters conflicts.

The immutable plan contains packages, destination, environment, fixed argv,
limits, permission revision/grants, task checkpoint metadata, snapshot content
hash and directory identity, and — for the container tier — a locally resolved
`sha256:` image ID. Image inspection never pulls or enables networking. A
task/image/checkpoint/grant or snapshot change requires a new plan and approval.

## Verification follows the sandbox tier

The plan records `execution_level`, so preparation and verification no longer
require Docker. On the **container** tier the install runs with bridge
networking in the approved local image, and a separate read-only, offline
container re-reads the target metadata. On the **local** tier the install runs
on the host into the task's private snapshot (`<snapshot>/.deps/python` for pip,
`<snapshot>` for npm) through the restricted local sandbox, and verification
re-runs the same metadata reader with the host interpreter; the plan stores no
image ID. Both tiers verify only exact installed versions read from
`importlib.metadata`; neither imports the packages. The `GET /sandbox` report
states which verification path is active.

Only `await approve(plan_id, intent_hash=...)` grants bridge networking (container
tier only; the local tier has no container network). It
commits `running` before scheduling bounded Docker work. An HTTP request may
await completion; cancellation of its awaiter does not cancel the installation.
The service retains its job and `aclose()` drains it. Process death is different:
on service construction, stale `running` records become `unknown`. No startup
replay occurs. Duplicate decisions never rerun effects. `unknown` fences all
new installs and task resume/application until manual owner reconciliation;
there is deliberately no API that retries or clears an uncertain effect.
Statuses are `pending`, `running`, `success`, `failed`, `unknown`. Owner rejection
is `failed` with `owner_rejected`. A confirmed failure can receive a separately
prepared and approved new request; it is never automatically retried.

## Parent integration required

The composition root, task module, action catalog and normal task runner are
outside this patch. The shared parent runtime checks `app.dependencies` in
resume/apply, sets task HOME/PYTHONPATH values, and excludes generated dependency
directories from scanning/writeback. The parent API must assign the **same
service** to both `app.dependencies` and `server.state.dependencies`:

```python
from lingnexus.dependencies import DependencyService
from lingnexus.api.dependencies import router

app.dependencies = DependencyService(app, task_runtime)
server.state.dependencies = app.dependencies
server.include_router(router)
# Parent shutdown: await server.state.dependencies.aclose() before task/app close.
```

The parent's existing resume/apply `ensure_task_ready()` checks reject active
and unknown installs. For operations that can suspend between checking and
mutating a snapshot, wrap the operation in the service's atomic guard:

```python
async with dependencies.task_guard(task_id):
    await task_runtime.resume(task_id)
```

Use one parent event loop/service lifecycle per database, as for `TaskRuntime`.
Do not mutate task snapshots concurrently from another process. Ordinary
`ensure_task_ready()` is a status check; `task_guard()` supplies the atomic
check-and-operation boundary. Tasks stay paused/blocked after installation,
and OWNER chooses when to resume. The service revalidates the stopped task
before executing but cannot intercept callers that bypass parent integration.

The router requires the existing OWNER bearer token and allowed origin:

| Method | Route | Purpose |
| --- | --- | --- |
| POST | `/tasks/{task_id}/dependencies/plan` | Prepare immutable intent |
| GET | `/tasks/{task_id}/dependencies` | List durable records |
| GET | `/tasks/{task_id}/dependencies/{plan_id}` | Plan and status |
| POST | `/tasks/{task_id}/dependencies/{plan_id}/decision` | Exact hash approve/reject |

The plan body contains only `manager`, `packages`, and `request_id`. The decision
body contains `decision` (`approve`/`reject`) and `intent_hash`. No owner methods
belong in model tool schemas. Model tools can forward only candidate data to an
owner-facing review flow; there is no model network/argv/environment option.

## Runner and filesystem contract

Docker keeps the existing non-root UID/GID `65534:65534`, read-only container
root, capability/resource restrictions and one snapshot mount. Only this
approved install passes `writable=True, network_mode="bridge"`; all existing
task commands retain constructor-default `network_mode="none"`. The local
trusted image needs `/usr/bin/env` and `/usr/local/bin/python` with pip, or
`/usr/local/bin/npm` and Node. Docker must use the trusted local Linux engine.

The fixed executable is `/usr/bin/env -i` with a fixed PATH,
`HOME=/workspace/.home`, and all temporary paths under `/workspace/.tmp`. No
host environment or API keys are forwarded to the container command. Private
directories use mode 0700. On native POSIX they must belong to UID/GID 65534;
a privileged parent can provision ownership of these task-owned directories,
otherwise trusted deployment must provision them first. Windows Docker bind
permissions follow Docker Desktop/NTFS semantics.

Pip uses `python -I -m pip install --target /workspace/.deps/python` with a
fixed PyPI index, config disabled, no cache/input/bytecode compilation, and
wheels only to prevent source build hooks. **Installation does not configure
the task runner:** parent runner commands must explicitly add
`PYTHONPATH=/workspace/.deps/python` through trusted env argv (and use a
compatible interpreter). Python `-I` ignores PYTHONPATH, so do not use that
flag on a future command that relies on this path.

Npm uses `install --ignore-scripts --no-audit --no-fund --prefix /workspace`
plus fixed registry, disabled lockfile use and disabled bin symlinks. Npm
uses user config `/dev/null` and a service-created empty global config at
`/workspace/.home/.npm-globalrc`; nonempty global configuration fails closed.
Packages are available through the project's `node_modules`; no NODE_PATH is needed.
Projects with `.npmrc`, shrinkwrap, workspaces, overrides or bundled
dependencies fail closed. Existing direct manifest dependencies must be exact
versions included in the approved package list. Lifecycle scripts remain
disabled. Installed transitive versions are registry-resolved; the intent pins
the requested direct versions, not a complete transitive lock.

Parent snapshot review/apply and bounded artifact scanning must treat `.deps`
and `.home` as generated install data, alongside the already excluded
`node_modules` and `.tmp`. They must never be applied back to the original
project. The parent has supplied these task-runner/review changes separately.

Checks mock only Docker's external subprocess boundary; they never run pip/npm
or install packages. The historical opt-in live smoke (already-local Python
image, synthetic filesystem/environment checks) was retired with the 2026-10-10
acceptance layering; re-create it ad hoc when the install network boundary
changes. The adapter never pulls images, opens network connections, or
downloads packages.

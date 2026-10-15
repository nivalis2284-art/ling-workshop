# Authorized workspace tools

These synchronous adapters enforce configuration supplied by the parent application.
They do not interpret model requests as authorization, expand grants, approve tasks,
persist effect intents, or automatically retry execution. The parent owns those steps,
revocation, cancellation policy and recovery after application termination.

## Public interfaces

Import from lingnexus.tools:

~~~python
from pathlib import Path
from lingnexus.tools import (
    FilePermissions, WorkspaceTools, DockerExecutor, ExecutionLimits,
)

# All paths must be caller-authorized, existing test directories.
worktree = Path("approved-worktree").resolve()
files = WorkspaceTools(
    worktree,
    FilePermissions(
        readable_roots=(worktree / "inputs", worktree / "outputs"),
        writable_roots=(worktree / "outputs",),
    ),
    max_read_bytes=1_048_576,
    max_write_bytes=1_048_576,
)
text = files.read_text("inputs/example.txt")
files.create_text("outputs/new.txt", text)  # Must not already exist.
files.write_text("outputs/new.txt", "updated")  # Must already exist.

# Construction is not an approval. The parent chooses and acquires the image.
runner = DockerExecutor(worktree, image="parent-approved-local-image:tag",
                        writable=True, limits=ExecutionLimits(timeout_seconds=30))
result = runner.execute(["python", "-c", "print('synthetic example')"])
~~~

read_bytes, create_bytes, and write_bytes are also available. Text is UTF-8.
Read and write grants are independent, immutable tuples of existing absolute
directories. An empty grant set denies all access. Relative paths use the configured
workspace; absolute paths are checked against the same component-aware grants.
Parents are never implicitly created. File limits count bytes; excessive reads raise
FileLimitExceeded, and excessive writes are rejected before opening the file.
Permission violations raise FileAccessDenied (a PermissionError); normal missing
file and exclusive-create errors retain their standard Python exception types.

## Execution sandbox port

`ExecutionSandbox` is a two-method interface (`level`, `execute(argv)`) with two
interchangeable implementations:

~~~python
from lingnexus.tools import create_executor, probe_engine, describe, LocalLimits

engine = probe_engine()            # {"available": bool, "name": "docker"|"podman"|None}
runner = create_executor("local", worktree, writable=True,
                         local_limits=LocalLimits(timeout_seconds=30))
result = runner.execute(["python", "-c", "print('synthetic example')"])
~~~

`RestrictedLocalSandbox` is the default tier. It runs the argument list with no
shell, `cwd` set to the worktree, and a minimal child environment whose TMP/TEMP/
HOME/PYTHONPATH live inside the worktree (host secrets are not forwarded). Where
the host can enforce them it also bounds process count, memory and CPU time and
kills the whole process tree on exit. On Windows this uses a Job Object created
through `ctypes` (`_jobobject.py`); the child is created suspended, assigned to
the job, then resumed so no descendant escapes. On POSIX the child runs in its
own session and a timeout kills the process group. `LocalLimits` mirrors
`ExecutionLimits` plus a `cpu_seconds` budget.

`ContainerSandbox` wraps `DockerExecutor` and prepends the fixed container
environment (`TMPDIR/TMP/TEMP=/workspace/.tmp`, `HOME=/workspace/.home`,
`PYTHONPATH=/workspace/.deps/python`).

`describe(level, job_limits=...)` returns the honest `protections`/`limitations`
lists. The local tier never claims filesystem or network isolation; when the host
cannot create a job object it states that process/memory/CPU bounds are
unavailable. `run_process` gained optional `cwd`, `env`, `job_limits` and
`new_session` arguments; existing callers are unchanged.

Paths reject parent traversal, sibling-prefix confusion, UNC/device/drive-relative
syntax, alternate data streams, Windows reserved names and trailing dots/spaces.
All symlinks, junctions, reparse points, hardlinked files and non-regular files are
denied, including links that stay inside a grant. On Windows, ancestor and file
handles deny delete sharing for the duration of each operation, preventing path
replacement while using that handle. POSIX uses directory descriptors and
O_NOFOLLOW. Existing-file writes use a same-directory temporary file, flush/fsync,
version checks and atomic replacement. An optional `expected_sha256` binds the
replacement to the version previously read. `edit_file` supplies this precondition
automatically; a conflicting change is preserved. Exclusive create never overwrites.
These are file-level guarantees, not a multi-file transaction or a cross-process
compare-and-swap: an external writer can still race the final check and rename.
The caller records and reconciles uncertain effects before retrying.

The parent exposes noninteractive command sessions through `run_command`,
`command_status` and `command_cancel`. Each session belongs to one agent/run,
persists bounded output with a cursor, and records exit, timeout and process cleanup.
Startup marks previously running sessions unknown without replaying a saved PID.
The conservative preset keeps a 30-second normal deadline; coding uses 120 seconds.
Explicit `long_running=true` permits up to 900 seconds, with two concurrent sessions
per run. Cancellation is confirmed only after the executor confirms process cleanup.

## Docker contract

~~~python
DockerExecutor(worktree, *, image, writable=False, limits=None, docker_binary="docker")
~~~

The worktree must be an absolute existing directory. execute(argv) accepts a
nonempty list or tuple of literal arguments; no host shell interprets them. Only
the parent should construct adapters or change their configuration. Its Docker
context/environment and CLI executable must be trusted and target the local Linux
daemon.

Every execution inspects the local image, rejects non-Linux images and declared
image volumes, and pins the inspected image ID for creation with --pull never.
It binds exactly the provided worktree to /workspace, with recursive submounts
disabled; the binding defaults to read-only. Writable worktrees require the explicit
constructor flag. There are no extra host mounts, socket mounts, published ports,
host environment injection, or automatic image acquisition. A bounded tree scan
rejects links, special files, nested mounts and engine socket names before Docker
is invoked (100,000 entries maximum).

Fixed container settings are UID/GID 65534:65534, read-only root filesystem,
all capabilities dropped, no-new-privileges=true, networking none, no image
healthcheck, no restart, and logging driver none. Docker's default seccomp profile
is retained. The image entrypoint is overridden by argv[0].

ExecutionLimits defaults:

| Field | Default |
| --- | --- |
| timeout_seconds | 30 seconds, covering create and attached execution |
| control_timeout_seconds | 5 seconds per image inspection / cleanup command |
| memory_bytes | 256 MiB, with memory-plus-swap set to the same bound |
| cpus | 1 CPU |
| pids_limit | 64 |
| max_output_bytes | 65,536 bytes combined stdout/stderr |

Output pipes are drained concurrently into a shared byte budget. Excess output is
discarded and output_truncated is true; it is never accumulated in an unbounded
communicate() buffer or temporary file. Each control response has a separate
16 KiB cap. Returned text decodes UTF-8 with replacement, so re-encoding malformed
UTF-8 can exceed the original captured byte count. No total ordering of stdout
and stderr is claimed.

ExecutionResult exposes status, exit_code, stdout, stderr, output_truncated,
timed_out, container_name, cleanup_confirmed, and error. Statuses are success,
failed, timeout, unknown. Container state inspection supplies the actual exit code,
including 125/126/127; the CLI return code is never substituted. Missing or malformed
state cannot become success. A timeout kills the CLI and requests docker kill on
the unique container name, then inspects and force-removes it. Cleanup has its own
bounded commands and can extend wall time beyond the execution limit. Unconfirmed
removal overrides the status to unknown, retaining the known exit code and timeout
flag. The returned name supports parent reconciliation. A daemon outage can prevent
cleanup; success is never invented.

## Verification and limits

The smoke layer covers workspace tools with synthetic pytest temporary
directories. The historical Docker contract/smoke tests (subprocess doubles,
junction/symlink checks, live-image cases) were retired with the 2026-10-10
acceptance layering and are recoverable from git history. No check accesses
user files.

When a Docker change needs deeper checks, re-create them ad hoc against an
explicitly acquired, local Linux Python image with /bin/sh and ordinary core
utilities and no declared volumes. The parent authorizes acquisition
separately. The adapter never pulls images. The verified acquisition and ID
pinning procedure is:

~~~powershell
docker pull docker.io/library/python:3.12-slim
docker image inspect docker.io/library/python:3.12-slim --format '{{json .Id}} {{json .RepoDigests}}'
$env:LINGNEXUS_DOCKER_SMOKE_IMAGE = docker image inspect docker.io/library/python:3.12-slim --format '{{.Id}}'
~~~

Record the resolved ID and repository digest. A re-created fixture should
resolve the configured local reference to its immutable ID, and every
execution should use that ID:

~~~powershell
& .venv/Scripts/python.exe -m pytest tests/ -q -rs
~~~

A re-created fixture should bind only its generated temporary worktree,
provision permissions for UID 65534 on that fixture alone, keep repositories,
user documents, API credentials and the Docker socket out of containers, and
verify a synthetic host-only environment marker absent inside the container.

### Recorded real verification: 2026-10-08

Image acquisition was explicitly authorized and performed using the official
docker.io/library/python:3.12-slim image. The unrelated pre-existing application
image was neither used nor deleted. The acquired Python image remains available.

- Resolved image ID: sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
- Repository digest: python@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
- Platform: linux/amd64. Docker's image store reported the same digest as its ID.
- Engine: 29.6.2, Docker Desktop 4.85.0, context desktop-linux.
- Kernel: 6.18.33.2-microsoft-standard-WSL2; cgroup v2, cgroupfs driver.
- Docker info returned no daemon warnings.

Final scoped verification with this image enabled: 84 passed, 2 skipped in 9.95s.
The two skips are Windows file/directory symlink tests lacking creation privilege;
the real junction tests and all three Docker smoke tests passed. Scoped Ruff
passed, and Pyright reported zero errors and warnings. No other project suite
is implied by these results.

The three live smoke tests passed, covering:

| Check | Observed evidence |
| --- | --- |
| Writable artifact / actual failure code | Generated file readable on host; container exit 23 preserved as failed |
| Nonroot identity | UID and GID 65534 |
| Privilege restrictions | CapEff=0000000000000000, NoNewPrivs=1, Seccomp=2 |
| Networking | HostConfig.NetworkMode=none; only lo; TEST-NET-1 connection returned ENETUNREACH (101) |
| Mounts | One bind, /workspace; no engine sockets; root write failed with EROFS |
| Default workspace mode | Workspace write failed with EROFS |
| Memory / swap | HostConfig matched; memory.max=134217728, memory.swap.max=0 |
| CPU | HostConfig.NanoCpus=500000000; cpu.max=50000 100000 |
| Process count | HostConfig.PidsLimit=32; pids.max=32 |
| Output bound | 200000 emitted bytes, 257 captured bytes, output_truncated=true |
| Timeout | Started Python sleep interrupted; status timeout, exit 137 |
| Cleanup | Removal confirmed; subsequent successful container-list queries found each test name absent |

Live testing exposed three adapter defects, each reproduced before its fix:
Windows DirEntry.stat's zero link count incorrectly rejected ordinary files;
Docker's absent Config.Volumes key caused a Go-template lookup failure; and the
attached Docker CLI's relayed nonzero exit was incorrectly classified as unknown
despite matching inspected container state. Fresh lstat, an optional-key lookup,
and matching the CLI status against the inspected exit fixed these respectively.
Malformed or inconsistent state and unconfirmed cleanup still remain unknown.

Remaining boundaries: the parent must exclusively own the worktree during the
scan and execution; the scan is not an atomic snapshot of a concurrently modified
host tree. POSIX ancestor renames and malicious concurrent hardlink creation are
not an OS sandbox. Bind writes have no disk quota. The resource checks verify
live Docker configuration and kernel cgroup values, not OOM, fork-limit, or CPU
saturation stress behavior. The live probe currently requires Linux cgroup v2;
cgroup v1 engines are not covered. Windows symlink behavior without the required
privilege and the POSIX host file-opening implementation remain unverified. A
hard application crash or daemon outage can leave a container; durable intent and
startup reconciliation belong to the parent. Docker is not a VM-grade boundary
against kernel exploits.

## Official Docker references read before implementation

- [Running containers and resource constraints](https://docs.docker.com/engine/containers/run/)
- [Bind mounts and recursive submounts](https://docs.docker.com/engine/storage/bind-mounts/)
- [Create flags and pull policy](https://docs.docker.com/reference/cli/docker/container/create/)
- [Start and attach](https://docs.docker.com/reference/cli/docker/container/start/)
- [Inspect](https://docs.docker.com/reference/cli/docker/container/inspect/)
- [Container wait and exit codes](https://docs.docker.com/reference/cli/docker/container/wait/)
- [Kill and SIGKILL](https://docs.docker.com/reference/cli/docker/container/kill/)

"""Real Docker/pip with generated wheels; only the package source is substituted."""

import asyncio
import base64
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from test_dependencies import approve, prepare
from test_docker_execution import assert_smoke_container_removed
from test_docker_execution import smoke_image as _local_smoke_image

from lingnexus.common.errors import Conflict
from lingnexus.common.types import new_id
from lingnexus.dependencies import DependencyService
from lingnexus.dependencies.policy import DependencyCandidate, verification_command
from lingnexus.models import ModelProfile
from lingnexus.tasks import TaskRuntime
from lingnexus.tools import DockerExecutor

smoke_image = _local_smoke_image


@pytest.mark.parametrize("found", [[], ["1.0"], ["2.0"], ["1.0", "2.0"]])
def test_verifier_reads_only_explicit_target_without_executing_package_code(tmp_path, found):
    target = tmp_path / "target"
    target.mkdir()
    for index, version in enumerate(found):
        info = target / f"upgrade_fixture-{index}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Name: Upgrade.Fixture\nVersion: {version}\n")
    # Matching metadata in the current directory must not satisfy a missing target.
    info = tmp_path / "upgrade_fixture-2.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Name: upgrade-fixture\nVersion: 2.0\n")
    hostile = "raise AssertionError('snapshot code must not run')\n"
    for folder in (tmp_path, target):
        for name in ("sitecustomize.py", "importlib.py", "json.py", "upgrade_fixture.py"):
            (folder / name).write_text(hostile)
        (folder / "poison.pth").write_text("import upgrade_fixture\n")
    candidate = DependencyCandidate(manager="pip", packages=["upgrade-fixture==2.0"])
    command = verification_command(candidate)
    assert command[:2] == ["/usr/bin/env", "-i"]
    offset = command.index("/usr/local/bin/python")
    assert command[offset + 1] == "-I"
    assert command[-2] == "/workspace/.deps/python"
    assert json.loads(command[-1]) == {"upgrade-fixture": "2.0"}
    executed = subprocess.run(
        [sys.executable, *command[offset + 1:-2], str(target), command[-1]],
        cwd=tmp_path, env={"PYTHONPATH": str(target), "PYTHONHOME": str(tmp_path),
                           "FIXTURE_SECRET": "synthetic-not-for-output"},
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert executed.returncode == 0 and executed.stderr == "", executed
    assert json.loads(executed.stdout) == {"versions": {"upgrade-fixture": found}}
    assert "synthetic-not-for-output" not in executed.stdout


@pytest.fixture
def setup(app, agent_id, tmp_path, boundary, smoke_image):
    project = tmp_path / "authorized-project"
    project.mkdir()
    (project / "main.py").write_text("print('original')")
    app.permissions.configure(agent_id, readable=[project], writable=[project], expected_revision=0)
    profile = ModelProfile(api_family="openai_chat_completions", model="fixture",
                           base_url="https://fixture.test")
    pid = app.profiles.save("fixture", profile, api_key="synthetic-unused")["id"]
    runtime = TaskRuntime(app, docker_image=smoke_image)
    tid = new_id()
    baseline = runtime.snapshots.create(tid, agent_id, str(project))
    runtime.store.insert(tid, {"agent_id": agent_id, "objective": "test",
                              "request_id": "task", "workspace": str(project)},
                         "fixture", pid, smoke_image, 40, {"baseline": baseline, "phase": "ready"})
    runtime.store.status(tid, "paused")
    service = DependencyService(app, runtime)
    yield service, runtime, tid, project
    asyncio.run(runtime.close())


def wheel(folder, version):
    """A valid wheel built with stdlib only: no build hooks or network."""
    metadata = f"upgrade_fixture-{version}.dist-info"
    files = {
        "upgrade_fixture/__init__.py": f"VERSION = {version!r}\n".encode(),
        f"{metadata}/METADATA": (
            f"Metadata-Version: 2.1\nName: upgrade-fixture\nVersion: {version}\n"
        ).encode(),
        f"{metadata}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: offline-fixture\n"
            b"Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    records = []
    for name, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        records.append(f"{name},sha256={digest},{len(data)}\n")
    records.append(f"{metadata}/RECORD,,\n")
    files[f"{metadata}/RECORD"] = "".join(records).encode()
    with zipfile.ZipFile(folder / f"upgrade_fixture-{version}-py3-none-any.whl", "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)


@pytest.fixture
def boundary(monkeypatch, smoke_image):
    """Run native Docker; substitute only PyPI with the snapshot's local wheels."""
    native = subprocess.Popen
    calls = []
    options = {"wrong_version": False}

    def offline_packages(args, **kwargs):
        if isinstance(args, list) and Path(args[0]).name.lower() in {"docker", "docker.exe"}:
            args = list(args)
            if args[1] == "create" and "--only-binary=:all:" in args:
                index = args.index("--index-url")
                args[index:index + 2] = ["--no-index", "--find-links", "/workspace/wheels"]
                if options["wrong_version"]:
                    args[-1] = "upgrade-fixture==1.0"
            calls.append(args)
        return native(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", offline_packages)
    return calls, options


def local_wheels(runtime, tid):
    root = runtime.workspace(tid)
    root.chmod(0o777)  # This generated fixture only, for the fixed container UID.
    folder = root / "wheels"
    folder.mkdir()
    wheel(folder, "1.0")
    wheel(folder, "2.0")
    return root


def installed(root, image):
    # Executing the synthetic package is intentional here, never in production verification.
    probe = """
import importlib.metadata as metadata, json, sys
target = '/workspace/.deps/python'
sys.path.insert(0, target)
import upgrade_fixture
versions = sorted(d.version for d in metadata.distributions(path=[target])
                  if d.metadata['Name'] == 'upgrade-fixture')
print(json.dumps({'import': upgrade_fixture.VERSION, 'metadata': versions}))
"""
    result = DockerExecutor(root, image=image).execute([
        "/usr/bin/env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin",
        "/usr/local/bin/python", "-I", "-c", probe,
    ])
    assert result.status == "success" and result.cleanup_confirmed, result
    assert_smoke_container_removed(result.container_name)
    return json.loads(result.stdout)


def test_real_docker_repeated_approved_pip_install_updates_code_and_metadata(setup, boundary):
    service, runtime, tid, project = setup
    calls, _ = boundary
    root = local_wheels(runtime, tid)
    plans = []
    for version in ("1.0", "2.0"):
        plan = prepare(setup, packages=[f"upgrade-fixture=={version}"],
                       request_id=f"install-{version}")
        result = approve(service, plan)
        assert result["status"] == "success", result
        assert json.loads(result["result"]["verification"]["stdout"]) == {
            "versions": {"upgrade-fixture": [version]},
        }
        assert installed(root, plan["plan"]["image_id"]) == {
            "import": version, "metadata": [version],
        }
        restarted = DependencyService(service.app, runtime)
        assert restarted.get(plan["id"]) == result
        count = len(calls)
        assert approve(restarted, plan) == result
        assert len(calls) == count  # Both durable successes are never replayed.
        plans.append(result)
    assert [p["status"] for p in service.list(tid)] == ["success", "success"]
    assert (project / "main.py").read_text() == "print('original')"
    assert not (project / ".deps").exists()
    assert runtime.get(tid)["status"] == "paused"
    installs = [c for c in calls if c[1] == "create" and "--only-binary=:all:" in c]
    assert len(installs) == 2
    assert all(c[c.index("--network") + 1] == "bridge" for c in installs)
    assert all("--no-index" in c and "https://pypi.org/simple" not in c for c in installs)
    for result in plans:
        assert_smoke_container_removed(result["result"]["container_name"])
        assert result["result"]["verification"]["cleanup_confirmed"]
        assert_smoke_container_removed(result["result"]["verification"]["container_name"])
    print("OFFLINE_PIP_UPGRADE " + json.dumps({
        "states": [p["status"] for p in plans],
        "final": installed(root, plans[-1]["plan"]["image_id"]),
    }))


def test_real_docker_exit_zero_with_wrong_version_is_durable_failure(setup, boundary):
    service, runtime, tid, _ = setup
    calls, options = boundary
    root = local_wheels(runtime, tid)
    initial = prepare(setup, packages=["upgrade-fixture==2.0"], request_id="existing-approved-2.0")
    assert approve(service, initial)["status"] == "success"
    assert installed(root, initial["plan"]["image_id"]) == {
        "import": "2.0", "metadata": ["2.0"],
    }
    # Pre-existing matching metadata must not conceal an exit-zero wrong install.
    plan = prepare(setup, packages=["upgrade-fixture==2.0"])
    options["wrong_version"] = True
    result = approve(service, plan)
    assert result["status"] == "failed", result
    assert result["error_code"] == "dependency_version_mismatch"
    assert result["result"]["exit_code"] == 0
    assert_smoke_container_removed(result["result"]["container_name"])
    assert_smoke_container_removed(result["result"]["verification"]["container_name"])
    assert installed(root, plan["plan"]["image_id"]) == {
        "import": "1.0", "metadata": ["1.0"],
    }
    restarted = DependencyService(service.app, runtime)
    count = len(calls)
    assert approve(restarted, plan) == result
    assert len(calls) == count


@pytest.mark.parametrize("contents", ["{}", "not JSON"])
def test_unconfirmed_verification_keeps_task_fenced(setup, boundary, monkeypatch, contents):
    service, runtime, tid, _ = setup
    calls, _ = boundary
    local_wheels(runtime, tid)
    plan = prepare(setup, packages=["upgrade-fixture==2.0"])
    native = subprocess.Popen

    def unreadable_metadata(args, **kwargs):
        if (isinstance(args, list) and args[1] == "create"
                and "--only-binary=:all:" not in args and "-c" in args):
            args = list(args)
            args[args.index("-c") + 1] = f"print({contents!r})"
        return native(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", unreadable_metadata)
    result = approve(service, plan)
    assert result["status"] == "unknown", result
    assert result["error_code"] == "dependency_verification_unconfirmed"
    with pytest.raises(Conflict):
        service.ensure_task_ready(tid)
    count = len(calls)
    assert approve(DependencyService(service.app, runtime), plan) == result
    assert len(calls) == count

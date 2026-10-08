"""Only the trusted constructor can opt into bridge networking."""
import json

import pytest
from test_docker_execution import DockerBoundary, assert_smoke_container_removed
from test_docker_execution import smoke_image as _local_smoke_image

from lingnexus.tools import DockerExecutor

smoke_image = _local_smoke_image


@pytest.fixture
def boundary(monkeypatch):
    import subprocess

    fake = DockerBoundary()
    monkeypatch.setattr(subprocess, "Popen", fake)
    return fake


@pytest.mark.parametrize("mode", ["none", "bridge"])
def test_trusted_network_preserves_isolation(boundary, tmp_path, mode):
    result = DockerExecutor(tmp_path, image="fixture:approved", network_mode=mode).execute(
        ["true"]
    )
    assert result.status == "success" and result.cleanup_confirmed
    create = next(c for c in boundary.calls if c[1] == "create")
    assert create[create.index("--network") + 1] == mode
    assert create[create.index("--user") + 1] == "65534:65534"
    assert create.count("--mount") == 1
    assert "readonly" in create[create.index("--mount") + 1]
    assert "--read-only" in create and "--cap-drop" in create
    assert not any(x in create for x in ("--env", "--env-file", "--privileged", "-v"))


@pytest.mark.parametrize("mode", ["host", "container:other", "default", "NONE", "",
                                  "bridge --privileged", None, True, 1, [], {}])
def test_other_networks_rejected_before_docker(boundary, tmp_path, mode):
    with pytest.raises(ValueError):
        DockerExecutor(tmp_path, image="fixture:approved", network_mode=mode)
    assert boundary.calls == []


def test_network_still_defaults_to_none(boundary, tmp_path):
    DockerExecutor(tmp_path, image="fixture:approved").execute(["true"])
    create = next(c for c in boundary.calls if c[1] == "create")
    assert create[create.index("--network") + 1] == "none"


def test_optional_trusted_bridge_smoke_is_synthetic(tmp_path, smoke_image, monkeypatch):
    """No sockets, package managers, downloads, or host credentials are used."""
    tmp_path.chmod(0o777)
    monkeypatch.setenv("LINGNEXUS_DEPENDENCY_HOST_ONLY", "synthetic-sentinel")
    probe = '''
import json, os
from pathlib import Path
assert os.getuid() == 65534
assert 'LINGNEXUS_DEPENDENCY_HOST_ONLY' not in os.environ
assert not Path('/var/run/docker.sock').exists()
assert 'eth0:' in Path('/proc/net/dev').read_text()
try:
    Path('/lingnexus-root-write-test').write_text('must fail')
except OSError:
    pass
else:
    raise AssertionError('container root must be read-only')
Path('/workspace/result.txt').write_text('synthetic')
print(json.dumps({'uid': os.getuid(), 'synthetic': True, 'root_readonly': True}))
'''
    result = DockerExecutor(tmp_path, image=smoke_image, writable=True,
                            network_mode="bridge").execute(["python", "-I", "-c", probe])
    assert result.status == "success" and result.cleanup_confirmed, result
    assert json.loads(result.stdout)["synthetic"] is True
    assert (tmp_path / "result.txt").read_text() == "synthetic"
    assert_smoke_container_removed(result.container_name)

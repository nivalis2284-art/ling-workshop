"""Registry package candidates and fixed commands; no executable input surface."""

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Manager = Literal["pip", "npm"]
Level = Literal["local", "container"]
MAX_PACKAGES = 32
MAX_PACKAGE_LENGTH = 200
_PIP = re.compile(
    r"([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)=="
    r"([0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?)"
)
_NPM = re.compile(
    r"((?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*)@"
    r"((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
    r"(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?)"
)

# Trusted code, with approved names passed as JSON data, never interpolated code.
# pip --upgrade replaces package directories but leaves older dist-info behind.
# Remove only the requested distributions' metadata before pip so neither an old
# version nor a pre-existing matching version can falsely satisfy verification.
# argv: <target> <approved-json> [pip args...]
_PIP_INSTALL = """
import json, re, shutil, subprocess, sys
from importlib.metadata import PathDistribution
from pathlib import Path
target = Path(sys.argv[1])
approved = json.loads(sys.argv[2])
try:
    for path in target.glob('*.dist-info'):
        name = PathDistribution(path).metadata.get('Name', '')
        if re.sub(r'[-_.]+', '-', name).lower() in approved:
            if path.is_symlink() or not path.is_dir():
                raise ValueError('unsafe metadata path')
            shutil.rmtree(path)
except Exception:
    print('dependency metadata preparation failed', file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(subprocess.run(
    [sys.executable, '-I', '-m', 'pip', 'install', *sys.argv[3:]],
    check=False,
).returncode)
"""

_PIP_VERIFY = """
import json, re, sys
from importlib.metadata import distributions
approved = json.loads(sys.argv[2])
versions = {name: [] for name in approved}
try:
    for distribution in distributions(path=[sys.argv[1]]):
        name = re.sub(r'[-_.]+', '-', distribution.metadata.get('Name', '')).lower()
        if name in versions:
            versions[name].append(distribution.version)
    print(json.dumps({'versions': versions}, sort_keys=True))
except Exception:
    print(json.dumps({'error': 'metadata_unavailable'}))
    raise SystemExit(1)
"""


def package_parts(manager: Manager, value: str) -> tuple[str, str]:
    if type(value) is not str or not 1 <= len(value) <= MAX_PACKAGE_LENGTH:
        raise ValueError("Expected a bounded versioned registry package name.")
    match = (_PIP if manager == "pip" else _NPM).fullmatch(value)
    if not match:
        raise ValueError("Packages must have exact registry versions; no flags, URLs or paths.")
    name, version = match.groups()
    if manager == "pip":
        name = re.sub(r"[-_.]+", "-", name).lower()
    return name, version


class DependencyCandidate(BaseModel):
    """Model-safe data only. Validating a candidate never grants network access."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    manager: Manager
    packages: list[str] = Field(min_length=1, max_length=MAX_PACKAGES)

    @model_validator(mode="after")
    def validate_packages(self):
        names = [package_parts(self.manager, value)[0] for value in self.packages]
        if len(set(names)) != len(names):
            raise ValueError("Specify each package name only once.")
        return self


def task_environment() -> dict[str, str]:
    """Fresh fixed container values for the trusted parent runner; no host reads."""
    return {
        "PYTHONPATH": "/workspace/.deps/python",
        "HOME": "/workspace/.home",
        "TMPDIR": "/workspace/.tmp",
        "TMP": "/workspace/.tmp",
        "TEMP": "/workspace/.tmp",
    }


def environment(manager: Manager) -> dict[str, str]:
    values = {
        **task_environment(),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "XDG_CONFIG_HOME": "/workspace/.home/.config",
        "PIP_CONFIG_FILE": "/dev/null",
    }
    if manager == "npm":
        values.update({
            "NPM_CONFIG_USERCONFIG": "/dev/null",
            "NPM_CONFIG_GLOBALCONFIG": "/workspace/.home/.npm-globalrc",
            "NPM_CONFIG_CACHE": "/workspace/.home/.npm",
        })
    return values


_PIP_INDEX = "https://pypi.org/simple"
_NPM_REGISTRY = "https://registry.npmjs.org"
_CONTAINER_PIP_TARGET = "/workspace/.deps/python"
_LOCAL_PYTHON: str | None = None


def local_python() -> str:
    """Interpreter for local installs and verification.

    It matches the interpreter task commands resolve through PATH so installed
    wheels are ABI-compatible. Override with LINGNEXUS_SANDBOX_PYTHON when the
    application interpreter (and its pip) differs.
    """
    global _LOCAL_PYTHON
    if _LOCAL_PYTHON is None:
        override = os.environ.get("LINGNEXUS_SANDBOX_PYTHON")
        _LOCAL_PYTHON = (
            override or shutil.which("python") or shutil.which("python3") or sys.executable
        )
    return _LOCAL_PYTHON


def _approved_json(candidate: DependencyCandidate) -> str:
    return json.dumps(dict(package_parts("pip", p) for p in candidate.packages), sort_keys=True)


def _container_command(candidate: DependencyCandidate) -> list[str]:
    argv = ["/usr/bin/env", "-i"]
    argv.extend(f"{key}={value}" for key, value in sorted(environment(candidate.manager).items()))
    if candidate.manager == "pip":
        # -I prevents snapshot Python modules/sitecustomize from hijacking pip.
        # Wheels only: a network-enabled installation never runs source build hooks.
        argv.extend([
            "/usr/local/bin/python", "-I", "-c", _PIP_INSTALL, _CONTAINER_PIP_TARGET,
            _approved_json(candidate),
            "--disable-pip-version-check", "--no-input", "--no-cache-dir",
            "--only-binary=:all:", "--no-compile", "--upgrade",
            "--index-url", _PIP_INDEX, "--target", _CONTAINER_PIP_TARGET,
        ])
    else:
        argv.extend([
            "/usr/local/bin/npm", "install", "--ignore-scripts", "--no-audit", "--no-fund",
            "--package-lock=false", "--bin-links=false",
            "--registry", _NPM_REGISTRY, "--prefix", "/workspace",
        ])
    return [*argv, *candidate.packages]


def _local_command(candidate: DependencyCandidate, root: Path) -> list[str]:
    """Host-run install into the private snapshot; the local tier has network."""
    if candidate.manager == "pip":
        target = root / ".deps" / "python"
        return [
            local_python(), "-I", "-c", _PIP_INSTALL, str(target), _approved_json(candidate),
            "--disable-pip-version-check", "--no-input", "--no-cache-dir",
            "--only-binary=:all:", "--no-compile", "--upgrade",
            "--index-url", _PIP_INDEX, "--target", str(target),
            *candidate.packages,
        ]
    return [
        "npm", "install", "--ignore-scripts", "--no-audit", "--no-fund",
        "--package-lock=false", "--bin-links=false",
        "--registry", _NPM_REGISTRY, "--prefix", str(root), *candidate.packages,
    ]


def command(candidate: DependencyCandidate, *, level: Level = "container",
            root: Path | None = None) -> list[str]:
    if level == "local":
        return _local_command(candidate, Path(root))
    return _container_command(candidate)


def _container_verification(candidate: DependencyCandidate) -> list[str]:
    return [
        "/usr/bin/env", "-i",
        *(f"{key}={value}" for key, value in sorted(environment("pip").items())),
        "/usr/local/bin/python", "-I", "-c", _PIP_VERIFY, _CONTAINER_PIP_TARGET,
        _approved_json(candidate),
    ]


def _local_verification(candidate: DependencyCandidate, root: Path) -> list[str]:
    return [
        local_python(), "-I", "-c", _PIP_VERIFY,
        str(root / ".deps" / "python"), _approved_json(candidate),
    ]


def verification_command(candidate: DependencyCandidate, *, level: Level = "container",
                         root: Path | None = None) -> list[str]:
    """Read only explicit target metadata in isolated Python; never import packages."""
    if candidate.manager != "pip":
        return []
    if level == "local":
        return _local_verification(candidate, Path(root))
    return _container_verification(candidate)

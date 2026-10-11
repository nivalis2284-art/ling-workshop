"""F-1 acceptance: skill packages are external *knowledge*, never capability.

A skill is a folder with a ``SKILL.md``. Loading one changes what the model
knows, never what it may do — every action a skill describes still goes through
the ordinary tools, the permission hook and the sandbox. These cases pin the
four things that can go wrong: discovery/shadowing, the resident index leaking
body text, on-demand reading losing its resource listing, and a resource read
escaping the skill folder.

The loop-level wiring (the ``read_skill`` tool being declared and dispatched
through the shared registry) is checked here too, because it is the same
knowledge-not-capability boundary seen from the tool side.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from lingnexus.actions import catalog
from lingnexus.models import APIFamily, ModelCapabilities, ModelProfile
from lingnexus.runtime.payload import build_payload
from lingnexus.skillpacks import SkillLibrary
from lingnexus.skillpacks.library import valid_name

# --- builders --------------------------------------------------------------


def write_skill(
    root: Path,
    folder: str,
    *,
    name: str | None = None,
    description: str | None = None,
    body: str = "# Body\n\nDo the thing.\n",
    extra: dict[str, str] | None = None,
) -> Path:
    """Create one skill folder; return it.

    By default the front-matter name matches the folder, so the folder name is
    the key — the heading fallback is exercised explicitly in its own case.
    """
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    lines.append(f"name: {name if name is not None else folder}")
    if description is not None:
        lines.append(f"description: {description}")
    lines.append("---")
    (directory / "SKILL.md").write_text(
        "\n".join(lines) + "\n" + body, encoding="utf-8"
    )
    for relative, content in (extra or {}).items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return directory


def profile(*, tools: bool = True) -> ModelProfile:
    return ModelProfile(
        api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
        model="fixture",
        base_url="https://fixture.test",
        capabilities=ModelCapabilities(tools=tools),
    )


# --- discovery and shadowing ----------------------------------------------


def test_discovers_all_roots_and_prefers_the_more_specific_one(tmp_path):
    bundled, user, project = (tmp_path / name for name in ("bundled", "user", "project"))
    write_skill(bundled, "alpha", description="bundled alpha")
    write_skill(bundled, "shared", description="bundled shared")
    write_skill(user, "shared", description="user shared")
    write_skill(project, "shared", description="project shared")

    library = SkillLibrary(
        (("project", project), ("user", user), ("bundled", bundled))
    )

    index = {item.name: item for item in library.index()}
    assert set(index) == {"alpha", "shared"}
    # project > user > bundled, and the losers are recorded rather than lost.
    assert index["shared"].description == "project shared"
    assert index["shared"].source == "project"
    assert len(index["shared"].shadowed) == 2


def test_name_comes_from_front_matter_then_heading_then_folder(tmp_path):
    root = tmp_path / "bundled"
    write_skill(root, "folder-a", name="explicit", description="from front matter")
    (root / "folder-b").mkdir()
    (root / "folder-b" / "SKILL.md").write_text(
        "# Heading Name\n\nThe description line.\n", encoding="utf-8"
    )
    (root / "folder-c").mkdir()
    (root / "folder-c" / "SKILL.md").write_text(
        "Just prose, no heading.\n", encoding="utf-8"
    )

    library = SkillLibrary((("bundled", root),))

    assert {item.name for item in library.index()} == {
        "explicit",
        "Heading Name",
        "folder-c",
    }
    assert library.get("Heading Name").description == "The description line."


def test_folders_without_a_skill_file_are_not_skills(tmp_path):
    root = tmp_path / "bundled"
    (root / "not-a-skill").mkdir(parents=True)
    (root / "not-a-skill" / "README.md").write_text("hello", encoding="utf-8")

    library = SkillLibrary((("bundled", root),))

    assert library.index() == []


def test_unreadable_skill_file_is_skipped_not_fatal(tmp_path):
    root = tmp_path / "bundled"
    write_skill(root, "good", description="fine")
    broken = root / "broken"
    broken.mkdir()
    # Invalid UTF-8 must not take down discovery.
    (broken / "SKILL.md").write_bytes(b"\xff\xfe\x00bad")

    library = SkillLibrary((("bundled", root),))

    assert {item.name for item in library.index()} == {"good"}


# --- level 1: the resident index ------------------------------------------


def test_resident_index_carries_name_and_description_only(tmp_path):
    root = tmp_path / "bundled"
    write_skill(
        root,
        "alpha",
        description="does a thing",
        body="# Alpha\n\nSECRET-BODY-MARKER\n",
    )

    library = SkillLibrary((("bundled", root),))

    index = library.prompt_index()
    assert "alpha" in index
    assert "does a thing" in index
    assert "SECRET-BODY-MARKER" not in index


def test_no_skills_installed_means_an_empty_index(tmp_path):
    library = SkillLibrary((("bundled", tmp_path / "missing"),))

    assert library.index() == []
    assert library.prompt_index() == ""


# --- level 2/3: on-demand reading -----------------------------------------


def test_read_returns_the_body_and_the_resource_listing(tmp_path):
    root = tmp_path / "bundled"
    write_skill(
        root,
        "alpha",
        description="d",
        body="# Alpha\n\nStep one.\n",
        extra={"references/notes.md": "notes", "scripts/run.py": "print('hi')"},
    )
    library = SkillLibrary((("bundled", root),))

    result = library.read("alpha")

    assert result["status"] == "completed"
    body = result["result"]
    assert "Step one." in body["body"]
    # Front matter is metadata, not part of the body handed to the model.
    assert "description:" not in body["body"]
    assert body["resources"] == ["references/notes.md", "scripts/run.py"]


def test_read_unknown_skill_reports_what_is_available(tmp_path):
    root = tmp_path / "bundled"
    write_skill(root, "alpha", description="d")
    library = SkillLibrary((("bundled", root),))

    result = library.read("nope")

    assert result["status"] == "failed"
    assert result["error_code"] == "skill_not_found"
    assert result["result"]["available"] == ["alpha"]


def test_resource_reads_cannot_escape_the_skill_folder(tmp_path):
    root = tmp_path / "bundled"
    write_skill(root, "alpha", description="d", extra={"references/notes.md": "notes"})
    (root / "secret.txt").write_text("secret", encoding="utf-8")
    library = SkillLibrary((("bundled", root),))

    # `..` and absolute paths are refused before any read is attempted.
    assert library.read("alpha", "../secret.txt")["error_code"] == "skill_resource_denied"
    assert (
        library.read("alpha", str(root / "secret.txt"))["error_code"]
        == "skill_resource_denied"
    )
    # A missing file and a directory are refused for their own honest reasons.
    assert (
        library.read("alpha", "references/missing.md")["error_code"]
        == "skill_resource_not_found"
    )
    assert library.read("alpha", "references")["error_code"] == "skill_resource_denied"
    # The happy path still works.
    ok = library.read("alpha", "references/notes.md")
    assert ok["status"] == "completed"
    assert ok["result"]["text"] == "notes"


def test_resource_paths_that_reset_the_base_are_refused(tmp_path):
    """A drive-relative or rooted resource is not caught by ``is_absolute()``.

    On Windows ``Path("C:foo").is_absolute()`` is False and ``\\foo`` has no
    drive, yet ``skill_path / "C:"`` (or ``/ "\\"``) discards ``skill_path``
    entirely — so a per-part join that trusts ``parts`` silently lands outside
    the skill folder. The hosts file is a real, always-present regular file on
    any Windows machine, which makes the escape observable rather than
    theoretical; on POSIX the same strings are refused by the shared rules.
    """
    root = tmp_path / "bundled"
    write_skill(root, "alpha")
    library = SkillLibrary((("bundled", root),))

    for resource in (
        "C:Windows\\System32\\drivers\\etc\\hosts",
        "\\Windows\\System32\\drivers\\etc\\hosts",
        "/Windows/System32/drivers/etc/hosts",
        "C:/Windows/System32/drivers/etc/hosts",
        "C:secret.txt",
    ):
        result = library.read("alpha", resource)
        assert result["error_code"] == "skill_resource_denied", resource


def make_alias(link: Path, target: Path) -> str | None:
    """Create an alias at ``link`` pointing to ``target``; say how, or give up.

    A POSIX symlink and a Windows junction are the same threat — a path that
    redirects outside the skill folder — but Python only reports the first as
    ``S_ISLNK``. Junctions need no privilege, so they are what actually occurs on
    Windows; a defence that only checks ``S_ISLNK`` passes the symlink case and
    silently fails there.
    """
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
        if link.is_symlink():
            return "symlink"
        link.unlink(missing_ok=True)
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0 and link.exists():
            return "junction"
    return None


def test_an_alias_inside_a_skill_is_never_listed_or_read(tmp_path):
    root = tmp_path / "bundled"
    directory = write_skill(
        root, "alpha", description="d", extra={"references/notes.md": "notes"}
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside secret", encoding="utf-8")
    mechanism = make_alias(directory / "references" / "alias", outside)
    if mechanism is None:
        pytest.skip("no alias mechanism is available here")
    library = SkillLibrary((("bundled", root),))

    # The alias is neither offered as a resource ...
    listed = library.read("alpha")["result"]["resources"]
    assert not any("alias" in item for item in listed), (mechanism, listed)
    # ... nor readable through.
    assert (
        library.read("alpha", "references/alias/secret.txt")["error_code"]
        == "skill_resource_denied"
    )


def test_a_skill_folder_that_is_an_alias_is_skipped(tmp_path):
    root = tmp_path / "bundled"
    write_skill(root, "real", description="real skill")
    write_skill(tmp_path, "outside-skill", description="from outside the root")
    mechanism = make_alias(root / "sneaky", tmp_path / "outside-skill")
    if mechanism is None:
        pytest.skip("no alias mechanism is available here")
    library = SkillLibrary((("bundled", root),))

    assert {item.name for item in library.index()} == {"real"}


# --- names are lookup keys, never paths -----------------------------------


def test_name_rules_allow_unicode_but_refuse_paths():
    assert valid_name("safe-bulk-edit")
    assert valid_name("表格整理")
    assert not valid_name("")
    assert not valid_name("a/b")
    assert not valid_name("a\\b")
    assert not valid_name("..")
    assert not valid_name(".")
    assert not valid_name("x" * 65)


def test_a_name_that_looks_like_a_path_is_not_indexed(tmp_path):
    root = tmp_path / "bundled"
    write_skill(root, "alpha", name="../escape", description="d")
    write_skill(root, "beta", description="fine")

    library = SkillLibrary((("bundled", root),))

    assert {item.name for item in library.index()} == {"beta"}


# --- the tool side: declared, dispatched, and budgeted --------------------


def test_read_skill_is_declared_as_a_meta_tool():
    spec = catalog.registry.get("read_skill")

    assert spec is not None
    assert spec.kind == "meta"
    # It is knowledge, so its schema carries no permission-bearing fields.
    assert set(spec.parameters.model_fields) == {"name", "resource"}


def test_reading_a_skill_through_the_library_matches_what_the_scope_returns(app):
    """`ChatToolScope.meta` delegates straight to the library; this pins the shape."""
    assert app.skillpacks.read("safe-bulk-edit")["status"] == "completed"
    assert app.skillpacks.read("safe-bulk-edit")["result"]["body"]


def test_resident_index_is_sent_only_to_tool_capable_profiles(app, agent_id, tmp_path):
    write_skill(tmp_path / "skills", "alpha", description="a resident skill")
    app.skillpacks = SkillLibrary((("bundled", tmp_path / "skills"),))
    messages = [{"id": "m1", "role": "user", "text": "你好"}]

    with_tools, _ = build_payload(app, agent_id, profile(tools=True), messages)
    without_tools, _ = build_payload(app, agent_id, profile(tools=False), messages)

    assert "a resident skill" in with_tools["messages"][0]["content"]
    assert "a resident skill" not in without_tools["messages"][0]["content"]


def test_the_bundled_set_is_small_and_generic():
    """The built-in set must stay克制: a handful of skills, no external deps."""
    from lingnexus.skillpacks import SkillLibrary as _Library

    bundled = Path(__file__).resolve().parents[1] / "src" / "lingnexus" / "skillpacks" / "builtin"
    library = _Library((("bundled", bundled),))

    names = {item.name for item in library.index()}
    assert names == {"safe-bulk-edit", "csv-to-markdown-table"}
    for item in library.index():
        assert item.description, f"{item.name} has no description for the index"
        # Third level must not be pre-read: resources are listed, not inlined.
        assert "SKILL.md" not in library.read(item.name)["result"]["resources"]

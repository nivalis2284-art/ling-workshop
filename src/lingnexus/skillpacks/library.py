"""Skill packages: folders holding a ``SKILL.md``, discovered and read on demand.

A skill is **knowledge, never a capability**. Loading one changes what the model
knows, never what it may do: every action a skill describes is still carried out
through the ordinary tools, so the permission hook and the execution sandbox
apply exactly as before. A skill cannot grant itself anything.

Three levels of progressive disclosure (see
``docs/设计/2026-10-11-第一部分能力板块.md`` §3.6):

1. **always resident** — only ``name`` + ``description`` are injected into a
   request (``prompt_index``);
2. **on demand** — the model reads the body with the ``read_skill`` tool;
3. **free** — the resource listing returned with the body points at references
   and scripts; scripts are meant to be *run*, not read.

Folders are scanned from three places, most specific first, so a project skill
shadows a user skill shadows a bundled one.
"""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from pathlib import Path

from lingnexus.tools._paths import FileAccessDenied, is_link, local_path

SKILL_FILE = "SKILL.md"
"""The one file that makes a folder a skill."""

_INDEX_LIMIT = 50
_RESOURCE_LIMIT = 200
_DESCRIPTION_CHARS = 512
_BODY_CHARS = 32_000
_RESOURCE_CHARS = 32_000
_NAME_CHARS = 64
# A name is only ever a lookup key, never a path — but it must not *look* like one,
# so separators and control characters are refused and the length is bounded.
_UNSAFE_NAME = re.compile(r"[/\\\x00-\x1f\x7f]")


def valid_name(name: str) -> bool:
    """Whether a discovered name may be used as a skill key."""
    return bool(
        name
        and len(name) <= _NAME_CHARS
        and name not in {".", ".."}
        and not _UNSAFE_NAME.search(name)
    )


@dataclass(frozen=True)
class Skill:
    """One discovered skill folder."""

    name: str
    description: str
    path: Path
    source: str
    shadowed: tuple[str, ...] = ()


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split a leading ``---`` block into flat ``key: value`` pairs plus the body.

    Only flat scalar keys are read; anything else is left in the body untouched.
    A missing or unterminated block yields no metadata and the whole text.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if sep and key.strip():
            meta[key.strip().lower()] = value.strip().strip('"').strip("'")
    return meta, "\n".join(lines[end + 1 :]).lstrip("\n")


def fallback_metadata(text: str) -> tuple[str, str]:
    """Without front matter: the first heading names it, the next line describes it."""
    heading = description = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not heading and stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            continue
        if heading and not description and not stripped.startswith("#"):
            description = stripped
            break
    return heading, description


class SkillLibrary:
    """Scans the skill folders and answers two questions: what exists, what is inside."""

    def __init__(self, roots: list[tuple[str, Path]] | tuple[tuple[str, Path], ...] = ()):
        self._roots = tuple(roots)
        self._skills: dict[str, Skill] = {}
        self.refresh()

    # --- discovery ----------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the index. Cheap enough to call at startup; never raises."""
        found: dict[str, list[tuple[str, Path, str]]] = {}
        for source, root in self._roots:
            if not root.is_dir():
                continue
            try:
                folders = sorted(entry for entry in root.iterdir() if entry.is_dir())
            except OSError:
                continue
            for folder in folders:
                # A skill folder that is itself a redirect would pull its
                # SKILL.md from outside the scanned root, so it is skipped.
                try:
                    if is_link(folder.lstat()):
                        continue
                except OSError:
                    continue
                document = folder / SKILL_FILE
                try:
                    text = document.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                meta, body = split_front_matter(text)
                fallback_name, fallback_description = fallback_metadata(body)
                name = meta.get("name") or fallback_name or folder.name
                if not valid_name(name):
                    continue
                description = (meta.get("description") or fallback_description).strip()
                found.setdefault(name, []).append((source, folder, description))

        skills: dict[str, Skill] = {}
        for name, entries in found.items():
            source, folder, description = entries[0]
            skills[name] = Skill(
                name=name,
                description=description[:_DESCRIPTION_CHARS],
                path=folder,
                source=source,
                shadowed=tuple(f"{item[0]}:{item[1]}" for item in entries[1:]),
            )
        self._skills = skills

    def index(self) -> list[Skill]:
        return sorted(self._skills.values(), key=lambda item: item.name)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    # --- level 1: the always-resident list -----------------------------------

    def prompt_index(self) -> str:
        """Name + description only. Empty when nothing is installed."""
        skills = self.index()
        if not skills:
            return ""
        shown = skills[:_INDEX_LIMIT]
        lines = [
            "可用技能（说明书，不是权限；需要时用 read_skill 读取正文）：",
            *(f"- {item.name}：{item.description}" for item in shown),
        ]
        if len(skills) > len(shown):
            lines.append(f"（另有 {len(skills) - len(shown)} 个技能未列出）")
        return "\n".join(lines)

    # --- level 2/3: on-demand reading ---------------------------------------

    def read(self, name: str, resource: str | None = None) -> dict:
        """Return a payload dict, never raising for a caller's mistake.

        ``status``/``error_code`` are shaped here so the loop can hand the model
        an honest, actionable reason instead of a generic failure.
        """
        skill = self._skills.get(name)
        if skill is None:
            return {
                "status": "failed",
                "error_code": "skill_not_found",
                "result": {"requested": name, "available": [item.name for item in self.index()]},
            }
        if resource:
            return self._read_resource(skill, resource)
        try:
            text = (skill.path / SKILL_FILE).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return {"status": "failed", "error_code": "skill_unreadable"}
        body = split_front_matter(text)[1]
        truncated = len(body) > _BODY_CHARS
        return {
            "status": "completed",
            "result": {
                "name": skill.name,
                "source": skill.source,
                "description": skill.description,
                "body": body[:_BODY_CHARS],
                "truncated": truncated,
                "path": str(skill.path),
                "resources": self.resources(skill),
            },
        }

    def resources(self, skill: Skill) -> list[str]:
        """Every regular file in the folder except ``SKILL.md``; aliases are skipped.

        "Alias" is the shared predicate (symlink, junction, mount point), not just
        ``S_ISLNK`` — a Windows junction reports as an ordinary directory and would
        otherwise be walked straight out of the skill folder.
        """
        found: list[str] = []
        stack = [skill.path]
        while stack and len(found) < _RESOURCE_LIMIT:
            folder = stack.pop()
            try:
                entries = sorted(folder.iterdir())
            except OSError:
                continue
            for entry in entries:
                try:
                    info = entry.lstat()
                except OSError:
                    continue
                if is_link(info):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    stack.append(entry)
                elif stat.S_ISREG(info.st_mode):
                    relative = entry.relative_to(skill.path).as_posix()
                    if relative != SKILL_FILE:
                        found.append(relative)
        return sorted(found)

    def _read_resource(self, skill: Skill, resource: str) -> dict:
        # Validate the *string* form through the shared path rules, not just
        # ``Path.parts``: on Windows a drive-relative ``C:foo`` or a rooted
        # ``\foo`` is not ``is_absolute()`` yet ``base / part`` resets the base
        # entirely, so a per-part join could still land outside the skill folder.
        try:
            raw = local_path(resource)
        except FileAccessDenied:
            return {"status": "failed", "error_code": "skill_resource_denied"}
        if raw.is_absolute() or not raw.parts:
            return {"status": "failed", "error_code": "skill_resource_denied"}
        target = skill.path
        for part in raw.parts:
            target = target / part
            try:
                info = target.lstat()
            except OSError:
                return {"status": "failed", "error_code": "skill_resource_not_found"}
            # An alias anywhere along the way could escape the skill folder.
            if is_link(info):
                return {"status": "failed", "error_code": "skill_resource_denied"}
        # Whatever the parts were, the join must still land inside the skill
        # folder — a rooted or drive component resets the base entirely.
        if not target.is_relative_to(skill.path):
            return {"status": "failed", "error_code": "skill_resource_denied"}
        final = target.lstat()
        if is_link(final) or not stat.S_ISREG(final.st_mode):
            return {"status": "failed", "error_code": "skill_resource_denied"}
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return {"status": "failed", "error_code": "skill_resource_unreadable"}
        truncated = len(text) > _RESOURCE_CHARS
        return {
            "status": "completed",
            "result": {
                "name": skill.name,
                "resource": raw.as_posix(),
                "text": text[:_RESOURCE_CHARS],
                "truncated": truncated,
            },
        }

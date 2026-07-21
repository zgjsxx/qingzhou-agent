"""Project skill discovery and on-demand loading."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
DEFAULT_SKILLS_DIR = PROJECT_ROOT / "skills"


@dataclass(frozen=True)
class SkillEntry:
    name: str
    description: str
    path: Path


SKILL_REGISTRY: dict[str, SkillEntry] = {}


def _skills_dir() -> Path:
    configured = os.getenv("AGENT_SKILLS_DIR", "").strip()
    if not configured:
        return DEFAULT_SKILLS_DIR
    path = Path(configured).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    meta: dict[str, str] = {}
    for raw_line in parts[1].splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip("\"'")

    return meta, parts[2].strip()


def _fallback_description(body: str, directory_name: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or directory_name
        if stripped:
            return stripped[:160]
    return directory_name


def _skill_manifests(skills_dir: Path) -> list[Path]:
    """Return SKILL.md files at any depth under the skills directory."""
    return sorted(
        (path for path in skills_dir.rglob("SKILL.md") if path.is_file()),
        key=lambda path: path.relative_to(skills_dir).as_posix().lower(),
    )


def scan_skills() -> dict[str, SkillEntry]:
    """Scan skill manifests and store metadata only, not full skill content."""
    registry: dict[str, SkillEntry] = {}
    skills_dir = _skills_dir()
    if not skills_dir.exists() or not skills_dir.is_dir():
        SKILL_REGISTRY.clear()
        return SKILL_REGISTRY

    for manifest in _skill_manifests(skills_dir):
        skill_dir = manifest.parent
        try:
            raw = manifest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        meta, body = _parse_frontmatter(raw)
        name = (meta.get("name") or skill_dir.name).strip()
        if not name:
            continue
        description = (
            meta.get("description")
            or meta.get("when_to_use")
            or _fallback_description(body or raw, skill_dir.name)
        ).strip()
        registry[name] = SkillEntry(name=name, description=description, path=manifest)

    SKILL_REGISTRY.clear()
    SKILL_REGISTRY.update(registry)
    return SKILL_REGISTRY


def list_skills() -> list[SkillEntry]:
    if not SKILL_REGISTRY:
        scan_skills()
    return list(SKILL_REGISTRY.values())


def skill_catalog_for_prompt() -> str:
    skills = list_skills()
    if not skills:
        return "(no skills found)"
    return "\n".join(f"- {skill.name}: {skill.description}" for skill in skills)


def load_skill_content(name: str) -> str:
    """Load full SKILL.md content by registry name only."""
    normalized_name = (name or "").strip()
    if not normalized_name:
        return "Error: skill name must not be empty."

    if not SKILL_REGISTRY:
        scan_skills()

    skill = SKILL_REGISTRY.get(normalized_name)
    if skill is None:
        available = ", ".join(entry.name for entry in list_skills()) or "(none)"
        return f"Error: skill not found: {normalized_name}. Available skills: {available}"

    try:
        content = skill.path.read_text(encoding="utf-8", errors="replace")
        skill_root = skill.path.parent
        return (
            f"Skill name: {skill.name}\n"
            f"Skill root directory: {skill_root}\n"
            "When using files referenced by this skill, resolve relative paths "
            "from the skill root directory. When running shell commands from "
            "this skill, pass the skill root directory as run_shell_command.cwd "
            "unless the skill explicitly says otherwise.\n\n"
            f"{content}"
        )
    except OSError as exc:
        return f"Error: failed to load skill {normalized_name}: {exc}"


scan_skills()

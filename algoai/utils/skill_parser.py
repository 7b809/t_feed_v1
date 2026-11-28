"""Parse skill metadata and discover SKILL.md files without PyYAML."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    category: str
    access: str
    path: Path
    root: Path
    execution: str = "agy"
    script: Optional[str] = None
    result_schema: Optional[str] = None

def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value

def parse_frontmatter(markdown: str) -> Tuple[Dict[str, str], str]:
    lines = markdown.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, markdown
    meta: Dict[str, str] = {}
    closing = None
    index = 1
    while index < len(lines):
        line = lines[index]
        if line.strip() == "---":
            closing = index
            break
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line or line[0].isspace():
            index += 1
            continue
        key, raw = line.split(":", 1)
        key, raw = key.strip().lower(), raw.strip()
        if raw in (">", ">-", "|", "|-"):
            block = []
            index += 1
            while index < len(lines):
                candidate = lines[index]
                if candidate.strip() and not candidate[0].isspace():
                    break
                block.append(candidate.strip())
                index += 1
            meta[key] = ("\n" if raw.startswith("|") else " ").join(part for part in block if part)
            continue
        meta[key] = _unquote(raw)
        index += 1
    if closing is None:
        return {}, markdown
    return meta, "\n".join(lines[closing + 1:])

def _fallback_description(body: str) -> str:
    for raw in body.splitlines():
        line = raw.strip()
        if line and not line.startswith(("#", "---")):
            return line.lstrip("-* ").strip()
    return "No description provided."

def _access_level(meta: Dict[str, str]) -> str:
    access = meta.get("access", "").strip().lower().replace("_", "-")
    if access in ("read-only", "readonly", "read only"):
        return "read-only"
    if access in ("write-capable", "write", "read-write", "read/write"):
        return "write-capable"
    read_only = meta.get("read_only", "").strip().lower()
    if read_only in ("true", "yes", "1"):
        return "read-only"
    if read_only in ("false", "no", "0"):
        return "write-capable"
    return "unknown"

def discover_skills(roots: List[Path]) -> List[SkillMeta]:
    discovered: List[SkillMeta] = []
    seen_names = set()
    for root in roots:
        if not root.is_dir():
            continue
        for md_file in sorted(root.rglob("SKILL.md")):
            if not md_file.is_file():
                continue
            try:
                raw = md_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta, body = parse_frontmatter(raw)
            name = meta.get("name", "").strip() or md_file.parent.name
            key = name.casefold()
            if key in seen_names:
                continue
            seen_names.add(key)
            category = meta.get("category", "").strip()
            if not category:
                relative = md_file.parent.relative_to(root)
                category = relative.parts[0] if len(relative.parts) > 1 else "General"
            category = category.replace("-", " ").replace("_", " ").title()
            description = meta.get("description", "").strip() or _fallback_description(body)
            discovered.append(SkillMeta(
                name=name, description=" ".join(description.split()),
                category=category, access=_access_level(meta),
                path=md_file.resolve(), root=root.resolve(),
                execution=meta.get("execution", "agy").strip().lower() or "agy",
                script=meta.get("script", "").strip() or None,
                result_schema=meta.get("result_schema", "").strip() or None,
            ))
    return sorted(discovered, key=lambda item: (item.category.casefold(), item.name.casefold()))

def find_skill(skills: List[SkillMeta], name: str) -> Optional[SkillMeta]:
    target = name.strip().casefold()
    return next((skill for skill in skills if skill.name.casefold() == target), None)

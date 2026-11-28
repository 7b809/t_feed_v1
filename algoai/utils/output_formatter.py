"""Terminal and JSON formatting helpers."""
from __future__ import annotations
import json
import textwrap
from typing import Iterable, List
from .skill_parser import SkillMeta

def skills_as_json(skills: Iterable[SkillMeta]) -> str:
    rows = [{
        "name": item.name, "description": item.description,
        "category": item.category, "access": item.access, "path": str(item.path),
    } for item in skills]
    return json.dumps(rows, indent=2)

def print_skills(skills: List[SkillMeta], title: str = "ALGOAI AVAILABLE SKILLS") -> int:
    divider = "─" * 72
    print(title)
    print(divider)
    if not skills:
        print("No skills were found in the configured skill directories.")
    else:
        grouped = {}
        for skill in skills:
            grouped.setdefault(skill.category, []).append(skill)
        for category, items in grouped.items():
            print("\n" + category.upper() + "\n")
            for item in items:
                print("  " + item.name)
                print(textwrap.fill(item.description, width=68,
                    initial_indent="    ", subsequent_indent="    "))
                print("    Access: " + item.access + "\n")
    print(divider)
    print("Total skills: " + str(len(skills)))
    return 0

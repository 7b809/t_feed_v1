"""Skill listing, searching and detail views."""
from __future__ import annotations
from typing import List
from algoai.core.config import skill_roots
from algoai.utils.output_formatter import print_skills, skills_as_json
from algoai.utils.skill_parser import SkillMeta, discover_skills, find_skill

def get_skills() -> List[SkillMeta]:
    return discover_skills(skill_roots())

def handle_skills(name: str = None, query: str = None, as_json: bool = False) -> int:
    skills = get_skills()
    if query is not None:
        terms = [part for part in query.casefold().split() if part]
        matches = [item for item in skills if all(
            term in " ".join((item.name, item.description, item.category)).casefold()
            for term in terms)]
        return print_skills(matches, title=f"ALGOAI SKILL SEARCH: {query!r}")
    if name:
        skill = find_skill(skills, name)
        if skill is None:
            print(f"[ERROR] Skill not found: {name}")
            if skills:
                print("Available skills: " + ", ".join(item.name for item in skills))
            return 1
        print(f"Name:        {skill.name}")
        print(f"Category:    {skill.category}")
        print(f"Access:      {skill.access}")
        print(f"Description: {skill.description}")
        print(f"File:        {skill.path}")
        print("─" * 72)
        print(skill.path.read_text(encoding="utf-8", errors="replace").rstrip())
        return 0
    if as_json:
        print(skills_as_json(skills))
        return 0
    return print_skills(skills)

def discover_for_status() -> List[SkillMeta]:
    return get_skills()

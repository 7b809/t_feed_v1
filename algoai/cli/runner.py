"""Run a discovered skill through AGY."""
from __future__ import annotations
import sys
from algoai.core.agy_launcher import launch_agy_with_token
from algoai.utils.skill_parser import SkillMeta

def run_skill(skill: SkillMeta, context: str, args) -> int:
    if skill.access == "write-capable" and not args.allow_write:
        print(f"[BLOCKED] Skill '{skill.name}' is marked write-capable. Re-run with --allow-write after reviewing its instructions.", file=sys.stderr)
        print("[SAFETY] --allow-write is separate from --skip-permissions and does not replace action-specific confirmations.", file=sys.stderr)
        return 2
    if skill.access == "unknown":
        print(f"[WARN] Access level for '{skill.name}' is unknown; review its SKILL.md. Add access: read-only or access: write-capable to its frontmatter.", file=sys.stderr)
    if args.allow_write:
        print("[SAFETY] Write-capable skill execution explicitly enabled for this invocation.")
    prompt = (
        f"Read and follow the complete skill instructions in this file: {skill.path}\n"
        "Read the entire SKILL.md before acting. Follow all safety checks and output rules "
        "defined by the skill. Do not read .env or print credentials.\n"
    )
    if context.strip():
        prompt += "\nUser task/context:\n" + context.strip() + "\n"
    else:
        prompt += "\nPerform the task requested by the user for this skill. Ask for required missing parameters; do not invent them.\n"
    prompt += (
        "\nThe --allow-write flag, if supplied, only permits write-capable skill use. "
        "It does not replace exact order, cancellation, modification, or exit confirmations "
        "required by the skill."
    )
    return launch_agy_with_token(prompt=prompt, model=args.model,
        skip_permissions=args.skip_permissions, output_format=args.output_format,
        env_path=args.env_file, show_progress=args.elapsed_log,
        progress_interval=args.progress_interval, command_name="run",
        skill_name=skill.name, access_level=skill.access, allow_write=args.allow_write)

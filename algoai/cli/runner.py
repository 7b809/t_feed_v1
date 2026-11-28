"""Run a discovered skill through AGY or its declared machine-readable handler."""
from __future__ import annotations
import json
import sys
from pathlib import Path
from algoai.core.agy_launcher import launch_agy_with_token, run_python_json
from algoai.utils.skill_parser import SkillMeta


def _safe_skill_file(skill: SkillMeta, relative: str) -> Path:
    candidate = (skill.path.parent / relative).resolve()
    try:
        candidate.relative_to(skill.path.parent.resolve())
    except ValueError:
        raise ValueError("Skill script/schema path must remain inside the skill directory.")
    if not candidate.is_file():
        raise FileNotFoundError(f"Declared skill file not found: {candidate}")
    return candidate


def _script_args(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--script-args-json must be a valid JSON array: {exc}") from None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("--script-args-json must be a JSON array containing only strings.")
    if len(value) > 100:
        raise ValueError("--script-args-json accepts at most 100 arguments.")
    return value


def run_skill(skill: SkillMeta, context: str, args) -> int:
    if skill.access == "write-capable" and not args.allow_write:
        print(f"[BLOCKED] Skill '{skill.name}' is marked write-capable. Re-run with --allow-write after reviewing its instructions.", file=sys.stderr)
        print("[SAFETY] --allow-write is separate from --skip-permissions and does not replace action-specific confirmations.", file=sys.stderr)
        return 2
    if args.result_format == "json":
        if skill.execution != "direct-python" or not skill.script or not skill.result_schema:
            print(f"[ERROR] Skill '{skill.name}' does not declare a validated JSON execution handler.", file=sys.stderr)
            print("[ERROR] Add execution: direct-python, script and result_schema to its SKILL.md before using --result-format json.", file=sys.stderr)
            return 2
        if context.strip():
            print("[ERROR] Direct JSON skills accept explicit --script-args-json, not free-form context.", file=sys.stderr)
            return 2
        try:
            script = _safe_skill_file(skill, skill.script)
            schema_path = _safe_skill_file(skill, skill.result_schema)
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            script_args = _script_args(args.script_args_json)
        except Exception as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        return run_python_json(
            script, schema, env_path=args.env_file, skill_name=skill.name,
            access_level=skill.access, script_args=script_args,
            write_authorized=bool(args.allow_write),
        )
    if args.script_args_json:
        print("[ERROR] --script-args-json is only supported with --result-format json.", file=sys.stderr)
        return 2
    if skill.access == "unknown":
        print("[WARN] Skill access level is unknown; review its SKILL.md.", file=sys.stderr)
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

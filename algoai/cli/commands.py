"""Dispatch parsed ALGOAI commands."""
from __future__ import annotations
from algoai.cli.help import print_help
from algoai.cli.runner import run_skill
from algoai.cli.skills import get_skills, handle_skills
from algoai.cli.status import print_status
from algoai.core.agy_launcher import launch_agy_with_token
from algoai.utils.skill_parser import find_skill

def dispatch(args) -> int:
    command = args.command
    if command == "help":
        return print_help()
    if command == "version":
        from algoai import __version__
        print(__version__)
        return 0
    if command == "status":
        return print_status(args.env_file)
    if command == "skills":
        return handle_skills(getattr(args, "name", None),
            getattr(args, "search", None), getattr(args, "as_json", False))
    if command == "run":
        skill = find_skill(get_skills(), args.skill_name)
        if skill is None:
            print(f"[ERROR] Skill not found: {args.skill_name}")
            return 1
        return run_skill(skill, args.prompt or "", args)
    if command == "prompt":
        return launch_agy_with_token(prompt=args.prompt, model=args.model,
            skip_permissions=args.skip_permissions, output_format=args.output_format,
            env_path=args.env_file, show_progress=args.elapsed_log,
            progress_interval=args.progress_interval, command_name="prompt",
            access_level="unknown", allow_write=args.allow_write)
    print_help()
    return 0

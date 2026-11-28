"""Argument parsing for subcommands and legacy prompt syntax."""
from __future__ import annotations
import argparse
from typing import List, Optional


def _add_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-m", "--model", default=None)
    parser.add_argument("--skip-permissions", action="store_true")
    parser.add_argument("--allow-write", action="store_true")
    parser.add_argument("--output-format", choices=["text", "json", "stream-json"], default=None,
                        help="AGY transport/output format (not the skill result schema).")
    parser.add_argument("--result-format", choices=["text", "json"], default="text",
                        help="Skill result format; json emits only schema-validated JSON.")
    parser.add_argument("--script-args-json", default=None,
                        help="JSON array of strings passed to a declared direct-Python skill handler.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("-y", "--elapsed-log", action="store_true")
    parser.add_argument("--progress-interval", type=int, default=5)


def parse_legacy_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="algoai",
        description="Load the Upstox access token from MongoDB and launch AGY.",
        allow_abbrev=False)
    parser.add_argument("positional_prompt", nargs="?", default=None)
    parser.add_argument("-p", "--prompt", dest="flag_prompt", default=None)
    _add_run_options(parser)
    args = parser.parse_args(argv)
    args.prompt = args.flag_prompt if args.flag_prompt is not None else args.positional_prompt
    args.command = "prompt"
    return args


def parse_subcommand_args(argv: List[str]) -> argparse.Namespace:
    command = argv[0]
    if command == "help":
        parser = argparse.ArgumentParser(prog="algoai help", add_help=False)
        parser.parse_args(argv[1:])
        return argparse.Namespace(command="help")
    if command == "skills":
        parser = argparse.ArgumentParser(prog="algoai skills", allow_abbrev=False)
        parser.add_argument("name", nargs="?", default=None)
        parser.add_argument("--search", default=None, metavar="QUERY")
        parser.add_argument("--json", action="store_true", dest="as_json")
        args = parser.parse_args(argv[1:])
        if args.name and args.search:
            parser.error("provide a skill name or --search, not both")
        args.command = "skills"
        return args
    if command == "status":
        parser = argparse.ArgumentParser(prog="algoai status", allow_abbrev=False)
        parser.add_argument("--env-file", default=".env")
        args = parser.parse_args(argv[1:])
        return argparse.Namespace(command="status", env_file=args.env_file)
    if command == "run":
        parser = argparse.ArgumentParser(prog="algoai run", allow_abbrev=False)
        _add_run_options(parser)
        parser.add_argument("skill_name")
        parser.add_argument("context", nargs="*")
        args = parser.parse_intermixed_args(argv[1:])
        if args.progress_interval < 1:
            parser.error("--progress-interval must be at least 1")
        args.command = "run"
        args.prompt = " ".join(args.context).strip() or None
        return args
    raise ValueError(f"Unsupported command: {command}")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    import sys
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] in {"help", "skills", "run", "status"}:
        return parse_subcommand_args(values)
    if values in (["--version"], ["-V"]):
        return argparse.Namespace(command="version")
    if values == ["--help"]:
        return argparse.Namespace(command="help")
    args = parse_legacy_args(values)
    if args.progress_interval < 1:
        raise SystemExit("--progress-interval must be at least 1")
    return args

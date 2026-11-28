"""ALGOAI CLI entry point."""
from __future__ import annotations
import sys
from typing import List, Optional
from algoai.cli.arguments import parse_args, parse_legacy_args
from algoai.cli.commands import dispatch
from algoai.cli.help import print_help

def main(argv: Optional[List[str]] = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    args = parse_legacy_args([]) if not values else parse_args(values)
    if args.command == "help":
        return print_help()
    return dispatch(args)

if __name__ == "__main__":
    raise SystemExit(main())

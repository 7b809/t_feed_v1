#!/usr/bin/env python3
"""Backward-compatible entry point for the modular ALGOAI CLI."""
from algoai import __version__
from algoai.cli.arguments import parse_legacy_args
from algoai.core.agy_launcher import launch_agy_with_token
from algoai.core.timing import format_duration
from algoai.main import main

def parse_args(argv=None):
    """Compatibility alias for callers that imported the previous parser."""
    return parse_legacy_args(argv)

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        import sys
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)

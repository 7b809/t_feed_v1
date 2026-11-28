"""Custom ALGOAI help output."""
from algoai import __version__

def print_help() -> int:
    print(f"""ALGOAI CLI v{__version__}
Usage:
  algoai help
  algoai --help
  algoai --version
  algoai status [--env-file PATH]
  algoai skills
  algoai skills <skill-name>
  algoai skills --search <query>
  algoai skills --json
  algoai run [run-options] <skill-name> [extra context...]
  algoai [run-options] --prompt "<prompt>"
  algoai [run-options] "<prompt>"

Commands:
  help                 Show this help.
  status               Show launcher, AGY, environment and skill status.
  skills               Discover and list skills with descriptions and access levels.
  skills <name>        Show the complete SKILL.md for one skill.
  skills --search      Search skill names, descriptions and categories.
  run <name>           Execute a discovered skill through AGY.

Run options:
  -m, --model MODEL    Override the AGY model.
  --skip-permissions   Pass --dangerously-skip-permissions to AGY (not trade approval).
  --allow-write        Explicitly allow a skill marked write-capable to run.
  --output-format FMT  text, json, or stream-json.
  --env-file PATH      Environment file (default: project .env).
  -y, --elapsed-log    Show elapsed-time progress.
  --progress-interval N
                       Progress interval in seconds (default: 5).

Examples:
  algoai skills
  algoai skills --search pnl
  algoai skills upstox-pnl
  algoai run upstox-pnl
  algoai --skip-permissions --prompt "Fetch today's positions and P&L"
  algoai run --allow-write upstox-order "Place the order described here"

Safety:
  --skip-permissions only bypasses AGY approval prompts. It is independent of
  --allow-write, and neither flag replaces action-specific trading confirmations.
""")
    return 0

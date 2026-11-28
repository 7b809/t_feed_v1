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

Run options:
  -m, --model MODEL    Override the AGY model.
  --skip-permissions   Bypass AGY approval prompts (not trade authorization).
  --allow-write        Explicitly permit a write-capable skill.
  --output-format FMT  AGY output protocol: text, json, or stream-json.
  --result-format FMT  Skill result: text or validated json (default: text).
  --env-file PATH      Environment file (default: project .env).
  -y, --elapsed-log    Show elapsed-time progress.
  --progress-interval N
                       Progress interval in seconds (default: 5).

Examples:
  algoai skills
  algoai skills --search pnl
  algoai run upstox-pnl
  algoai run upstox-pnl --result-format json
  algoai run upstox-orders-fetch
  algoai --skip-permissions --prompt "Fetch today's positions and P&L"

JSON mode:
  Available only for skills declaring a direct handler and JSON Schema.
  Prints validated JSON to stdout only. Errors go to stderr with non-zero exit.
""")
    return 0

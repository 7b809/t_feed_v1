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
  -m, --model MODEL       Override the AGY model.
  --skip-permissions      Bypass AGY approval prompts (not trade authorization).
  --allow-write           Explicitly permit a write-capable skill.
  --output-format FMT     AGY transport protocol: text, json, or stream-json.
  --result-format FMT     Skill result: text or validated json (default: text).
  --script-args-json JSON JSON array of strings passed to a declared direct-Python handler.
  --env-file PATH         Environment file (default: project .env).
  -y, --elapsed-log       Show elapsed-time progress.
  --progress-interval N   Progress interval in seconds (default: 5).

Examples:
  algoai skills
  algoai skills --search pnl
  algoai run upstox-funds --result-format json
  algoai run upstox-orders-fetch --result-format json --script-args-json '[ "--kind", "all" ]'
  algoai run upstox-portfolio --result-format json --script-args-json '[ "--kind", "holdings" ]'
  algoai run upstox-margin --result-format json --script-args-json '[ "--kind", "brokerage", "--instrument", "NSE_EQ|<ISIN>", "--quantity", "1", "--transaction-type", "BUY", "--product", "D", "--price", "100" ]'
  algoai --skip-permissions --prompt "Existing free-form prompt"

JSON mode:
  Available only for skills declaring execution: direct-python, script and result_schema.
  Handler arguments are passed only through --script-args-json as a JSON array of strings.
  Stdout is parsed and validated against the declared JSON Schema. Credentials and logs are never emitted to stdout.
  Write-capable skills still require --allow-write and their own explicit action confirmations.
""")
    return 0

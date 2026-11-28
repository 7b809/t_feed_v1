# ALGOAI CLI v2

The modular CLI retains the existing launch_upstox_agy.py entry point and the current ~/.local/bin/algoai wrapper contract. The wrapper can keep changing into the application directory and invoking the Python entry point with the project virtual environment.

## Commands

    algoai help
    algoai --help
    algoai --version
    algoai status
    algoai skills
    algoai skills upstox-pnl
    algoai skills --search "order fetch"
    algoai skills --json
    algoai run upstox-pnl
    algoai run upstox-orders-fetch "Show today's pending orders"
    algoai --skip-permissions --prompt "Existing free-form prompt"
    algoai "Existing positional prompt"

## Skill discovery precedence

1. ALGOAI_SKILLS_DIR (paths separated by the platform path separator)
2. ~/.config/algoai/skills
3. Application-root/.agents/skills
4. Current-workspace/.agents/skills

Earlier roots win when duplicate skill names are found. Relative ALGOAI_SKILLS_DIR entries resolve against the application root. ALGOAI_APP_DIR can override the application root. AGY runs with the application root as its working directory, and the exact absolute SKILL.md path is included in the skill task prompt.

## Skill metadata and safety

Each SKILL.md can declare name, description, category, and access in YAML frontmatter. Use access: read-only or access: write-capable. A skill marked write-capable is blocked unless --allow-write is supplied. Unknown access levels are shown as unknown and generate a warning.

--skip-permissions is an independent AGY option. It only passes --dangerously-skip-permissions to AGY; it does not imply --allow-write and is not a trade authorization. Skill-specific exact-state and action confirmations remain mandatory.

## Execution logs

JSON Lines metadata is appended to ~/.local/state/algoai/executions.jsonl (or ALGOAI_LOG_FILE). The directory and file use restrictive permissions where supported. Records contain command, skill, timestamps, duration, exit code, output format, permission-bypass state, write-authorization flag, access classification, and error type. Prompt text, access tokens, and environment values are never logged.

## Dependencies

The new CLI modules use only the Python standard library. Existing token retrieval continues to use the project's python-dotenv and pymongo dependencies.

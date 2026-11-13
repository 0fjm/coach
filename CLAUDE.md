# Personal Trainer

A local cycling coach. Garmin Connect data is synced into SQLite and exposed through a read-only stdio MCP server.

The coaching stance and workflow live in the MCP resource `trainer://coach` (served by the `trainer` MCP server). Read it before answering coaching questions.

## Commands

- Run tests: `uv run pytest`
- `uv run trainer login` and `uv run trainer backfill` require Garmin credentials. Run them only in the user's own terminal, never via the Bash tool. `trainer` is not on the shell PATH, so always prefix with `uv run`.

## Constraints

- Never read `.secrets/`.

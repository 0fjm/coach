# Personal Trainer

A local cycling coach. Garmin Connect data is synced into SQLite and exposed to Claude Code through a read-only stdio MCP server. Everything stays on your machine.

## How it works

- `trainer sync` and `trainer backfill` pull your Garmin data into a local SQLite file.
- The MCP server (`trainer-mcp`) exposes that data and your plan files to Claude.
- `/coach <question>` in Claude Code loads the coaching stance (`trainer://coach`) and answers from your data.

## Requirements

- [uv](https://docs.astral.sh/uv/)
- [Claude Code](https://claude.com/claude-code)
- A Garmin Connect account

## Setup

1. Clone the repo and install dependencies.

   ```sh
   git clone https://github.com/0fjm/coach.git
   cd coach
   uv sync
   ```

2. Create your data folder and the MCP config. Pick a folder outside the repo, for example `~/trainer-data`. It holds your database, login token and plans.

   ```sh
   uv run trainer init --home ~/trainer-data
   ```

   This creates the folders, runs `git init` there so plan changes are versioned, and writes `.mcp.json` with the right paths. An existing `.mcp.json` is never overwritten.

3. Start Claude Code in this repo and run `/coach`. With no `profile.md` yet, the coach interviews you (races, weekly hours, injuries, equipment, experience) and writes `profile.md`, `season-<year>.md` and `log.md`.

4. Log in to Garmin in your own terminal. It asks for email, password and MFA code. The token goes to the OS keychain.

   ```sh
   uv run trainer login
   ```

   If no keychain is available, use `uv run trainer login --store file`. The token is then written to `$TRAINER_HOME/.secrets/`.

5. Import your history. The default is one year; use `--since YYYY-MM-DD` for more.

   ```sh
   uv run trainer backfill
   ```

6. Run `/coach how am I doing?`.

`init` remembers the folder in `.trainer-home` (gitignored), so later commands find it. `TRAINER_HOME` overrides it. Without `--home`, `init` uses the repo folder itself, which keeps your data next to the code.

## Commands

All commands run as `uv run trainer <command>`.

| Command | Purpose |
| --- | --- |
| `init [--home PATH]` | Create the data folder and `.mcp.json` |
| `login [--store keychain\|file]` | Log in to Garmin and store the token |
| `sync` | Fetch the recent days (7 day overlap) |
| `backfill [--since DATE] [--interval SECONDS]` | Import history |
| `rebuild` | Rebuild the database from stored raw responses |
| `status` | Show last sync, oldest synced day and activity count |

`/coach` calls `sync` itself, so you rarely need to run it by hand.

## Privacy

- Your data lives only in `TRAINER_HOME`: `data/` (SQLite), `.secrets/` (token), `plans/` (profile and logs).
- Never commit `TRAINER_HOME`, `.mcp.json` or your plans.
- Run `login` and `backfill` only in your own terminal, never through an AI tool.

## Limits

- The Garmin client (`garminconnect`) is unofficial. Garmin can change or block it, and logins can be rate limited.
- This is not medical advice. See a professional for pain or injury.

## Tests

```sh
uv run pytest
```

from __future__ import annotations

import logging
import sys
from datetime import UTC, date, datetime
from typing import Callable

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from trainer.auth import open_source
from trainer.config import Paths, Settings, load_paths, load_settings
from trainer.db import connect
from trainer.plans import PlanPathError
from trainer.plans import list_plans as _list_plans
from trainer.plans import read_plan as _read_plan
from trainer.plans import write_plan as _write_plan
from trainer.queries import QueryError
from trainer.queries import describe_schema as _describe_schema
from trainer.queries import daily_status as _daily_status
from trainer.queries import list_activities, query_sql as _query_sql
from trainer.queries import training_load as _training_load
from trainer.stance import COACH_STANCE
from trainer.sync import run_sync


def build_server(
    paths: Paths,
    settings: Settings,
    source_factory=None,
    today: Callable[[], date] = date.today,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> MCPServer:
    if source_factory is None:
        source_factory = lambda: open_source(paths, settings)

    connect(paths.db).close()

    server = MCPServer(
        name="trainer",
        instructions="See trainer://coach for role and workflow.",
    )

    @server.tool()
    async def sync() -> dict:
        """Sync Garmin data; returns SyncResult fields including last_sync_at."""
        def _run():
            conn = connect(paths.db)
            try:
                result = run_sync(
                    conn,
                    source_factory,
                    lock_path=paths.lock,
                    today=today(),
                    now=now(),
                    settings=settings,
                )
                return result.to_dict()
            finally:
                conn.close()

        return await anyio.to_thread.run_sync(_run)

    @server.tool()
    def describe_schema() -> dict:
        """Return schema info for all tables; call before query_sql."""
        return _describe_schema(paths.db)

    @server.tool()
    def query_sql(sql: str, max_rows: int | None = None) -> dict:
        """Run a read-only SQL query; dates are YYYY-MM-DD, max_rows caps results."""
        if max_rows is None:
            max_rows = settings.sql_default_rows
        try:
            return _query_sql(paths.db, sql, max_rows, settings)
        except QueryError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    def daily_status(start: str, end: str) -> dict:
        """Return daily metrics for a YYYY-MM-DD range (start, end inclusive)."""
        try:
            return _daily_status(paths.db, start, end)
        except QueryError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    def activities(
        start: str,
        end: str,
        sport_family: str | None = None,
        include_duplicates: bool = False,
    ) -> dict:
        """List activities for a YYYY-MM-DD range; sport_family filters by sport."""
        try:
            return list_activities(paths.db, start, end, sport_family, include_duplicates)
        except QueryError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    def training_load(start: str, end: str) -> dict:
        """Return training load metrics for a YYYY-MM-DD range."""
        try:
            return _training_load(paths.db, start, end)
        except QueryError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    def list_plans() -> list[str]:
        """List all plan files in the plans directory."""
        return _list_plans(paths.plans)

    @server.tool()
    def read_plan(name: str) -> str:
        """Read a plan file by relative POSIX path ending in .md."""
        try:
            return _read_plan(paths.plans, name)
        except PlanPathError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    def write_plan(name: str, content: str, summary: str | None = None) -> dict:
        """Write or update a plan file; commits to git; dates are YYYY-MM-DD."""
        try:
            result = _write_plan(paths.root, paths.plans, name, content, summary, settings)
        except PlanPathError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "path": result.path,
            "committed": result.committed,
            "commit": result.commit,
            "commit_error": result.commit_error,
        }

    @server.prompt()
    def coach(question: str = "") -> str:
        """Cycling coach prompt; include a question to ask the coach."""
        if question:
            return f"{COACH_STANCE}\n\nQuestion: {question}"
        return COACH_STANCE

    @server.resource("trainer://coach", mime_type="text/markdown")
    def coach_resource() -> str:
        """Coaching stance and workflow."""
        return COACH_STANCE

    return server


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    logging.getLogger("garminconnect").setLevel(logging.WARNING)
    build_server(load_paths(), load_settings()).run()

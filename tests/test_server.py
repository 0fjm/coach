from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from fakes import FakeSource

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def make_server(tmp_path):
    from trainer.config import Paths, Settings
    from trainer.db import connect
    from trainer.rawstore import mark_status
    from trainer.server import build_server

    db_path = tmp_path / "data" / "trainer.db"
    conn = connect(db_path)
    mark_status(
        conn,
        "user_summary",
        date.today().isoformat(),
        "ok",
        datetime.now(timezone.utc),
    )
    conn.close()

    paths = Paths(
        root=tmp_path,
        db=db_path,
        plans=tmp_path / "plans",
        secrets=tmp_path / ".secrets",
        token_file=tmp_path / ".secrets" / "garmin.token",
        lock=tmp_path / "data" / "sync.lock",
    )
    fake = FakeSource.default()
    return lambda settings=Settings(): build_server(paths, settings, source_factory=lambda: fake)


@pytest.fixture
def server(make_server):
    return make_server()


async def test_tool_names(server):
    assert {t.name for t in await server.list_tools()} == {
        "sync", "describe_schema", "query_sql", "daily_status", "activities",
        "training_load", "list_plans", "read_plan", "write_plan"}


async def test_coach_prompt_and_resource_share_stance(server):
    from trainer.stance import COACH_STANCE

    prompt = await server.get_prompt("coach", {"question": "Threshold today?"})
    resource = list(await server.read_resource("trainer://coach"))
    assert COACH_STANCE in prompt.messages[0].content.text
    assert resource[0].content == COACH_STANCE
    assert "—" not in COACH_STANCE


async def test_sync_tool_writes_nothing_to_stdout(server, capfd):
    await server.call_tool("sync", {})
    assert capfd.readouterr().out == ""


async def test_query_sql_error_is_tool_error(server):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError):
        await server.call_tool("query_sql", {"sql": "DELETE FROM activities"})


async def test_write_plan_traversal_is_tool_error(server):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError):
        await server.call_tool("write_plan", {"name": "../x.md", "content": "x"})


def test_mcp_json_registers_server():
    cfg = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["trainer"]
    assert cfg["command"] == "uv" and cfg["args"][-1] == "trainer-mcp"


async def test_query_sql_default_rows_come_from_settings(make_server):
    from trainer.config import Settings

    server = make_server(Settings(sql_default_rows=3))
    result = await server.call_tool(
        "query_sql",
        {"sql": "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n LIMIT 600) SELECT i FROM n"},
    )
    assert json.loads(result.content[0].text)["row_count"] == 3


@pytest.mark.parametrize("tool", ["daily_status", "activities", "training_load"])
async def test_invalid_dates_are_tool_errors(server, tool):
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

    with pytest.raises(ToolError) as info:
        await server.call_tool(tool, {"start": "yesterday", "end": "2026-11-11"})
    assert not isinstance(info.value, UnexpectedToolError)


def test_stance_has_no_personal_details():
    from trainer.stance import COACH_STANCE

    assert "Germany" not in COACH_STANCE
    assert "profile.md" in COACH_STANCE
    assert "Onboarding" in COACH_STANCE

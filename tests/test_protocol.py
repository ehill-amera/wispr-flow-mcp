"""Starts the real server as a subprocess and talks MCP to it over stdio."""

import json
import os
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {
    "search_dictations", "recent_dictations", "get_dictation", "dictation_stats",
    "search_dictionary", "list_dictionary", "search_notes", "get_note",
}


def run_session(db_path, errlog_path, body):
    async def main():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", "from wispr_flow_mcp import main; main()"],
            env={**os.environ, "WISPR_FLOW_DB": str(db_path)},
        )
        with open(errlog_path, "w") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await body(session)

    return anyio.run(main)


def payload(result):
    assert not result.isError if hasattr(result, "isError") else not result.is_error
    return json.loads(result.content[0].text)


def test_lists_exactly_the_eight_tools(fixture_db, tmp_path):
    async def body(session):
        return {t.name for t in (await session.list_tools()).tools}

    assert run_session(fixture_db, tmp_path / "err.log", body) == EXPECTED_TOOLS


def test_null_rows_work_through_the_protocol(fixture_db, tmp_path):
    async def body(session):
        recent = payload(await session.call_tool("recent_dictations", {"limit": 100}))
        stats = payload(await session.call_tool("dictation_stats", {"group_by": "app"}))
        return recent, stats

    recent, stats = run_session(fixture_db, tmp_path / "err.log", body)
    assert recent["results"][-1]["id"] == "no-time"
    assert stats["history_window"]["total_dictations"] == 8


def test_server_writes_no_dictation_text_to_stderr(fixture_db, tmp_path):
    errlog = tmp_path / "err.log"

    async def body(session):
        await session.call_tool("search_dictations", {"query": "alpha"})
        await session.call_tool("get_dictation", {"id": "newest"})
        await session.call_tool("get_dictation", {"id": "missing"})

    run_session(fixture_db, errlog, body)
    logged = errlog.read_text()
    assert "alpha" not in logged
    assert "zebra" not in logged


def test_network_is_refused_inside_the_server_process():
    code = (
        "import socket\n"
        "from wispr_flow_mcp.local_only import refuse_network\n"
        "refuse_network()\n"
        "s = socket.socket()\n"
        "try:\n"
        "    s.connect(('127.0.0.1', 9))\n"
        "except OSError as e:\n"
        "    print('refused' if 'local only' in str(e) else 'other')\n"
    )
    import subprocess
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "refused"


def test_main_turns_on_the_network_refusal_before_serving():
    code = (
        "import socket\n"
        "import wispr_flow_mcp.server as server\n"
        "server.mcp.run = lambda *_: None\n"
        "server.main()\n"
        "try:\n"
        "    socket.socket().connect(('127.0.0.1', 9))\n"
        "except OSError as e:\n"
        "    print('refused' if 'local only' in str(e) else 'other')\n"
    )
    import subprocess
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "refused"

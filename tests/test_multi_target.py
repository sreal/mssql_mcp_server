"""Multi-target mode: one server process, several named databases (MSSQL_TARGETS)."""
import os
from unittest.mock import MagicMock, patch

import pytest

from mssql_mcp_server.server import (
    allow_writes,
    call_tool,
    get_command,
    get_db_config,
    get_targets,
    list_resources,
    list_tools,
    write_enable_command,
)

TWO_TARGETS = {
    "MSSQL_TARGETS": "alpha, beta",
    "MSSQL_ALPHA_SERVER": "alpha.database.windows.net",
    "MSSQL_ALPHA_DATABASE": "alphadb",
    "MSSQL_ALPHA_USER": "alpha_user",
    "MSSQL_ALPHA_PASSWORD": "alpha_pw",
    "MSSQL_BETA_SERVER": "beta.database.windows.net",
    "MSSQL_BETA_DATABASE": "betadb",
    "MSSQL_BETA_USER": "beta_user",
    "MSSQL_BETA_PASSWORD": "beta_pw",
    "MSSQL_BETA_QUERY_TIMEOUT": "0",
    "MSSQL_BETA_CONN_PROPERTIES": "SET ANSI_NULLS ON;",
}


def fake_connection(columns=("x",), rows=((1,),)):
    cursor = MagicMock()
    cursor.description = [(name,) for name in columns]
    cursor.fetchall.return_value = list(rows)
    cursor.rowcount = 0
    connection = MagicMock()
    connection.cursor.return_value = cursor
    return connection


def test_targets_parsed_and_normalised():
    with patch.dict(os.environ, TWO_TARGETS, clear=True):
        assert get_targets() == ["alpha", "beta"]


def test_no_targets_means_single_target_mode():
    with patch.dict(os.environ, {}, clear=True):
        assert get_targets() == []


def test_invalid_target_name_rejected():
    with patch.dict(os.environ, {"MSSQL_TARGETS": "ok,bad-name"}, clear=True), \
         pytest.raises(ValueError, match="Invalid target name"):
        get_targets()


def test_each_target_reads_its_own_settings():
    with patch.dict(os.environ, TWO_TARGETS, clear=True):
        alpha = get_db_config("alpha")
        beta = get_db_config("beta")
    assert (alpha["server"], alpha["database"], alpha["user"], alpha["timeout"]) == \
        ("alpha.database.windows.net", "alphadb", "alpha_user", 300)
    assert "conn_properties" not in alpha
    assert (beta["server"], beta["database"], beta["user"], beta["timeout"]) == \
        ("beta.database.windows.net", "betadb", "beta_user", 0)
    assert beta["conn_properties"] == "SET ANSI_NULLS ON;"


def test_missing_target_settings_name_the_target():
    env = {k: v for k, v in TWO_TARGETS.items() if k != "MSSQL_BETA_PASSWORD"}
    with patch.dict(os.environ, env, clear=True), \
         pytest.raises(ValueError, match="for beta"):
        get_db_config("beta")


@pytest.mark.asyncio
async def test_tool_requires_server_from_target_list():
    with patch.dict(os.environ, TWO_TARGETS, clear=True):
        tool = (await list_tools())[0]
    assert tool.name == "query"
    assert tool.inputSchema["properties"]["server"]["enum"] == ["alpha", "beta"]
    assert tool.inputSchema["required"] == ["server", "query"]
    assert tool.annotations.readOnlyHint is True
    assert "alpha, beta" in tool.description


@pytest.mark.asyncio
async def test_query_connects_to_the_named_target():
    with patch.dict(os.environ, TWO_TARGETS, clear=True), \
         patch("mssql_mcp_server.server.pymssql.connect", return_value=fake_connection()) as connect:
        result = await call_tool("query", {"server": "beta", "query": "SELECT 1 AS x"})
    assert connect.call_args.kwargs["server"] == "beta.database.windows.net"
    assert connect.call_args.kwargs["database"] == "betadb"
    assert result[0].text == "x\n1"


@pytest.mark.asyncio
async def test_database_argument_still_overrides_within_target():
    with patch.dict(os.environ, TWO_TARGETS, clear=True), \
         patch("mssql_mcp_server.server.pymssql.connect", return_value=fake_connection()) as connect:
        await call_tool("query", {"server": "alpha", "query": "SELECT 1 AS x", "database": "otherdb"})
    assert connect.call_args.kwargs["server"] == "alpha.database.windows.net"
    assert connect.call_args.kwargs["database"] == "otherdb"


@pytest.mark.asyncio
@pytest.mark.parametrize("server_arg", [None, "gamma", ""])
async def test_missing_or_unknown_server_rejected(server_arg):
    arguments = {"query": "SELECT 1"}
    if server_arg is not None:
        arguments["server"] = server_arg
    with patch.dict(os.environ, TWO_TARGETS, clear=True), \
         patch("mssql_mcp_server.server.pymssql.connect") as connect, \
         pytest.raises(ValueError, match="server must be one of: alpha, beta"):
        await call_tool("query", arguments)
    connect.assert_not_called()


@pytest.mark.asyncio
async def test_read_only_by_default_on_every_target():
    with patch.dict(os.environ, TWO_TARGETS, clear=True), \
         patch("mssql_mcp_server.server.pymssql.connect") as connect:
        result = await call_tool("query", {"server": "alpha", "query": "DELETE FROM dbo.t"})
    assert "only SELECT queries are allowed" in result[0].text
    connect.assert_not_called()


@pytest.mark.asyncio
async def test_per_target_write_switch_only_opens_that_target():
    env = {**TWO_TARGETS, "MSSQL_BETA_ALLOW_WRITES": "1"}
    with patch.dict(os.environ, env, clear=True):
        assert allow_writes("beta") is True
        assert allow_writes("alpha") is False
        assert get_command() == "execute_sql"
        tool = (await list_tools())[0]
        assert tool.annotations is None
        assert "WARNING: writes are enabled on: beta." in tool.description
        assert "Read-only (SELECT only) on: alpha." in tool.description
        with patch("mssql_mcp_server.server.pymssql.connect") as connect:
            blocked = await call_tool("execute_sql", {"server": "alpha", "query": "DELETE FROM dbo.t"})
            connect.assert_not_called()
        assert "only SELECT queries are allowed" in blocked[0].text
        with patch("mssql_mcp_server.server.pymssql.connect", return_value=fake_connection()) as connect:
            allowed = await call_tool("execute_sql", {"server": "beta", "query": "DELETE FROM dbo.t WHERE 1 = 0"})
        assert connect.call_args.kwargs["server"] == "beta.database.windows.net"
        assert "Query executed successfully" in allowed[0].text


def test_global_write_switch_opens_every_target():
    with patch.dict(os.environ, {**TWO_TARGETS, "MSSQL_ALLOW_WRITES": "1"}, clear=True):
        assert allow_writes("alpha") and allow_writes("beta")


@pytest.mark.asyncio
async def test_resources_are_empty_in_multi_target_mode():
    with patch.dict(os.environ, TWO_TARGETS, clear=True), \
         patch("mssql_mcp_server.server.pymssql.connect") as connect:
        assert await list_resources() == []
    connect.assert_not_called()


@pytest.mark.asyncio
async def test_read_only_tool_description_says_how_to_enable_writes():
    env = {**TWO_TARGETS, "MSSQL_RESTART_COMMAND": "cw --continue"}
    with patch.dict(os.environ, env, clear=True):
        tool = (await list_tools())[0]
    assert "Read-only (SELECT only) on: alpha, beta." in tool.description
    assert "`MSSQL_<SERVER>_ALLOW_WRITES=1 cw --continue`" in tool.description
    assert "MSSQL_ALLOW_WRITES=1 instead allows writes on every server" in tool.description


@pytest.mark.asyncio
async def test_blocked_write_names_the_command_and_warns():
    env = {**TWO_TARGETS, "MSSQL_RESTART_COMMAND": "cw --continue"}
    with patch.dict(os.environ, env, clear=True), \
         patch("mssql_mcp_server.server.pymssql.connect") as connect:
        result = await call_tool("query", {"server": "beta", "query": "UPDATE dbo.t SET x = 1"})
    connect.assert_not_called()
    text = result[0].text
    assert text.startswith("Error: beta is in read-only mode: only SELECT queries are allowed. Nothing was run.")
    assert "`MSSQL_BETA_ALLOW_WRITES=1 cw --continue`" in text
    assert "live database" in text


def test_restart_command_defaults_to_claude_continue():
    with patch.dict(os.environ, TWO_TARGETS, clear=True):
        assert write_enable_command("beta") == "MSSQL_BETA_ALLOW_WRITES=1 claude --continue"
        assert write_enable_command() == "MSSQL_ALLOW_WRITES=1 claude --continue"


def test_restart_dir_and_session_are_part_of_the_command():
    env = {**TWO_TARGETS, "MSSQL_RESTART_COMMAND": "cw --resume abc-123", "MSSQL_RESTART_DIR": "/work/my project"}
    with patch.dict(os.environ, env, clear=True):
        assert write_enable_command("beta") == \
            "cd '/work/my project' && MSSQL_BETA_ALLOW_WRITES=1 cw --resume abc-123"

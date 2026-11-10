"""Tests for the local fork additions: database argument, CSV escaping, conn_properties."""
import os
from unittest.mock import MagicMock, patch

import pytest

from mssql_mcp_server.server import call_tool, format_rows, get_db_config, list_tools

BASE_ENV = {
    "MSSQL_SERVER": "myserver.database.windows.net",
    "MSSQL_USER": "testuser",
    "MSSQL_PASSWORD": "testpass",
    "MSSQL_DATABASE": "defaultdb",
}


def make_fake_connection(columns, rows):
    cursor = MagicMock()
    cursor.description = [(name,) for name in columns]
    cursor.fetchall.return_value = rows
    connection = MagicMock()
    connection.cursor.return_value = cursor
    return connection


def test_format_rows_quotes_commas_quotes_and_newlines():
    text = format_rows(["id", "note"], [(1, "a,b"), (2, 'say "hi"'), (3, "line1\nline2"), (4, None)])
    assert text == 'id,note\n1,"a,b"\n2,"say ""hi"""\n3,"line1\nline2"\n4,None'


def test_format_rows_plain_values_unchanged():
    assert format_rows(["a", "b"], [(1, "x")]) == "a,b\n1,x"


@pytest.mark.asyncio
async def test_list_tools_offers_optional_database_argument():
    with patch.dict(os.environ, BASE_ENV, clear=True):
        tool = (await list_tools())[0]
    assert "database" in tool.inputSchema["properties"]
    assert tool.inputSchema["required"] == ["query"]


@pytest.mark.asyncio
async def test_database_argument_overrides_configured_database():
    connection = make_fake_connection(["name"], [("x",)])
    with patch.dict(os.environ, BASE_ENV, clear=True), \
         patch("mssql_mcp_server.server.pymssql.connect", return_value=connection) as connect:
        result = await call_tool("query", {"query": "SELECT name FROM sys.tables", "database": "otherdb"})
    assert connect.call_args.kwargs["database"] == "otherdb"
    assert result[0].text == "name\nx"


@pytest.mark.asyncio
async def test_information_schema_tables_returns_all_columns():
    """Regression: queries on INFORMATION_SCHEMA.TABLES used to return only the first column."""
    connection = make_fake_connection(
        ["TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME"],
        [("mydb", "dbo", "Brand"), ("mydb", "AU", "Metric")],
    )
    with patch.dict(os.environ, BASE_ENV, clear=True), \
         patch("mssql_mcp_server.server.pymssql.connect", return_value=connection):
        result = await call_tool(
            "query", {"query": "SELECT TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES"}
        )
    assert result[0].text == "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME\nmydb,dbo,Brand\nmydb,AU,Metric"


@pytest.mark.asyncio
async def test_without_database_argument_uses_configured_database():
    connection = make_fake_connection(["name"], [("x",)])
    with patch.dict(os.environ, BASE_ENV, clear=True), \
         patch("mssql_mcp_server.server.pymssql.connect", return_value=connection) as connect:
        await call_tool("query", {"query": "SELECT name FROM sys.tables"})
    assert connect.call_args.kwargs["database"] == "defaultdb"


@pytest.mark.asyncio
async def test_blank_database_argument_is_rejected():
    with patch.dict(os.environ, BASE_ENV, clear=True), \
         pytest.raises(ValueError, match="database must be a non-empty string"):
        await call_tool("query", {"query": "SELECT 1", "database": "  "})


@pytest.mark.asyncio
async def test_read_only_gate_still_applies_with_database_argument():
    with patch.dict(os.environ, BASE_ENV, clear=True), \
         patch("mssql_mcp_server.server.pymssql.connect") as connect:
        result = await call_tool("query", {"query": "DROP TABLE dbo.t", "database": "otherdb"})
    assert "only SELECT queries are allowed" in result[0].text
    connect.assert_not_called()


def test_conn_properties_passed_through_when_set():
    with patch.dict(os.environ, {**BASE_ENV, "MSSQL_CONN_PROPERTIES": "SET ANSI_NULLS ON;"}, clear=True):
        assert get_db_config()["conn_properties"] == "SET ANSI_NULLS ON;"


def test_conn_properties_absent_by_default():
    with patch.dict(os.environ, BASE_ENV, clear=True):
        assert "conn_properties" not in get_db_config()

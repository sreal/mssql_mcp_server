import asyncio
import contextlib
import csv
import io
import logging
import os
import re
import shlex
from typing import Any

import pymssql
from mcp.server import Server
from mcp.types import Resource, TextContent, Tool, ToolAnnotations
from pydantic import AnyUrl
from sqlglot import exp, parse
from sqlglot.errors import ErrorLevel

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mssql_mcp_server")

def sanitize_error_message(message: str) -> str:
    """Redact obvious credential material from error strings returned to clients."""
    # Matches: password='x', password "x", password: x, password 'x', with password 'x'
    message = re.sub(
        r"(?i)(password\s*[=:]?\s*)(['\"])([^'\"]+)\2",
        r"\1\2***\2",
        message,
    )
    message = re.sub(
        r"(?i)(password\s*[=:]\s*)(\S+)",
        r"\1***",
        message,
    )
    message = re.sub(
        r"(?i)(pwd\s*[=:]?\s*)(['\"])([^'\"]+)\2",
        r"\1\2***\2",
        message,
    )
    return message


def validate_table_name(table_name: str) -> str:
    """Validate and escape table name to prevent SQL injection."""
    # Allow only alphanumeric, underscore, and dot (for schema.table)
    if not re.match(r'^[a-zA-Z0-9_]+(\.[a-zA-Z0-9_]+)?$', table_name):
        raise ValueError(f"Invalid table name: {table_name}")
    
    # Split schema and table if present
    parts = table_name.split('.')
    if len(parts) == 2:
        # Escape both schema and table name
        return f"[{parts[0]}].[{parts[1]}]"
    else:
        # Just table name
        return f"[{table_name}]"

def _is_true(value) -> bool:
    return str(value).lower() in ("1", "true", "yes")

def get_targets() -> list[str]:
    """Named database targets from MSSQL_TARGETS (e.g. "australia,synapse").

    Empty means single-target mode: one database from the plain MSSQL_* variables.
    With targets, each one reads MSSQL_<TARGET>_SERVER, _DATABASE, _USER, _PASSWORD, etc.
    and the query tool takes a required ``server`` argument naming the target.
    """
    targets = []
    for name in os.getenv("MSSQL_TARGETS", "").split(","):
        name = name.strip().lower()
        if not name:
            continue
        if not re.match(r"^[a-z0-9_]+$", name):
            raise ValueError(f"Invalid target name in MSSQL_TARGETS: {name!r} (use a-z, 0-9, _)")
        targets.append(name)
    return targets

def _env_prefix(target: str | None) -> str:
    return "MSSQL_" if target is None else f"MSSQL_{target.upper()}_"

def allow_writes(target: str | None = None) -> bool:
    """Whether non-SELECT statements are permitted (default: read-only).

    MSSQL_ALLOW_WRITES enables writes everywhere; MSSQL_<TARGET>_ALLOW_WRITES enables one target.
    """
    if _is_true(os.getenv("MSSQL_ALLOW_WRITES", "false")):
        return True
    return target is not None and _is_true(os.getenv(f"{_env_prefix(target)}ALLOW_WRITES", "false"))

def any_writes() -> bool:
    """Whether writes are enabled for at least one target (decides the tool name and hints)."""
    return allow_writes() or any(allow_writes(t) for t in get_targets())

def get_db_config(target: str | None = None):
    """Get database configuration from environment variables (MSSQL_* or MSSQL_<TARGET>_*)."""
    prefix = _env_prefix(target)
    label = target or "default"
    # Basic configuration
    server = os.getenv(prefix + "SERVER", "localhost")
    logger.info(f"[{label}] {prefix}SERVER environment variable: {os.getenv(prefix + 'SERVER', 'NOT SET')}")
    logger.info(f"Using server: {server}")
    
    # Handle LocalDB connections (Issue #6)
    # LocalDB format: (localdb)\instancename
    if server.startswith("(localdb)\\"):
        # For LocalDB, pymssql needs special formatting
        # Convert (localdb)\MSSQLLocalDB to localhost\MSSQLLocalDB with dynamic port
        instance_name = server.replace("(localdb)\\", "")
        server = f".\\{instance_name}"
        logger.info(f"Detected LocalDB connection, converted to: {server}")
    
    config: dict[str, Any] = {
        "server": server,
        "user": os.getenv(prefix + "USER"),
        "password": os.getenv(prefix + "PASSWORD"),
        "database": os.getenv(prefix + "DATABASE"),
    }
    # Port support (Issue #8) — only set when explicitly configured
    port = os.getenv(prefix + "PORT")
    if port:
        try:
            config["port"] = int(port)
        except ValueError:
            logger.warning(f"Invalid MSSQL_PORT value: {port}. Using default port.")
    
    # Encryption settings (Issues #11, #33, #34)
    # pymssql.connect() accepts `encryption` (not `encrypt`); values:
    # 'default' | 'off' | 'request' | 'require'
    if config["server"] and ".database.windows.net" in config["server"]:
        config["tds_version"] = "7.4"  # Required for Azure SQL
        # Azure SQL requires encryption
        if os.getenv(prefix + "ENCRYPT", "true").lower() == "true":
            config["encryption"] = "require"
    else:
        # For non-Azure connections, respect the MSSQL_ENCRYPT setting
        encrypt_str = os.getenv(prefix + "ENCRYPT", "false")
        if encrypt_str.lower() == "true":
            config["encryption"] = "require"
        else:
            config["encryption"] = "off"

    # Trust server certificate (Issue #39)
    # FreeTDS/pymssql do not expose a first-class kwarg; when requested we append
    # the ODBC-style option that many FreeTDS builds honor on the server string.
    trust_env = os.getenv(prefix + "TRUST_SERVER_CERTIFICATE")
    is_azure = bool(config["server"]) and ".database.windows.net" in str(config["server"]).split(";")[0]
    if trust_env is None and config.get("encryption") == "require" and not is_azure:
        # On-prem encrypt often needs trust. Azure gets no suffix: its certs are valid,
        # and FreeTDS treats "host;TrustServerCertificate=no" as the host name, so the
        # connection fails ("failed for an unknown reason").
        trust_env = "true"
    if trust_env is not None:
        flag = "yes" if trust_env.lower() in ("1", "true", "yes") else "no"
        base_server = str(config["server"]).split(";")[0]
        config["server"] = f"{base_server};TrustServerCertificate={flag}"
            
    # Windows Authentication support (Issue #7)
    use_windows_auth = os.getenv(prefix + "WINDOWS_AUTH", "false").lower() == "true"
    
    if use_windows_auth:
        # For Windows authentication, user and password are not required
        if not config["database"]:
            logger.error(f"{prefix}DATABASE is required")
            raise ValueError(f"Missing required database configuration for {label}")
        # Remove user and password for Windows auth
        config.pop("user", None)
        config.pop("password", None)
        logger.info("Using Windows Authentication")
    else:
        # SQL Authentication - user and password are required
        if not all([config["user"], config["password"], config["database"]]):
            logger.error("Missing required database configuration. Please check environment variables:")
            logger.error(f"{prefix}USER, {prefix}PASSWORD, and {prefix}DATABASE are required")
            raise ValueError(f"Missing required database configuration for {label}")

    # Query timeout support (Issue #40 / PR #41)
    try:
        config["timeout"] = int(os.getenv(prefix + "QUERY_TIMEOUT", "300"))
    except ValueError:
        logger.warning("Invalid MSSQL_QUERY_TIMEOUT; using default 300 seconds")
        config["timeout"] = 300

    # Session SET options run on connect. pymssql's default batch is rejected by
    # Azure Synapse ("Could not set connection properties"), so allow overriding it.
    conn_properties = os.getenv(prefix + "CONN_PROPERTIES")
    if conn_properties is not None:
        config["conn_properties"] = conn_properties

    # Safety net: never pass unsupported `encrypt` kwarg to pymssql (PR #38 / issues #33/#34)
    config.pop("encrypt", None)
    
    return config

def format_rows(columns, rows) -> str:
    """Render a result set as CSV. Values with commas, quotes or newlines are quoted."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([str(value) for value in row])
    return buffer.getvalue().rstrip("\n")

def get_command():
    """Get the MCP tool name (query for read-only, execute_sql for write mode)."""
    override = os.getenv("MSSQL_COMMAND")
    if override:
        return override
    return "execute_sql" if any_writes() else "query"

# Statement roots permitted in read-only mode (fail-closed otherwise).
_READ_ONLY_ROOTS = (
    exp.Select,
    exp.Union,
    exp.Except,
    exp.Intersect,
)

# Function / procedure names that must not appear even inside an otherwise-SELECT AST.
# Defense in depth: parser gaps should not silently allow these.
_BLOCKED_NAME_FRAGMENTS = frozenset({
    "OPENROWSET",
    "OPENDATASOURCE",
    "OPENQUERY",
    "OPENXML",
    "XP_CMDSHELL",
    "SP_EXECUTESQL",
    "SP_OACREATE",
    "SP_OAMETHOD",
})


def _expression_name(node: exp.Expr) -> str | None:
    """Best-effort identifier/function name from a sqlglot node."""
    this = getattr(node, "this", None)
    if isinstance(this, str):
        return this
    if isinstance(this, exp.Identifier):
        return this.name
    if isinstance(this, exp.Expression) and hasattr(this, "name"):
        return this.name
    if hasattr(node, "name") and isinstance(node.name, str):
        return node.name
    return None


def _contains_blocked_name(tree: exp.Expression) -> bool:
    for node in tree.walk():
        name = _expression_name(node)
        if name and name.upper() in _BLOCKED_NAME_FRAGMENTS:
            return True
        # Also inspect raw SQL fragments for Command nodes
        if isinstance(node, (exp.Command, exp.Execute)):
            return True
    return False


def is_select_query(query: str) -> bool:
    """
    Fail-closed check: allow only a single read-only SELECT/WITH/set-operation.

    Uses sqlglot's T-SQL parser (not a prefix regex). Rejects stacked statements,
    SELECT INTO, DML/DDL/EXEC, and known dangerous table-valued helpers when the
    AST names them. Parse failures reject the query.

    Limitations (by design — prefer DB least-privilege as defense in depth):
    - Does not claim to prove arbitrary T-SQL is free of side effects.
    - Dialect/parser gaps fail closed (query denied) rather than allowed.
    - ``MSSQL_ALLOW_WRITES`` bypasses this gate entirely for opt-in write mode.
    """
    if not isinstance(query, str) or not query.strip():
        return False

    try:
        expressions = parse(query, read="tsql", error_level=ErrorLevel.RAISE)
    except Exception:  # noqa: BLE001 — fail closed on any parse/tokenize failure
        logger.info("Read-only gate: rejecting unparseable / unsupported T-SQL")
        return False

    # Drop empty placeholders and trailing semicolon markers.
    statements = [
        node
        for node in expressions
        if node is not None and not isinstance(node, exp.Semicolon)
    ]
    if len(statements) != 1:
        return False

    tree = statements[0]
    if not isinstance(tree, _READ_ONLY_ROOTS):
        return False

    # SELECT INTO writes a table — never read-only.
    if any(isinstance(node, exp.Into) for node in tree.find_all(exp.Into)):
        return False
    if isinstance(tree, exp.Select) and tree.args.get("into") is not None:
        return False

    return not _contains_blocked_name(tree)

# Initialize server
app = Server("mssql_mcp_server")

@app.list_resources()
async def list_resources() -> list[Resource]:
    """List SQL Server tables as resources (single-target mode only)."""
    if get_targets():
        return []
    config = get_db_config()
    conn = None
    cursor = None
    try:
        conn = pymssql.connect(**config)
        cursor = conn.cursor()
        # Query to get user tables from the current database
        cursor.execute("""
            SELECT TABLE_NAME 
            FROM INFORMATION_SCHEMA.TABLES 
            WHERE TABLE_TYPE = 'BASE TABLE'
        """)
        tables = cursor.fetchall()
        logger.info(f"Found tables: {tables}")
        
        resources = []
        for table in tables:
            resources.append(
                Resource(
                    uri=AnyUrl(f"mssql://{table[0]}/data"),
                    name=f"Table: {table[0]}",
                    mimeType="text/plain",
                    description=f"Data in table: {table[0]}"
                )
            )
        return resources
    except Exception as e:  # noqa: BLE001 — return empty list to client on any DB/listing failure
        logger.error(f"Failed to list resources: {e!s}")
        return []
    finally:
        if cursor is not None:
            with contextlib.suppress(Exception):
                cursor.close()
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()

@app.read_resource()
async def read_resource(uri: AnyUrl) -> str:
    """Read table contents."""
    if get_targets():
        raise ValueError("Resources are not available when MSSQL_TARGETS is set; use the query tool")
    config = get_db_config()
    uri_str = str(uri)
    logger.info(f"Reading resource: {uri_str}")
    
    if not uri_str.startswith("mssql://"):
        raise ValueError(f"Invalid URI scheme: {uri_str}")
        
    parts = uri_str[8:].split('/')
    table = parts[0]
    
    try:
        # Validate table name to prevent SQL injection
        safe_table = validate_table_name(table)
        
        conn = pymssql.connect(**config)
        cursor = conn.cursor()
        # Use TOP 100 for MSSQL (equivalent to LIMIT in MySQL)
        cursor.execute(f"SELECT TOP 100 * FROM {safe_table}")
        if cursor.description is None:
            raise RuntimeError("Query did not return a result set")
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return format_rows(columns, rows)
                
    except pymssql.OperationalError as e:
        if "timed out" in str(e).lower():
            logger.error(
                "Query timed out after %s seconds reading resource %s",
                os.getenv("MSSQL_QUERY_TIMEOUT", "300"),
                uri_str,
            )
            raise RuntimeError(
                "Query timed out and was terminated. Please narrow the query and try again."
            ) from e
        logger.error(f"Database error reading resource {uri}: {e!s}")
        raise RuntimeError(f"Database error: {e!s}") from e
    except Exception as e:
        logger.error(f"Database error reading resource {uri}: {e!s}")
        raise RuntimeError(f"Database error: {e!s}") from e

LIVE_DATABASE_WARNING = (
    "Writes run against the live database: show the user the exact statement and get "
    "their confirmation before running it."
)

def write_enable_command(target: str | None = None) -> str:
    """The shell command that restarts the client with writes allowed on target (all if None).

    MSSQL_RESTART_COMMAND is how the user starts their MCP client (default: "claude --continue").
    The write switch is read from the environment, so it only changes when the client restarts.
    MSSQL_RESTART_DIR, when set, is prepended as "cd <dir> &&" (resuming a session needs its directory).
    """
    restart = os.getenv("MSSQL_RESTART_COMMAND", "").strip() or "claude --continue"
    command = f"{_env_prefix(target)}ALLOW_WRITES=1 {restart}"
    restart_dir = os.getenv("MSSQL_RESTART_DIR", "").strip()
    if restart_dir:
        command = f"cd {shlex.quote(restart_dir)} && {command}"
    return command

def write_enable_hint(target: str | None = None) -> str:
    """Tell the user how to allow writes; used when a server is read-only."""
    where = f" on {target}" if target else ""
    return (
        f"To allow writes{where}, the user must exit and restart with: "
        f"`{write_enable_command(target)}`. {LIVE_DATABASE_WARNING}"
    )

def _tool_description(targets: list[str]) -> str:
    if not targets:
        if allow_writes():
            return f"Execute an SQL query on the SQL Server. WARNING: writes are enabled. {LIVE_DATABASE_WARNING}"
        return f"Run a read-only SQL query (SELECT only). {write_enable_hint()}"
    parts = [f"Run an SQL query on one of these SQL Servers: {', '.join(targets)}."]
    writable = [t for t in targets if allow_writes(t)]
    read_only = [t for t in targets if not allow_writes(t)]
    if writable:
        parts.append(f"WARNING: writes are enabled on: {', '.join(writable)}. {LIVE_DATABASE_WARNING}")
    if read_only:
        parts.append(
            f"Read-only (SELECT only) on: {', '.join(read_only)}. If the user needs a write there, "
            f"tell them to exit and restart with `{write_enable_command('<server>')}` "
            f"(MSSQL_ALLOW_WRITES=1 instead allows writes on every server)."
        )
    return " ".join(parts)

def _server_property(targets: list[str]) -> dict:
    if not targets:
        return {}
    return {"server": {
        "type": "string",
        "enum": targets,
        "description": "Which configured SQL Server to run the query on.",
    }}

@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available SQL Server tools."""
    command = get_command()
    targets = get_targets()
    logger.info("Listing tools...")
    annotations = None
    if not any_writes():
        annotations = ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    return [
        Tool(
            name=command,
            description=_tool_description(targets),
            inputSchema={
                "type": "object",
                "properties": {
                    **_server_property(targets),
                    "query": {
                        "type": "string",
                        "description": "The SQL query to execute"
                    },
                    "database": {
                        "type": "string",
                        "description": (
                            "Optional database to run the query in. Defaults to the "
                            "configured MSSQL_DATABASE. Use this instead of three-part "
                            "names, which Azure SQL Database does not support."
                        ),
                    },
                },
                "required": (["server", "query"] if targets else ["query"])
            },
            annotations=annotations,
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Execute SQL commands."""
    command = get_command()
    logger.info(f"Calling tool: {name} with arguments: {arguments}")
    
    if name != command:
        raise ValueError(f"Unknown tool: {name}")
    
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Query is required")

    target = None
    targets = get_targets()
    if targets:
        target = arguments.get("server")
        if target not in targets:
            raise ValueError(f"server must be one of: {', '.join(targets)} (got {target!r})")

    if not allow_writes(target) and not is_select_query(query):
        read_only_name = f"{target} is in read-only mode" if target else "This server is in read-only mode"
        return [TextContent(
            type="text",
            text=(
                f"Error: {read_only_name}: only SELECT queries are allowed. Nothing was run. "
                + write_enable_hint(target)
            ),
        )]

    config = get_db_config(target)
    database = arguments.get("database")
    if database is not None:
        if not isinstance(database, str) or not database.strip():
            raise ValueError("database must be a non-empty string when given")
        config["database"] = database.strip()

    conn = None
    cursor = None
    try:
        conn = pymssql.connect(**config)
        cursor = conn.cursor()
        cursor.execute(query)
        
        # SELECT queries return every column. (Upstream special-cased INFORMATION_SCHEMA.TABLES
        # and returned only the first column, which silently dropped data.)
        if is_select_query(query):
            if cursor.description is None:
                raise RuntimeError("Query did not return a result set")
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            return [TextContent(type="text", text=format_rows(columns, rows))]
        
        # Non-SELECT queries
        else:
            conn.commit()
            affected_rows = cursor.rowcount
            return [TextContent(type="text", text=f"Query executed successfully. Rows affected: {affected_rows}")]

    except pymssql.OperationalError as e:
        if "timed out" in str(e).lower():
            logger.error(
                "Query timed out after %s seconds: %s",
                config.get("timeout"),
                query[:200],
            )
            return [TextContent(
                type="text",
                text="Query timed out and was terminated. Please narrow the query and try again.",
            )]
        logger.error(f"Error executing SQL '{query}': {e}")
        return [TextContent(type="text", text=f"Error executing query: {sanitize_error_message(str(e))}")]
    except Exception as e:  # noqa: BLE001 — return sanitized error message to client
        logger.error(f"Error executing SQL '{query}': {e}")
        return [TextContent(type="text", text=f"Error executing query: {sanitize_error_message(str(e))}")]
    finally:
        if cursor is not None:
            with contextlib.suppress(Exception):
                cursor.close()
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()

async def main():
    """Main entry point to run the MCP server."""
    from mcp.server.stdio import stdio_server
    
    logger.info("Starting MSSQL MCP server...")
    # Fail fast if any target is misconfigured. Log without exposing sensitive data.
    for target in (get_targets() or [None]):
        config = get_db_config(target)
        server_info = str(config['server']).split(';')[0]
        if 'port' in config:
            server_info += f":{config['port']}"
        user_info = config.get('user', 'Windows Auth')
        logger.info(f"[{target or 'default'}] Database config: {server_info}/{config['database']} as {user_info}")
        logger.info("[%s] Write mode: %s", target or "default", "enabled" if allow_writes(target) else "read-only")
    
    async with stdio_server() as (read_stream, write_stream):
        try:
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options()
            )
        except Exception:
            logger.exception("Server error")
            raise

if __name__ == "__main__":
    asyncio.run(main())

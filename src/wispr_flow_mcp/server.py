"""MCP server over stdio. Local only: outbound connections are refused at the socket layer."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from . import tools
from .local_only import refuse_network

# Above INFO, so request handling is never logged.
mcp = MCPServer("wispr-flow", log_level="ERROR")


@mcp.tool()
def search_dictations(
    query: str,
    since: str | None = None,
    until: str | None = None,
    app: str | None = None,
    limit: int = 25,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Search Wispr Flow dictations, newest first, case-insensitive.

    Covers the permanent archive plus the live Wispr file. Rows with source "recovered" came from deleted
    data, and possibly_damaged means parts may be wrong or missing, so say so when using one.
    Searches what was pasted, falling back to the edited, cleaned, then raw text. since and until are
    ISO dates or datetimes in local time unless they carry an offset. app matches part of the
    app identifier, such as "chrome". Zero results include history_window: say how far back the
    history goes rather than only that nothing was found.
    """
    return tools.search_dictations(query, since, until, app, limit, include_archived)


@mcp.tool()
def recent_dictations(
    limit: int = 25, since: str | None = None, app: str | None = None, include_archived: bool = False
) -> dict[str, Any]:
    """List recent dictations that have text, newest first. Rows without a timestamp come last."""
    return tools.recent_dictations(limit, since, app, include_archived)


@mcp.tool()
def get_dictation(id: str, include_archived: bool = False) -> dict[str, Any]:
    """Get one dictation with every text version side by side, plus whether it is recovered or possibly damaged."""
    return tools.get_dictation(id, include_archived)


@mcp.tool()
def dictation_stats(
    since: str | None = None,
    until: str | None = None,
    group_by: Literal["day", "app"] = "day",
    include_archived: bool = False,
) -> dict[str, Any]:
    """Count dictations and words by local day or by app, with the full range held."""
    return tools.dictation_stats(since, until, group_by, include_archived)


@mcp.tool()
def search_dictionary(query: str, limit: int = 50) -> dict[str, Any]:
    """Search Wispr Flow's personal dictionary by phrase or replacement, most used first."""
    return tools.search_dictionary(query, limit)


@mcp.tool()
def list_dictionary(limit: int = 50, starred_only: bool = False) -> dict[str, Any]:
    """List Wispr Flow's personal dictionary, most used first."""
    return tools.list_dictionary(limit, starred_only)


@mcp.tool()
def search_notes(query: str, limit: int = 25) -> dict[str, Any]:
    """Search Wispr Flow notes by title or content, most recently modified first. Returns snippets."""
    return tools.search_notes(query, limit)


@mcp.tool()
def get_note(id: str) -> dict[str, Any]:
    """Get one Wispr Flow note in full."""
    return tools.get_note(id)


def main() -> None:
    refuse_network()
    mcp.run("stdio")

"""Browse tool for the OKF MCP Server.

Progressive disclosure over the bundle hierarchy (OKF v0.2 §8).
"""

from typing import Any, Dict, Optional

from fastmcp.exceptions import ToolError

from okf_mcp_server.src.knowledge.bundle import BundleError, list_directory
from okf_mcp_server.src.knowledge.paging import (
    CursorError,
    decode_cursor,
    encode_cursor,
)
from okf_mcp_server.src.knowledge.service import open_snapshot
from okf_mcp_server.utils.pylogger import get_python_logger

logger = get_python_logger()

MAX_PAGE_SIZE = 100


def browse_knowledge(
    path: str = "",
    page_size: int = 50,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """List one directory of the knowledge bundle.

    TOOL_NAME=browse_knowledge
    DISPLAY_NAME=Browse Knowledge
    USECASE=Discover what knowledge exists, one directory level at a time, when you do not know what to search for or a search missed
    INSTRUCTIONS=1. Call with no path to see the top level, 2. Call again with a returned directory path to go deeper, 3. Open a concept with get_knowledge, 4. Pass next_cursor to see more entries
    INPUT_DESCRIPTION=path (bundle-relative directory, default root, e.g. "policies"), page_size (1-100, default 50), cursor (next_cursor from a previous call)
    OUTPUT_DESCRIPTION=Dictionary with status, path, entries (directories with concept_count; concepts with concept_id, type, title, description, tags, status, trust_tier, stale), next_cursor and snapshot_id
    EXAMPLES=browse_knowledge(), browse_knowledge("policies"), browse_knowledge("policies", 20, "<next_cursor>")
    PREREQUISITES=None
    RELATED_TOOLS=search_knowledge, get_knowledge

    Listings are synthesized from concept frontmatter, never returned as a
    raw stored index, so entry filtering can be applied before output.

    Raises:
        ToolError: For invalid paths, cursors or when no snapshot is published.
    """
    snapshot = open_snapshot()
    size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    target = f"browse:{path.strip().strip('/')}"
    try:
        offset = decode_cursor(cursor, snapshot.id, target)
        entries = list_directory(snapshot.bundle, path)
    except (BundleError, CursorError) as e:
        raise ToolError(str(e)) from e

    page = entries[offset : offset + size]
    more = offset + size < len(entries)
    logger.info(
        f"browse_knowledge path={path!r} returned {len(page)} of {len(entries)}"
    )
    return {
        "status": "success",
        "operation": "browse_knowledge",
        "snapshot_id": snapshot.id,
        "path": path.strip().strip("/"),
        "total": len(entries),
        "entries": page,
        "next_cursor": encode_cursor(snapshot.id, target, offset + size)
        if more
        else None,
        "message": f"Listed {len(page)} of {len(entries)} entries",
    }

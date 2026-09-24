"""Request-scoped access to the published snapshot, shared by the MCP tools.

Every tool call resolves the snapshot once, so a publish during the call
cannot mix a bundle with another snapshot's index.
"""

from pathlib import Path

from fastmcp.exceptions import ToolError

from okf_mcp_server.src.knowledge.snapshot import (
    Snapshot,
    SnapshotError,
    current_snapshot,
)
from okf_mcp_server.src.settings import settings


def require_access() -> None:
    """Fail closed until per-user permission filtering exists.

    The pilot serves one trusted operator with authentication disabled. With
    OAuth enabled the template authenticates callers but does not pass their
    identity to tools (phase 5 gap), so no evidence may be returned.

    Raises:
        ToolError: When authentication is enabled.
    """
    if settings.ENABLE_AUTH:
        raise ToolError(
            "Knowledge access is disabled: per-user permission filtering is not "
            "implemented yet, so authenticated shared access fails closed."
        )


def open_snapshot() -> Snapshot:
    """Resolve the published snapshot for this request.

    Raises:
        ToolError: If no snapshot is published.
    """
    require_access()
    try:
        return current_snapshot(Path(settings.OKF_DATA_DIR))
    except SnapshotError as e:
        raise ToolError(str(e)) from e

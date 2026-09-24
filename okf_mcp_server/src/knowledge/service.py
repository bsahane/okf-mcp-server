"""Request-scoped access to the published snapshot, shared by the MCP tools.

Every tool call resolves the snapshot once, so a publish during the call
cannot mix a bundle with another snapshot's index.
"""

from functools import lru_cache
from pathlib import Path
from typing import Optional

from fastmcp.exceptions import ToolError

from okf_mcp_server.src.knowledge.embed import (
    Embedder,
    EmbeddingUnavailable,
    get_embedder,
)
from okf_mcp_server.src.knowledge.snapshot import (
    Snapshot,
    SnapshotError,
    current_snapshot,
)
from okf_mcp_server.src.settings import settings
from okf_mcp_server.utils.pylogger import get_python_logger

logger = get_python_logger()


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


def search_embedder() -> Optional[Embedder]:
    """The embedder for hybrid search, or None for keyword-only search.

    None when disabled with OKF_SEMANTIC_SEARCH=false, or when the model or
    its libraries are not installed (logged once, not an error).
    """
    if not settings.OKF_SEMANTIC_SEARCH:
        return None
    return _load_embedder()


@lru_cache(maxsize=1)
def _load_embedder() -> Optional[Embedder]:
    try:
        return get_embedder()
    except EmbeddingUnavailable as e:
        logger.warning(f"Semantic search unavailable, using keyword search: {e}")
        return None


def warm_up_search() -> None:
    """Load the embedding model before serving, so no request pays for it.

    A no-op when semantic search is disabled or unavailable.
    """
    embedder = search_embedder()
    if embedder is not None:
        embedder.encode(["warm-up"], "query")
        logger.info("Semantic search ready")

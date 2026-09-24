"""Pytest configuration and common fixtures."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from okf_mcp_server.src.settings import settings as app_settings


@pytest.fixture
def mock_settings():
    """Provide mock settings for testing."""
    settings = Mock()
    settings.MCP_HOST = "0.0.0.0"
    settings.MCP_PORT = 4000
    settings.MCP_TRANSPORT_PROTOCOL = "streamable-http"
    settings.PYTHON_LOG_LEVEL = "INFO"
    settings.MCP_SSL_KEYFILE = None
    settings.MCP_SSL_CERTFILE = None
    return settings


@pytest.fixture
def mock_logger():
    """Provide mock logger for testing."""
    logger = Mock()
    logger.info = Mock()
    logger.error = Mock()
    logger.warning = Mock()
    logger.debug = Mock()
    logger.critical = Mock()
    return logger


@pytest.fixture
def mock_fastmcp():
    """Provide mock FastMCP for testing."""
    mcp = Mock()
    mcp.tool = Mock()
    mcp.resource = Mock()
    mcp.prompt = Mock()
    mcp.http_app = Mock()
    return mcp


@pytest.fixture
def sample_error_response():
    """Provide sample error response for testing."""
    return {
        "status": "error",
        "error": "Test error message",
        "message": "Failed to perform operation",
    }


@pytest.fixture
def sample_success_response():
    """Provide sample success response for testing."""
    return {
        "status": "success",
        "operation": "test_operation",
        "result": 42,
        "message": "Operation completed successfully",
    }


@pytest.fixture
def mock_context():
    """Provide mock MCP context for testing."""
    context = Mock()
    context.info = Mock()
    context.error = Mock()
    context.warning = Mock()
    return context


@pytest.fixture
def mock_path():
    """Provide mock path for file operations."""
    mock_path_instance = Mock()
    mock_path_instance.parent = Mock()
    mock_path_instance.parent.__truediv__ = Mock(return_value=Mock())
    return mock_path_instance


@pytest.fixture
def mock_structlog():
    """Provide mock structlog for testing."""
    mock_structlog = Mock()
    mock_structlog.configure = Mock()
    mock_structlog.get_logger = Mock(return_value=Mock())
    mock_structlog.stdlib = Mock()
    mock_structlog.stdlib.LoggerFactory = Mock()
    mock_structlog.stdlib.BoundLogger = Mock()
    mock_structlog.stdlib.filter_by_level = Mock()
    mock_structlog.stdlib.add_logger_name = Mock()
    mock_structlog.stdlib.add_log_level = Mock()
    mock_structlog.stdlib.PositionalArgumentsFormatter = Mock()
    mock_structlog.processors = Mock()
    mock_structlog.processors.TimeStamper = Mock()
    mock_structlog.processors.StackInfoRenderer = Mock()
    mock_structlog.processors.format_exc_info = Mock()
    mock_structlog.processors.UnicodeDecoder = Mock()
    mock_structlog.processors.JSONRenderer = Mock()
    return mock_structlog


@pytest.fixture(autouse=True)
def mock_imports():
    """Mock external dependencies to avoid import errors during testing."""
    with patch.dict(
        "sys.modules",
        {
            "fastmcp": Mock(),
            "structlog": Mock(),
            "pydantic": Mock(),
            "pydantic_settings": Mock(),
            "fastapi": Mock(),
            "uvicorn": Mock(),
            "httpx": Mock(),
            "requests": Mock(),
        },
    ):
        yield


@pytest.fixture
def mock_app():
    """Provide mock FastAPI app for testing."""
    app = Mock()
    app.routes = [
        Mock(path="/health"),
        Mock(path="/mcp"),
        Mock(path="/mcp/tools"),
        Mock(path="/mcp/resources"),
    ]
    app.router = Mock()
    return app


@pytest.fixture
def mock_client():
    """Provide mock HTTP client for testing."""
    client = Mock()
    client.get = Mock()
    client.post = Mock()
    client.put = Mock()
    client.delete = Mock()
    return client


CORPUS = {
    "_okf.yaml": """
bundle_title: Test corpus
types:
  policies/: Policy
documents:
  policies/travel.md:
    status: stable
    description: Current travel rules.
    tags: [finance, travel]
    stale_after: 2099-01-01T00:00:00Z
    verified:
      - { by: "human:owner", at: 2026-01-01T00:00:00Z }
  policies/travel-2023.md:
    status: deprecated
    tags: [finance, travel]
  guides/expenses.md:
    stale_after: 2020-01-01T00:00:00Z
""",
    "policies/travel.md": """# Travel Policy

Intro paragraph.

## Hotels

The hotel limit in London is 260 USD per night.

## Meals

The meal per diem is 75 USD.

```
# not a heading
```
""",
    "policies/travel-2023.md": "# Old Travel Policy\n\n## Hotels\n\nThe hotel limit is 150 USD.\n",
    "guides/expenses.md": "# Expenses\n\nSubmit claims within 30 days. Product SKU-4471 is exempt.\n",
    "notes.txt": "Payroll runs on the last working day.\n",
}


def write_corpus(root: Path, files=None) -> Path:
    """Write a small Markdown/text corpus (no Docling needed)."""
    for rel, text in (files or CORPUS).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def corpus_writer():
    """The `write_corpus` helper, as a fixture (tests must not import conftest)."""
    return write_corpus


@pytest.fixture
def corpus(tmp_path):
    """Source folder with the default test corpus."""
    return write_corpus(tmp_path / "source")


@pytest.fixture
def published(tmp_path, corpus, monkeypatch):
    """Build and publish a snapshot, and point the server settings at it."""
    from okf_mcp_server.src.ingest.pipeline import build

    data_dir = tmp_path / "data"
    report = build(corpus, data_dir)
    assert report.published, (report.failures, report.problems)
    monkeypatch.setattr(app_settings, "OKF_DATA_DIR", str(data_dir))
    monkeypatch.setattr(app_settings, "ENABLE_AUTH", False)
    return data_dir

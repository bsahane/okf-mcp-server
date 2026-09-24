"""Smoke-test the actual application over Streamable HTTP, without mocks."""

import asyncio
import os
import subprocess
import sys

import httpx
import pytest
from fastmcp import Client


@pytest.fixture(autouse=True)
def mock_imports():
    """Keep the real dependencies for this integration test."""
    yield


async def test_http_retrieval_and_origin_protection(
    published, tmp_path, unused_tcp_port
):
    """Exercise startup, MCP negotiation, retrieval and errors over a real socket."""
    env = {
        **os.environ,
        "ENABLE_AUTH": "False",
        "USE_EXTERNAL_BROWSER_AUTH": "False",
        "ENVIRONMENT": "development",
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(unused_tcp_port),
        "MCP_TRANSPORT_PROTOCOL": "streamable-http",
        "OKF_DATA_DIR": str(published),
        # Keyword-path smoke test: loading the embedding model would add ~5 s to startup.
        "OKF_SEMANTIC_SEARCH": "False",
        "MCP_SSL_KEYFILE": "",
        "MCP_SSL_CERTFILE": "",
    }
    url = f"http://127.0.0.1:{unused_tcp_port}"
    log_path = tmp_path / "server.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "okf_mcp_server.src.main"],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            async with httpx.AsyncClient(timeout=1, trust_env=False) as http:
                for _ in range(100):
                    if process.poll() is not None:
                        pytest.fail(log_path.read_text())
                    try:
                        if (await http.get(url + "/health")).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("Server did not become ready: " + log_path.read_text())

                assert (
                    await http.post(
                        url + "/mcp", headers={"Origin": "https://evil.example"}
                    )
                ).status_code == 403
                assert (
                    await http.post(url + "/mcp", headers={"Host": "evil.example"})
                ).status_code == 421

            async with Client(url + "/mcp", timeout=10) as client:
                assert {tool.name for tool in await client.list_tools()} == {
                    "browse_knowledge",
                    "search_knowledge",
                    "get_knowledge",
                }
                browse = await client.call_tool("browse_knowledge", {})
                assert browse.structured_content["entries"]
                search = await client.call_tool(
                    "search_knowledge", {"query": "hotel limit London"}
                )
                hit = search.structured_content["results"][0]
                assert hit["source"]["location"]["section"] == "Hotels"
                fetched = await client.call_tool(
                    "get_knowledge",
                    {"concept_id": hit["concept_id"], "section": hit["section"]},
                )
                assert "260 USD" in fetched.structured_content["content"]
                bad = await client.call_tool(
                    "get_knowledge",
                    {"concept_id": "../../private"},
                    raise_on_error=False,
                )
                assert bad.is_error
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

#!/usr/bin/env python3
"""MCP Server Demo - Using FastMCP Client.

This example connects to the running OKF MCP server with the FastMCP Client
and makes real browse, search and fetch calls.

Prerequisites:
- A published snapshot (`make sample`) and the server running (`make local`)
- Install dependencies: pip install fastmcp httpx requests
"""

import asyncio

import requests
from fastmcp import Client


class FastMCPClient:
    """Demo client for the OKF MCP server using FastMCP Client."""

    def __init__(self, server_url: str):
        """Initialize the MCP client demo.

        Args:
            server_url: URL of the MCP server
        """
        self.server_url = server_url
        self.health_endpoint = f"{server_url}/health"

        # MCP configuration
        self.config = {"mcpServers": {"okf_mcp_server": {"url": f"{server_url}/mcp"}}}

    def check_server_health(self):
        """Check if the MCP server is healthy."""
        try:
            response = requests.get(self.health_endpoint, timeout=10)
            if response.status_code == 200:
                health_data = response.json()
                print("Server is healthy")
                print(f"   Service: {health_data.get('service')}")
                print(f"   Transport Protocol: {health_data.get('transport_protocol')}")
                print(f"   Version: {health_data.get('version')}")
                return True
            else:
                print(f"Health check failed: {response.status_code}")
                return False
        except Exception as e:
            print(f"Error connecting to MCP server: {e}")
            return False

    async def demonstrate_tools(self, client):
        """Demonstrate all available tools using actual MCP calls."""
        print("\n" + "=" * 60)
        print("Available MCP Tools")
        print("=" * 60)

        try:
            # List available tools
            tools = await client.list_tools()
            print(f"Found {len(tools)} tool(s):")
            for tool in tools:
                print(f"   - {tool.name}: {tool.description}")

            print("\n--- browse_knowledge ---")
            result = await client.call_tool("browse_knowledge", {})
            for entry in result.structured_content["entries"]:
                print(f"   {entry['kind']}: {entry.get('path') or entry['concept_id']}")

            print("\n--- search_knowledge ---")
            result = await client.call_tool(
                "search_knowledge", {"query": "hotel limit London", "limit": 3}
            )
            for hit in result.structured_content["results"]:
                source = hit["source"]
                print(
                    f"   {hit['concept_id']} [{hit['status']}, {hit['trust_tier']}"
                    f"{', stale' if hit['stale'] else ''}] {source['location_text']}"
                )

            print("\n--- get_knowledge ---")
            top = result.structured_content["results"][0]
            result = await client.call_tool(
                "get_knowledge",
                {"concept_id": top["concept_id"], "section": top["section"]},
            )
            print(f"   {result.structured_content['content']}")

        except Exception as e:
            print(f"   Error accessing tools: {e}")

    async def run_demo(self):
        """Run the complete demo using FastMCP Client."""
        print("OKF MCP Server Demo with FastMCP Client")
        print("=" * 60)

        # Check server health
        if not self.check_server_health():
            print("Cannot proceed without a healthy MCP server")
            return

        # Create MCP client
        client = Client(self.config)

        try:
            async with client:
                print("Connected to MCP server")

                # Demonstrate all tools
                await self.demonstrate_tools(client)

                print("\nDemo completed successfully!")

        except Exception as e:
            print(f"Error during demo: {e}")


async def main():
    """Main function to run the demo."""
    # Test MCP server deployed locally
    demo = FastMCPClient(server_url="http://localhost:5001")

    # Test MCP server deployed on OpenShift
    # demo = FastMCPClient(server_url="https://your-mcp-server.apps.example.com")
    await demo.run_demo()


if __name__ == "__main__":
    asyncio.run(main())

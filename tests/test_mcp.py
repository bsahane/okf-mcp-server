"""Tests for the MCP server module."""

from unittest.mock import Mock, patch

import pytest

from okf_mcp_server.src.mcp import OKFMCPServer


class TestOKFMCPServer:
    """Test the OKFMCPServer class."""

    @patch("okf_mcp_server.src.mcp.force_reconfigure_all_loggers")
    @patch("okf_mcp_server.src.mcp.settings")
    @patch("okf_mcp_server.src.mcp.FastMCP")
    @patch("okf_mcp_server.src.mcp.logger")
    def test_init_success(
        self, mock_logger, mock_fastmcp, mock_settings, mock_force_reconfigure
    ):
        """Test successful initialization of OKFMCPServer."""
        # Arrange
        mock_mcp = Mock()
        mock_fastmcp.return_value = mock_mcp
        mock_settings.PYTHON_LOG_LEVEL = "INFO"

        # Act
        server = OKFMCPServer()

        # Assert
        assert server.mcp == mock_mcp
        mock_logger.info.assert_called_with("OKF MCP Server initialized successfully")
        # In tools-first architecture, we only register tools
        mock_mcp.tool.assert_called()

    @patch("okf_mcp_server.src.mcp.force_reconfigure_all_loggers")
    @patch("okf_mcp_server.src.mcp.settings")
    @patch("okf_mcp_server.src.mcp.FastMCP")
    @patch("okf_mcp_server.src.mcp.logger")
    def test_init_failure(
        self, mock_logger, mock_fastmcp, mock_settings, mock_force_reconfigure
    ):
        """Test initialization failure handling."""
        # Arrange
        mock_fastmcp.side_effect = Exception("Test error")
        mock_settings.PYTHON_LOG_LEVEL = "INFO"

        # Act & Assert
        with pytest.raises(Exception, match="Test error"):
            OKFMCPServer()

        mock_logger.error.assert_called_with(
            "Failed to initialize OKF MCP Server: Test error"
        )

    @patch("okf_mcp_server.src.mcp.force_reconfigure_all_loggers")
    @patch("okf_mcp_server.src.mcp.settings")
    @patch("okf_mcp_server.src.mcp.FastMCP")
    def test_register_mcp_tools(
        self, mock_fastmcp, mock_settings, mock_force_reconfigure
    ):
        """Test MCP tools registration."""
        # Arrange
        mock_mcp = Mock()
        mock_fastmcp.return_value = mock_mcp
        mock_settings.PYTHON_LOG_LEVEL = "INFO"
        server = OKFMCPServer()

        # Act
        server._register_mcp_tools()

        # Assert
        mock_mcp.tool.assert_called()

    @patch("okf_mcp_server.src.mcp.force_reconfigure_all_loggers")
    @patch("okf_mcp_server.src.mcp.settings")
    @patch("okf_mcp_server.src.mcp.FastMCP")
    def test_register_mcp_tools_functionality(
        self, mock_fastmcp, mock_settings, mock_force_reconfigure
    ):
        """Test that MCP tools registration includes all expected tools."""
        # Arrange
        mock_mcp = Mock()
        mock_fastmcp.return_value = mock_mcp
        mock_settings.PYTHON_LOG_LEVEL = "INFO"
        server = OKFMCPServer()
        mock_mcp.tool.reset_mock()

        # Act
        server._register_mcp_tools()

        # Assert
        # Verify that tool() was called once for each tool
        # browse_knowledge, search_knowledge, get_knowledge
        assert mock_mcp.tool.call_count == 3

    def test_server_attributes(self):
        """Test that server has required attributes for tools-first architecture."""
        # Arrange & Act
        with (
            patch("okf_mcp_server.src.mcp.settings") as mock_settings,
            patch("okf_mcp_server.src.mcp.FastMCP"),
            patch("okf_mcp_server.src.mcp.force_reconfigure_all_loggers"),
        ):
            mock_settings.PYTHON_LOG_LEVEL = "INFO"
            server = OKFMCPServer()

        # Assert
        assert hasattr(server, "mcp")
        assert hasattr(server, "_register_mcp_tools")

    def test_tools_first_architecture_compliance(self):
        """Test that server adheres to tools-first architecture by not having resource/prompt methods."""
        # Arrange & Act
        with (
            patch("okf_mcp_server.src.mcp.settings") as mock_settings,
            patch("okf_mcp_server.src.mcp.FastMCP"),
            patch("okf_mcp_server.src.mcp.force_reconfigure_all_loggers"),
        ):
            mock_settings.PYTHON_LOG_LEVEL = "INFO"
            server = OKFMCPServer()

        # Assert - These methods should NOT exist in tools-first architecture
        assert not hasattr(server, "_register_mcp_resources"), (
            "_register_mcp_resources should not exist in tools-first architecture"
        )
        assert not hasattr(server, "_register_mcp_prompts"), (
            "_register_mcp_prompts should not exist in tools-first architecture"
        )

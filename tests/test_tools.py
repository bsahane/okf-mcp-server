"""Tests for the knowledge tools: browse, search and get."""

import os

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from okf_mcp_server.src.ingest.pipeline import build
from okf_mcp_server.src.settings import settings
from okf_mcp_server.src.tools.browse_knowledge_tool import browse_knowledge
from okf_mcp_server.src.tools.get_knowledge_tool import MAX_PAGE_CHARS, get_knowledge
from okf_mcp_server.src.tools.search_knowledge_tool import search_knowledge


@pytest.fixture(autouse=True)
def mock_imports():
    """Override conftest's sys.modules mocking: these tests run the real stack."""
    yield


class TestBrowseKnowledge:
    """browse_knowledge: progressive disclosure."""

    def test_root_lists_directories_then_concepts(self, published):
        result = browse_knowledge()
        assert result["status"] == "success"
        kinds = [
            (e["kind"], e.get("path") or e.get("concept_id")) for e in result["entries"]
        ]
        assert kinds == [
            ("directory", "guides"),
            ("directory", "policies"),
            ("concept", "notes"),
        ]

    def test_subdirectory_shows_trust_and_lifecycle(self, published):
        entries = {e["concept_id"]: e for e in browse_knowledge("policies")["entries"]}
        assert entries["policies/travel"]["trust_tier"] == "human-reviewed"
        assert entries["policies/travel"]["status"] == "stable"
        assert entries["policies/travel-2023"]["status"] == "deprecated"
        assert entries["policies/travel-2023"]["trust_tier"] == "unverified"

    def test_never_lists_reserved_or_raw_index_files(self, published):
        for entry in browse_knowledge()["entries"]:
            assert entry.get("concept_id") not in ("index", "log")

    def test_pagination(self, published):
        first = browse_knowledge("", page_size=2)
        assert len(first["entries"]) == 2 and first["next_cursor"]
        second = browse_knowledge("", page_size=2, cursor=first["next_cursor"])
        assert len(second["entries"]) == 1 and second["next_cursor"] is None

    def test_cursor_for_another_path_is_rejected(self, published):
        cursor = browse_knowledge("", page_size=1)["next_cursor"]
        with pytest.raises(ToolError, match="does not belong"):
            browse_knowledge("policies", cursor=cursor)

    def test_cursor_from_old_snapshot_requires_restart(self, published, corpus):
        cursor = browse_knowledge("", page_size=1)["next_cursor"]
        assert build(corpus, published).published
        with pytest.raises(ToolError, match="restart"):
            browse_knowledge("", page_size=1, cursor=cursor)

    def test_garbage_cursor(self, published):
        with pytest.raises(ToolError, match="invalid cursor"):
            browse_knowledge("", cursor="!!!")

    @pytest.mark.parametrize(
        "path", ["..", "../..", "policies/../../x", "/../etc", "a\\b", "x\x00"]
    )
    def test_path_traversal_rejected(self, published, path):
        with pytest.raises(ToolError):
            browse_knowledge(path)

    def test_missing_directory(self, published):
        with pytest.raises(ToolError, match="not found"):
            browse_knowledge("nope")

    def test_page_size_is_bounded(self, published):
        assert len(browse_knowledge("", page_size=10_000)["entries"]) <= 100


class TestSearchKnowledge:
    """search_knowledge: keyword search with trust signals."""

    def test_finds_evidence_with_citation(self, published):
        result = search_knowledge("hotel limit London")
        top = result["results"][0]
        assert top["concept_id"] == "policies/travel"
        assert top["section"] == "hotels"
        assert top["source"]["uri"].startswith("file://")
        assert top["source"]["location"]["section"] == "Hotels"
        assert "lines" in top["source"]["location_text"]
        assert top["stale"] is False and top["trust_tier"] == "human-reviewed"

    def test_deprecated_and_stale_are_flagged(self, published):
        by_concept = {
            r["concept_id"]: r
            for r in search_knowledge("hotel limit", limit=20)["results"]
        }
        assert by_concept["policies/travel-2023"]["status"] == "deprecated"
        stale = search_knowledge("claims")["results"][0]
        assert stale["concept_id"] == "guides/expenses" and stale["stale"] is True

    def test_deprecated_ranks_after_current(self, published):
        ids = [
            r["concept_id"]
            for r in search_knowledge("hotel limit", limit=20)["results"]
        ]
        assert ids.index("policies/travel") < ids.index("policies/travel-2023")

    def test_exact_code(self, published):
        assert (
            search_knowledge("SKU-4471")["results"][0]["concept_id"]
            == "guides/expenses"
        )

    def test_filters(self, published):
        results = search_knowledge("hotel", type="Policy", tags=["travel", "finance"])[
            "results"
        ]
        assert results and all(r["type"] == "Policy" for r in results)
        assert search_knowledge("hotel", type="SOP")["results"] == []
        assert search_knowledge("hotel", tags=["missing"])["results"] == []

    def test_empty_result_is_success_not_error(self, published):
        result = search_knowledge("zebra xylophone")
        assert result["status"] == "success" and result["results"] == []

    @pytest.mark.parametrize(
        "query",
        ['"', "AND OR NOT", "hotel*", "(hotel", "col:hotel", "NEAR(a b)", "-- '; DROP"],
    )
    def test_fts_syntax_is_not_interpreted(self, published, query):
        assert search_knowledge(query)["status"] == "success"

    def test_limit_is_bounded(self, published):
        assert len(search_knowledge("the", limit=1000)["results"]) <= 20

    def test_oversized_table_excerpt_is_bounded_and_fetchable(self, published, corpus):
        from okf_mcp_server.src.knowledge.index import MAX_PASSAGE_CHARS

        body = (
            "# Table\n\n| Code | Notes |\n|---|---|\n| SKU-999 | "
            + "note " * 5000
            + " |\n"
        )
        (corpus / "table.md").write_text(body)
        assert build(corpus, published).published
        result = search_knowledge("SKU-999")["results"][0]
        assert len(result["excerpt"]) <= MAX_PASSAGE_CHARS
        assert result["excerpt_truncated"] is True
        page = get_knowledge(result["concept_id"], section=result["section"])
        assert page["next_cursor"]

    @pytest.mark.parametrize("query", ["", "   ", "x" * 501])
    def test_invalid_queries(self, published, query):
        with pytest.raises(ToolError):
            search_knowledge(query)


class TestGetKnowledge:
    """get_knowledge: fetch with provenance and continuation."""

    def test_full_concept(self, published):
        result = get_knowledge("policies/travel")
        assert result["concept"]["title"] == "Travel Policy"
        assert result["sources"][0]["resource"].startswith("file://")
        assert result["verified"][0]["by"] == "human:owner"
        assert result["stale_after"] == "2099-01-01T00:00:00Z"
        assert [s["slug"] for s in result["sections"]] == [
            "travel-policy",
            "hotels",
            "meals",
        ]
        assert result["next_cursor"] is None

    def test_accepts_leading_slash_and_md_suffix(self, published):
        assert (
            get_knowledge("/policies/travel.md")["concept"]["concept_id"]
            == "policies/travel"
        )

    def test_single_section(self, published):
        result = get_knowledge("policies/travel", section="meals")
        assert "75 USD" in result["content"] and "260" not in result["content"]
        assert "# not a heading" in result["content"]

    def test_unknown_section(self, published):
        with pytest.raises(ToolError, match="section not found"):
            get_knowledge("policies/travel", section="nope")

    def test_continuation_returns_everything_once(self, published):
        full = get_knowledge("policies/travel")["content"]
        pieces, cursor = [], None
        while True:
            page = get_knowledge("policies/travel", page_size=500, cursor=cursor)
            assert len(page["content"]) <= 500
            pieces.append(page["content"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert "".join(pieces) == full

    def test_page_size_is_bounded(
        self, published, tmp_path, monkeypatch, corpus_writer
    ):
        big = {"big.md": "# Big\n\n" + ("word " * 20000)}
        data = tmp_path / "bigdata"
        assert build(corpus_writer(tmp_path / "bigsrc", big), data).published
        monkeypatch.setattr(settings, "OKF_DATA_DIR", str(data))
        page = get_knowledge("big", page_size=10**9)
        assert len(page["content"]) == MAX_PAGE_CHARS and page["next_cursor"]

    @pytest.mark.parametrize(
        "concept_id",
        [
            "../../etc/passwd",
            "/etc/passwd",
            "policies/../../x",
            "index",
            "policies/log",
            "",
            "nope",
        ],
    )
    def test_rejects_paths_outside_bundle_and_reserved_files(
        self, published, concept_id
    ):
        with pytest.raises(ToolError):
            get_knowledge(concept_id)

    def test_symlink_escape_rejected(self, published, tmp_path):
        secret = tmp_path / "secret.md"
        secret.write_text("---\ntype: Secret\n---\nsecret\n")
        bundle = published / "current" / "bundle"
        os.symlink(secret, bundle / "leak.md")
        with pytest.raises(ToolError, match="concept not found"):
            get_knowledge("leak")


class TestAccessAndProtocol:
    """Fail-closed auth guard and MCP error signalling."""

    def test_auth_enabled_fails_closed(self, published, monkeypatch):
        monkeypatch.setattr(settings, "ENABLE_AUTH", True)
        for call in (
            lambda: browse_knowledge(),
            lambda: search_knowledge("hotel"),
            lambda: get_knowledge("policies/travel"),
        ):
            with pytest.raises(ToolError, match="fails closed"):
                call()

    def test_no_snapshot_published(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "OKF_DATA_DIR", str(tmp_path / "empty"))
        monkeypatch.setattr(settings, "ENABLE_AUTH", False)
        with pytest.raises(ToolError, match="okf-ingest build"):
            search_knowledge("hotel")

    async def test_mcp_client_sees_is_error_and_success(self, published):
        # A real FastMCP registered like OKFMCPServer._register_mcp_tools (other
        # template tests reload modules with mocked dependencies).
        mcp = FastMCP("okf-test")
        for tool in (browse_knowledge, search_knowledge, get_knowledge):
            mcp.tool()(tool)
        async with Client(mcp) as client:
            names = {t.name for t in await client.list_tools()}
            assert names == {"browse_knowledge", "search_knowledge", "get_knowledge"}

            ok = await client.call_tool("search_knowledge", {"query": "zebra"})
            assert ok.is_error is False
            assert ok.structured_content["results"] == []

            bad = await client.call_tool(
                "get_knowledge",
                {"concept_id": "../../etc/passwd"},
                raise_on_error=False,
            )
            assert bad.is_error is True


class TestReviewFindings:
    """Reserved files, empty access entries and audit of failed calls."""

    def test_reserved_names_are_not_concepts_in_any_case(self, published):
        for name in ("INDEX", "Index", "LOG"):
            with pytest.raises(ToolError, match="concept not found"):
                get_knowledge(name)

    def test_empty_document_access_is_rejected(self, tmp_path):
        from okf_mcp_server.src.ingest.pipeline import load_config

        (tmp_path / "_okf.yaml").write_text(
            "documents:\n  hr/salaries.md: {access: }\n"
        )
        with pytest.raises(ValueError, match="access is empty"):
            load_config(tmp_path)

    def test_failed_calls_are_audited(self, published, monkeypatch):
        from okf_mcp_server.src.knowledge import service

        events = []
        monkeypatch.setattr(
            service, "audit", lambda tool, identity, **f: events.append((tool, f))
        )
        import okf_mcp_server.src.tools.browse_knowledge_tool as browse
        import okf_mcp_server.src.tools.get_knowledge_tool as get
        import okf_mcp_server.src.tools.search_knowledge_tool as search

        for module in (browse, get, search):
            monkeypatch.setattr(module, "audit", service.audit)
        for call in (
            lambda: browse.browse_knowledge("no/such/dir"),
            lambda: get.get_knowledge("no/such"),
            lambda: search.search_knowledge(""),
        ):
            with pytest.raises(ToolError):
                call()
        assert [t for t, f in events if f.get("outcome") == "error"] == [
            "browse_knowledge",
            "get_knowledge",
            "search_knowledge",
        ]


def test_null_sections_in_okf_yaml_mean_empty(tmp_path):
    """`suggest-config` emits `documents:` with only comments when it has no suggestions."""
    from okf_mcp_server.src.ingest.pipeline import load_config

    (tmp_path / "_okf.yaml").write_text("types:\ndocuments:\n  # none yet\naccess:\n")
    assert load_config(tmp_path) == {"types": None, "documents": None, "access": None}

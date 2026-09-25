"""Per-user access control: identity, ingestion ACLs, filtering and audit."""

import json

import pytest
from fastmcp.exceptions import ToolError

from okf_mcp_server.src.ingest.pipeline import access_for, build, load_config
from okf_mcp_server.src.knowledge import service
from okf_mcp_server.src.knowledge.access import (
    LOCAL_OPERATOR,
    AccessError,
    Identity,
    allowed,
    identity_from_claims,
    normalize_access,
)
from okf_mcp_server.src.knowledge.bundle import split_frontmatter
from okf_mcp_server.src.knowledge.snapshot import current_snapshot
from okf_mcp_server.src.tools import (
    browse_knowledge_tool,
    get_knowledge_tool,
    search_knowledge_tool,
)
from okf_mcp_server.src.tools.browse_knowledge_tool import browse_knowledge
from okf_mcp_server.src.tools.get_knowledge_tool import get_knowledge
from okf_mcp_server.src.tools.search_knowledge_tool import search_knowledge

FILES = {
    "finance/travel.md": "# Travel\n\nThe hotel limit is 150 GBP per night.\n",
    "finance/budget.md": "# Budget\n\nThe hotel budget line is capped.\n",
    "hr/salaries.md": "# Salaries\n\nHotel allowance for relocation is 500 GBP.\n",
    "hr/private/cases.md": "# Cases\n\nConfidential hotel incident notes.\n",
    "public/handbook.md": "# Handbook\n\nBook every hotel through the portal.\n",
    "unlabelled.md": "# Unlabelled\n\nA hotel note without any access rule.\n",
    "_okf.yaml": """
access:
  finance/: {groups: [finance]}
  hr/: [group:hr]
  public/: ["*"]
documents:
  hr/private/cases.md: {access: [user:carol@example.com]}
""",
}

ALICE = Identity(
    subject="a-1", email="alice@example.com", groups=frozenset({"finance"})
)
BOB = Identity(subject="b-2", email="bob@example.com", groups=frozenset({"hr"}))
CAROL = Identity(subject="c-3", email="carol@example.com", groups=frozenset({"hr"}))
EVE = Identity(subject="e-4", email="eve@example.com")


@pytest.fixture(autouse=True)
def mock_imports():
    """Override conftest's sys.modules mocking: these tests run the real stack."""
    yield


@pytest.fixture
def secured(tmp_path, corpus_writer, monkeypatch):
    """A published corpus with ACLs, served with authentication enabled."""
    data = tmp_path / "data"
    src = corpus_writer(tmp_path / "src", FILES)
    (src / ".okf").mkdir()
    (src / ".okf" / "access.json").write_text(
        json.dumps(
            {"finance/budget.md": ["group:finance-leads", "user:alice@example.com"]}
        )
    )
    assert build(src, data).published
    monkeypatch.setattr(service.settings, "OKF_DATA_DIR", str(data))
    monkeypatch.setattr(service.settings, "ENABLE_AUTH", True)
    monkeypatch.setattr(service.settings, "OKF_DEFAULT_ACCESS", "deny")
    return data


def as_user(monkeypatch, identity):
    for module in (search_knowledge_tool, browse_knowledge_tool, get_knowledge_tool):
        monkeypatch.setattr(module, "caller", lambda: identity)


class TestIdentity:
    def test_claims_to_identity(self):
        identity = identity_from_claims(
            {
                "sub": "u1",
                "iss": "https://sso/realms/okf",
                "email": "A@x.io",
                "email_verified": True,
                "preferred_username": "alice",
                "groups": ["/finance/payroll", "hr"],
                "aud": ["okf-mcp", "account"],
                "scope": "openid okf.read",
            },
            required_audience="okf-mcp",
            required_scope="okf.read",
        )
        assert identity.groups == {"finance/payroll", "hr"}
        assert {
            "user:u1",
            "user:a@x.io",
            "user:alice",
            "group:finance/payroll",
            "*",
        } <= identity.principals

    @pytest.mark.parametrize(
        "claims, kwargs, message",
        [
            ({}, {}, "no subject"),
            (
                {"sub": "u", "aud": "other"},
                {"required_audience": "okf-mcp"},
                "audience",
            ),
            (
                {"sub": "u", "azp": "okf-mcp", "scope": "openid"},
                {"required_scope": "okf.read"},
                "scope",
            ),
        ],
    )
    def test_rejections(self, claims, kwargs, message):
        with pytest.raises(AccessError, match=message):
            identity_from_claims(claims, **kwargs)

    def test_azp_is_not_an_audience(self):
        with pytest.raises(AccessError, match="audience"):
            identity_from_claims(
                {"sub": "u", "azp": "okf-mcp"}, required_audience="okf-mcp"
            )

    def test_claims_cannot_borrow_other_identities(self):
        identity = identity_from_claims(
            {
                "sub": "m",
                "groups": ["/marketing/finance"],
                "email": "carol@x.io",
                "email_verified": False,
                "preferred_username": "alice@x.io",
            }
        )
        assert identity.principals == {"*", "user:m", "group:marketing/finance"}
        assert identity_from_claims(
            {"sub": "m", "groups": "HR Contractors"}
        ).groups == {"HR Contractors"}

    def test_normalize_and_allowed(self):
        assert normalize_access({"groups": ["/Finance"], "users": ["Bob@x"]}) == [
            "group:finance",
            "user:bob@x",
        ]
        assert normalize_access(None) is None
        with pytest.raises(ValueError):
            normalize_access(["finance"])
        assert allowed(LOCAL_OPERATOR, ["group:none"], "deny")
        assert not allowed(EVE, None, "deny") and allowed(EVE, None, "authenticated")
        assert allowed(EVE, ["*"], "deny")


class TestIngestionAccess:
    def test_precedence(self, tmp_path, corpus_writer):
        src = corpus_writer(tmp_path / "src", FILES)
        config = load_config(src)
        acls = {"finance/budget.md": ["user:x"]}
        assert access_for("hr/private/cases.md", config, acls) == [
            "user:carol@example.com"
        ]
        assert access_for("finance/budget.md", config, acls) == ["user:x"]
        assert access_for("finance/travel.md", config, acls) == ["group:finance"]
        assert access_for("unlabelled.md", config, acls) is None

    def test_access_is_written_to_frontmatter(self, secured):
        bundle = current_snapshot(secured).bundle
        fm, _ = split_frontmatter((bundle / "finance" / "budget.md").read_text())
        assert fm["access"] == ["group:finance-leads", "user:alice@example.com"]
        fm, _ = split_frontmatter((bundle / "unlabelled.md").read_text())
        assert "access" not in fm

    @pytest.mark.parametrize(
        "config", ["access: {hr/: [finance]}", "documents: {a.md: {access: [nobody]}}"]
    )
    def test_invalid_rules_fail_the_build(self, tmp_path, corpus_writer, config):
        src = corpus_writer(
            tmp_path / "s", {"a.md": "# A\n\ntext\n", "_okf.yaml": config}
        )
        with pytest.raises(ValueError, match="access"):
            build(src, tmp_path / "data")

    def test_invalid_source_acl_file(self, tmp_path, corpus_writer):
        src = corpus_writer(tmp_path / "s", {"a.md": "# A\n\ntext\n"})
        (src / ".okf").mkdir()
        (src / ".okf" / "access.json").write_text('{"a.md": ["oops"]}')
        with pytest.raises(ValueError, match="access.json"):
            build(src, tmp_path / "data")


class TestFiltering:
    def search_ids(self, **kwargs):
        return {
            r["concept_id"]
            for r in search_knowledge("hotel", limit=20, **kwargs)["results"]
        }

    def test_users_see_only_their_documents(self, secured, monkeypatch):
        as_user(monkeypatch, ALICE)
        assert self.search_ids() == {
            "finance/travel",
            "finance/budget",
            "public/handbook",
        }
        as_user(monkeypatch, BOB)
        assert self.search_ids() == {"hr/salaries", "public/handbook"}
        as_user(monkeypatch, CAROL)
        assert self.search_ids() == {
            "hr/salaries",
            "hr/private/cases",
            "public/handbook",
        }
        as_user(monkeypatch, EVE)
        assert self.search_ids() == {"public/handbook"}

    def test_default_access_authenticated(self, secured, monkeypatch):
        monkeypatch.setattr(service.settings, "OKF_DEFAULT_ACCESS", "authenticated")
        as_user(monkeypatch, EVE)
        assert self.search_ids() == {"public/handbook", "unlabelled"}

    def test_trusted_operator_sees_everything(self, secured, monkeypatch):
        monkeypatch.setattr(service.settings, "ENABLE_AUTH", False)
        assert len(self.search_ids()) == 6

    def test_browse_hides_documents_and_folders(self, secured, monkeypatch):
        as_user(monkeypatch, BOB)
        root = {
            e.get("path") or e.get("concept_id"): e
            for e in browse_knowledge()["entries"]
        }
        assert set(root) == {"hr", "public"}
        assert root["hr"]["concept_count"] == 1
        assert [
            e.get("path") or e["concept_id"] for e in browse_knowledge("hr")["entries"]
        ] == ["hr/salaries"]
        with pytest.raises(ToolError, match="directory not found"):
            browse_knowledge("hr/private")
        with pytest.raises(ToolError, match="directory not found"):
            browse_knowledge("finance")

    def test_get_denied_looks_like_missing(self, secured, monkeypatch):
        as_user(monkeypatch, BOB)
        with pytest.raises(ToolError) as denied:
            get_knowledge("finance/travel")
        with pytest.raises(ToolError) as missing:
            get_knowledge("finance/nope")
        assert str(denied.value).replace("travel", "X") == str(missing.value).replace(
            "nope", "X"
        )
        as_user(monkeypatch, ALICE)
        assert "150 GBP" in get_knowledge("finance/travel")["content"]

    def test_semantic_candidates_are_filtered(
        self, secured, monkeypatch, fake_embedder, tmp_path, corpus_writer
    ):
        # Rebuild with vectors: the semantic ranking must honour access too.
        src = tmp_path / "src"
        assert build(src, secured).embedding_model
        as_user(monkeypatch, BOB)
        result = search_knowledge("lodging accommodation night", limit=20)
        assert result["search_mode"] == "hybrid"
        assert {r["concept_id"] for r in result["results"]} <= {
            "hr/salaries",
            "public/handbook",
        }


class TestCallerAndAudit:
    def test_caller_fails_closed_without_identity(self, monkeypatch):
        monkeypatch.setattr(service.settings, "ENABLE_AUTH", True)
        with pytest.raises(ToolError, match="fails closed"):
            service.caller()

    def test_caller_reads_identity_from_request_scope(self, monkeypatch):
        import fastmcp.server.dependencies as deps

        class FakeRequest:
            scope = {"state": {service.IDENTITY_STATE_KEY: ALICE}}

        monkeypatch.setattr(service.settings, "ENABLE_AUTH", True)
        monkeypatch.setattr(deps, "get_http_request", lambda: FakeRequest())
        assert service.caller() is ALICE
        FakeRequest.scope = {"state": {service.IDENTITY_STATE_KEY: "forged"}}
        with pytest.raises(ToolError):
            service.caller()

    def test_audit_log_records_calls(self, secured, monkeypatch, tmp_path):
        log = tmp_path / "audit.jsonl"
        monkeypatch.setattr(service.settings, "OKF_AUDIT_LOG", str(log))
        as_user(monkeypatch, BOB)
        search_knowledge("hotel")
        with pytest.raises(ToolError):
            get_knowledge("finance/travel")
        get_knowledge("hr/salaries")
        records = [json.loads(line) for line in log.read_text().splitlines()]
        assert [r["tool"] for r in records] == [
            "search_knowledge",
            "get_knowledge",
            "get_knowledge",
        ]
        assert records[0]["caller"]["email"] == "bob@example.com"
        assert "hr/salaries#salaries" in records[0]["returned"]
        assert records[1]["outcome"] == "denied" and records[2]["outcome"] == "ok"


class TestMiddleware:
    @pytest.fixture
    def app(self, monkeypatch):
        import importlib

        import okf_mcp_server.src.api as api_module

        monkeypatch.setattr(service.settings, "ENABLE_AUTH", True)
        monkeypatch.setattr(service.settings, "USE_EXTERNAL_BROWSER_AUTH", False)
        monkeypatch.setattr(service.settings, "MCP_HOST", "localhost")
        monkeypatch.setattr(service.settings, "OKF_REQUIRED_AUDIENCE", "okf-mcp")
        return importlib.reload(api_module)

    def test_missing_invalid_and_wrong_audience(self, app, monkeypatch):
        from fastapi.testclient import TestClient

        client = TestClient(app.app, base_url="http://localhost")
        assert client.post("/mcp", json={}).status_code == 401
        monkeypatch.setattr(
            app.OAuth2Handler,
            "verify_authorization_header",
            staticmethod(lambda h: None),
        )
        assert (
            client.post(
                "/mcp", json={}, headers={"Authorization": "Bearer bad"}
            ).status_code
            == 401
        )
        monkeypatch.setattr(
            app.OAuth2Handler,
            "verify_authorization_header",
            staticmethod(lambda h: {"active": True, "sub": "u", "aud": "someone-else"}),
        )
        assert (
            client.post(
                "/mcp", json={}, headers={"Authorization": "Bearer x"}
            ).status_code
            == 403
        )

    def test_valid_token_sets_identity(self, app, monkeypatch):
        from fastapi.testclient import TestClient
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        seen = {}

        async def probe(request):
            seen.update(request.scope.get("state", {}))
            return PlainTextResponse("ok")

        app.app.router.routes.insert(0, Route("/probe", probe))
        monkeypatch.setattr(
            app.OAuth2Handler,
            "verify_authorization_header",
            staticmethod(
                lambda h: {
                    "active": True,
                    "sub": "u9",
                    "aud": "okf-mcp",
                    "groups": ["/hr"],
                }
            ),
        )
        client = TestClient(app.app, base_url="http://localhost")
        assert (
            client.get("/probe", headers={"Authorization": "Bearer ok"}).status_code
            == 200
        )
        identity = seen[service.IDENTITY_STATE_KEY]
        assert identity.subject == "u9" and "hr" in identity.groups


def test_refresh_and_id_tokens_are_not_access_tokens():
    for typ in ("Refresh", "Offline", "ID"):
        with pytest.raises(AccessError, match="not an access token"):
            identity_from_claims({"sub": "u", "typ": typ})
    assert identity_from_claims({"sub": "u", "typ": "Bearer"}).subject == "u"

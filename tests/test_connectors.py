"""Connectors: mirror semantics, file shares, SharePoint (Graph) and Google Drive.

SharePoint and Drive run against in-process mock servers (httpx.MockTransport)
that follow the real APIs' shapes: paging, throttling, redirects, exports and
permissions. They are not a substitute for a live tenant.
"""

import json
import os
import stat

import httpx
import pytest

from okf_mcp_server.src.connectors import config as connectors
from okf_mcp_server.src.connectors import gdrive, sharepoint
from okf_mcp_server.src.connectors.base import RemoteItem, safe_relpath, sync
from okf_mcp_server.src.connectors.fileshare import list_items, posix_principals
from okf_mcp_server.src.connectors.http import request
from okf_mcp_server.src.ingest.cli import main as cli_main
from okf_mcp_server.src.ingest.pipeline import load_source_acls


@pytest.fixture(autouse=True)
def mock_imports():
    """Override conftest's sys.modules mocking: these tests run the real stack."""
    yield


def item(key, path, version="v1", principals=("*",), content=b"# Doc\n\ntext\n"):
    def download(target):
        target.write_bytes(content)

    return RemoteItem(
        key=key,
        path=path,
        version=version,
        principals=list(principals),
        download=download,
    )


class TestSync:
    def test_incremental_update_delete_and_permissions(self, tmp_path):
        mirror = tmp_path / "m"
        first = sync(
            "s",
            mirror,
            [item("1", "a.md"), item("2", "dir/b.md", principals=["group:hr"])],
        )
        assert sorted(first.downloaded) == ["a.md", "dir/b.md"]
        again = sync(
            "s",
            mirror,
            [item("1", "a.md"), item("2", "dir/b.md", principals=["group:hr"])],
        )
        assert again.downloaded == [] and again.unchanged == 2
        changed = sync(
            "s",
            mirror,
            [
                item("1", "a.md", "v2", content=b"# New\n\nnew\n"),
                item("2", "dir/b.md", principals=["group:finance"]),
            ],
        )
        assert changed.downloaded == ["a.md"] and (
            mirror / "a.md"
        ).read_text().startswith("# New")
        acl = json.loads((mirror / ".okf/access.json").read_text())
        assert acl["dir/b.md"] == [
            "group:finance"
        ]  # permission change without content change
        gone = sync("s", mirror, [item("1", "a.md", "v2")])
        assert gone.deleted == ["dir/b.md"] and not (mirror / "dir").exists()

    def test_names_are_confined_and_disambiguated(self, tmp_path):
        mirror = tmp_path / "m"
        result = sync(
            "s",
            mirror,
            [
                item("1", "../../escape.md"),
                item("2", ".hidden.md"),
                item("3", "same.md"),
                item("4", "same.md"),
                item("5", "pic.png"),
                item("6", "/../.."),
            ],
        )
        files = sorted(
            p.relative_to(mirror).as_posix()
            for p in mirror.rglob("*")
            if p.is_file() and ".okf" not in p.parts
        )
        assert files == ["_hidden.md", "escape.md", "same (4).md", "same.md"]
        assert "pic.png" in result.skipped and "/../.." in result.skipped
        assert all(
            (mirror / f).resolve().is_relative_to(mirror.resolve()) for f in files
        )

    def test_failed_download_keeps_last_good_copy(self, tmp_path):
        mirror = tmp_path / "m"
        sync("s", mirror, [item("1", "a.md")])

        def boom(target):
            raise OSError("network down")

        result = sync(
            "s",
            mirror,
            [RemoteItem("1", "a.md", "v2", principals=["*"], download=boom)],
        )
        assert "a.md" in result.errors and (mirror / "a.md").is_file()
        assert json.loads((mirror / ".okf/access.json").read_text()) == {"a.md": ["*"]}
        assert not list(mirror.glob(".okf-part-*"))

    def test_safe_relpath(self):
        assert safe_relpath("a/../b/c:d?.md") == "a/b/c_d_.md"
        with pytest.raises(ValueError):
            safe_relpath("../..")


class TestFileShare:
    def test_posix_permissions(self, tmp_path):
        share = tmp_path / "share"
        (share / "team").mkdir(parents=True)
        (share / "team" / "group.md").write_text("# G\n\ntext\n")
        (share / "open.md").write_text("# O\n\ntext\n")
        (share / "private.md").write_text("# P\n\ntext\n")
        (share / ".git").mkdir()
        (share / ".git" / "x.md").write_text("# hidden\n")
        os.chmod(share / "team" / "group.md", 0o640)
        os.chmod(share / "open.md", 0o644)
        os.chmod(share / "private.md", 0o600)
        os.symlink(share / "open.md", share / "link.md")  # inside the share: followed
        outside = tmp_path / "outside.md"
        outside.write_text("# Out\n\nsecret\n")
        os.symlink(outside, share / "escape.md")  # outside the share: skipped
        items = {i.path: i for i in list_items(share)}
        assert set(items) == {"team/group.md", "open.md", "private.md", "link.md"}
        assert "*" in items["open.md"].principals
        assert any(p.startswith("group:") for p in items["team/group.md"].principals)
        assert "*" not in items["team/group.md"].principals
        assert [
            p for p in items["private.md"].principals if not p.startswith("user:")
        ] == []
        assert all(i.principals is None for i in list_items(share, permissions="none"))

    def test_principals_from_stat(self, tmp_path):
        f = tmp_path / "f"
        f.write_text("x")
        os.chmod(f, stat.S_IRUSR | stat.S_IROTH)
        assert "*" in posix_principals(f.stat())

    def test_missing_share(self, tmp_path):
        with pytest.raises(ValueError, match="not found"):
            list(list_items(tmp_path / "nope"))

    def test_kubernetes_configmap_layout(self, tmp_path):
        """ConfigMap volumes: visible names (files and folders) link into ..data."""
        share = tmp_path / "cm"
        data = share / "..2026_09_25" / "finance"
        data.mkdir(parents=True)
        (data / "travel.md").write_text("# Travel\n\ntext\n")
        (share / "..2026_09_25" / "policy.md").write_text("# Policy\n\ntext\n")
        os.symlink("..2026_09_25", share / "..data")
        os.symlink("..data/finance", share / "finance")  # folder link
        os.symlink("..data/policy.md", share / "policy.md")  # file link
        assert sorted(i.path for i in list_items(share, "none")) == [
            "finance/travel.md",
            "policy.md",
        ]

    def test_link_loops_and_escapes_end(self, tmp_path):
        share = tmp_path / "share"
        (share / "a").mkdir(parents=True)
        (share / "a" / "doc.md").write_text("# D\n\ntext\n")
        os.symlink(share, share / "a" / "loop")  # back to the root
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "elsewhere" / "x.md").write_text("# X\n\nsecret\n")
        os.symlink(tmp_path / "elsewhere", share / "escape")  # outside the share
        assert [i.path for i in list_items(share, "none")] == ["a/doc.md"]


class TestRetries:
    def test_retry_after_then_success(self):
        calls, slept = [], []

        def handler(req):
            calls.append(1)
            return (
                httpx.Response(429, headers={"Retry-After": "2"})
                if len(calls) < 3
                else httpx.Response(200, json={"ok": 1})
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        assert request(client, "GET", "https://x/y", sleep=slept.append).json() == {
            "ok": 1
        }
        assert slept == [2.0, 2.0]

    def test_gives_up(self):
        client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(503))
        )
        with pytest.raises(httpx.HTTPStatusError):
            request(client, "GET", "https://x/y", attempts=2, sleep=lambda s: None)


def graph_server():
    """Mock Microsoft Graph for one site, one library, a folder and three files."""
    throttled = {"done": False}
    permissions = {
        "f1": [
            {"grantedToV2": {"user": {"email": "alice@contoso.com"}}},
            {"grantedToV2": {"siteGroup": {"displayName": "HR Members"}}},
        ],
        "f2": [{"link": {"scope": "organization"}}],
        "f3": [
            {
                "grantedToIdentitiesV2": [
                    {"group": {"id": "g-42", "displayName": "Finance"}}
                ]
            }
        ],
    }
    contents = {
        "f1": b"# Leave\n\n25 days.\n",
        "f2": b"# Handbook\n\nWelcome.\n",
        "f3": b"# Budget\n\nLimits.\n",
    }

    def handler(req: httpx.Request) -> httpx.Response:
        url, path = str(req.url), req.url.path
        if "login.microsoftonline.com" in url:
            assert b"client_credentials" in req.content and b"s3cret" in req.content
            return httpx.Response(200, json={"access_token": "graph-token"})
        if req.url.host == "download.example":
            return httpx.Response(200, content=contents[path.strip("/")])
        assert req.headers["Authorization"] == "Bearer graph-token"
        if path == "/v1.0/sites/contoso.sharepoint.com:/sites/HR":
            return httpx.Response(200, json={"id": "site1"})
        if path == "/v1.0/sites/site1/drives":
            if "page=2" in url:
                return httpx.Response(
                    200, json={"value": [{"id": "drive1", "name": "Documents"}]}
                )
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "d0", "name": "Other"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/sites/site1/drives?page=2",
                },
            )
        if path == "/v1.0/drives/drive1/root:/Policies:":
            return httpx.Response(200, json={"id": "root1"})
        if path == "/v1.0/drives/drive1/items/root1/children":
            if not throttled["done"]:
                throttled["done"] = True
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "f1",
                            "name": "Leave.md",
                            "file": {},
                            "cTag": "c1",
                            "size": 20,
                        },
                        {"id": "fold", "name": "Finance", "folder": {}},
                        {"id": "img", "name": "logo.png", "file": {}, "cTag": "c9"},
                    ]
                },
            )
        if path == "/v1.0/drives/drive1/items/fold/children":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "f2", "name": "Handbook.md", "file": {}, "cTag": "c2"},
                        {"id": "f3", "name": "Budget.md", "file": {}, "cTag": "c3"},
                    ]
                },
            )
        if path.endswith("/permissions"):
            return httpx.Response(
                200, json={"value": permissions.get(path.split("/")[-2], [])}
            )
        if path.endswith("/content"):
            return httpx.Response(
                302,
                headers={"Location": f"https://download.example/{path.split('/')[-2]}"},
            )
        return httpx.Response(404, json={"error": path})

    return httpx.Client(transport=httpx.MockTransport(handler))


class TestSharePoint:
    def test_sync_files_and_permissions(self, tmp_path):
        client = graph_server()
        token = sharepoint.get_token(client, "tenant", "app", "s3cret")
        source = sharepoint.SharePointSource(
            client, token, "contoso.sharepoint.com:/sites/HR", "Documents", "Policies"
        )
        result = sync("hr-sp", tmp_path / "m", source.items())
        assert sorted(result.downloaded) == [
            "Finance/Budget.md",
            "Finance/Handbook.md",
            "Leave.md",
        ]
        assert "logo.png" in result.skipped
        acl = json.loads((tmp_path / "m" / ".okf/access.json").read_text())
        assert acl["Leave.md"] == ["group:HR Members", "user:alice@contoso.com"]
        assert acl["Finance/Handbook.md"] == ["*"]
        assert acl["Finance/Budget.md"] == ["group:Finance"]
        assert (tmp_path / "m" / "Leave.md").read_bytes().startswith(b"# Leave")

    def test_group_ids(self):
        perms = [
            {
                "grantedToIdentitiesV2": [
                    {"group": {"id": "g-42", "displayName": "Finance"}}
                ]
            }
        ]
        assert sharepoint._principals_from(perms, "id") == ["group:g-42"]

    def test_missing_drive(self):
        client = graph_server()
        with pytest.raises(ValueError, match="drive 'Nope' not found"):
            sharepoint.SharePointSource(
                client, "graph-token", "contoso.sharepoint.com:/sites/HR", "Nope"
            )


def rsa_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return key, {
        "client_email": "okf@proj.iam.gserviceaccount.com",
        "private_key": pem.decode(),
        "private_key_id": "k1",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def drive_server(public_key):
    import base64

    def handler(req: httpx.Request) -> httpx.Response:
        path, params = req.url.path, dict(req.url.params)
        if req.url.host == "oauth2.googleapis.com":
            form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
            header, claims, signature = form["assertion"].split(".")
            pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding

            public_key.verify(
                pad(signature),
                f"{header}.{claims}".encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            assert json.loads(pad(claims))["scope"].endswith("drive.readonly")
            return httpx.Response(200, json={"access_token": "drive-token"})
        assert req.headers["Authorization"] == "Bearer drive-token"
        if path == "/drive/v3/files/root1":
            return httpx.Response(200, json={"id": "root1", "mimeType": gdrive.FOLDER})
        if path == "/drive/v3/files":
            assert params["supportsAllDrives"] == "true"
            if "'root1' in parents" in params["q"]:
                if params.get("pageToken") != "p2":
                    return httpx.Response(
                        200,
                        json={
                            "nextPageToken": "p2",
                            "files": [
                                {
                                    "id": "doc1",
                                    "name": "Plan",
                                    "mimeType": "application/vnd.google-apps.document",
                                    "modifiedTime": "2026-01-01T00:00:00Z",
                                    "permissions": [
                                        {
                                            "type": "group",
                                            "emailAddress": "ops@corp.com",
                                        }
                                    ],
                                }
                            ],
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "files": [
                            {
                                "id": "sub",
                                "name": "Specs",
                                "mimeType": "application/vnd.google-apps.folder",
                            },
                            {
                                "id": "form1",
                                "name": "Survey",
                                "mimeType": "application/vnd.google-apps.form",
                            },
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "id": "md1",
                            "name": "notes.md",
                            "mimeType": "text/markdown",
                            "md5Checksum": "abc",
                            "size": "12",
                            "permissions": [
                                {"type": "domain", "domain": "corp.com"},
                                {"type": "user", "emailAddress": "bo@corp.com"},
                            ],
                        }
                    ]
                },
            )
        if path == "/drive/v3/files/doc1/export":
            assert "wordprocessingml" in params["mimeType"]
            return httpx.Response(200, content=b"DOCX-BYTES")
        if path == "/drive/v3/files/md1":
            assert params["alt"] == "media"
            return httpx.Response(200, content=b"# Notes\n\nhi\n")
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


class TestGoogleDrive:
    def test_sync_exports_and_permissions(self, tmp_path):
        key, key_json = rsa_key()
        client = drive_server(key.public_key())
        token = gdrive.get_token(client, key_json)
        result = sync(
            "ops", tmp_path / "m", gdrive.DriveSource(client, token, "root1").items()
        )
        assert sorted(result.downloaded) == ["Plan.docx", "Specs/notes.md"]
        assert (tmp_path / "m" / "Plan.docx").read_bytes() == b"DOCX-BYTES"
        acl = json.loads((tmp_path / "m" / ".okf/access.json").read_text())
        assert acl == {
            "Plan.docx": ["group:ops@corp.com"],
            "Specs/notes.md": ["*", "user:bo@corp.com"],
        }

    def test_folder_id_is_validated(self):
        with pytest.raises(ValueError, match="not a Drive folder ID"):
            gdrive.DriveSource(httpx.Client(), "t", "x' or '1'='1")

    def test_non_rsa_key_rejected(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        with pytest.raises(ValueError, match="RSA"):
            gdrive.service_account_assertion(
                {"client_email": "x", "private_key": pem.decode()}
            )


class TestConfigAndRefresh:
    def write(self, tmp_path, text):
        path = tmp_path / "connectors.yaml"
        path.write_text(text)
        return path

    @pytest.mark.parametrize(
        "text, message",
        [
            ("sources: []", "mirror"),
            ("mirror: /m\nsources: []", "sources"),
            ("mirror: /m\nsources: [{name: A, kind: fileshare, path: /x}]", "slugs"),
            ("mirror: /m\nsources: [{name: a, kind: ftp}]", "kind"),
            (
                "mirror: /m\nsources: [{name: a, kind: sharepoint, tenant_id: t, client_id: c, client_secret: x, site: s}]",
                "environment variable",
            ),
            ("mirror: /m\nsources: [{name: a, kind: gdrive}]", "missing"),
        ],
    )
    def test_validation(self, tmp_path, text, message):
        with pytest.raises(ValueError, match=message):
            connectors.load(self.write(tmp_path, text))

    def test_missing_secret_env_is_reported_per_source(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OKF_TEST_SECRET", raising=False)
        cfg = connectors.load(
            self.write(
                tmp_path,
                f"""mirror: {tmp_path / "mirror"}
sources:
  - {{name: sp, kind: sharepoint, tenant_id: t, client_id: c, client_secret_env: OKF_TEST_SECRET, site: s}}
""",
            )
        )
        [result] = connectors.run(cfg)
        assert "OKF_TEST_SECRET" in result.errors["<listing>"]

    def test_refresh_fileshare_end_to_end(self, tmp_path, capsys, monkeypatch):
        from okf_mcp_server.src.knowledge import service
        from okf_mcp_server.src.knowledge.access import Identity
        from okf_mcp_server.src.tools import search_knowledge_tool

        share = tmp_path / "share"
        share.mkdir()
        (share / "open.md").write_text("# Open\n\nThe canteen opens at 8.\n")
        (share / "secret.md").write_text("# Secret\n\nThe canteen code is 4471.\n")
        os.chmod(share / "open.md", 0o644)
        os.chmod(share / "secret.md", 0o600)
        cfg = self.write(
            tmp_path,
            f"mirror: {tmp_path / 'mirror'}\nsources:\n  - {{name: share, kind: fileshare, path: {share}}}\n",
        )
        audit = tmp_path / "audit.jsonl"
        monkeypatch.setattr(service.settings, "OKF_AUDIT_LOG", str(audit))
        data = tmp_path / "data"
        assert (
            cli_main(["--data-dir", str(data), "refresh", str(cfg), "--no-embeddings"])
            == 0
        )
        out = capsys.readouterr().out
        assert "synced share: 2 downloaded" in out and "published" in out
        assert set(load_source_acls(tmp_path / "mirror")) == {
            "share/open.md",
            "share/secret.md",
        }

        monkeypatch.setattr(service.settings, "OKF_DATA_DIR", str(data))
        monkeypatch.setattr(service.settings, "ENABLE_AUTH", True)
        monkeypatch.setattr(
            search_knowledge_tool, "caller", lambda: Identity(subject="someone-else")
        )
        found = {
            r["concept_id"]
            for r in search_knowledge_tool.search_knowledge("canteen")["results"]
        }
        assert found == {"share/open"}  # world-readable only; secret.md is owner-only
        events = [json.loads(line)["event"] for line in audit.read_text().splitlines()]
        assert events == ["ingest.sync", "ingest.build", "tool.search_knowledge"]

    def test_refresh_does_not_build_after_failed_sync(self, tmp_path, capsys):
        cfg = self.write(
            tmp_path,
            f"mirror: {tmp_path / 'mirror'}\nsources:\n  - {{name: gone, kind: fileshare, path: {tmp_path / 'missing'}}}\n",
        )
        assert cli_main(["--data-dir", str(tmp_path / "d"), "refresh", str(cfg)]) == 1
        assert "NOT building" in capsys.readouterr().err
        assert cli_main(["--data-dir", str(tmp_path / "d"), "sync", str(cfg)]) == 1


def test_source_default_access_and_okf_config(tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    (share / "a.md").write_text("# A\n\ntext\n")
    rules = tmp_path / "_okf.yaml"
    rules.write_text("types: {share/: Policy}\n")
    cfg_file = tmp_path / "connectors.yaml"
    cfg_file.write_text(
        f"mirror: {tmp_path / 'mirror'}\nokf_config: {rules}\nsources:\n"
        f"  - {{name: share, kind: fileshare, path: {share}, permissions: none, access: [group:HR]}}\n"
    )
    cfg = connectors.load(cfg_file)
    connectors.run(cfg)
    assert (tmp_path / "mirror" / "_okf.yaml").read_text() == rules.read_text()
    assert load_source_acls(tmp_path / "mirror") == {"share/a.md": ["group:hr"]}
    cfg_file.write_text(
        f"mirror: /m\nokf_config: {tmp_path / 'nope.yaml'}\nsources: [{{name: s, kind: fileshare, path: /x}}]\n"
    )
    with pytest.raises(ValueError, match="okf_config not found"):
        connectors.load(cfg_file)


class TestSyncSafety:
    """Review findings: collisions, empty listings, leaked tokens, removed sources."""

    def test_case_and_unicode_collisions_get_distinct_files(self, tmp_path):
        mirror = tmp_path / "m"
        sync(
            "s",
            mirror,
            [
                item("1", "Report.md", content=b"# Public\n"),
                item("2", "report.md", principals=["group:hr"], content=b"# Secret\n"),
                item("3", "café.md"),
                item("4", "café.md"),
            ],
        )
        acl = json.loads((mirror / ".okf/access.json").read_text())
        assert len(acl) == 4 and acl["Report.md"] == ["*"]
        secret = next(p for p, a in acl.items() if a == ["group:hr"])
        assert (mirror / secret).read_bytes() == b"# Secret\n"
        assert (mirror / "Report.md").read_bytes() == b"# Public\n"

    def test_case_only_rename_keeps_the_file(self, tmp_path):
        mirror = tmp_path / "m"
        sync("s", mirror, [item("1", "Policy.md")])
        result = sync("s", mirror, [item("1", "policy.md", "v2")])
        assert result.deleted == []
        assert [p.name for p in mirror.iterdir() if p.is_file()] in (
            ["policy.md"],
            ["Policy.md"],  # case-insensitive filesystems may keep the old case
        )

    def test_empty_listing_keeps_the_mirror(self, tmp_path):
        mirror = tmp_path / "m"
        sync("s", mirror, [item("1", "a.md")])
        result = sync("s", mirror, [])
        assert "<listing>" in result.errors and (mirror / "a.md").is_file()

    def test_download_errors_do_not_leak_urls_with_tokens(self, tmp_path):
        def fail(target):
            req = httpx.Request(
                "GET", "https://x.sharepoint.com/d.aspx?tempauth=SECRET"
            )
            raise httpx.HTTPStatusError(
                "boom " + str(req.url), request=req, response=httpx.Response(503)
            )

        bad = RemoteItem(key="1", path="a.md", version="v", download=fail)
        result = sync("s", tmp_path / "m", [bad])
        assert "SECRET" not in str(result.errors) and "503" in result.errors["a.md"]

    def test_long_names_keep_their_extension(self):
        rel = safe_relpath("é" * 150 + ".pdf")
        assert rel.endswith(".pdf") and len(rel.encode()) <= 200

    def test_removed_source_and_okf_config_are_unpublished(self, tmp_path):
        share = tmp_path / "share"
        share.mkdir()
        (share / "a.md").write_text("# A\n\ntext\n")
        rules = tmp_path / "_okf.yaml"
        rules.write_text("types: {}\n")
        mirror = tmp_path / "mirror"
        both = (
            f"mirror: {mirror}\nokf_config: {rules}\nsources:\n"
            f"  - {{name: one, kind: fileshare, path: {share}}}\n"
            f"  - {{name: two, kind: fileshare, path: {share}}}\n"
        )
        cfg_file = tmp_path / "connectors.yaml"
        cfg_file.write_text(both)
        connectors.run(connectors.load(cfg_file))
        assert (mirror / "two" / "a.md").is_file()
        cfg_file.write_text(
            f"mirror: {mirror}\nsources:\n  - {{name: one, kind: fileshare, path: {share}}}\n"
        )
        results = connectors.run(connectors.load(cfg_file))
        assert not (mirror / "two").exists() and not (mirror / "_okf.yaml").exists()
        assert any(r.source == "two" and r.deleted for r in results)

    def test_drive_items_without_permissions_use_the_default(self):
        def handler(req):
            if req.url.path == "/drive/v3/files/root1":
                return httpx.Response(200, json={"mimeType": gdrive.FOLDER})
            return httpx.Response(
                200,
                json={
                    "files": [{"id": "f", "name": "a.md", "mimeType": "text/markdown"}]
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        (only,) = gdrive.DriveSource(client, "t", "root1").items()
        assert only.principals is None

    def test_drive_folder_that_is_not_a_folder_fails_listing(self):
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"mimeType": "application/pdf"})
            )
        )
        with pytest.raises(ValueError, match="not a folder"):
            list(gdrive.DriveSource(client, "t", "root1").items())

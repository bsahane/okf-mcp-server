"""End-to-end check of per-user access with a real OIDC provider (Keycloak).

Starts Keycloak and PostgreSQL in Docker, builds a corpus with access rules,
runs the MCP server with authentication on, and checks as real users:

- no token -> 401; token for another client (wrong audience) -> 403; refresh token rejected
- alice (finance) and bob (hr) each see only their documents plus public
- get_knowledge on a denied document answers "not found"
- revoking bob's session makes his token stop working (401)
- removing carol from hr removes her access on her next token
- every call lands in the audit log with the caller

Usage (Docker running, venv with dev deps):
    python tests/e2e/auth_e2e.py
Secrets are generated per run and never written to the repository.
"""

import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ROOT = Path(__file__).resolve().parents[2]
KEYCLOAK_IMAGE = "quay.io/keycloak/keycloak:26.4"
POSTGRES_IMAGE = "postgres:15-alpine"
PREFIX = "okf-e2e"
CORPUS = {
    "finance/travel.md": "# Travel\n\nThe hotel limit is 150 GBP per night.\n",
    "hr/salaries.md": "# Salaries\n\nThe hotel allowance for relocation is 500 GBP.\n",
    "public/handbook.md": "# Handbook\n\nBook every hotel through the travel portal.\n",
    "_okf.yaml": "access:\n  finance/: [group:finance]\n  hr/: [group:hr]\n  public/: ['*']\n",
}
CHECKS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok))
    print(
        f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + detail) if detail and not ok else ''}"
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def docker(*args: str, check_rc: bool = True) -> str:
    out = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check_rc and out.returncode:
        raise RuntimeError(f"docker {' '.join(args[:2])}: {out.stderr.strip()}")
    return out.stdout.strip()


def realm(client_secret: str, passwords: dict) -> dict:
    user = lambda name, group: {  # noqa: E731
        "username": name,
        "enabled": True,
        "email": f"{name}@example.com",
        "emailVerified": True,
        "firstName": name.title(),
        "lastName": "Test",
        "credentials": [
            {"type": "password", "value": passwords[name], "temporary": False}
        ],
        "groups": [f"/{group}"],
    }
    return {
        "realm": "okf",
        "enabled": True,
        "groups": [{"name": "finance"}, {"name": "hr"}],
        "clients": [
            {  # the MCP server itself: introspects tokens
                "clientId": "okf-mcp",
                "enabled": True,
                "publicClient": False,
                "secret": client_secret,
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": True,
            },
            {  # what users sign in with; tokens are addressed to okf-mcp
                "clientId": "okf-cli",
                "enabled": True,
                "publicClient": True,
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": True,
                "protocolMappers": [
                    {
                        "name": "audience-okf-mcp",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-audience-mapper",
                        "config": {
                            "included.client.audience": "okf-mcp",
                            "access.token.claim": "true",
                            "introspection.token.claim": "true",
                        },
                    },
                    {
                        "name": "groups",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-group-membership-mapper",
                        "config": {
                            "claim.name": "groups",
                            "full.path": "true",
                            "access.token.claim": "true",
                            "introspection.token.claim": "true",
                            "id.token.claim": "false",
                            "userinfo.token.claim": "true",
                        },
                    },
                ],
            },
            {  # an unrelated app in the same realm: its tokens must be refused
                "clientId": "other-app",
                "enabled": True,
                "publicClient": True,
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": True,
            },
        ],
        "users": [user("alice", "finance"), user("bob", "hr"), user("carol", "hr")],
    }


def token(kc: str, username: str, password: str, client_id: str = "okf-cli") -> dict:
    response = httpx.post(
        f"{kc}/realms/okf/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": client_id,
            "username": username,
            "password": password,
            "scope": "openid",
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def admin(kc: str, admin_password: str) -> httpx.Client:
    t = httpx.post(
        f"{kc}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": "admin",
            "password": admin_password,
        },
        timeout=20,
    ).json()["access_token"]
    return httpx.Client(
        base_url=f"{kc}/admin/realms/okf",
        headers={"Authorization": f"Bearer {t}"},
        timeout=20,
    )


async def mcp(url: str, bearer: str, tool: str, args: dict):
    transport = StreamableHttpTransport(
        url, headers={"Authorization": f"Bearer {bearer}"}
    )
    async with Client(transport, timeout=30) as client:
        return await client.call_tool(tool, args, raise_on_error=False)


def visible(url: str, bearer: str) -> set:
    result = asyncio.run(
        mcp(url, bearer, "search_knowledge", {"query": "hotel", "limit": 20})
    )
    return {r["concept_id"] for r in result.structured_content["results"]}


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="okf-e2e-"))
    kc_port, pg_port, mcp_port = free_port(), free_port(), free_port()
    kc = f"http://localhost:{kc_port}"
    url = f"http://localhost:{mcp_port}/mcp"
    admin_password, client_secret, pg_password = (
        secrets.token_urlsafe(18) for _ in range(3)
    )
    passwords = {u: secrets.token_urlsafe(12) for u in ("alice", "bob", "carol")}
    server = None
    try:
        (work / "realm").mkdir()
        (work / "realm" / "okf.json").write_text(
            json.dumps(realm(client_secret, passwords))
        )
        os.chmod(work / "realm", 0o755)
        os.chmod(work / "realm" / "okf.json", 0o644)
        print("starting Keycloak and PostgreSQL ...")
        docker(
            "run",
            "-d",
            "--rm",
            "--name",
            f"{PREFIX}-kc",
            "-p",
            f"127.0.0.1:{kc_port}:8080",
            "-e",
            "KC_BOOTSTRAP_ADMIN_USERNAME=admin",
            "-e",
            f"KC_BOOTSTRAP_ADMIN_PASSWORD={admin_password}",
            "-v",
            f"{work / 'realm'}:/opt/keycloak/data/import:ro",
            KEYCLOAK_IMAGE,
            "start-dev",
            "--import-realm",
        )
        docker(
            "run",
            "-d",
            "--rm",
            "--name",
            f"{PREFIX}-pg",
            "-p",
            f"127.0.0.1:{pg_port}:5432",
            "-e",
            "POSTGRES_USER=okf",
            "-e",
            f"POSTGRES_PASSWORD={pg_password}",
            "-e",
            "POSTGRES_DB=okf",
            POSTGRES_IMAGE,
        )
        for _ in range(120):
            try:
                if (
                    httpx.get(
                        f"{kc}/realms/okf/.well-known/openid-configuration", timeout=2
                    ).status_code
                    == 200
                ):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            raise RuntimeError("Keycloak did not start")

        src, data = work / "src", work / "data"
        for rel, text in CORPUS.items():
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_text(text)
        ingest = subprocess.run(
            [
                sys.executable,
                "-m",
                "okf_mcp_server.src.ingest.cli",
                "--data-dir",
                str(data),
                "build",
                "--no-embeddings",
                str(src),
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        check(
            "corpus with access rules builds",
            ingest.returncode == 0,
            ingest.stderr[-300:],
        )

        audit_log = work / "audit.jsonl"
        env = {
            **os.environ,
            "ENABLE_AUTH": "True",
            "USE_EXTERNAL_BROWSER_AUTH": "False",
            "ENVIRONMENT": "development",
            "MCP_HOST": "localhost",
            "MCP_PORT": str(mcp_port),
            "MCP_TRANSPORT_PROTOCOL": "streamable-http",
            "SSO_CLIENT_ID": "okf-mcp",
            "SSO_CLIENT_SECRET": client_secret,
            "SSO_INTROSPECTION_URL": f"{kc}/realms/okf/protocol/openid-connect/token/introspect",
            "SSO_AUTHORIZATION_URL": f"{kc}/realms/okf/protocol/openid-connect/auth",
            "SSO_TOKEN_URL": f"{kc}/realms/okf/protocol/openid-connect/token",
            "SSO_CALLBACK_URL": f"http://localhost:{mcp_port}/auth/callback/oidc",
            "SSO_SCOPES": "openid",
            "SESSION_SECRET": secrets.token_urlsafe(32),
            "POSTGRES_HOST": "localhost",
            "POSTGRES_PORT": str(pg_port),
            "POSTGRES_DB": "okf",
            "POSTGRES_USER": "okf",
            "POSTGRES_PASSWORD": pg_password,
            "OKF_DATA_DIR": str(data),
            "OKF_REQUIRED_AUDIENCE": "okf-mcp",
            "OKF_DEFAULT_ACCESS": "deny",
            "OKF_AUDIT_LOG": str(audit_log),
            "OKF_SEMANTIC_SEARCH": "False",
        }
        server = subprocess.Popen(
            [sys.executable, "-m", "okf_mcp_server.src.main"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(90):
            try:
                if (
                    httpx.get(
                        f"http://localhost:{mcp_port}/health", timeout=2
                    ).status_code
                    == 200
                ):
                    break
            except httpx.HTTPError:
                pass
            if server.poll() is not None:
                raise RuntimeError(
                    "server exited:\n" + (server.stdout.read() if server.stdout else "")
                )
            time.sleep(1)

        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "e2e", "version": "1"},
            },
        }
        headers = {"Accept": "application/json, text/event-stream"}
        r = httpx.post(url, json=init, headers=headers, timeout=10)
        check("no token -> 401", r.status_code == 401, str(r.status_code))
        other = token(kc, "alice", passwords["alice"], client_id="other-app")[
            "access_token"
        ]
        r = httpx.post(
            url,
            json=init,
            headers={**headers, "Authorization": f"Bearer {other}"},
            timeout=10,
        )
        check(
            "token for another client (wrong audience) -> 403",
            r.status_code == 403,
            str(r.status_code),
        )

        refresh = token(kc, "alice", passwords["alice"])["refresh_token"]
        r = httpx.post(
            url,
            json=init,
            headers={**headers, "Authorization": f"Bearer {refresh}"},
            timeout=10,
        )
        check(
            "refresh token used as a bearer token -> rejected",
            r.status_code in (401, 403),
            str(r.status_code),
        )

        alice = token(kc, "alice", passwords["alice"])["access_token"]
        bob_tokens = token(kc, "bob", passwords["bob"])
        bob = bob_tokens["access_token"]
        seen = visible(url, alice)
        check(
            "alice (finance) sees finance + public only",
            seen == {"finance/travel", "public/handbook"},
            str(seen),
        )
        seen = visible(url, bob)
        check(
            "bob (hr) sees hr + public only",
            seen == {"hr/salaries", "public/handbook"},
            str(seen),
        )
        denied = asyncio.run(
            mcp(url, bob, "get_knowledge", {"concept_id": "finance/travel"})
        )
        check(
            "bob get_knowledge(finance/travel) -> not found",
            denied.is_error and "not found" in denied.content[0].text,
        )
        ok = asyncio.run(
            mcp(url, alice, "get_knowledge", {"concept_id": "finance/travel"})
        )
        check(
            "alice get_knowledge(finance/travel) -> content",
            not ok.is_error and "150 GBP" in ok.structured_content["content"],
        )
        root = asyncio.run(mcp(url, bob, "browse_knowledge", {})).structured_content[
            "entries"
        ]
        check(
            "bob browse root hides finance",
            {e.get("path") for e in root} == {"hr", "public"},
            str(root),
        )

        with admin(kc, admin_password) as api:
            bob_id = api.get(
                "/users", params={"username": "bob", "exact": "true"}
            ).json()[0]["id"]
            api.post(f"/users/{bob_id}/logout").raise_for_status()
            r = httpx.post(
                url,
                json=init,
                headers={**headers, "Authorization": f"Bearer {bob}"},
                timeout=10,
            )
            check(
                "bob's token after session revocation -> 401",
                r.status_code == 401,
                str(r.status_code),
            )

            carol_before = visible(
                url, token(kc, "carol", passwords["carol"])["access_token"]
            )
            carol_id = api.get(
                "/users", params={"username": "carol", "exact": "true"}
            ).json()[0]["id"]
            hr_id = next(
                g["id"] for g in api.get("/groups").json() if g["name"] == "hr"
            )
            api.delete(f"/users/{carol_id}/groups/{hr_id}").raise_for_status()
            carol_after = visible(
                url, token(kc, "carol", passwords["carol"])["access_token"]
            )
            check(
                "carol loses hr documents after leaving the group",
                "hr/salaries" in carol_before and carol_after == {"public/handbook"},
                f"{carol_before} -> {carol_after}",
            )

        records = (
            [json.loads(line) for line in audit_log.read_text().splitlines()]
            if audit_log.exists()
            else []
        )
        emails = {r["caller"]["email"] for r in records}
        check(
            "audit log records every caller",
            {"alice@example.com", "bob@example.com", "carol@example.com"} <= emails,
            str(emails),
        )
        check(
            "audit log records the denied fetch",
            any(
                r["tool"] == "get_knowledge" and r.get("outcome") == "denied"
                for r in records
            ),
        )
    finally:
        if server:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
        docker("rm", "-f", f"{PREFIX}-kc", f"{PREFIX}-pg", check_rc=False)
        shutil.rmtree(work, ignore_errors=True)
    failed = [name for name, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed or not CHECKS else 0


if __name__ == "__main__":
    sys.exit(main())

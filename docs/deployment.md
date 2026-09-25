# Deployment Guide

## Using Podman / Docker Compose

1. **Build and run with Podman Compose:**
   ```bash
   podman compose up --build
   ```

2. **Or build manually:**
   ```bash
   podman build -t okf-mcp-server .
   podman run -p 5001:5001 --env-file .env okf-mcp-server
   ```

## Deploying to OpenShift

Manifests are a shared `deployment/base/` (Deployment, Service, ConfigMap, Secret, PVC, refresh CronJob, connectors ConfigMap) with two overlays: `deployment/openshift/` adds BuildConfig, ImageStream and Route; `deployment/kubernetes/` sets a local image for plain Kubernetes. See the README section *Deploy on Kubernetes or OpenShift*.

Quick start:

```bash
# Deploy to OpenShift namespace
make deploy openshift NAMESPACE=your-project-name

# Remove deployment
make undeploy openshift
```

### PostgreSQL Requirement

The OpenShift manifests **do not include a PostgreSQL deployment**. If you enable authentication (`ENABLE_AUTH=True`), you must provision PostgreSQL separately and point `POSTGRES_HOST`/`POSTGRES_PORT`/`POSTGRES_DB` in `deployment/base/configmap.yaml` and the credentials in `deployment/base/secret.yaml` to it.

Common approaches:

| Option | When to Use |
|--------|------------|
| [CloudNativePG Operator](https://cloudnative-pg.io/) | Recommended for Kubernetes/OpenShift — operator-managed, HA-ready, Apache 2.0 licensed |
| Managed cloud DB (RDS, Cloud SQL, Azure DB) | Hybrid deployments or when the cluster is not self-hosted |
| Standalone PostgreSQL Pod | Quick testing only — not recommended for production |

Update these values in `deployment/base/secret.yaml` before deploying:

```yaml
POSTGRES_DB: "your-db-name"
POSTGRES_USER: "your-db-user"
POSTGRES_PASSWORD: "your-secure-password"
```

The MCP server also needs `POSTGRES_HOST` and `POSTGRES_PORT` — set these in `deployment/base/configmap.yaml` to match your database endpoint.

> **Without auth:** If `ENABLE_AUTH=False`, PostgreSQL is not required and the `POSTGRES_*` values are ignored.

## Container Build (`Containerfile`)

The `Containerfile` is a single stage on a Red Hat UBI base (`--build-arg EXTRAS=ingest` adds Docling, CPU PyTorch and baked models for the ingest image):

| Stage | What Happens |
|-------|-------------|
| **Base image** | `registry.access.redhat.com/ubi9/python-312:latest` — Red Hat UBI 9 with Python 3.12 |
| **Dependency install** | Switches to `root`, copies `pyproject.toml`, installs `uv`, creates a venv, and installs runtime deps via `uv pip install -r pyproject.toml` |
| **Source copy** | Copies `okf_mcp_server/` into `/app` (dev deps and tests are excluded) |
| **Environment** | Sets `VIRTUAL_ENV`, prepends venv to `PATH`, sets `PYTHONPATH=/app` |
| **User** | Runs as UID `1001` (non-root; OpenShift assigns its own UID) |
| **Entrypoint** | `CMD ["/app/.venv/bin/python", "-m", "okf_mcp_server.src.main"]` |

Port **5001** is exposed via `EXPOSE 5001`.

> **Rename note:** When forking the template, update the `COPY` source path and the `CMD` module path in `Containerfile` to match your package name.

## Container Configuration

The `compose.yaml` maps container port 5001 to host port 5001 and overrides `MCP_HOST=0.0.0.0` so the server binds to all interfaces inside the container (required for port forwarding to work).

- **Health Check**: `curl -f http://localhost:5001/health`

## Manual Testing

1. **Container testing:**
   ```bash
   podman compose up -d
   curl -f http://localhost:5001/health
   podman compose down
   ```

2. **SSL testing (if configured):**
   ```bash
   curl -k https://localhost:5001/health
   ```

3. **Import validation:**
   ```bash
   podman run --rm okf-mcp-server python -c "import okf_mcp_server; print('OK')"
   ```

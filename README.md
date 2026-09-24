# OKF MCP Server

Retrieval over enterprise documents converted to [Open Knowledge Format (OKF) v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md), exposed to AI applications through MCP.

Built on the [Red Hat template MCP server](https://github.com/redhat-data-and-ai/template-mcp-server/tree/286b9e7b732af9d58f9dfd9be68c071e55919ea4) (commit `286b9e7`) and following its conventions. The design and its open decisions are in [PLAN.md](PLAN.md).

```
source folder ──okf-ingest build──► snapshot (OKF bundle + extraction metadata + FTS5 index) ──► MCP server ──► AI client
 PDF DOCX XLSX      Docling / native      validated, then `current` switched atomically       browse / search / get
 PPTX HTML MD TXT
```

## Status

Pilot for **one trusted local operator**. Authentication is off and the server only answers loopback Host/Origin headers. Tools **fail closed** when `ENABLE_AUTH=True`, because per-user permission filtering is not implemented yet (PLAN.md phase 5). Use only documents approved for the machine running it.

## Quick start

```bash
make install          # venv + dev deps + pre-commit; opens a subshell, `exit` when done
make ingest-install   # adds Docling (large; PDF needs a one-time model download)
make sample           # publishes ./data from samples/finance
make local            # serves http://localhost:5001/mcp
```

Connect a local MCP client, for example Claude Code:

```bash
claude mcp add --transport http okf http://localhost:5001/mcp
```

Port 5001 is the template default; set `MCP_PORT` in `.env` if it is taken.

## Ingesting your documents

```bash
okf-ingest build /path/to/documents            # new snapshot, published only if valid
okf-ingest validate                            # OKF v0.2 §11 conformance of the published bundle
okf-ingest eval questions.yaml                 # Hit@5 over your question set
```

- **One concept per source file.** Folders are mirrored; same-stem files and the reserved names `index`/`log` are disambiguated.
- **Deterministic frontmatter.** `type`, `status`, `tags`, `description`, `stale_after` and `verified` come from an optional `_okf.yaml` in the source root (see [samples/finance/_okf.yaml](samples/finance/_okf.yaml)). No generative model is used.
- **Citations survive rebuilds.** Page, sheet, section and line locations are stored per snapshot in `extraction/<source-id>.json` (including Docling's JSON), not guessed from Markdown.
- **Incremental.** Unchanged files (same SHA-256) reuse the previous extraction. Renames are detected by content, and deleted files disappear from the new snapshot.
- **Safe publishing.** If any file fails to extract, or the bundle fails validation, the new snapshot is discarded and the current one keeps serving. Use `--allow-failures` to publish anyway; `failures.json` then lists the failed files.
- **Offline.** Pre-download Docling models and set `DOCLING_ARTIFACTS_PATH` (or `--artifacts-path`).

Snapshot layout under `OKF_DATA_DIR` (default `./data`):

```
current -> snapshots/<id>
snapshots/<id>/bundle/        OKF bundle (index.md per directory, log.md)
snapshots/<id>/extraction/    per-source extraction metadata
snapshots/<id>/index.db       SQLite FTS5 passage index
snapshots/<id>/manifest.json  source ID -> concept, revision
```

## MCP tools

| Tool | Purpose |
| --- | --- |
| `browse_knowledge(path, page_size, cursor)` | One directory at a time (OKF progressive disclosure). |
| `search_knowledge(query, type, tags, limit)` | BM25 keyword search. Each result carries `status`, trust tier, `stale` and a source location. Deprecated concepts rank after current ones. |
| `get_knowledge(concept_id, section, page_size, cursor)` | A concept or one section, with provenance and continuation. |

Failures reach clients as MCP `isError: true` (tools raise `ToolError`). Paths are confined to the bundle, including through symlinks. Output is bounded and paged. Cursors are tied to a snapshot: after a new snapshot is published, a client must restart from the first page.

## Development

```bash
make lint && make test        # ruff, mypy, pytest (coverage ≥ 80% in CI)
pytest -m docling             # Docling adapter; needs the ingest extra
make pre-commit
```

CI runs the main suite without Docling (the adapter is excluded from coverage) plus a separate `ingest` job with the extra installed.

See [docs/](docs/) for the template's architecture, authentication, deployment and CI guides.

## License

[Apache 2.0](LICENSE), as the template it is based on.

# Verification — 24 September 2026

The local pilot works end to end after the fixes below. This does not verify readiness for shared enterprise deployment.

## Results

| Check | Result |
| --- | --- |
| Original test suite | 379 passed before changes |
| Final suite, including new regressions and HTTP integration | 392 passed |
| Coverage | 89.34%, above the configured 80% threshold |
| Ruff and mypy | Passed |
| Pre-commit hooks, including Bandit | Passed |
| Real DOCX and XLSX conversion | Passed through the existing Docling adapter tests |
| Sample ingestion | 6 concepts, 14 passages, no extraction failures |
| Sample retrieval | Hit@5 = 1.0 for 8 answerable questions; all configured expected concepts retrieved |
| Actual Streamable HTTP MCP application | Startup, discovery, browse/search/fetch, citations, tool errors and Host/Origin rejection passed |
| Container runtime | Not verified: Docker daemon unavailable; Podman not installed |

Tests ran locally on Python 3.12.2. Coverage excludes the Docling adapter as configured by the project; it is exercised separately by the real conversion tests. The sample evaluation checks expected concept IDs, not individual supporting passages or the correctness of an AI-generated answer. The one unanswerable sample does not prove model abstention. Two upstream deprecation warnings remain (Starlette/AnyIO and Authlib).

## Fixed issues

- **Source confinement:** file symlinks outside the selected source folder could import unintended data. They now produce a per-file failure and block publication by default.
- **Self-ingestion:** placing the snapshot directory inside the input folder let later runs discover prior output. This layout is now rejected before creating output. Set `--data-dir` to a location outside the source folder.
- **Read failures:** errors while reading or statting a source previously escaped the build report. They are now reported per file, with the previous snapshot preserved and the incomplete build removed by the normal failure path.
- **Text preservation:** `.txt` files beginning with `---` could lose content as though it were YAML frontmatter. Only Markdown files undergo frontmatter removal, using an exact delimiter line and correct source-line offsets.
- **Section identity:** headings such as `A`, another `A`, and `A-2` could receive duplicate IDs, causing section fetches and citations to select the wrong content. Section IDs are now unique within each document.
- **Metadata validation:** malformed root-index YAML and non-list `sources` could crash validation. Validation now returns problems and also checks verification-event identity/timestamps.
- **Partial extraction:** incomplete Docling conversion results could be treated as successful documents. Only successful conversions are accepted.
- **Oversized excerpts:** a single large table row could bypass the passage-size limit. Search excerpts are now capped at 1,500 characters with `excerpt_truncated`; the original section remains available through paged fetches.

Added regression coverage for these cases and a reusable real HTTP smoke test in `tests/test_http.py`. Updated README instructions for the changed behavior. No deployment, source-data migration or commit was performed.

## Next improvements, in priority order

1. **Before sharing the server:** implement authenticated identity propagation and source-permission enforcement. The current tools deliberately refuse access when authentication is enabled; OAuth alone does not finish phase 5.
2. **Before trusting enterprise answers:** use representative documents and about 30 real questions, with expected sections/passages as well as document IDs. Include conflicting versions, absent answers, multilingual content and scanned PDFs. Current synthetic results are too small to justify retrieval-quality claims.
3. **Before deployment:** run the container and offline PDF/OCR checks on the intended Linux host. This pass did not exercise scanned PDF conversion or an offline model installation.
4. **Before increasing corpus size:** measure extraction time, index size and query latency. The current process reads source files into memory and retains snapshots. Set retention and workload limits based on measurements; add semantic retrieval if the real question set shows keyword misses.

## Reproduce

```bash
make lint
.venv/bin/python -m pytest --cov=okf_mcp_server --cov-report=term --cov-fail-under=80 -q
make pre-commit
.venv/bin/python -m pytest tests/test_http.py -q
```

The complete suite needs the ingestion extra for its DOCX/XLSX checks; without it, those adapter tests are skipped. HTTP verification uses a temporary synthetic corpus and an ephemeral loopback port, and stops the server afterward.

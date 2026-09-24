"""Tests for ingestion, snapshots, validation and evaluation (no Docling needed)."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from okf_mcp_server.src.ingest import pipeline
from okf_mcp_server.src.ingest.cli import main as cli_main
from okf_mcp_server.src.ingest.evaluate import evaluate
from okf_mcp_server.src.ingest.extract import (
    ExtractedDoc,
    ExtractedSection,
    extract,
    extract_native,
)
from okf_mcp_server.src.ingest.pipeline import assign_concept_ids, build, render_body
from okf_mcp_server.src.knowledge.bundle import (
    BundleError,
    is_stale,
    split_frontmatter,
    split_sections,
    trust_tier,
)
from okf_mcp_server.src.knowledge.index import (
    SearchIndex,
    SearchIndexError,
    _split_long,
    check_fts5,
    format_location,
    fts_query,
)
from okf_mcp_server.src.knowledge.paging import (
    CursorError,
    decode_cursor,
    encode_cursor,
)
from okf_mcp_server.src.knowledge.snapshot import SnapshotError, current_snapshot
from okf_mcp_server.src.knowledge.validate import validate_bundle


@pytest.fixture(autouse=True)
def mock_imports():
    """Override conftest's sys.modules mocking: these tests run the real stack."""
    yield


def concept(data_dir: Path, cid: str):
    """Parse one concept from the published snapshot."""
    path = current_snapshot(data_dir).bundle / f"{cid}.md"
    return split_frontmatter(path.read_text(encoding="utf-8"))


class TestBuild:
    """End-to-end snapshot builds."""

    def test_publishes_conformant_bundle(self, corpus, tmp_path):
        data = tmp_path / "data"
        report = build(corpus, data)
        assert report.published and report.concepts == 4 and not report.failures
        snap = current_snapshot(data)
        assert validate_bundle(snap.bundle) == []
        root_index = (snap.bundle / "index.md").read_text()
        assert root_index.startswith('---\nokf_version: "0.2"\n---')
        assert "[policies](policies/index.md)" in root_index
        assert (snap.bundle / "policies" / "index.md").is_file()
        assert "(deprecated)" in (snap.bundle / "policies" / "index.md").read_text()
        assert "**Creation**" in (snap.bundle / "log.md").read_text()
        manifest = json.loads(snap.manifest.read_text())
        assert {e["concept_id"] for e in manifest["sources"].values()} == {
            "policies/travel",
            "policies/travel-2023",
            "guides/expenses",
            "notes",
        }

    def test_frontmatter_is_deterministic_and_config_driven(self, corpus, tmp_path):
        data = tmp_path / "data"
        build(corpus, data)
        fm, body = concept(data, "policies/travel")
        assert fm["type"] == "Policy" and fm["status"] == "stable"
        assert fm["title"] == "Travel Policy"
        assert fm["tags"] == ["finance", "travel"]
        assert fm["generated"]["by"].startswith("okf-ingest/")
        assert fm["generated"]["at"].endswith("Z")
        assert fm["verified"] == [{"by": "human:owner", "at": "2026-01-01T00:00:00Z"}]
        assert fm["sources"][0]["id"].startswith("src-")
        assert fm["sources"][0]["resource"].startswith("file://")
        assert fm["source_revision"].startswith("sha256:")
        assert trust_tier(fm) == "human-reviewed"
        default_fm, _ = concept(data, "notes")
        assert default_fm["type"] == "Reference" and default_fm["status"] == "draft"
        assert default_fm["title"] == "Notes"

    def test_unchanged_sources_reuse_extraction_and_keep_generated_at(
        self, corpus, tmp_path, monkeypatch
    ):
        data = tmp_path / "data"
        build(corpus, data, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        first_at = concept(data, "notes")[0]["generated"]["at"]
        calls = []
        monkeypatch.setattr(
            pipeline, "extract", lambda p, a=None: calls.append(p) or extract(p)
        )
        (corpus / "notes.txt").write_text("Payroll runs on the 25th.\n")
        report = build(corpus, data, now=datetime(2026, 2, 1, tzinfo=timezone.utc))
        assert report.reused == 3 and [p.name for p in calls] == ["notes.txt"]
        assert (
            concept(data, "notes")[0]["generated"]["at"]
            == "2026-02-01T00:00:00Z"
            != first_at
        )
        assert (
            concept(data, "policies/travel")[0]["generated"]["at"]
            == "2026-01-01T00:00:00Z"
        )
        assert ("Update", "Re-extracted [Notes](/notes.md)") in report.changes

    def test_rename_and_deletion_leave_no_stale_duplicates(self, corpus, tmp_path):
        data = tmp_path / "data"
        build(corpus, data)
        (corpus / "guides" / "expenses.md").rename(corpus / "guides" / "claims.md")
        (corpus / "notes.txt").unlink()
        report = build(corpus, data)
        kinds = [k for k, _ in report.changes]
        assert "Rename" in kinds and "Deletion" in kinds and report.reused == 3
        bundle = current_snapshot(data).bundle
        assert (bundle / "guides" / "claims.md").is_file()
        assert not (bundle / "guides" / "expenses.md").exists()
        assert not (bundle / "notes.md").exists()
        with SearchIndex(current_snapshot(data).index) as index:
            assert {
                r["concept_id"] for r in index.search("payroll claims", limit=20)
            } <= {"guides/claims"}
        log = (bundle / "log.md").read_text()
        assert log.count("## ") == 1 and "Rename" in log and "Creation" in log

    def test_log_keeps_previous_dates(self, corpus, tmp_path):
        data = tmp_path / "data"
        build(corpus, data, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        (corpus / "new.md").write_text("# New\n\ntext\n")
        build(corpus, data, now=datetime(2026, 2, 1, tzinfo=timezone.utc))
        log = (current_snapshot(data).bundle / "log.md").read_text()
        assert log.index("## 2026-02-01") < log.index("## 2026-01-01")

    def test_failed_extraction_is_not_published_by_default(self, corpus, tmp_path):
        data = tmp_path / "data"
        assert build(corpus, data).published
        before = current_snapshot(data).id
        (corpus / "binary.exe").write_bytes(b"\x00")
        report = build(corpus, data)
        assert not report.published and "binary.exe" in report.failures
        assert current_snapshot(data).id == before
        assert not list((data / "snapshots").glob(".building-*"))
        allowed = build(corpus, data, allow_failures=True)
        assert allowed.published
        failures = json.loads(
            (current_snapshot(data).root / "failures.json").read_text()
        )
        assert "binary.exe" in failures

    def test_invalid_config_timestamp_blocks_publish(self, corpus, tmp_path):
        cfg = corpus / "_okf.yaml"
        cfg.write_text(cfg.read_text().replace("2099-01-01T00:00:00Z", "next spring"))
        report = build(corpus, tmp_path / "data")
        assert not report.published
        assert any("stale_after" in p for p in report.problems)

    @pytest.mark.parametrize(
        "config, message",
        [
            ("[1, 2]", "must be a mapping"),
            ("documents: {a.md: {status: live}}", "status"),
            ("documents: {a.md: {verified: [{by: x}]}}", "verified"),
            ("types: [a]", "`types` must be a mapping"),
        ],
    )
    def test_config_errors(self, tmp_path, config, message, corpus_writer):
        src = corpus_writer(tmp_path / "s", {"a.md": "# A\n", "_okf.yaml": config})
        with pytest.raises(ValueError, match=message):
            build(src, tmp_path / "data")

    def test_config_for_missing_file_is_reported(self, tmp_path, corpus_writer):
        src = corpus_writer(
            tmp_path / "s", {"a.md": "# A\n", "_okf.yaml": "documents: {gone.md: {}}"}
        )
        report = build(src, tmp_path / "data")
        assert report.published and "matches no source file" in report.warnings[0]

    def test_missing_source_dir(self, tmp_path):
        with pytest.raises(ValueError, match="not found"):
            build(tmp_path / "nope", tmp_path / "data")

    def test_hidden_files_are_skipped(self, tmp_path, corpus_writer):
        src = corpus_writer(
            tmp_path / "s", {"a.md": "# A\n", ".git/x.md": "# X\n", ".DS_Store": "x"}
        )
        assert build(src, tmp_path / "data").concepts == 1

    def test_output_inside_source_is_rejected(self, corpus):
        with pytest.raises(ValueError, match="outside the source"):
            build(corpus, corpus / "data")
        assert not (corpus / "data").exists()

    def test_source_symlink_cannot_import_outside_files(self, corpus, tmp_path):
        secret = tmp_path / "private.txt"
        secret.write_text("Private payroll records")
        (corpus / "linked.txt").symlink_to(secret)
        report = build(corpus, tmp_path / "data")
        assert not report.published
        assert "outside the source" in report.failures["linked.txt"]

    def test_source_read_failure_keeps_previous_snapshot(
        self, corpus, tmp_path, monkeypatch
    ):
        data = tmp_path / "data"
        build(corpus, data)
        previous = current_snapshot(data).id
        original_read = Path.read_bytes

        def unreadable(path):
            if path.name == "notes.txt":
                raise PermissionError("read denied")
            return original_read(path)

        monkeypatch.setattr(Path, "read_bytes", unreadable)
        report = build(corpus, data)
        assert not report.published and "notes.txt" in report.failures
        assert current_snapshot(data).id == previous
        assert not list((data / "snapshots").glob(".building-*"))


class TestConceptIds:
    """Mapping source paths to concept IDs."""

    def test_same_stem_and_reserved_names(self):
        ids = assign_concept_ids(
            ["Budget.xlsx", "budget.pdf", "Team Docs/Index.md", "log.txt", "a/b.md"]
        )
        assert ids["Budget.xlsx"] == "budget-xlsx"
        assert ids["budget.pdf"] == "budget-pdf"
        assert ids["Team Docs/Index.md"] == "team-docs/index-doc"
        assert ids["log.txt"] == "log-doc"
        assert ids["a/b.md"] == "a/b"

    def test_remaining_collisions_get_counters(self):
        ids = assign_concept_ids(["a b.md", "a-b.md"])
        assert sorted(ids.values()) == ["a-b-md", "a-b-md-2"]


class TestExtractionAndRendering:
    """Native extraction and the section <-> body round trip."""

    def test_markdown_locations(self, tmp_path):
        path = tmp_path / "a.md"
        path.write_text("intro\n\n# T\nbody\n```\n# not\n```\n## S\nmore\n")
        doc = extract_native(path)
        assert [(s.title, s.location["lines"]) for s in doc.sections] == [
            ("", [1, 2]),
            ("T", [3, 7]),
            ("S", [8, 10]),
        ]
        assert doc.title == "T"

    def test_text_file_is_one_section(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_text("line one\nline two\n")
        doc = extract_native(path)
        assert len(doc.sections) == 1 and doc.sections[0].location == {"lines": [1, 3]}

    def test_text_delimiters_do_not_discard_content(self, tmp_path):
        path = tmp_path / "a.txt"
        text = "---\nCritical terms\n---\nRest of the agreement"
        path.write_text(text)
        assert extract_native(path).sections[0].markdown == text

    def test_markdown_frontmatter_requires_exact_closing_delimiter(self, tmp_path):
        path = tmp_path / "a.md"
        path.write_text("---\ntitle: A\n---not-a-delimiter\n---\n\n# A\nBody\n")
        doc = extract_native(path)
        assert len(doc.sections) == 1 and doc.sections[0].title == "A"
        assert doc.sections[0].location["lines"] == [6, 8]

    def test_unsupported(self, tmp_path):
        with pytest.raises(ValueError, match="unsupported"):
            extract(tmp_path / "x.exe")

    def test_extracted_headings_cannot_create_sections(self):
        doc = [ExtractedSection("Real", 1, "# injected\ntext", {"pages": [2]})]
        body, written = render_body(doc)
        sections = split_sections(body)
        assert [s.title for s in sections] == ["Real"] and len(written) == 1
        assert "\\# injected" in body

    def test_untitled_sections_merge_into_previous(self):
        doc = [
            ExtractedSection("", 0, "preamble", {}),
            ExtractedSection("A", 2, "a", {"pages": [1]}),
            ExtractedSection("", 0, "orphan", {"pages": [2]}),
            ExtractedSection("", 0, "", {}),
        ]
        body, written = render_body(doc)
        assert [s.title for s in written] == ["", "A"]
        assert (
            len(split_sections(body)) == 2 and "orphan" in split_sections(body)[1].text
        )

    def test_locations_survive_index_rebuild_from_extraction_json(
        self, tmp_path, monkeypatch, corpus_writer
    ):
        """Docling-style locations come from extraction metadata, not Markdown."""
        src = corpus_writer(tmp_path / "s", {"report.pdf": "%PDF-fake"})
        fake = ExtractedDoc(
            extractor="docling",
            title="Quarterly report",
            sections=[
                ExtractedSection("Quarterly report", 1, "Summary.", {"pages": [1]}),
                ExtractedSection(
                    "Revenue",
                    2,
                    "Revenue grew 4%.",
                    {"section": "Revenue", "pages": [3, 4]},
                ),
            ],
            raw={"schema_name": "DoclingDocument"},
        )
        monkeypatch.setattr(pipeline, "extract", lambda p, a=None: fake)
        data = tmp_path / "data"
        assert build(src, data).published
        snap = current_snapshot(data)
        record = json.loads(next(snap.extraction.glob("*.json")).read_text())
        assert record["docling"] == {"schema_name": "DoclingDocument"}
        assert record["sections"][1]["slug"] == "revenue"
        with SearchIndex(snap.index) as index:
            hit = index.search("revenue grew")[0]
        assert hit["source"]["location"]["pages"] == [3, 4]
        assert hit["source"]["location_text"] == "pages 3–4, section “Revenue”"
        # A rebuild reuses the stored extraction and keeps the citation.
        monkeypatch.setattr(
            pipeline, "extract", lambda p, a=None: pytest.fail("re-extracted")
        )
        assert build(src, data).reused == 1
        with SearchIndex(current_snapshot(data).index) as index:
            assert index.search("revenue grew")[0]["source"]["location"]["pages"] == [
                3,
                4,
            ]


class TestKnowledgeHelpers:
    """Bundle, index and cursor helpers."""

    def test_frontmatter_errors(self):
        for text in (
            "no frontmatter",
            "---\ntype: X\n",
            "---\n- a\n---\n",
            "---\n: [\n---\n",
        ):
            with pytest.raises(BundleError):
                split_frontmatter(text)
        assert split_frontmatter("\ufeff---\ntype: X\n---\nbody")[0] == {"type": "X"}

    def test_section_slugs_remain_unique_with_numbered_headings(self):
        sections = split_sections("intro\n# Preamble\na\n# A\nb\n# A\nc\n# A-2\nd\n")
        slugs = [s.slug for s in sections]
        assert len(slugs) == len(set(slugs))
        assert [s.text for s in sections] == ["intro", "a", "b", "c", "d"]

    def test_trust_tiers_and_bare_verified_mapping(self):
        assert trust_tier({}) == "unverified"
        assert (
            trust_tier({"verified": {"by": "process:nightly", "at": "x"}})
            == "machine-confirmed"
        )
        assert (
            trust_tier({"verified": [{"by": "process:a"}, {"by": "human:b"}]})
            == "human-reviewed"
        )

    def test_staleness(self):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        assert not is_stale({}, now)
        assert is_stale({"stale_after": "2026-06-01T00:00:00Z"}, now)
        assert not is_stale({"stale_after": "2026-06-02T00:00:00+00:00"}, now)
        assert is_stale({"stale_after": "garbage"}, now)

    def test_long_tables_repeat_their_header(self):
        rows = "\n".join(f"| r{i} | {'x' * 40} |" for i in range(100))
        passages = _split_long(f"| k | v |\n|---|---|\n{rows}", limit=500)
        assert len(passages) > 1
        assert all(p.startswith("| k | v |\n|---|---|") for p in passages)
        assert all(len(p) <= 500 for p in passages)

    def test_long_paragraph_is_cut(self):
        assert [len(p) for p in _split_long("x" * 1200, limit=500)] == [500, 500, 200]

    def test_fts_query_is_quoted(self):
        assert fts_query('SKU-4471 "AND" (x') == '"SKU 4471" OR "AND" OR "x"'
        assert fts_query("?? !!") == ""

    def test_format_location(self):
        assert format_location({"pages": [2]}) == "page 2"
        assert (
            format_location({"sheet": "Q1", "lines": [1, 4]}) == "sheet Q1, lines 1–4"
        )
        assert format_location({}) == ""

    def test_fts5_available(self):
        check_fts5()

    def test_fts5_missing_is_reported(self):
        class NoFts:
            def execute(self, sql):
                raise sqlite3.OperationalError("no such module: fts5")

        with pytest.raises(SearchIndexError, match="lacks FTS5"):
            check_fts5(NoFts())

    def test_missing_index(self, tmp_path):
        with pytest.raises(SearchIndexError):
            SearchIndex(tmp_path / "nope.db")

    def test_cursor_round_trip(self):
        cursor = encode_cursor("s1", "t", 10)
        assert decode_cursor(cursor, "s1", "t") == 10
        assert decode_cursor(None, "s1", "t") == 0
        with pytest.raises(CursorError, match="restart"):
            decode_cursor(cursor, "s2", "t")

    def test_invalid_current_link(self, tmp_path):
        with pytest.raises(SnapshotError):
            current_snapshot(tmp_path)
        (tmp_path / "elsewhere" / "bundle").mkdir(parents=True)
        (tmp_path / "current").symlink_to(tmp_path / "elsewhere")
        with pytest.raises(SnapshotError, match="invalid"):
            current_snapshot(tmp_path)


class TestValidate:
    """Conformance checks (spec §11)."""

    def test_reports_problems(self, tmp_path, corpus_writer):
        files = {
            "no-type.md": "---\ntitle: x\n---\n",
            "no-fm.md": "hello",
            "bad-gen.md": "---\ntype: X\ngenerated: {at: 2026-01-01T00:00:00}\n---\n",
            "bad-status.md": "---\ntype: X\nstatus: live\n---\n",
            "bad-src.md": "---\ntype: X\nsources: [{title: y}]\n---\n",
            "naive.md": "---\ntype: X\ngenerated: {by: a, at: '2026-01-01T00:00:00'}\n---\n",
            "sub/index.md": "---\nokf_version: '0.2'\n---\n",
            "log.md": "# Log\n\n## yesterday\n",
            "index.md": "---\nokf_version: '0.1'\n---\n",
        }
        problems = "\n".join(validate_bundle(corpus_writer(tmp_path, files)))
        for needle in (
            "no-type.md",
            "no-fm.md",
            "bad-gen.md",
            "bad-status.md",
            "bad-src.md",
            "naive.md",
            "sub/index.md",
            "log.md",
            "okf_version",
        ):
            assert needle in problems

    @pytest.mark.parametrize(
        "filename, text",
        [
            ("index.md", "---\nokf_version: [\n---\n"),
            ("a.md", "---\ntype: X\nsources: 123\n---\n"),
            (
                "a.md",
                "---\ntype: X\nverified: [{by: 'human:owner', at: yesterday}]\n---\n",
            ),
        ],
    )
    def test_malformed_metadata_reports_problems(self, tmp_path, filename, text):
        (tmp_path / filename).write_text(text)
        assert validate_bundle(tmp_path)


class TestEvaluateAndCli:
    """Evaluation and the okf-ingest command."""

    def test_eval_hit_at_k(self, published, tmp_path):
        questions = tmp_path / "q.yaml"
        questions.write_text(
            "questions:\n"
            "  - {question: hotel limit London, expected: [policies/travel]}\n"
            "  - {question: SKU-4471, expected: [guides/expenses, notes]}\n"
            "  - {question: parental leave, expected: []}\n"
        )
        result = evaluate(published, questions, k=5)
        assert result["answerable"] == 2 and result["unanswerable"] == 1
        assert result["hit_at_5"] == 1.0
        assert result["questions"][1]["all_found"] is False

    def test_cli_build_validate_eval(self, corpus, tmp_path, capsys):
        data = tmp_path / "data"
        assert cli_main(["--data-dir", str(data), "build", str(corpus)]) == 0
        assert "published" in capsys.readouterr().out
        assert cli_main(["--data-dir", str(data), "validate"]) == 0
        questions = tmp_path / "q.yaml"
        questions.write_text(
            "questions:\n  - {question: hotel, expected: [policies/travel]}\n"
        )
        assert cli_main(["--data-dir", str(data), "eval", str(questions)]) == 0
        assert '"hit_at_5": 1.0' in capsys.readouterr().out

    def test_cli_build_failure_exit_code(self, corpus, tmp_path, capsys):
        (corpus / "x.exe").write_bytes(b"\x00")
        assert cli_main(["--data-dir", str(tmp_path / "d"), "build", str(corpus)]) == 1
        assert "FAILED  x.exe" in capsys.readouterr().err

    def test_cli_validate_reports_problems(self, published, capsys):
        bad = current_snapshot(published).bundle / "bad.md"
        bad.write_text("no frontmatter")
        assert cli_main(["--data-dir", str(published), "validate"]) == 1
        assert "bad.md" in capsys.readouterr().out

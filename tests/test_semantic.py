"""Hybrid (keyword + semantic) search, with a deterministic fake embedder."""

import sqlite3

import numpy as np
import pytest

from okf_mcp_server.src.ingest.pipeline import build
from okf_mcp_server.src.knowledge import embed
from okf_mcp_server.src.knowledge.index import SearchIndex, load_vectors
from okf_mcp_server.src.knowledge.snapshot import current_snapshot
from okf_mcp_server.src.tools.search_knowledge_tool import search_knowledge

FILES = {
    "hr/sickness.md": "# Sickness Absence\n\nA fit note is required after 7 calendar days.\n",
    "finance/travel.md": "# Travel\n\n## Lodging\n\nThe lodging limit is 150 GBP.\n",
    "finance/old-travel.md": "# Old travel\n\n## Lodging\n\nThe lodging limit was 120 GBP.\n",
    "misc/other.md": "# Other\n\nNothing relevant here at all.\n",
    "_okf.yaml": "types: {hr/: Policy, finance/: Policy}\ndocuments:\n  finance/old-travel.md: {status: deprecated}\n",
}


@pytest.fixture(autouse=True)
def mock_imports():
    """Override conftest's sys.modules mocking: these tests run the real stack."""
    yield


@pytest.fixture
def hybrid(tmp_path, corpus_writer, fake_embedder, monkeypatch):
    # The settings object the tools hold (template tests reload the settings module).
    from okf_mcp_server.src.knowledge.service import settings

    data = tmp_path / "data"
    report = build(corpus_writer(tmp_path / "src", FILES), data)
    assert report.published
    monkeypatch.setattr(settings, "OKF_DATA_DIR", str(data))
    monkeypatch.setattr(settings, "ENABLE_AUTH", False)
    return data, report


def test_build_stores_one_vector_per_passage(hybrid, fake_embedder):
    data, report = hybrid
    assert report.embedding_model == "fake-embedder@1"
    assert report.embedded == report.passages and report.vectors_reused == 0
    db = sqlite3.connect(current_snapshot(data).index)
    assert db.execute("SELECT count(*) FROM vectors").fetchone()[0] == report.passages


def test_semantic_match_without_shared_words(hybrid, fake_embedder):
    data, _ = hybrid
    with SearchIndex(current_snapshot(data).index, embedder=fake_embedder) as index:
        assert index.mode == "hybrid"
        top = index.search("physician certificate")[0]
    assert top["concept_id"] == "hr/sickness" and top["matched_by"] == "semantic"
    with SearchIndex(current_snapshot(data).index) as index:  # keyword only
        assert index.mode == "keyword"
        assert all(
            r["concept_id"] != "hr/sickness"
            for r in index.search("physician certificate")
        )


def test_both_rankings_and_deprecated_last(hybrid, fake_embedder):
    data, _ = hybrid
    with SearchIndex(current_snapshot(data).index, embedder=fake_embedder) as index:
        results = index.search("lodging limit")
    ids = [r["concept_id"] for r in results]
    assert ids.index("finance/travel") < ids.index("finance/old-travel")
    assert results[0]["matched_by"] == "both"
    assert len({(r["concept_id"], r["section"]) for r in results}) == len(results)


def test_filters_apply_to_semantic_results(hybrid, fake_embedder):
    data, _ = hybrid
    with SearchIndex(current_snapshot(data).index, embedder=fake_embedder) as index:
        # Vector search always returns nearest neighbours, so filtering is the test:
        # the only Reference concept may appear, the Policy match may not.
        assert {
            r["type"] for r in index.search("physician certificate", type_="Reference")
        } <= {"Reference"}
        assert (
            index.search("physician certificate", type_="Policy")[0]["concept_id"]
            == "hr/sickness"
        )


def test_rebuild_reuses_vectors(hybrid, fake_embedder, tmp_path):
    data, _ = hybrid
    calls = fake_embedder.calls
    (tmp_path / "src" / "misc" / "other.md").write_text(
        "# Other\n\nA changed sentence.\n"
    )
    report = build(tmp_path / "src", data)
    assert report.embedded == 1 and report.vectors_reused == report.passages - 1
    assert fake_embedder.calls == calls + 1


def test_model_mismatch_falls_back_to_keyword(hybrid):
    data, _ = hybrid

    class OtherModel:
        model_id = "other@2"

        def encode(self, texts, kind):
            raise AssertionError("must not be called")

    with SearchIndex(current_snapshot(data).index, embedder=OtherModel()) as index:
        assert index.mode == "keyword" and index.search("lodging")
    assert load_vectors(current_snapshot(data).index, "other@2") == {}


def test_tool_reports_mode_and_match(hybrid):
    result = search_knowledge("physician certificate")
    assert result["search_mode"] == "hybrid"
    assert result["results"][0]["matched_by"] == "semantic"


def test_tool_falls_back_when_disabled(hybrid, monkeypatch):
    from okf_mcp_server.src.knowledge.service import settings

    monkeypatch.setattr(settings, "OKF_SEMANTIC_SEARCH", False)
    assert search_knowledge("lodging limit")["search_mode"] == "keyword"


def test_build_without_model_warns_and_stays_keyword(tmp_path, corpus_writer):
    report = build(corpus_writer(tmp_path / "src", FILES), tmp_path / "data")
    assert report.published and report.embedding_model == ""
    assert any("semantic index skipped" in w for w in report.warnings)


def test_no_embeddings_flag(tmp_path, corpus_writer, fake_embedder):
    report = build(
        corpus_writer(tmp_path / "src", FILES), tmp_path / "data", embeddings=False
    )
    assert report.embedding_model == "" and fake_embedder.calls == 0
    assert not any("semantic" in w for w in report.warnings)


def test_passage_text_and_hash():
    assert embed.passage_text("T", "S", "body") == "T | S\nbody"
    assert embed.text_hash("a") != embed.text_hash("b")


@pytest.mark.semantic
def test_real_model_if_installed():
    """Runs only where the pinned model is cached (`okf-ingest fetch-model`)."""
    try:
        model = embed.Embedder(device="cpu")
    except embed.EmbeddingUnavailable as e:
        pytest.skip(str(e))
    q = model.encode(["doctor's note for sick leave"], "query")[0]
    a, b = model.encode(
        [
            "A fit note is required after 7 days of sickness.",
            "The hotel limit is 150 GBP.",
        ],
        "passage",
    )
    assert q.shape == (embed.DIM,) and abs(float(np.linalg.norm(q)) - 1) < 1e-4
    assert float(q @ a) > float(q @ b)


def test_lazy_embedder_loads_nothing_until_encoding(monkeypatch):
    loads = []
    monkeypatch.setattr(embed, "get_embedder", lambda: loads.append(1) or FakeModel())

    class FakeModel:
        def encode(self, texts, kind):
            return np.ones((len(texts), 2), dtype=np.float32)

    lazy = embed.LazyEmbedder()
    assert lazy.model_id == embed.Embedder.model_id and loads == []
    assert lazy.encode(["x"], "query").shape == (1, 2) and loads == [1]


def test_unavailable_reason_names_the_fix(monkeypatch):
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert "semantic` extra" in embed.unavailable_reason()


def test_warm_up_loads_the_embedder_once(fake_embedder, monkeypatch):
    from okf_mcp_server.src.knowledge import service

    service.warm_up_search()
    assert fake_embedder.calls == 1
    monkeypatch.setattr(service.settings, "OKF_SEMANTIC_SEARCH", False)
    service.warm_up_search()  # disabled: no model use
    assert fake_embedder.calls == 1


class TestVectorBackends:
    """sqlite-vec for large corpora; filters apply before ranking on both."""

    def build(self, tmp_path, corpus_writer, backend, files=FILES):
        from okf_mcp_server.src.knowledge import index as index_module

        data = tmp_path / f"data-{backend}"
        src = corpus_writer(tmp_path / f"src-{backend}", files)
        orig = index_module.build_index

        def forced(*args, **kwargs):
            kwargs["vector_backend"] = backend
            return orig(*args, **kwargs)

        import okf_mcp_server.src.ingest.pipeline as pipeline_module

        pipeline_module_build = pipeline_module.build_index
        pipeline_module.build_index = forced
        try:
            assert build(src, data).published
        finally:
            pipeline_module.build_index = pipeline_module_build
        return current_snapshot(data).index

    def test_sqlite_vec_matches_numpy(self, tmp_path, corpus_writer, fake_embedder):
        pytest.importorskip("sqlite_vec")
        results = {}
        for backend in ("numpy", "sqlite-vec"):
            db = self.build(tmp_path, corpus_writer, backend)
            with SearchIndex(db, embedder=fake_embedder) as index:
                assert index._meta("vector_backend") == backend
                results[backend] = [
                    r["concept_id"]
                    for r in index.search("physician certificate lodging")
                ]
        assert results["numpy"] == results["sqlite-vec"]

    def test_auto_switches_at_the_limit(self, monkeypatch):
        pytest.importorskip("sqlite_vec")
        from okf_mcp_server.src.knowledge import index as index_module

        monkeypatch.setattr(index_module, "VECTOR_MEMORY_LIMIT", 10)
        assert index_module.choose_vector_backend(10) == "numpy"
        assert index_module.choose_vector_backend(11) == "sqlite-vec"
        with pytest.raises(ValueError):
            index_module.choose_vector_backend(1, "faiss")

    @pytest.mark.parametrize("backend", ["numpy", "sqlite-vec"])
    def test_small_allowed_set_is_found_despite_many_closer_passages(
        self, tmp_path, corpus_writer, fake_embedder, backend
    ):
        """The caller may see one far-away document; 60 closer ones are hidden."""
        pytest.importorskip("sqlite_vec")
        files = {
            f"hr/case-{i}.md": f"# Case {i}\n\nA doctor issued a fit note and sick certificate {i}.\n"
            for i in range(60)
        }
        files["public/visible.md"] = (
            "# Visible\n\nMedical questions go to the lodging desk.\n"
        )
        files["_okf.yaml"] = "access:\n  hr/: [group:hr]\n  public/: ['*']\n"
        db = self.build(tmp_path, corpus_writer, backend, files)
        from okf_mcp_server.src.knowledge.access import Identity

        principals = Identity(subject="eve").principals
        with SearchIndex(db, embedder=fake_embedder) as index:
            results = index.search(
                "physician certificate", limit=1, principals=principals
            )
        assert [r["concept_id"] for r in results] == ["public/visible"]

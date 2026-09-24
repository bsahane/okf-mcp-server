"""Tests for `okf-ingest suggest-config`."""

import pytest
import yaml

from okf_mcp_server.src.ingest.cli import main as cli_main
from okf_mcp_server.src.ingest.pipeline import build
from okf_mcp_server.src.ingest.suggest import suggest_config


@pytest.fixture(autouse=True)
def mock_imports():
    """Override conftest's sys.modules mocking: these tests run the real stack."""
    yield


FILES = {
    "HR/Policies/Leave Policy 2024.md": "# Leave\n\n22 days.\n",
    "HR/Policies/Leave Policy 2026.md": "# Leave\n\n25 days.\n",
    "HR/Policies/other/probation.md": "# Probation\n\n6 months.\n",
    "Finance/Travel Policy.md": "# Travel\n\nHotel 150.\n",
    "Finance/Travel Policy FINAL.md": "# Travel\n\nHotel 150.\n",
    "Finance/Travel Policy v2 DRAFT.md": "# Travel\n\nHotel 180.\n",
    "IT/Runbooks/erp.md": "# ERP\n\nRestart the app tier.\n",
    "IT/Access SOP.md": "# Access\n\nRaise a request.\n",
    "misc/readme.txt": "Plain notes.\n",
    "archive/old.zip": "PK",
}


def test_suggestions(tmp_path, corpus_writer):
    src = corpus_writer(tmp_path / "src", FILES)
    config = yaml.safe_load(suggest_config(src))
    assert config["types"] == {"HR/Policies/": "Policy", "IT/Runbooks/": "Runbook"}
    docs = config["documents"]
    assert docs["HR/Policies/Leave Policy 2024.md"]["status"] == "deprecated"
    assert docs["HR/Policies/Leave Policy 2026.md"]["status"] == "stable"
    assert docs["Finance/Travel Policy FINAL.md"]["status"] == "deprecated"
    assert "status" not in docs["Finance/Travel Policy.md"]
    assert docs["Finance/Travel Policy v2 DRAFT.md"]["status"] == "draft"
    assert docs["IT/Access SOP.md"]["type"] == "SOP"
    assert docs["Finance/Travel Policy.md"]["type"] == "Policy"
    # Inherited folder types are not repeated; unsupported files are ignored.
    assert "HR/Policies/other/probation.md" not in docs
    assert not any("zip" in rel for rel in docs)
    assert not any("verified" in doc for doc in docs.values())


def test_suggestion_builds_and_explains(tmp_path, corpus_writer):
    src = corpus_writer(tmp_path / "src", FILES)
    text = suggest_config(src)
    assert "# superseded by HR/Policies/Leave Policy 2026.md" in text
    assert "# same content as Finance/Travel Policy.md" in text
    (src / "_okf.yaml").write_text(text)
    report = build(src, tmp_path / "data")
    assert report.published and not any("same content" in w for w in report.warnings)


def test_cli_and_empty_folder(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    assert cli_main(["suggest-config", str(tmp_path / "empty")]) == 0
    config = yaml.safe_load(capsys.readouterr().out)
    assert config["types"] == {} and config["documents"] is None

"""Docling adapter tests; run by the ingestion CI job (`pytest -m docling`).

Skipped when the `ingest` extra is not installed. DOCX and XLSX conversion
needs no model downloads; PDF is exercised manually on real files.
"""

from types import SimpleNamespace

import pytest

pytest.importorskip("docling")

from okf_mcp_server.src.ingest.docling_adapter import extract_with_docling  # noqa: E402

pytestmark = pytest.mark.docling


@pytest.fixture(autouse=True)
def mock_imports():
    """Override conftest's sys.modules mocking: these tests run the real stack."""
    yield


def test_docx_sections_and_tables(tmp_path):
    from docx import Document

    path = tmp_path / "terms.docx"
    doc = Document()
    doc.add_heading("Warranty Terms", 0)
    doc.add_heading("Coverage", 1)
    table = doc.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "SKU", "Months"
    table.rows[1].cells[0].text, table.rows[1].cells[1].text = "SKU-4471", "36"
    doc.add_heading("Claims", 1)
    doc.add_paragraph("Raise claims within 14 days.")
    doc.save(path)

    result = extract_with_docling(path)
    titles = [s.title for s in result.sections]
    assert titles == ["Warranty Terms", "Coverage", "Claims"]
    assert "| SKU-4471 |" in result.sections[1].markdown.replace("  ", " ")
    assert result.sections[2].location["section"] == "Claims"
    assert result.raw and result.raw.get("schema_name") == "DoclingDocument"


def test_xlsx_sheets_become_sections(tmp_path):
    from openpyxl import Workbook

    path = tmp_path / "budget.xlsx"
    wb = Workbook()
    wb.active.title = "Cost centres"
    wb.active.append(["Code", "Owner"])
    wb.active.append(["CC-210", "Tom"])
    wb.create_sheet("Limits").append(["Role", "USD"])
    wb.save(path)

    result = extract_with_docling(path)
    assert [s.location.get("sheet") for s in result.sections] == [
        "Cost centres",
        "Limits",
    ]
    assert "pages" not in result.sections[0].location
    assert "CC-210" in result.sections[0].markdown


@pytest.mark.parametrize("status", ["partial_success", "failure"])
def test_incomplete_conversion_is_rejected(tmp_path, monkeypatch, status):
    from docling.datamodel.base_models import ConversionStatus

    from okf_mcp_server.src.ingest import docling_adapter

    result = SimpleNamespace(
        status=ConversionStatus(status),
        document=SimpleNamespace(
            iterate_items=lambda **kwargs: iter(()), export_to_dict=lambda: {}
        ),
    )
    monkeypatch.setattr(
        docling_adapter,
        "_converter",
        lambda *_: SimpleNamespace(convert=lambda path: result),
    )
    with pytest.raises(ValueError, match="conversion did not complete"):
        extract_with_docling(tmp_path / "broken.pdf")


def _text_pdf(path, text):
    """One-page PDF with a real text layer (Helvetica)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
    ]
    data, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(data))
        data += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(data)
    data += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    data += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n".encode()
    data += f"startxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(data)


def test_ocr_only_when_a_page_has_no_text_layer(tmp_path):
    from PIL import Image, ImageDraw

    from okf_mcp_server.src.ingest.docling_adapter import needs_ocr

    scan = tmp_path / "scan.pdf"
    img = Image.new("L", (600, 800), 255)
    ImageDraw.Draw(img).text((50, 50), "Scanned text", fill=0)
    img.save(scan, "PDF")
    assert needs_ocr(scan) is True

    text_pdf = tmp_path / "text.pdf"
    _text_pdf(text_pdf, "This page has an embedded text layer for extraction.")
    assert needs_ocr(text_pdf) is False

    assert needs_ocr(tmp_path / "notes.docx") is False
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 garbage")
    assert needs_ocr(tmp_path / "broken.pdf") is True

"""Thin Docling adapter: the only module that imports Docling.

Converts PDF, DOCX, PPTX, XLSX, HTML and CSV into sections with page or sheet
provenance, and returns Docling's JSON document so it can be kept as durable
extraction metadata. Processing is local. To run offline, pre-download the
models and set `DOCLING_ARTIFACTS_PATH` (or pass `--artifacts-path`).
"""

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import (
    DocItemLabel,
    GroupItem,
    ListItem,
    SectionHeaderItem,
    TableItem,
    TextItem,
    TitleItem,
)

from okf_mcp_server.src.ingest.extract import ExtractedDoc, ExtractedSection


@lru_cache(maxsize=4)
def _converter(artifacts_path: Optional[str]) -> DocumentConverter:
    options = PdfPipelineOptions(do_ocr=True, do_table_structure=True)
    if artifacts_path:
        options.artifacts_path = artifacts_path
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )


def extract_with_docling(
    path: Path, artifacts_path: Optional[Path] = None
) -> ExtractedDoc:
    """Convert one file with Docling into ordered, located sections."""
    result = _converter(str(artifacts_path) if artifacts_path else None).convert(path)
    if result.status != ConversionStatus.SUCCESS:
        raise ValueError(f"Docling conversion did not complete: {result.status.value}")
    doc = result.document

    sections: List[ExtractedSection] = []
    title: Optional[str] = None
    current = ExtractedSection("", 0, "", {})
    parts: List[str] = []
    pages: List[int] = []
    sheet: Optional[str] = None

    def flush() -> None:
        nonlocal current, parts, pages
        current.markdown = "\n\n".join(p for p in parts if p.strip())
        if pages:
            current.location["pages"] = sorted(set(pages))
        if current.title or current.markdown:
            sections.append(current)
        parts, pages = [], []

    def start(heading: str, level: int) -> None:
        nonlocal current
        flush()
        location: Dict[str, Any] = {} if heading == sheet else {"section": heading}
        if sheet:
            location["sheet"] = sheet
        current = ExtractedSection(heading, level, "", location)

    for item, level in doc.iterate_items(with_groups=True):
        if isinstance(item, GroupItem):
            if item.name.startswith("sheet: "):
                sheet = item.name[len("sheet: ") :]
                start(sheet, 1)
            continue
        if isinstance(item, (TitleItem, SectionHeaderItem)):
            heading_level = 1 if isinstance(item, TitleItem) else item.level + 1
            if isinstance(item, TitleItem) and title is None:
                title = item.text
            start(item.text.strip(), min(heading_level, 6))
        elif isinstance(item, TableItem):
            parts.append(item.export_to_markdown(doc=doc))
        elif isinstance(item, ListItem):
            parts.append(f"- {item.text}")
        elif isinstance(item, TextItem):
            if item.label in (DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER):
                continue
            parts.append(item.text)
        else:
            continue
        for prov in getattr(item, "prov", None) or []:
            # Spreadsheet "pages" are sheets, recorded separately above.
            if sheet is None and prov.page_no:
                pages.append(prov.page_no)
    flush()
    # Join consecutive list items into one block.
    for sec in sections:
        sec.markdown = sec.markdown.replace("\n\n- ", "\n- ")

    raw: Dict[str, Any] = doc.export_to_dict()
    return ExtractedDoc(extractor="docling", sections=sections, title=title, raw=raw)

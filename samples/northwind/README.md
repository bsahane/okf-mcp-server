# Northwind: synthetic benchmark corpus

A fictional company (HR, Finance, IT, Operations) with 64 files in the formats and states real folders have: DOCX, XLSX, PPTX, text PDFs, an image-only scan, HTML, CSV, Markdown and text. It deliberately includes superseded versions, an identical `FINAL` copy, an unapproved draft, same-name files in two formats, a file named `index.md`, a German document, deep folders, a 300-row budget, an empty file, a corrupt spreadsheet and unsupported files. All facts are invented.

```bash
python samples/northwind/generate.py /tmp/northwind-corpus   # refuses to overwrite non-Northwind folders
okf-ingest suggest-config /tmp/northwind-corpus > /tmp/northwind-corpus/_okf.yaml
okf-ingest --data-dir data/northwind build /tmp/northwind-corpus --allow-failures
okf-ingest --data-dir data/northwind eval samples/northwind/questions.yaml
```

`--allow-failures` is expected: the corrupt spreadsheet and the empty file are meant to fail. The 34 questions (32 answerable, 2 with no answer) were written by the corpus author, so scores are optimistic; use them to compare changes, not as a quality baseline. Reference results are recorded in [PLAN.md](../../PLAN.md) under *Implementation status*.

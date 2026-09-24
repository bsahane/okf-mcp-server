"""Build and publish an OKF snapshot from a folder of source documents.

One concept per source document. Frontmatter is filled deterministically
from file metadata, the ingestion run and the optional `_okf.yaml` config in
the source root; no generative model is involved.

A build writes a new snapshot beside the published one, validates it, builds
its index and only then switches `current`. Unchanged sources (same content
hash) reuse the previous snapshot's extraction instead of re-running Docling.
"""

import hashlib
import json
import secrets
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from okf_mcp_server.src.ingest.extract import (
    SUPPORTED_SUFFIXES,
    ExtractedDoc,
    ExtractedSection,
    extract,
)
from okf_mcp_server.src.knowledge.bundle import (
    FENCE,
    HEADING,
    OKF_VERSION,
    slugify,
    split_frontmatter,
    split_sections,
)
from okf_mcp_server.src.knowledge.index import build_index
from okf_mcp_server.src.knowledge.snapshot import (
    Snapshot,
    SnapshotError,
    current_snapshot,
    publish,
)
from okf_mcp_server.src.knowledge.validate import validate_bundle

CONFIG_FILE = "_okf.yaml"
STATUSES = ("draft", "stable", "deprecated")

try:
    PRODUCER = f"okf-ingest/{version('okf-mcp-server')}"
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    PRODUCER = "okf-ingest/0.1.0"


@dataclass
class BuildReport:
    """Outcome of one ingestion run."""

    snapshot_id: str
    published: bool = False
    concepts: int = 0
    passages: int = 0
    reused: int = 0
    failures: Dict[str, str] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    changes: List[Tuple[str, str]] = field(default_factory=list)


def iso(dt: datetime) -> str:
    """ISO 8601 with an explicit UTC `Z` suffix, second precision."""
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def load_config(source_root: Path) -> Dict[str, Any]:
    """Load `_okf.yaml` (optional).

    Raises:
        ValueError: If the config is malformed.
    """
    path = source_root / CONFIG_FILE
    if not path.is_file():
        return {}
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(config, dict):
        raise ValueError(f"{CONFIG_FILE} must be a mapping")
    for key in ("types", "documents"):
        if not isinstance(config.get(key, {}), dict):
            raise ValueError(f"{CONFIG_FILE}: `{key}` must be a mapping")
    for rel, doc in (config.get("documents") or {}).items():
        if not isinstance(doc, dict):
            raise ValueError(f"{CONFIG_FILE}: documents[{rel!r}] must be a mapping")
        if doc.get("status", "draft") not in STATUSES:
            raise ValueError(
                f"{CONFIG_FILE}: documents[{rel!r}].status must be one of {STATUSES}"
            )
        for event in doc.get("verified") or []:
            if (
                not isinstance(event, dict)
                or not event.get("by")
                or not event.get("at")
            ):
                raise ValueError(
                    f"{CONFIG_FILE}: documents[{rel!r}].verified needs `by` and `at`"
                )
    return config


def discover(source_root: Path) -> List[Path]:
    """Source files under the root, sorted; hidden files and the config are skipped."""
    files = []
    for path in sorted(source_root.rglob("*")):
        rel = path.relative_to(source_root)
        if not path.is_file() or any(p.startswith(".") for p in rel.parts):
            continue
        if rel.as_posix() == CONFIG_FILE:
            continue
        files.append(path)
    return files


def source_id_for(rel: str) -> str:
    """Stable source ID derived from the source-relative path."""
    return "src-" + hashlib.sha256(rel.encode()).hexdigest()[:16]


def assign_concept_ids(rels: List[str]) -> Dict[str, str]:
    """Map source-relative paths to unique concept IDs mirroring the folders.

    Same-stem files get the extension appended (`budget-xlsx`), reserved
    names `index`/`log` get a `-doc` suffix, and remaining clashes a counter.
    """

    def base(rel: str, with_suffix: bool) -> str:
        path = Path(rel)
        dirs = [slugify(p, "folder") for p in path.parent.parts]
        stem = slugify(path.stem, "document")
        if stem in ("index", "log"):
            stem += "-doc"
        if with_suffix:
            stem += "-" + slugify(path.suffix.lstrip("."), "file")
        return "/".join(dirs + [stem])

    plain: Dict[str, List[str]] = {}
    for rel in rels:
        plain.setdefault(base(rel, False), []).append(rel)
    ids: Dict[str, str] = {}
    used: set = set()
    for rel in sorted(rels):
        cid = base(rel, len(plain[base(rel, False)]) > 1)
        candidate, n = cid, 2
        while candidate in used:
            candidate, n = f"{cid}-{n}", n + 1
        used.add(candidate)
        ids[rel] = candidate
    return ids


def type_for(rel: str, config: Dict[str, Any]) -> str:
    """Concept type: per-document override, else longest matching path prefix, else default."""
    doc = (config.get("documents") or {}).get(rel) or {}
    if doc.get("type"):
        return str(doc["type"])
    matches = [p for p in (config.get("types") or {}) if rel.startswith(p)]
    if matches:
        return str(config["types"][max(matches, key=len)])
    return str(config.get("default_type") or "Reference")


def _escape_headings(markdown: str) -> str:
    """Escape heading-like lines so extracted text cannot create new sections."""
    out, in_fence = [], False
    for line in markdown.split("\n"):
        if FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and HEADING.match(line):
            line = "\\" + line
        out.append(line)
    return "\n".join(out)


def render_body(sections: List[ExtractedSection]) -> Tuple[str, List[ExtractedSection]]:
    """Render sections as Markdown; returns (body, sections actually written)."""
    written: List[ExtractedSection] = []
    chunks: List[str] = []
    for sec in sections:
        text = _escape_headings(sec.markdown.strip())
        if not sec.title.strip() and not text:
            continue
        if sec.title.strip():
            heading = (
                "#" * max(1, min(sec.level or 1, 6)) + " " + " ".join(sec.title.split())
            )
            chunks.append(f"{heading}\n\n{text}" if text else heading)
        else:
            if written:
                # An untitled section after the first would merge into its predecessor.
                chunks[-1] += f"\n\n{text}"
                written[-1] = ExtractedSection(
                    written[-1].title,
                    written[-1].level,
                    written[-1].markdown + "\n\n" + sec.markdown,
                    written[-1].location,
                )
                continue
            chunks.append(text)
        written.append(sec)
    return "\n\n".join(chunks).strip() + "\n", written


def render_concept(frontmatter: Dict[str, Any], body: str) -> str:
    """Serialize a concept document."""
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, width=1000)
    return f"---\n{fm}---\n\n{body}"


def _extraction_record(
    source_id: str,
    rel: str,
    uri: str,
    revision: str,
    doc: ExtractedDoc,
    slugs: List[str],
) -> Dict[str, Any]:
    return {
        "source_id": source_id,
        "path": rel,
        "uri": uri,
        "revision": revision,
        "extractor": doc.extractor,
        "title": doc.title,
        "sections": [
            {
                "slug": slug,
                "title": sec.title,
                "level": sec.level,
                "markdown": sec.markdown,
                "location": sec.location,
            }
            for slug, sec in zip(slugs, doc.sections)
        ],
        "docling": doc.raw,
    }


def _doc_from_record(record: Dict[str, Any]) -> ExtractedDoc:
    return ExtractedDoc(
        extractor=record["extractor"],
        title=record.get("title"),
        raw=record.get("docling"),
        sections=[
            ExtractedSection(
                s["title"], s["level"], s["markdown"], s.get("location") or {}
            )
            for s in record["sections"]
        ],
    )


def _write_indexes(bundle: Path, bundle_title: str) -> None:
    """Write an `index.md` in every directory (spec §8)."""
    dirs = sorted({bundle} | {p for p in bundle.rglob("*") if p.is_dir()})
    for directory in dirs:
        subdirs = sorted(p for p in directory.iterdir() if p.is_dir())
        concepts = sorted(
            p for p in directory.glob("*.md") if p.name not in ("index.md", "log.md")
        )
        lines: List[str] = []
        if directory == bundle:
            lines += ["---", f'okf_version: "{OKF_VERSION}"', "---", ""]
        if subdirs:
            lines.append("# Subdirectories\n")
            for sub in subdirs:
                count = sum(
                    1 for p in sub.rglob("*.md") if p.name not in ("index.md", "log.md")
                )
                lines.append(
                    f"* [{sub.name}]({sub.name}/index.md) - {count} concept(s)"
                )
            lines.append("")
        if concepts:
            heading = bundle_title if directory == bundle else directory.name
            lines.append(f"# {heading}\n")
            for path in concepts:
                fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
                summary = fm.get("description") or fm.get("type")
                status = fm.get("status")
                if status == "deprecated":
                    summary = f"{summary} (deprecated)"
                lines.append(
                    f"* [{fm.get('title') or path.stem}]({path.name}) - {summary}"
                )
            lines.append("")
        (directory / "index.md").write_text("\n".join(lines), encoding="utf-8")


def _write_log(
    bundle: Path, previous: Optional[Path], changes: List[Tuple[str, str]], today: str
) -> None:
    """Prepend today's changes to the carried-over `log.md` (spec §9)."""
    old = (
        previous.read_text(encoding="utf-8") if previous and previous.is_file() else ""
    )
    body = old.split("\n", 1)[1].lstrip("\n") if old.startswith("# ") else old
    if not changes:
        new = body
    else:
        entries = "\n".join(f"* **{kind}**: {text}" for kind, text in changes)
        if body.startswith(f"## {today}\n"):
            first, _, rest = body.partition("\n")
            new = (
                f"{first}\n{entries}\n{rest.lstrip(chr(10))}"
                if rest
                else f"{first}\n{entries}\n"
            )
        else:
            new = f"## {today}\n{entries}\n\n{body}"
    (bundle / "log.md").write_text(
        "# Knowledge Update Log\n\n" + new.rstrip("\n") + "\n", encoding="utf-8"
    )


def build(
    source_root: Path,
    data_dir: Path,
    artifacts_path: Optional[Path] = None,
    allow_failures: bool = False,
    now: Optional[datetime] = None,
) -> BuildReport:
    """Ingest `source_root` into a new snapshot and publish it if it validates."""
    now = now or datetime.now(timezone.utc)
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise ValueError(f"source directory not found: {source_root}")
    data_dir = data_dir.resolve()
    if data_dir.is_relative_to(source_root):
        raise ValueError("snapshot data directory must be outside the source directory")
    config = load_config(source_root)
    docs_config = config.get("documents") or {}

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "snapshots").mkdir(exist_ok=True)
    prev: Optional[Snapshot] = None
    prev_manifest: Dict[str, Dict[str, Any]] = {}
    try:
        prev = current_snapshot(data_dir)
        prev_manifest = json.loads(prev.manifest.read_text(encoding="utf-8"))["sources"]
    except (SnapshotError, FileNotFoundError, KeyError, json.JSONDecodeError):
        prev, prev_manifest = None, {}

    snapshot_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    report = BuildReport(snapshot_id=snapshot_id)
    work = data_dir / "snapshots" / f".building-{snapshot_id}"
    snap = Snapshot(id=snapshot_id, root=work)
    snap.bundle.mkdir(parents=True)
    snap.extraction.mkdir()

    files = discover(source_root)
    rels = [p.relative_to(source_root).as_posix() for p in files]
    for rel in docs_config:
        if rel not in rels:
            report.warnings.append(
                f"{CONFIG_FILE}: documents[{rel!r}] matches no source file"
            )
    concept_ids = assign_concept_ids(rels)
    vanished_by_revision = {
        entry["revision"]: (sid, entry)
        for sid, entry in prev_manifest.items()
        if entry["path"] not in rels
    }
    manifest: Dict[str, Dict[str, Any]] = {}
    renamed: set = set()

    for path, rel in zip(files, rels):
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            report.failures[rel] = f"unsupported file type {path.suffix or '(none)'}"
            continue
        sid = source_id_for(rel)
        try:
            if not path.resolve(strict=True).is_relative_to(source_root):
                raise ValueError("source file resolves outside the source directory")
            content = path.read_bytes()
            stat = path.stat()
        except (OSError, ValueError, RuntimeError) as e:
            report.failures[rel] = f"{type(e).__name__}: {e}"
            continue
        revision = "sha256:" + hashlib.sha256(content).hexdigest()
        uri = path.as_uri()
        prior = prev_manifest.get(sid)
        renamed_from = None
        if prior is None and revision in vanished_by_revision:
            renamed_from = vanished_by_revision.pop(revision)
            renamed.add(renamed_from[0])
            prior = renamed_from[1]
        doc: Optional[ExtractedDoc] = None
        if prev and prior and prior["revision"] == revision:
            old = prev.extraction / f"{renamed_from[0] if renamed_from else sid}.json"
            if old.is_file():
                doc = _doc_from_record(json.loads(old.read_text(encoding="utf-8")))
                report.reused += 1
        if doc is None:
            try:
                doc = extract(path, artifacts_path)
            except Exception as e:  # noqa: BLE001 - every extractor failure is reported per file
                report.failures[rel] = f"{type(e).__name__}: {e}"
                continue

        body, written = render_body(doc.sections)
        slugs = [s.slug for s in split_sections(body)]
        if len(slugs) != len(written):
            report.failures[rel] = "internal error: section mapping mismatch"
            continue
        doc.sections = written

        cfg = docs_config.get(rel) or {}
        unchanged = prior is not None and prior["revision"] == revision
        generated_at = iso(now)
        if prior is not None and unchanged and prior.get("generated_at"):
            generated_at = prior["generated_at"]
        fm: Dict[str, Any] = {
            "type": type_for(rel, config),
            "title": str(cfg.get("title") or doc.title or _title_from_filename(rel)),
        }
        if cfg.get("description"):
            fm["description"] = str(cfg["description"])
        fm["resource"] = uri
        if cfg.get("tags"):
            fm["tags"] = [str(t) for t in cfg["tags"]]
        fm["sources"] = [
            {
                "id": sid,
                "resource": uri,
                "title": Path(rel).name,
                "last_modified": iso(
                    datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                ),
            }
        ]
        fm["generated"] = {"by": PRODUCER, "at": generated_at}
        if cfg.get("verified"):
            fm["verified"] = [
                {"by": str(v["by"]), "at": _iso_value(v["at"])} for v in cfg["verified"]
            ]
        fm["status"] = str(cfg.get("status") or "draft")
        if cfg.get("stale_after"):
            fm["stale_after"] = _iso_value(cfg["stale_after"])
        fm["source_revision"] = revision
        fm["source_path"] = rel

        cid = concept_ids[rel]
        target = snap.bundle / f"{cid}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_concept(fm, body), encoding="utf-8")
        record = _extraction_record(sid, rel, uri, revision, doc, slugs)
        (snap.extraction / f"{sid}.json").write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )
        manifest[sid] = {
            "path": rel,
            "concept_id": cid,
            "revision": revision,
            "extractor": doc.extractor,
            "generated_at": generated_at,
        }
        link = f"[{fm['title']}](/{cid}.md)"
        if renamed_from:
            report.changes.append(
                ("Rename", f"{link} (was `{renamed_from[1]['path']}`)")
            )
        elif prior is None:
            report.changes.append(("Creation", f"Added {link}"))
        elif not unchanged:
            report.changes.append(("Update", f"Re-extracted {link}"))

    for sid, entry in prev_manifest.items():
        if entry["path"] not in rels and sid not in renamed:
            report.changes.append(("Deletion", f"Removed `{entry['path']}`"))

    report.concepts = len(manifest)
    _write_indexes(snap.bundle, str(config.get("bundle_title") or "Concepts"))
    _write_log(
        snap.bundle,
        prev.bundle / "log.md" if prev else None,
        report.changes,
        now.date().isoformat(),
    )
    (work / "manifest.json").write_text(
        json.dumps(
            {
                "snapshot_id": snapshot_id,
                "created_at": iso(now),
                "source_root": str(source_root),
                "sources": manifest,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if report.failures:
        (work / "failures.json").write_text(
            json.dumps(report.failures, indent=2), encoding="utf-8"
        )

    report.problems += validate_bundle(snap.bundle)
    if report.problems or (report.failures and not allow_failures):
        shutil.rmtree(work)
        return report

    report.passages = build_index(snap.bundle, snap.extraction, snap.index, snapshot_id)
    final = data_dir / "snapshots" / snapshot_id
    work.rename(final)
    publish(data_dir, Snapshot(id=snapshot_id, root=final))
    report.published = True
    return report


def _title_from_filename(rel: str) -> str:
    """`cost-centres.xlsx` -> `Cost centres`."""
    words = " ".join(Path(rel).stem.replace("_", " ").replace("-", " ").split())
    return words[:1].upper() + words[1:] if words else rel


def _iso_value(value: Any) -> str:
    """Normalize a config timestamp (YAML may load it as a datetime) to ISO 8601 with offset."""
    if isinstance(value, datetime):
        return iso(value if value.tzinfo else value.replace(tzinfo=timezone.utc))
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text  # left as-is so validation reports it
    return iso(parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc))

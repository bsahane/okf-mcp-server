"""SQLite FTS5 passage index built from an OKF snapshot.

Passages are cut from concept sections (never across them), with table
header rows repeated when a long table is split. Source locations come from
the snapshot's extraction metadata, not from the Markdown, so rebuilding the
index preserves citations.
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from okf_mcp_server.src.knowledge.bundle import (
    is_stale,
    iter_concepts,
    lifecycle_status,
    split_frontmatter,
    split_sections,
    to_jsonable,
    trust_tier,
)

MAX_PASSAGE_CHARS = 1500
MAX_LIMIT = 20
_TOKEN = re.compile(r"\w+", re.UNICODE)

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE passages (
    id INTEGER PRIMARY KEY,
    concept_id TEXT NOT NULL,
    section TEXT NOT NULL,
    section_title TEXT NOT NULL,
    text TEXT NOT NULL,
    title TEXT NOT NULL,
    type TEXT,
    tags TEXT NOT NULL,
    status TEXT NOT NULL,
    stale_after TEXT,
    verified TEXT NOT NULL,
    source_id TEXT,
    source_uri TEXT,
    source_revision TEXT,
    location TEXT NOT NULL
);
CREATE VIRTUAL TABLE passages_fts USING fts5(
    title, section_title, text,
    content='passages', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
"""


class SearchIndexError(RuntimeError):
    """Raised when the search index is missing, unusable or lacks FTS5."""


def check_fts5(conn: Optional[sqlite3.Connection] = None) -> None:
    """Verify this SQLite build can create an FTS5 table.

    Raises:
        SearchIndexError: If FTS5 is not compiled in.
    """
    own = conn is None
    conn = conn or sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.fts5_probe")
    except sqlite3.OperationalError as e:
        raise SearchIndexError(
            f"SQLite {sqlite3.sqlite_version} lacks FTS5: {e}"
        ) from e
    finally:
        if own:
            conn.close()


def _split_long(text: str, limit: int = MAX_PASSAGE_CHARS) -> List[str]:
    """Split section text into passages at blank-line blocks.

    A table block larger than the limit is split by rows, repeating its
    header and separator rows at the top of every piece.
    """
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    pieces: List[str] = []
    for block in blocks:
        lines = block.split("\n")
        is_table = (
            len(lines) > 2
            and lines[0].lstrip().startswith("|")
            and set(lines[1].replace("|", "").strip()) <= set("-: ")
        )
        if len(block) <= limit or not is_table:
            pieces.append(block)
            continue
        header, rows = lines[:2], lines[2:]
        chunk: List[str] = []
        for row in rows:
            if chunk and len("\n".join(header + chunk + [row])) > limit:
                pieces.append("\n".join(header + chunk))
                chunk = []
            chunk.append(row)
        if chunk:
            pieces.append("\n".join(header + chunk))

    passages: List[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) + 2 > limit:
            passages.append(current)
            current = ""
        if len(piece) > limit and not piece.lstrip().startswith("|"):
            # ponytail: hard cut of an oversized paragraph; split on sentences if recall suffers.
            passages.extend(piece[i : i + limit] for i in range(0, len(piece), limit))
            continue
        current = f"{current}\n\n{piece}" if current else piece
    if current:
        passages.append(current)
    return passages


def _load_locations(extraction_dir: Path, source_id: Optional[str]) -> Dict[str, Any]:
    """Return {section_slug: location} from the durable extraction metadata."""
    if not source_id:
        return {}
    path = extraction_dir / f"{source_id}.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {s["slug"]: s.get("location") or {} for s in data.get("sections", [])}


def build_index(
    bundle_root: Path, extraction_dir: Path, db_path: Path, snapshot_id: str
) -> int:
    """Build a fresh index file for one snapshot. Returns the passage count."""
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        check_fts5(conn)
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO meta VALUES ('snapshot_id', ?)", (snapshot_id,))
        count = 0
        for concept_id, path in iter_concepts(bundle_root):
            fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
            sources = fm.get("sources") or []
            source = sources[0] if sources and isinstance(sources[0], dict) else {}
            locations = _load_locations(extraction_dir, source.get("id"))
            for section in split_sections(body):
                for text in _split_long(section.text) or [""]:
                    if not text.strip() and not section.title:
                        continue
                    conn.execute(
                        "INSERT INTO passages (concept_id, section, section_title, text, title,"
                        " type, tags, status, stale_after, verified, source_id, source_uri,"
                        " source_revision, location) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            concept_id,
                            section.slug,
                            section.title,
                            text,
                            str(fm.get("title") or concept_id),
                            fm.get("type"),
                            json.dumps(to_jsonable(fm.get("tags") or [])),
                            lifecycle_status(fm),
                            json.dumps(to_jsonable(fm.get("stale_after"))),
                            json.dumps(to_jsonable(fm.get("verified"))),
                            source.get("id"),
                            source.get("resource"),
                            fm.get("source_revision"),
                            json.dumps(locations.get(section.slug, {})),
                        ),
                    )
                    count += 1
        conn.execute("INSERT INTO passages_fts(passages_fts) VALUES ('rebuild')")
        conn.commit()
        return count
    finally:
        conn.close()


def fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 query.

    Each whitespace-separated term becomes a quoted phrase of its word
    characters (so `SKU-4471` matches the adjacent tokens `sku 4471`), and
    terms are OR-ed and ranked by BM25. User punctuation and FTS operators
    are never passed through as query syntax.
    """
    phrases = []
    for term in query.split():
        tokens = _TOKEN.findall(term)
        if tokens:
            phrases.append('"' + " ".join(tokens) + '"')
    return " OR ".join(dict.fromkeys(phrases))


def format_location(location: Dict[str, Any]) -> str:
    """Human-readable location from extraction metadata; empty if unknown."""
    parts = []
    pages = location.get("pages") or []
    if pages:
        parts.append(
            f"page {pages[0]}" if len(pages) == 1 else f"pages {pages[0]}–{pages[-1]}"
        )
    if location.get("sheet"):
        parts.append(f"sheet {location['sheet']}")
    if location.get("section"):
        parts.append(f"section “{location['section']}”")
    if location.get("lines"):
        start, end = location["lines"]
        parts.append(f"lines {start}–{end}")
    return ", ".join(parts)


class SearchIndex:
    """Read-only access to one snapshot's index file."""

    def __init__(self, db_path: Path):
        """Open the index read-only; snapshots are immutable once published."""
        if not db_path.is_file():
            raise SearchIndexError(f"search index not found: {db_path.name}")
        self.conn = sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro&immutable=1", uri=True
        )
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        """Close the connection."""
        self.conn.close()

    def __enter__(self) -> "SearchIndex":
        """Context-manager entry."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Context-manager exit."""
        self.close()

    def snapshot_id(self) -> str:
        """Snapshot ID recorded when the index was built."""
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='snapshot_id'"
        ).fetchone()
        return row[0] if row else ""

    def search(
        self,
        query: str,
        type_: Optional[str] = None,
        tags: Sequence[str] = (),
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Return up to `limit` ranked passages with trust and lifecycle signals."""
        match = fts_query(query)
        if not match:
            return []
        sql = [
            "SELECT p.*, bm25(passages_fts, 5.0, 2.0, 1.0) AS score",
            "FROM passages_fts JOIN passages p ON p.id = passages_fts.rowid",
            "WHERE passages_fts MATCH ?",
        ]
        params: List[Any] = [match]
        if type_:
            sql.append("AND p.type = ?")
            params.append(type_)
        for tag in tags:
            sql.append("AND EXISTS (SELECT 1 FROM json_each(p.tags) WHERE value = ?)")
            params.append(tag)
        # Deprecated concepts stay retrievable (flagged) but rank after current ones.
        sql.append("ORDER BY p.status = 'deprecated', score LIMIT ?")
        params.append(max(1, min(int(limit), MAX_LIMIT)))
        results = []
        for row in self.conn.execute(" ".join(sql), params):
            fm = {
                "status": row["status"],
                "stale_after": json.loads(row["stale_after"]),
                "verified": json.loads(row["verified"]),
            }
            location = json.loads(row["location"])
            results.append(
                {
                    "concept_id": row["concept_id"],
                    "title": row["title"],
                    "type": row["type"],
                    "section": row["section"],
                    "section_title": row["section_title"],
                    "excerpt": row["text"][:MAX_PASSAGE_CHARS],
                    "excerpt_truncated": len(row["text"]) > MAX_PASSAGE_CHARS,
                    "status": row["status"],
                    "trust_tier": trust_tier(fm),
                    "stale": is_stale(fm),
                    "source": {
                        "id": row["source_id"],
                        "uri": row["source_uri"],
                        "revision": row["source_revision"],
                        "location": location,
                        "location_text": format_location(location),
                    },
                }
            )
        return results

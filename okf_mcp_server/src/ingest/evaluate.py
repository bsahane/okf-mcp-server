"""Retrieval evaluation: Hit@k over an agreed question set.

Question file (YAML)::

    questions:
      - question: What is the hotel limit in London?
        expected: [policies/travel-policy]   # concept IDs holding the evidence
      - question: Who approves exceptions to the parking rule?
        expected: []                          # no answer in the corpus

Hit@k is the fraction of answerable questions with at least one expected
concept in the top k results. For questions with several expected concepts,
`all_found` separately reports whether every one was retrieved.
"""

from pathlib import Path
from typing import Any, Dict

import yaml

from okf_mcp_server.src.knowledge.index import SearchIndex
from okf_mcp_server.src.knowledge.snapshot import current_snapshot


def evaluate(data_dir: Path, questions_file: Path, k: int = 5) -> Dict[str, Any]:
    """Run every question against the published snapshot's index."""
    spec = yaml.safe_load(questions_file.read_text(encoding="utf-8")) or {}
    snapshot = current_snapshot(data_dir)
    rows = []
    with SearchIndex(snapshot.index) as index:
        for q in spec.get("questions") or []:
            expected = [
                str(e).strip("/").removesuffix(".md") for e in q.get("expected") or []
            ]
            results = index.search(str(q["question"]), limit=k)
            found = [r["concept_id"] for r in results]
            rows.append(
                {
                    "question": q["question"],
                    "expected": expected,
                    "retrieved": list(dict.fromkeys(found)),
                    "answerable": bool(expected),
                    "hit": any(e in found for e in expected),
                    "all_found": all(e in found for e in expected)
                    if expected
                    else None,
                    "flags": list(
                        dict.fromkeys(
                            f"{r['concept_id']}: {r['status']}"
                            + (" (stale)" if r["stale"] else "")
                            for r in results
                            if r["status"] == "deprecated" or r["stale"]
                        )
                    ),
                }
            )
    answerable = [r for r in rows if r["answerable"]]
    hits = sum(r["hit"] for r in answerable)
    return {
        "snapshot_id": snapshot.id,
        "k": k,
        "answerable": len(answerable),
        "unanswerable": len(rows) - len(answerable),
        f"hit_at_{k}": hits / len(answerable) if answerable else None,
        "questions": rows,
    }

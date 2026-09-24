"""`okf-ingest`: operator command for building, validating and evaluating snapshots.

Runs separately from the MCP server; AI clients never trigger ingestion.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from okf_mcp_server.src.settings import settings


def _quiet_third_party() -> None:
    """Hide model-loading chatter so FAILED/SKIPPED/WARNING lines stay readable.

    Must run before Docling imports its models. RapidOCR resets its own logger
    to INFO on import, so INFO is disabled globally. Warnings from Python's
    `warnings` module (for example Pillow's DecompressionBombWarning) still show.
    """
    import logging
    import warnings

    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TQDM_DISABLE", "1")
    logging.disable(logging.INFO)
    warnings.filterwarnings("ignore", category=UserWarning, module=r"torch\..*")


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for the `okf-ingest` console script."""
    parser = argparse.ArgumentParser(prog="okf-ingest", description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(settings.OKF_DATA_DIR),
        help="snapshot directory (default: OKF_DATA_DIR)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser(
        "build", help="ingest a source folder and publish a new snapshot"
    )
    b.add_argument("source", type=Path, help="folder of source documents")
    b.add_argument(
        "--artifacts-path",
        type=Path,
        help="pre-downloaded Docling models (offline use)",
    )
    b.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show Docling/OCR/model logs and progress bars",
    )
    b.add_argument(
        "--keep",
        type=int,
        default=3,
        help="snapshots to keep after publishing, including the new one (min 2)",
    )
    b.add_argument(
        "--allow-failures",
        action="store_true",
        help="publish even if some files failed to extract (failures.json lists them)",
    )

    sub.add_parser("validate", help="check the published bundle against OKF v0.2 §11")

    s = sub.add_parser(
        "suggest-config", help="print a draft _okf.yaml for a source folder (review it)"
    )
    s.add_argument("source", type=Path, help="folder of source documents")

    e = sub.add_parser("eval", help="measure Hit@k against a question set")
    e.add_argument("questions", type=Path, help="YAML question file")
    e.add_argument("-k", type=int, default=5)

    args = parser.parse_args(argv)
    if args.command == "build":
        if not args.verbose:
            _quiet_third_party()
        from okf_mcp_server.src.ingest.pipeline import build

        try:
            report = build(
                args.source,
                args.data_dir,
                args.artifacts_path,
                args.allow_failures,
                keep=args.keep,
            )
        except ValueError as e:
            print(f"ERROR   {e}", file=sys.stderr)
            return 2
        for rel, error in sorted(report.failures.items()):
            print(f"FAILED  {rel}: {error}", file=sys.stderr)
        for rel, reason in sorted(report.skipped.items()):
            print(f"SKIPPED {rel}: {reason}", file=sys.stderr)
        for warning in report.warnings:
            print(f"WARNING {warning}", file=sys.stderr)
        for problem in report.problems:
            print(f"INVALID {problem}", file=sys.stderr)
        for kind, text in report.changes:
            print(f"{kind:<9} {text}")
        state = "published" if report.published else "NOT published"
        print(
            f"snapshot {report.snapshot_id} {state}: {report.concepts} concepts, "
            f"{report.passages} passages, {report.reused} extractions reused, "
            f"{len(report.failures)} failures, {len(report.skipped)} skipped"
        )
        if report.pruned:
            print(
                f"removed {len(report.pruned)} old snapshot(s): {', '.join(report.pruned)}"
            )
        return 0 if report.published and not report.failures else 1

    if args.command == "suggest-config":
        from okf_mcp_server.src.ingest.suggest import suggest_config

        print(suggest_config(args.source), end="")
        return 0

    from okf_mcp_server.src.knowledge.snapshot import current_snapshot

    if args.command == "validate":
        from okf_mcp_server.src.knowledge.validate import validate_bundle

        problems = validate_bundle(current_snapshot(args.data_dir).bundle)
        for problem in problems:
            print(problem)
        print("conformant" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0

    from okf_mcp_server.src.ingest.evaluate import evaluate

    result = evaluate(args.data_dir, args.questions, args.k)
    for row in result["questions"]:
        mark = "n/a " if not row["answerable"] else ("HIT " if row["hit"] else "MISS")
        extra = "" if row["all_found"] in (None, True) else "  (not all evidence found)"
        print(f"{mark} {row['question']}{extra}")
        for flag in row["flags"]:
            print(f"       flagged: {flag}")
    score = result[f"hit_at_{args.k}"]
    print(json.dumps({k: v for k, v in result.items() if k != "questions"}))
    return 0 if score is not None else 1


if __name__ == "__main__":
    sys.exit(main())

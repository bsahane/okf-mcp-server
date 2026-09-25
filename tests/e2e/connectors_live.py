"""Read-only check of connectors against a live SharePoint site or Google Drive folder.

Runs your connectors.yaml against the real source, but into a temporary mirror
and data dir (your configured mirror is not touched), and checks:

- the source lists and downloads without errors
- every item carries permissions from the source (or the source `access` default)
- a second sync downloads nothing (versions are stable, so refreshes are incremental)
- the mirror builds into a published, OKF-conformant snapshot
- optional `--expect path=principal`: that principal (or everyone, `*`) can read it

It prints who can read what, so you can compare it with the source's
"Manage access" / "Share" dialogs. Secrets come from the environment variables
named in connectors.yaml; nothing is written back to the source.

Usage:
  set -a; . ~/.config/okf/live.env; set +a     # your secrets, never committed
  python tests/e2e/connectors_live.py connectors.yaml [--source NAME] \\
      [--expect Policies/leave.docx=group:hr] [--keep]
"""

import argparse
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from auth_e2e import CHECKS, check  # noqa: E402

from okf_mcp_server.src.connectors import config as connectors  # noqa: E402
from okf_mcp_server.src.connectors.base import ACCESS_FILE, safe_relpath  # noqa: E402
from okf_mcp_server.src.ingest.pipeline import build  # noqa: E402
from okf_mcp_server.src.knowledge.access import normalize_access  # noqa: E402
from okf_mcp_server.src.knowledge.bundle import read_concept  # noqa: E402
from okf_mcp_server.src.knowledge.validate import validate_bundle  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path, help="your connectors.yaml")
    parser.add_argument("--source", action="append", help="only these sources")
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        help="SOURCE-RELATIVE-PATH=PRINCIPAL that must be allowed (repeatable)",
    )
    parser.add_argument("--keep", action="store_true", help="keep the temp folder")
    args = parser.parse_args()

    cfg = connectors.load(args.config)
    work = Path(tempfile.mkdtemp(prefix="okf-live-"))
    cfg["mirror"] = str(work / "mirror")
    names = [
        s["name"] for s in cfg["sources"] if not args.source or s["name"] in args.source
    ]
    try:
        first = connectors.run(cfg, only=names)
        for r in first:
            s = r.summary()
            check(
                f"{r.source}: lists and downloads without errors",
                not r.errors and bool(r.downloaded),
                json.dumps({**s, "errors": r.errors})[:800],
            )
            for rel, reason in sorted(r.skipped.items()):
                print(f"      skipped {rel}: {reason}")

        for name in names:
            acl_file = Path(cfg["mirror"]) / name / ACCESS_FILE
            acl = json.loads(acl_file.read_text()) if acl_file.exists() else {}
            files = [
                p
                for p in (Path(cfg["mirror"]) / name).rglob("*")
                if p.is_file() and ".okf" not in p.parts
            ]
            missing = [
                p.relative_to(Path(cfg["mirror"]) / name).as_posix()
                for p in files
                if not acl.get(p.relative_to(Path(cfg["mirror"]) / name).as_posix())
            ]
            check(
                f"{name}: every item has source permissions",
                bool(files) and not missing,
                f"{len(missing)} without: {missing[:5]}",
            )
            counts = Counter(p for principals in acl.values() for p in principals)
            print(f"      who can read what in {name} ({len(acl)} items):")
            for principal, n in counts.most_common(15):
                print(f"        {n:>5}  {principal}")

        second = connectors.run(cfg, only=names)
        for r in second:
            check(
                f"{r.source}: second sync is incremental (nothing re-downloaded)",
                not r.errors and not r.downloaded and not r.deleted,
                json.dumps(r.summary()),
            )

        report = build(
            Path(cfg["mirror"]), work / "data", allow_failures=True, embeddings=False
        )
        for rel, error in sorted(report.failures.items()):
            print(f"      extraction failed {rel}: {error}")
        check(
            "mirror builds into a published snapshot",
            report.published and report.concepts > 0,
            f"{report.concepts} concepts, {len(report.failures)} failures",
        )
        current = work / "data" / "current"
        problems = (
            validate_bundle(current / "bundle")
            if report.published
            else ["not published"]
        )
        check("bundle is OKF v0.2 conformant", not problems, "; ".join(problems[:5]))

        # What the server enforces: the published concept's `access`, after _okf.yaml rules.
        manifest = (
            json.loads((current / "manifest.json").read_text())["sources"].values()
            if report.published
            else []
        )
        concepts = {s["path"]: s["concept_id"] for s in manifest}
        for expectation in args.expect:
            path, _, principal = expectation.partition("=")
            source, _, rel = path.partition("/")
            if source not in names:  # path given relative to a single source
                source, rel = names[0], path
            mirrored = f"{source}/{safe_relpath(rel)}"
            concept_id = next(
                (
                    concepts[p]
                    for p in (
                        mirrored,
                        *(mirrored + x for x in (".docx", ".xlsx", ".pptx")),
                    )
                    if p in concepts
                ),
                None,
            )
            access = (
                read_concept(current / "bundle", concept_id)[0].get("access")
                if concept_id
                else None
            )
            check(
                f"{principal} can read {source}/{rel}",
                access is not None
                and bool(
                    (set(normalize_access([principal]) or []) | {"*"}) & set(access)
                ),
                f"concept {concept_id}: access {access}",
            )
    finally:
        if args.keep:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)

    failed = [name for name, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed or not CHECKS else 0


if __name__ == "__main__":
    sys.exit(main())

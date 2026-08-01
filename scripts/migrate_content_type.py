#!/usr/bin/env python3
"""Migrate legacy flat ${LARKSCOUT_DOCS_DIR}/<doc_id> ingestions into the
per-content_type layout introduced by PR #49.

Inference rules (in order):
  1. Explicit --mapping JSON override wins.
  2. doc_id starts with NBS (case-insensitive) -> Contract.
  3. Tags or metadata.document_profile contain a keyword (bid/contract/...) -> matched category.
  4. Fallback -> General.

Usage:
  scripts/migrate_content_type.py                       # dry-run on ${LARKSCOUT_DOCS_DIR}
  scripts/migrate_content_type.py --docs-dir /path      # dry-run on a custom dir
  scripts/migrate_content_type.py --mapping fix.json    # dry-run with overrides
  scripts/migrate_content_type.py --apply               # actually move + rewrite index

Stop the LarkScout server before running with --apply. The resolver tolerates a
half-finished run (it scans category subdirs and falls back to the flat path),
but doc-index.json is only rewritten at the end, so a crashed run will leave
the index slightly stale until you re-run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CONTENT_TYPES: tuple[str, ...] = ("General", "Contract", "Bid", "Knowledge")

# Tag / profile substring -> target content_type. Order matters: first hit wins.
_TAG_RULES: tuple[tuple[str, str], ...] = (
    ("bid", "Bid"),
    ("tender", "Bid"),
    ("投标", "Bid"),
    ("招标", "Bid"),
    ("contract", "Contract"),
    ("合同", "Contract"),
    ("knowledge", "Knowledge"),
    ("kb", "Knowledge"),
    ("wiki", "Knowledge"),
)


def infer_content_type(entry: dict[str, Any], overrides: dict[str, str]) -> str:
    doc_id = str(entry.get("id") or "")
    if doc_id in overrides:
        return overrides[doc_id]
    if doc_id.upper().startswith("NBS"):
        return "Contract"
    haystack_parts: list[str] = [str(t) for t in (entry.get("tags") or [])]
    metadata = entry.get("metadata") or {}
    profile = metadata.get("document_profile") if isinstance(metadata, dict) else None
    if profile:
        haystack_parts.append(str(profile))
    haystack = " ".join(haystack_parts).lower()
    for needle, target in _TAG_RULES:
        if needle in haystack:
            return target
    return "General"


def needs_migration(entry: dict[str, Any]) -> bool:
    storage_path = str(entry.get("storage_path") or "").strip().strip("/")
    if not storage_path:
        return True
    # A legacy entry's storage_path is just the doc_id (no slash).
    return "/" not in storage_path


def update_sidecar_json(path: Path, content_type: str, storage_path: str) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("content_type") == content_type and data.get("storage_path") == storage_path:
        return False
    data["content_type"] = content_type
    data["storage_path"] = storage_path
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return True


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def plan_migrations(
    docs_dir: Path,
    entries: list[dict[str, Any]],
    overrides: dict[str, str],
) -> tuple[list[dict[str, Any]], int]:
    plans: list[dict[str, Any]] = []
    already = 0
    for entry in entries:
        if not needs_migration(entry):
            already += 1
            continue
        doc_id = str(entry["id"])
        target = infer_content_type(entry, overrides)
        src = docs_dir / doc_id
        dst = docs_dir / target / doc_id
        plans.append(
            {
                "entry": entry,
                "doc_id": doc_id,
                "target": target,
                "src": src,
                "dst": dst,
                "src_exists": src.is_dir(),
                "dst_exists": dst.exists(),
                "storage_path": f"{target}/{doc_id}",
                "digest_path": f"docs/{target}/{doc_id}/digest.md",
            }
        )
    return plans, already


def apply_migrations(plans: list[dict[str, Any]], docs_dir: Path) -> tuple[int, int]:
    moved = 0
    skipped = 0
    for plan in plans:
        if not plan["src_exists"]:
            skipped += 1
            continue
        if plan["dst_exists"]:
            skipped += 1
            continue
        (docs_dir / plan["target"]).mkdir(parents=True, exist_ok=True)
        shutil.move(str(plan["src"]), str(plan["dst"]))
        entry = plan["entry"]
        entry["content_type"] = plan["target"]
        entry["storage_path"] = plan["storage_path"]
        entry["digest_path"] = plan["digest_path"]
        update_sidecar_json(plan["dst"] / "manifest.json", plan["target"], plan["storage_path"])
        update_sidecar_json(plan["dst"] / ".meta.json", plan["target"], plan["storage_path"])
        moved += 1
    return moved, skipped


def print_plan(plans: list[dict[str, Any]], already: int, total: int) -> None:
    print(f"Docs in index: {total}")
    print(f"Already categorized: {already}")
    print(f"Migrations planned: {len(plans)}")
    by_type: dict[str, int] = {}
    issues: list[str] = []
    for plan in plans:
        by_type[plan["target"]] = by_type.get(plan["target"], 0) + 1
        if not plan["src_exists"]:
            issues.append(f"  - {plan['doc_id']}: source missing at {plan['src']}")
        elif plan["dst_exists"]:
            issues.append(
                f"  - {plan['doc_id']}: destination already exists at {plan['dst']}"
            )
    if by_type:
        print()
        for ct in CONTENT_TYPES:
            if ct in by_type:
                print(f"  -> {ct}: {by_type[ct]}")
    if issues:
        print()
        print(f"WARNING: {len(issues)} doc(s) will be skipped:")
        for line in issues:
            print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--docs-dir",
        default=os.environ.get("LARKSCOUT_DOCS_DIR")
        or str(Path.home() / ".larkscout" / "docs"),
        help="LarkScout docs directory (defaults to $LARKSCOUT_DOCS_DIR or ~/.larkscout/docs)",
    )
    parser.add_argument(
        "--mapping",
        help="Optional JSON file of {doc_id: content_type} overrides",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the migration (default is dry-run)",
    )
    args = parser.parse_args()

    docs_dir = Path(args.docs_dir).expanduser().resolve()
    index_path = docs_dir / "doc-index.json"
    if not index_path.exists():
        print(f"error: doc-index.json not found at {index_path}", file=sys.stderr)
        return 2

    overrides: dict[str, str] = {}
    if args.mapping:
        mapping_raw = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
        if not isinstance(mapping_raw, dict):
            print("error: --mapping must be a JSON object", file=sys.stderr)
            return 2
        for doc_id, value in mapping_raw.items():
            if value not in CONTENT_TYPES:
                print(
                    f"error: mapping for {doc_id!r}: {value!r} is not one of {CONTENT_TYPES}",
                    file=sys.stderr,
                )
                return 2
            overrides[str(doc_id)] = value

    index = json.loads(index_path.read_text(encoding="utf-8"))
    entries = index.get("documents") or []
    plans, already = plan_migrations(docs_dir, entries, overrides)
    print_plan(plans, already, len(entries))

    if not args.apply:
        print()
        print("Dry-run only. Re-run with --apply to perform the migration.")
        return 0

    print()
    print("Applying...")
    moved, skipped = apply_migrations(plans, docs_dir)
    index["last_updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_json_atomic(index_path, index)
    print()
    print(f"Done. Moved {moved} document(s); skipped {skipped}.")
    print(f"Updated index: {index_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Tests for scripts/migrate_content_type.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "migrate_content_type.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("migrate_content_type", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def _make_doc(docs_dir: Path, doc_id: str, *, manifest_extra: dict | None = None) -> None:
    doc_dir = docs_dir / doc_id
    doc_dir.mkdir(parents=True)
    manifest = {"doc_id": doc_id, "filename": f"{doc_id}.pdf"}
    if manifest_extra:
        manifest.update(manifest_extra)
    (doc_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (doc_dir / ".meta.json").write_text(json.dumps({"doc_id": doc_id}), encoding="utf-8")
    (doc_dir / "digest.md").write_text("digest", encoding="utf-8")


def _write_index(docs_dir: Path, entries: list[dict]) -> Path:
    index_path = docs_dir / "doc-index.json"
    index_path.write_text(
        json.dumps({"version": "2", "documents": entries}, ensure_ascii=False),
        encoding="utf-8",
    )
    return index_path


def test_infer_nbs_prefix_to_contract(mod):
    assert mod.infer_content_type({"id": "NBS260336"}, {}) == "Contract"
    assert mod.infer_content_type({"id": "nbs250321"}, {}) == "Contract"


def test_infer_tag_rules(mod):
    assert mod.infer_content_type({"id": "DOC-001", "tags": ["bid", "Q3"]}, {}) == "Bid"
    assert mod.infer_content_type({"id": "DOC-002", "tags": ["招标"]}, {}) == "Bid"
    assert mod.infer_content_type({"id": "DOC-003", "tags": ["合同"]}, {}) == "Contract"
    assert (
        mod.infer_content_type(
            {"id": "DOC-004", "tags": [], "metadata": {"document_profile": "wiki"}},
            {},
        )
        == "Knowledge"
    )


def test_infer_falls_back_to_general(mod):
    assert mod.infer_content_type({"id": "DOC-099", "tags": ["misc"]}, {}) == "General"


def test_mapping_override_beats_rules(mod):
    # NBS prefix would normally route to Contract; override wins.
    assert (
        mod.infer_content_type({"id": "NBS260336"}, {"NBS260336": "Bid"}) == "Bid"
    )


def test_needs_migration(mod):
    assert mod.needs_migration({"id": "DOC-001"}) is True
    assert mod.needs_migration({"id": "DOC-001", "storage_path": ""}) is True
    assert mod.needs_migration({"id": "DOC-001", "storage_path": "DOC-001"}) is True
    assert (
        mod.needs_migration({"id": "DOC-001", "storage_path": "General/DOC-001"})
        is False
    )


def test_dry_run_makes_no_changes(tmp_path, mod, capsys):
    _make_doc(tmp_path, "NBS260336")
    _make_doc(tmp_path, "DOC-050")
    index_path = _write_index(
        tmp_path,
        [
            {"id": "NBS260336", "filename": "nbs.pdf", "tags": []},
            {"id": "DOC-050", "filename": "kb.pdf", "tags": ["knowledge"]},
        ],
    )

    rc = mod.main.__wrapped__() if hasattr(mod.main, "__wrapped__") else None
    # Use argv to invoke without --apply
    argv_backup = sys.argv[:]
    sys.argv = ["migrate_content_type.py", "--docs-dir", str(tmp_path)]
    try:
        rc = mod.main()
    finally:
        sys.argv = argv_backup
    assert rc == 0

    # Files unmoved, index untouched
    assert (tmp_path / "NBS260336").is_dir()
    assert (tmp_path / "DOC-050").is_dir()
    assert not (tmp_path / "Contract").exists()
    assert not (tmp_path / "Knowledge").exists()
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert "storage_path" not in index["documents"][0]

    out = capsys.readouterr().out
    assert "Migrations planned: 2" in out
    assert "-> Contract: 1" in out
    assert "-> Knowledge: 1" in out
    assert "Dry-run only" in out


def test_apply_moves_files_and_updates_index_and_manifest(tmp_path, mod):
    _make_doc(tmp_path, "NBS260336")
    _make_doc(tmp_path, "DOC-070", manifest_extra={"content_type": "stale"})
    index_path = _write_index(
        tmp_path,
        [
            {"id": "NBS260336", "filename": "nbs.pdf", "tags": [], "digest_path": "docs/NBS260336/digest.md"},
            {"id": "DOC-070", "filename": "bid.pdf", "tags": ["bid"], "digest_path": "docs/DOC-070/digest.md"},
        ],
    )

    argv_backup = sys.argv[:]
    sys.argv = ["migrate_content_type.py", "--docs-dir", str(tmp_path), "--apply"]
    try:
        rc = mod.main()
    finally:
        sys.argv = argv_backup
    assert rc == 0

    # Files moved
    assert not (tmp_path / "NBS260336").exists()
    assert (tmp_path / "Contract" / "NBS260336").is_dir()
    assert (tmp_path / "Bid" / "DOC-070").is_dir()

    # Index entries updated
    index = json.loads(index_path.read_text(encoding="utf-8"))
    by_id = {e["id"]: e for e in index["documents"]}
    assert by_id["NBS260336"]["content_type"] == "Contract"
    assert by_id["NBS260336"]["storage_path"] == "Contract/NBS260336"
    assert by_id["NBS260336"]["digest_path"] == "docs/Contract/NBS260336/digest.md"
    assert by_id["DOC-070"]["content_type"] == "Bid"
    assert by_id["DOC-070"]["storage_path"] == "Bid/DOC-070"

    # manifest.json updated in-place (also overwriting the stale value)
    moved_manifest = json.loads(
        (tmp_path / "Bid" / "DOC-070" / "manifest.json").read_text(encoding="utf-8")
    )
    assert moved_manifest["content_type"] == "Bid"
    assert moved_manifest["storage_path"] == "Bid/DOC-070"

    # .meta.json also updated
    moved_meta = json.loads(
        (tmp_path / "Contract" / "NBS260336" / ".meta.json").read_text(encoding="utf-8")
    )
    assert moved_meta["content_type"] == "Contract"


def test_apply_skips_collision(tmp_path, mod, capsys):
    _make_doc(tmp_path, "NBS260336")
    # Pre-create the collision target so the script must refuse to move.
    (tmp_path / "Contract" / "NBS260336").mkdir(parents=True)
    _write_index(tmp_path, [{"id": "NBS260336", "filename": "nbs.pdf", "tags": []}])

    argv_backup = sys.argv[:]
    sys.argv = ["migrate_content_type.py", "--docs-dir", str(tmp_path), "--apply"]
    try:
        rc = mod.main()
    finally:
        sys.argv = argv_backup
    assert rc == 0

    # Source still present, collision target untouched
    assert (tmp_path / "NBS260336").is_dir()
    out = capsys.readouterr().out
    assert "destination already exists" in out
    assert "Moved 0 document(s); skipped 1" in out


def test_mapping_file_overrides_inference(tmp_path, mod):
    _make_doc(tmp_path, "NBS260336")
    _write_index(tmp_path, [{"id": "NBS260336", "filename": "nbs.pdf", "tags": []}])
    mapping = tmp_path / "overrides.json"
    mapping.write_text(json.dumps({"NBS260336": "Bid"}), encoding="utf-8")

    argv_backup = sys.argv[:]
    sys.argv = [
        "migrate_content_type.py",
        "--docs-dir",
        str(tmp_path),
        "--mapping",
        str(mapping),
        "--apply",
    ]
    try:
        rc = mod.main()
    finally:
        sys.argv = argv_backup
    assert rc == 0
    assert (tmp_path / "Bid" / "NBS260336").is_dir()
    assert not (tmp_path / "Contract").exists()


def test_invalid_mapping_value_exits_nonzero(tmp_path, mod, capsys):
    _write_index(tmp_path, [])
    mapping = tmp_path / "overrides.json"
    mapping.write_text(json.dumps({"DOC-001": "Random"}), encoding="utf-8")

    argv_backup = sys.argv[:]
    sys.argv = [
        "migrate_content_type.py",
        "--docs-dir",
        str(tmp_path),
        "--mapping",
        str(mapping),
    ]
    try:
        rc = mod.main()
    finally:
        sys.argv = argv_backup
    assert rc == 2
    err = capsys.readouterr().err
    assert "not one of" in err

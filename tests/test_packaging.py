"""The built wheel must be a usable install, not just a file that exists (#205).

Everything the services import by name has to be in it, and so do the files they
open at runtime — a wheel that installs cleanly and then raises
ModuleNotFoundError on first import is worse than no wheel at all.

These read the built metadata rather than installing, so they stay fast; the
end-to-end check (build, install into an empty target, import from outside the
repo) is in the PR that introduced them.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
SETUPTOOLS = PYPROJECT["tool"]["setuptools"]


def test_top_level_modules_are_packaged() -> None:
    """Both service packages do `import i18n`; an install without it cannot start."""
    declared = set(SETUPTOOLS["py-modules"])
    on_disk = {p.stem for p in ROOT.glob("*.py")}
    assert on_disk <= declared, f"top-level modules missing from the wheel: {on_disk - declared}"


def test_service_packages_are_findable_where_they_live() -> None:
    """They are imported as top-level names but live under services/<name>/."""
    roots = set(SETUPTOOLS["packages"]["find"]["where"])
    for pkg in ("mantisfetch_browser", "mantisfetch_docreader", "mantisfetch_mcp"):
        parent = next(p for p in ROOT.glob(f"services/*/{pkg}") if p.is_dir()).parent
        rel = parent.relative_to(ROOT).as_posix()
        assert rel in roots, f"{pkg} lives in {rel}, which is not a packaging root"


def test_runtime_assets_are_declared_as_package_data() -> None:
    """Files opened at runtime, which a wheel drops unless they are declared."""
    data = SETUPTOOLS["package-data"]
    assert "readability.js" in data["mantisfetch_browser"]

    docreader = data["mantisfetch_docreader"]
    assert "paddle_ocr_worker.py" in docreader, "the OCR worker is launched by path"
    assert any("config_profiles" in pattern for pattern in docreader)


def test_shipped_profiles_live_inside_the_package() -> None:
    """A repo-root path resolves to nothing once the package is installed."""
    from mantisfetch_docreader.profiles import (
        DOCUMENT_PROFILE_CONFIG_DIR,
        FIELD_OCR_CONFIG_DIR,
    )

    pkg = ROOT / "services" / "docreader" / "mantisfetch_docreader"
    for d in (DOCUMENT_PROFILE_CONFIG_DIR, FIELD_OCR_CONFIG_DIR):
        assert d.is_relative_to(pkg), f"{d} is outside the package"
        assert list(d.glob("*.json")), f"no profiles shipped in {d}"


def test_a_repo_configs_profile_still_wins(tmp_path, monkeypatch) -> None:
    """The old location keeps working as an override.

    configs/document_profiles/ was the only place profiles lived before they
    moved into the package, so a deployment that dropped a custom one there must
    still find it — and it must take precedence over the shipped copy.
    """
    import mantisfetch_docreader.profiles as profiles

    override_dir = tmp_path / "document_profiles"
    override_dir.mkdir()
    (override_dir / "contract_cn.json").write_text('{"upgrade_policy": {}}', encoding="utf-8")
    monkeypatch.setattr(profiles, "_REPO_PROFILE_DIRS", (override_dir,))

    resolved = profiles._resolve_profile_config_path("contract_cn")
    assert resolved == override_dir / "contract_cn.json"


def test_the_ocr_worker_is_beside_the_package_that_launches_it() -> None:
    from mantisfetch_docreader.ocr.engines import _local_ocr_worker_command

    worker = Path(_local_ocr_worker_command()[-1])
    assert worker.exists(), f"the worker script is not where engines.py looks: {worker}"
    assert worker.parent.name == "mantisfetch_docreader"

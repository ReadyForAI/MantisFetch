"""constraints.txt must pin everything the requirements files ask for.

The image and CI install with ``-c constraints.txt`` so that one tag always gets
the same dependencies. A requirement added or narrowed without regenerating the
file (scripts/refresh_constraints.sh) would either install unpinned or fail to
resolve at build time; this makes it a red PR instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
# The arm64 OCR file is left out on purpose: it is a dev-only stack that the
# x86_64 pins contradict (numpy<2, paddlepaddle 2.6.2), and it installs unconstrained.
REQUIREMENT_FILES = ("requirements.txt", "requirements-ocr-linux-x86_64.txt")


def _requirements(name: str) -> list[Requirement]:
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    return [Requirement(line.split("#")[0]) for line in lines if line.split("#")[0].strip()]


def _pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in (ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            name, sep, version = line.partition("==")
            assert sep, f"constraints.txt line is not an exact pin: {line!r}"
            pins[canonicalize_name(name)] = version.strip()
    return pins


@pytest.mark.parametrize(
    "req",
    [r for name in REQUIREMENT_FILES for r in _requirements(name)],
    ids=lambda r: str(r),
)
def test_each_requirement_is_pinned_within_its_range(req: Requirement) -> None:
    pins = _pins()
    name = canonicalize_name(req.name)
    assert name in pins, f"{req.name} is not in constraints.txt; run scripts/refresh_constraints.sh"
    assert req.specifier.contains(pins[name], prereleases=True), (
        f"constraints.txt pins {req.name}=={pins[name]}, outside {req.specifier}; "
        "run scripts/refresh_constraints.sh"
    )

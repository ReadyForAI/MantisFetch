"""Every tunable the docs offer is one a compose deployment can actually set.

`.env.example` is the reference operators read, and compose is how MantisFetch
is deployed. A key documented in one and absent from the other is worse than an
undocumented key: the operator sets it, nothing happens, and the only way to
find out is to read the container's environment. Six keys were in that state —
including the raw-channel ceilings and both browser gates, which are the ones a
deployment is most likely to want to change.

`[compose: no]` in `.env.example` marks a key that is deliberately host-side or
build-time. Everything else has to appear in `docker-compose.yml`.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _documented_keys() -> list[tuple[str, bool]]:
    keys = []
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#?\s*(MANTISFETCH_[A-Z0-9_]+)=", line.strip())
        if match:
            keys.append((match.group(1), "[compose: no]" in line))
    return keys


def test_every_documented_tunable_reaches_a_compose_deployment() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    documented = _documented_keys()
    assert len(documented) > 40, "the .env.example parser stopped matching"

    missing = [key for key, host_side in documented if not host_side and key not in compose]
    assert not missing, (
        "documented in .env.example but not passed through docker-compose.yml, "
        f"so setting them in .env does nothing: {missing}"
    )


def test_the_marker_tells_the_truth_in_both_directions() -> None:
    """`[compose: no]` is a claim about the file next to it, and it was wrong on
    four keys — including one that had been in compose all along. A marker that
    can drift is worse than none: an operator reads it and does not try."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    lying = [key for key, marked in _documented_keys() if marked and key in compose]

    assert not lying, (
        "marked [compose: no] in .env.example but present in docker-compose.yml, "
        f"so the marker tells operators not to bother with a key that works: {lying}"
    )


def test_the_marker_is_not_a_way_to_avoid_the_check_above() -> None:
    """It means "host-side or build-time", not "not plumbed yet". Keep the list
    small enough that adding to it is a visible decision."""
    host_side = [key for key, marked in _documented_keys() if marked]
    assert len(host_side) <= 10, host_side

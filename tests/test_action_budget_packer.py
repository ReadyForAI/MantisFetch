"""A6: _apply_total_output_budget must not duplicate actions or emit a truncated
CSS selector (a 40-char prefix matches the wrong node)."""

from mantisfetch_browser.ranking import _apply_total_output_budget, _estimate_meta_chars


def _action(i: int, selector: str, confidence: float) -> dict:
    return {
        "aid": f"aid{i}",
        "role": "button",
        "name": "",
        "strategy": {"type": "css", "selector": selector},
        "actions": ["click"],
        "confidence": confidence,
        "source": "dom",
    }


def test_budget_packer_no_duplicates_and_no_truncated_selector():
    # a0 is far too big to fit; a1..a5 are small. With min_actions_to_keep=2 the
    # old packer (a) squeezed a 40-char-truncated a0 in, and (b) re-appended an
    # already-packed action in the second round. Neither may happen now.
    meta: dict = {}
    selectors = ["#" + "z" * 250] + [f"#p{i}" for i in range(1, 6)]  # a0 huge, rest len-3
    actions = [_action(i, sel, confidence=0.99 - i * 0.01) for i, sel in enumerate(selectors)]

    remaining_target = 200  # overhead(200) + meta + sections(0) are added back below
    total_budget = remaining_target + _estimate_meta_chars(meta) + 200

    _, packed, _ = _apply_total_output_budget(
        sections=[],
        actions=actions,
        meta=meta,
        total_budget=total_budget,
        min_actions_to_keep=2,
        name_max=80,
        selector_max=400,  # keep a0's long selector rather than dropping it
    )

    aids = [a["aid"] for a in packed]
    assert len(aids) == len(set(aids)), f"duplicate actions packed: {aids}"

    originals = set(selectors)
    for a in packed:
        sel = a["strategy"].get("selector")
        if sel is not None:
            assert sel in originals, f"packed a truncated/foreign selector: {sel!r}"

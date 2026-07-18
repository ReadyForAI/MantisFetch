"""A6: _apply_total_output_budget must not duplicate actions or emit a truncated
CSS selector (a 40-char prefix matches the wrong node).

A1 (2026-07-17): WebMCP input_schema must count toward the budget and oversized
schemas must be stubbed rather than shipping multi-KB JSON in distill.
"""

import json

from mantisfetch_browser.ranking import (
    _WEBMCP_SCHEMA_MAX_CHARS,
    _apply_total_output_budget,
    _estimate_action_chars,
    _estimate_meta_chars,
    _trim_action_fields,
)


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


def _webmcp_action(i: int, schema: dict, confidence: float = 0.95) -> dict:
    return {
        "aid": f"wm{i}",
        "role": "webmcp_tool",
        "name": f"[WebMCP] tool_{i}",
        "strategy": {
            "type": "webmcp",
            "tool_name": f"tool_{i}",
            "source": "webmcp",
            "input_schema": schema,
        },
        "actions": ["invoke"],
        "confidence": confidence,
        "source": "webmcp",
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


def test_webmcp_large_schema_trimmed_and_counted_in_budget():
    """A multi-KB input_schema must not blow past total_output_budget_chars."""
    huge_schema = {
        "type": "object",
        "properties": {f"field_{i}": {"type": "string", "description": "x" * 80} for i in range(80)},
    }
    schema_json_len = len(json.dumps(huge_schema, ensure_ascii=False, separators=(",", ":")))
    assert schema_json_len > _WEBMCP_SCHEMA_MAX_CHARS

    raw = _webmcp_action(0, huge_schema)
    # Estimate before trim must include the full schema cost.
    assert _estimate_action_chars(raw) > _WEBMCP_SCHEMA_MAX_CHARS

    trimmed = _trim_action_fields(raw, name_max=80, selector_max=400)
    assert trimmed["strategy"]["input_schema"] == {"schema_truncated": True}
    assert "webmcp_discover" in (trimmed["strategy"].get("schema_note") or "")
    assert trimmed["strategy"]["tool_name"] == "tool_0"

    # After trim, packer must keep the action within a tight budget.
    meta: dict = {}
    total_budget = 400 + _estimate_meta_chars(meta) + 200
    _, packed, _ = _apply_total_output_budget(
        sections=[],
        actions=[raw, _webmcp_action(1, {"type": "object", "properties": {}})],
        meta=meta,
        total_budget=total_budget,
        min_actions_to_keep=2,
        name_max=80,
        selector_max=400,
    )
    assert len(packed) >= 1
    for a in packed:
        if a["strategy"].get("type") == "webmcp":
            schema = a["strategy"].get("input_schema")
            if schema is not None:
                encoded = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
                assert len(encoded) <= _WEBMCP_SCHEMA_MAX_CHARS + 50
    # Total packed action payload must fit the remaining budget envelope.
    packed_chars = sum(_estimate_action_chars(a) for a in packed)
    assert packed_chars <= total_budget

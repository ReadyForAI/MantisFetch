"""Distill output post-processing: stable ids, text utils, diffs, action budget.

Pure helper functions used to turn a raw page distillation into a stable,
budget-bounded payload:

- stable ids / hashing (`_aid`, `_hash_text`, `_make_stable_sid`),
- text normalize / clip / word-boundary truncate (`_normalize`, `_clip`,
  `_smart_truncate`),
- incremental diffs between captures (`_sections_diff`, `_actions_diff`),
- action dedup / ranking / field trimming (`_dedup_actions`, `_rank_actions`,
  `_pick_action_methods`, `_trim_action_fields`),
- total-output character budgeting (`_estimate_*_chars`, `_apply_total_output_budget`).

Self-contained leaf: depends only on the stdlib (json, hashlib, re).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def _aid(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return "a" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def _hash_text(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def _clip(s: str, max_chars: int) -> str:
    if len(s) <= max_chars:
        return s
    cut = s[:max_chars]
    last = cut.rfind("\n\n")
    if last > max_chars * 0.6:
        cut = cut[:last]
    return cut.strip()


# Word-boundary truncation
def _smart_truncate(s: str, max_chars: int) -> str:
    if len(s) <= max_chars:
        return s
    cut = s[:max_chars]
    last_space = cut.rfind(" ")
    if last_space > max_chars * 0.6:
        cut = cut[:last_space]
    return cut.rstrip() + "…"


def _normalize(s: str) -> str:
    s = re.sub(r"\r\n|\r", "\n", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _make_stable_sid(heading: str | None, text: str) -> str:
    h = (heading or "").strip().lower()
    t = re.sub(r"\s+", " ", (text or "").strip()).lower()
    anchor = t[:400]
    raw = (h + "\n" + anchor).encode("utf-8", errors="ignore")
    return "s_" + hashlib.sha1(raw).hexdigest()[:10]


def _sections_diff(
    old_sections: list[dict[str, Any]], new_sections: list[dict[str, Any]]
) -> dict[str, Any]:
    old_map = {s["sid"]: _hash_text(s["t"]) for s in old_sections}
    new_map = {s["sid"]: _hash_text(s["t"]) for s in new_sections}

    old_sids = set(old_map)
    new_sids = set(new_map)

    added = sorted(new_sids - old_sids)  # set comprehension
    removed = sorted(old_sids - new_sids)
    changed = sorted(sid for sid in (old_sids & new_sids) if old_map[sid] != new_map[sid])

    return {"added_sids": added, "removed_sids": removed, "changed_sids": changed}


def _actions_diff(
    old_actions: list[dict[str, Any]], new_actions: list[dict[str, Any]]
) -> dict[str, Any]:
    old_set = {a["aid"] for a in old_actions}  # ✅ IMPROVED
    new_set = {a["aid"] for a in new_actions}
    return {
        "actions_added": sorted(new_set - old_set),
        "actions_removed": sorted(old_set - new_set),
    }


def _pick_action_methods(role: str) -> list[str]:
    acts = ["scroll_into_view"]
    if role in ("button", "link", "checkbox", "radio"):
        acts.insert(0, "click")
    if role == "textbox":
        acts.insert(0, "type")
    if role == "combobox":
        acts.insert(0, "select")
    return acts


def _dedup_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for a in actions:
        key = (
            a.get("role", ""),
            a.get("name", ""),
            json.dumps(a.get("strategy", {}), sort_keys=True),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def _merge_actions(
    primary: list[dict[str, Any]], secondary: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge two action lists keyed by (role, name, nth) identity.

    ``primary`` (the a11y tree) defines the canonical identity, ordering, and
    surviving aid; a css fallback contributed by either source is folded into the
    surviving entry, and elements only ``secondary`` (DOM) surfaced are appended.
    Confidence becomes the max of the merged entries. Inputs are not mutated.
    """

    def key(a: dict[str, Any]) -> tuple[Any, ...]:
        st = a.get("strategy") or {}
        if st.get("type") == "role":
            return ("role", st.get("role") or "", st.get("name") or "", st.get("nth", 0))
        return ("css", st.get("selector") or "")

    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    for a in [*primary, *secondary]:
        k = key(a)
        existing = by_key.get(k)
        if existing is None:
            entry = dict(a)
            entry["strategy"] = dict(a.get("strategy") or {})
            by_key[k] = entry
            order.append(k)
            continue
        st = a.get("strategy") or {}
        if not existing["strategy"].get("css") and st.get("css"):
            existing["strategy"]["css"] = st["css"]
        existing["confidence"] = max(
            float(existing.get("confidence", 0.0)), float(a.get("confidence", 0.0))
        )
    return [by_key[k] for k in order]


def _rank_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    src_w = {"dom": 0.03, "a11y": 0.02, "vision": 0.01}
    role_w = {
        "textbox": 0.03,
        "button": 0.02,
        "combobox": 0.02,
        "link": 0.015,
        "checkbox": 0.01,
        "radio": 0.01,
    }

    def score(a) -> float:
        c = float(a.get("confidence", 0.7))
        c += src_w.get(a.get("source", "dom"), 0.0)
        c += role_w.get(a.get("role", ""), 0.0)
        if (a.get("name") or "").strip():
            c += 0.02
        if (a.get("strategy") or {}).get("type") == "role":
            c += 0.01
        return c

    return sorted(actions, key=score, reverse=True)


# WebMCP tools embed a full JSON Schema in strategy.input_schema. A single
# complex form tool can be multi-KB; past this size we drop the schema body and
# point the consumer at webmcp discovery for the full definition.
_WEBMCP_SCHEMA_MAX_CHARS = 2048
_WEBMCP_SCHEMA_NOTE = (
    "schema truncated; full input_schema via POST /web/session/webmcp_discover "
    "or MCP web_webmcp_discover"
)


def _webmcp_schema_chars(strat: dict[str, Any]) -> int:
    schema = strat.get("input_schema")
    if schema is None:
        return 0
    try:
        return len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return 0


def _trim_action_fields(a: dict[str, Any], name_max: int, selector_max: int) -> dict[str, Any]:
    a = dict(a)
    a["name"] = _smart_truncate(a.get("name") or "", name_max)  # word-boundary truncation

    strat = dict(a.get("strategy") or {})
    if strat.get("type") == "css":
        # Never truncate a CSS selector — a partial selector matches the wrong
        # element or nothing. Keep it whole within budget; drop it otherwise so
        # the consumer fails fast (empty-selector error) instead of acting on the
        # wrong node.
        selector = strat.get("selector") or ""
        if len(selector) > selector_max:
            strat.pop("selector", None)
    elif strat.get("type") == "role":
        strat["name"] = _smart_truncate(strat.get("name") or "", name_max)
        # Drop (never truncate) an oversized css fallback for the same reason; the
        # role identity remains the primary locator either way.
        css = strat.get("css") or ""
        if len(css) > selector_max:
            strat.pop("css", None)
    elif strat.get("type") == "webmcp":
        # Oversized schemas punch a hole in total_output_budget_chars; replace
        # with a stub so the action remains invokable (tool_name kept) without
        # shipping multi-KB JSON in every distill.
        if _webmcp_schema_chars(strat) > _WEBMCP_SCHEMA_MAX_CHARS:
            strat["input_schema"] = {"schema_truncated": True}
            strat["schema_note"] = _WEBMCP_SCHEMA_NOTE
    a["strategy"] = strat
    return a


def _estimate_meta_chars(meta: dict[str, Any]) -> int:
    try:
        return len(json.dumps(meta, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        return 200


def _estimate_action_chars(a: dict[str, Any]) -> int:
    # Prefer compact serialization so packing matches real response cost
    # (aid/actions/confidence/strategy punctuation + any input_schema).
    try:
        return len(json.dumps(a, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        role = a.get("role") or ""
        name = a.get("name") or ""
        strat = a.get("strategy") or {}
        sel = strat.get("selector") or strat.get("name") or ""
        css = strat.get("css") or ""
        schema_chars = _webmcp_schema_chars(strat) if strat.get("type") == "webmcp" else 0
        return 40 + len(role) + len(name) + len(sel) + len(css) + schema_chars + len(
            a.get("source", "")
        )


def _apply_total_output_budget(
    sections: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    meta: dict[str, Any],
    total_budget: int,
    min_actions_to_keep: int,
    name_max: int,
    selector_max: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    actions = [
        _trim_action_fields(a, name_max=name_max, selector_max=selector_max) for a in actions
    ]

    meta_chars = _estimate_meta_chars(meta)
    sec_chars = sum(len(s.get("t") or "") + len(s.get("h") or "") for s in sections)

    overhead = 200
    remaining = total_budget - (meta_chars + sec_chars + overhead)

    if remaining < 0:
        need = -remaining
        for i in range(len(sections) - 1, -1, -1):
            t = sections[i].get("t") or ""
            if len(t) <= 220:
                continue
            cut = min(need, len(t) - 220)
            sections[i]["t"] = t[: max(220, len(t) - cut)]
            need -= cut
            if need <= 0:
                break

        sec_chars = sum(len(s.get("t") or "") + len(s.get("h") or "") for s in sections)
        remaining = total_budget - (meta_chars + sec_chars + overhead)

    if remaining <= 0 or not actions:
        return sections, [], meta

    ranked = _rank_actions(_dedup_actions(actions))
    packed: list[dict[str, Any]] = []
    packed_idx: set[int] = set()
    used = 0

    # Round 1: pack the highest-ranked actions up to min_actions_to_keep. Actions
    # are already field-trimmed; if one still doesn't fit, skip it — never
    # truncate a CSS selector to force a fit (a partial selector matches the wrong
    # node or nothing), matching _trim_action_fields's drop-don't-truncate policy.
    for i, a in enumerate(ranked):
        if len(packed) >= min_actions_to_keep:
            break
        size = _estimate_action_chars(a)
        if used + size <= remaining:
            packed.append(a)
            packed_idx.add(i)
            used += size

    # Round 2: fill the remaining budget with actions not already packed (exclude
    # by index so a round-1 pick can't be appended a second time).
    for i, a in enumerate(ranked):
        if i in packed_idx:
            continue
        size = _estimate_action_chars(a)
        if used + size <= remaining:
            packed.append(a)
            packed_idx.add(i)
            used += size

    return sections, packed, meta

"""doc_chunks must not hand a RAG consumer broken markdown.

The splitter divided text on every blank line. A blank line between two
functions in a code sample is completely ordinary, so a fenced block was cut in
half: the first chunk kept an opening ``` with nothing closing it, the next
started with a stray closing one. Either half renders everything after the
orphan fence as code.

Two things measured before writing any of this, because the plan called for
porting browser-use's whole atomic-block chunker and most of it turned out to be
unnecessary here:

- markdown tables are never split. They carry no blank lines, so the splitter
  already treats one as a single paragraph — it becomes one oversized chunk,
  which is the same soft-limit choice browser-use makes.
- doc_table needs no pagination. Once captures stopped clipping, a 223-row table
  stores whole (measured: 224 markdown rows, truncated=false, Tuvalu present) and
  reads back in ~2,400 tokens for a call that deliberately asks for that table.

So only the fence case is a real defect, and only that is fixed.
"""

import mantisfetch_docreader as dr

FENCED = """Intro paragraph before the sample.

```python
def one():
    return 1

def two():
    return 2
```

Text after the sample."""


def _record(text: str) -> dict:
    return {
        "doc_id": "D-1", "sid": "s1", "title": "T", "text": text,
        "token_estimate": dr._estimate_tokens(text),
    }


def _split(text: str, *, max_tokens: int, overlap_tokens: int = 0) -> list[str]:
    chunks = dr._split_text_by_token_estimate(
        _record(text), max_tokens=max_tokens, overlap_tokens=overlap_tokens,
        include_text=True, start_index=1,
    )
    return [c.get("text") or "" for c in chunks]


# ── fences stay whole ───────────────────────────────────────────────────────────
def test_a_blank_line_inside_a_fence_does_not_split_it() -> None:
    for chunk in _split(FENCED, max_tokens=12):
        assert chunk.count("```") % 2 == 0, f"unbalanced fence in {chunk!r}"


def test_the_fence_survives_a_budget_smaller_than_itself() -> None:
    """A block larger than the budget is emitted whole rather than cut — the
    same soft limit browser-use applies, and the alternative is invalid output."""
    chunks = _split(FENCED, max_tokens=5)
    code = [c for c in chunks if "```" in c]
    assert len(code) == 1
    assert "def one():" in code[0] and "def two():" in code[0]
    assert code[0].count("```") == 2


def test_prose_around_a_fence_is_still_split_normally() -> None:
    chunks = _split(FENCED, max_tokens=12)
    assert len(chunks) > 1
    assert "Intro paragraph" in chunks[0]
    assert any("Text after the sample" in c for c in chunks)


def test_tilde_fences_count_too() -> None:
    text = "Intro line here.\n\n~~~\ncode one\n\ncode two\n~~~\n\nAfter."
    for chunk in _split(text, max_tokens=8):
        assert chunk.count("~~~") % 2 == 0


def test_an_unclosed_fence_does_not_swallow_the_rest_silently() -> None:
    """Malformed input still produces output; it just stays in one block."""
    text = "Intro.\n\n```python\ncode\n\nmore code"
    chunks = _split(text, max_tokens=8)
    assert any("code" in c for c in chunks)


# ── overlap carries whole lines ─────────────────────────────────────────────────
def test_overlap_starts_on_a_line_boundary() -> None:
    """It was the last N characters, which cuts mid-word and mid-table-row. Half
    a row is not context, which is what the overlap is for."""
    para = "Sentence one here. " * 20
    chunks = _split(para + "\n\n" + para, max_tokens=40, overlap_tokens=8)
    assert len(chunks) > 1
    for chunk in chunks[1:]:
        assert not chunk.startswith(" ")
        assert chunk.lstrip().startswith("Sentence")


def test_zero_overlap_carries_nothing() -> None:
    assert dr._tail_lines_within("a\nb\nc", 0) == ""


def test_overlap_keeps_at_least_one_line_even_when_it_does_not_fit() -> None:
    """Otherwise a long final line would produce an empty overlap, which is
    silently no overlap at all."""
    long_line = "word " * 200
    assert dr._tail_lines_within(long_line, 2).strip() != ""


# ── the block splitter itself ───────────────────────────────────────────────────
def test_blocks_split_on_blank_lines_outside_fences() -> None:
    assert dr._split_into_atomic_blocks("one\n\ntwo\n\nthree") == ["one", "two", "three"]


def test_a_fence_is_one_block_regardless_of_blank_lines() -> None:
    blocks = dr._split_into_atomic_blocks("before\n\n```\na\n\nb\n```\n\nafter")
    assert blocks == ["before", "```\na\n\nb\n```", "after"]


def test_a_table_is_one_block() -> None:
    """Confirms the measurement above: no blank lines, so it was never at risk."""
    table = "| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"
    assert dr._split_into_atomic_blocks(table) == [table]


def test_empty_and_whitespace_input() -> None:
    assert dr._split_into_atomic_blocks("") == []
    assert dr._split_into_atomic_blocks("\n\n   \n\n") == []

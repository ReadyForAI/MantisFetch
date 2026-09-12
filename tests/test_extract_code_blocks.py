"""A code block keeps its line breaks and its indentation.

`_clean_text` collapsed every run of whitespace, and `<pre>` went through it
like a paragraph. Python came back as one line, YAML lost the nesting that *is*
its meaning, and a shell transcript lost the boundary between commands. The
damage happens at capture: nothing downstream can put the newlines back.

The Markdown path never had the problem — a fenced block is collected verbatim
there — so the two paths also disagreed about the same document depending on
how it was served.
"""

from mantisfetch_browser.extract import html_to_blocks, markdown_to_blocks

PY_SRC = 'def hello():\n    print("first")\n    print("second")\n'


def _first(tag, blocks):
    return next(b["text"] for b in blocks if b["tag"] == tag)


def test_python_keeps_its_indentation() -> None:
    blocks = html_to_blocks(f"<article><h1>T</h1><pre><code>{PY_SRC}</code></pre></article>")
    assert _first("pre", blocks) == PY_SRC.rstrip("\n")


def test_yaml_keeps_the_nesting_that_is_its_meaning() -> None:
    yaml = "services:\n  web:\n    image: nginx\n    ports:\n      - 80:80\n"
    blocks = html_to_blocks(f"<article><pre>{yaml}</pre></article>")
    assert _first("pre", blocks) == yaml.rstrip("\n")


def test_a_shell_transcript_keeps_its_command_boundaries() -> None:
    sh = "$ git status\nOn branch main\n$ git log --oneline -1\nabc1234 fix\n"
    blocks = html_to_blocks(f"<article><pre>{sh}</pre></article>")
    assert _first("pre", blocks).splitlines() == sh.rstrip("\n").splitlines()


def test_inline_highlighting_spans_do_not_add_gaps() -> None:
    """Highlighters wrap tokens in spans. Those must not become spaces, and the
    line structure around them must survive."""
    html = (
        "<article><pre><code>"
        '<span class="k">def</span> <span class="n">hello</span>():\n'
        '    <span class="k">return</span> 1\n'
        "</code></pre></article>"
    )
    assert _first("pre", html_to_blocks(html)) == "def hello():\n    return 1"


def test_prose_is_still_collapsed() -> None:
    """The other half: a paragraph broken across source lines is one sentence,
    and it should read as one."""
    html = "<article><p>This paragraph\n   is broken across\n\tsource lines.</p></article>"
    assert _first("p", html_to_blocks(html)) == "This paragraph is broken across source lines."


def test_the_two_paths_agree_about_the_same_code() -> None:
    """HTML and Markdown are two renderings of one document; the block they
    produce for the same code should not depend on which one arrived."""
    from_html = _first(
        "pre", html_to_blocks(f"<article><pre><code>{PY_SRC}</code></pre></article>")
    )
    from_md = _first("pre", markdown_to_blocks(f"# T\n\n```python\n{PY_SRC}```\n"))
    assert from_html == from_md


def test_a_pathological_pre_is_still_bounded() -> None:
    """Keeping newlines must not lift the ceiling on what one capture writes."""
    blob = ("x" * 200 + "\n") * 200
    blocks = html_to_blocks(f"<article><pre>{blob}</pre></article>", max_chars=1000)
    assert sum(len(b["text"]) for b in blocks) <= 1000 + 200 * 201


# ── and it has to survive the rest of the pipeline ───────────────────────────────
def test_indentation_survives_section_assembly() -> None:
    """The extractor keeping newlines is half the job. Section assembly ran the
    joined text through a normalizer that collapses runs of spaces and tabs —
    right for a paragraph, and where the indentation was being lost the second
    time."""
    import mantisfetch_browser as web

    blocks = html_to_blocks(
        f"<article><h1>T</h1><p>Some  prose   with gaps.</p>"
        f"<pre><code>{PY_SRC}</code></pre></article>"
    )
    sections = web._blocks_to_sections_stable(blocks, 10, 10_000, 100_000)

    body = sections[0]["t"]
    assert '    print("first")' in body, body
    assert "Some prose with gaps." in body, "prose should still be collapsed"


def test_a_pre_that_uses_br_for_its_line_breaks() -> None:
    """Some pages render a code block's newlines as <br> rather than as literal
    newlines. With the verbatim separator empty, dropping them joined the two
    lines into one word — worse than the space the collapsing version gave."""
    blocks = html_to_blocks(
        "<article><pre>echo first-command<br>echo second-command</pre></article>"
    )
    assert _first("pre", blocks) == "echo first-command\necho second-command"


# ── a code block inside a list item or a quote ───────────────────────────────────
# The outermost container is emitted whole so its text lands in one block, and a
# <pre> inside it used to go through the prose collapse with it. The code in a
# tutorial's numbered steps and in a quoted example came out as one line.


def _blocks(html):
    return [(b["tag"], b["text"]) for b in html_to_blocks(html)]


def test_a_code_block_in_a_quote_keeps_its_lines() -> None:
    html = f"<article><blockquote><pre><code>{PY_SRC}</code></pre></blockquote></article>"
    assert _blocks(html) == [("pre", PY_SRC.rstrip("\n"))]


def test_a_code_block_in_a_list_item_keeps_its_lines_and_its_place() -> None:
    """The step's prose stays on either side of its code, in order."""
    html = (
        "<article><ol><li>Define the function first:"
        f"<pre><code>{PY_SRC}</code></pre>"
        "then call it from the module.</li></ol></article>"
    )
    assert _blocks(html) == [
        ("li", "Define the function first:"),
        ("pre", PY_SRC.rstrip("\n")),
        ("li", "then call it from the module."),
    ]


def test_a_code_block_two_containers_deep() -> None:
    html = (
        "<article><ul><li>Setup<ol><li>Write the config:"
        "<pre>services:\n  web:\n    image: nginx</pre>"
        "</li></ol></li></ul></article>"
    )
    assert _blocks(html) == [
        ("li", "Setup Write the config:"),
        ("pre", "services:\n  web:\n    image: nginx"),
    ]


def test_highlighting_and_br_inside_a_nested_code_block() -> None:
    html = (
        "<article><blockquote><p>As the docs put it:</p><pre><code>"
        '<span class="k">def</span> <span class="n">f</span>():<br>'
        '    <span class="k">return</span> 1'
        "</code></pre></blockquote></article>"
    )
    assert _blocks(html) == [
        ("blockquote", "As the docs put it:"),
        ("pre", "def f():\n    return 1"),
    ]


def test_a_nested_code_block_is_written_once() -> None:
    html = f"<article><ul><li>Run it:<pre>{PY_SRC}</pre></li></ul></article>"
    blocks = html_to_blocks(html)
    assert sum(PY_SRC.strip().splitlines()[0] in b["text"] for b in blocks) == 1


def test_a_container_without_code_is_written_as_before() -> None:
    """Pages with no nested code must produce exactly what they did, or every
    re-capture of them gets a new content hash."""
    html = (
        "<article><ul><li>A list item\n   broken <b>across</b> lines, long enough.</li></ul>"
        "<blockquote><p>A quoted\tparagraph that is long enough.</p></blockquote></article>"
    )
    assert _blocks(html) == [
        ("li", "A list item broken across lines, long enough."),
        ("blockquote", "A quoted paragraph that is long enough."),
    ]


def test_a_short_step_before_its_code_is_kept() -> None:
    """Whether a container is kept is decided on all of it. "Run:" alone is
    under the body minimum, and dropping it would lose what the code is for."""
    html = "<article><ol><li>Run:<pre>pip install mantisfetch</pre></li></ol></article>"
    assert _blocks(html) == [("li", "Run:"), ("pre", "pip install mantisfetch")]


def test_nested_code_survives_sections_and_the_stored_capture(tmp_path) -> None:
    """Through the rest of the pipeline: section assembly, then what is written
    to disk and read back."""
    import json

    import mantisfetch_browser as web

    blocks = html_to_blocks(
        "<article><h1>T</h1><ol><li>Define it:"
        f"<pre><code>{PY_SRC}</code></pre></li></ol></article>"
    )
    sections = web._blocks_to_sections_stable(blocks, 10, 10_000, 100_000)
    assert '    print("first")' in sections[0]["t"], sections[0]["t"]

    web._persist_web_capture(
        "WEB-1", "https://example.com/t", "T", sections, "d", [], "h", tmp_path
    )
    doc_dir = tmp_path / "General" / "WEB-1"
    manifest = json.loads((doc_dir / "manifest.json").read_text())
    section_file = doc_dir / manifest["sections"][0]["file"]
    assert '\n    print("first")\n' in section_file.read_text()
    assert '\n    print("first")\n' in (doc_dir / "full.md").read_text()

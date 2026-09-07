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

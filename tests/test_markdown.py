from markdown_it import MarkdownIt

from slack_conductor.markdown import BLOCK_BUDGET, markdown_chunks
from slack_conductor.presentation import pages

MD = MarkdownIt("js-default")


def test_many_short_headings_split_before_slack_expansion_limit():
    text = "\n\n".join(f"# Heading {i}" for i in range(75))
    parts = list(markdown_chunks(text))
    assert len(parts) == 4
    assert "\n\n".join(parts) == text
    assert all(p.count("# Heading") <= BLOCK_BUDGET for p in parts)


def test_formatted_paragraph_moves_whole_instead_of_splitting_its_emphasis():
    paragraph = "**Important: " + "preserve this " * 100 + "**"
    parts = list(markdown_chunks("a" * 3000 + "\n\n" + paragraph))
    assert parts == ["a" * 3000, paragraph]


def test_oversized_paragraph_reopens_nested_emphasis_and_links():
    text = "***" + ("important words " * 500).rstrip() + "*** [docs](https://example.com/a_(b))"
    parts = list(markdown_chunks(text))
    assert len(parts) > 1 and all(len(p) <= 3900 for p in parts)
    count = 0
    for p in parts:
        children = MD.parseInline(p)[0].children
        assert sum(t.nesting for t in children) == 0
        assert any(t.type == "strong_open" for t in children)
        count += sum(t.content.count("important words") for t in children if t.type == "text")
    assert count == 500
    assert parts[-1].endswith("[docs](https://example.com/a_(b))")


def test_long_table_repeats_header_and_preserves_each_row_once():
    header = "| Name | State |\n| --- | --- |\n"
    rows = [f"| job {i} | **passed** |\n" for i in range(180)]
    parts = list(markdown_chunks("# Results\n\n" + header + "".join(rows)))
    tables = [
        "\n".join(p.splitlines()[slice(*t.map)])
        for p in parts
        for t in MD.parse(p)
        if t.type == "table_open"
    ]
    assert len(tables) >= 2
    assert all(p.startswith(header) and len(p.splitlines()) <= 101 for p in tables)
    assert [line for p in tables for line in p.splitlines()[2:]] == [r.rstrip() for r in rows]
    assert all(any(t.type == "table_open" for t in MD.parse(p)) for p in tables)


def test_many_short_table_rows_respect_slack_row_limit():
    text = "| A |\n| - |\n" + "| x |\n" * 180
    tables = [t for p in markdown_chunks(text) for t in MD.parse(p) if t.type == "table_open"]
    assert len(tables) == 2
    assert all(t.map[1] - t.map[0] <= 101 for t in tables)


def test_multiline_single_list_item_and_nested_quote_stay_bounded():
    item = "1. **Start**\n" + "   continuation words\n" * 400
    for text in [item, "> > " + "nested quote words " * 500]:
        parts = list(markdown_chunks(text))
        assert len(parts) > 1 and all(len(p) <= 3900 for p in parts)
        assert all(MD.parse(p)[0].type == MD.parse(text)[0].type for p in parts)


def test_long_list_keeps_each_number_and_nested_code():
    text = "# Steps\n\n" + "\n".join(f"{i}. **step {i}**" for i in range(1, 150))
    parts = list(markdown_chunks(text))
    combined = "\n".join(parts)
    for i in range(1, 150):
        assert combined.count(f"{i}. **step {i}**") == 1
    assert all(len(p) <= 3900 for p in parts)


def test_tilde_fence_containing_backticks_preserves_language_and_content():
    code = ('print("``` is literal code")\n' * 300).rstrip()
    parts = list(markdown_chunks("~~~python\n" + code + "\n~~~"))
    fences = [t for p in parts for t in MD.parse(p) if t.type == "fence"]
    assert len(fences) == len(parts) > 1
    assert all(t.info == "python" for t in fences)
    assert "".join(t.content for t in fences).rstrip() == code


def test_inline_code_and_quote_continuations_stay_formatted():
    for text, expected in [
        ("`" + "literal_value " * 500 + "`", "code_inline"),
        ("> " + "quoted text " * 500, "blockquote_open"),
    ]:
        parts = list(markdown_chunks(text))
        assert len(parts) > 1
        assert all(len(p) <= 3900 for p in parts)
        for p in parts:
            tokens = MD.parseInline(p)[0].children if expected == "code_inline" else MD.parse(p)
            assert any(t.type == expected for t in tokens)


def test_continuations_keep_status_headers_for_block_limit_splits():
    output = pages("done", "\n\n".join(f"# Heading {i}" for i in range(75)))
    assert len(output) == 4
    assert output[0].startswith("✅ *Done*\n\n")
    assert all(p.startswith("✅ *Done* · continued\n\n") for p in output[1:])

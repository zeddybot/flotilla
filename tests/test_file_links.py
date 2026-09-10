import pytest

from slack_conductor.file_links import image_blocks, local_path, references, substitute


def test_links_anywhere_ignore_code_and_preserve_source_spans():
    text = r"""# Result
![Screenshot](<.context/my image.png>) and [report](report(1).pdf).

BACKTICK[literal](secret.txt)BACKTICK and escaped \[example](secret.txt).

BACKTICKBACKTICKBACKTICKmd
![example](secret.png)
BACKTICKBACKTICKBACKTICK

    [indented example](secret.csv)

> [download][report]

[report]: report.csv
""".replace("BACKTICK", chr(96))
    found = references(text)
    assert [r["href"] for r in found] == [".context/my%20image.png", "report(1).pdf", "report.csv"]
    assert [text[r["start"] : r["end"]] for r in found] == [
        "![Screenshot](<.context/my image.png>)",
        "[report](report(1).pdf)",
        "[download][report]",
    ]
    assert found[0]["label"] == "Screenshot"


@pytest.mark.parametrize(
    "href",
    [
        "https://example.com/a.pdf",
        "mailto:a@b.com",
        "//host/a",
        "#heading",
        "app.py:42",
        "/src/app.py:42:2",
        "app.py#L4-L8",
    ],
)
def test_non_file_and_source_links_are_unchanged(href, tmp_path):
    assert local_path(href, tmp_path) is None


def test_relative_absolute_encoded_and_file_uri_paths(tmp_path):
    assert local_path("out/my%20file.csv", tmp_path) == tmp_path / "out/my file.csv"
    assert local_path(str(tmp_path / "a.png"), None) == tmp_path / "a.png"
    assert local_path((tmp_path / "a.png").as_uri(), None) == tmp_path / "a.png"
    assert references("[file](file:///tmp/a.png)")[0]["href"] == "file:///tmp/a.png"
    with pytest.raises(ValueError, match="workspace path"):
        local_path("relative.csv", None)


def test_replacement_and_images_preserve_order_and_missing_file_answer():
    text = "Before. ![Plot](plot.png) Between. [CSV](data.csv) After. [Missing](gone.pdf)"
    refs = references(text)
    files = [
        {"permalink": "https://slack.test/plot", "image": True},
        {"permalink": "https://slack.test/csv"},
        {"error": "not found"},
    ]
    for i, ref in enumerate(refs):
        ref["file"] = i
    output = substitute(text, refs, files)
    blocks = image_blocks(output, {"https://slack.test/plot": "F123"})
    assert [b["type"] for b in blocks] == ["markdown", "image", "markdown"]
    assert blocks[0]["text"] == "Before."
    assert blocks[1]["slack_file"] == {"id": "F123"}
    assert blocks[1]["alt_text"] == "Plot"
    assert (
        blocks[2]["text"]
        == "Between. [CSV](https://slack.test/csv) After. Missing (file unavailable: not found)"
    )

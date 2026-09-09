"""Locate Markdown file references without treating code examples as attachments."""

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_inline import image, link


def _located(rule):
    def parse(state, silent):
        start, count = state.pos, len(state.tokens)
        matched = rule(state, silent)
        if matched and not silent:
            kind = "image" if rule is image else "link_open"
            token = next(t for t in state.tokens[count:] if t.type == kind)
            token.meta["span"] = (start, state.pos)
            label_start = start + (2 if token.type == "image" else 1)
            label_end = state.md.helpers.parseLinkLabel(state, label_start - 1, False)
            token.meta["label"] = state.src[label_start:label_end]
        return matched

    return parse


PARSER = MarkdownIt("js-default")
PARSER.inline.ruler.at("link", _located(link))
PARSER.inline.ruler.at("image", _located(image))
_validate = PARSER.validateLink
PARSER.validateLink = lambda url: url.startswith("file:") or _validate(url)


def references(text):
    """Return source spans, including reference-style links, preserving surrounding Markdown."""
    env = {}
    blocks = PARSER.parse(text, env)
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    masked = list(text)
    excluded = [t.map for t in blocks if t.type in {"fence", "code_block", "html_block"}]
    excluded += [r["map"] for r in env.get("references", {}).values()]
    for start, end in excluded:
        for i in range(offsets[start], offsets[end]):
            if masked[i] != "\n":
                masked[i] = " "
    tokens = []
    PARSER.inline.parse("".join(masked), PARSER, env, tokens)
    found = []
    for token in tokens:
        if token.type not in {"link_open", "image"} or "span" not in token.meta:
            continue
        start, end = token.meta["span"]
        found.append(
            {
                "start": start,
                "end": end,
                "label": token.meta["label"],
                "image": token.type == "image",
                "href": token.attrGet("src" if token.type == "image" else "href"),
            }
        )
    return sorted(found, key=lambda ref: (ref["start"], -ref["end"]))


def local_path(href, workspace):
    """Only explicit local file links opt in; source locations remain references."""
    url = urlsplit(href)
    if url.scheme not in {"", "file"} or url.netloc or not url.path:
        return None
    path = unquote(url.path)
    if re.search(r":\d+(?::\d+)?$", path) or re.fullmatch(r"L\d+(?:-L?\d+)?", url.fragment):
        return None
    path = Path(path).expanduser()
    if not path.is_absolute():
        if not workspace:
            raise ValueError("workspace path unavailable")
        path = Path(workspace) / path
    return path.resolve()


def substitute(text, refs, files):
    """Replace selected links using durable upload results; never alter literal examples."""
    cursor, pieces = 0, []
    for ref in refs:
        if ref["start"] < cursor:
            continue
        item = files[ref["file"]]
        pieces.append(text[cursor : ref["start"]])
        if item.get("permalink"):
            prefix = "!" if ref["image"] and item.get("image") else ""
            pieces.append(f"{prefix}[{ref['label']}]({item['permalink']})")
        else:
            pieces.append(
                f"{ref['label']} (file unavailable: {item.get('error', 'upload pending')})"
            )
        cursor = ref["end"]
    return "".join(pieces) + text[cursor:]


def image_blocks(body, images):
    """Place uploaded image blocks between the surrounding Markdown fragments."""
    cursor, output = 0, []
    for ref in references(body):
        if not ref["image"] or ref["href"] not in images or ref["start"] < cursor:
            continue
        before = body[cursor : ref["start"]].strip()
        if before:
            output.append({"type": "markdown", "text": before})
        output.append(
            {
                "type": "image",
                "slack_file": {"id": images[ref["href"]]},
                "alt_text": ref["label"][:2000] or "Agent image",
            }
        )
        cursor = ref["end"]
    if body[cursor:].strip():
        output.append({"type": "markdown", "text": body[cursor:].strip()})
    return output

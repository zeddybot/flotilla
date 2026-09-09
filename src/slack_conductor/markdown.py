"""Split Markdown at syntax boundaries before Slack expands it into native blocks."""

import re

from markdown_it import MarkdownIt

MD = MarkdownIt("js-default")
BLOCK_BUDGET = 24  # Leave room for Slack's expansion and our status header (limit: 50).


def _cost(tokens):
    kinds = {
        "heading_open",
        "paragraph_open",
        "fence",
        "code_block",
        "hr",
        "table_open",
        "blockquote_open",
        "bullet_list_open",
        "ordered_list_open",
    }
    return sum(t.type in kinds and not t.hidden for t in tokens) + sum(
        2 for t in tokens for child in (t.children or []) if child.type == "image"
    )


def _escape(text):
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", text)


def _slices(text, limit, lines=False):
    if limit < 2:
        raise ValueError("Markdown wrappers exceed the message size")
    while len(text) > limit:
        newline = text.rfind("\n", 0, limit)
        cut = (newline if lines else max(newline, text.rfind(" ", 0, limit))) + 1 or limit
        if len(text[:cut]) - len(text[:cut].rstrip("\\")) & 1:
            cut -= 1
        if cut <= 0:
            cut = limit
        yield text[:cut]
        text = text[cut:]
    if text:
        yield text


def _inline(text, limit, budget=BLOCK_BUDGET):
    """Reopen emphasis, links and inline code when a single paragraph is oversized."""
    stack, page, weight = [], "", 0
    for token in MD.parseInline(text)[0].children:
        if token.nesting == 1:
            pair = {
                "strong_open": ("**", "**"),
                "em_open": ("*", "*"),
                "s_open": ("~~", "~~"),
                "link_open": ("[", f"]({token.attrGet('href')})"),
            }[token.type]
            stack.append(pair)
            continue
        if token.nesting == -1:
            stack.pop()
            continue
        prefix = "".join(p[0] for p in stack)
        suffix = "".join(p[1] for p in reversed(stack))
        value = _escape(token.content)
        if token.type == "code_inline":
            fence = "`" * max(
                1, max((len(s) + 1 for s in re.findall(r"`+", token.content)), default=1)
            )
            prefix += fence + " "
            suffix = " " + fence + suffix
            value = token.content
        elif token.type in {"softbreak", "hardbreak"}:
            value = "\n"
        elif token.type == "image":
            prefix += "!["
            suffix = f"]({token.attrGet('src')})" + suffix
            if page and weight + 2 >= budget:
                yield page
                page, weight = "", 0
            weight += 2
        if len(prefix + suffix) >= limit - 2:
            # A URL/delimiter larger than a message cannot be a single Slack link.
            value = prefix + value + suffix
            prefix = suffix = ""
        for part in _slices(value, limit - len(prefix + suffix)):
            core = part.strip()
            leading = part[: len(part) - len(part.lstrip())]
            trailing = part[len(part.rstrip()) :]
            wrapped = leading + prefix + core + suffix + trailing if core else part
            if page and len(page + wrapped) > limit:
                yield page
                page = ""
                weight = 2 if token.type == "image" else 0
            page += wrapped
    if page:
        yield page


def _large(source, limit, budget):
    tokens = MD.parse(source)  # Source maps must be relative to this block.
    first = tokens[0]
    lines = source.splitlines(keepends=True)
    if first.type in {"fence", "code_block"}:
        marker = "`" * max(
            3, max((len(s) + 1 for s in re.findall(r"`+", first.content)), default=3)
        )
        info = first.info if len(first.info) <= 32 else ""
        opening = marker + info + "\n"
        for piece in _slices(first.content, limit - len(opening) - len(marker) - 1, lines=True):
            yield opening + piece + ("" if piece.endswith("\n") else "\n") + marker
    elif first.type == "table_open":
        header = "".join(lines[:2])
        page, rows = header, 0
        for row in lines[2:]:
            if rows and (len(page + row) > limit or rows == 99):
                yield page.rstrip()
                page, rows = header, 0
            if len(header + row) > limit:
                # Keep an oversized row readable rather than cutting its pipes in half.
                yield from _inline(row, limit)
                continue
            page += row
            rows += 1
        if rows or not lines[2:]:
            yield page.rstrip()
    elif first.type in {"bullet_list_open", "ordered_list_open"}:
        items = [t for t in tokens if t.type == "list_item_open" and t.level == 1]
        if len(items) > 1:
            for item in items:
                yield from markdown_chunks("".join(lines[slice(*item.map)]).rstrip(), limit, budget)
        else:
            match = re.match(r"(\s*(?:[-+*]|\d+[.)])\s+)(.*)", source, re.DOTALL)
            prefix, body = match.groups()
            width = len(prefix)
            body = re.sub(rf"(?m)^ {{1,{width}}}", "", body)
            for piece in markdown_chunks(body, (limit - width) // (width + 1), budget):
                yield prefix + piece.replace("\n", "\n" + " " * width)
    elif first.type == "blockquote_open":
        body = re.sub(r"(?m)^ {0,3}> ?", "", source)
        for piece in markdown_chunks(body, (limit - 2) // 3, budget - 1):
            yield "> " + piece.replace("\n", "\n> ")
    else:
        prefix = "#" * int(first.tag[1:]) + " " if first.type == "heading_open" else ""
        inline = next((t.content for t in tokens if t.type == "inline"), source)
        for piece in _inline(inline, limit - len(prefix), budget):
            yield prefix + piece


def _units(text, limit, budget):
    tokens = MD.parse(text)
    large_table = any(t.type == "table_open" and t.map[1] - t.map[0] > 101 for t in tokens)
    if len(text) <= limit and _cost(tokens) <= budget and not large_table:
        yield text
        return
    starts = [i for i, t in enumerate(tokens) if t.level == 0 and t.map]
    lines = text.splitlines(keepends=True)
    for n, start in enumerate(starts):
        group = tokens[start : starts[n + 1] if n + 1 < len(starts) else len(tokens)]
        block = "".join(lines[slice(*group[0].map)]).rstrip()
        weight = max(1, _cost(group))
        large_table = group[0].type == "table_open" and len(block.splitlines()) > 101
        if len(block) > limit or weight > budget or large_table:
            yield from _large(block, limit, budget)
        else:
            yield block
    if not starts and text:
        yield from _slices(text, limit)


def markdown_chunks(text, limit=3900, budget=BLOCK_BUDGET):
    page = ""
    for block in _units(text, limit, budget):
        combined = page + "\n\n" + block if page else block
        if page and (len(combined) > limit or _cost(MD.parse(combined)) > budget):
            yield page
            page = block
        else:
            page = combined
    if page:
        yield page

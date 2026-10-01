"""Convert a Markdown subset to Atlassian Document Format (ADF).

JIRA Cloud's v3 API takes ADF for comment and description bodies and
renders no Markdown, so text written with code fences, lists or bold
would otherwise show its markup literally.

Supported:
  blocks  -- paragraphs (single newlines become hard breaks), fenced code
             blocks with an optional language, "> " block quotes, "#"
             headings, "-"/"*" bullet and "1." ordered lists (one level,
             with indented continuation lines), "---" rules, and pipe
             tables whose second row is a |---| separator
  inline  -- `code`, **bold**, *italic*, [text](url), bare http(s) URLs,
             and @[Display Name] mentions when a resolver is given

Plain text with none of this markup converts exactly as before: one
paragraph per blank-line-separated block, with hard breaks inside.
"""

import re
from collections.abc import Callable
from typing import Any

Node = dict[str, Any]
MentionResolver = Callable[[str], str]

_FENCE = re.compile(r"^\s*(```|~~~)\s*([\w+-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s{0,3}[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s{0,3}(\d{1,9})[.)]\s+(.*)$")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")

# Inline tokens, tried left to right; the earliest match wins.
# Emphasis opens only at the start of a word, so globs such as osc.*.stats
# and llite.*.max_cached_mb stay text.
_OPEN = r"(?<![^\s(\[{\"'/-])"
_INLINE = re.compile(
    r"\\(?P<esc>[\\`*\[\]_~@#>+.-])"
    r"|(?P<code>`+)(?P<code_text>.+?)(?P=code)"
    r"|\[(?P<link_text>[^\]]+)\]\((?P<link_url>[^)\s]+)\)"
    r"|@\[(?P<mention>[^\]]+)\]"
    r"|" + _OPEN + r"\*\*\*(?P<bold_em>\S(?:.*?\S)?)\*\*\*(?!\w)"
    r"|" + _OPEN + r"\*\*(?P<bold>\S(?:.*?\S)?)\*\*(?!\w)"
    r"|" + _OPEN + r"\*(?P<em>[^\s*](?:[^*]*?[^\s*])?)\*(?![\w*])"
    r"|(?P<url>https?://[^\s<>()]+[^\s<>().,;:!?'\"])"
)


def markdown_to_adf(text: str, resolve_mention: MentionResolver | None = None) -> Node:
    """Return an ADF document for *text*."""
    content = _blocks(text.split("\n"), resolve_mention)
    if not content:
        content = [{"type": "paragraph", "content": [{"type": "text", "text": ""}]}]
    return {"version": 1, "type": "doc", "content": content}


def _is_block_start(line: str) -> bool:
    return bool(
        _FENCE.match(line) or _HEADING.match(line) or _BULLET.match(line)
        or _ORDERED.match(line) or _RULE.match(line) or _QUOTE.match(line)
    )


def _blocks(lines: list[str], resolve: MentionResolver | None) -> list[Node]:
    out: list[Node] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        m = _FENCE.match(line)
        if m:
            fence, lang = m.group(1), m.group(2)
            body: list[str] = []
            i += 1
            while i < n and lines[i].strip() != fence:
                body.append(lines[i])
                i += 1
            i += 1  # closing fence, or end of text
            node: Node = {"type": "codeBlock"}
            if lang:
                node["attrs"] = {"language": lang}
            code = "\n".join(body)
            if code:
                node["content"] = [{"type": "text", "text": code}]
            out.append(node)
            continue

        m = _HEADING.match(line)
        if m:
            out.append({
                "type": "heading",
                "attrs": {"level": len(m.group(1))},
                "content": _inline(m.group(2), resolve),
            })
            i += 1
            continue

        if _RULE.match(line):
            out.append({"type": "rule"})
            i += 1
            continue

        if _QUOTE.match(line):
            quoted = []
            while i < n and _QUOTE.match(lines[i]):
                quoted.append(_QUOTE.match(lines[i]).group(1))
                i += 1
            inner = _blocks(quoted, resolve)
            if inner:
                out.append({"type": "blockquote", "content": inner})
            continue

        if _BULLET.match(line) or _ORDERED.match(line):
            node, i = _list(lines, i, resolve)
            out.append(node)
            continue

        if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            node, i = _table(lines, i, resolve)
            out.append(node)
            continue

        para = []
        while i < n and lines[i].strip() and (not para or not _is_block_start(lines[i])):
            para.append(lines[i])
            i += 1
        out.append({"type": "paragraph", "content": _paragraph_inline(para, resolve)})
    return out


def _paragraph_inline(lines: list[str], resolve: MentionResolver | None) -> list[Node]:
    content: list[Node] = []
    for k, line in enumerate(lines):
        content.extend(_inline(line, resolve))
        if k < len(lines) - 1:
            content.append({"type": "hardBreak"})
    return content


def _list(lines: list[str], i: int, resolve: MentionResolver | None) -> tuple[Node, int]:
    ordered = bool(_ORDERED.match(lines[i]))
    item_re = _ORDERED if ordered else _BULLET
    start = int(_ORDERED.match(lines[i]).group(1)) if ordered else 1
    items: list[list[str]] = []
    width = 0
    n = len(lines)
    while i < n:
        line = lines[i].expandtabs(4)
        indent = len(line) - len(line.lstrip())
        # Lines indented to the item's text belong to it (continuation text
        # or a nested list); a blank line ends the list unless an indented
        # line follows.
        if items and line.strip() and indent >= width:
            items[-1].append(line[width:])
            i += 1
            continue
        m = item_re.match(lines[i])
        if m:
            items.append([m.group(m.lastindex)])
            width = m.start(m.lastindex)
            i += 1
            continue
        if not items:
            break
        if line[:1] == " " and line.strip():
            items[-1].append(line[indent:])
            i += 1
        elif not line.strip() and i + 1 < n and lines[i + 1][:1] in (" ", "\t") \
                and lines[i + 1].strip():
            items[-1].append("")
            i += 1
        else:
            break
    node: Node = {
        "type": "orderedList" if ordered else "bulletList",
        "content": [
            {"type": "listItem",
             "content": _blocks(item, resolve) or [{"type": "paragraph", "content": []}]}
            for item in items
        ],
    }
    if ordered:
        node["attrs"] = {"order": start}
    return node, i


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _table(lines: list[str], i: int, resolve: MentionResolver | None) -> tuple[Node, int]:
    def row(cells: list[str], cell_type: str) -> Node:
        return {
            "type": "tableRow",
            "content": [
                {"type": cell_type,
                 "content": [{"type": "paragraph", "content": _inline(c, resolve)}]}
                for c in cells
            ],
        }

    rows = [row(_cells(lines[i]), "tableHeader")]
    i += 2
    while i < len(lines) and "|" in lines[i] and lines[i].strip():
        rows.append(row(_cells(lines[i]), "tableCell"))
        i += 1
    return {
        "type": "table",
        "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
        "content": rows,
    }, i


def _text(text: str, marks: list[Node] | None = None) -> Node:
    node: Node = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    return node


def _inline(text: str, resolve: MentionResolver | None, marks: list[Node] | None = None) -> list[Node]:
    """Inline nodes for one line; ADF text nodes may not be empty."""
    marks = marks or []
    out: list[Node] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.group("mention") is not None and resolve is None:
            continue
        if m.start() > pos:
            out.append(_text(text[pos:m.start()], marks))
        if m.group("esc") is not None:
            out.append(_text(m.group("esc"), marks))
        elif m.group("code") is not None:
            # code may only be combined with link marks
            out.append(_text(m.group("code_text").strip() or m.group("code_text"),
                             [k for k in marks if k["type"] == "link"] + [{"type": "code"}]))
        elif m.group("link_text") is not None:
            out.extend(_inline(m.group("link_text"), resolve,
                               marks + [{"type": "link", "attrs": {"href": m.group("link_url")}}]))
        elif m.group("mention") is not None:
            name = m.group("mention")
            out.append({"type": "mention",
                        "attrs": {"id": resolve(name), "text": f"@{name}"}})
        elif m.group("bold_em") is not None:
            out.extend(_inline(m.group("bold_em"), resolve,
                               marks + [{"type": "strong"}, {"type": "em"}]))
        elif m.group("bold") is not None:
            out.extend(_inline(m.group("bold"), resolve, marks + [{"type": "strong"}]))
        elif m.group("em") is not None:
            out.extend(_inline(m.group("em"), resolve, marks + [{"type": "em"}]))
        else:
            url = m.group("url")
            out.append(_text(url, marks + [{"type": "link", "attrs": {"href": url}}]))
        pos = m.end()
    if pos < len(text):
        out.append(_text(text[pos:], marks))
    return _merge(out)


def _merge(nodes: list[Node]) -> list[Node]:
    """Join adjacent text nodes with the same marks (escapes split them)."""
    out: list[Node] = []
    for node in nodes:
        prev = out[-1] if out else None
        if (prev and node["type"] == "text" and prev["type"] == "text"
                and prev.get("marks") == node.get("marks")):
            out[-1] = _text(prev["text"] + node["text"], node.get("marks"))
        else:
            out.append(node)
    return out


def adf_to_markdown(adf: Any) -> Any:
    """Render an ADF document as Markdown, the same subset markdown_to_adf reads.

    Strings and None are returned unchanged (Server returns plain text).
    Nodes this does not know are rendered as their text content.
    """
    if adf is None or isinstance(adf, str):
        return adf
    if not isinstance(adf, dict) or adf.get("type") != "doc":
        return str(adf)
    text = _render_blocks(adf.get("content", []))
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def _render_blocks(nodes: list[Node]) -> str:
    return "\n\n".join(b for b in (_render_block(n) for n in nodes) if b)


def _prefix_lines(text: str, first: str, rest: str) -> str:
    lines = text.split("\n")
    return "\n".join([first + lines[0]] + [(rest + ln) if ln else rest.rstrip() for ln in lines[1:]])


def _render_block(node: Node) -> str:
    t = node.get("type", "")
    attrs = node.get("attrs") or {}
    content = node.get("content") or []

    if t == "paragraph":
        return "\n".join(_escape_block_start(ln) for ln in _render_inline(content).split("\n"))
    if t == "heading":
        return "#" * attrs.get("level", 1) + " " + _render_inline(content)
    if t == "codeBlock":
        lang = attrs.get("language") or ""
        code = "".join(c.get("text", "") for c in content)
        return f"```{lang}\n{code}\n```"
    if t == "blockquote":
        return _prefix_lines(_render_blocks(content), "> ", "> ")
    if t in ("bulletList", "orderedList", "taskList"):
        start = attrs.get("order", 1)
        items = []
        for k, item in enumerate(content):
            if t == "orderedList":
                marker = f"{start + k}. "
            elif t == "taskList":
                marker = "- [x] " if (item.get("attrs") or {}).get("state") == "DONE" else "- [ ] "
            else:
                marker = "- "
            body = (_render_inline(item.get("content") or []) if item.get("type") == "taskItem"
                    else _render_blocks(item.get("content") or []).replace("\n\n", "\n"))
            items.append(_prefix_lines(body, marker, " " * len(marker)))
        return "\n".join(items)
    if t == "table":
        rows = []
        for r, row in enumerate(content):
            cells = [_render_blocks(c.get("content") or []).replace("\n", " ").replace("|", "\\|")
                     for c in row.get("content") or []]
            rows.append("| " + " | ".join(cells) + " |")
            if r == 0:
                rows.append("|" + "|".join("---" for _ in cells) + "|")
        return "\n".join(rows)
    if t == "rule":
        return "---"
    if t == "panel":
        inner = _render_blocks(content)
        return _prefix_lines(f"**{attrs.get('panelType', 'info')}:** " + inner, "> ", "> ")
    if t in ("expand", "nestedExpand"):
        title = attrs.get("title") or ""
        inner = _render_blocks(content)
        return f"**{title}**\n\n{inner}" if title else inner
    if t in ("mediaSingle", "mediaGroup"):
        return " ".join(_render_media(m) for m in content)
    if t == "media":
        return _render_media(node)
    # unknown block: try as blocks, then as inline
    if content and all(isinstance(c, dict) and c.get("type") != "text" for c in content):
        return _render_blocks(content)
    return _render_inline(content)


def _escape_block_start(line: str) -> str:
    """Keep a paragraph line that looks like a list, quote, etc. as text."""
    if not _is_block_start(line):
        return line
    m = _ORDERED.match(line)
    if m:
        k = m.end(1)
        return line[:k] + "\\" + line[k:]
    k = len(line) - len(line.lstrip())
    return line[:k] + "\\" + line[k:]


def _render_media(node: Node) -> str:
    alt = (node.get("attrs") or {}).get("alt")
    return f"[media: {alt}]" if alt else "[media]"


def _render_inline(nodes: list[Node]) -> str:
    out = []
    for n in nodes:
        t = n.get("type", "")
        attrs = n.get("attrs") or {}
        if t == "text":
            out.append(_render_text(n))
        elif t == "hardBreak":
            out.append("\n")
        elif t == "mention":
            out.append("@[" + (attrs.get("text") or "unknown").lstrip("@") + "]")
        elif t == "emoji":
            out.append(attrs.get("text") or attrs.get("shortName", ""))
        elif t in ("inlineCard", "blockCard", "embedCard"):
            out.append(attrs.get("url", ""))
        elif t == "status":
            out.append(f"[{attrs.get('text', '')}]")
        elif t == "date":
            out.append(str(attrs.get("timestamp", "")))
        elif t == "media" or t == "mediaInline":
            out.append(_render_media(n))
        else:
            out.append(_render_inline(n.get("content") or []))
    return "".join(out)


_ESCAPE = re.compile(r"([\\`*\[\]@])")


def _escape(text: str) -> str:
    """Escape *text* only where it would otherwise parse as markup."""
    if not text or _inline(text, lambda name: "") == [_text(text)]:
        return text
    return _ESCAPE.sub(r"\\\1", text)


def _render_text(node: Node) -> str:
    raw = node.get("text", "")
    marks = {m.get("type"): m.get("attrs") or {} for m in node.get("marks") or []}
    if "code" in marks:
        tick = "``" if "`" in raw else "`"
        text = f"{tick}{raw}{tick}"
    elif not raw.strip():
        text = raw
    else:
        # markers must touch the text: "**a **" would not parse back
        core = raw.strip()
        lead = raw[:len(raw) - len(raw.lstrip())]
        trail = raw[len(raw.rstrip()):]
        text = _escape(core)
        if core:
            if "strong" in marks:
                text = f"**{text}**"
            if "em" in marks:
                text = f"*{text}*"
            if "strike" in marks:
                text = f"~~{text}~~"
        text = lead + text + trail
    if "link" in marks:
        href = marks["link"].get("href", "")
        if href and href != raw:
            text = f"[{text}]({href})"
    return text

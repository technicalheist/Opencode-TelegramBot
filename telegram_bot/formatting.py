from __future__ import annotations

import re

_FENCED_RE = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(((?:https?://|mailto:)[^\s)]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*&gt;")
_LIST_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")


def escape_html(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _placeholder(index: int) -> str:
    return f"\x00{index}\x00"


def _escape_href(url: str) -> str:
    return url.replace('"', "&quot;")


def _code_block_html(info: str, content: str) -> str:
    language = (info or "").strip().split()[0] if (info or "").strip() else ""
    escaped = escape_html(content.rstrip("\n"))
    if language:
        return (
            f'<pre><code class="language-{escape_html(language)}">'
            f"{escaped}</code></pre>"
        )
    return f"<pre><code>{escaped}</code></pre>"


def _convert_blocks(text: str) -> str:
    lines = text.split("\n")
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        heading = _HEADING_RE.match(line)
        if heading:
            output.append(f"<b>{heading.group(2)}</b>")
            index += 1
            continue
        if _QUOTE_RE.match(line):
            quote_lines: list[str] = []
            while index < len(lines) and _QUOTE_RE.match(lines[index]):
                quote_lines.append(re.sub(r"^\s*&gt;\s?", "", lines[index]))
                index += 1
            output.append("<blockquote>" + "\n".join(quote_lines) + "</blockquote>")
            continue
        list_match = _LIST_RE.match(line)
        if list_match:
            output.append(f"{list_match.group(1)}• {list_match.group(2)}")
            index += 1
            continue
        output.append(line)
        index += 1
    return "\n".join(output)


def _convert_inline(text: str) -> str:
    text = _LINK_RE.sub(
        lambda match: (
            f'<a href="{_escape_href(match.group(2))}">{match.group(1)}</a>'
        ),
        text,
    )
    text = re.sub(r"\*\*([^\n]+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__([^\n]+?)__", r"<b>\1</b>", text)
    text = re.sub(r"~~([^\n]+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", text)
    return text


def markdown_to_telegram_html(text: str) -> str:
    if not text:
        return ""
    placeholders: list[str] = []

    def stash(html: str) -> str:
        placeholders.append(html)
        return _placeholder(len(placeholders) - 1)

    working = _FENCED_RE.sub(
        lambda match: stash(_code_block_html(match.group(1), match.group(2))), text
    )
    working = _INLINE_CODE_RE.sub(
        lambda match: stash(f"<code>{escape_html(match.group(1))}</code>"),
        working,
    )
    working = escape_html(working)
    working = _convert_blocks(working)
    working = _convert_inline(working)
    for index, html in enumerate(placeholders):
        working = working.replace(_placeholder(index), html)
    return working


def markdown_to_plain(text: str) -> str:
    if not text:
        return ""
    result = _FENCED_RE.sub(lambda match: match.group(2), text)
    result = re.sub(r"```[^\n]*\n?", "", result)
    result = _INLINE_CODE_RE.sub(lambda match: match.group(1), result)
    result = result.replace("`", "")
    result = _LINK_RE.sub(lambda match: match.group(1), result)
    result = re.sub(r"(?m)^\s*#{1,6}\s+", "", result)
    result = re.sub(r"(?m)^\s*>\s?", "", result)
    result = re.sub(r"(?m)^(\s*)[-*+]\s+", r"\1• ", result)
    result = re.sub(r"\*\*([^\n]+?)\*\*", r"\1", result)
    result = re.sub(r"__([^\n]+?)__", r"\1", result)
    result = re.sub(r"~~([^\n]+?)~~", r"\1", result)
    result = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"\1", result)
    result = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"\1", result)
    result = _HTML_TAG_RE.sub("", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()

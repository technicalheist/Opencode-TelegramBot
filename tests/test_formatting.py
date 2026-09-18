from __future__ import annotations

from telegram_bot.formatting import (
    escape_html,
    markdown_to_plain,
    markdown_to_telegram_html,
)


def test_escape_html_escapes_ampersand_first():
    assert escape_html("a & b") == "a &amp; b"
    assert escape_html("<script>") == "&lt;script&gt;"
    assert escape_html("x > y") == "x &gt; y"


def test_raw_tags_are_escaped():
    output = markdown_to_telegram_html("<script>alert(1)</script>")
    assert "<script>" not in output
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in output


def test_bold_italic_strike():
    assert markdown_to_telegram_html("**bold**") == "<b>bold</b>"
    assert markdown_to_telegram_html("__bold__") == "<b>bold</b>"
    assert markdown_to_telegram_html("*italic*") == "<i>italic</i>"
    assert markdown_to_telegram_html("~~gone~~") == "<s>gone</s>"


def test_heading_bullet_and_numbered_list():
    assert markdown_to_telegram_html("## Title") == "<b>Title</b>"
    assert markdown_to_telegram_html("- item") == "• item"
    assert markdown_to_telegram_html("* item") == "• item"
    assert markdown_to_telegram_html("1. first") == "1. first"


def test_blockquote_groups_consecutive_lines():
    output = markdown_to_telegram_html("> one\n> two")
    assert output == "<blockquote>one\ntwo</blockquote>"


def test_link_conversion():
    assert (
        markdown_to_telegram_html("[docs](https://x.test/a)")
        == '<a href="https://x.test/a">docs</a>'
    )
    assert (
        markdown_to_telegram_html("[mail](mailto:a@b.test)")
        == '<a href="mailto:a@b.test">mail</a>'
    )


def test_fenced_code_with_language_is_escaped_and_not_formatted():
    output = markdown_to_telegram_html("```python\nprint('<hi>')\n```")
    assert '<pre><code class="language-python">' in output
    assert "&lt;hi&gt;" in output
    assert "<hi>" not in output
    assert "<b>" not in output


def test_fenced_code_without_language():
    output = markdown_to_telegram_html("```\nplain **text**\n```")
    assert output == "<pre><code>plain **text**</code></pre>"


def test_inline_code_is_not_formatted_inside():
    assert markdown_to_telegram_html("`**not bold**`") == "<code>**not bold**</code>"


def test_mixed_text_inline_code_and_bold():
    output = markdown_to_telegram_html("use `pip install` and **now**")
    assert "<code>pip install</code>" in output
    assert "<b>now</b>" in output
    assert "**" not in output


def test_unclosed_markers_left_literal():
    assert markdown_to_telegram_html("**unclosed") == "**unclosed"
    assert markdown_to_telegram_html("*italic") == "*italic"


def test_markdown_to_plain_strips_markers_and_tags():
    plain = markdown_to_plain(
        "# Title\n\n**bold** and `code` and ~~gone~~\n\n> quote\n\n"
        "[docs](https://x.test) <b>t</b>"
    )
    assert "#" not in plain
    assert "**" not in plain
    assert "`" not in plain
    assert "~~" not in plain
    assert "<" not in plain
    assert "Title" in plain
    assert "bold" in plain
    assert "code" in plain
    assert "docs" in plain


def test_markdown_to_plain_keeps_code_text():
    assert markdown_to_plain("```python\nprint('x')\n```") == "print('x')"
    assert markdown_to_plain("`pip install`") == "pip install"


def test_markdown_to_plain_collapses_blank_lines():
    assert markdown_to_plain("a\n\n\n\nb") == "a\n\nb"

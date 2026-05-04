"""Tests for sanitize_clipboard_paste."""

import docdb_studio

sanitize_clipboard_paste = docdb_studio.sanitize_clipboard_paste


def test_empty_input_returns_empty_string() -> None:
    assert sanitize_clipboard_paste("") == ""


def test_strips_zero_width_space() -> None:
    # U+200B is a Cf (format) character; it should be removed.
    assert sanitize_clipboard_paste("hello​world") == "helloworld"


def test_preserves_newlines_and_tabs() -> None:
    result = sanitize_clipboard_paste("line1\nline2\tindented")
    assert result == "line1\nline2\tindented"


def test_strips_other_control_chars() -> None:
    # U+0007 BEL is Cc; should be removed.
    assert sanitize_clipboard_paste("foo\x07bar") == "foobar"


def test_normalises_to_nfc() -> None:
    # 'é' decomposed (e + combining acute) should normalise to a single codepoint.
    decomposed = "é"
    out = sanitize_clipboard_paste(decomposed)
    assert out == "é"


def test_preserves_printable_unicode() -> None:
    result = sanitize_clipboard_paste("héllo — 世界 🌍")
    assert result == "héllo — 世界 🌍"

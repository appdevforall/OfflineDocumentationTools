"""Tests for sanitize_text — the input-validation choke-point that rejects
non-printing / format / surrogate / private-use codepoints, while allowing
printable Unicode (NFC-normalized)."""

import pytest

import docdb_studio

sanitize_text = docdb_studio.sanitize_text


def test_plain_ascii_passes_through_unchanged():
    assert sanitize_text("hello world", field="tag") == "hello world"


def test_printable_unicode_is_allowed():
    assert sanitize_text("café — naïve", field="summary") == "café — naïve"
    assert sanitize_text("中文 日本語", field="detail") == "中文 日本語"


def test_nfc_normalization_collapses_decomposed_accents():
    decomposed = "café"  # 'e' + combining acute
    out = sanitize_text(decomposed, field="tag")
    assert out == "café"
    assert len(out) == 4


def test_zero_width_space_is_rejected():
    with pytest.raises(ValueError, match="ZERO WIDTH SPACE"):
        sanitize_text("hi​there", field="tag")


def test_bom_is_rejected():
    with pytest.raises(ValueError, match="ZERO WIDTH NO-BREAK SPACE|BYTE ORDER MARK"):
        sanitize_text("﻿hello", field="summary")


def test_rtl_override_is_rejected():
    with pytest.raises(ValueError, match="RIGHT-TO-LEFT OVERRIDE"):
        sanitize_text("safe‮gnp.exe", field="button uri")


def test_null_byte_is_rejected():
    with pytest.raises(ValueError, match="U\\+0000"):
        sanitize_text("hello\x00world", field="tag")


def test_carriage_return_is_rejected_even_when_newline_allowed():
    with pytest.raises(ValueError, match="U\\+000D"):
        sanitize_text("line1\r\nline2", field="detail", allow_newline=True)


def test_newline_rejected_by_default():
    with pytest.raises(ValueError, match="U\\+000A"):
        sanitize_text("line1\nline2", field="tag")


def test_newline_allowed_when_flag_set():
    out = sanitize_text("line1\nline2", field="detail", allow_newline=True)
    assert out == "line1\nline2"


def test_tab_is_rejected():
    with pytest.raises(ValueError, match="U\\+0009"):
        sanitize_text("col1\tcol2", field="summary")


def test_lone_surrogate_is_rejected():
    # surrogateescape can produce these from undecodable filesystem bytes
    with pytest.raises(ValueError, match="U\\+DC80"):
        sanitize_text("a\udc80b", field="path")


def test_field_name_appears_in_error_message():
    with pytest.raises(ValueError, match="^button uri:"):
        sanitize_text("bad​", field="button uri")


def test_offset_is_reported_in_error_message():
    with pytest.raises(ValueError, match="at offset 5"):
        sanitize_text("clean​dirty", field="tag")


def test_empty_string_is_fine():
    assert sanitize_text("", field="tag") == ""

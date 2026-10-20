#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
#     "pikepdf>=10",
#     "pytest",
# ]
# ///
"""
test_optimize_pdfs.py

Covers optimize_pdfs.py: what the rewrite changes in a PDF (and what it must
leave alone), the checks that refuse a bad rewrite, and whole runs over real
SQLite databases (Bookshelf triggers, chunked rows, dry run, rerun, rows that
are not Brotli, the database's own dictionary) and over files.

Run it either way:

    uv run scripts/test_optimize_pdfs.py
    uv run --with brotli --with Pillow --with pikepdf --with pytest pytest scripts/test_optimize_pdfs.py
"""
from __future__ import annotations

import io
import random
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
import zlib
from contextlib import redirect_stdout
from pathlib import Path

import brotli
import pikepdf
from pikepdf import Array, Dictionary, Name
from PIL import Image

import optimize_pdfs as tool  # noqa: E402
from optimize_db_media import CHUNK_SIZE, BrotliCodec, reassemble  # noqa: E402
from update_media_references import replace_row  # noqa: E402

CONTENT = (b"BT /F1 12 Tf 72 720 Td (" + b"Hello, offline world. " * 40 + b") Tj ET\n"
           + b"q 64 0 0 64 72 72 cm /Rgb Do Q\n" * 8)


def gradient(mode: str, size=(64, 48)) -> Image.Image:
    """Smooth samples: PNG predictors shrink these a lot against Flate with none."""
    img = Image.new("RGB", size)
    img.putdata([(x * 4 % 256, y * 5 % 256, (x + y) * 2 % 256) for y in range(size[1]) for x in range(size[0])])
    return img.convert(mode)


def noisy_rows(width: int = 512, height: int = 64) -> Image.Image:
    """1-bit rows that each repeat the one above with two bits flipped: the Up predictor turns them into zeros."""
    random.seed(9)
    row, rows = bytearray(random.randrange(256) for _ in range(width // 8)), []
    for _ in range(height):
        row = bytearray(row)
        for _ in range(2):
            row[random.randrange(len(row))] ^= 1 << random.randrange(8)
        rows.append(bytes(row))
    return Image.frombytes("1", (width, height), b"".join(rows))


def letters(n: int, seed: int) -> bytes:
    """Random text over 16 letters: repeated, it is a few MB of PDF that Brotli at q11 shrinks to almost nothing."""
    random.seed(seed)
    return bytes(random.choice(b"abcdefghijklmnop") for _ in range(n))


def jpeg_bytes(size=(80, 60)) -> bytes:
    random.seed(3)
    img = Image.new("RGB", size)
    img.putdata([(random.randrange(256), random.randrange(256), 128) for _ in range(size[0] * size[1])])
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue()


def image_stream(pdf: pikepdf.Pdf, img: Image.Image, colour_space, bpc: int, level: int = 6,
                 **extra) -> pikepdf.Stream:
    """An image XObject holding `img`'s samples as Flate with no predictor, as many PDF producers write them."""
    s = pikepdf.Stream(pdf, zlib.compress(img.tobytes(), level))
    s.Type, s.Subtype, s.Width, s.Height = Name.XObject, Name.Image, img.width, img.height
    s.BitsPerComponent = bpc
    if colour_space is not None:
        s.ColorSpace = colour_space
    s.Filter = Name.FlateDecode
    for k, v in extra.items():
        s["/" + k] = v
    return pdf.make_indirect(s)


def sample_pdf(pages: int = 2, extra_content: bytes = b"") -> bytes:
    """A small PDF with each kind of stream the rewrite treats differently, named destinations both ways, and an
    outline with a destination and an action."""
    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(612, 792))
    jpeg = jpeg_bytes()
    images = {
        "/Rgb": image_stream(pdf, gradient("RGB"), Name.DeviceRGB, 8),
        "/Gray": image_stream(pdf, gradient("L"), Name.DeviceGray, 8),
        "/Mask": image_stream(pdf, noisy_rows(), None, 1, level=1, ImageMask=True),  # a fast-Flate producer
        "/Bare": pdf.make_indirect(pikepdf.Stream(pdf, jpeg, Type=Name.XObject, Subtype=Name.Image, Width=80,
                                                  Height=60, BitsPerComponent=8, ColorSpace=Name.DeviceRGB,
                                                  Filter=Name.DCTDecode)),
        "/Wrapped": pdf.make_indirect(pikepdf.Stream(
            pdf, zlib.compress(jpeg, 9), Type=Name.XObject, Subtype=Name.Image, Width=80, Height=60,
            BitsPerComponent=8, ColorSpace=Name.DeviceRGB, Filter=Array([Name.FlateDecode, Name.DCTDecode]),
            DecodeParms=Array([None, Dictionary(ColorTransform=1)]))),
    }
    font_file = pdf.make_indirect(pikepdf.Stream(pdf, zlib.compress(b"\x00\x01fontdata" * 300), Filter=Name.FlateDecode,
                                                 Length1=2700))
    font = Dictionary(Type=Name.Font, Subtype=Name.TrueType, BaseFont=Name.Demo,
                      FontDescriptor=pdf.make_indirect(Dictionary(Type=Name.FontDescriptor, FontName=Name.Demo,
                                                                  FontFile2=font_file)))
    for page in pdf.pages:
        page.obj.Contents = pdf.make_indirect(pikepdf.Stream(pdf, zlib.compress(CONTENT + extra_content, 9),
                                                             Filter=Name.FlateDecode))
        page.obj.Resources = Dictionary(XObject=Dictionary({k: v for k, v in images.items()}),
                                        Font=Dictionary(F1=font))
    first = pdf.pages[0].obj
    pdf.Root.Dests = Dictionary(old=Array([first, Name.Fit]))
    tree = pikepdf.NameTree.new(pdf)
    tree["chapter.1"] = Array([pdf.pages[-1].obj, Name.XYZ, 0, 700, None])
    tree["chapter.2"] = Array([first, Name.FitH, 500])
    pdf.Root.Names = Dictionary(Dests=tree.obj)
    with pdf.open_outline() as o:
        one = pikepdf.OutlineItem("One", 0)
        one.children.append(pikepdf.OutlineItem("One.1", pages - 1))
        o.root.append(one)
        o.root.append(pikepdf.OutlineItem("Two", action=Dictionary(S=Name.GoTo, D=Array([first, Name.Fit]))))
    buf = io.BytesIO()
    pdf.save(buf, compress_streams=False, object_stream_mode=pikepdf.ObjectStreamMode.disable,
             stream_decode_level=pikepdf.StreamDecodeLevel.none)  # keep each stream exactly as built
    return buf.getvalue()


def open_pdf(data: bytes) -> pikepdf.Pdf:
    return pikepdf.open(io.BytesIO(data))


def xobject(pdf: pikepdf.Pdf, name: str) -> pikepdf.Stream:
    return pdf.pages[0].obj.Resources.XObject[name]


# --- the rewrite --------------------------------------------------------------------------------

def test_a_flate_content_stream_and_a_font_are_stored_plain():
    old = sample_pdf()
    new, counts = tool.rewrite_pdf(old)
    with open_pdf(old) as a, open_pdf(new) as b:
        for i in range(2):
            assert tool.filters(b.pages[i].obj.Contents) == []
            assert b.pages[i].obj.Contents.read_raw_bytes() == a.pages[i].obj.Contents.read_bytes()
        font_file = b.pages[0].obj.Resources.Font.F1.FontDescriptor.FontFile2
        assert tool.filters(font_file) == [] and font_file.read_raw_bytes() == b"\x00\x01fontdata" * 300
        assert font_file.Length1 == 2700  # the font's own keys stay
    assert counts["decoded"] >= 3


def test_flate_images_get_png_predictors_and_give_back_the_same_samples():
    old = sample_pdf()
    new, counts = tool.rewrite_pdf(old)
    with open_pdf(old) as a, open_pdf(new) as b:
        for name, colours, bpc in (("/Rgb", 3, 8), ("/Gray", 1, 8), ("/Mask", 1, 1)):
            before, after = xobject(a, name), xobject(b, name)
            assert tool.filters(after) == ["/FlateDecode"], name
            assert after.DecodeParms.Predictor == tool.PNG_OPTIMUM, name
            assert (after.DecodeParms.Colors, after.DecodeParms.BitsPerComponent, after.DecodeParms.Columns) == \
                (colours, bpc, int(before.Width)), name
            assert after.read_bytes() == before.read_bytes(), name  # every sample as it was
            assert len(after.read_raw_bytes()) < len(before.read_raw_bytes()), name
    assert counts["images"] == 3


def test_a_flate_wrapped_jpeg_is_stored_bare_with_its_dct_parameters():
    old = sample_pdf()
    new, counts = tool.rewrite_pdf(old)
    with open_pdf(new) as b:
        wrapped = xobject(b, "/Wrapped")
        assert tool.filters(wrapped) == ["/DCTDecode"]
        assert wrapped.read_raw_bytes() == jpeg_bytes()
        assert wrapped.DecodeParms == Dictionary(ColorTransform=1)
    assert counts["jpegs"] == 1


def test_a_bare_jpeg_is_left_byte_for_byte():
    new, _counts = tool.rewrite_pdf(sample_pdf())
    with open_pdf(new) as b:
        bare = xobject(b, "/Bare")
        assert tool.filters(bare) == ["/DCTDecode"] and bare.read_raw_bytes() == jpeg_bytes()
        assert "/DecodeParms" not in bare


def _with_image(build) -> tuple:
    """(original bytes, rewritten bytes or None, counts or reason) for sample_pdf() plus one extra image `build`
    makes, drawn on the first page as /Extra."""
    pdf = open_pdf(sample_pdf())
    pdf.pages[0].obj.Resources.XObject.Extra = pdf.make_indirect(build(pdf))
    buf = io.BytesIO()
    pdf.save(buf, compress_streams=False, stream_decode_level=pikepdf.StreamDecodeLevel.none)
    _path, new, info = tool.rewrite_job(("x.pdf", buf.getvalue()))
    return buf.getvalue(), new, info


def test_a_wrapped_jpeg_whose_flate_has_a_predictor_is_left_as_it_is():
    jpeg = jpeg_bytes()

    def build(pdf):
        rows = b"\x00" + jpeg  # one row, PNG filter None: the predicted bytes are the JPEG plus a filter byte
        return pikepdf.Stream(pdf, zlib.compress(rows), Type=Name.XObject, Subtype=Name.Image, Width=80, Height=60,
                              BitsPerComponent=8, ColorSpace=Name.DeviceRGB,
                              Filter=Array([Name.FlateDecode, Name.DCTDecode]),
                              DecodeParms=Array([Dictionary(Predictor=10, Columns=len(jpeg)), None]))

    old, new, counts = _with_image(build)
    assert new is not None and counts["jpegs"] == 1  # the other wrapped JPEG still is unwrapped
    with open_pdf(old) as a, open_pdf(new) as b:
        before, after = xobject(a, "/Extra"), xobject(b, "/Extra")
        assert tool.filters(after) == ["/FlateDecode", "/DCTDecode"]
        assert after.read_raw_bytes() == before.read_raw_bytes()


def test_a_wrapped_jpeg_with_one_decode_dictionary_for_two_filters_is_left_as_it_is():
    # Malformed: readers differ on which filter the dictionary is for, so its meaning cannot be kept.
    def build(pdf):
        return pikepdf.Stream(pdf, zlib.compress(jpeg_bytes()), Type=Name.XObject, Subtype=Name.Image, Width=80,
                              Height=60, BitsPerComponent=8, ColorSpace=Name.DeviceRGB,
                              Filter=Array([Name.FlateDecode, Name.DCTDecode]),
                              DecodeParms=Dictionary(ColorTransform=0))

    old, new, counts = _with_image(build)
    assert new is not None and counts["jpegs"] == 1
    with open_pdf(new) as b:
        after = xobject(b, "/Extra")
        assert tool.filters(after) == ["/FlateDecode", "/DCTDecode"] and after.DecodeParms.ColorTransform == 0


def test_an_encrypted_pdf_is_left_as_it_is():
    pdf = open_pdf(sample_pdf())
    buf = io.BytesIO()
    pdf.save(buf, encryption=pikepdf.Encryption(owner="owner", user="", allow=pikepdf.Permissions(extract=False)))
    _path, new, reason = tool.rewrite_job(("x.pdf", buf.getvalue()))
    assert new is None and reason == "it is encrypted"


def test_an_image_the_predictor_does_not_shrink_is_left_as_it_is():
    pdf = pikepdf.new()
    pdf.add_blank_page()
    random.seed(11)
    noise = Image.frombytes("L", (64, 64), bytes(random.randrange(256) for _ in range(64 * 64)))
    s = image_stream(pdf, noise, Name.DeviceGray, 8)
    pdf.pages[0].obj.Resources = Dictionary(XObject=Dictionary(N=s))
    raw = s.read_raw_bytes()
    tool.rewrite_objects(pdf)
    assert s.read_raw_bytes() == raw and "/DecodeParms" not in s


def test_named_destinations_and_the_outline_are_kept():
    old = sample_pdf()
    new, _counts = tool.rewrite_pdf(old)
    with open_pdf(new) as b:
        pages = [p.obj.objgen for p in b.pages]
        assert b.Root.Dests.old[0].objgen == pages[0] and b.Root.Dests.old[1] == Name.Fit
        tree = pikepdf.NameTree(b.Root.Names.Dests)
        assert sorted(tree.keys()) == ["chapter.1", "chapter.2"]
        assert tree["chapter.1"][0].objgen == pages[1] and tree["chapter.2"][0].objgen == pages[0]
        with b.open_outline() as o:
            titles = [item.title for item in o.root]
            nested = [item.title for item in o.root[0].children]
        assert titles == ["One", "Two"] and nested == ["One.1"]
        first = b.Root.Outlines.First
        assert first.Dest[0].objgen == pages[0] and first.Next.A.D[0].objgen == pages[0]
        assert first.First.Dest[0].objgen == pages[1]


def test_the_rewrite_is_deterministic_and_a_second_rewrite_changes_nothing():
    old = sample_pdf()
    once, _ = tool.rewrite_pdf(old)
    time.sleep(1.1)  # qpdf's other /ID hashes the time to the second
    again, _ = tool.rewrite_pdf(old)
    assert once == again  # the /ID comes from the content, not the clock
    twice, counts = tool.rewrite_pdf(once)
    assert twice == once and counts == {"decoded": 0, "images": 0, "jpegs": 0}


def test_a_pdf_with_a_stream_qpdf_cannot_decode_cleanly_is_left_as_it_is():
    # Its partial data, stored plain, would pass every check: the step must not quietly repair what a reader gets.
    pdf = open_pdf(sample_pdf())
    pdf.pages[0].obj.Contents.write(zlib.compress(CONTENT, 9)[:-30], filter=Name.FlateDecode)
    buf = io.BytesIO()
    pdf.save(buf, compress_streams=False, stream_decode_level=pikepdf.StreamDecodeLevel.none)
    path, new, reason = tool.rewrite_job(("x.pdf", buf.getvalue()))
    assert new is None and reason.startswith("the syntax check reports WARNING: the PDF")


def test_keep_rewrite_wants_strictly_smaller():
    assert tool.keep_rewrite(10, 9) and not tool.keep_rewrite(10, 10) and not tool.keep_rewrite(10, 11)


# --- the checks: each one refuses the fault it exists for ------------------------------------

def _with_fault(fault, *switch_off):
    """Runs rewrite_pdf with `fault(pdf)` applied after the rewrite, and the named checks replaced by no-ops, so the
    check under test is the only one that can see the fault. Returns the Refused message, or None."""
    real_objects = tool.rewrite_objects
    saved = {name: getattr(tool, name) for name in switch_off}

    def faulty(pdf):
        counts = real_objects(pdf)
        fault(pdf)
        return counts

    tool.rewrite_objects = faulty
    for name in switch_off:
        setattr(tool, name, {"compare_graphs": lambda old, new: None,
                             "named_destinations": lambda pdf, pages: {},
                             "outline": lambda pdf, pages: []}[name])
    try:
        tool.rewrite_pdf(sample_pdf())
    except tool.Refused as exc:
        return str(exc)
    finally:
        tool.rewrite_objects = real_objects
        for name, value in saved.items():
            setattr(tool, name, value)
    return None


def test_the_graph_check_refuses_a_changed_dictionary_value():
    def fault(pdf):
        pdf.pages[0].obj.Resources.Font.F1.BaseFont = Name.Other
    refusal = _with_fault(fault)
    assert refusal is not None and "/BaseFont" in refusal, refusal


def test_the_graph_check_refuses_a_changed_stream_key():
    def fault(pdf):
        pdf.pages[0].obj.Resources.Font.F1.FontDescriptor.FontFile2.Length1 = 1
    refusal = _with_fault(fault)
    assert refusal is not None and "/Length1" in refusal, refusal


def test_the_graph_check_refuses_changed_stream_data():
    def fault(pdf):
        contents = pdf.pages[0].obj.Contents
        contents.write(contents.read_bytes() + b" ")
    refusal = _with_fault(fault)
    assert refusal is not None and "decodes differently" in refusal, refusal


def test_the_graph_check_refuses_changed_image_samples():
    def fault(pdf):
        image = pdf.pages[0].obj.Resources.XObject.Gray
        samples = bytearray(image.read_bytes())
        samples[0] ^= 1
        image.write(zlib.compress(bytes(samples)), filter=Name.FlateDecode)
    refusal = _with_fault(fault)
    assert refusal is not None and "decodes differently" in refusal, refusal


def test_the_graph_check_refuses_a_changed_dct_parameter():
    def fault(pdf):
        pdf.pages[0].obj.Resources.XObject.Wrapped.DecodeParms = Dictionary(ColorTransform=0)
    refusal = _with_fault(fault)
    assert refusal is not None and "decodes differently" in refusal, refusal


def test_the_page_count_check_refuses_a_lost_page():
    def fault(pdf):
        del pdf.pages[-1]
    assert _with_fault(fault, "compare_graphs", "named_destinations", "outline") == "2 pages became 1"


def test_the_named_destination_check_refuses_a_changed_destination():
    def fault(pdf):
        pdf.Root.Dests.old = Array([pdf.pages[1].obj, Name.Fit])
    assert _with_fault(fault, "compare_graphs", "outline") == "the named destinations changed"


def test_the_named_destination_check_reads_the_name_tree():
    def fault(pdf):
        pikepdf.NameTree(pdf.Root.Names.Dests)["chapter.2"] = Array([pdf.pages[1].obj, Name.Fit])
    assert _with_fault(fault, "compare_graphs", "outline") == "the named destinations changed"


def test_the_outline_check_refuses_a_changed_title():
    def fault(pdf):
        pdf.Root.Outlines.First.Title = pikepdf.String("Changed")
    assert _with_fault(fault, "compare_graphs", "named_destinations") == "the outline changed"


def test_the_outline_check_reads_nested_items():
    def fault(pdf):
        pdf.Root.Outlines.First.First.Title = pikepdf.String("Changed")
    assert _with_fault(fault, "compare_graphs", "named_destinations") == "the outline changed"


def test_the_syntax_check_refuses_a_damaged_file():
    real_save = tool.save
    tool.save = lambda pdf: real_save(pdf).replace(b"startxref", b"startxrex")
    try:
        tool.rewrite_pdf(sample_pdf())
    except tool.Refused as exc:
        assert str(exc).startswith("the syntax check reports WARNING: the PDF: ")
    else:
        raise AssertionError("a damaged file was accepted")
    finally:
        tool.save = real_save


# --- whole runs over a database ------------------------------------------------------------------

PDF_ID, HTML_ID, RAW_PDF_ID = 14, 12, 30
BOOK_PATH = "bookshelf/org.appdevforall.bookshelfplugin/Book.pdf"


def _database(tmp_path: Path, rows: dict, dictionary: bytes = None, raw_rows: dict = None,
              fast: bool = False) -> Path:
    """A database with the real Content/Bookshelf schema and its two triggers; every PDF row gets a curated
    Bookshelf entry, as the shipped books have. `fast` stores the rows with Brotli's fastest settings (plain
    databases only), so a few MB of PDF starts out chunked."""
    db_path = tmp_path / "documentation.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE, compression TEXT NOT NULL);"
        "CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, languageID INTEGER NOT NULL, "
        "content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, templateId INTEGER NOT NULL DEFAULT 0, UNIQUE(path));"
        "CREATE TABLE Bookshelf (contentID INTEGER NOT NULL, title STRING DEFAULT '', description STRING DEFAULT '', "
        "bookCategoryID INTEGER, UNIQUE(title, bookCategoryId));"
    )
    conn.execute("INSERT INTO ContentTypes VALUES (?, 'application/pdf', 'brotli'), (?, 'text/html', 'brotli'), "
                 "(?, 'application/x-raw-pdf', 'none')", (PDF_ID, HTML_ID, RAW_PDF_ID))
    if dictionary is not None:
        conn.execute("CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
        conn.execute("INSERT INTO CompressionDictionary VALUES (1, ?)", (dictionary,))
    codec = BrotliCodec(dictionary)
    for path, data in rows.items():
        type_id = HTML_ID if path.endswith(".html") else PDF_ID
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, 1, x'00', ?, 7)", (path, type_id))
        stored = brotli.compress(data, quality=0, lgwin=10) if fast else codec.compress(data, "brotli")
        replace_row(conn, path, 1, stored, 1, type_id, 7, lambda m: None)
    for path, data in (raw_rows or {}).items():
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, 1, ?, ?)",
                     (path, data, RAW_PDF_ID))
    codec.close()
    conn.executescript(
        "CREATE TRIGGER AddBook AFTER INSERT ON Content WHEN NEW.path LIKE '%.pdf' BEGIN "
        "INSERT INTO Bookshelf (contentID, title) VALUES (NEW.id, CURRENT_TIMESTAMP || NEW.id); END;"
        "CREATE TRIGGER DeleteBook AFTER DELETE ON Content WHEN OLD.path LIKE '%.pdf' BEGIN "
        "DELETE FROM Bookshelf WHERE contentID = OLD.id; END;"
    )
    for (content_id,) in conn.execute("SELECT id FROM Content WHERE path LIKE '%.pdf'").fetchall():
        conn.execute("INSERT INTO Bookshelf (contentID, title, description, bookCategoryID) VALUES (?, ?, ?, 2)",
                     (content_id, f"Curated {content_id}", "A curated description"))
    conn.commit()
    conn.close()
    return db_path


def _read(db_path: Path, path: str, dictionary: bytes = None) -> bytes:
    conn = sqlite3.connect(db_path)
    codec = BrotliCodec(dictionary)
    try:
        content, comp = conn.execute("SELECT c.content, t.compression FROM Content c JOIN ContentTypes t "
                                     "ON t.id = c.contentTypeID WHERE c.path = ?", (path,)).fetchone()
        return codec.decompress(reassemble(conn, path, content), comp)
    finally:
        codec.close()
        conn.close()


def _rows(db_path: Path) -> list:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT id, path, languageID, contentTypeID, templateId, LENGTH(content) FROM Content "
                            "ORDER BY path").fetchall()
    finally:
        conn.close()


def _bookshelf(db_path: Path) -> list:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT * FROM Bookshelf ORDER BY contentID").fetchall()
    finally:
        conn.close()


def _cfg(db_path: Path, **overrides) -> dict:
    cfg = {"db_path": db_path, "dry_run": False, "workers": 1, "verbose": False}
    cfg.update(overrides)
    return cfg


def _two_rows() -> dict:
    return {BOOK_PATH: sample_pdf(), "d/Guide.pdf": sample_pdf(pages=3), "d/page.html": b"<html>guide</html>"}


def test_end_to_end_rewrites_each_pdf_and_nothing_else(tmp_path: Path):
    rows = _two_rows()
    db_path = _database(tmp_path, rows)
    before = {r[1]: r for r in _rows(db_path)}
    assert tool.run(_cfg(db_path)) == 0
    after = {r[1]: r for r in _rows(db_path)}
    for path in (BOOK_PATH, "d/Guide.pdf"):
        new = _read(db_path, path)
        assert new != rows[path]
        tool.verify(rows[path], new)  # the stored PDF passes every check against the original
        assert after[path][:5] == before[path][:5]  # same id, path, language, type, templateId
        assert after[path][5] < before[path][5]
    assert _read(db_path, "d/page.html") == rows["d/page.html"]
    assert len(list(tmp_path.glob("*.backup-*"))) == 1
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0  # VACUUMed after the rewrite
    conn.close()


def test_the_bookshelf_rows_keep_their_ids_and_curated_titles(tmp_path: Path):
    db_path = _database(tmp_path, _two_rows())
    shelf = _bookshelf(db_path)
    assert len(shelf) == 2 and all(title.startswith("Curated") for _id, title, *_ in shelf)
    assert tool.run(_cfg(db_path)) == 0
    assert _bookshelf(db_path) == shelf


def test_a_chunked_row_is_rejoined_and_its_continuations_trimmed(tmp_path: Path):
    big = sample_pdf(extra_content=b"%" + letters(100_000, 5) * 30 + b"\n")
    db_path = _database(tmp_path, {BOOK_PATH: big}, fast=True)
    assert [r[1] for r in _rows(db_path)][:3] == [BOOK_PATH, f"{BOOK_PATH}-1", f"{BOOK_PATH}-2"]
    shelf = _bookshelf(db_path)
    assert tool.run(_cfg(db_path)) == 0
    assert [r[1] for r in _rows(db_path)] == [BOOK_PATH]
    tool.verify(big, _read(db_path, BOOK_PATH))
    assert _bookshelf(db_path) == shelf


def test_a_row_still_over_a_chunk_is_written_back_in_chunks(tmp_path: Path):
    random.seed(6)
    buf = io.BytesIO()
    # A 1.2 MB JPEG of noise: the rewrite leaves it as it is and no compressor shrinks it.
    Image.frombytes("RGB", (720, 720), random.randbytes(720 * 720 * 3)).save(buf, "JPEG", quality=95, subsampling=0)
    pdf = open_pdf(sample_pdf(extra_content=b"%" + letters(100_000, 5) * 30 + b"\n"))
    pdf.Root.Noise = pdf.make_indirect(pikepdf.Stream(pdf, buf.getvalue(), Filter=Name.DCTDecode, Type=Name.XObject,
                                                      Subtype=Name.Image, Width=720, Height=720,
                                                      BitsPerComponent=8, ColorSpace=Name.DeviceRGB))
    buf = io.BytesIO()
    pdf.save(buf, compress_streams=False, stream_decode_level=pikepdf.StreamDecodeLevel.none)
    big = buf.getvalue()
    db_path = _database(tmp_path, {BOOK_PATH: big}, fast=True)
    assert len(_rows(db_path)) > 3
    assert tool.run(_cfg(db_path)) == 0
    rows = _rows(db_path)
    assert [r[1] for r in rows] == [BOOK_PATH, f"{BOOK_PATH}-1"]
    assert rows[0][5] == CHUNK_SIZE and rows[1][4] == 7  # the continuation keeps the base row's templateId
    tool.verify(big, _read(db_path, BOOK_PATH))


def test_dry_run_writes_nothing(tmp_path: Path):
    db_path = _database(tmp_path, _two_rows())
    before = db_path.read_bytes()
    out = io.StringIO()
    with redirect_stdout(out):
        assert tool.run(_cfg(db_path, dry_run=True)) == 0
    assert "Dry run: nothing written." in out.getvalue()
    assert db_path.read_bytes() == before and not list(tmp_path.glob("*.backup-*"))


def test_a_second_run_changes_nothing(tmp_path: Path):
    db_path = _database(tmp_path, _two_rows())
    assert tool.run(_cfg(db_path)) == 0
    once = db_path.read_bytes()
    out = io.StringIO()
    with redirect_stdout(out):
        assert tool.run(_cfg(db_path)) == 0
    assert "Nothing to do" in out.getvalue()
    assert db_path.read_bytes() == once and len(list(tmp_path.glob("*.backup-*"))) == 1


def test_a_rewrite_that_is_not_smaller_is_left_alone(tmp_path: Path):
    rows = _two_rows()
    db_path = _database(tmp_path, rows)
    before = db_path.read_bytes()
    real = tool.rewrite_pdf

    def padded(data):
        new, counts = real(data)
        # Valid, checked, but larger once compressed: random bytes after %%EOF are ignored by readers.
        return new + bytes(random.randrange(256) for _ in range(len(data))), counts

    tool.rewrite_pdf = padded
    try:
        assert tool.run(_cfg(db_path)) == 0
    finally:
        tool.rewrite_pdf = real
    assert db_path.read_bytes() == before


def test_a_refused_pdf_is_left_as_it_is_and_named(tmp_path: Path):
    rows = _two_rows()
    db_path = _database(tmp_path, rows)
    real = tool.rewrite_objects

    def faulty(pdf):
        counts = real(pdf)
        pdf.pages[0].obj.Resources.Font.F1.BaseFont = Name.Other
        return counts

    tool.rewrite_objects = faulty
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            assert tool.run(_cfg(db_path)) == 0
    finally:
        tool.rewrite_objects = real
    assert f"{BOOK_PATH}: left as it is: /Root/Pages/Kids[0]/Resources/Font/F1/BaseFont" in out.getvalue()
    for path in (BOOK_PATH, "d/Guide.pdf"):
        assert _read(db_path, path) == rows[path]


def test_a_pdf_row_that_is_not_brotli_is_skipped_and_named(tmp_path: Path):
    db_path = _database(tmp_path, {}, raw_rows={"x/raw.pdf": sample_pdf()})
    before = db_path.read_bytes()
    out = io.StringIO()
    with redirect_stdout(out):
        real_type = tool.PDF_TYPE
        tool.PDF_TYPE = "application/x-raw-pdf"
        try:
            assert tool.run(_cfg(db_path)) == 0
        finally:
            tool.PDF_TYPE = real_type
    assert "x/raw.pdf: skipped: stored with compression 'none', not brotli" in out.getvalue()
    assert db_path.read_bytes() == before


def test_an_unreadable_row_stops_the_run_with_nothing_written(tmp_path: Path):
    db_path = _database(tmp_path, _two_rows())
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE Content SET content = x'0102030405' WHERE path = 'd/Guide.pdf'")
    conn.commit()
    conn.close()
    before = db_path.read_bytes()
    assert tool.run(_cfg(db_path)) == 1
    assert db_path.read_bytes() == before


def test_the_process_pool_gives_the_same_result(tmp_path: Path):
    rows = _two_rows()  # one set of rows for both: each sample_pdf() carries its own random /ID
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    one, two = _database(tmp_path / "a", rows), _database(tmp_path / "b", rows)
    assert tool.run(_cfg(one, workers=1)) == 0
    assert tool.run(_cfg(two, workers=3)) == 0
    for path in (BOOK_PATH, "d/Guide.pdf"):
        assert _read(one, path) == _read(two, path)


def test_rows_stay_in_the_database_dictionary(tmp_path: Path):
    if shutil.which("brotli") is None:
        print("  (skipped: no brotli CLI)")
        return
    dictionary = b"/Type /XObject /Subtype /Image /Filter /FlateDecode /DecodeParms BT ET Tj Tf " * 60
    rows = {BOOK_PATH: sample_pdf()}
    db_path = _database(tmp_path, rows, dictionary=dictionary)
    assert tool.run(_cfg(db_path)) == 0
    new = _read(db_path, BOOK_PATH, dictionary)
    tool.verify(rows[BOOK_PATH], new)
    conn = sqlite3.connect(db_path)
    stored = conn.execute("SELECT content FROM Content WHERE path = ?", (BOOK_PATH,)).fetchone()[0]
    conn.close()
    try:
        plain = brotli.decompress(stored)
    except brotli.error:
        plain = None
    assert plain != new  # it needs the dictionary to decode back


# --- file mode -----------------------------------------------------------------------------------

def _file_cfg(source: Path, target: Path, **overrides) -> dict:
    cfg = {"file": (source, target), "dry_run": False}
    cfg.update(overrides)
    return cfg


def test_file_mode_takes_a_pdf(tmp_path: Path):
    source = tmp_path / "Book.pdf"
    source.write_bytes(sample_pdf())
    target = tmp_path / "out" / "Book.pdf.br"
    assert tool.run_file(_file_cfg(source, target)) == 0
    new = brotli.decompress(target.read_bytes())
    tool.verify(source.read_bytes(), new)
    as_shipped = brotli.compress(source.read_bytes(), quality=11, lgwin=24)
    assert len(target.read_bytes()) < len(as_shipped)
    assert target.read_bytes() == brotli.compress(new, quality=11, lgwin=24)


def test_file_mode_takes_a_plain_brotli_pdf_and_may_write_over_it(tmp_path: Path):
    source = tmp_path / "Book.pdf.br"
    source.write_bytes(brotli.compress(sample_pdf(), quality=11, lgwin=24))
    old = source.read_bytes()
    assert tool.run_file(_file_cfg(source, source)) == 0
    new = brotli.decompress(source.read_bytes())
    tool.verify(brotli.decompress(old), new)
    assert len(source.read_bytes()) < len(old)
    assert [p.name for p in tmp_path.iterdir()] == ["Book.pdf.br"]  # no temporary file left behind
    assert tool.run_file(_file_cfg(source, source)) == 0
    assert brotli.decompress(source.read_bytes()) == new  # a second run changes nothing


def test_file_mode_keeps_the_input_when_the_rewrite_is_refused(tmp_path: Path):
    source = tmp_path / "Book.pdf.br"
    source.write_bytes(brotli.compress(sample_pdf(), quality=11, lgwin=24))
    target = tmp_path / "Out.pdf.br"
    real = tool.rewrite_pdf

    def refuse(data):
        raise tool.Refused("a test refusal")

    tool.rewrite_pdf = refuse
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            assert tool.run_file(_file_cfg(source, target)) == 0
    finally:
        tool.rewrite_pdf = real
    assert "left as it is: a test refusal" in out.getvalue()
    assert target.read_bytes() == source.read_bytes()


def test_file_mode_dry_run_writes_nothing(tmp_path: Path):
    source = tmp_path / "Book.pdf"
    source.write_bytes(sample_pdf())
    target = tmp_path / "Book.pdf.br"
    assert tool.run_file(_file_cfg(source, target, dry_run=True)) == 0
    assert not target.exists()


def test_file_mode_refuses_a_target_that_is_not_pdf_br(tmp_path: Path):
    source = tmp_path / "Book.pdf"
    source.write_bytes(sample_pdf())
    assert tool.run_file(_file_cfg(source, tmp_path / "Book.br")) == 1
    assert not (tmp_path / "Book.br").exists()


def _main() -> int:
    failures = []
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        with tempfile.TemporaryDirectory() as directory:
            needs_tmp = "tmp_path" in function.__code__.co_varnames[:function.__code__.co_argcount]
            try:
                function(Path(directory)) if needs_tmp else function()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 - a failure is the result here
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                failures.append(f"{name} (line {frame.lineno}: {frame.line}): {type(exc).__name__}: {exc}")
                print(f"FAIL {name}", file=sys.stderr)
            else:
                print(f"ok   {name}")
    print()
    print(f"{len(failures)} failure(s)" if failures else "all tests passed")
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())

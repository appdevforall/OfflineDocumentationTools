#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
#     "pikepdf>=10",
# ]
# ///
"""
optimize_pdfs.py

Rewrites PDFs losslessly so the Brotli they ship in compresses them better:
the PDF rows of a documentation database, or one PDF file such as a Bookshelf
plugin book (ADFA-6629).

Why: every shipped PDF is already wrapped in Brotli (a Content row, or the
plugin's .pdf.br asset), but inside it most streams are Flate-compressed, and
Brotli cannot shrink Flate output. Storing those streams plain lets the outer
Brotli compress them instead. Classic PDF optimization (qpdf's object streams,
Flate recompressed at level 9) saved 0.1% here, and qpdf's --recompress-flate
strips the PNG predictors from images (Java Notes grew 60 KB), so it is not
used.

The rewrite, per PDF:
- Every stream that is not an image and whose filters are all generalized
  (Flate, LZW, ASCII85, ASCIIHex, RunLength) is stored plain: content streams,
  fonts, ICC profiles, forms.
- A Flate image of 8-bit gray, 8-bit RGB or 1-bit samples is re-encoded as
  PNG-predicted Flate (Pillow's optimized PNG encoder) when that is smaller,
  and only when Pillow gives back exactly the same sample bytes (it zeroes the
  pad bits at the end of a 1-bit row, which a reader may not). Image data
  stays compressed, so the plain PDF stays reasonably small.
- A JPEG wrapped in Flate ([/FlateDecode /DCTDecode]) is stored as the bare
  JPEG, its DCT decode parameters kept, when its Flate data is whole: the
  checksum right and nothing after the end. The outer Brotli compresses these
  JPEGs better than Flate does.
- Other images (bare JPEG, JPEG 2000, JBIG2, CCITT) are left as they are.
- Saved with object streams, no stream recompressed, and a deterministic /ID,
  so the same input always gives the same bytes and a second run changes
  nothing.

Measured on the 2026-10-19 ship-time database and the Bookshelf books of
addons 6d14aa7, with pikepdf 10.5.1 (qpdf 12.3.2), Pillow 12.2.0 and brotli
1.2.0. Sizes as shipped: a database row against its dictionary, a book as
plain Brotli q11 w24.

    PDF                                       before      after   plain PDF before -> after
    d/PluginDevelopmentGuide.pdf             376,166    241,111      406,791 ->  1,124,176
    d/KeystoreGeneratorPluginDocumentation   279,750    192,150      900,857 ->  2,270,799
    d/SnippetsPluginDocumentation            193,215    129,435      304,385 ->    770,663
    d/BeepyPluginDocumentation               147,590    101,932      209,603 ->    601,784
    d/MarkdownPreviewerPluginDocumentation    97,427     76,757      114,266 ->    194,994
    d/APKViewerPluginDocumentation            93,606     79,950      112,701 ->    197,122
    AndroidNotesForProfessionals           8,810,306  6,320,438    9,797,404 -> 33,930,413
    JavaNotesForProfessionals              4,772,016  2,812,106    6,867,048 -> 25,480,261
    JavaJavaJava                           4,162,347  2,258,547    4,752,514 -> 14,733,459
    BeejGuideToCProgramming                1,346,539    705,655    1,434,906 ->  6,771,870
    KotlinNotesForProfessionals              787,651    590,175    1,097,854 ->  3,718,498
    ModernCplusplusTutorialOuChangkun        388,053    278,975      417,807 ->  1,234,691
    PebbleTemplateGuide                       90,299     50,466      154,606 ->    321,412

The six database rows: 1,187,754 -> 821,335 stored bytes. Packed as the
pipeline packs (rebuild_database.py --pack Content,TooltipTexts --page-size
4096) and set against the same database packed the same way without this
step, the file goes from 85,708,800 to 85,340,160 bytes (-368,640) and its .br
(brotli -q 11 -w 24) from 67,288,387 to 66,914,581 (-373,806). The nine
placeholder PDF rows (the Bookshelf stand-ins, under 2.4 KB) stay as they are:
their rewrite is larger. The seven books: 20,357,211 -> 13,016,362 bytes.
Every page of all thirteen (3,816) renders the same in poppler at 72 dpi
before and after, and pdfinfo -dests lists the same destinations.

Before anything is written, the rewritten PDF must pass every check, or it is
left as it is and the report says why:
- walked from the trailer's /Root and /Info, both object graphs have the same
  shape, and every dictionary is unchanged apart from a stream's /Filter,
  /DecodeParms and /Length;
- every stream's decoded data is identical: fully decoded for generalized
  filters (so a re-encoded image gives back the same samples), and for a JPEG,
  JPEG 2000, JBIG2 or CCITT stream the payload that filter reads, with its
  decode parameters (a stream among them, such as a JBIG2Globals, compared by
  its data the same way);
- pikepdf's syntax check (qpdf's, which also decodes every stream, JPEGs and
  JBIG2 included) finds nothing;
- the page count, the named destinations (the catalog's /Dests and the
  /Names /Dests tree) and the outline, nested items, actions and levels
  included, are unchanged; a stream in any of them is compared by its data.
A rewrite that is not smaller once compressed is left alone too. A stream qpdf
cannot decode without a warning is not rewritten, an image included: storing
the part qpdf could read would quietly change what a reader gets. Such a
stream, left as it is, usually fails the syntax check, and its PDF then stays
as it is.

Left alone by policy, which is not a failure: an encrypted PDF, and a signed
one (its /AcroForm sets SigFlags bit 1, or it has a /Perms entry), because a
signature covers the file's bytes and every check would pass while it broke.

pikepdf decodes JBIG2 through the jbig2dec program; where it is missing, the
syntax check cannot run on a PDF with a JBIG2 image, which then stays as it is
(refused, "it cannot be rewritten").

The qpdf command line's --check is stricter about name trees than the syntax
check: on the rewritten Beej and JavaJavaJava books it reports "skipping over
invalid kid", for empty dictionaries their /Dests name trees already hold in
the original, at the same places. The graph check proves those trees are
unchanged.

The plain PDF grows (Android Notes from 9.8 MB to 33 MB), and Code On The Go
decodes a whole row into memory before serving it, so the phone test measures
the load. A size cap, if one is needed, belongs in keep_rewrite().

Database mode: every application/pdf Content row (chunk rows joined as
WebServer.kt joins them), decoded with the database's own dictionary,
rewritten, and re-encoded with that dictionary. Rows are updated in place:
the base row is never deleted and re-inserted, because the AddBook and
DeleteBook triggers would replace its curated Bookshelf entry. Continuation
rows are trimmed or added as the new size needs. A PDF row that is not
Brotli-compressed is skipped and named. Runs in one transaction (rolled back on
any error), backs the database up first, and VACUUMs afterwards. --dry-run does
all the work, the checks included, and writes nothing. The report ends with a
summary: how many PDFs were rewritten, and how many were left as they are
because they were not smaller, refused by a check (by check) or skipped by
policy (by reason). Exit 1 means nothing was written: a row could not be read,
or a check refused every PDF that policy did not skip.

Pipeline position: on the copy that ships, after svg_images_to_webp.py and
before the final dictionary re-mint and rebuild_database.py --pack. It touches
only PDF rows.

File mode: a .pdf, or a .pdf.br holding plain Brotli, to a .pdf.br of plain
Brotli at q11 w24 (no dictionary: the Bookshelf plugin inserts its asset bytes
as they are). When the rewrite is not smaller or policy skips the PDF, the
output holds the input PDF unchanged; when a check refuses it, nothing is
written and the exit is 1. The target keeps its mode; a new one gets
0666 less the umask, as open() would give it.

    uv run scripts/optimize_pdfs.py documentation.db --dry-run
    uv run scripts/optimize_pdfs.py documentation.db [--workers N] [--verbose]
    uv run scripts/optimize_pdfs.py --file books/JavaJavaJava.pdf.br out/JavaJavaJava.pdf.br
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sqlite3
import stat
import struct
import sys
import tempfile
import zlib
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import brotli
import pikepdf
from PIL import Image

from optimize_db_media import BrotliCodec, backup_database, human, is_continuation_path, load_dictionary, reassemble
from update_media_references import replace_row

PDF_TYPE = "application/pdf"
GENERALIZED = frozenset({"/FlateDecode", "/LZWDecode", "/ASCII85Decode", "/ASCIIHexDecode", "/RunLengthDecode"})
# Stream dictionary keys a rewrite may change; every other key must stay as it was.
REWRITABLE_KEYS = frozenset({"/Filter", "/DecodeParms", "/Length"})
# (components, bits per component) -> Pillow mode, for the images re-encoded as PNG-predicted Flate.
PNG_MODES = {(1, 8): "L", (3, 8): "RGB", (1, 1): "1"}
# The PNG predictor that lets each row choose its own filter, as a PNG encoder does.
PNG_OPTIMUM = 15
# File mode's Brotli: the settings the Bookshelf books and the database download ship with.
FILE_QUALITY, FILE_LGWIN = 11, 24


class LeftAsIs(Exception):
    """Why a PDF is left as it is. `kind` names the check or the policy, for the summary. Both go in args, so the
    exception survives the process pool's pickling."""

    def __init__(self, message: str, kind: str):
        super().__init__(message, kind)

    @property
    def kind(self) -> str:
        return self.args[1]

    def __str__(self) -> str:
        return self.args[0]


class Refused(LeftAsIs):
    """A rewritten PDF failed a check: a failure, which the summary and the exit code report."""


class Skipped(LeftAsIs):
    """A PDF this step does not rewrite by policy (encrypted, signed, not Brotli): not a failure."""


def filters(stream: pikepdf.Object) -> list:
    f = stream.get("/Filter")
    if f is None:
        return []
    return [str(x) for x in f] if isinstance(f, pikepdf.Array) else [str(f)]


def decode_parms(stream: pikepdf.Object, count: int) -> list:
    """The stream's /DecodeParms as one entry per filter (None where absent)."""
    p = stream.get("/DecodeParms")
    if isinstance(p, pikepdf.Array):
        return list(p) + [None] * (count - len(p))
    return [p] + [None] * (count - 1) if count else []


def has_predictor(parms) -> bool:
    return isinstance(parms, pikepdf.Dictionary) and int(parms.get("/Predictor", 1)) > 1


def inflate(raw: bytes) -> bytes:
    """Flate data decoded as a tolerant reader decodes it (a missing end of stream is not an error)."""
    return zlib.decompressobj().decompress(raw)


def inflate_whole(raw: bytes) -> bytes | None:
    """Flate data decoded only when it is whole: its checksum right and nothing after its end. Otherwise None, and
    the stream is not rewritten."""
    d = zlib.decompressobj()
    try:
        data = d.decompress(raw) + d.flush()
    except zlib.error:
        return None
    return data if d.eof and not d.unused_data else None


def components(colour_space) -> int | None:
    if colour_space is None:
        return None
    name = str(colour_space[0] if isinstance(colour_space, pikepdf.Array) else colour_space)
    if name in ("/DeviceGray", "/CalGray", "/Indexed", "/Separation"):
        return 1
    if name in ("/DeviceRGB", "/CalRGB", "/Lab"):
        return 3
    if name == "/ICCBased":
        return int(colour_space[1].get("/N", 0))
    return None


def png_flate(stream: pikepdf.Object, samples: bytes) -> tuple | None:
    """(PNG-predicted Flate data, its /DecodeParms) for an image stream's decoded samples, or None when the image is
    not one this step re-encodes."""
    mask = bool(stream.get("/ImageMask", False))
    bpc = int(stream.get("/BitsPerComponent", 1 if mask else 8))
    n = 1 if mask else components(stream.get("/ColorSpace"))
    mode = PNG_MODES.get((n, bpc))
    if mode is None:
        return None
    width, height = int(stream.Width), int(stream.Height)
    if len(samples) != height * ((width * n * bpc + 7) // 8):
        return None
    img = Image.frombytes(mode, (width, height), samples)
    if img.tobytes() != samples:
        return None  # Pillow zeroes the pad bits at the end of a 1-bit row; set ones must stay as they are
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    png, pos, idat = buf.getvalue(), 8, []
    while pos < len(png):
        size, kind = struct.unpack(">I4s", png[pos:pos + 8])
        if kind == b"IDAT":
            idat.append(png[pos + 8:pos + 8 + size])
        pos += 12 + size
    parms = pikepdf.Dictionary(Predictor=PNG_OPTIMUM, Colors=n, BitsPerComponent=bpc, Columns=width)
    return b"".join(idat), parms


def rewrite_objects(pdf: pikepdf.Pdf) -> dict:
    """Applies the rewrite to every stream of `pdf` in place; returns counts of what changed."""
    counts = {"decoded": 0, "images": 0, "jpegs": 0}
    for obj in pdf.objects:
        if not isinstance(obj, pikepdf.Stream):
            continue
        fs = filters(obj)
        if not fs:
            continue
        if obj.get("/Subtype") == "/Image":
            raw = obj.read_raw_bytes()
            if fs == ["/FlateDecode"]:
                warnings_before = len(pdf.get_warnings())
                samples = obj.read_bytes()
                if len(pdf.get_warnings()) > warnings_before:
                    continue  # qpdf could not decode it cleanly; keep the stream as the reader gets it today
                got = png_flate(obj, samples)
                if got is not None and len(got[0]) < len(raw):
                    obj.write(got[0], filter=pikepdf.Name.FlateDecode, decode_parms=got[1])
                    counts["images"] += 1
            elif fs == ["/FlateDecode", "/DCTDecode"]:
                p = obj.get("/DecodeParms")
                if p is not None and not isinstance(p, pikepdf.Array):
                    continue  # one dictionary for two filters is malformed; leave it to the reader
                flate_parms, dct_parms = decode_parms(obj, 2)
                if has_predictor(flate_parms):
                    continue
                jpeg = inflate_whole(raw)
                if jpeg is None:
                    continue  # a bad checksum, a cut end or bytes after it: keep it as the reader gets it today
                obj.write(jpeg, filter=pikepdf.Name.DCTDecode, decode_parms=dct_parms)
                counts["jpegs"] += 1
            continue
        if set(fs) <= GENERALIZED:
            warnings_before = len(pdf.get_warnings())
            data = obj.read_bytes()
            if len(pdf.get_warnings()) > warnings_before:
                continue  # qpdf could not decode it cleanly; keep the stream as the reader gets it today
            obj.write(data)
            counts["decoded"] += 1
    return counts


def save(pdf: pikepdf.Pdf) -> bytes:
    buf = io.BytesIO()
    pdf.save(buf, object_stream_mode=pikepdf.ObjectStreamMode.generate, compress_streams=False,
             stream_decode_level=pikepdf.StreamDecodeLevel.none, deterministic_id=True,
             fix_metadata_version=False)
    return buf.getvalue()


def payload(stream: pikepdf.Object) -> tuple:
    """What a reader gets from a stream: the fully decoded data when every filter is generalized; otherwise the data
    its last (image) filter reads, with that filter's name and decode parameters."""
    fs = filters(stream)
    if not fs:
        return ("data", stream.read_raw_bytes())
    if set(fs) <= GENERALIZED:
        try:
            return ("data", stream.read_bytes())
        except pikepdf.PdfError:
            return ("raw", tuple(fs), stream.read_raw_bytes())  # the rewrite leaves such a stream as it is
    parms = decode_parms(stream, len(fs))
    head, last = fs[:-1], fs[-1]
    if head == []:
        data = stream.read_raw_bytes()
    elif head == ["/FlateDecode"] and not has_predictor(parms[0]):
        data = inflate(stream.read_raw_bytes())
    else:
        return ("raw", tuple(fs), stream.read_raw_bytes())
    return (last, canonical(parms[-1]), data)


def canonical(obj, pages: dict = None, depth: int = 0):
    """A plain-Python value for comparing small objects (destinations, actions, decode parameters) across two
    files: a page reference becomes ("page", its index), any other indirect object its content."""
    if depth > 32:
        return ("deep",)
    if isinstance(obj, pikepdf.Object) and obj.is_indirect and pages is not None and obj.objgen in pages:
        return ("page", pages[obj.objgen])
    if isinstance(obj, pikepdf.Stream):
        # By what a reader gets, as compare_graphs compares one: the rewrite may change how it is stored.
        return ("stream", {str(k): canonical(obj[k], pages, depth + 1) for k in sorted(obj.keys())
                           if k not in REWRITABLE_KEYS}, payload(obj))
    if isinstance(obj, pikepdf.Dictionary):
        return {str(k): canonical(obj[k], pages, depth + 1) for k in sorted(obj.keys())}
    if isinstance(obj, pikepdf.Array):
        return [canonical(x, pages, depth + 1) for x in obj]
    if isinstance(obj, pikepdf.Object):
        return ("bytes", bytes(obj)) if obj._type_code == pikepdf.ObjectType.string else str(obj)
    return (type(obj).__name__, obj)


def named_destinations(pdf: pikepdf.Pdf, pages: dict) -> dict:
    dests = {}
    old_style = pdf.Root.get("/Dests")
    if isinstance(old_style, pikepdf.Dictionary):
        for k in old_style.keys():
            dests[("catalog", str(k))] = canonical(old_style[k], pages)
    names = pdf.Root.get("/Names")
    if isinstance(names, pikepdf.Dictionary) and isinstance(names.get("/Dests"), pikepdf.Dictionary):
        for k, v in pikepdf.NameTree(names.Dests).items():
            dests[("tree", k)] = canonical(v, pages)
    return dests


def outline(pdf: pikepdf.Pdf, pages: dict) -> list:
    """[(depth, title, destination, action)] in reading order, walking /First and /Next."""
    out, seen = [], set()
    root = pdf.Root.get("/Outlines")
    stack = [(root.get("/First"), 0)] if isinstance(root, pikepdf.Dictionary) else []
    while stack:
        item, depth = stack.pop()
        if not isinstance(item, pikepdf.Dictionary) or item.objgen in seen:
            continue
        seen.add(item.objgen)
        out.append((depth, canonical(item.get("/Title")), canonical(item.get("/Dest"), pages),
                    canonical(item.get("/A"), pages)))
        stack.append((item.get("/Next"), depth))
        stack.append((item.get("/First"), depth + 1))
    return out


def page_index(pdf: pikepdf.Pdf) -> dict:
    return {p.objgen: i for i, p in enumerate(pdf.pages)}


def same_scalar(a, b) -> bool:
    if isinstance(a, pikepdf.Object) and isinstance(b, pikepdf.Object):
        if a._type_code != b._type_code:
            return False
        if a._type_code == pikepdf.ObjectType.string:
            return bytes(a) == bytes(b)
        return a.unparse() == b.unparse()
    return type(a) is type(b) and a == b


def compare_graphs(old: pikepdf.Pdf, new: pikepdf.Pdf) -> str | None:
    """None when `new` is `old`'s object graph (from the trailer's /Root and /Info) with only the allowed changes;
    otherwise where they first differ. A reference to a missing object reads as null on either side."""
    mapping, taken = {}, set()
    stack = [(key, old.trailer.get(key), new.trailer.get(key)) for key in ("/Info", "/Root")]
    while stack:
        where, a, b = stack.pop()
        a_ind = isinstance(a, pikepdf.Object) and a.is_indirect
        b_ind = isinstance(b, pikepdf.Object) and b.is_indirect
        if a_ind != b_ind:
            return f"{where}: indirect on one side only"
        if a_ind:
            if a.objgen in mapping:
                if mapping[a.objgen] != b.objgen:
                    return f"{where}: object {a.objgen} written as two objects"
                continue
            if b.objgen in taken:
                return f"{where}: two objects written as one"
            mapping[a.objgen] = b.objgen
            taken.add(b.objgen)
        if isinstance(a, pikepdf.Stream) or isinstance(b, pikepdf.Stream):
            if not (isinstance(a, pikepdf.Stream) and isinstance(b, pikepdf.Stream)):
                return f"{where}: a stream on one side only"
            ka, kb = set(a.keys()) - REWRITABLE_KEYS, set(b.keys()) - REWRITABLE_KEYS
            if ka != kb:
                return f"{where}: stream keys differ ({sorted(ka ^ kb)})"
            stack.extend((f"{where}{k}", a[k], b[k]) for k in sorted(ka))
            if payload(a) != payload(b):
                return f"{where}: stream {a.objgen} decodes differently"
        elif isinstance(a, pikepdf.Dictionary) or isinstance(b, pikepdf.Dictionary):
            if not (isinstance(a, pikepdf.Dictionary) and isinstance(b, pikepdf.Dictionary)):
                return f"{where}: a dictionary on one side only"
            if set(a.keys()) != set(b.keys()):
                return f"{where}: keys differ ({sorted(set(a.keys()) ^ set(b.keys()))})"
            stack.extend((f"{where}{k}", a[k], b[k]) for k in sorted(a.keys()))
        elif isinstance(a, pikepdf.Array) or isinstance(b, pikepdf.Array):
            if not (isinstance(a, pikepdf.Array) and isinstance(b, pikepdf.Array)) or len(a) != len(b):
                return f"{where}: arrays differ"
            stack.extend((f"{where}[{i}]", x, y) for i, (x, y) in enumerate(zip(a, b)))
        elif not same_scalar(a, b):
            return f"{where}: {a!r} became {b!r}"
    return None


def verify(old_data: bytes, new_data: bytes) -> None:
    """Raises Refused unless `new_data` passes every check against `old_data`."""
    with pikepdf.open(io.BytesIO(old_data)) as old, pikepdf.open(io.BytesIO(new_data)) as new:
        # The graph first: qpdf repairs a damaged page tree or name tree in memory when one is read (the checks
        # below read both), and the comparison has to see each file as it is written.
        difference = compare_graphs(old, new)
        if difference:
            raise Refused(difference, "graph")
        problems = new.check_pdf_syntax()
        if problems:
            # qpdf names an in-memory file by its Python repr; say "the PDF" instead.
            problem = re.sub(r"stream <_io\.BytesIO object at 0x[0-9a-f]+>", "the PDF", problems[0])
            raise Refused(f"the syntax check reports {problem}", "syntax")
        old_pages, new_pages = page_index(old), page_index(new)
        if len(old_pages) != len(new_pages):
            raise Refused(f"{len(old_pages)} pages became {len(new_pages)}", "pages")
        if named_destinations(old, old_pages) != named_destinations(new, new_pages):
            raise Refused("the named destinations changed", "destinations")
        if outline(old, old_pages) != outline(new, new_pages):
            raise Refused("the outline changed", "outline")


def rewrite_pdf(data: bytes) -> tuple:
    """(new PDF bytes, counts) for a PDF's bytes, checked; raises Refused when it cannot be rewritten safely, and
    Skipped for a PDF this step leaves alone by policy."""
    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except pikepdf.PdfError as exc:
        raise Refused(f"it cannot be opened ({exc})", "unreadable") from exc
    with pdf:
        if pdf.is_encrypted:
            raise Skipped("it is encrypted", "encrypted")
        # A signature covers the file's bytes, which any rewrite changes: the checks would pass and the signature
        # would break. SigFlags bit 1 says the form holds a signature; /Perms holds DocMDP or usage-rights ones.
        form = pdf.Root.get("/AcroForm")
        if isinstance(form, pikepdf.Dictionary) and int(form.get("/SigFlags", 0)) & 1:
            raise Skipped("it is signed (its /AcroForm sets SigFlags)", "signed")
        if "/Perms" in pdf.Root:
            raise Skipped("it is signed (it has a /Perms entry)", "signed")
        counts = rewrite_objects(pdf)
        new = save(pdf)
    verify(data, new)
    return new, counts


def keep_rewrite(old_size: int, new_size: int) -> bool:
    """Whether a checked rewrite is worth writing, given both sizes as they ship."""
    return new_size < old_size


def rewrite_job(item: tuple) -> tuple:
    """(path, new PDF bytes, counts), or (path, None, the LeftAsIs saying why), for the process pool."""
    path, data = item
    try:
        new, counts = rewrite_pdf(data)
    except LeftAsIs as exc:
        return path, None, exc
    except Exception as exc:  # noqa: BLE001 - anything else about one PDF leaves that PDF as it is
        return path, None, Refused(f"it cannot be rewritten ({type(exc).__name__}: {exc})", "error")
    return path, new, counts


def summary(rewritten: int, not_smaller: int, refused: Counter, skipped: Counter) -> str:
    """One line: how many PDFs were rewritten, and why each of the others was left as it is."""
    def by_kind(counter):
        return ", ".join(f"{kind} {n}" for kind, n in sorted(counter.items()))
    parts = [f"{not_smaller} not smaller"]
    if refused:
        parts.append(f"{sum(refused.values())} refused by a check ({by_kind(refused)})")
    if skipped:
        parts.append(f"{sum(skipped.values())} skipped by policy ({by_kind(skipped)})")
    return f"{rewritten} rewritten; left as they are: " + "; ".join(parts) + "."


def all_refused(refused: Counter, candidates: int) -> bool:
    """Whether a check refused every PDF that policy did not skip: nothing is written, and the run fails."""
    return candidates > 0 and sum(refused.values()) == candidates


def describe(counts: dict) -> str:
    return f"{counts['decoded']} stream(s) stored plain, {counts['images']} image(s) re-encoded, " \
           f"{counts['jpegs']} JPEG(s) unwrapped"


def run(cfg: dict) -> int:
    db_path = cfg["db_path"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1
    conn = sqlite3.connect(db_path)
    codec = None
    try:
        lengths = {path: n for path, n in conn.execute("SELECT path, LENGTH(content) FROM Content")}
        rows = [r for r in conn.execute(
            "SELECT c.path, c.content, c.languageID, c.contentTypeID, c.templateId, ct.compression "
            "FROM Content c JOIN ContentTypes ct ON ct.id = c.contentTypeID WHERE ct.value = ? ORDER BY c.path",
            (PDF_TYPE,)) if not is_continuation_path(lengths, r[0])]
        codec = BrotliCodec(load_dictionary(conn))
        work, problems, refused, skipped, not_smaller = [], [], Counter(), Counter(), 0
        for path, content, lang, ctid, tid, comp in rows:
            if comp != "brotli":
                print(f"  {path}: skipped: stored with compression {comp!r}, not brotli")
                skipped["not brotli"] += 1
                continue
            blob = reassemble(conn, path, content)
            try:
                work.append((path, blob, codec.decompress(blob, comp), lang, ctid, tid))
            except Exception as exc:  # noqa: BLE001 - one unreadable row stops the run
                problems.append(f"{path}: cannot be read ({exc})")
        if problems:
            for p in problems:
                print(f"  error: {p}", file=sys.stderr)
            print(f"error: {len(problems)} PDF row(s) cannot be read; nothing was written", file=sys.stderr)
            return 1
        print(f"{len(rows)} PDF row(s); {len(work)} to try.")

        jobs = [(w[0], w[2]) for w in work]
        if cfg["workers"] > 1:
            with ProcessPoolExecutor(max_workers=cfg["workers"]) as pool:
                done = list(pool.map(rewrite_job, jobs))
        else:
            done = [rewrite_job(job) for job in jobs]
        results = {path: (new, info) for path, new, info in done}
        changes = []
        for path, blob, plain, lang, ctid, tid in work:
            new, info = results[path]
            if new is None:
                print(f"  {path}: left as it is: {info}")
                (skipped if isinstance(info, Skipped) else refused)[info.kind] += 1
                continue
            stored = codec.compress(new, "brotli")
            if not keep_rewrite(len(blob), len(stored)):
                not_smaller += 1
                if cfg["verbose"]:
                    print(f"  {path}: left as it is: {human(len(stored))} stored is not smaller than "
                          f"{human(len(blob))}")
                continue
            print(f"  {path}: {human(len(blob))} -> {human(len(stored))} stored; plain PDF {human(len(plain))} -> "
                  f"{human(len(new))}" + (f"; {describe(info)}" if cfg["verbose"] else ""))
            changes.append((path, lengths[path], len(blob), stored, lang, ctid, tid))
        print(summary(len(changes), not_smaller, refused, skipped))
        candidates = len(work) - sum(1 for new, info in results.values() if isinstance(info, Skipped))
        if all_refused(refused, candidates):
            print("error: a check refused every PDF it tried; nothing was written", file=sys.stderr)
            return 1
        if not changes:
            print("Nothing to do: no PDF gets smaller.")
            return 0
        saved = sum(before - len(stored) for _p, _b, before, stored, *_ in changes)
        print(f"{len(changes)} PDF row(s) to rewrite, saving {human(saved)} stored bytes.")

        if cfg["dry_run"]:
            print("Dry run: nothing written.")
            return 0
        print(f"Backup written to {backup_database(db_path)}")
        conn.execute("BEGIN")
        try:
            # replace_row UPDATEs the base row, so the Bookshelf triggers (AFTER INSERT / AFTER DELETE on a '%.pdf'
            # path) never fire for it; the "<path>-N" continuation rows it trims or adds do not match '%.pdf'.
            for path, base_len, _before, stored, lang, ctid, tid in changes:
                replace_row(conn, path, base_len, stored, lang, ctid, tid, lambda m: print(m, file=sys.stderr))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        if codec is not None:
            codec.close()
        conn.close()

    vac = sqlite3.connect(db_path)
    try:
        vac.execute("VACUUM")
    finally:
        vac.close()
    print(f"Done. Rewrote {len(changes)} PDF row(s).")
    return 0


def run_file(cfg: dict) -> int:
    source, target = cfg["file"]
    if not source.is_file():
        print(f"error: {source} does not exist", file=sys.stderr)
        return 1
    if target.suffixes[-2:] != [".pdf", ".br"]:
        print(f"error: {target} must end in .pdf.br", file=sys.stderr)
        return 1
    raw = source.read_bytes()
    if source.name.endswith(".br"):
        try:
            plain = brotli.decompress(raw)
        except brotli.error as exc:
            print(f"error: {source} is not plain Brotli ({exc})", file=sys.stderr)
            return 1
        before = raw
    else:
        plain = raw
        before = brotli.compress(plain, quality=FILE_QUALITY, lgwin=FILE_LGWIN)
    _path, new, info = rewrite_job((str(source), plain))
    out, rewritten, refused, skipped, not_smaller = before, 0, Counter(), Counter(), 0
    if new is None:
        print(f"{source}: left as it is: {info}")
        (skipped if isinstance(info, Skipped) else refused)[info.kind] += 1
    else:
        after = brotli.compress(new, quality=FILE_QUALITY, lgwin=FILE_LGWIN)
        if keep_rewrite(len(before), len(after)):
            out, rewritten = after, 1
            print(f"{source}: {human(len(before))} -> {human(len(after))} bytes; plain PDF {human(len(plain))} -> "
                  f"{human(len(new))}; {describe(info)}")
        else:
            not_smaller = 1
            print(f"{source}: left as it is: {human(len(after))} is not smaller than {human(len(before))}")
    print(summary(rewritten, not_smaller, refused, skipped))
    if all_refused(refused, 1 - sum(skipped.values())):
        print(f"error: a check refused {source}; nothing was written", file=sys.stderr)
        return 1
    if cfg["dry_run"]:
        print("Dry run: nothing written.")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    # The target keeps its mode; a new one gets what open() would give it. mkstemp's 0600 would otherwise stay.
    if target.exists():
        mode = stat.S_IMODE(target.stat().st_mode)
    else:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    # Through a temporary file in the same directory, so the target is never half written and may be the source.
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(out)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    print(f"Wrote {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, nargs="?", help="SQLite database to edit in place, e.g. documentation.db")
    p.add_argument("--file", type=Path, nargs=2, metavar=("SOURCE", "TARGET"),
                   help="Rewrite one .pdf or plain-Brotli .pdf.br into TARGET (.pdf.br) instead of a database")
    p.add_argument("--dry-run", action="store_true", help="Do all the work and checks; write nothing")
    p.add_argument("--workers", type=int, default=8, help="Processes that rewrite PDFs; 1 runs in this one "
                   "(default: 8)")
    p.add_argument("--verbose", action="store_true", help="Print what each rewrite changed")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if (args.db_path is None) == (args.file is None):
        parser.error("give a database or --file SOURCE TARGET, not both")
    if args.file:
        sys.exit(run_file({"file": args.file, "dry_run": args.dry_run}))
    sys.exit(run({"db_path": args.db_path, "dry_run": args.dry_run, "workers": args.workers,
                  "verbose": args.verbose}))


if __name__ == "__main__":
    main()

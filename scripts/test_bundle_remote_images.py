#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
#     "pytest",
# ]
# ///
"""
test_bundle_remote_images.py

Covers bundle_remote_images.py: that each listed file is stored as a Content
row the way a/devsite/media/ rows are, that each <img src> loading it from
developer.android.com is pointed at the stored copy by a relative path and no
other byte of the page changes, that a file not matching its recorded size and
sha256 stops the run before anything is written, and that any other appearance
of a listed URL is reported rather than rewritten.

Fixtures use the real URLs and markup of three of the diagrams, with small
stand-in files; ContentTypes ids differ from the production database's, so a
tool that hard-coded them would fail. One test checks the files actually
committed in remote_media/adfa-6589/ against their list.

Run it either way - as a plain script, needing nothing installed:

    uv run scripts/test_bundle_remote_images.py

or under pytest:

    uv run --with brotli --with Pillow --with pytest pytest scripts/test_bundle_remote_images.py

The dictionary tests need the `brotli` CLI on PATH, as the tool itself does.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from urllib.parse import urljoin

import brotli
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bundle_remote_images as tool  # noqa: E402

PNG_URL = "https://developer.android.com/images/axis_globe.png"
GIF_URL = "https://developer.android.com/images/mediaplayer_state_diagram.gif"
SVG_URL = "https://developer.android.com/static/images/reference/androidx/media3/common/timeline-window.svg"
PNG_PATH = "a/devsite/media/images_axis_globe.png"
GIF_PATH = "a/devsite/media/images_mediaplayer_state_diagram.gif"
SVG_PATH = "a/devsite/media/static_images_reference_androidx_media3_common_timeline-window.svg"


def _image(fmt: str) -> bytes:
    out = io.BytesIO()
    Image.new("P" if fmt == "GIF" else "RGB", (3, 2), 1).save(out, fmt)
    return out.getvalue()


PNG = _image("PNG")
GIF = _image("GIF")
SVG = (b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
       b'width="10" height="10"><rect width="10" height="10" fill="#3ddc84"/></svg>\n')
FILES = {PNG_URL: (PNG_PATH, "image/png", PNG), GIF_URL: (GIF_PATH, "image/gif", GIF),
         SVG_URL: (SVG_PATH, "image/svg+xml", SVG)}

# Markup as the API reference pages carry it, with neighbours that must stay:
# a link to developer.android.com (not a listed file) and a root-relative
# image that is not this tool's business.
HEAD = (b'<!DOCTYPE html>\n<html>\n<body>\n<a href="https://developer.android.com/reference">API reference</a>\n'
        b'<img alt="" src="/static/reference/android/images/text/style/dynamicdrawablespan.png"/>\n')
TAIL = b"\n</body>\n</html>\n"


def _img(url: str, alt: str = "World coordinate-system diagram.") -> bytes:
    return f'<img alt="{alt}" border="0" src="{url}"/>'.encode()


PAGES = {
    "a/android/hardware/SensorEvent.html": HEAD + _img(PNG_URL) + TAIL,
    "a/android/hardware/SensorManager.html": HEAD + _img(PNG_URL) + TAIL,
    "a/android/media/MediaPlayer.html": HEAD + _img(GIF_URL, "MediaPlayer State diagram") + TAIL,
    "a/androidx/media3/common/Timeline.Window.html": HEAD + _img(SVG_URL, "Information defined by a timeline window")
    + TAIL,
}
EXPECTED = {
    "a/android/hardware/SensorEvent.html": HEAD + _img("../../devsite/media/images_axis_globe.png") + TAIL,
    "a/android/hardware/SensorManager.html": HEAD + _img("../../devsite/media/images_axis_globe.png") + TAIL,
    "a/android/media/MediaPlayer.html": HEAD + _img("../../devsite/media/images_mediaplayer_state_diagram.gif",
                                                    "MediaPlayer State diagram") + TAIL,
    "a/androidx/media3/common/Timeline.Window.html": HEAD + _img(
        "../../../devsite/media/static_images_reference_androidx_media3_common_timeline-window.svg",
        "Information defined by a timeline window") + TAIL,
}

# Ids unlike the production database's (png 3, gif 11, svg 2).
TYPES = {"text/html": (12, "brotli"), "image/png": (30, "none"), "image/gif": (31, "none"),
         "image/svg+xml": (32, "brotli")}


def _compress(raw: bytes, dictionary: bytes = None) -> bytes:
    """Stored bytes for a brotli row, at quality 5: the tool re-compresses at the
    default (11), so a row it touched comes out different from one it did not."""
    if dictionary is None:
        return brotli.compress(raw, quality=5)
    with tempfile.TemporaryDirectory() as directory:
        dict_path = Path(directory) / "dictionary.bin"
        dict_path.write_bytes(dictionary)
        return subprocess.run(["brotli", "-D", str(dict_path), "-q", "5", "-c"], input=raw,
                              stdout=subprocess.PIPE, check=True).stdout


def _decompress(blob: bytes, dictionary: bytes = None) -> bytes:
    """Decoded independently of the tool's own codec."""
    if dictionary is None:
        return brotli.decompress(blob)
    with tempfile.TemporaryDirectory() as directory:
        dict_path = Path(directory) / "dictionary.bin"
        dict_path.write_bytes(dictionary)
        return subprocess.run(["brotli", "-D", str(dict_path), "-d", "-c"], input=blob,
                              stdout=subprocess.PIPE, check=True).stdout


def _media_dir(tmp_path: Path, files: dict = None, lines: list = None) -> Path:
    """A remote_media folder: each file under its database path's last
    component, and a files.tsv listing them (or `lines`, written as given)."""
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    rows = [tool.INDEX_HEADER, "# " + "\t".join(tool.INDEX_COLUMNS)]
    for url, (path, content_type, data) in (FILES if files is None else files).items():
        (media_dir / path.rsplit("/", 1)[1]).write_bytes(data)
        rows.append("\t".join((url, path, content_type, str(len(data)), hashlib.sha256(data).hexdigest(),
                               "Apache-2.0 / CC-BY-2.5, developer.android.com/license")))
    (media_dir / tool.INDEX_NAME).write_text("\n".join(rows if lines is None else lines) + "\n")
    return media_dir


def _database(tmp_path: Path, pages: dict = None, dictionary: bytes = None, types: dict = None,
              extra: dict = None) -> Path:
    """A miniature documentation database holding `pages` (path -> decoded
    bytes, or (bytes, True) to store them as they are) and `extra` rows
    (path -> (content type, decoded bytes))."""
    db_path = tmp_path / "documentation.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE, "
        "compression TEXT NOT NULL);"
        "CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, "
        "languageID INTEGER NOT NULL, content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, "
        "templateId INTEGER NOT NULL DEFAULT 0, UNIQUE(path));"
    )
    types = TYPES if types is None else types
    conn.executemany("INSERT INTO ContentTypes (id, value, compression) VALUES (?, ?, ?)",
                     [(type_id, value, compression) for value, (type_id, compression) in types.items()])
    if dictionary is not None:
        conn.execute("CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
        conn.execute("INSERT INTO CompressionDictionary (id, data) VALUES (1, ?)", (dictionary,))
    rows = {path: ("text/html", page) for path, page in (PAGES if pages is None else pages).items()}
    rows.update(extra or {})
    for path, (content_type, value) in rows.items():
        raw, as_is = value if isinstance(value, tuple) else (value, False)
        type_id, compression = types[content_type]
        stored = raw if as_is or compression == "none" else _compress(raw, dictionary)
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, 1, ?, ?, 0)", (path, stored, type_id))
    conn.commit()
    conn.close()
    return db_path


def _stored(db_path: Path) -> dict:
    """{path: (id, languageID, content, contentTypeID, templateId)} for every row."""
    conn = sqlite3.connect(db_path)
    try:
        return {row[0]: row[1:] for row in conn.execute(
            "SELECT path, id, languageID, content, contentTypeID, templateId FROM Content")}
    finally:
        conn.close()


def _run(db_path: Path, media_dir: Path, **overrides) -> tuple:
    """(exit code, stdout, stderr) of one run of the tool."""
    cfg = {"db_path": db_path, "media_dir": media_dir, "dry_run": False, "workers": 2, "verbose": False}
    cfg.update(overrides)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = tool.run(cfg)
    return code, out.getvalue(), err.getvalue()


def _page(db_path: Path, path: str, dictionary: bytes = None) -> bytes:
    return _decompress(_stored(db_path)[path][2], dictionary)


def test_each_file_is_stored_like_the_other_devsite_media(tmp_path: Path):
    """languageID 1, templateId 0, the ContentTypes id read from the database,
    and compressed as that type says: PNG and GIF as they are, SVG in Brotli."""
    db_path = _database(tmp_path)
    code, out, err = _run(db_path, _media_dir(tmp_path))
    assert (code, err) == (0, "")
    rows = _stored(db_path)
    for path, content_type, data in FILES.values():
        _row_id, language_id, content, content_type_id, template_id = rows[path]
        assert (language_id, content_type_id, template_id) == (1, TYPES[content_type][0], 0), path
        assert (_decompress(content) if TYPES[content_type][1] == "brotli" else content) == data, path
    assert rows[PNG_PATH][2] == PNG and rows[SVG_PATH][2] != SVG
    assert "Storing 3 file(s)" in out


def test_each_img_src_points_at_the_stored_file(tmp_path: Path):
    """Relative to the page, from two and three directories deep, resolving on
    the server to the stored row; the rest of each page is byte for byte the
    same, and each page keeps its id, language, type and templateId."""
    db_path = _database(tmp_path)
    before = _stored(db_path)
    assert _run(db_path, _media_dir(tmp_path))[0] == 0
    after = _stored(db_path)
    for path, expected in EXPECTED.items():
        assert _page(db_path, path) == expected, path
        assert after[path][:2] + after[path][3:] == before[path][:2] + before[path][3:]
    for path, url in (("a/android/media/MediaPlayer.html", "../../devsite/media/images_mediaplayer_state_diagram.gif"),
                      ("a/androidx/media3/common/Timeline.Window.html",
                       "../../../devsite/media/static_images_reference_androidx_media3_common_timeline-window.svg")):
        resolved = urljoin(tool.SERVER + path, url)
        assert resolved[len(tool.SERVER):] in after


def test_a_relative_reference_resolves_from_any_depth():
    for page in ("index.html", "a/x.html", "a/devsite/x.html", "a/android/media/x.html", "a/androidx/media3/common/x.html"):
        reference = tool.relative_reference(page, SVG_PATH)
        assert urljoin(tool.SERVER + page, reference) == tool.SERVER + SVG_PATH
    assert tool.relative_reference("a/devsite/x.html", SVG_PATH) == "media/" + SVG_PATH.rsplit("/", 1)[1]


def test_a_tampered_file_is_refused_and_nothing_written(tmp_path: Path):
    """One byte changed, size the same: the digest catches it before the
    database is opened. No backup, no row, no page touched."""
    media_dir = _media_dir(tmp_path)
    tampered = bytearray(PNG)
    tampered[-5] ^= 0xFF
    (media_dir / "images_axis_globe.png").write_bytes(bytes(tampered))
    db_path = _database(tmp_path)
    before = db_path.read_bytes()
    code, _out, err = _run(db_path, media_dir)
    assert code == 1
    assert db_path.read_bytes() == before
    assert not list(tmp_path.glob("*.backup-*"))
    assert "images_axis_globe.png" in err and hashlib.sha256(bytes(tampered)).hexdigest() in err
    assert "nothing was written" in err


def test_a_missing_file_is_refused(tmp_path: Path):
    media_dir = _media_dir(tmp_path)
    (media_dir / "images_mediaplayer_state_diagram.gif").unlink()
    db_path = _database(tmp_path)
    before = db_path.read_bytes()
    code, _out, err = _run(db_path, media_dir)
    assert code == 1
    assert db_path.read_bytes() == before
    assert "images_mediaplayer_state_diagram.gif is missing" in err


def test_a_list_not_as_written_is_refused(tmp_path: Path):
    """A foreign header, a short line, a path that is not the URL's
    a/devsite/media/ name, a malformed digest, a URL listed twice: each stops
    the run, naming the line, before the database is touched."""
    good = [tool.INDEX_HEADER] + [
        "\t".join((url, path, content_type, str(len(data)), hashlib.sha256(data).hexdigest(), "x"))
        for url, (path, content_type, data) in FILES.items()]
    cases = {
        "foreign header": (["# some other list"] + good[1:], "the first line is not"),
        "short line": (good + ["\t".join(good[1].split("\t")[:5])], "expected 6 tab-separated fields, got 5"),
        "wrong path": (good[:1] + [good[1].replace(PNG_PATH, "a/devsite/media/axis_globe.png")] + good[2:],
                       "is not the a/devsite/media/ path for"),
        "bad digest": (good[:1] + [good[1].replace(hashlib.sha256(PNG).hexdigest(), "ab12")] + good[2:],
                       "expected a byte count, a sha256 and an image/ content type"),
        "twice": (good + [good[1]], "is listed twice"),
    }
    for name, (lines, message) in cases.items():
        case_dir = tmp_path / name.replace(" ", "-")
        case_dir.mkdir()
        db_path = _database(case_dir)
        before = db_path.read_bytes()
        code, _out, err = _run(db_path, _media_dir(case_dir, lines=lines))
        assert code == 1, name
        assert db_path.read_bytes() == before, name
        assert message in err, (name, err)


def test_a_content_type_the_database_lacks_is_refused(tmp_path: Path):
    types = {value: entry for value, entry in TYPES.items() if value != "image/gif"}
    db_path = _database(tmp_path, types=types)
    before = db_path.read_bytes()
    code, _out, err = _run(db_path, _media_dir(tmp_path))
    assert code == 1
    assert db_path.read_bytes() == before
    assert "ContentTypes has no row for image/gif" in err


def test_a_listed_url_anywhere_but_an_img_src_is_reported(tmp_path: Path):
    """A link, a srcset, a data-src, JSON-escaped markup, a src with more after
    the URL and a src on something other than an <img> are each left as they
    are and named; the run fails, but the plain <img src> on another page is
    still rewritten."""
    odd = (b'<a href="' + PNG_URL.encode() + b'">full size</a>\n'
           b'<iframe src="' + PNG_URL.encode() + b'"></iframe>\n'
           b'<img alt="" src="media/x.png" srcset="' + PNG_URL.encode() + b' 2x"/>\n'
           b'<img alt="" data-src="' + PNG_URL.encode() + b'"/>\n'
           b'<img alt=\\"\\" src=\\"' + PNG_URL.encode() + b'\\"/>\n'
           b'<img alt="" src="' + PNG_URL.encode() + b'?raw=1"/>\n')
    pages = {"a/android/hardware/Odd.html": HEAD + odd + TAIL,
             "a/android/hardware/SensorEvent.html": PAGES["a/android/hardware/SensorEvent.html"]}
    db_path = _database(tmp_path, pages)
    before = _stored(db_path)
    code, _out, err = _run(db_path, _media_dir(tmp_path))
    assert code == 1
    after = _stored(db_path)
    assert after["a/android/hardware/Odd.html"] == before["a/android/hardware/Odd.html"]
    assert _page(db_path, "a/android/hardware/SensorEvent.html") == EXPECTED["a/android/hardware/SensorEvent.html"]
    assert PNG_PATH in after
    assert err.count("appears other than as the whole src of an <img>") == 6


def test_a_row_already_holding_the_file_is_used(tmp_path: Path):
    """Same bytes, same type: no second row, and the pages point at it."""
    db_path = _database(tmp_path, extra={PNG_PATH: ("image/png", PNG)})
    before = _stored(db_path)
    code, out, _err = _run(db_path, _media_dir(tmp_path))
    assert code == 0
    after = _stored(db_path)
    assert after[PNG_PATH] == before[PNG_PATH]
    assert _page(db_path, "a/android/hardware/SensorEvent.html") == EXPECTED["a/android/hardware/SensorEvent.html"]
    assert "Storing 2 file(s)" in out and "1 already stored" in out


def test_a_row_holding_something_else_is_reported_and_its_pages_left(tmp_path: Path):
    """The path is taken by other bytes: those pages keep their remote src and
    the run fails; the other files are still stored and their pages fixed."""
    db_path = _database(tmp_path, extra={PNG_PATH: ("image/png", GIF)})
    before = _stored(db_path)
    code, _out, err = _run(db_path, _media_dir(tmp_path))
    assert code == 1
    after = _stored(db_path)
    for path in (PNG_PATH, "a/android/hardware/SensorEvent.html", "a/android/hardware/SensorManager.html"):
        assert after[path] == before[path], path
    assert _page(db_path, "a/android/media/MediaPlayer.html") == EXPECTED["a/android/media/MediaPlayer.html"]
    assert GIF_PATH in after and SVG_PATH in after
    assert err.count(f"{PNG_PATH} already holds something other than {PNG_URL}") == 2


def test_a_row_holding_the_bytes_under_another_type_is_reported(tmp_path: Path):
    """The right bytes filed as image/gif would be served with the wrong type."""
    db_path = _database(tmp_path, extra={PNG_PATH: ("image/gif", PNG)})
    before = _stored(db_path)
    code, _out, err = _run(db_path, _media_dir(tmp_path))
    assert code == 1
    after = _stored(db_path)
    assert after[PNG_PATH] == before[PNG_PATH]
    assert after["a/android/hardware/SensorEvent.html"] == before["a/android/hardware/SensorEvent.html"]
    assert f"{PNG_PATH} already holds something other than {PNG_URL}" in err


def test_a_file_no_page_loads_is_not_stored(tmp_path: Path):
    pages = {path: page for path, page in PAGES.items() if "Timeline" not in path}
    db_path = _database(tmp_path, pages)
    code, out, _err = _run(db_path, _media_dir(tmp_path))
    assert code == 0
    assert SVG_PATH not in _stored(db_path)
    assert "Storing 2 file(s)" in out


def test_a_second_run_changes_nothing(tmp_path: Path):
    """Exit 0 and not a byte written - no backup, no VACUUM, no row added -
    the second time."""
    db_path = _database(tmp_path)
    media_dir = _media_dir(tmp_path)
    assert _run(db_path, media_dir)[0] == 0
    first = db_path.read_bytes()
    backups = sorted(tmp_path.glob("*.backup-*"))
    assert len(backups) == 1
    code, out, err = _run(db_path, media_dir)
    assert (code, err) == (0, "")
    assert "nothing to change" in out
    assert db_path.read_bytes() == first
    assert sorted(tmp_path.glob("*.backup-*")) == backups


def test_a_converted_file_is_not_stored_again(tmp_path: Path):
    """After optimize_db_media.py --webp has replaced the stored GIF with a
    WebP, the GIF's row is gone but no page loads it from the internet any
    more, so a later run stores nothing."""
    db_path = _database(tmp_path)
    media_dir = _media_dir(tmp_path)
    assert _run(db_path, media_dir)[0] == 0
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE Content SET path = ? WHERE path = ?", (GIF_PATH[:-4] + ".webp", GIF_PATH))
    conn.commit()
    conn.close()
    first = db_path.read_bytes()
    assert _run(db_path, media_dir)[0] == 0
    assert db_path.read_bytes() == first


def test_dry_run_writes_nothing(tmp_path: Path):
    db_path = _database(tmp_path)
    before = db_path.read_bytes()
    code, out, _err = _run(db_path, _media_dir(tmp_path), dry_run=True)
    assert code == 0
    assert "Storing 3 file(s)" in out and "Dry run: nothing written." in out
    assert db_path.read_bytes() == before
    assert not list(tmp_path.glob("*.backup-*"))


def test_a_dictionary_database(tmp_path: Path):
    """Pages and the SVG are decoded and encoded against the shared dictionary."""
    dictionary = b"".join(PAGES.values()) * 3 + SVG
    db_path = _database(tmp_path, dictionary=dictionary,
                        extra={"a/android/Other.html": ("text/html", HEAD + TAIL)})
    before = _stored(db_path)
    try:
        plain = brotli.decompress(before["a/android/media/MediaPlayer.html"][2])
    except brotli.error:
        plain = None
    assert plain != PAGES["a/android/media/MediaPlayer.html"]
    assert _run(db_path, _media_dir(tmp_path))[0] == 0
    after = _stored(db_path)
    for path, expected in EXPECTED.items():
        assert _page(db_path, path, dictionary) == expected, path
    assert _decompress(after[SVG_PATH][2], dictionary) == SVG
    assert after["a/android/Other.html"] == before["a/android/Other.html"]


def test_a_chunked_page_is_read_and_written_whole(tmp_path: Path):
    """A page over 1 MiB is read across its "-1" row, where the <img> sits, and
    written back re-chunked. Stored uncompressed so its size is exact."""
    chunk = 1024 * 1024
    filler = b"x" * (chunk + 500 - len(HEAD + _img(PNG_URL) + TAIL))
    page = HEAD + filler + _img(PNG_URL) + TAIL
    types = dict(TYPES, **{"text/html": (12, "none")})
    db_path = _database(tmp_path, {"a/android/hardware/Big.html": (page[:chunk], True),
                                   "a/android/hardware/Big.html-1": (page[chunk:], True)}, types=types)
    assert _run(db_path, _media_dir(tmp_path))[0] == 0
    after = _stored(db_path)
    assert after["a/android/hardware/Big.html"][2] + after["a/android/hardware/Big.html-1"][2] == \
        HEAD + filler + _img("../../devsite/media/images_axis_globe.png") + TAIL
    assert PNG_PATH in after


def test_an_unreadable_row_is_reported_and_the_rest_still_change(tmp_path: Path):
    pages = dict(PAGES)
    pages["a/android/Broken.html"] = (b"not brotli at all", True)
    db_path = _database(tmp_path, pages)
    code, _out, err = _run(db_path, _media_dir(tmp_path))
    assert code == 1
    assert "a/android/Broken.html" in err and "could not be read" in err
    assert _page(db_path, "a/android/media/MediaPlayer.html") == EXPECTED["a/android/media/MediaPlayer.html"]


def test_the_committed_files_match_their_list():
    """The 20 files in remote_media/adfa-6589/: each listed, each matching its
    size and sha256, each what its content type says, and nothing else there."""
    entries, problems = tool.read_index(tool.MEDIA_DIR)
    assert problems == []
    data, problems = tool.check_files(tool.MEDIA_DIR, entries)
    assert problems == []
    assert len(entries) == 20 and sum(len(blob) for blob in data.values()) == 249_079
    signatures = {"image/png": (b"\x89PNG\r\n\x1a\n",), "image/gif": (b"GIF87a", b"GIF89a"),
                  "image/svg+xml": (b"<?xml", b"<svg")}
    for entry in entries:
        assert data[entry.database_path].startswith(signatures[entry.content_type]), entry.database_path
    names = {entry.database_path.rsplit("/", 1)[1] for entry in entries} | {tool.INDEX_NAME}
    assert {path.name for path in tool.MEDIA_DIR.iterdir()} == names


def _main() -> int:
    """Runs the tests without pytest installed as a runner, so this file works
    the same way the tools beside it do (`uv run scripts/test_bundle_remote_images.py`)."""
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
                failures.append(f"{name} (line {frame.lineno}: {frame.line}): "
                                f"{type(exc).__name__}: {exc}")
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

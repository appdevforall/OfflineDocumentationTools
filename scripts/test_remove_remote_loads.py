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
test_remove_remote_loads.py

Covers remove_remote_loads.py: that each of the four shapes it rewrites (a
remote srcset beside a stored src, a dark-theme <source>, a kotl.in badge, a
YouTube <video>) is rewritten exactly and no other byte of the page changes,
that JSON pages stay valid JSON, that rows without those elements keep their
stored bytes, and that anything resembling them without being them is reported
and left alone.

The markup is copied from the 2026-10-13 database's pages, shortened. JSON
pages are built with json.dumps in the form populate_db.py stores (no spaces,
non-ASCII kept), so the escaping is the real one. Stored rows are compressed at
a quality the tool never uses, so a row it re-compressed cannot pass for one it
left alone.

Run it either way - as a plain script, needing nothing installed:

    uv run scripts/test_remove_remote_loads.py

or under pytest:

    uv run --with brotli --with Pillow --with pytest pytest scripts/test_remove_remote_loads.py

The dictionary tests need the `brotli` CLI on PATH, as the tool itself does.
"""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import brotli

sys.path.insert(0, str(Path(__file__).resolve().parent))

import remove_remote_loads as tool  # noqa: E402

CHUNK_SIZE = 1024 * 1024

# Group A and B, as on a/devsite/develop_ui_views_touch-and-input_stylus-input.html.
DARK_SOURCE = (b'<source alt="" class="devsite-landing-row-item-icon devsite-dark-theme" loading="lazy" '
               b'media="(prefers-color-scheme: dark)" sizes="64px" '
               b'srcset="https://developer.android.com/static/images/picto-icons/reduce_dt.svg"/>')
ICON = (b'<img alt="" class="devsite-landing-row-item-icon" loading="lazy" sizes="64px" '
        b'src="media/static_images_picto-icons_reduce.svg" '
        b'srcset="https://developer.android.com/static/images/picto-icons/reduce.svg"/>')
ICON_AFTER = (b'<img alt="" class="devsite-landing-row-item-icon" loading="lazy" '
              b'src="media/static_images_picto-icons_reduce.svg"/>')
CARD = (b'<img alt="" loading="lazy" sizes="(max-width: 840px) 100vw, 1440px" '
        b'src="media/static_images_cards_distribute_engage_ls-gallery.webp" '
        b'srcset="https://developer.android.com/static/images/cards/distribute/engage/ls-gallery_36.png 36w,'
        b'https://developer.android.com/static/images/cards/distribute/engage/ls-gallery_48.png 48w"/>')
CARD_AFTER = b'<img alt="" loading="lazy" src="media/static_images_cards_distribute_engage_ls-gallery.webp"/>'
# As on the snackbar page: density descriptors, no sizes, an attribute after srcset.
SNACKBAR = (b'<img alt="" src="media/static_images_training_snackbar_snackbar_drive_2x.webp" '
            b'srcset="https://developer.android.com/static/images/training/snackbar/snackbar_drive.png 1x,'
            b'https://developer.android.com/static/images/training/snackbar/snackbar_drive_2x.png 2x" width="400"/>')
SNACKBAR_AFTER = b'<img alt="" src="media/static_images_training_snackbar_snackbar_drive_2x.webp" width="400"/>'

# Markup on the same pages that must come through untouched: links (not loads),
# a local srcset with its sizes, an API-diagram <img src> (group C, left to its own
# step, so neither rewritten nor reported here) and a stored video.
KEEP = (b'<a class="external" href="https://developer.android.com/develop/ui/views/touch-and-input/stylus-input">'
        b'Stylus</a>\n'
        b'<img alt="" sizes="10px" src="media/static_images_local.svg" '
        b'srcset="media/static_images_local.svg 1x, media/static_images_local_2x.svg 2x"/>\n'
        b'<img alt="MediaPlayer State diagram" border="0" '
        b'src="https://developer.android.com/images/mediaplayer_state_diagram.gif"/>\n'
        b'<a class="external" href="https://www.youtube.com/watch?v=F8boaQsioH8">Stylus video</a>\n'
        b'<video controls=""><source src="media/clip.mp4" type="video/mp4"/></video>\n')

HEAD = b'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8"/>\n</head>\n<body>\n'
TAIL = b'\n</body>\n</html>\n'


def _picture(*elements: bytes) -> bytes:
    return b"<picture>\n" + b"\n".join(elements) + b"\n</picture>\n"


STYLUS = HEAD + _picture(DARK_SOURCE, ICON) + KEEP + CARD + b"\n" + SNACKBAR + TAIL
# The dark <source> goes and leaves its line break behind, like the
# remove_skin_font_comment.py comment: only the element's own bytes are cut.
STYLUS_AFTER = HEAD + b"<picture>\n\n" + ICON_AFTER + b"\n</picture>\n" + KEEP + CARD_AFTER + b"\n" + SNACKBAR_AFTER + TAIL

MEDIA = ("a/devsite/media/static_images_picto-icons_reduce.svg",
         "a/devsite/media/static_images_cards_distribute_engage_ls-gallery.webp",
         "a/devsite/media/static_images_training_snackbar_snackbar_drive_2x.webp",
         "a/devsite/media/static_images_local.svg")


def _json_page(page_id: str, blocks: list) -> bytes:
    """A page as populate_db.py stores it."""
    return json.dumps({"id": page_id, "sourceFile": f"topics/{page_id}.md", "title": "T", "blocks": blocks},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _badge(level: str) -> str:
    return (f'<img src="https://kotl.in/badges/{level}.svg" alt="{level.capitalize()} stability level" />'
            f'{{type=&quot;joined&quot;}} stands for <strong>{level.capitalize()}</strong>')


LEVELS = ("experimental", "alpha", "beta", "stable")
STABILITY_BLOCKS = [
    {"type": "paragraph", "html": "The stability status:\nsee <a href=\"#levels\">levels</a>"},
    {"type": "list", "ordered": False, "items": [{"blocks": [{"type": "paragraph", "html": _badge(level)}]}
                                                for level in LEVELS]},
]
STABILITY = _json_page("k/html/components-stability", STABILITY_BLOCKS)


def _stability_after() -> bytes:
    blocks = json.loads(json.dumps(STABILITY_BLOCKS))
    for item, level in zip(blocks[1]["items"], LEVELS):
        item["blocks"][0]["html"] = (f"<code>{level.capitalize()}</code>{{type=&quot;joined&quot;}} stands for "
                                     f"<strong>{level.capitalize()}</strong>")
    return _json_page("k/html/components-stability", blocks)


# Both src forms the database uses, with and without a size, and a title with
# an apostrophe and a non-ASCII dash.
VIDEOS = [
    ('<video width="560" height="315" src="https://www.youtube.com/v/6-XSehwRgSY" '
     'title="Get Ready for Advent of Code 2021"/>',
     '<a href="https://www.youtube.com/watch?v=6-XSehwRgSY">Get Ready for Advent of Code 2021 (video on YouTube)</a>'),
    ('<video src="https://www.youtube.com/watch?v=jne3923lWtw" title="What\'s new in Kotlin 2.2.0"/>',
     '<a href="https://www.youtube.com/watch?v=jne3923lWtw">What\'s new in Kotlin 2.2.0 (video on YouTube)</a>'),
    ('<video src="https://www.youtube.com/v/jNu5LY9HIbw" title="Kover \u2013 The Code Coverage Plugin"/>',
     '<a href="https://www.youtube.com/watch?v=jNu5LY9HIbw">Kover \u2013 The Code Coverage Plugin '
     '(video on YouTube)</a>'),
]
VIDEO_PROSE = {"type": "paragraph", "html": "Watch the \"overview\":\nor read <a href=\"https://www.youtube.com/"
                                            "watch?v=v0AHdAIBnbs\">the transcript</a>."}


def _video_page(videos: list) -> bytes:
    return _json_page("k/html/whatsnew", [VIDEO_PROSE] + [{"type": "html", "html": v} for v in videos])


WHATSNEW = _video_page([before for before, _after in VIDEOS])
WHATSNEW_AFTER = _video_page([after for _before, after in VIDEOS])


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


def _database(tmp_path: Path, rows: dict, dictionary: bytes = None, html_compression: str = "brotli",
              media=MEDIA) -> Path:
    """A miniature documentation database: ContentTypes, Content, and - when
    `dictionary` is given - a CompressionDictionary every brotli row is
    compressed against. `rows` maps path -> (content type, decoded bytes);
    a value of (content type, bytes, True) stores the bytes as they are.
    Every path in `media` is added as a small stored image."""
    db_path = tmp_path / "documentation.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE, "
        "compression TEXT NOT NULL);"
        "CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, "
        "languageID INTEGER NOT NULL, content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, "
        "templateId INTEGER NOT NULL DEFAULT 0, UNIQUE(path));"
    )
    types = {"text/html": (12, html_compression), "text/javascript": (6, "brotli"), "image/webp": (26, "none"),
             "image/svg+xml": (2, "none"), "video/mp4": (25, "none")}
    conn.executemany("INSERT INTO ContentTypes (id, value, compression) VALUES (?, ?, ?)",
                     [(type_id, value, compression) for value, (type_id, compression) in types.items()])
    if dictionary is not None:
        conn.execute("CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
        conn.execute("INSERT INTO CompressionDictionary (id, data) VALUES (1, ?)", (dictionary,))
    rows = dict(rows)
    for path in media:
        rows.setdefault(path, ("image/webp" if path.endswith(".webp") else "image/svg+xml", b"<image bytes>"))
    for path, (content_type, raw, *as_is) in rows.items():
        type_id, compression = types[content_type]
        stored = raw if as_is or compression == "none" else _compress(raw, dictionary)
        template_id = 2 if path.startswith("k/") else 0
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, 1, ?, ?, ?)", (path, stored, type_id, template_id))
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


def _run(db_path: Path, **overrides) -> tuple:
    """(exit code, stdout, stderr) of one run of the tool."""
    cfg = {"db_path": db_path, "dry_run": False, "workers": 2, "verbose": False}
    cfg.update(overrides)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = tool.run(cfg)
    return code, out.getvalue(), err.getvalue()


def _page(db_path: Path, path: str, dictionary: bytes = None) -> bytes:
    return _decompress(_stored(db_path)[path][2], dictionary)


def test_a_remote_srcset_and_its_sizes_go_and_the_stored_src_stays(tmp_path: Path):
    """Group A: an icon (sizes, single candidate), a card (sizes, width
    descriptors) and the snackbar (no sizes, density descriptors, an attribute
    after srcset). Nothing else on the page changes - links to
    developer.android.com, a local srcset with its own sizes, an API diagram's
    remote src - and the row keeps its id, language, type and templateId."""
    page = HEAD + ICON + b"\n" + KEEP + CARD + b"\n" + SNACKBAR + TAIL
    db_path = _database(tmp_path, {"a/devsite/stylus.html": ("text/html", page)})
    before = _stored(db_path)
    code, out, err = _run(db_path)
    assert (code, err) == (0, "")
    assert _page(db_path, "a/devsite/stylus.html") == HEAD + ICON_AFTER + b"\n" + KEEP + CARD_AFTER + b"\n" \
        + SNACKBAR_AFTER + TAIL
    after = _stored(db_path)["a/devsite/stylus.html"]
    assert after[:2] + after[3:] == before["a/devsite/stylus.html"][:2] + before["a/devsite/stylus.html"][3:]
    assert f"{tool.LABELS[tool.SRCSET]}: 3 on 1 page(s)" in out


def test_a_dark_source_goes_and_its_picture_keeps_the_img(tmp_path: Path):
    """Group B, on the real stylus layout: the <source> is cut, its line break
    stays, and the <img> beside it loses its remote srcset (group A)."""
    db_path = _database(tmp_path, {"a/devsite/stylus.html": ("text/html", STYLUS)})
    code, out, _err = _run(db_path)
    assert code == 0
    assert _page(db_path, "a/devsite/stylus.html") == STYLUS_AFTER
    assert f"{tool.LABELS[tool.DARK_SOURCE]}: 1 on 1 page(s)" in out


def test_kotlin_badges_become_their_level_in_code_style(tmp_path: Path):
    """Group D: each <img> becomes <code>Level</code>; the {type=...} text after
    it and every other byte of the JSON page stay, and the page still parses."""
    db_path = _database(tmp_path, {"k/html/components-stability.html": ("text/html", STABILITY)})
    code, out, _err = _run(db_path)
    assert code == 0
    page = _page(db_path, "k/html/components-stability.html")
    assert page == _stability_after()
    expected = STABILITY
    for level in LEVELS:
        word = level.capitalize().encode()
        expected = expected.replace(b'<img src=\\"https://kotl.in/badges/' + level.encode() + b'.svg\\" alt=\\"'
                                    + word + b' stability level\\" />', b"<code>" + word + b"</code>")
    assert page == expected != STABILITY
    json.loads(page)
    assert f"{tool.LABELS[tool.BADGE]}: 4 on 1 page(s)" in out


def test_youtube_players_become_links_to_the_video(tmp_path: Path):
    """Both src forms (/v/ID and /watch?v=ID), with and without width/height;
    the title carries over byte for byte, non-ASCII included. A link to YouTube
    elsewhere on the page is not a load and stays."""
    db_path = _database(tmp_path, {"k/html/whatsnew.html": ("text/html", WHATSNEW)})
    code, out, _err = _run(db_path)
    assert code == 0
    assert _page(db_path, "k/html/whatsnew.html") == WHATSNEW_AFTER
    assert b"watch?v=v0AHdAIBnbs" in WHATSNEW_AFTER
    assert f"{tool.LABELS[tool.YOUTUBE]}: 3 on 1 page(s)" in out


def test_json_pages_change_only_inside_the_rewritten_strings(tmp_path: Path):
    """Parsed, the edited pages equal the originals with only the rewritten
    strings different - and those differ only by the element."""
    db_path = _database(tmp_path, {"k/html/components-stability.html": ("text/html", STABILITY),
                                   "k/html/whatsnew.html": ("text/html", WHATSNEW)})
    assert _run(db_path)[0] == 0
    before, after = json.loads(WHATSNEW), json.loads(_page(db_path, "k/html/whatsnew.html"))
    assert after["blocks"][0] == before["blocks"][0] == VIDEO_PROSE
    assert [b["html"] for b in after["blocks"][1:]] == [new for _old, new in VIDEOS]
    assert {k: v for k, v in after.items() if k != "blocks"} == {k: v for k, v in before.items() if k != "blocks"}
    stability = json.loads(_page(db_path, "k/html/components-stability.html"))
    assert stability["blocks"][0] == STABILITY_BLOCKS[0]


def test_rows_without_remote_loads_keep_their_stored_bytes(tmp_path: Path):
    """Not re-compressed: pages with nothing to rewrite, including ones that
    only link to developer.android.com or YouTube, keep their blobs exactly."""
    db_path = _database(tmp_path, {"a/devsite/stylus.html": ("text/html", STYLUS),
                                   "a/devsite/links.html": ("text/html", HEAD + KEEP + TAIL),
                                   "k/html/plain.html": ("text/html", _json_page("k/html/plain", [VIDEO_PROSE])),
                                   "i/page.html": ("text/html", b"<p>help</p>")})
    before = _stored(db_path)
    assert _run(db_path)[0] == 0
    after = _stored(db_path)
    for path in ("a/devsite/links.html", "k/html/plain.html", "i/page.html", *MEDIA):
        assert after[path] == before[path], path
    assert after["a/devsite/stylus.html"] != before["a/devsite/stylus.html"]


def test_an_img_whose_src_is_not_stored_keeps_its_srcset_and_is_reported(tmp_path: Path):
    """Dropping the srcset would leave such an image showing nothing - whether
    src names a file the database lacks or an address off the device. Its
    neighbour on the same page, whose src is stored, is still fixed."""
    missing = ICON.replace(b"media/static_images_picto-icons_reduce.svg", b"media/static_images_gone.svg")
    remote = ICON.replace(b"media/static_images_picto-icons_reduce.svg",
                          b"https://developer.android.com/static/images/picto-icons/reduce.svg")
    db_path = _database(tmp_path, {"a/devsite/odd.html": ("text/html", HEAD + missing + remote + ICON + TAIL)})
    code, _out, err = _run(db_path)
    assert code == 1
    assert _page(db_path, "a/devsite/odd.html") == HEAD + missing + remote + ICON_AFTER + TAIL
    assert err.count("which is not a stored file") == 2
    assert "media/static_images_gone.svg" in err and "a/devsite/odd.html" in err


def test_a_srcset_mixing_remote_and_local_candidates_is_reported(tmp_path: Path):
    mixed = ICON.replace(b'srcset="https://developer.android.com/static/images/picto-icons/reduce.svg"',
                         b'srcset="media/static_images_picto-icons_reduce.svg 1x, '
                         b'https://developer.android.com/static/images/picto-icons/reduce_2x.svg 2x"')
    db_path = _database(tmp_path, {"a/devsite/odd.html": ("text/html", HEAD + mixed + TAIL)})
    before = _stored(db_path)
    code, _out, err = _run(db_path)
    assert code == 1
    assert _stored(db_path) == before
    assert "mixes developer.android.com with other addresses" in err


def test_a_source_that_is_not_the_dark_variant_is_reported(tmp_path: Path):
    light = DARK_SOURCE.replace(b"(prefers-color-scheme: dark)", b"(prefers-color-scheme: light)")
    no_media = DARK_SOURCE.replace(b'media="(prefers-color-scheme: dark)" ', b"")
    page = HEAD + _picture(light, ICON_AFTER) + _picture(no_media, ICON_AFTER) + TAIL
    db_path = _database(tmp_path, {"a/devsite/odd.html": ("text/html", page)})
    before = _stored(db_path)
    code, _out, err = _run(db_path)
    assert code == 1
    assert _stored(db_path) == before
    assert err.count("is not the dark-theme variant") == 2


def test_a_dark_source_outside_a_picture_is_reported(tmp_path: Path):
    """Before any <picture>, and after one has closed."""
    page = HEAD + DARK_SOURCE + b"\n" + _picture(ICON_AFTER) + DARK_SOURCE + b"\n" + ICON_AFTER + TAIL
    db_path = _database(tmp_path, {"a/devsite/odd.html": ("text/html", page)})
    before = _stored(db_path)
    code, _out, err = _run(db_path)
    assert code == 1
    assert _stored(db_path) == before
    assert err.count("is not inside a <picture>") == 2


def test_a_dark_source_with_no_img_to_fall_back_to_is_reported(tmp_path: Path):
    """No <img> in its <picture> - the only one comes after the </picture> - or
    a <picture> that is never closed."""
    page = HEAD + _picture(DARK_SOURCE) + ICON_AFTER + b"\n<picture>\n" + DARK_SOURCE + TAIL
    db_path = _database(tmp_path, {"a/devsite/odd.html": ("text/html", page)})
    before = _stored(db_path)
    code, _out, err = _run(db_path)
    assert code == 1
    assert _stored(db_path) == before
    assert err.count("has no <img> after it in its <picture>") == 2


def test_a_tag_this_tool_cannot_parse_is_reported(tmp_path: Path):
    """Single-quoted attributes, or one named twice: not read, not guessed at."""
    quoted = (b"<img alt='' src='media/static_images_picto-icons_reduce.svg' "
              b"srcset='https://developer.android.com/static/images/picto-icons/reduce.svg'/>")
    twice = ICON.replace(b'alt=""', b'src="x.svg"')
    db_path = _database(tmp_path, {"a/devsite/odd.html": ("text/html", HEAD + quoted + twice + TAIL)})
    before = _stored(db_path)
    code, _out, err = _run(db_path)
    assert code == 1
    assert _stored(db_path) == before
    assert err.count("this tool does not parse") == 2


def test_badges_and_videos_in_other_shapes_are_reported(tmp_path: Path):
    """A badge whose alt names another level, one with its attributes the other
    way round, one for a level that is not one of the four; a <video> with an
    extra attribute, and titles holding a quote, a JSON escape or an angle
    bracket, none of which can move into link text as they are: each left in
    place and named, while the recognised ones beside them still change."""
    odd_badges = ['<img src="https://kotl.in/badges/alpha.svg" alt="Beta stability level" />',
                  '<img alt="Stable stability level" src="https://kotl.in/badges/stable.svg" />',
                  '<img src="https://kotl.in/badges/deprecated.svg" alt="Deprecated stability level" />']
    odd_videos = ['<video controls src="https://www.youtube.com/v/6-XSehwRgSY" title="T"/>',
                  '<video src="https://www.youtube.com/v/6-XSehwRgSY" title="A \\"quote\\""/>',
                  '<video src="https://www.youtube.com/v/6-XSehwRgSY" title="Two\nlines"/>',
                  '<video src="https://www.youtube.com/v/6-XSehwRgSY" title="List<T> tips"/>']
    blocks = ([{"type": "paragraph", "html": badge} for badge in odd_badges]
              + [{"type": "paragraph", "html": _badge("beta")}]
              + [{"type": "html", "html": video} for video in odd_videos]
              + [{"type": "html", "html": VIDEOS[0][0]}])
    page = _json_page("k/html/odd", blocks)
    db_path = _database(tmp_path, {"k/html/odd.html": ("text/html", page)})
    code, _out, err = _run(db_path)
    assert code == 1
    after = [b["html"] for b in json.loads(_page(db_path, "k/html/odd.html"))["blocks"]]
    assert after[:3] == odd_badges
    assert after[3].startswith("<code>Beta</code>")
    assert after[4:8] == odd_videos
    assert after[8] == VIDEOS[0][1]
    assert err.count("a kotl.in badge not in the shape") == 3
    assert err.count("a YouTube <video> not in the shape") == 4


def test_json_escaped_shapes_in_a_page_that_is_not_json_are_reported(tmp_path: Path):
    """The badge and player shapes are matched with JSON escaping, so they are
    only ever rewritten inside a JSON page; and a player in plain HTML is not a
    shape this tool knows."""
    escaped_badge = b'<img src=\\"https://kotl.in/badges/beta.svg\\" alt=\\"Beta stability level\\" />'
    plain_video = b'<video src="https://www.youtube.com/v/6-XSehwRgSY" title="T"/>'
    page = HEAD + escaped_badge + plain_video + TAIL
    db_path = _database(tmp_path, {"a/devsite/odd.html": ("text/html", page)})
    before = _stored(db_path)
    code, _out, err = _run(db_path)
    assert code == 1
    assert _stored(db_path) == before
    assert "written JSON-escaped in a page that is not JSON" in err
    assert "a YouTube <video> not in the shape" in err


def test_a_json_page_that_does_not_parse_is_left_and_reported(tmp_path: Path):
    page = STABILITY[:-2]
    db_path = _database(tmp_path, {"k/html/broken.html": ("text/html", page)})
    before = _stored(db_path)
    code, _out, err = _run(db_path)
    assert code == 1
    assert _stored(db_path) == before
    assert "the page is not valid JSON" in err


def test_an_edit_that_would_leave_invalid_json_is_refused(tmp_path: Path):
    """Defensive: the shapes cannot cross a JSON string's end, but if an edit
    ever did break the page, the page is left as it was rather than stored."""
    db_path = _database(tmp_path, {"k/html/components-stability.html": ("text/html", STABILITY)})
    before = _stored(db_path)
    real = tool.json_edits

    def breaking(page):
        edits, counts, odd = real(page)
        return [(start, end, b'"') for start, end, _new in edits], counts, odd

    tool.json_edits = breaking
    try:
        code, _out, err = _run(db_path)
    finally:
        tool.json_edits = real
    assert code == 1
    assert _stored(db_path) == before
    assert "would not be valid JSON after the edit" in err


def test_a_second_run_changes_nothing(tmp_path: Path):
    """Exit 0 and not a byte written - no backup, no VACUUM - the second time."""
    db_path = _database(tmp_path, {"a/devsite/stylus.html": ("text/html", STYLUS),
                                   "k/html/whatsnew.html": ("text/html", WHATSNEW)})
    assert _run(db_path)[0] == 0
    first = db_path.read_bytes()
    backups = sorted(tmp_path.glob("*.backup-*"))
    assert len(backups) == 1
    code, out, err = _run(db_path)
    assert (code, err) == (0, "")
    assert "nothing to change" in out
    assert db_path.read_bytes() == first
    assert sorted(tmp_path.glob("*.backup-*")) == backups


def test_dry_run_writes_nothing(tmp_path: Path):
    db_path = _database(tmp_path, {"a/devsite/stylus.html": ("text/html", STYLUS)})
    before = db_path.read_bytes()
    code, out, _err = _run(db_path, dry_run=True)
    assert code == 0
    assert "1 row(s) load from the internet" in out and "Dry run: nothing written." in out
    assert db_path.read_bytes() == before
    assert not list(tmp_path.glob("*.backup-*"))


def test_a_non_html_row_is_untouched(tmp_path: Path):
    db_path = _database(tmp_path, {"a/devsite/script.js": ("text/javascript", STYLUS),
                                   "a/devsite/stylus.html": ("text/html", STYLUS)})
    before = _stored(db_path)
    assert _run(db_path)[0] == 0
    after = _stored(db_path)
    assert after["a/devsite/script.js"] == before["a/devsite/script.js"]
    assert _page(db_path, "a/devsite/stylus.html") == STYLUS_AFTER


def test_a_dictionary_database(tmp_path: Path):
    """Rows are decoded and re-encoded against the shared dictionary, and a
    page with nothing to change keeps its stored bytes."""
    dictionary = (STYLUS + WHATSNEW) * 3
    db_path = _database(tmp_path, {"a/devsite/stylus.html": ("text/html", STYLUS),
                                   "k/html/whatsnew.html": ("text/html", WHATSNEW),
                                   "a/devsite/links.html": ("text/html", HEAD + KEEP + TAIL)},
                        dictionary=dictionary)
    before = _stored(db_path)
    try:
        plain = brotli.decompress(before["a/devsite/stylus.html"][2])
    except brotli.error:
        plain = None
    assert plain != STYLUS
    assert _run(db_path)[0] == 0
    assert _page(db_path, "a/devsite/stylus.html", dictionary) == STYLUS_AFTER
    assert _page(db_path, "k/html/whatsnew.html", dictionary) == WHATSNEW_AFTER
    assert _stored(db_path)["a/devsite/links.html"] == before["a/devsite/links.html"]


def test_a_chunked_page_is_read_and_written_whole(tmp_path: Path):
    """A page over CHUNK_SIZE is read across its "-1" row and written back
    re-chunked. Stored uncompressed so its size is exact."""
    filler = b"x" * (CHUNK_SIZE + 500 - len(HEAD + CARD + TAIL))
    page = HEAD + filler + CARD + TAIL
    db_path = _database(tmp_path, {"a/devsite/big.html": ("text/html", page[:CHUNK_SIZE], True),
                                   "a/devsite/big.html-1": ("text/html", page[CHUNK_SIZE:], True)},
                        html_compression="none")
    assert _run(db_path)[0] == 0
    after = _stored(db_path)
    expected = HEAD + filler + CARD_AFTER + TAIL
    assert after["a/devsite/big.html"][2] + after["a/devsite/big.html-1"][2] == expected


def test_an_unreadable_row_is_reported_and_the_rest_still_change(tmp_path: Path):
    db_path = _database(tmp_path, {"a/devsite/stylus.html": ("text/html", STYLUS),
                                   "a/devsite/broken.html": ("text/html", b"not brotli at all", True)})
    code, _out, err = _run(db_path)
    assert code == 1
    assert "a/devsite/broken.html" in err and "could not be read" in err
    assert _page(db_path, "a/devsite/stylus.html") == STYLUS_AFTER


def _main() -> int:
    """Runs the tests without pytest installed as a runner, so this file works
    the same way the tools beside it do (`uv run scripts/test_remove_remote_loads.py`)."""
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

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
test_video_webm.py

Covers what ADFA-6596 added to optimize_db_media.py and
update_media_references.py: --video-webm, which turns each H.264 video row
(video/mp4, video/quicktime) into VP9 WebM, encoded from the original file of
the same name in --video-originals and never from the stored copy; renaming
the references to those videos, with a <source>'s type set to video/webm; and
undoing both exactly.

The clips are made at test time from ffmpeg's lavfi test sources, and encoded
by the real ffmpeg, as test_animated_media.py does for GIFs - so ffmpeg (with
libx264, aac, libvpx-vp9 and libopus) and ffprobe must be on PATH. Each stored
row is cut down from its original the way the production database's were: 15
fps, 480 px wide, enlarged when the original is narrower. That enlargement is
what shows a WebM came from the original: a 320 px original makes a 320 px
WebM, where its stored copy would have made a 480 px one.

Run it either way - as a plain script:

    uv run scripts/test_video_webm.py

or under pytest:

    uv run --with brotli --with Pillow --with pytest pytest scripts/test_video_webm.py
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import optimize_db_media as opt  # noqa: E402
import test_animated_media as animated  # noqa: E402
import test_media_bundle as base  # noqa: E402
import update_media_references as ref  # noqa: E402

VIDEO_TYPES = [(12, "text/html", "brotli"), (25, "video/mp4", "none"), (26, "image/webp", "brotli"),
               (28, "video/quicktime", "none"), (30, "video/webm", "none")]
FFMPEG = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]


def _clip(path: Path, width: int, height: int, seconds: float = 2, audio: bool = False, noise: int = 0,
          pattern: str = "testsrc2") -> Path:
    """An H.264 original from one of ffmpeg's test patterns at 30 fps, with a
    440 Hz tone as AAC when `audio`. Noise makes it expensive to store."""
    source = f"{pattern}=size={width}x{height}:rate=30:duration={seconds}" + (f",noise=alls={noise}:allf=t"
                                                                           if noise else "")
    command = FFMPEG + ["-f", "lavfi", "-i", source]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac", "-shortest"]
    subprocess.run(command + ["-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def _cut_down(original: Path, path: Path, width: int = 480, crf: int = 23) -> bytes:
    """The database's kind of copy of `original`: 15 fps, `width` px wide
    whatever the original's width, audio kept."""
    subprocess.run(FFMPEG + ["-i", str(original), "-vf", f"fps=15,scale={width}:-2", "-c:v", "libx264",
                             "-crf", str(crf), "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)
    return path.read_bytes()


def _scene(tmp_path: Path, pages: list = ()) -> tuple:
    """Three clips stored cut down, and a directory of their originals with
    the digest of each one's stored clip: one wider than 480 px, one narrower
    (a .mov, so video/quicktime is covered), and one with sound, plus `pages`.
    Returns (database, originals directory)."""
    originals, work = tmp_path / "originals", tmp_path / "work"
    originals.mkdir()
    work.mkdir()
    wide = _clip(originals / "wide.mp4", 640, 360)
    narrow = _clip(originals / "narrow.mov", 320, 240)
    talk = _clip(originals / "talk.mp4", 640, 360, audio=True)
    db_path = tmp_path / "documentation.db"
    animated._build_database(db_path, [("a/media/wide.mp4", "video/mp4", _cut_down(wide, work / "wide.mp4")),
                                       ("a/media/narrow.mov", "video/quicktime",
                                        _cut_down(narrow, work / "narrow.mov")),
                                       ("a/media/talk.mp4", "video/mp4", _cut_down(talk, work / "talk.mp4")),
                                       *pages], VIDEO_TYPES)
    _record_digests(db_path, originals)
    return db_path, originals


def _record_digests(db_path: Path, originals: Path, rerecord: tuple = ()) -> str:
    """--write-video-digests, as when the originals are gathered; returns what
    it printed. A test that changes a stored clip afterwards and means the
    change to be accepted names that clip's original in `rerecord`, as
    --rerecord-video-digest does."""
    code, output = _call(opt.write_video_digests, {"db_path": db_path, "write_video_digests": originals,
                                                   "rerecord_video_digests": list(rerecord)})
    assert code == 0, output
    return output


def _video_cfg(db_path: Path, originals: Path, **overrides) -> dict:
    return base._optimize_cfg(db_path, video_webm=True, video_originals=originals, **overrides)


def _streams(data: bytes, tmp_path: Path) -> dict:
    """{codec_type: ffprobe's fields} for each stream of a video in memory."""
    video = tmp_path / "probe.webm"
    video.write_bytes(data)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height,avg_frame_rate", "-of", "json", str(video)],
                         capture_output=True, text=True, check=True).stdout
    return {stream["codec_type"]: stream for stream in json.loads(out)["streams"]}


def _call(function, cfg: dict) -> tuple:
    """(exit code, everything `function` printed)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = function(cfg)
    return code, out.getvalue()


def _run(cfg: dict) -> tuple:
    return _call(opt.run, cfg)


def test_video_webm_encodes_each_clip_from_its_original(tmp_path):
    db_path, originals = _scene(tmp_path)

    assert opt.run(_video_cfg(db_path, originals)) == 0

    expected = {"a/media/wide": (480, False), "a/media/narrow": (320, False), "a/media/talk": (480, True)}
    for stem, (width, audio) in expected.items():
        path, value, data = animated._stored_media(db_path, stem)
        assert (path, value) == (f"{stem}.webm", "video/webm")
        streams = _streams(data, tmp_path)
        video = streams["video"]
        # 320 px is the original's width; its stored copy is 480 px wide, so a
        # WebM made from that, or enlarged, would be 480 px too.
        assert (video["codec_name"], video["width"], video["avg_frame_rate"]) == ("vp9", width, "15/1"), stem
        assert ("audio" in streams) == audio, f"{stem}: {streams}"
        if audio:
            assert streams["audio"]["codec_name"] == "opus"


def test_video_webm_encodes_to_the_same_bytes_every_time(tmp_path):
    """A rebuilt database differs from the last one only where content did:
    the muxer's random track IDs would otherwise differ on every run."""
    original = _clip(tmp_path / "talk.mp4", 640, 360, audio=True)
    stored = _cut_down(original, tmp_path / "stored.mp4")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    assert (opt.encode_video_webm(original, stored, opt.digest(stored), 40, ffmpeg, ffprobe)
            == opt.encode_video_webm(original, stored, opt.digest(stored), 40, ffmpeg, ffprobe))


def test_a_video_with_no_original_is_left_and_reported(tmp_path):
    """Encoding the stored copy instead would stack a second generation of loss
    on a clip that is lossy already, which is what the originals are for. The
    rest are committed, but the run exits non-zero, so a wrong originals
    folder cannot pass for a clean run. A script stopping on that exit code
    must still rewrite the references to the clips that did convert, so the
    run says so. Given the original, a rerun converts the clip that was left."""
    db_path, originals = _scene(tmp_path)
    aside = tmp_path / "wide.mp4"
    (originals / "wide.mp4").rename(aside)
    before, manifest = base._snapshot(db_path), tmp_path / "manifest.tsv"

    code, output = _run(_video_cfg(db_path, originals, manifest_out=manifest))

    assert code == 1, output
    after = base._snapshot(db_path)
    assert after["a/media/wide.mp4"] == before["a/media/wide.mp4"], "converted with no original"
    assert "a/media/talk.webm" in after, "the other clips should still convert"
    assert "a/media/wide.mp4: no original" in output and "exits non-zero" in output, output
    assert "run update_media_references.py" in output, "the references step must still run for the rest"
    assert " 0 error(s)" in output, "a clip left for want of its original is listed, not counted as an error"
    assert "a/media/wide.mp4" not in opt.read_manifest(manifest), "a later run with the original must retry it"

    aside.rename(originals / "wide.mp4")
    code, output = _run(_video_cfg(db_path, originals))
    assert code == 0, output
    assert "a/media/wide.webm" in base._snapshot(db_path)


def test_a_clip_replaced_since_its_original_was_gathered_waits_for_its_own(tmp_path):
    """An author replaced wide.mp4 with another clip of the same length, so
    only the digest recorded when the originals were gathered tells that the
    original in hand is stale. Encoding it would have put the old clip back
    over theirs. Once the new clip's original is gathered, it converts."""
    db_path, originals = _scene(tmp_path)
    # 320 px wide, so a WebM made from it is 320 px where one made from the
    # stale 640 px original is 480; noisy and stored at high quality, so any
    # WebM is smaller than the stored copy and only the digest keeps the row.
    replacement = _clip(tmp_path / "replacement.mp4", 320, 180, noise=40, pattern="smptebars")
    stored = _cut_down(replacement, tmp_path / "replacement-stored.mp4", crf=8)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE Content SET content = ? WHERE path = 'a/media/wide.mp4'", (stored,))
    before = base._snapshot(db_path)

    code, output = _run(_video_cfg(db_path, originals))

    assert base._snapshot(db_path).get("a/media/wide.mp4") == before["a/media/wide.mp4"], "the stale original won"
    assert code == 1, output
    assert "a/media/wide.mp4: the stored clip changed since wide.mp4 was gathered" in output, output

    shutil.copy(replacement, originals / "wide.mp4")
    _record_digests(db_path, originals, rerecord=["wide.mp4"])
    code, output = _run(_video_cfg(db_path, originals))

    assert code == 0, output
    path, _value, data = animated._stored_media(db_path, "a/media/wide")
    assert (path, _streams(data, tmp_path)["video"]["width"]) == ("a/media/wide.webm", 320)


def test_an_original_with_no_recorded_digest_is_left_and_reported(tmp_path):
    db_path, originals = _scene(tmp_path)
    digests = originals / opt.VIDEO_DIGESTS
    digests.write_text("".join(line for line in digests.read_text().splitlines(keepends=True)
                               if not line.startswith("wide.mp4\t")))
    before = base._snapshot(db_path)

    code, output = _run(_video_cfg(db_path, originals))

    after = base._snapshot(db_path)
    assert after.get("a/media/wide.mp4") == before["a/media/wide.mp4"], "converted with no recorded digest"
    assert code == 1, output
    assert "a/media/talk.webm" in after, "the other clips should still convert"
    assert "a/media/wide.mp4: stored-digests.tsv" in output and "records no stored clip" in output, output


def test_the_digests_record_the_clip_each_original_was_gathered_for(tmp_path):
    """Read back as written, one line per original with a video row. Written
    again, the file gains a line for an original gathered since and keeps
    every other line as it was."""
    db_path, originals = _scene(tmp_path)
    digests, aside = originals / opt.VIDEO_DIGESTS, tmp_path / "narrow.mov"
    digests.unlink()
    (originals / "narrow.mov").rename(aside)
    snapshot = base._snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        stored = {path.rsplit("/", 1)[-1]: hashlib.sha256(content).hexdigest()
                  for path, content in conn.execute("SELECT path, content FROM Content")}

    _record_digests(db_path, originals)

    assert digests.read_text().splitlines()[:2] == [opt.VIDEO_DIGESTS_HEADER, opt.VIDEO_DIGESTS_COLUMNS]
    assert opt.read_video_digests(digests) == {name: stored[name] for name in ("wide.mp4", "talk.mp4")}
    written = digests.read_text()
    aside.rename(originals / "narrow.mov")
    _record_digests(db_path, originals)
    assert digests.read_text() == written + f"narrow.mov\t{stored['narrow.mov']}\n"
    written = digests.read_text()
    _record_digests(db_path, originals)
    assert digests.read_text() == written, "nothing was left to record, yet the file changed"
    assert base._snapshot(db_path) == snapshot, "writing the digests changed the database"


def test_a_digest_file_not_as_written_is_refused_naming_the_line(tmp_path):
    """A wrong digest used to be reported as a changed clip, a name given
    twice let its last line win, and a missing header went unnoticed. Each is
    now refused, naming the line, by the run and by the writer before either
    does anything. Windows line ends and a trailing blank line are read."""
    sha = "a" * 64
    good = f"{opt.VIDEO_DIGESTS_HEADER}\n{opt.VIDEO_DIGESTS_COLUMNS}\nwide.mp4\t{sha}\n"
    path = tmp_path / opt.VIDEO_DIGESTS
    for text in (good.replace("\n", "\r\n"), good + "\n"):
        path.write_text(text, encoding="utf-8", newline="")
        assert opt.read_video_digests(path) == {"wide.mp4": sha}
    duplicated = good + f"wide.mp4\t{'b' * 64}\n"
    for text, line, why in ((f"wide.mp4\t{sha}\n", 1, "header"), (good.replace(" v1", " v2"), 1, "header"),
                            ("", 1, "header"), (duplicated, 4, "wide.mp4 is recorded already, on line 3"),
                            (good.replace(sha, sha.upper()), 3, "not a sha256"),
                            (good.replace(sha, sha[1:]), 3, "not a sha256"),
                            (good.replace(sha, sha + " "), 3, "not a sha256")):
        path.write_text(text, encoding="utf-8", newline="")
        with pytest.raises(RuntimeError) as caught:
            opt.read_video_digests(path)
        assert f"{path}:{line}: " in str(caught.value) and why in str(caught.value), (text, caught.value)

    db_path, originals = tmp_path / "documentation.db", tmp_path / "originals"
    originals.mkdir()
    (originals / "wide.mp4").write_bytes(b"an original")
    (originals / opt.VIDEO_DIGESTS).write_text(duplicated)
    animated._build_database(db_path, [("a/media/wide.mp4", "video/mp4", b"a clip")], VIDEO_TYPES)
    code, output = _run(_video_cfg(db_path, originals))
    assert code == 1 and "recorded already" in output and "Backing up" not in output, output
    code, output = _call(opt.write_video_digests, _digests_cfg(db_path, originals))
    assert code == 1 and "recorded already" in output, output
    assert (originals / opt.VIDEO_DIGESTS).read_text() == duplicated


def _digests_cfg(db_path: Path, originals: Path) -> dict:
    return {"db_path": db_path, "write_video_digests": originals, "rerecord_video_digests": []}


def test_one_name_storing_two_clips_is_not_recorded(tmp_path):
    """Originals are found by file name, so one original cannot vouch for two
    different clips stored under its name: recording either would let the
    other be encoded from an original gathered for something else."""
    originals, db_path = tmp_path / "originals", tmp_path / "documentation.db"
    originals.mkdir()
    (originals / "wide.mp4").write_bytes(b"an original")
    animated._build_database(db_path, [("a/x/wide.mp4", "video/mp4", b"one clip"),
                                       ("b/y/wide.mp4", "video/mp4", b"another clip")], VIDEO_TYPES)

    code, output = _call(opt.write_video_digests, _digests_cfg(db_path, originals))

    assert code == 1, output
    assert "wide.mp4: a/x/wide.mp4, b/y/wide.mp4" in output and "nothing was written" in output, output
    assert not (originals / opt.VIDEO_DIGESTS).exists()


def test_digests_with_no_original_matching_a_row_are_not_written(tmp_path):
    """A folder of originals that matches no row is the wrong folder or the
    wrong database; a file recording nothing would pass for a gathered set."""
    originals, db_path = tmp_path / "originals", tmp_path / "documentation.db"
    originals.mkdir()
    (originals / "other.mp4").write_bytes(b"an original")
    animated._build_database(db_path, [("a/media/wide.mp4", "video/mp4", b"a clip")], VIDEO_TYPES)

    code, output = _call(opt.write_video_digests, _digests_cfg(db_path, originals))

    assert code == 1, output
    assert "none of the 1 video row(s) has an original" in output, output
    assert not (originals / opt.VIDEO_DIGESTS).exists()


def _replace_clip(db_path: Path, name: str, work: Path, pattern: str = "smptebars") -> Path:
    """An author's replacement for a/media/<name>, stored in its row; returns
    the new clip's original. It is 320 px wide, so a WebM made from it is 320
    px where one made from the stale 640 px original is 480; noisy and stored
    at high quality, so any WebM is smaller than the stored copy and only the
    digest keeps the row."""
    replacement = _clip(work / f"new-{name}", 320, 180, noise=40, pattern=pattern)
    stored = _cut_down(replacement, work / f"stored-{name}", crf=8)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE Content SET content = ? WHERE path = ?", (stored, f"a/media/{name}"))
    return replacement


def test_recording_one_replaced_clip_again_leaves_another_refused(tmp_path):
    """Authors replaced wide.mp4 and talk.mp4, and a new original was gathered
    for wide.mp4 alone. Writing the digests again records neither, and
    --rerecord-video-digest wide.mp4 records only that one. Recording every
    row again, as --overwrite-video-digests did, would have encoded talk.mp4's
    stale original over the author's clip."""
    db_path, originals = _scene(tmp_path)
    digests = originals / opt.VIDEO_DIGESTS
    new_wide = _replace_clip(db_path, "wide.mp4", tmp_path)
    _replace_clip(db_path, "talk.mp4", tmp_path, pattern="rgbtestsrc")
    shutil.copy(new_wide, originals / "wide.mp4")
    gathered = digests.read_text()

    output = _record_digests(db_path, originals)
    assert digests.read_text() == gathered, "writing the digests again re-accepted a changed clip"
    assert "--rerecord-video-digest" in output, output
    _record_digests(db_path, originals, rerecord=["wide.mp4"])
    talk = base._snapshot(db_path)["a/media/talk.mp4"]

    code, output = _run(_video_cfg(db_path, originals))

    assert base._snapshot(db_path).get("a/media/talk.mp4") == talk, "talk.mp4's stale original was encoded"
    assert code == 1, output
    assert "a/media/talk.mp4: the stored clip changed since talk.mp4 was gathered" in output, output
    path, _value, data = animated._stored_media(db_path, "a/media/wide")
    assert (path, _streams(data, tmp_path)["video"]["width"]) == ("a/media/wide.webm", 320)


def test_rerecording_a_name_with_no_original_is_refused_by_name(tmp_path):
    """A mistyped name, or one whose original is missing, is named and
    nothing is recorded, rather than the rest being recorded as though done."""
    db_path, originals = _scene(tmp_path)
    digests = originals / opt.VIDEO_DIGESTS
    gathered = digests.read_text()
    (originals / "narrow.mov").unlink()
    _replace_clip(db_path, "wide.mp4", tmp_path)

    code, output = _call(opt.write_video_digests, {"db_path": db_path, "write_video_digests": originals,
                                                   "rerecord_video_digests": ["wide.mp4", "narrow.mov", "wdie.mp4"]})

    assert code == 1, output
    assert "narrow.mov: no original named narrow.mov" in output and "wdie.mp4: no video row" in output, output
    assert digests.read_text() == gathered


def test_adding_an_original_gathered_later_leaves_a_replaced_clip_refused(tmp_path):
    """narrow.mov's original was gathered after the digests were written, and
    meanwhile an author replaced talk.mp4. Writing the digests again records
    narrow.mov and leaves talk.mp4's line alone, so its stale original is
    still refused."""
    db_path, originals = _scene(tmp_path)
    digests = originals / opt.VIDEO_DIGESTS
    digests.write_text("".join(line for line in digests.read_text().splitlines(keepends=True)
                               if not line.startswith("narrow.mov\t")))
    _replace_clip(db_path, "talk.mp4", tmp_path)
    talk = base._snapshot(db_path)["a/media/talk.mp4"]

    _record_digests(db_path, originals)
    code, output = _run(_video_cfg(db_path, originals))

    after = base._snapshot(db_path)
    assert after.get("a/media/talk.mp4") == talk, "talk.mp4's stale original was encoded"
    assert code == 1, output
    assert "a/media/talk.mp4: the stored clip changed since talk.mp4 was gathered" in output, output
    assert "a/media/narrow.webm" in after, "the original gathered later should convert"


def test_an_original_of_another_length_is_left_and_reported(tmp_path):
    """The digest ties an original to its row's clip by name; the length is a
    check on the file itself, such as another clip saved under that name."""
    db_path, originals = _scene(tmp_path)
    # Stored at high quality, so the WebM of either original would be smaller
    # and only the length can keep the row as it is.
    stored = _cut_down(originals / "wide.mp4", tmp_path / "fine.mp4", crf=8)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE Content SET content = ? WHERE path = 'a/media/wide.mp4'", (stored,))
    _record_digests(db_path, originals, rerecord=["wide.mp4"])
    _clip(originals / "wide.mp4", 640, 360, seconds=4)
    longer = (originals / "wide.mp4").read_bytes()
    assert len(opt.encode_video_webm(originals / "wide.mp4", longer, opt.digest(longer), 40,
                                     shutil.which("ffmpeg"), shutil.which("ffprobe"))) < len(stored)
    before = base._snapshot(db_path)

    code, output = _run(_video_cfg(db_path, originals))

    assert code == 1, output
    assert base._snapshot(db_path)["a/media/wide.mp4"] == before["a/media/wide.mp4"]
    assert "a/media/wide.mp4" in output and "not the clip" in output, output


def test_a_webm_no_smaller_than_the_stored_row_leaves_the_row(tmp_path):
    db_path, originals = _scene(tmp_path)
    tiny = _cut_down(originals / "wide.mp4", tmp_path / "tiny.mp4", width=96, crf=51)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE Content SET content = ? WHERE path = 'a/media/wide.mp4'", (tiny,))
    _record_digests(db_path, originals, rerecord=["wide.mp4"])
    before, manifest = base._snapshot(db_path), tmp_path / "manifest.tsv"

    code, output = _run(_video_cfg(db_path, originals, manifest_out=manifest))

    assert code == 0, output
    after = base._snapshot(db_path)
    assert after["a/media/wide.mp4"] == before["a/media/wide.mp4"] and "a/media/wide.webm" not in after
    assert "kept as stored" in output and "a/media/wide.mp4" in output, output
    # Recorded, so a run given this manifest does not spend the encode again.
    assert opt.read_manifest(manifest)["a/media/wide.mp4"][1] == len(tiny)


def test_convert_then_restore_reproduces_the_videos(tmp_path):
    """The bundle holds each row as stored, not its original, so a restore puts
    back exactly the clip the database held - here including one stored over
    several 1 MiB chunks, like constraint-layout-chain.mov."""
    db_path, originals = _scene(tmp_path)
    big = _clip(originals / "big.mov", 640, 360, noise=30)
    with sqlite3.connect(db_path) as conn:
        for number, start in enumerate(range(0, big.stat().st_size, opt.CHUNK_SIZE)):
            conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 28)",
                         ("a/media/big.mov" + (f"-{number}" if number else ""),
                          big.read_bytes()[start:start + opt.CHUNK_SIZE]))
    _record_digests(db_path, originals)
    assert big.stat().st_size > 2 * opt.CHUNK_SIZE
    before, bundle = base._snapshot(db_path), tmp_path / "bundle"

    assert opt.run(_video_cfg(db_path, originals, save_originals=bundle)) == 0
    converted = base._snapshot(db_path)
    assert {"a/media/big.webm", "a/media/wide.webm", "a/media/narrow.webm"} <= set(converted), sorted(converted)

    assert opt.restore_originals(base._optimize_cfg(db_path, restore_originals=bundle)) == 0
    assert base._snapshot(db_path) == before


def test_a_rerun_converts_nothing(tmp_path):
    """Neither the WebMs nor a clip kept because its WebM was larger change on
    a second run - with the first run's manifest, or without one."""
    db_path, originals = _scene(tmp_path)
    tiny = _cut_down(originals / "wide.mp4", tmp_path / "tiny.mp4", width=96, crf=51)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE Content SET content = ? WHERE path = 'a/media/wide.mp4'", (tiny,))
    _record_digests(db_path, originals, rerecord=["wide.mp4"])
    first, second = tmp_path / "first.tsv", tmp_path / "second.tsv"
    assert opt.run(_video_cfg(db_path, originals, manifest_out=first)) == 0
    converted = base._snapshot(db_path)

    code, output = _run(_video_cfg(db_path, originals))
    assert code == 0 and "optimized 0 media file(s)" in output, output
    assert base._snapshot(db_path) == converted

    code, output = _run(_video_cfg(db_path, originals, manifest_in=first, manifest_out=second))
    assert code == 0 and "optimized 0 media file(s)" in output, output
    assert base._snapshot(db_path) == converted
    assert opt.read_manifest(second) == opt.read_manifest(first)


def test_without_video_webm_video_rows_are_neither_read_nor_archived(tmp_path):
    """A run without --video-webm archives and reports what it did before
    video could be converted."""
    db_path, _originals = _scene(tmp_path)
    before, bundle = base._snapshot(db_path), tmp_path / "bundle"

    code, output = _run(base._optimize_cfg(db_path, save_originals=bundle))

    assert code == 0, output
    assert base._snapshot(db_path) == before
    archived = {entry[2] for entry in opt.read_originals_index(bundle / opt.BUNDLE_ORIGINALS_INDEX)}
    assert not archived & set(before), sorted(archived)


def test_video_settings_without_video_webm_are_reported_as_ignored(tmp_path):
    db_path = tmp_path / "documentation.db"
    animated._build_database(db_path, [], VIDEO_TYPES)

    code, output = _run(base._optimize_cfg(db_path, video_originals=tmp_path, video_crf=30))

    assert code == 0, output
    assert "warning: --video-originals, --video-crf has no effect without --video-webm" in output, output


def _optimizer(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(Path(opt.__file__)), *map(str, args)], capture_output=True,
                          text=True)


def test_writing_the_digests_is_refused_as_part_of_a_conversion(tmp_path):
    """Recorded just before converting, the digests would match whatever the
    rows hold by then, stale originals included, so the writer runs on its own
    and refuses the conversion flags rather than ignoring them."""
    db_path, originals = _scene(tmp_path)
    digests = originals / opt.VIDEO_DIGESTS
    digests.unlink()
    before = base._snapshot(db_path)
    for flags in (["--video-webm", "--video-originals", originals], ["--save-originals", tmp_path / "bundle"],
                  ["--restore-originals", tmp_path / "bundle"]):
        result = _optimizer(db_path, "--write-video-digests", originals, *flags)

        assert result.returncode == 2, result
        assert "--write-video-digests runs on its own" in result.stderr, result.stderr
        assert not digests.exists(), f"{flags[0]}: the digests were written"
    assert base._snapshot(db_path) == before


def test_writing_the_digests_takes_no_other_option(tmp_path):
    """The writer changes nothing in the database and has no dry run: under
    --dry-run it used to write the file anyway, and it ignored the optimizing
    and output options without a word. Each is now refused, by name.
    --rerecord-video-digest is the one option it takes."""
    db_path, originals, manifest = tmp_path / "documentation.db", tmp_path / "originals", tmp_path / "m.tsv"
    originals.mkdir()
    (originals / "wide.mp4").write_bytes(b"an original")
    animated._build_database(db_path, [("a/media/wide.mp4", "video/mp4", b"a clip")], VIDEO_TYPES)
    digests = originals / opt.VIDEO_DIGESTS
    for flags in (["--dry-run"], ["--webp"], ["--manifest-out", manifest], ["--types", "video/mp4"],
                  ["--max-width", "100"], ["--verbose"]):
        result = _optimizer(db_path, "--write-video-digests", originals, *flags)

        assert result.returncode == 2, result
        assert "--write-video-digests runs on its own" in result.stderr and flags[0] in result.stderr, result.stderr
        assert not digests.exists() and not manifest.exists(), f"{flags[0]}: a file was written"
    assert _optimizer(db_path, "--write-video-digests", originals).returncode == 0
    result = _optimizer(db_path, "--write-video-digests", originals, "--rerecord-video-digest", "wide.mp4")
    assert result.returncode == 0, result


def test_video_webm_needs_its_originals_and_a_video_type_to_convert(tmp_path):
    """And the digests of the clips they were gathered for, without which no
    original can be trusted. Each is refused before the run starts, with no
    backup made, rather than left to refuse every row."""
    db_path, originals = _scene(tmp_path)
    before, undigested = base._snapshot(db_path), tmp_path / "undigested"
    shutil.copytree(originals, undigested, ignore=shutil.ignore_patterns(opt.VIDEO_DIGESTS))
    for cfg in (_video_cfg(db_path, None), _video_cfg(db_path, tmp_path / "missing"),
                _video_cfg(db_path, undigested), _video_cfg(db_path, originals, types={"image/gif"})):
        code, output = _run(cfg)
        assert code == 1 and "Backing up" not in output, output
        assert base._snapshot(db_path) == before
    code, output = _run(_video_cfg(db_path, originals, types={"video/quicktime"}))
    assert code == 0, output
    after = base._snapshot(db_path)
    assert "a/media/narrow.webm" in after and after["a/media/wide.mp4"] == before["a/media/wide.mp4"]


RENAMES = {"clip.mp4": "clip.webm", "bias.mov": "bias.webm", "spring.gif": "spring.webm", "hero.png": "hero.webp"}
PATTERN = ref.build_pattern(RENAMES)


def _rewrite(text: str) -> tuple:
    rewritten, sites = ref.rewrite_text(text, PATTERN, RENAMES)
    assert ref.undo_rewrite(rewritten, sites, RENAMES) == text, "undo must give back the original exactly"
    return rewritten, [type(old) for _offset, old in sites]


def test_a_source_naming_a_converted_video_declares_webm():
    """Left saying video/mp4, the type would describe a WebM as MP4, and a
    browser that cannot play MP4 skips a source by its type, unfetched."""
    assert _rewrite('<video controls="" muted=""><source src="media/clip.mp4" type="video/mp4"/></video>') == (
        '<video controls="" muted=""><source src="media/clip.webm" type="video/webm"/></video>', [ref.TagEdit])


def test_a_video_reference_with_no_type_is_only_renamed():
    """<video src> has no type, and a <source> without one is not given one."""
    assert _rewrite('<video controls="" src="media/bias.mov" width="30%"></video>') == (
        '<video controls="" src="media/bias.webm" width="30%"></video>', [str])
    assert _rewrite('<video><source src="media/clip.mp4"/></video>') == (
        '<video><source src="media/clip.webm"/></video>', [str])


def test_a_json_escaped_source_keeps_its_escaping():
    """Kotlin's templated pages carry their markup inside JSON strings."""
    assert _rewrite('{"html":"<video><source src=\\"/k/v/clip.mp4\\" type=\\"video/mp4\\"/></video>"}') == (
        '{"html":"<video><source src=\\"/k/v/clip.webm\\" type=\\"video/webm\\"/></video>"}', [ref.TagEdit])


def test_a_source_keeps_everything_but_its_names_and_its_type():
    """Order, quoting and spacing stay as written. A renamed image in the same
    tag is renamed with it, and a codecs parameter naming H.264 goes with the
    old type."""
    assert _rewrite("<source  poster='hero.png' src=clip.mp4 TYPE='video/mp4; codecs=\"avc1\"' >") == (
        "<source  poster='hero.webp' src=clip.webm TYPE='video/webm' >", [ref.TagEdit])


def test_whitespace_after_a_sources_equals_sign_still_sets_its_type():
    """build_pattern finds src= clip.mp4 now, but source_tag_to_webm looked for
    the name only from where the src value starts, after the whitespace such a
    match begins with. It found none there, so the src was renamed and the type
    went on saying video/mp4."""
    assert _rewrite('<source src= clip.mp4 type="video/mp4">') == (
        '<source src= clip.webm type="video/webm">', [ref.TagEdit])
    assert _rewrite('<source type =\tvideo/mp4 src =\n media/clip.mp4>') == (
        '<source type =\tvideo/webm src =\n media/clip.webm>', [ref.TagEdit])


def test_a_source_keeps_its_type_when_the_converted_video_is_not_its_src():
    """Only the src decides: a converted name elsewhere in the tag is renamed
    and the type is left alone."""
    assert _rewrite('<source src="keep.mp4" type="video/mp4" data-x="clip.mp4">') == (
        '<source src="keep.mp4" type="video/mp4" data-x="clip.webm">', [str])


def test_the_first_of_two_type_attributes_is_the_one_set():
    """As in HTML, which ignores a repeated attribute."""
    assert _rewrite('<source src="media/clip.mp4" type="video/mp4" type="x">') == (
        '<source src="media/clip.webm" type="video/webm" type="x">', [ref.TagEdit])


def test_a_source_whose_type_cannot_be_set_is_refused():
    """The ">" inside title ends the tag early, so its attributes cannot be
    parsed. Renaming the src alone, as this used to, left a WebM declared as
    video/mp4, which a browser that cannot play MP4 skips unfetched."""
    with pytest.raises(ref.UnconvertibleReference, match="cannot parse"):
        ref.rewrite_text('<source src="media/clip.mp4" type="video/mp4" title="a > b">', PATTERN, RENAMES)


def test_a_backslash_quote_in_html_is_not_json_escaping():
    """Only markup whose every double quote is escaped is JSON-escaped. One
    literal \\" in an HTML attribute used to make the whole tag count as
    escaped, and every quote in the result came out as \\"."""
    assert _rewrite('<source src="media/clip.mp4" type="video/mp4" title=\'say \\"hi\\"\'>') == (
        '<source src="media/clip.webm" type="video/webm" title=\'say \\"hi\\"\'>', [ref.TagEdit])
    assert _rewrite('<img alt="A" src="spring.gif" title=\'say \\"hi\\"\'>') == (
        '<video autoplay loop muted playsinline role=img aria-label="A" src="spring.webm" '
        'title=\'say \\"hi\\"\'></video>', [ref.TagEdit])
    # An apostrophe is not escaped in JSON, so it leaves a JSON tag escaped.
    assert _rewrite('{"h":"<img alt=\\"it\'s\\" src=\\"spring.gif\\"><source src=\\"clip.mp4\\" '
                    'type=\\"video/mp4\\">"}') == (
        '{"h":"<video autoplay loop muted playsinline role=img aria-label=\\"it\'s\\" '
        'src=\\"spring.webm\\"></video><source src=\\"clip.webm\\" type=\\"video/webm\\">"}',
        [ref.TagEdit, ref.TagEdit])


def test_single_quoted_html_with_an_escaped_quote_inside_stays_literal():
    """Every double quote in these tags sits escaped inside a single-quoted
    value, so they are HTML, not JSON. The <img> used to be read as escaped,
    and its role="img" came out as role=\\"img\\". A single-quoted value in
    markup that is JSON-escaped leaves it escaped."""
    for tag in ("<img src='spring.gif' alt='say \\\"hi\\\"'>",
                "<source src='clip.mp4' type='video/mp4' title='say \\\"hi\\\"'>"):
        assert ref.unescape_tag(tag) == (tag, False)
    assert _rewrite("<img src='spring.gif' alt='say \\\"hi\\\"'>") == (
        "<video autoplay loop muted playsinline src='spring.webm' role=img aria-label='say \\\"hi\\\"'>"
        "</video>", [ref.TagEdit])
    assert _rewrite("{\"h\":\"<img alt=\\\"A\\\" src='spring.gif'>\"}") == (
        "{\"h\":\"<video autoplay loop muted playsinline role=img aria-label=\\\"A\\\" src='spring.webm'>"
        "</video>\"}", [ref.TagEdit])


def test_a_tag_with_no_escaped_quote_is_not_json_escaped():
    """With no \\" in a tag, "every double quote is escaped" holds trivially.
    Such a tag, its attributes unquoted or single-quoted, is HTML, and the
    <video> it becomes keeps plain quotes."""
    for tag in ("<img alt=A src=spring.gif>", "<img alt='A' src='spring.gif'>"):
        assert ref.unescape_tag(tag) == (tag, False)
    assert _rewrite("<img alt='A' src='spring.gif'>") == (
        "<video autoplay loop muted playsinline role=img aria-label='A' src='spring.webm'></video>",
        [ref.TagEdit])


def test_a_converted_video_can_be_referenced_anywhere():
    """A GIF become WebM is refused outside an <img>, the one tag that can be
    turned into a <video>. A video become WebM is still a video, so a link or
    a CSS url() to it is renamed like an image's."""
    assert _rewrite('<a href="media/clip.mp4">download</a> div{background:url("bias.mov")}') == (
        '<a href="media/clip.webm">download</a> div{background:url("bias.webm")}', [str, str])


def test_the_rename_map_follows_video_into_webm(tmp_path):
    """Including a .mov stored over two chunks, as constraint-layout-chain.mov
    is: its continuation row leaves with it rather than being a rename."""
    before_path, after_path = tmp_path / "before.db", tmp_path / "after.db"
    animated._build_database(before_path, [("a/media/clip.mp4", "video/mp4", b"mp4"),
                                           ("a/media/bias.mov", "video/quicktime", b"\x00" * opt.CHUNK_SIZE),
                                           ("a/media/bias.mov-1", "video/quicktime", b"\x00" * 5)], VIDEO_TYPES)
    animated._build_database(after_path, [("a/media/clip.webm", "video/webm", b"\x1aE\xdf\xa3"),
                                          ("a/media/bias.webm", "video/webm", b"\x1aE\xdf\xa4")], VIDEO_TYPES)
    with sqlite3.connect(after_path) as after, sqlite3.connect(before_path) as before:
        assert ref.build_rename_map(after, before) == {"clip.mp4": "clip.webm", "bias.mov": "bias.webm"}


def _reference_cfg(db_path: Path, before: Path, **overrides) -> dict:
    return {"db_path": db_path, "before": before, "dry_run": False, "workers": 2, "verbose": False,
            "save_originals": None, "restore_originals": None, **overrides}


def test_a_video_whose_webm_name_was_taken_is_paired_through_the_manifest(tmp_path):
    """wide.webm was stored beside wide.mp4 already, the layout
    drop_ogv_videos.py describes, so the optimizer stored the new WebM as
    wide-mp4.webm. Paired by stem, wide.mp4 had no replacement, and its page
    kept naming the deleted file with only a note. The optimizer's manifest
    says what each file was converted from; without it the run is refused."""
    page = (b'<video><source src="media/wide.mp4" type="video/mp4"/>'
            b'<source src="media/wide.webm" type="video/webm"/></video>')
    db_path, originals = _scene(tmp_path, [("a/media/wide.webm", "video/webm", b"\x1aE\xdf\xa3 stored before"),
                                           ("a/page.html", "text/html", page)])
    pre_conversion, manifest = tmp_path / "before.db", tmp_path / "manifest.tsv"
    shutil.copy(db_path, pre_conversion)
    assert opt.run(_video_cfg(db_path, originals, manifest_out=manifest)) == 0
    assert "a/media/wide-mp4.webm" in base._snapshot(db_path)
    converted = base._snapshot(db_path)

    code, output = _call(ref.run, _reference_cfg(db_path, pre_conversion))
    assert (code, base._snapshot(db_path)) == (1, converted), output
    assert "a/media/wide.mp4" in output and "--manifest" in output, output

    code, output = _call(ref.run, _reference_cfg(db_path, pre_conversion, manifest=manifest))
    assert code == 0, output
    assert animated._stored_media(db_path, "a/page")[2] == (
        b'<video><source src="media/wide-mp4.webm" type="video/webm"/>'
        b'<source src="media/wide.webm" type="video/webm"/></video>')


def test_a_gif_and_a_video_of_one_stem_each_keep_their_own_webm(tmp_path):
    """foo.gif became foo.webm, so foo.mp4's WebM is foo-mp4.webm. Paired by
    stem, both old names claimed foo.webm, and the page would have shown the
    GIF's animation in place of the video. Through the manifest each finds its
    own file; without it the run is refused rather than guess."""
    page = ("a/page.html", "text/html",
            b'<img alt="Spin" src="m/foo.gif"><video><source src="m/foo.mp4" type="video/mp4"/></video>')
    before_path, db_path, manifest = tmp_path / "before.db", tmp_path / "documentation.db", tmp_path / "manifest.tsv"
    types = VIDEO_TYPES + [(11, "image/gif", "none")]
    animated._build_database(before_path, [("a/m/foo.gif", "image/gif", b"GIF89a"),
                                           ("a/m/foo.mp4", "video/mp4", b"mp4"), page], types)
    from_gif, from_mp4 = b"\x1aE\xdf\xa3 from the gif", b"\x1aE\xdf\xa3 from the mp4"
    animated._build_database(db_path, [("a/m/foo.webm", "video/webm", from_gif),
                                       ("a/m/foo-mp4.webm", "video/webm", from_mp4), page], types)
    opt.write_manifest(manifest, {"a/m/foo.webm": (opt.digest(from_gif), len(from_gif), "a/m/foo.gif"),
                                  "a/m/foo-mp4.webm": (opt.digest(from_mp4), len(from_mp4), "a/m/foo.mp4")})

    code, output = _call(ref.run, _reference_cfg(db_path, before_path))
    assert code == 1 and "a/m/foo.gif" in output and "a/m/foo.mp4" in output, output
    assert animated._stored_media(db_path, "a/page")[2] == page[2], "rewritten with no pairing"

    code, output = _call(ref.run, _reference_cfg(db_path, before_path, manifest=manifest))
    assert code == 0, output
    assert animated._stored_media(db_path, "a/page")[2] == (
        b'<video autoplay loop muted playsinline role=img aria-label="Spin" src="m/foo.webm"></video>'
        b'<video><source src="m/foo-mp4.webm" type="video/webm"/></video>')


def test_a_manifest_naming_a_file_not_stored_leaves_the_rename_unresolved(tmp_path):
    """The manifest says foo.mp4 became foo.webm, but no foo.webm is stored.
    Paired anyway, the pages would be sent to a missing file; foo.mp4 is
    reported as having no replacement, which says what to do about it."""
    before_path, after_path, mp4 = tmp_path / "before.db", tmp_path / "after.db", b"mp4"
    animated._build_database(before_path, [("a/m/foo.mp4", "video/mp4", mp4)], VIDEO_TYPES)
    animated._build_database(after_path, [("a/m/bar.webm", "video/webm", b"\x1aE\xdf\xa3")], VIDEO_TYPES)
    manifest = {"a/m/foo.webm": (opt.digest(b"\x1aE\xdf\xa3"), 4, "a/m/foo.mp4")}

    with sqlite3.connect(after_path) as after, sqlite3.connect(before_path) as before:
        with pytest.raises(ref.UnresolvedRename) as caught:
            ref.build_rename_map(after, before, manifest)

    assert caught.value.paths == ["a/m/foo.mp4"]


def test_a_video_named_in_a_source_that_cannot_be_parsed_is_left_unconverted(tmp_path):
    """The ">" inside title ends each <source> early, so the references step
    cannot parse it and refuses its page. The optimizer converted the video
    anyway, and the page went on naming the deleted file. It now checks first,
    as it does for GIFs: the video stays stored and listed, the run exits
    non-zero, and the page needs no rewrite."""
    pages = [("a/one.html", "text/html", b'<video><source src="media/wide.mp4" type="video/mp4" title="a > b">'
                                         b'</video>'),
             ("a/two.html", "text/html", b'<video><source src="media/narrow.mov" title="a > b"></video>')]
    db_path, originals = _scene(tmp_path, pages)
    pre_conversion, manifest = tmp_path / "before.db", tmp_path / "manifest.tsv"
    shutil.copy(db_path, pre_conversion)
    before = base._snapshot(db_path)

    code, output = _run(_video_cfg(db_path, originals, manifest_out=manifest))

    after = base._snapshot(db_path)
    for path, page in (("a/media/wide.mp4", "a/one.html"), ("a/media/narrow.mov", "a/two.html")):
        assert after.get(path) == before[path], f"{path} converted, though its <source> cannot be rewritten"
        assert f"{path}: named in a <source> whose attributes cannot be parsed, on {page}" in output, output
        assert path not in opt.read_manifest(manifest), "a run after the tag is fixed must convert it"
    assert code == 1, output
    assert "a/media/talk.webm" in after, "the other clips should still convert"
    code, output = _call(ref.run, _reference_cfg(db_path, pre_conversion))
    assert code == 0, output
    assert base._snapshot(db_path) == after


def test_a_manifest_that_does_not_describe_the_stored_file_is_refused(tmp_path):
    """A manifest from an earlier run says foo.webm came from foo.mp4. This run
    found the author's own foo.webm stored and wrote foo-mp4.webm instead.
    Paired through the earlier manifest, the page was pointed at the author's
    file and the run exited 0. Each pairing now needs the manifest's digest
    and size for the new file to match the stored row."""
    page = ("a/page.html", "text/html", b'<video><source src="m/foo.mp4" type="video/mp4"/></video>')
    own, made = b"\x1aE\xdf\xa3 the author's own", b"\x1aE\xdf\xa3 from the mp4"
    before_path, db_path = tmp_path / "before.db", tmp_path / "documentation.db"
    animated._build_database(before_path, [("a/m/foo.mp4", "video/mp4", b"mp4"), ("a/m/foo.webm", "video/webm", own),
                                           page], VIDEO_TYPES)
    animated._build_database(db_path, [("a/m/foo.webm", "video/webm", own), ("a/m/foo-mp4.webm", "video/webm", made),
                                       page], VIDEO_TYPES)
    stale = {"earlier.tsv": {"a/m/foo.webm": (opt.digest(b"\x1aE\xdf\xa3 an older WebM"), 25, "a/m/foo.mp4")},
             "wrong-size.tsv": {"a/m/foo-mp4.webm": (opt.digest(made), len(made) + 1, "a/m/foo.mp4")}}
    for name, entries in stale.items():
        opt.write_manifest(tmp_path / name, entries)

        code, output = _call(ref.run, _reference_cfg(db_path, before_path, manifest=tmp_path / name))

        assert code == 1, f"{name}: {output}"
        assert animated._stored_media(db_path, "a/page")[2] == page[2], f"{name}: rewritten off a stale manifest"
        new_path = next(iter(entries))
        assert f"{new_path} (paired with a/m/foo.mp4)" in output and "nothing was rewritten" in output, output

    this_run = tmp_path / "manifest.tsv"
    opt.write_manifest(this_run, {"a/m/foo-mp4.webm": (opt.digest(made), len(made), "a/m/foo.mp4"),
                                  "a/m/foo.webm": (opt.digest(own), len(own), None)})
    code, output = _call(ref.run, _reference_cfg(db_path, before_path, manifest=this_run))
    assert code == 0, output
    assert animated._stored_media(db_path, "a/page")[2] == (
        b'<video><source src="m/foo-mp4.webm" type="video/webm"/></video>')


def test_convert_rewrite_then_restore_reproduces_the_database(tmp_path):
    """The pipeline over video: each clip becomes WebM, every kind of reference
    follows - a <source> with a type and one without, a <video src>, a link,
    and a Kotlin page's JSON-escaped markup - a second reference pass finds
    nothing left to do, and putting both halves back gives exactly the
    database it started as."""
    db_path, originals = _scene(tmp_path, [
        ("a/one.html", "text/html",
         b'<video controls="" muted=""><source src="media/wide.mp4" type="video/mp4"/>'
         b'<a href="media/wide.mp4">download</a></video>'),
        ("a/two.html", "text/html",
         b'<video controls="" src="media/narrow.mov" width="30%"></video>'
         b'<video><source src="media/talk.mp4"/></video>'),
        ("k/three.html", "text/html",
         b'{"html":"<video><source src=\\"/a/media/talk.mp4\\" type=\\"video/mp4\\"/></video>"}', 2),
    ])
    before, pre_conversion, bundle = base._snapshot(db_path), tmp_path / "before.db", tmp_path / "bundle"
    shutil.copy(db_path, pre_conversion)

    assert opt.run(_video_cfg(db_path, originals, save_originals=bundle)) == 0
    reference_cfg = {"db_path": db_path, "before": pre_conversion, "dry_run": False, "workers": 2,
                     "verbose": False, "save_originals": bundle, "restore_originals": None}
    assert ref.run(reference_cfg) == 0

    assert animated._stored_media(db_path, "a/one")[2] == (
        b'<video controls="" muted=""><source src="media/wide.webm" type="video/webm"/>'
        b'<a href="media/wide.webm">download</a></video>')
    assert animated._stored_media(db_path, "a/two")[2] == (
        b'<video controls="" src="media/narrow.webm" width="30%"></video>'
        b'<video><source src="media/talk.webm"/></video>')
    assert animated._stored_media(db_path, "k/three")[2] == (
        b'{"html":"<video><source src=\\"/a/media/talk.webm\\" type=\\"video/webm\\"/></video>"}')
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT templateId FROM Content WHERE path = 'k/three.html'").fetchone() == (2,)

    rewritten = base._snapshot(db_path)
    assert ref.run({**reference_cfg, "save_originals": None}) == 0
    assert base._snapshot(db_path) == rewritten, "a second pass rewrote something"

    assert opt.restore_originals(base._optimize_cfg(db_path, restore_originals=bundle)) == 0
    assert ref.restore_references({**reference_cfg, "before": None, "save_originals": None,
                                   "restore_originals": bundle}) == 0
    assert base._snapshot(db_path) == before


if __name__ == "__main__":
    sys.exit(base._main(globals()))

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

Covers what ADFA-6596 added to optimize_db_media.py: --video-webm, which turns
each H.264 video row (video/mp4, video/quicktime) into VP9 WebM, encoded from
the original file of the same name in --video-originals and never from the
stored copy.

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
import io
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import optimize_db_media as opt  # noqa: E402
import test_animated_media as animated  # noqa: E402
import test_media_bundle as base  # noqa: E402

VIDEO_TYPES = [(12, "text/html", "brotli"), (25, "video/mp4", "none"), (26, "image/webp", "brotli"),
               (28, "video/quicktime", "none"), (30, "video/webm", "none")]
FFMPEG = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]


def _clip(path: Path, width: int, height: int, seconds: float = 2, audio: bool = False, noise: int = 0) -> Path:
    """An H.264 original from ffmpeg's test pattern at 30 fps, with a 440 Hz
    tone as AAC when `audio`. Noise makes it expensive to store."""
    source = f"testsrc2=size={width}x{height}:rate=30:duration={seconds}" + (f",noise=alls={noise}:allf=t"
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


def _scene(tmp_path: Path) -> tuple:
    """Three clips stored cut down, and a directory of their originals: one
    wider than 480 px, one narrower (a .mov, so video/quicktime is covered),
    and one with sound. Returns (database, originals directory)."""
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
                                       ("a/media/talk.mp4", "video/mp4", _cut_down(talk, work / "talk.mp4"))],
                             VIDEO_TYPES)
    return db_path, originals


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


def _run(cfg: dict) -> tuple:
    """(exit code, everything the run printed)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = opt.run(cfg)
    return code, out.getvalue()


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
    assert (opt.encode_video_webm(original, stored, 40, ffmpeg, ffprobe)
            == opt.encode_video_webm(original, stored, 40, ffmpeg, ffprobe))


def test_a_video_with_no_original_is_left_and_reported(tmp_path):
    """Encoding the stored copy instead would stack a second generation of loss
    on a clip that is lossy already, which is what the originals are for."""
    db_path, originals = _scene(tmp_path)
    (originals / "wide.mp4").unlink()
    before, manifest = base._snapshot(db_path), tmp_path / "manifest.tsv"

    code, output = _run(_video_cfg(db_path, originals, manifest_out=manifest))

    assert code == 0, output
    after = base._snapshot(db_path)
    assert after["a/media/wide.mp4"] == before["a/media/wide.mp4"], "converted with no original"
    assert "a/media/talk.webm" in after, "the other clips should still convert"
    assert "a/media/wide.mp4: no original" in output, output
    assert "a/media/wide.mp4" not in opt.read_manifest(manifest), "a later run with the original must retry it"


def test_an_original_of_another_length_is_left_and_reported(tmp_path):
    """An author may have replaced a clip since its original was saved, and
    encoding the old original over theirs would lose their edit."""
    db_path, originals = _scene(tmp_path)
    # Stored at high quality, so the WebM of either original would be smaller
    # and only the length can keep the row as it is.
    stored = _cut_down(originals / "wide.mp4", tmp_path / "fine.mp4", crf=8)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE Content SET content = ? WHERE path = 'a/media/wide.mp4'", (stored,))
    _clip(originals / "wide.mp4", 640, 360, seconds=4)
    assert len(opt.encode_video_webm(originals / "wide.mp4", (originals / "wide.mp4").read_bytes(), 40,
                                     shutil.which("ffmpeg"), shutil.which("ffprobe"))) < len(stored)
    before = base._snapshot(db_path)

    code, output = _run(_video_cfg(db_path, originals))

    assert code == 0, output
    assert base._snapshot(db_path)["a/media/wide.mp4"] == before["a/media/wide.mp4"]
    assert "a/media/wide.mp4" in output and "not the clip" in output, output


def test_a_webm_no_smaller_than_the_stored_row_leaves_the_row(tmp_path):
    db_path, originals = _scene(tmp_path)
    tiny = _cut_down(originals / "wide.mp4", tmp_path / "tiny.mp4", width=96, crf=51)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE Content SET content = ? WHERE path = 'a/media/wide.mp4'", (tiny,))
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
    first, second = tmp_path / "first.tsv", tmp_path / "second.tsv"
    assert opt.run(_video_cfg(db_path, originals, manifest_out=first)) == 0
    converted = base._snapshot(db_path)

    code, output = _run(_video_cfg(db_path, originals))
    assert code == 0 and "optimized 0 image(s)" in output, output
    assert base._snapshot(db_path) == converted

    code, output = _run(_video_cfg(db_path, originals, manifest_in=first, manifest_out=second))
    assert code == 0 and "optimized 0 image(s)" in output, output
    assert base._snapshot(db_path) == converted
    assert opt.read_manifest(second) == opt.read_manifest(first)


def test_video_webm_needs_its_originals_and_a_video_type_to_convert(tmp_path):
    db_path, originals = _scene(tmp_path)
    before = base._snapshot(db_path)
    for cfg in (_video_cfg(db_path, None), _video_cfg(db_path, tmp_path / "missing"),
                _video_cfg(db_path, originals, types={"image/gif"})):
        assert _run(cfg)[0] == 1
        assert base._snapshot(db_path) == before
    code, output = _run(_video_cfg(db_path, originals, types={"video/quicktime"}))
    assert code == 0, output
    after = base._snapshot(db_path)
    assert "a/media/narrow.webm" in after and after["a/media/wide.mp4"] == before["a/media/wide.mp4"]


if __name__ == "__main__":
    sys.exit(base._main(globals()))

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
# ]
# ///
"""
compare_svg_renders.py

Visual check for svg_images_to_webp.py: renders every SVG row whose content
differs between two documentation databases, before and after, in headless
Chrome, and reports how far the two renders differ (PSNR, in dB; 99 means
identical pixels).

Each SVG is drawn both ways the documentation's pages draw one - through an
<img> (the a/devsite and k/ pages) and through an <object> (i/prefs-editor.html)
- at its viewBox size and a device scale factor of 2, once on a white page and
once on a black one, and the lower PSNR of the two is reported for each way. Chrome loads only file://
pages and is given no route off the machine: every host name resolves to
nothing except 127.0.0.1, and the proxy is a closed local port.

    uv run scripts/compare_svg_renders.py before.db after.db --out renders/ [--chrome PATH]

Exit 1 if a render fails; the PSNR figures are for a person to judge, not a
pass/fail line.
"""
from __future__ import annotations

import argparse
import math
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from optimize_db_media import BrotliCodec, is_continuation_path, load_dictionary, reassemble

SVG_TYPE = "image/svg+xml"
SCALE = 2
MAX_CSS_WIDTH = 1200
MIN_WINDOW = (800, 600)
BACKGROUNDS = {"white": "#ffffff", "black": "#000000"}
# The two ways a documentation page draws an SVG.
EMBEDS = {
    "img": '<img src="{name}" style="display:block;width:{w}px;height:{h}px">',
    "object": '<object data="{name}" type="image/svg+xml" style="display:block;width:{w}px;height:{h}px"></object>',
}
# No route off the machine: names resolve nowhere but loopback, and the proxy is a dead port. No
# --proxy-bypass-list, which would send loopback to the dead proxy as well.
CHROME_FLAGS = ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                "--disable-background-networking", "--disable-component-update", "--disable-sync",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", "--proxy-server=127.0.0.1:9",
                f"--force-device-scale-factor={SCALE}",
                # Without these, the screenshot can be taken before the SVG's embedded image has decoded, and the
                # render shows only part of it.
                "--virtual-time-budget=15000", "--run-all-compositor-stages-before-draw"]


def svg_rows(db: str) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    codec = BrotliCodec(load_dictionary(conn))
    try:
        lengths = {p: n for p, n in conn.execute("SELECT path, LENGTH(content) FROM Content")}
        out = {}
        for path, content, comp in conn.execute(
                "SELECT c.path, c.content, ct.compression FROM Content c JOIN ContentTypes ct "
                "ON ct.id = c.contentTypeID WHERE ct.value = ?", (SVG_TYPE,)):
            if not is_continuation_path(lengths, path):
                out[path] = codec.decompress(reassemble(conn, path, content), comp)
        return out
    finally:
        codec.close()
        conn.close()


def css_size(svg: bytes) -> tuple:
    root = re.search(rb"<svg\b[^>]*>", svg).group(0)
    vb = re.search(rb'viewBox="\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)', root)
    w, h = (float(vb.group(1)), float(vb.group(2))) if vb else (300.0, 150.0)
    k = min(1.0, MAX_CSS_WIDTH / w)
    return max(1, round(w * k)), max(1, round(h * k))


def render(chrome: str, svg_file: Path, size: tuple, background: str, png: Path, profile: Path,
           embed: str = "img") -> None:
    page = svg_file.with_suffix(f".{png.stem}.html")
    page.write_text(f'<!doctype html><html><body style="margin:0;background:{background}">'
                    + EMBEDS[embed].format(name=svg_file.name, w=size[0], h=size[1]) + "</body></html>")
    # A window no bigger than the image clips it (a 398x69 one drew only its top rows), so the window is larger and
    # the screenshot is cropped to the image afterwards.
    window = (max(size[0], MIN_WINDOW[0]) + 20, max(size[1], MIN_WINDOW[1]) + 20)
    subprocess.run([chrome, *CHROME_FLAGS, f"--user-data-dir={profile}", f"--window-size={window[0]},{window[1]}",
                    f"--screenshot={png}", page.as_uri()], check=True, capture_output=True, timeout=120)
    if not png.is_file():
        raise RuntimeError(f"Chrome wrote no screenshot for {svg_file.name}")
    Image.open(png).crop((0, 0, size[0] * SCALE, size[1] * SCALE)).save(png)


def psnr(a: Path, b: Path) -> float:
    x, y = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    if x.size != y.size:
        return 0.0
    mse = sum(v * v for v in ImageStat.Stat(ImageChops.difference(x, y)).rms) / 3
    return 99.0 if mse == 0 else 10 * math.log10(255 * 255 / mse)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("before")
    p.add_argument("after")
    p.add_argument("--out", type=Path, required=True, help="directory for the SVGs and screenshots")
    p.add_argument("--chrome", default=shutil.which("google-chrome") or shutil.which("chromium"))
    args = p.parse_args()
    if not args.chrome:
        print("error: no Chrome found; pass --chrome", file=sys.stderr)
        return 1
    before, after = svg_rows(args.before), svg_rows(args.after)
    changed = sorted(path for path in after if path in before and after[path] != before[path])
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"{len(changed)} SVG row(s) differ.")
    with tempfile.TemporaryDirectory() as profile:
        for n, path in enumerate(changed):
            size = css_size(after[path])
            for side, data in (("before", before[path]), ("after", after[path])):
                (args.out / f"{n}-{side}.svg").write_bytes(data)
            worst = {}
            for embed in EMBEDS:
                worst[embed] = 99.0
                for name, colour in BACKGROUNDS.items():
                    shots = []
                    for side in ("before", "after"):
                        png = args.out / f"{n}-{side}-{embed}-{name}.png"
                        render(args.chrome, args.out / f"{n}-{side}.svg", size, colour, png, Path(profile), embed)
                        shots.append(png)
                    worst[embed] = min(worst[embed], psnr(*shots))
            print(f"  {path}: drawn at {size[0]}x{size[1]} CSS px x{SCALE}; rendered PSNR "
                  + ", ".join(f"{worst[e]:.1f} dB through <{e}>" for e in EMBEDS))
    return 0


if __name__ == "__main__":
    sys.exit(main())

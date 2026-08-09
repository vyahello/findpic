#!/usr/bin/env python3
"""Build the demo photo the README's example output is generated from.

The README used to show output from a `demo.jpg` that lived on one laptop and
was never committed. That made every sample in it unverifiable: when a renderer
changed, nothing could tell you the documented output had gone stale — and one
of them did, silently dropping the "mm" from a lens focal length.

So the demo file is built here instead, from a description, with no real
photograph involved. The coordinates are the Eiffel Tower and the owner is
"Demo Owner": a privacy tool must not demonstrate itself on somebody's home.

    python scripts/make-demo.py            write demo.jpg in the current directory
    python scripts/make-demo.py out.jpg    write it somewhere else

Needs ImageMagick and exiftool, the same two tools the test fixtures use.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

#: The pixels. A gradient at a phone's sensor dimensions — big enough for the
#: megapixel and aspect-ratio lines to say something, small enough to build fast.
GEOMETRY = ("-size", "4032x3024", "gradient:#1f4e79-#e8c07d", "-quality", "92")

#: The story the tags tell: an iPhone photo taken outside the Eiffel Tower, then
#: re-saved by something that is not a camera. That mix is deliberate — it
#: exercises an originality finding, several privacy findings, and a clean
#: structure verdict all at once.
TAGS = (
    "-Make=Apple",
    "-Model=iPhone 15 Pro",
    "-Software=17.5.1",
    "-LensModel=iPhone 15 Pro back triple camera 6.765mm f/1.78",
    "-Artist=Demo Owner",
    "-OwnerName=Demo Owner",
    "-SerialNumber=DEMO-SN-0001",
    "-ImageUniqueID=A1B2C3D4E5F60718",
    "-DateTimeOriginal=2024:07:14 19:42:08",
    "-CreateDate=2024:07:14 19:42:08",
    "-ModifyDate=2024:07:14 19:42:08",
    "-OffsetTimeOriginal=+02:00",
    "-OffsetTime=+02:00",
    "-SubSecTimeOriginal=372",
    "-ISO=64",
    "-FNumber=1.78",
    "-ExposureTime=1/120",
    "-FocalLength=6.765",
    "-FocalLengthIn35mmFormat=24",
    "-Flash=Off, Did not fire",
    "-BrightnessValue=4.2",
    "-GPSLatitude=48.858370",
    "-GPSLatitudeRef=N",
    "-GPSLongitude=2.294481",
    "-GPSLongitudeRef=E",
    "-GPSAltitude=38.4",
    "-GPSAltitudeRef=Above Sea Level",
    "-GPSHPositioningError=6",
    "-GPSImgDirection=291",
    "-GPSImgDirectionRef=T",
    "-GPSSpeed=0",
    "-GPSSpeedRef#=K",
    "-GPSDateStamp=2024:07:14",
    "-GPSTimeStamp=17:42:08",
    "-ExifImageWidth=4032",
    "-ExifImageHeight=3024",
    # "File saved" reports the filesystem's own mtime, so it is pinned here.
    # It still prints in the reader's local timezone — it describes the copy on
    # this disk, not the photograph, and that is the point of the row.
    "-FileModifyDate=2024:07:14 19:42:08+02:00",
)


def imagemagick() -> list[str]:
    """ImageMagick 7 renamed `convert` to `magick`; accept either."""
    for name in ("magick", "convert"):
        found = shutil.which(name)
        if found:
            return [found]
    raise SystemExit("ImageMagick not found — install imagemagick and try again.")


def build(destination: Path) -> Path:
    subprocess.run([*imagemagick(), *GEOMETRY, str(destination)], check=True)
    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", *TAGS, str(destination)],
        check=True,
    )
    return destination


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "demo.jpg")
    print(f"wrote {build(target)}")

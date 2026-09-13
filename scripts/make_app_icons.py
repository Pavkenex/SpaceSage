#!/usr/bin/env python3
"""Render the app icon derivatives from the one SVG source (design §14).

The icon is drawn once, in ``spacesage/app/assets/app-icon.svg``, and this script
turns it into the files the *packaging* needs:

* ``packaging/spacesage.ico`` -- the Windows executable icon (PNG-compressed
  entries, one per size Windows asks for -- 16px in the Explorer list up to the
  256px tile);
* ``docs/assets/app-icon.png`` -- the 256px render the README and the docs show.

Both are committed, so a Windows build never has to render anything, and the
test suite can prove the committed pair still matches the source SVG (a stale
icon is a packaging bug nobody sees until release).

Usage::

    uv run python scripts/make_app_icons.py            # write the files
    uv run python scripts/make_app_icons.py --check    # verify they are current
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SVG_SOURCE = REPO_ROOT / "spacesage" / "app" / "assets" / "app-icon.svg"
ICO_TARGET = REPO_ROOT / "packaging" / "spacesage.ico"
PNG_TARGET = REPO_ROOT / "docs" / "assets" / "app-icon.png"

#: The sizes an .ico should carry: Explorer's list/desk/tile renderings.
ICO_SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)

#: The size of the standalone PNG (GitHub renders it in the README hero).
PNG_SIZE = 256


def render_png(svg_path: Path, size: int) -> bytes:
    """Rasterise ``svg_path`` at ``size`` x ``size`` and return PNG bytes."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(QByteArray(svg_path.read_bytes()))
    if not renderer.isValid():  # pragma: no cover - a broken source is a build error
        raise SystemExit(f"the icon source is not valid SVG: {svg_path}")
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):  # pragma: no cover - Qt can always write PNG
        raise SystemExit(f"could not encode the {size}px render")
    return bytes(buffer.data())


def build_ico(images: list[tuple[int, bytes]]) -> bytes:
    """Pack PNG-encoded ``(size, bytes)`` entries into one .ico container.

    The container is the plain ICONDIR/ICONDIRENTRY layout; the entries hold PNG
    data, which Windows has accepted since Vista and which keeps the 256px entry
    small.  A 256px entry is written as height/width ``0`` -- that is the format's
    way of saying 256.
    """
    header = struct.pack("<HHH", 0, 1, len(images))
    directory = b""
    payload = b""
    offset = len(header) + 16 * len(images)
    for size, data in images:
        dimension = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset)
        payload += data
        offset += len(data)
    return header + directory + payload


def expected_files(svg_path: Path = SVG_SOURCE) -> dict[Path, bytes]:
    """The bytes each committed derivative must contain, rendered from the SVG."""
    return {
        ICO_TARGET: build_ico([(size, render_png(svg_path, size)) for size in ICO_SIZES]),
        PNG_TARGET: render_png(svg_path, PNG_SIZE),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="render the app icon derivatives")
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 when a committed derivative is missing or stale",
    )
    args = parser.parse_args(argv)

    stale: list[Path] = []
    for path, data in expected_files().items():
        if args.check:
            if not path.is_file() or path.read_bytes() != data:
                stale.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"wrote {path.relative_to(REPO_ROOT)} ({len(data)} bytes)")

    if stale:
        for path in stale:
            print(f"stale: {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        print("run: uv run python scripts/make_app_icons.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

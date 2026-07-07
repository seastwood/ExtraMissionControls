"""Generate assets/icon.icns — the app icon.

Draws the tray icon's 2x2 window-grid motif on a dark rounded tile, with a
red ✕ badge on the first cell (the feature). Vector-redrawn at every icon
size, then packed with iconutil.

    .venv/bin/python scripts/make_icon.py
"""

import os
import shutil
import subprocess

from AppKit import (
    NSBezierPath,
    NSBitmapImageFileTypePNG,
    NSBitmapImageRep,
    NSColor,
    NSDeviceRGBColorSpace,
    NSGradient,
    NSGraphicsContext,
)
from Foundation import NSMakeRect, NSMakePoint

ASSETS = os.path.join(os.path.dirname(__file__), "..", "assets")

# All geometry is designed on a 1024pt canvas, bottom-left origin.
_TILE = (100, 100, 824, 824)
_TILE_RADIUS = 185
_CELL = 300.0
_GAP = 48.0
_GRID_ORIGIN = (1024 - (2 * _CELL + _GAP)) / 2  # 188
_CELL_RADIUS = 44
# Overlaps the top-left cell's corner while staying fully inside the tile's
# rounded corner (badge extent must clear the corner arc, or it looks cut off).
_BADGE_CENTER = (226, 798)
_BADGE_RADIUS = 76
_X_HALF = 34.0
_X_WIDTH = 18.0


def _rounded(rect, radius):
    return NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(*rect), radius, radius)


def draw(size):
    s = size / 1024.0

    def r(rect):
        return tuple(v * s for v in rect)

    # Tile background: dark vertical gradient.
    tile = _rounded(r(_TILE), _TILE_RADIUS * s)
    NSGradient.alloc().initWithStartingColor_endingColor_(
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.24, 0.24, 0.27, 1.0),
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.09, 0.09, 0.11, 1.0),
    ).drawInBezierPath_angle_(tile, -90.0)

    # 2x2 grid of "window" cells.
    NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.92).setFill()
    for col in (0, 1):
        for row in (0, 1):
            x = _GRID_ORIGIN + col * (_CELL + _GAP)
            y = _GRID_ORIGIN + row * (_CELL + _GAP)
            _rounded(r((x, y, _CELL, _CELL)), _CELL_RADIUS * s).fill()

    # Red ✕ badge on the top-left cell's corner.
    bx, by = _BADGE_CENTER[0] * s, _BADGE_CENTER[1] * s
    br = _BADGE_RADIUS * s
    NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.372, 0.34, 1.0).setFill()
    NSBezierPath.bezierPathWithOvalInRect_(
        NSMakeRect(bx - br, by - br, 2 * br, 2 * br)).fill()

    NSColor.whiteColor().setStroke()
    for dx1, dy1, dx2, dy2 in ((-1, -1, 1, 1), (-1, 1, 1, -1)):
        stroke = NSBezierPath.bezierPath()
        stroke.setLineWidth_(_X_WIDTH * s)
        stroke.setLineCapStyle_(1)  # round
        stroke.moveToPoint_(NSMakePoint(bx + dx1 * _X_HALF * s,
                                        by + dy1 * _X_HALF * s))
        stroke.lineToPoint_(NSMakePoint(bx + dx2 * _X_HALF * s,
                                        by + dy2 * _X_HALF * s))
        stroke.stroke()


def render_png(size, path):
    rep = NSBitmapImageRep.alloc(). \
        initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, size, size, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(context)
    try:
        draw(size)
    finally:
        NSGraphicsContext.restoreGraphicsState()
    rep.representationUsingType_properties_(
        NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(path, True)


def main():
    os.makedirs(ASSETS, exist_ok=True)
    iconset = os.path.join(ASSETS, "icon.iconset")
    shutil.rmtree(iconset, ignore_errors=True)
    os.makedirs(iconset)

    for points in (16, 32, 128, 256, 512):
        render_png(points, os.path.join(iconset, "icon_%dx%d.png" % (points, points)))
        render_png(points * 2, os.path.join(
            iconset, "icon_%dx%d@2x.png" % (points, points)))

    icns = os.path.join(ASSETS, "icon.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
    shutil.rmtree(iconset)
    print("wrote", os.path.abspath(icns))

    # Standalone PNG for the README header.
    readme_png = os.path.join(ASSETS, "icon_256.png")
    render_png(256, os.path.abspath(readme_png))
    print("wrote", os.path.abspath(readme_png))


if __name__ == "__main__":
    main()

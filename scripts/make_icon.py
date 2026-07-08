"""Generate assets/icon.icns — the app icon.

Draws a window thumbnail (with two faint windows stacked behind it, the
Mission Control "overview" feel) carrying the app's row of control buttons in
its top-left corner: ✕ close (red), − minimize (yellow), ⤢ full screen
(green), ⏻ quit (purple). Vector-redrawn at every icon size, then packed with
iconutil.

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

# Two faint window cards stacked behind the main one (Mission Control overview).
_CARDS = [  # (rect, radius, white alpha)
    ((250, 545, 524, 235), 40, 0.12),
    ((216, 500, 592, 250), 46, 0.22),
]
_WINDOW = (185, 250, 654, 470)
_WINDOW_RADIUS = 56

# The four control buttons, in a row at the window's top-left corner.
_BTN_RADIUS = 58
_BTN_CY = 642
_BTN_CX0 = 280
_BTN_DX = 150
_BUTTONS = [  # (rgb, glyph)
    ((0.99, 0.37, 0.34), "close"),     # red
    ((1.00, 0.74, 0.18), "minimize"),  # yellow
    ((0.22, 0.74, 0.32), "expand"),    # green
    ((0.66, 0.42, 0.94), "power"),     # purple
]


def _rounded(rect, radius):
    return NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(*rect), radius, radius)


def _stroke(pts, width):
    path = NSBezierPath.bezierPath()
    path.setLineWidth_(width)
    path.setLineCapStyle_(1)   # round
    path.setLineJoinStyle_(1)  # round
    path.moveToPoint_(NSMakePoint(*pts[0]))
    for pt in pts[1:]:
        path.lineToPoint_(NSMakePoint(*pt))
    path.stroke()


def _glyph(kind, cx, cy, g, w):
    """Draw a white control glyph centred at (cx, cy); g = half-extent."""
    NSColor.whiteColor().setStroke()
    if kind == "close":
        _stroke([(cx - g, cy - g), (cx + g, cy + g)], w)
        _stroke([(cx - g, cy + g), (cx + g, cy - g)], w)
    elif kind == "minimize":
        _stroke([(cx - g, cy), (cx + g, cy)], w)
    elif kind == "expand":
        # Diagonal double-arrow (bottom-left ↙ / top-right ↗).
        _stroke([(cx - g, cy - g), (cx + g, cy + g)], w)
        a = g * 1.05
        _stroke([(cx + g - a, cy + g), (cx + g, cy + g), (cx + g, cy + g - a)], w)
        _stroke([(cx - g + a, cy - g), (cx - g, cy - g), (cx - g, cy - g + a)], w)
    elif kind == "power":
        # IEC power symbol: ring with a gap at the top + a vertical bar.
        ring = NSBezierPath.bezierPath()
        ring.setLineWidth_(w)
        ring.setLineCapStyle_(1)
        ring.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
            NSMakePoint(cx, cy - g * 0.12), g, 118.0, 62.0)
        ring.stroke()
        _stroke([(cx, cy - g * 0.12), (cx, cy + g * 1.05)], w)


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

    # Faint stacked window cards behind the main window.
    for rect, radius, alpha in _CARDS:
        NSColor.colorWithCalibratedWhite_alpha_(1.0, alpha).setFill()
        _rounded(r(rect), radius * s).fill()

    # Main window.
    NSColor.colorWithCalibratedWhite_alpha_(0.95, 1.0).setFill()
    _rounded(r(_WINDOW), _WINDOW_RADIUS * s).fill()

    # Row of control buttons at its top-left.
    for i, (rgb, kind) in enumerate(_BUTTONS):
        cx = (_BTN_CX0 + i * _BTN_DX) * s
        cy = _BTN_CY * s
        rad = _BTN_RADIUS * s
        NSColor.colorWithCalibratedRed_green_blue_alpha_(
            rgb[0], rgb[1], rgb[2], 1.0).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - rad, cy - rad, 2 * rad, 2 * rad)).fill()
        _glyph(kind, cx, cy, _BTN_RADIUS * 0.46 * s, _BTN_RADIUS * 0.19 * s)


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

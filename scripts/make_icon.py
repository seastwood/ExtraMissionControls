"""Generate assets/icon.icns — the app icon.

The five control buttons — ✕ close (red), − minimize (yellow), ⤢ full screen
(green), ◫ arrange (teal), ⏻ quit (purple) — drawn as colored glass discs laid
out in the interlocking 3-over-2 Olympic-rings formation, sitting in a Liquid
Glass tray. Vector-redrawn at every icon size, then packed with iconutil.

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
_BODY = (100, 100, 824, 824)   # the icon "squircle" (macOS leaves a margin)
_BODY_RADIUS = 185

# Control colors (match the app's traffic-light palette).
_RED = (0.99, 0.37, 0.34)
_YELLOW = (1.00, 0.74, 0.18)
_GREEN = (0.22, 0.74, 0.32)
_TEAL = (0.16, 0.72, 0.70)
_PURPLE = (0.66, 0.42, 0.94)
# macOS dark-mode window background (NSColor.windowBackgroundColor, #1E1E1E).
_DARK_BG = (0.118, 0.118, 0.118)

# Olympic layout: three discs on top, two nestled below in the gaps.
_DISC_R = 92
_CX = 512
_TOP_DX = 206          # top-row centre spacing
_TOP_Y = 566
_BOT_Y = 458
_DISCS = [             # (cx, cy, rgb, glyph) — drawn back-to-front, so the two
                       # bottom-row discs sit behind the three top-row ones.
    (_CX - _TOP_DX / 2, _BOT_Y, _PURPLE, "power"),     # quit, bottom-left
    (_CX + _TOP_DX / 2, _BOT_Y, _TEAL, "arrange"),     # arrange, bottom-right
    (_CX - _TOP_DX, _TOP_Y, _RED, "close"),            # close, top-left
    (_CX,           _TOP_Y, _GREEN, "expand"),         # full screen, top-centre
    (_CX + _TOP_DX, _TOP_Y, _YELLOW, "minimize"),      # minimize, top-right
]

# Glass tray hugging the disc cluster.
_TRAY_PAD = 52
_xs = [d[0] for d in _DISCS]
_ys = [d[1] for d in _DISCS]
_TRAY = (min(_xs) - _DISC_R - _TRAY_PAD,
         min(_ys) - _DISC_R - _TRAY_PAD,
         (max(_xs) - min(_xs)) + 2 * (_DISC_R + _TRAY_PAD),
         (max(_ys) - min(_ys)) + 2 * (_DISC_R + _TRAY_PAD))
_TRAY_RADIUS = 96


def _lighten(rgb, amount):
    return tuple(c + (1.0 - c) * amount for c in rgb)


def _rgba(rgb, alpha=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(
        rgb[0], rgb[1], rgb[2], alpha)


def _rounded(rect, radius):
    return NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(*rect), radius, radius)


def _oval(cx, cy, r):
    return NSBezierPath.bezierPathWithOvalInRect_(
        NSMakeRect(cx - r, cy - r, 2 * r, 2 * r))


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
    elif kind == "arrange":
        # Square split by a vertical line (the ◫ tiling glyph).
        box = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(cx - g, cy - g, 2 * g, 2 * g), g * 0.28, g * 0.28)
        box.setLineWidth_(w)
        box.setLineJoinStyle_(1)
        box.stroke()
        _stroke([(cx, cy - g), (cx, cy + g)], w)
    elif kind == "power":
        # IEC power symbol: ring with a gap at the top + a vertical bar.
        ring = NSBezierPath.bezierPath()
        ring.setLineWidth_(w)
        ring.setLineCapStyle_(1)
        ring.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
            NSMakePoint(cx, cy - g * 0.12), g, 118.0, 62.0)
        ring.stroke()
        _stroke([(cx, cy - g * 0.12), (cx, cy + g * 1.05)], w)


def _draw_disc(cx, cy, r, rgb, kind):
    # Soft contact shadow so the disc reads as sitting on the glass.
    NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.18).setFill()
    _oval(cx, cy - r * 0.06, r * 1.02).fill()
    # Colored body with a top-lit glassy gradient.
    NSGradient.alloc().initWithStartingColor_endingColor_(
        _rgba(_lighten(rgb, 0.28)), _rgba(rgb)).drawInBezierPath_angle_(
            _oval(cx, cy, r), -90.0)
    # Full rim + a brighter specular arc across the top edge.
    ring = _oval(cx, cy, r - r * 0.02)
    ring.setLineWidth_(r * 0.05)
    NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.35).setStroke()
    ring.stroke()
    sheen = NSBezierPath.bezierPath()
    sheen.setLineWidth_(r * 0.06)
    sheen.setLineCapStyle_(1)
    sheen.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
        NSMakePoint(cx, cy), r * 0.9, 52.0, 128.0)
    NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.55).setStroke()
    sheen.stroke()
    _glyph(kind, cx, cy, r * 0.46, r * 0.17)


def _draw_tray(rect, radius):
    x, y, w, h = rect
    # Drop shadow.
    NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.22).setFill()
    _rounded((x, y - h * 0.03, w, h), radius).fill()
    tray = _rounded(rect, radius)
    # Frosted fill: translucent white, brighter at the top.
    NSGradient.alloc().initWithStartingColor_endingColor_(
        NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.30),
        NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.10),
    ).drawInBezierPath_angle_(tray, -90.0)
    # Rim.
    tray.setLineWidth_(max(2.0, h * 0.012))
    NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.45).setStroke()
    tray.stroke()


def draw(size):
    s = size / 1024.0

    def sc(rect):
        return tuple(v * s for v in rect)

    # Background: the flat macOS dark-mode window color.
    body = _rounded(sc(_BODY), _BODY_RADIUS * s)
    _rgba(_DARK_BG).setFill()
    body.fill()

    _draw_tray(sc(_TRAY), _TRAY_RADIUS * s)

    for cx, cy, rgb, kind in _DISCS:
        _draw_disc(cx * s, cy * s, _DISC_R * s, rgb, kind)


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

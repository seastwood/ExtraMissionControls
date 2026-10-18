"""Generate assets/icon.icns and assets/icon_256.png — the app icon.

A miniature Mission Control in monochrome: a graphite square holding the
Spaces Bar (three desktop tiles, the middle one selected) above three window
cards, with EMC's round ✕ button on the active window's corner — the one
thing the app adds. Drawn with Pillow at 4x and downsampled.

    .venv/bin/pip install Pillow
    .venv/bin/python scripts/make_icon.py
"""

import os

from PIL import Image, ImageDraw, ImageFilter

ASSETS = os.path.join(os.path.dirname(__file__), "..", "assets")

S = 4                 # supersampling factor
N = 1024 * S          # canvas size (designed on a 1024 grid)
BODY = (100, 100, 924, 924)   # the icon square (macOS leaves a margin)
BODY_RADIUS = 64


def _box(b):
    return [v * S for v in b]


def _layer():
    im = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    return im, ImageDraw.Draw(im)


def draw():
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))

    # Graphite body with a soft top-to-bottom gradient.
    grad = Image.new("RGBA", (N, N))
    gd = ImageDraw.Draw(grad)
    for y in range(N):
        c = int(50 - 26 * y / N)
        gd.line([(0, y), (N, y)], fill=(c, c, c + 2, 255))
    mask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mask).rounded_rectangle(_box(BODY), radius=BODY_RADIUS * S,
                                           fill=255)
    img.paste(grad, (0, 0), mask)
    edge, ed = _layer()
    ed.rounded_rectangle(_box((101, 101, 923, 923)), radius=(BODY_RADIUS - 1) * S,
                         outline=(255, 255, 255, 30), width=3 * S)
    img.alpha_composite(edge)

    # Spaces Bar: three desktop tiles, the middle one selected.
    bar, bd = _layer()
    tw, th, gap = 156, 96, 36
    x0 = 512 - (3 * tw + 2 * gap) / 2
    for i in range(3):
        x = x0 + i * (tw + gap)
        bd.rounded_rectangle(_box((x, 178, x + tw, 178 + th)), radius=14 * S,
                             fill=(255, 255, 255, 64 if i == 1 else 30))
    x = x0 + (tw + gap)
    bd.rounded_rectangle(_box((x - 8, 170, x + tw + 8, 178 + th + 8)),
                         radius=20 * S, outline=(255, 255, 255, 215), width=6 * S)
    img.alpha_composite(bar)

    def card(b, shade):
        x1, y1, x2, y2 = b
        sh, sd = _layer()
        sd.rounded_rectangle(_box((x1, y1 + 10, x2, y2 + 10)), radius=24 * S,
                             fill=(0, 0, 0, 120))
        img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(14 * S)))
        cl, cd = _layer()
        cd.rounded_rectangle(_box(b), radius=24 * S,
                             fill=(shade, shade, shade + 3, 255))
        top = min(255, shade + 22)
        cd.rounded_rectangle(_box((x1, y1, x2, y1 + 40)), radius=24 * S,
                             fill=(top, top, top + 2, 255))
        cd.rectangle(_box((x1, y1 + 24, x2, y1 + 40)), fill=(top, top, top + 2, 255))
        img.alpha_composite(cl)

    card((556, 352, 824, 572), 92)
    card((556, 612, 824, 832), 104)
    card((200, 352, 516, 832), 158)   # the active window

    # EMC's ✕ button on the active window's corner.
    cx, cy, r = 222, 374, 62
    sh, sd = _layer()
    sd.ellipse(_box((cx - r, cy - r + 10, cx + r, cy + r + 10)), fill=(0, 0, 0, 150))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(12 * S)))
    btn, nd = _layer()
    nd.ellipse(_box((cx - r, cy - r, cx + r, cy + r)), fill=(248, 248, 250, 255))
    k, ink = 25, (30, 30, 32, 255)
    for a, b in (((cx - k, cy - k), (cx + k, cy + k)),
                 ((cx - k, cy + k), (cx + k, cy - k))):
        nd.line([(a[0] * S, a[1] * S), (b[0] * S, b[1] * S)], fill=ink,
                width=14 * S)
        for px, py in (a, b):  # round caps
            nd.ellipse(_box((px - 7, py - 7, px + 7, py + 7)), fill=ink)
    img.alpha_composite(btn)

    return img.resize((1024, 1024), Image.LANCZOS)


def main():
    icon = draw()
    icon.save(os.path.join(ASSETS, "icon.icns"),
              sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256),
                     (512, 512), (1024, 1024)])
    icon.resize((256, 256), Image.LANCZOS).save(
        os.path.join(ASSETS, "icon_256.png"))
    print("wrote assets/icon.icns and assets/icon_256.png")


if __name__ == "__main__":
    main()

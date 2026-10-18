"""Generate the app icon and the menu-bar icon.

App icon (assets/icon.icns, assets/icon_256.png): full-bleed graphite (macOS
rounds it) holding
a 2x2 grid — three grey window tiles and, in the top-left, a
pink-to-violet ring: Mission Control's windows plus the control EMC adds.

Menu-bar icon (assets/tray_icon.png): the same grid and ring with no
background, drawn as a macOS *template* image (black + alpha) — macOS shows
it white on a dark menu bar and dark on a light one.

    .venv/bin/pip install Pillow
    .venv/bin/python scripts/make_icon.py
"""

import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ASSETS = os.path.join(os.path.dirname(__file__), "..", "assets")

S = 4                     # supersampling factor
N = 1024 * S              # app-icon canvas (designed on a 1024 grid)
# Full-bleed: macOS 26+ applies its own rounded-square mask to app icons;
# artwork with its own rounded body and transparent margin gets shrunk onto
# a grey plate instead. So the background fills the whole canvas.
BODY = (0, 0, 1024, 1024)
BODY_RADIUS = 0
TILE, GAP = 350, 64
X0 = 512 - (2 * TILE + GAP) / 2


def _box(b, scale=S):
    return [v * scale for v in b]


def _cell(r, c):
    x, y = X0 + c * (TILE + GAP), X0 + r * (TILE + GAP)
    return x, y, x + TILE, y + TILE


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def _diag(c1, c2):
    """Diagonal gradient, top-left -> bottom-right."""
    small = Image.new("RGBA", (256, 256))
    px = small.load()
    for y in range(256):
        for x in range(256):
            px[x, y] = _lerp(c1, c2, (x + y) / 510) + (255,)
    return small.resize((N, N), Image.BICUBIC)


def _grad(c1, c2, horizontal=False):
    g = Image.new("RGBA", (256, 1) if horizontal else (1, 256))
    for i in range(256):
        g.putpixel((i, 0) if horizontal else (0, i), _lerp(c1, c2, i / 255) + (255,))
    return g.resize((N, N), Image.BICUBIC)


def _mask(draw_fn):
    m = Image.new("L", (N, N), 0)
    draw_fn(ImageDraw.Draw(m))
    return m


def _shadow(img, mask, dy=12, blur=16, alpha=110):
    sh = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    sh.putalpha(mask.point(lambda v: v * alpha // 255))
    sh = sh.transform((N, N), Image.AFFINE, (1, 0, 0, 0, 1, -dy * S))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(blur * S)))


def app_icon():
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    body = _mask(lambda d: d.rounded_rectangle(_box(BODY), radius=BODY_RADIUS * S,
                                               fill=255))
    img.paste(_diag((62, 63, 70), (16, 16, 20)), (0, 0), body)

    # Window tiles: solid greys, lit from the top, with soft shadows.
    for (r, c), (top, bottom) in (((0, 1), ((128, 128, 136), (100, 100, 108))),
                                  ((1, 0), ((92, 92, 100), (68, 68, 75))),
                                  ((1, 1), ((176, 176, 184), (146, 146, 154)))):
        box = _cell(r, c)
        m = _mask(lambda d, b=box: d.rounded_rectangle(_box(b), radius=52 * S,
                                                       fill=255))
        _shadow(img, m, dy=12, blur=16, alpha=100)
        tile = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        tile.paste(_grad(top, bottom), (0, 0), m)
        img.alpha_composite(tile)
        x1, y1, x2, y2 = box
        edge = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        ImageDraw.Draw(edge).rounded_rectangle(
            _box((x1 + 2, y1 + 2, x2 - 2, y2 - 2)), radius=50 * S,
            outline=(255, 255, 255, 60), width=3 * S)
        fade = _mask(lambda d, y=y1, a=x1, b=x2: d.rectangle(
            _box((a, y, b, y + 70)), fill=255)).filter(ImageFilter.GaussianBlur(24 * S))
        edge.putalpha(ImageChops.multiply(edge.getchannel("A"), fade))
        img.alpha_composite(edge)

    # The ring: pink -> violet gradient with a soft drop shadow.
    x1, y1, x2, y2 = _cell(0, 0)
    ring = _mask(lambda d: d.ellipse(_box((x1 + 21, y1 + 21, x2 - 21, y2 - 21)),
                                     outline=255, width=56 * S))
    _shadow(img, ring, dy=10, blur=12, alpha=120)
    fill = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    fill.paste(_grad((255, 96, 140), (128, 108, 255), horizontal=True), (0, 0), ring)
    img.alpha_composite(fill)
    sheen = Image.new("RGBA", (N, N), (255, 255, 255, 0))
    top_half = _mask(lambda d: d.rectangle(_box((x1, y1, x2, (y1 + y2) / 2)),
                                           fill=70)).filter(ImageFilter.GaussianBlur(30 * S))
    sheen.putalpha(ImageChops.multiply(ring, top_half))
    img.alpha_composite(sheen)

    return img.resize((1024, 1024), Image.LANCZOS)


def tray_icon(px=36):
    """Template image: 18pt menu-bar icon at @2x (36px)."""
    k = 16  # supersample
    n = px * k
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    margin, gap = 2.0, 2.5
    tile = (px - 2 * margin - gap) / 2
    ink = (0, 0, 0, 255)

    def cell(r, c):
        x = margin + c * (tile + gap)
        y = margin + r * (tile + gap)
        return [x * k, y * k, (x + tile) * k, (y + tile) * k]

    for r, c in ((0, 1), (1, 0), (1, 1)):
        d.rounded_rectangle(cell(r, c), radius=3.2 * k, fill=ink)
    x1, y1, x2, y2 = cell(0, 0)
    inset = 0.6 * k
    d.ellipse([x1 + inset, y1 + inset, x2 - inset, y2 - inset],
              outline=ink, width=int(2.6 * k))
    return img.resize((px, px), Image.LANCZOS)


def main():
    icon = app_icon()
    icon.save(os.path.join(ASSETS, "icon.icns"),
              sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256),
                     (512, 512), (1024, 1024)])
    icon.resize((256, 256), Image.LANCZOS).save(
        os.path.join(ASSETS, "icon_256.png"))
    tray_icon().save(os.path.join(ASSETS, "tray_icon.png"))
    print("wrote assets/icon.icns, assets/icon_256.png, assets/tray_icon.png")


if __name__ == "__main__":
    main()

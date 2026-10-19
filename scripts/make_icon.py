"""Generate the app icon and the menu-bar icon.

App icon (assets/icon.icns): full-bleed graphite (macOS rounds it) holding
a 2x2 grid — three grey window tiles and, in the top-left, a
pink-to-violet mark: Mission Control's windows plus the control EMC adds.
The mark is an "E", with an "M" and a squared "C" on the bottom tiles
(E-M-C); EMC_ICON_MARK=ring draws the original ring and plain tiles.

README image (assets/icon_256.png): the same icon with the macOS rounded
shape baked in, since GitHub shows it as-is.

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
MARK = os.environ.get("EMC_ICON_MARK", "E").lower()   # "e" or "ring"
STROKE = 56  # the mark's line weight (1024 grid)
# The mark's colour: (from, to, vertical). Pink -> violet across the icon, or
# a gold from light at the top of the E to deep at its foot.
MARK_PALETTES = {
    "pink": ((255, 96, 140), (128, 108, 255), False),
    "gold": ((255, 222, 120), (206, 142, 36), True),
    "champagne": ((246, 224, 168), (190, 150, 82), True),
    "amber": ((255, 204, 84), (224, 120, 28), True),
}
MARK_COLORS = MARK_PALETTES[os.environ.get("EMC_ICON_COLOR", "gold")]
LETTER_INSET = 0    # M / C reach their tiles' edges
LETTER_STROKE = STROKE
LETTER_GREYS = ((160, 160, 168), (134, 134, 142))  # M / C: monochrome mid grey
LETTER_RIM = 0      # alpha of a light rim around M / C (0 = none)
TILE_SHADES = {     # (row, col): (top, bottom) grey of each window tile
    (0, 1): ((128, 128, 136), (100, 100, 108)),
    (1, 0): ((92, 92, 100), (68, 68, 75)),
    (1, 1): ((176, 176, 184), (146, 146, 154)),
}
if MARK != "ring":
    # Dark panels under the M and C so the mid-grey letters stand out.
    TILE_SHADES[(1, 0)] = TILE_SHADES[(1, 1)] = ((62, 62, 70), (46, 46, 52))
X0 = 512 - (2 * TILE + GAP) / 2


def _letter_bars(letter, x1, y1, x2, y2, stroke, scale, inset=0.0):
    """A blocky letter as rounded bars filling a cell (less `inset`), with
    the ring's line weight:
      E — spine + three arms, the middle arm a touch shorter;
      M — the same E turned 90° clockwise (a top bar with three legs);
      C — a squared C (spine + top and bottom arms)."""
    x1, y1, x2, y2 = x1 + inset, y1 + inset, x2 - inset, y2 - inset
    w, h = x2 - x1, y2 - y1
    t = stroke
    if letter == "M":
        bars = [(x1, y1, x2, y1 + t),                                  # top
                (x1, y1, x1 + t, y2),                                  # left
                (x1 + (w - t) / 2, y1, x1 + (w + t) / 2, y1 + h * 0.84),
                (x2 - t, y1, x2, y2)]                                  # right
    else:
        bars = [(x1, y1, x1 + t, y2),                                  # spine
                (x1, y1, x2, y1 + t),                                  # top arm
                (x1, y2 - t, x2, y2)]                                  # bottom
        if letter == "E":
            bars.append((x1, y1 + (h - t) / 2, x1 + w * 0.84, y1 + (h + t) / 2))
    return [([v * scale for v in b], t / 2 * scale) for b in bars]


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


def _grad_span(c1, c2, a, b, vertical=False):
    """c1 -> c2 between a and b (1024 grid) along one axis, flat outside."""
    g = Image.new("RGBA", (1, 1024) if vertical else (1024, 1))
    for i in range(1024):
        t = min(1.0, max(0.0, (i - a) / float(b - a)))
        g.putpixel((0, i) if vertical else (i, 0), _lerp(c1, c2, t) + (255,))
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
    for (r, c), (top, bottom) in TILE_SHADES.items():
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

    # The mark: pink -> violet gradient with a soft drop shadow.
    x1, y1, x2, y2 = _cell(0, 0)
    if MARK == "ring":
        ring = _mask(lambda d: d.ellipse(_box((x1 + 21, y1 + 21, x2 - 21, y2 - 21)),
                                         outline=255, width=STROKE * S))
    else:
        def draw_e(d):
            for box, radius in _letter_bars("E", x1, y1, x2, y2, STROKE, S):
                d.rounded_rectangle(box, radius=radius, fill=255)
        ring = _mask(draw_e)
    _shadow(img, ring, dy=10, blur=12, alpha=120)
    fill = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    c1, c2, vertical = MARK_COLORS
    if vertical:   # spans the mark's own cell, top to bottom (metallic)
        fill.paste(_grad_span(c1, c2, y1, y2, vertical=True), (0, 0), ring)
    else:          # spans the whole icon, left to right (as the ring had it)
        fill.paste(_grad(c1, c2, horizontal=True), (0, 0), ring)
    img.alpha_composite(fill)
    sheen = Image.new("RGBA", (N, N), (255, 255, 255, 0))
    top_half = _mask(lambda d: d.rectangle(_box((x1, y1, x2, (y1 + y2) / 2)),
                                           fill=70)).filter(ImageFilter.GaussianBlur(30 * S))
    sheen.putalpha(ImageChops.multiply(ring, top_half))
    img.alpha_composite(sheen)

    if MARK != "ring":
        # "M" and "C" on the bottom tiles, monochrome (only the E carries
        # colour), their outer edges on the tiles' edges.
        for letter, cell in (("M", _cell(1, 0)), ("C", _cell(1, 1))):
            m = _mask(lambda d, c=cell, l=letter: [
                d.rounded_rectangle(box, radius=radius, fill=255)
                for box, radius in _letter_bars(l, *c, LETTER_STROKE, S,
                                                inset=LETTER_INSET)])
            _shadow(img, m, dy=6, blur=8, alpha=110)
            ink = Image.new("RGBA", (N, N), (0, 0, 0, 0))
            ink.paste(_grad(*LETTER_GREYS), (0, 0), m)
            img.alpha_composite(ink)
            if not LETTER_RIM:
                continue
            # A faint light rim, like the tiles' top edge.
            rim = ImageChops.subtract(m, m.filter(ImageFilter.MinFilter(4 * S + 1)))
            edge = Image.new("RGBA", (N, N), (255, 255, 255, 0))
            edge.putalpha(rim.point(lambda v: v * LETTER_RIM // 255))
            img.alpha_composite(edge)

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

    lettered = MARK != "ring"
    for r, c in ((0, 1), (1, 0), (1, 1)):
        # Template images are alpha only: under a letter the square is faint
        # so the full-strength letter (edge to edge) reads on top of it.
        faint = lettered and r == 1
        d.rounded_rectangle(cell(r, c), radius=3.2 * k,
                            fill=(0, 0, 0, 90) if faint else ink)
    if lettered:
        for letter, (r, c) in (("M", (1, 0)), ("C", (1, 1))):
            x1, y1, x2, y2 = cell(r, c)
            for box, radius in _letter_bars(letter, x1 / k, y1 / k, x2 / k,
                                            y2 / k, 2.6, k):
                d.rounded_rectangle(box, radius=radius, fill=ink)
    x1, y1, x2, y2 = cell(0, 0)
    if MARK == "ring":
        inset = 0.6 * k
        d.ellipse([x1 + inset, y1 + inset, x2 - inset, y2 - inset],
                  outline=ink, width=int(2.6 * k))
    else:
        for box, radius in _letter_bars("E", x1 / k, y1 / k, x2 / k, y2 / k, 2.6, k):
            d.rounded_rectangle(box, radius=radius, fill=ink)
    return img.resize((px, px), Image.LANCZOS)


def _squircle_mask(size, k=4):
    """macOS's app-icon shape: a superellipse (|x|^5 + |y|^5 = 1), drawn at
    k-times resolution and downsampled for smooth edges."""
    n = size * k
    small = Image.new("L", (n, n), 0)
    px = small.load()
    half = n / 2.0
    exp = 5.0
    for y in range(n):
        dy = abs((y + 0.5 - half) / half) ** exp
        if dy >= 1.0:
            continue
        span = (1.0 - dy) ** (1.0 / exp) * half
        x0, x1 = int(round(half - span)), int(round(half + span))
        for x in range(max(0, x0), min(n, x1)):
            px[x, y] = 255
    return small.resize((size, size), Image.LANCZOS)


def main():
    icon = app_icon()
    icon.save(os.path.join(ASSETS, "icon.icns"),
              sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256),
                     (512, 512), (1024, 1024)])
    # README / web copy: macOS rounds the app icon itself, but GitHub shows
    # the file as-is, so this one gets the macOS icon shape baked in.
    rounded = Image.new("RGBA", icon.size, (0, 0, 0, 0))
    rounded.paste(icon, (0, 0), _squircle_mask(icon.size[0]))
    rounded.resize((256, 256), Image.LANCZOS).save(
        os.path.join(ASSETS, "icon_256.png"))
    tray_icon().save(os.path.join(ASSETS, "tray_icon.png"))
    print("wrote assets/icon.icns, assets/icon_256.png, assets/tray_icon.png")


if __name__ == "__main__":
    main()

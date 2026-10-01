"""Make Atomic's app icon and sidebar mark from the owner's icon.

    py -3.13 packaging/make_brand.py

Source: packaging/brand_source.png - the owner's "Atomic Icon" (1 October
2026: "use the new Icon I added in the dir as app icon and as the Icon on
the sidebar"), a 1254px RGB picture of a black rounded tile carrying a
white "A" with a play triangle cut into it, on a white ground.

Writes:
  src/assets/app_icon.ico    - the tile, with the white ground around it
                               made transparent, at 16-256px.
  src/assets/atomic_icon.png - the white "A" alone, for the sidebar and
                               the splash. Those recolour the picture to
                               theme.ACCENT and keep only its alpha
                               (images.tinted_asset), so the mark is
                               handed over as alpha: the A's own white.

**The ground is found by flood-filling from the corners**, not by
brightness alone: the A is as white as the ground, and only the white
that touches the picture's edges is ground. Edge pixels inside that
region keep a partial alpha from their own grey, so the tile's outline
stays antialiased.
"""
import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "packaging", "brand_source.png")
ASSETS = os.path.join(ROOT, "src", "assets")

# The tile's ink - what a ground pixel at the tile's edge is drawn in
# once its alpha carries the antialiasing.
TILE_INK = (10, 10, 10)
# Brightness above which a pixel counts as white (ground or the A).
WHITE = 128
# How far in from the tile's own edge the A is looked for, so the tile's
# outline never counts as part of the mark.
MARK_INSET = 120


def _ground(lum):
    """L mask: 255 where the pixel is ground (white touching the edges)."""
    white = lum.point(lambda v: 255 if v > WHITE else 0)
    marked = white.copy()
    w, h = marked.size
    for corner in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        if marked.getpixel(corner) == 255:
            ImageDraw.floodfill(marked, corner, 128)
    return marked.point(lambda v: 255 if v == 128 else 0)


def tile():
    src = Image.open(SOURCE).convert("RGB")
    lum = src.convert("L")
    ground = _ground(lum)
    # Opaque everywhere but the ground; on the ground, as opaque as the
    # pixel is dark - which is only its antialiased edge with the tile.
    inverse = lum.point(lambda v: 255 - v)
    alpha = Image.composite(inverse, Image.new("L", lum.size, 255), ground)
    out = src.convert("RGBA")
    edge = Image.new("RGBA", src.size, TILE_INK + (255,))
    out = Image.composite(edge, out, ground)
    out.putalpha(alpha)
    box = alpha.point(lambda v: 255 if v > 8 else 0).getbbox()
    out = out.crop(box)
    side = max(out.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.alpha_composite(out, ((side - out.width) // 2, (side - out.height) // 2))
    return square


def mark():
    src = Image.open(SOURCE).convert("RGB")
    lum = src.convert("L")
    ground = _ground(lum)
    tile_box = ground.point(lambda v: 0 if v else 255).getbbox()
    left, top, right, bottom = tile_box
    inner = (left + MARK_INSET, top + MARK_INSET, right - MARK_INSET, bottom - MARK_INSET)
    # The A is white on the black tile. The tile's own near-black is not
    # nothing to the tint (it came through as a faint square on the first
    # render), so dark reads as zero; and the ground inside the crop - the
    # white in the tile's rounded corners - is taken out before the crop
    # is measured, or its specks set the bounds.
    alpha = lum.point(lambda v: 0 if v < 40 else v)
    alpha = Image.composite(Image.new("L", lum.size, 0), alpha, ground).crop(inner)
    box = alpha.point(lambda v: 255 if v > 24 else 0).getbbox()
    alpha = alpha.crop(box)
    shape = Image.new("RGBA", alpha.size, (255, 255, 255, 0))
    shape.putalpha(alpha)
    # The sidebar reads a 3:2 picture (main._build_sidebar takes the
    # aspect from this file); the mark is centred in it at full height.
    h = 1024
    w = round(h * 1.5)
    scale = h * 0.94 / shape.height
    shape = shape.resize((round(shape.width * scale), round(shape.height * scale)),
                         Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    canvas.alpha_composite(shape, ((w - shape.width) // 2, (h - shape.height) // 2))
    return canvas


if __name__ == "__main__":
    icon = tile().resize((256, 256), Image.Resampling.LANCZOS)
    icon.save(os.path.join(ASSETS, "app_icon.ico"),
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    mark().save(os.path.join(ASSETS, "atomic_icon.png"))
    print("wrote app_icon.ico, atomic_icon.png")

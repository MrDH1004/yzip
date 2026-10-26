"""Generate the final yzip Vacuum Seal icon using Pillow only.

Usage: python make_icon.py [preview.png]
"""
from __future__ import annotations

import io
import math
import struct
import sys
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
SMALL = frozenset((16, 20, 24, 32))
LANCZOS = Image.Resampling.LANCZOS


def mix(a: Sequence[int], b: Sequence[int], t: float) -> tuple[int, ...]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def rounded_mask(size, box, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    return mask


def polygon_mask(size, points, soften=0):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).polygon(points, fill=255)
    if soften:
        mask = mask.filter(ImageFilter.GaussianBlur(soften))
        mask = mask.point(lambda v: 255 if v >= 128 else 0)
    return mask


def gradient(size, stops, diagonal=0.0, radial=None):
    """Render a compact smooth RGBA gradient and enlarge it."""
    w, h = size
    rw, rh = min(w, 256), min(h, 256)
    image = Image.new("RGBA", (rw, rh))
    pixels = image.load()
    for y in range(rh):
        for x in range(rw):
            xn, yn = x / max(1, rw - 1), y / max(1, rh - 1)
            t = max(0.0, min(1.0, yn + diagonal * (xn - .5)))
            color = stops[-1][1]
            for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
                if t <= p1:
                    color = mix(c0, c1, (t - p0) / max(.0001, p1 - p0))
                    break
            if radial:
                cx, cy, radius, glow_color, amount = radial
                light = max(0.0, 1.0 - math.hypot(xn - cx, yn - cy) / radius) * amount
                color = mix(color, glow_color, light)
            pixels[x, y] = color
    return image if image.size == size else image.resize(size, Image.Resampling.BICUBIC)


def composite_mask(dst, fill, mask):
    layer = Image.new("RGBA", dst.size, fill) if isinstance(fill, tuple) else fill.copy()
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
    dst.alpha_composite(layer)


def offset_mask(mask, dx, dy):
    shifted = Image.new("L", mask.size, 0)
    shifted.paste(mask, (dx, dy))
    return shifted


def shadow(dst, mask, dx, dy, blur, color):
    alpha = offset_mask(mask, dx, dy).filter(ImageFilter.GaussianBlur(blur))
    alpha = alpha.point(lambda v: v * color[3] // 255)
    layer = Image.new("RGBA", dst.size, (*color[:3], 0))
    layer.putalpha(alpha)
    dst.alpha_composite(layer)


def tile(n, unit=1, small=False):
    """Lifted graphite tile with transparent corners and a restrained rim."""
    out = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    side = n // unit
    inset = (1 if side <= 20 else 2) * unit if small else round(n * .065)
    box = (inset, inset, n - inset - unit, n - inset - unit)
    radius = (4 if side <= 20 else 6) * unit if small else round(n * .205)
    mask = rounded_mask(out.size, box, radius)
    shadow(out, mask, 0, max(unit, round(n * .025)), max(unit * .70, n * .038), (0, 0, 0, 125))
    bg = gradient(out.size,
        ((0, (69, 72, 76, 255)), (.38, (47, 50, 54, 255)), (1, (27, 29, 33, 255))),
        .08, (.24, .10, .72, (117, 122, 128, 255), .23))
    composite_mask(out, bg, mask)

    highlight = Image.new("L", out.size, 0)
    ImageDraw.Draw(highlight).ellipse((n * .08, -n * .16, n * .88, n * .44), fill=54)
    highlight = ImageChops.multiply(highlight.filter(ImageFilter.GaussianBlur(max(unit, n * .09))), mask)
    sheen = Image.new("RGBA", out.size, (255, 255, 255, 0))
    sheen.putalpha(highlight)
    out.alpha_composite(sheen)

    edge = Image.new("RGBA", out.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(edge)
    ew = unit if small else max(1, round(n * .007))
    draw.rounded_rectangle(box, radius=radius, outline=(180, 185, 190, 82), width=ew)
    out.alpha_composite(edge)
    return out


def draw_tapered_crease(dst, points, width, dark_alpha=86, light_alpha=54):
    """A soft, tapered tension fold formed from overlapping solid segments."""
    under = Image.new("RGBA", dst.size, (0, 0, 0, 0))
    ud = ImageDraw.Draw(under)
    count = len(points) - 1
    for i, (a, b) in enumerate(zip(points, points[1:])):
        w = max(1, round(width * (1.0 - .58 * i / max(1, count))))
        ud.line((a, b), fill=(16, 19, 20, dark_alpha), width=w, joint="curve")
        r = w // 2
        ud.ellipse((b[0] - r, b[1] - r, b[0] + r, b[1] + r), fill=(16, 19, 20, dark_alpha))
    dst.alpha_composite(under.filter(ImageFilter.GaussianBlur(max(1, width * .13))))
    ridge = Image.new("RGBA", dst.size, (0, 0, 0, 0))
    rd = ImageDraw.Draw(ridge)
    shifted = [(x - max(1, width // 5), y - max(1, width // 6)) for x, y in points]
    rd.line(shifted, fill=(246, 249, 242, light_alpha), width=max(1, width // 3), joint="curve")
    dst.alpha_composite(ridge.filter(ImageFilter.GaussianBlur(max(.5, width * .08))))


def render_large(size):
    ss = 4
    n = size * ss
    out = tile(n)
    P = lambda x, y: (round(x * n), round(y * n))
    pouch_points = [
        P(.245, .300), P(.455, .265), P(.675, .285), P(.755, .365),
        P(.735, .555), P(.755, .690), P(.665, .755), P(.455, .742),
        P(.300, .755), P(.235, .665), P(.255, .500), P(.220, .390),
    ]
    pouch = polygon_mask(out.size, pouch_points, max(1, round(n * .004)))
    shadow(out, pouch, 0, round(n * .025), n * .025, (0, 0, 0, 150))
    film = gradient(out.size,
        ((0, (220, 225, 220, 178)), (.30, (160, 167, 164, 166)),
         (.62, (105, 113, 112, 176)), (1, (62, 69, 70, 205))),
        .10, (.34, .28, .55, (250, 252, 247, 220), .30))
    composite_mask(out, film, pouch)

    spec = Image.new("L", out.size, 0)
    ImageDraw.Draw(spec).ellipse((n * .255, n * .255, n * .675, n * .445), fill=96)
    spec = ImageChops.multiply(spec.filter(ImageFilter.GaussianBlur(n * .030)), pouch)
    spec_layer = Image.new("RGBA", out.size, (255, 255, 250, 0))
    spec_layer.putalpha(spec)
    out.alpha_composite(spec_layer)

    payload_box = (round(n * .398), round(n * .405), round(n * .612), round(n * .625))
    glow = Image.new("RGBA", out.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((n * .345, n * .350, n * .665, n * .680), fill=(174, 255, 41, 110))
    out.alpha_composite(glow.filter(ImageFilter.GaussianBlur(n * .040)))
    payload = rounded_mask(out.size, payload_box, round(n * .065))
    shadow(out, payload, round(n * .008), round(n * .018), n * .018, (20, 28, 10, 125))
    lime = gradient(out.size,
        ((0, (225, 255, 101, 255)), (.34, (181, 246, 48, 255)),
         (.70, (131, 210, 24, 255)), (1, (77, 146, 14, 255))),
        .12, (.44, .43, .25, (246, 255, 174, 255), .55))
    composite_mask(out, lime, payload)

    target = {"tl": P(.415, .425), "tr": P(.595, .425), "br": P(.600, .605), "bl": P(.412, .605)}
    crease_specs = (
        ([P(.275, .335), P(.340, .370), target["tl"]], .024),
        ([P(.478, .290), P(.470, .355), target["tl"]], .020),
        ([P(.690, .335), P(.655, .375), target["tr"]], .024),
        ([P(.715, .525), P(.665, .545), target["br"]], .018),
        ([P(.685, .700), P(.650, .655), target["br"]], .025),
        ([P(.465, .712), P(.455, .665), target["bl"]], .019),
        ([P(.285, .690), P(.350, .650), target["bl"]], .025),
        ([P(.260, .505), P(.340, .520), target["tl"]], .017),
    )
    for points, width in crease_specs:
        draw_tapered_crease(out, points, max(2, round(n * width)))

    cap = Image.new("RGBA", out.size, (0, 0, 0, 0))
    cd = ImageDraw.Draw(cap)
    cd.rounded_rectangle(payload_box, radius=round(n * .065), fill=(232, 243, 224, 22),
                         outline=(245, 255, 230, 95), width=max(1, round(n * .009)))
    cd.arc((n * .414, n * .420, n * .592, n * .590), 194, 304,
           fill=(255, 255, 225, 170), width=max(1, round(n * .012)))
    out.alpha_composite(cap)

    seam = Image.new("RGBA", out.size, (0, 0, 0, 0))
    se = ImageDraw.Draw(seam)
    se.line(pouch_points + [pouch_points[0]], fill=(242, 245, 239, 142),
            width=max(2, round(n * .013)), joint="curve")
    inset_points = [P(.270, .323), P(.458, .293), P(.657, .310), P(.722, .378),
                    P(.706, .552), P(.722, .674), P(.650, .722), P(.458, .711),
                    P(.316, .724), P(.270, .650), P(.288, .505), P(.255, .405), P(.270, .323)]
    se.line(inset_points, fill=(37, 42, 42, 78), width=max(1, round(n * .006)), joint="curve")
    out.alpha_composite(seam)
    return out.resize((size, size), LANCZOS)


def render_small(size):
    """Pixel-snapped solid pouch/core variants for 16/20/24/32 px."""
    ss = 8
    n = size * ss
    out = tile(n, ss, True)
    if size == 16:
        pouch_xy, core = [(4, 5), (11, 4), (13, 6), (12, 12), (5, 12), (3, 10)], (6, 7, 10, 11)
    elif size == 20:
        pouch_xy, core = [(5, 6), (14, 5), (16, 7), (15, 15), (6, 15), (4, 12)], (8, 8, 12, 13)
    elif size == 24:
        pouch_xy, core = [(6, 7), (17, 6), (19, 8), (18, 18), (7, 18), (5, 15)], (9, 10, 15, 16)
    else:
        pouch_xy, core = [(8, 9), (23, 8), (26, 11), (24, 24), (9, 24), (6, 20)], (12, 13, 20, 21)
    pts = [(x * ss, y * ss) for x, y in pouch_xy]
    pouch = polygon_mask(out.size, pts)
    shadow(out, pouch, 0, ss // 2, ss, (0, 0, 0, 145))
    film = gradient(out.size, ((0, (210, 216, 211, 235)), (.48, (133, 142, 139, 235)),
                                     (1, (72, 81, 81, 245))), .08)
    composite_mask(out, film, pouch)
    detail = Image.new("RGBA", out.size, (0, 0, 0, 0))
    dd = ImageDraw.Draw(detail)
    dd.line(pts + [pts[0]], fill=(242, 246, 239, 185), width=max(ss // 2, 2), joint="curve")
    x0, y0, x1, y1 = core
    cbox = (x0 * ss, y0 * ss, x1 * ss, y1 * ss)
    cx, cy = ((x0 + x1) * ss // 2, (y0 + y1) * ss // 2)
    wedge = (92, 101, 97, 180)
    dd.polygon(((4 * ss, 6 * ss), ((x0 + 1) * ss, y0 * ss), (cx, cy)), fill=wedge)
    dd.polygon((((x1 - 1) * ss, y0 * ss), ((pouch_xy[2][0] - 1) * ss, pouch_xy[2][1] * ss), (cx, cy)), fill=wedge)
    if size >= 24:
        dd.polygon(((pouch_xy[-2][0] * ss, (pouch_xy[-2][1] - 1) * ss),
                    ((x0 + 1) * ss, y1 * ss), (cx, cy)), fill=(61, 68, 68, 155))
        dd.polygon((((x1 - 1) * ss, y1 * ss),
                    (pouch_xy[3][0] * ss, (pouch_xy[3][1] - 1) * ss), (cx, cy)), fill=(61, 68, 68, 155))
    out.alpha_composite(detail)

    halo = Image.new("RGBA", out.size, (0, 0, 0, 0))
    ImageDraw.Draw(halo).rounded_rectangle((cbox[0] - ss, cbox[1] - ss, cbox[2] + ss, cbox[3] + ss),
                                           radius=2 * ss, fill=(170, 255, 35, 100))
    out.alpha_composite(halo.filter(ImageFilter.GaussianBlur(ss)))
    cm = rounded_mask(out.size, cbox, max(ss, (y1 - y0) * ss // 3))
    lime = gradient(out.size, ((0, (224, 255, 91, 255)), (.52, (158, 232, 35, 255)),
                                    (1, (91, 165, 15, 255))), .10)
    composite_mask(out, lime, cm)
    cap = Image.new("RGBA", out.size, (0, 0, 0, 0))
    cd = ImageDraw.Draw(cap)
    cd.rounded_rectangle(cbox, radius=max(ss, (y1 - y0) * ss // 3),
                         outline=(244, 255, 202, 215), width=max(2, ss // 2))
    cd.line(((x0 + 1) * ss, (y0 + 1) * ss, (x1 - 1) * ss, (y0 + 1) * ss),
            fill=(250, 255, 213, 170), width=max(2, ss // 2))
    out.alpha_composite(cap)
    return out.resize((size, size), LANCZOS)


def make_images():
    return [render_small(size) if size in SMALL else render_large(size) for size in SIZES]


def write_ico(path: Path, images: Iterable[Image.Image]):
    images = [image.convert("RGBA") for image in images]
    blobs = []
    for image in images:
        stream = io.BytesIO()
        image.save(stream, "PNG", optimize=True)
        blobs.append(stream.getvalue())
    offset = 6 + 16 * len(images)
    directory = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    for image, blob in zip(images, blobs):
        width = 0 if image.width == 256 else image.width
        height = 0 if image.height == 256 else image.height
        directory.extend(struct.pack("<BBBBHHII", width, height, 0, 0, 1, 32, len(blob), offset))
        offset += len(blob)
    path.write_bytes(directory + b"".join(blobs))


def write_preview(path: Path, images):
    canvas = Image.new("RGB", (1200, 720), (243, 243, 243))
    canvas.paste(Image.new("RGB", (1200, 330), (32, 32, 32)), (0, 390))
    for background_y, label_color in ((0, (45, 45, 45)), (390, (225, 225, 225))):
        draw = ImageDraw.Draw(canvas)
        draw.text((24, background_y + 18), "LIGHT #F3F3F3" if background_y == 0 else "DARK #202020", fill=label_color)
        x = 28
        baseline = background_y + 90
        for image in images:
            canvas.paste(image, (x, baseline), image)
            draw.text((x, baseline + image.height + 12), f"{image.width}", fill=label_color)
            x += max(image.width, 44) + 28
        hero = render_large(256)
        canvas.paste(hero, (900, background_y + 50), hero)
    canvas.save(path, "PNG", optimize=True)


def main():
    images = make_images()
    write_ico(HERE / "yzip.ico", images)
    if len(sys.argv) > 1:
        write_preview(Path(sys.argv[1]), images)


if __name__ == "__main__":
    main()


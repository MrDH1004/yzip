"""Generate yzip.ico: a blue rounded tile with a white zipper. Small sizes use a simplified glyph so 16 px stays crisp.
usage: python make_icon.py [preview.png]"""
import io
import os
import struct
import sys

from PIL import Image, ImageChops, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
M = 1024  # master canvas
TOP, BOTTOM = (82, 132, 255), (36, 62, 214)


def tile(margin, radius):
    """Rounded square with a diagonal gradient and a soft top sheen."""
    grad = Image.new("RGBA", (M, M))
    px = grad.load()
    for y in range(M):
        for x in range(M):
            t = (x * 0.35 + y * 0.65) / M
            px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)) + (255,)
    sheen = Image.linear_gradient("L").resize((M, M)).point(lambda v: max(0, 34 - v * 34 // 150))
    grad = Image.composite(Image.new("RGBA", (M, M), (255, 255, 255, 255)), grad, sheen)
    mask = Image.new("L", (M, M), 0)
    ImageDraw.Draw(mask).rounded_rectangle([margin, margin, M - margin, M - margin], radius, fill=255)
    out = Image.new("RGBA", (M, M), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def glyph(simple):
    """White zipper: interlocking teeth above a pull tab with a slot."""
    g = Image.new("L", (M, M), 0)
    d = ImageDraw.Draw(g)
    cx = M // 2
    if simple:  # 3 bold teeth, big pull
        rows, y0, step, tw, th, over = 3, 170, 118, 210, 96, 64
        pull = [cx - 150, 520, cx + 150, 870]
        slot = [cx - 54, 616, cx + 54, 780]
        r, sr = 96, 54
    else:
        rows, y0, step, tw, th, over = 5, 160, 82, 150, 64, 46
        pull = [cx - 118, 580, cx + 118, 860]
        slot = [cx - 42, 656, cx + 42, 796]
        r, sr = 80, 42
    for i in range(rows):
        y = y0 + i * step
        if i % 2 == 0:
            box = [cx - tw + over, y, cx + over, y + th]
        else:
            box = [cx - over, y, cx + tw - over, y + th]
        d.rounded_rectangle(box, th // 2, fill=255)
    stem_top = y0 + (rows - 1) * step + th - 6
    d.rounded_rectangle([cx - th // 2, stem_top, cx + th // 2, pull[1] + 40], th // 4, fill=255)
    d.rounded_rectangle(pull, r, fill=255)
    hole = Image.new("L", (M, M), 0)
    ImageDraw.Draw(hole).rounded_rectangle(slot, sr, fill=255)
    return ImageChops.subtract(g, hole)


def render(simple):
    base = tile(56 if not simple else 24, 210 if not simple else 230)
    shadow = Image.new("RGBA", (M, M), (10, 20, 80, 70))
    sh = glyph(simple).transform((M, M), Image.AFFINE, (1, 0, 0, 0, 1, -10))  # 10 px soft drop shadow
    base.paste(shadow, (0, 0), sh.point(lambda v: v // 3))
    base.paste(Image.new("RGBA", (M, M), (255, 255, 255, 255)), (0, 0), glyph(simple))
    return base


def write_ico(path, images):
    """ICO with PNG-compressed entries (supported by Windows since Vista)."""
    blobs = []
    for im in images:
        b = io.BytesIO()
        im.save(b, "PNG")
        blobs.append(b.getvalue())
    off = 6 + 16 * len(images)
    head = struct.pack("<HHH", 0, 1, len(images))
    dirs = b""
    for im, blob in zip(images, blobs):
        w = im.width if im.width < 256 else 0
        dirs += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(blob), off)
        off += len(blob)
    with open(path, "wb") as f:
        f.write(head + dirs + b"".join(blobs))


def main():
    full, simple = render(False), render(True)
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    imgs = [(simple if s <= 32 else full).resize((s, s), Image.LANCZOS) for s in sizes]
    write_ico(os.path.join(HERE, "yzip.ico"), imgs)
    full.resize((512, 512), Image.LANCZOS).save(os.path.join(HERE, "yzip.png"))
    if len(sys.argv) > 1:  # contact sheet: every size at 1x plus 16/24/32 magnified 6x
        sheet = Image.new("RGBA", (1100, 420), (246, 247, 249, 255))
        x = 10
        for im in imgs:
            sheet.paste(im, (x, 10), im)
            x += im.width + 12
        x = 10
        for im in imgs[:4]:
            big = im.resize((im.width * 6, im.height * 6), Image.NEAREST)
            sheet.paste(big, (x, 280 - big.height // 2 + 20), big)
            x += big.width + 20
        dark = Image.new("RGBA", (300, 140), (32, 32, 36, 255))
        dark.paste(imgs[-2], (10, 6), imgs[-2])
        dark.paste(imgs[3], (160, 50), imgs[3])
        dark.paste(imgs[0], (220, 58), imgs[0])
        sheet.paste(dark, (790, 270))
        sheet.save(sys.argv[1])


if __name__ == "__main__":
    main()

"""
podium.py - draws the purple "top 3" podium banner used by the live leaderboard.

Only needs Pillow and Poppins-Bold.ttf sitting next to this file.
No names are drawn on purpose (staff names use fancy unicode fonts that would
turn into empty boxes) - the names/points are shown by the embed itself.
"""

import io
import math
import os
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
W, H = 1200, 440

GOLD = (255, 214, 102)
SILVER = (222, 208, 255)
ROSE = (255, 150, 205)
LILAC = (190, 120, 255)

BG_FILE = "podium_bg.png"        # background picture (1200x440 works best)
BG_TINT = (16, 6, 36, 40)       # dark purple overlay on top of it (last number = strength 0-255)

USE_BUTTERFLY = False   # False = go back to the normal circle avatars
EMOJI_ID = "1553826933890879518"
EMOJI_URL = f"https://cdn.discordapp.com/emojis/{EMOJI_ID}.png?size=256&quality=lossless"


def _butterfly(size: int):
    """Loads butterfly.png next to this file (downloaded once from Discord, then cached)."""
    path = os.path.join(HERE, "butterfly.png")
    if not os.path.exists(path):
        try:
            import urllib.request
            req = urllib.request.Request(EMOJI_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r, open(path, "wb") as f:
                f.write(r.read())
        except Exception:
            return None
    try:
        return Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
    except Exception:
        return None


def _font(size: int):
    for path in (
        os.path.join(HERE, "Poppins-Bold.ttf"),
        os.path.join(HERE, "fonts", "Poppins-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _star(draw, cx, cy, r, fill):
    """4-point sparkle."""
    pts = []
    for i in range(8):
        ang = math.pi / 4 * i - math.pi / 2
        rad = r if i % 2 == 0 else r * 0.28
        pts.append((cx + math.cos(ang) * rad, cy + math.sin(ang) * rad))
    draw.polygon(pts, fill=fill)


def _heart(draw, cx, cy, s, fill):
    r = s * 0.27
    draw.ellipse([cx - s * 0.5, cy - s * 0.45, cx - s * 0.5 + 2 * r * 1.0 + s * 0.04, cy - s * 0.45 + 2 * r], fill=fill)
    draw.ellipse([cx + s * 0.5 - 2 * r - s * 0.04, cy - s * 0.45, cx + s * 0.5, cy - s * 0.45 + 2 * r], fill=fill)
    draw.polygon([(cx - s * 0.5, cy - s * 0.1), (cx + s * 0.5, cy - s * 0.1), (cx, cy + s * 0.55)], fill=fill)


def _glow(base, draw_fn, blur):
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer))
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    base.alpha_composite(layer)


def _circle_avatar(data, d):
    if USE_BUTTERFLY:
        bf = _butterfly(int(d * 0.78))
        if bf is not None:
            im = Image.new("RGBA", (d, d), (38, 14, 74, 255))
            im.alpha_composite(bf, ((d - bf.width) // 2, (d - bf.height) // 2))
            mask = Image.new("L", (d * 4, d * 4), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, d * 4 - 1, d * 4 - 1], fill=255)
            im.putalpha(mask.resize((d, d), Image.LANCZOS))
            return im
    if data:
        try:
            im = Image.open(io.BytesIO(data)).convert("RGBA")
            side = min(im.size)
            left, top = (im.width - side) // 2, (im.height - side) // 2
            im = im.crop((left, top, left + side, top + side)).resize((d, d), Image.LANCZOS)
        except Exception:
            im = None
    else:
        im = None
    if im is None:  # placeholder
        im = Image.new("RGBA", (d, d), (110, 55, 190, 255))
        ImageDraw.Draw(im).ellipse([d * 0.3, d * 0.2, d * 0.7, d * 0.6], fill=(190, 140, 255, 255))
        ImageDraw.Draw(im).ellipse([d * 0.15, d * 0.62, d * 0.85, d * 1.25], fill=(190, 140, 255, 255))
    mask = Image.new("L", (d * 4, d * 4), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, d * 4 - 1, d * 4 - 1], fill=255)
    mask = mask.resize((d, d), Image.LANCZOS)
    im.putalpha(mask)
    return im


def _background():
    """podium_bg.png next to this file, cropped to fill the banner and darkened a bit
    so the podium and text stay readable. Falls back to the purple gradient."""
    try:
        bg = Image.open(os.path.join(HERE, BG_FILE)).convert("RGB")
        scale = max(W / bg.width, H / bg.height)
        bg = bg.resize((round(bg.width * scale), round(bg.height * scale)), Image.LANCZOS)
        left, top = (bg.width - W) // 2, (bg.height - H) // 2
        bg = bg.crop((left, top, left + W, top + H)).convert("RGBA")
        bg.alpha_composite(Image.new("RGBA", (W, H), BG_TINT))
        return bg
    except Exception:
        img = Image.new("RGB", (W, H))
        px = ImageDraw.Draw(img)
        for y in range(H):
            t = y / H
            px.line([(0, y), (W, y)], fill=(int(10 + 36 * t), int(5 + 9 * t), int(24 + 74 * t)))
        return img.convert("RGBA")


def render_podium(avatars) -> bytes:
    """avatars: list of up to 3 PNG/JPEG byte strings (or None) for places 1, 2, 3."""
    avatars = list(avatars) + [None] * (3 - len(avatars))
    rnd = random.Random(7)

    # --- background: podium_bg.png (cover-fit + dark overlay), else the purple gradient
    img = _background()

    # --- big soft glow behind the winner
    _glow(img, lambda d: d.ellipse([600 - 330, 40, 600 + 330, 420], fill=(176, 102, 255, 45)), 90)
    _glow(img, lambda d: d.ellipse([600 - 200, 70, 600 + 200, 300], fill=(255, 120, 220, 25)), 70)

    # --- bokeh, sparkles, hearts
    bokeh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    bd = ImageDraw.Draw(bokeh)
    for _ in range(20):
        r = rnd.randint(4, 16)
        x, y = rnd.randint(0, W), rnd.randint(0, H)
        bd.ellipse([x - r, y - r, x + r, y + r], fill=(200, 140, 255, rnd.randint(25, 70)))
    img.alpha_composite(bokeh.filter(ImageFilter.GaussianBlur(2)))

    deco = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dd = ImageDraw.Draw(deco)
    for _ in range(22):
        x, y = rnd.randint(30, W - 30), rnd.randint(20, H - 160)
        _star(dd, x, y, rnd.randint(6, 15), (255, 240, 255, rnd.randint(130, 235)))
    for x, y, s, c in [
        (110, 150, 34, (255, 140, 210, 170)), (1090, 170, 40, (255, 140, 210, 170)),
        (170, 300, 24, (200, 150, 255, 150)), (1030, 300, 26, (200, 150, 255, 150)),
        (60, 240, 18, (255, 190, 235, 140)), (1140, 250, 20, (255, 190, 235, 140)),
    ]:
        _heart(dd, x, y, s, c)
    img.alpha_composite(deco)

    # --- header text
    d = ImageDraw.Draw(img)
    d.text((48, 30), "STAFF LEADERBOARD", font=_font(40), fill=(245, 232, 255, 255))
    d.text((W - 48, 36), "VOICE HOURS", font=_font(30), fill=(200, 150, 255, 255), anchor="ra")
    d.rounded_rectangle([48, 82, 330, 87], radius=3, fill=(176, 102, 255, 255))

    # --- podium geometry:  (center x, pedestal height, avatar diameter, ring color, data)
    base_y = 412
    slots = [
        (300, 95, 150, SILVER, avatars[1], "2"),
        (900, 70, 150, ROSE, avatars[2], "3"),
        (600, 130, 190, GOLD, avatars[0], "1"),
    ]
    for cx, ph, ad, ring, data, label in slots:
        top = base_y - ph
        # pedestal glow + block
        _glow(img, lambda g, cx=cx, top=top: g.rounded_rectangle([cx - 140, top, cx + 140, base_y], 18, fill=ring + (110,)), 18)
        ped = ImageDraw.Draw(img)
        ped.rounded_rectangle([cx - 140, top, cx + 140, base_y], radius=18, fill=(52, 22, 98, 255), outline=ring + (255,), width=3)
        ped.rounded_rectangle([cx - 132, top + 8, cx + 132, top + 20], radius=6, fill=(255, 255, 255, 38))
        ped.text((cx, top + ph / 2 + 8), label, font=_font(64 if label == "1" else 54), fill=ring + (255,), anchor="mm")

        # avatar with glowing ring
        r = ad // 2
        cy = top - r - 20
        _glow(img, lambda g, cx=cx, cy=cy, r=r, ring=ring: g.ellipse([cx - r - 16, cy - r - 16, cx + r + 16, cy + r + 16], fill=ring + (210,)), 16)
        ring_d = ImageDraw.Draw(img)
        ring_d.ellipse([cx - r - 9, cy - r - 9, cx + r + 9, cy + r + 9], fill=ring + (255,))
        ring_d.ellipse([cx - r - 4, cy - r - 4, cx + r + 4, cy + r + 4], fill=(18, 8, 34, 255))
        img.alpha_composite(_circle_avatar(data, ad), (cx - r, cy - r))

        if label == "1":  # crown
            ty = cy - r - 82
            crown = [(-56, 62), (-62, 8), (-28, 34), (0, -4), (28, 34), (62, 8), (56, 62)]
            _glow(img, lambda g, cx=cx, ty=ty, crown=crown: g.polygon([(cx + x, ty + y) for x, y in crown], fill=GOLD + (200,)), 10)
            cd = ImageDraw.Draw(img)
            cd.polygon([(cx + x, ty + y) for x, y in crown], fill=GOLD + (255,), outline=(255, 244, 196, 255))
            for jx, jy, jc in [(-62, 8, ROSE), (0, -4, (255, 255, 255)), (62, 8, ROSE)]:
                cd.ellipse([cx + jx - 8, ty + jy - 8, cx + jx + 8, ty + jy + 8], fill=jc + (255,))
            cd.rectangle([cx - 56, ty + 50, cx + 56, ty + 62], fill=(255, 190, 80, 255))

    # --- thin neon frame
    ImageDraw.Draw(img).rounded_rectangle([3, 3, W - 4, H - 4], radius=26, outline=(176, 102, 255, 255), width=4)

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


if __name__ == "__main__":  # quick preview:  python podium.py
    def fake(c):
        im = Image.new("RGB", (256, 256), c)
        ImageDraw.Draw(im).ellipse([60, 40, 196, 176], fill=(255, 255, 255))
        b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()
    open("podium_preview.png", "wb").write(render_podium([fake((90, 120, 220)), fake((220, 90, 120)), fake((90, 200, 140))]))
    print("wrote podium_preview.png")

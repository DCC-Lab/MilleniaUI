#!/usr/bin/env python3
"""Generate MilleniaUI.icns — a 532 nm green laser beam on a dark squircle.

Renders a 1024x1024 master with PIL, writes the macOS .iconset size ladder,
and calls `iconutil` to produce packaging/MilleniaUI.icns.

    python packaging/make_icon.py
"""

import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
MASTER = 1024
GREEN = (60, 230, 90)          # 532 nm-ish
CORE = (220, 255, 215)         # near-white beam core


def rounded_mask(size, radius):
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def vertical_gradient(size, top, bottom):
    t = np.linspace(0.0, 1.0, size, dtype=np.float32)[:, None]
    top = np.array(top, dtype=np.float32)
    bottom = np.array(bottom, dtype=np.float32)
    rows = (top[None, :] * (1 - t) + bottom[None, :] * t)  # (size,3)
    arr = np.repeat(rows[:, None, :], size, axis=1)        # (size,size,3)
    return Image.fromarray(arr.astype("uint8"), "RGB")


def add_glow(base, layer, blur):
    """Screen-add a blurred RGBA glow layer onto an RGBA base."""
    glow = layer.filter(ImageFilter.GaussianBlur(blur))
    return Image.alpha_composite(base, glow)


def render_master():
    S = MASTER
    # Squircle body with a little padding so it reads as a native app icon.
    pad = int(S * 0.085)
    body = S - 2 * pad
    radius = int(body * 0.235)

    bg = vertical_gradient(body, (34, 36, 44), (10, 11, 14)).convert("RGBA")
    body_mask = rounded_mask(body, radius)

    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    icon.paste(bg, (pad, pad), body_mask)

    cy = S // 2

    # --- the beam: a soft green glow plus a bright sharp core ---
    beam = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bd = ImageDraw.Draw(beam)
    x0 = int(S * 0.30)   # leaves the emitter on the left
    x1 = int(S * 0.86)
    bd.rounded_rectangle([x0, cy - 26, x1, cy + 26], radius=26,
                         fill=GREEN + (255,))
    icon = add_glow(icon, beam, blur=46)        # wide outer glow
    icon = add_glow(icon, beam, blur=16)        # tighter inner glow

    core = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    cd = ImageDraw.Draw(core)
    cd.rounded_rectangle([x0, cy - 7, x1, cy + 7], radius=7, fill=CORE + (255,))
    icon = add_glow(icon, core, blur=4)

    # --- emitter aperture on the left with a radial flare ---
    flare = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    fd = ImageDraw.Draw(flare)
    ex, r = int(S * 0.275), 96
    fd.ellipse([ex - r, cy - r, ex + r, cy + r], fill=GREEN + (255,))
    icon = add_glow(icon, flare, blur=40)

    head = ImageDraw.Draw(icon)
    # metallic emitter housing
    hx0, hx1 = int(S * 0.16), int(S * 0.30)
    head.rounded_rectangle([hx0, cy - 116, hx1, cy + 116], radius=44,
                           fill=(64, 68, 78, 255))
    head.rounded_rectangle([hx0 + 10, cy - 104, hx1 + 6, cy + 104], radius=38,
                           fill=(90, 95, 107, 255))
    # bright aperture
    ar = 60
    head.ellipse([ex - ar, cy - ar, ex + ar, cy + ar], fill=(18, 22, 24, 255))
    head.ellipse([ex - 40, cy - 40, ex + 40, cy + 40], fill=GREEN + (255,))
    head.ellipse([ex - 16, cy - 16, ex + 16, cy + 16], fill=CORE + (255,))

    # Re-clip everything to the squircle so the glow can't bleed past the edge.
    full_mask = Image.new("L", (S, S), 0)
    full_mask.paste(body_mask, (pad, pad))
    icon.putalpha(Image.composite(icon.getchannel("A"),
                                  Image.new("L", (S, S), 0), full_mask))

    # subtle top sheen
    sheen = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sheen)
    sd.ellipse([pad - body, pad - int(body * 0.78), S - pad + body,
                pad + int(body * 0.30)], fill=(255, 255, 255, 26))
    sheen.putalpha(Image.composite(sheen.getchannel("A"),
                                   Image.new("L", (S, S), 0), full_mask))
    icon = Image.alpha_composite(icon, sheen)
    return icon


def main():
    master = render_master()
    iconset = HERE / "MilleniaUI.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()

    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = size * scale
            img = master.resize((px, px), Image.LANCZOS)
            suffix = "" if scale == 1 else "@2x"
            img.save(iconset / f"icon_{size}x{size}{suffix}.png")

    icns = HERE / "MilleniaUI.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
                   check=True)
    shutil.rmtree(iconset)

    # Windows icon (.ico, cross-platform via PIL; ICO tops out at 256 px).
    ico = HERE / "MilleniaUI.ico"
    master.save(ico, format="ICO",
                sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])

    master.save(HERE / "MilleniaUI_icon_1024.png")
    print("wrote", icns, "and", ico)


if __name__ == "__main__":
    main()

"""Draw the app icon (five fret buttons on a stage-plum tile) and write
icon.png (1024), icon.ico (Windows) and an .iconset folder (for macOS iconutil)."""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

out = Path(sys.argv[1] if len(sys.argv) > 1 else "build/icon")
out.mkdir(parents=True, exist_ok=True)
S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
pad = 92
# tile with a soft vertical gradient
tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
g = Image.new("RGBA", (1, S))
for y in range(S):
    t = y / S
    g.putpixel((0, y), (int(42 - 18 * t), int(24 - 10 * t), int(84 - 40 * t), 255))
g = g.resize((S, S))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([pad, pad, S - pad, S - pad], radius=190, fill=255)
tile.paste(g, (0, 0), mask)
img.alpha_composite(tile)
# amber "spotlight" glow
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(glow).ellipse([260, 140, 764, 520], fill=(255, 181, 46, 70))
img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(80)))
# five frets
colors = [(60, 200, 90), (232, 65, 58), (242, 210, 58), (58, 142, 232), (240, 138, 42)]
r, gap = 62, 22
total = 5 * 2 * r + 4 * gap
x0, y = (S - total) // 2 + r, 610
for i, c in enumerate(colors):
    x = x0 + i * (2 * r + gap)
    d.ellipse([x - r, y - r, x + r, y + r], fill=c + (255,))
    d.ellipse([x - r + 22, y - r + 22, x + r - 22, y + r - 22], fill=(255, 255, 255, 70))
# a lyric "wipe" bar above the frets
d.rounded_rectangle([250, 380, 774, 430], radius=25, fill=(246, 241, 231, 60))
d.rounded_rectangle([250, 380, 590, 430], radius=25, fill=(255, 181, 46, 255))
img.save(out / "icon.png")
img.save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
iconset = out / "icon.iconset"
iconset.mkdir(exist_ok=True)
for size in (16, 32, 128, 256, 512):
    img.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
    img.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
print("icons written to", out)

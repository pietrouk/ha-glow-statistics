"""Draw the Glow Statistics icon: a rounded tile with rising hourly bars, a flame and a bolt."""

import math
import sys

from PIL import Image, ImageDraw

S = 2048  # draw large, then downsample for smooth edges
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

# Tile with a vertical gradient (deep teal to night blue)
grad = Image.new("RGBA", (S, S))
top, bottom = (18, 110, 130), (16, 42, 92)
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / (S - 1)
    gd.line([(0, y), (S, y)], fill=(*(round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)), 255))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
img.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(img)

# Baseline and rising bars (hourly statistics)
base_y = int(S * 0.80)
bar_w, gap = int(S * 0.115), int(S * 0.045)
left = (S - (5 * bar_w + 4 * gap)) // 2
heights = [0.18, 0.26, 0.22, 0.32, 0.40]
for i, h in enumerate(heights):
    x0 = left + i * (bar_w + gap)
    d.rounded_rectangle(
        [x0, base_y - int(S * h), x0 + bar_w, base_y], radius=int(bar_w * 0.28), fill=(255, 255, 255, 235)
    )
d.rounded_rectangle(
    [int(S * 0.12), base_y + int(S * 0.03), int(S * 0.88), base_y + int(S * 0.055)],
    radius=int(S * 0.012),
    fill=(255, 255, 255, 150),
)

# Lightning bolt (electricity), top left
bx, by, k = int(S * 0.25), int(S * 0.15), S / 1000
bolt = [
    (bx + 110 * k, by),
    (bx + 10 * k, by + 190 * k),
    (bx + 85 * k, by + 190 * k),
    (bx + 40 * k, by + 330 * k),
    (bx + 175 * k, by + 125 * k),
    (bx + 100 * k, by + 125 * k),
    (bx + 150 * k, by),
]
d.polygon(bolt, fill=(255, 202, 40, 255))


# Flame (gas), top right: outer orange teardrop with an inner yellow one
def flame(cx, cy, w, h, color):
    """Teardrop: an ellipse whose upper half narrows to a point."""
    pts = []
    for a in range(0, 361, 3):
        t = math.radians(a)
        x = math.sin(t) * w * (1 - 0.25 * (1 - math.cos(t)))
        if math.cos(t) > 0:  # upper half: pull towards the centre line for a pointed tip
            x *= (1 - math.cos(t)) ** 0.9
        pts.append((cx + x, cy - math.cos(t) * h))
    d.polygon(pts, fill=color)


flame(int(S * 0.66), int(S * 0.28), int(S * 0.11), int(S * 0.15), (255, 112, 67, 255))
flame(int(S * 0.66), int(S * 0.32), int(S * 0.057), int(S * 0.09), (255, 213, 79, 255))

out = sys.argv[1] if len(sys.argv) > 1 else "."
img.resize((512, 512), Image.LANCZOS).save(f"{out}/icon@2x.png", optimize=True)
img.resize((256, 256), Image.LANCZOS).save(f"{out}/icon.png", optimize=True)

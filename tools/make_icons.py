#!/usr/bin/env python3
"""生成 videocut 的应用图标（纯标准库，无需 Pillow）。

设计：圆角蓝色方形 + 两条白色胶片 + 中间断口（剪切）+ 片孔。
输出：ICON.PNG(64) / ICON_256.PNG(256) / app/ui/images/icon_64.png / icon_256.png
"""

import os
import struct
import zlib

SS = 4  # 超采样倍数（抗锯齿）

BG_RADIUS = 0.245
BG_INSET = 0.045
BAR_TOP, BAR_BOTTOM, BAR_RADIUS = 0.30, 0.70, 0.045
BARS = ((0.155, 0.462), (0.538, 0.845))
HOLE_W, HOLE_H = 0.052, 0.05
HOLE_BANDS = (0.318, 0.632)

GRAD_A = (0.278, 0.588, 1.0)
GRAD_B = (0.078, 0.271, 0.784)


def _round_rect(u, v, x0, y0, x1, y1, r):
    if u < x0 or u > x1 or v < y0 or v > y1:
        return False
    cx = min(max(u, x0 + r), x1 - r)
    cy = min(max(v, y0 + r), y1 - r)
    if u == cx or v == cy:
        return True
    return (u - cx) ** 2 + (v - cy) ** 2 <= r * r


def _gradient(u, v):
    t = max(0.0, min(1.0, (u + v) / 2.0))
    return tuple(GRAD_A[i] + (GRAD_B[i] - GRAD_A[i]) * t for i in range(3))


def shade(u, v):
    """返回 (r, g, b, a)，颜色为 0..1。"""
    inset = BG_INSET
    if not _round_rect(u, v, inset, inset, 1 - inset, 1 - inset, BG_RADIUS):
        return (0.0, 0.0, 0.0, 0.0)

    r, g, b = _gradient(u, v)

    in_bar = False
    bar_index = 0
    for i, (x0, x1) in enumerate(BARS):
        if _round_rect(u, v, x0, BAR_TOP, x1, BAR_BOTTOM, BAR_RADIUS):
            in_bar, bar_index = True, i
            break
    if in_bar:
        r = g = b = 1.0
        x0, x1 = BARS[bar_index]
        bw = x1 - x0
        for band in HOLE_BANDS:
            for k in (1, 3, 5):
                hx = x0 + bw * k / 6.0
                if _round_rect(
                    u, v,
                    hx - HOLE_W / 2, band, hx + HOLE_W / 2, band + HOLE_H,
                    min(HOLE_W, HOLE_H) / 2 * 0.9,
                ):
                    return _gradient(u, v) + (1.0,)
    return (r, g, b, 1.0)


def render(size):
    raw = bytearray()
    inv = 1.0 / size
    n = SS * SS
    for y in range(size):
        raw.append(0)  # filter type 0
        for x in range(size):
            ar = ag = ab = aa = 0.0
            for sy in range(SS):
                v = (y + (sy + 0.5) / SS) * inv
                for sx in range(SS):
                    u = (x + (sx + 0.5) / SS) * inv
                    r, g, b, a = shade(u, v)
                    ar += r * a
                    ag += g * a
                    ab += b * a
                    aa += a
            if aa <= 0:
                raw.extend((0, 0, 0, 0))
                continue
            raw.extend((
                int(round(ar / aa * 255)),
                int(round(ag / aa * 255)),
                int(round(ab / aa * 255)),
                int(round(aa / n * 255)),
            ))
    return bytes(raw)


def png_bytes(size):
    raw = render(size)
    out = bytearray(b"\x89PNG\r\n\x1a\n")

    def chunk(tag, data):
        out.extend(struct.pack(">I", len(data)))
        out.extend(tag)
        out.extend(data)
        out.extend(struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    chunk(b"IDAT", zlib.compress(raw, 9))
    chunk(b"IEND", b"")
    return bytes(out)


TARGETS = (
    ("ICON.PNG", 64),
    ("ICON_256.PNG", 256),
    ("app/ui/images/icon_64.png", 64),
    ("app/ui/images/icon_256.png", 256),
)


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache = {}
    for rel, size in TARGETS:
        if size not in cache:
            cache[size] = png_bytes(size)
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(cache[size])
        print("wrote %s (%d bytes)" % (rel, len(cache[size])))


if __name__ == "__main__":
    main()

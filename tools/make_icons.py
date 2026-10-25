"""Draws the extension icons (blue rounded square with a white download arrow) without any image library."""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "extension" / "icons"


def png(size):
    s = size
    px = []
    r = s * 0.2
    for y in range(s):
        row = bytearray([0])
        for x in range(s):
            cx, cy = x + 0.5, y + 0.5
            # rounded square mask
            dx = max(r - cx, 0, cx - (s - r))
            dy = max(r - cy, 0, cy - (s - r))
            inside = dx * dx + dy * dy <= r * r
            if not inside:
                row += bytes((0, 0, 0, 0))
                continue
            t = cy / s
            col = (int(43 + (21 - 43) * t), int(138 + (101 - 138) * t), int(232 + (192 - 232) * t), 255)
            u, v = cx / s, cy / s
            shaft = 0.42 <= u <= 0.58 and 0.18 <= v <= 0.55
            head = v >= 0.45 and v <= 0.72 and abs(u - 0.5) <= (0.72 - v) * 1.1
            base = 0.22 <= u <= 0.78 and 0.78 <= v <= 0.86
            if shaft or head or base:
                col = (255, 255, 255, 255)
            row += bytes(col)
        px.append(bytes(row))
    raw = b"".join(px)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", s, s, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 48, 128):
        (OUT / f"{size}.png").write_bytes(png(size))
    print("icons written to", OUT)

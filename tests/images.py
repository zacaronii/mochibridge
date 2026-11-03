"""Tiny valid images built by hand, so tests need no image library."""
import struct
import zlib


def png(w: int = 6, h: int = 9, rgb=(200, 60, 90)) -> bytes:
    rows = b"".join(b"\0" + bytes(rgb) * w for _ in range(h))
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


# 1x1 lossless WebP
WEBP = bytes.fromhex(
    "524946461a000000574542505650384c0d0000002f0000001007101111888808"
    "0000"
)

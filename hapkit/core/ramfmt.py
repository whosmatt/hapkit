"""haptic_ram.bin: Awinic AW8693x RAM waveform library.

File layout (big endian):
  [0:2]  checksum = sum(file[2:]) & 0xffff
  [2:4]  base_addr: SRAM address the rest of the file is written to (RTP FIFO lies below it)
  [4:]   SRAM image: version byte, N x (start, end) u16 wave table (inclusive addresses),
         wave data (signed 8-bit PCM). The first wave starts at base_addr + 1 + 4 * N.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SRAM_END = 0x1FFF
DEFAULT_BASE = 0x0800
DEFAULT_VERSION = 0x85
HEADER = 4


@dataclass
class RamWave:
    start: int
    end: int
    data: bytes

    @property
    def length(self) -> int:
        return self.end - self.start + 1


@dataclass
class RamImage:
    base_addr: int
    version: int
    waves: list[RamWave]
    checksum: int
    checksum_ok: bool
    extra: bytes = b""
    gaps: list[tuple[int, int]] = field(default_factory=list)


def checksum(blob: bytes) -> int:
    return sum(blob[2:]) & 0xFFFF


def int8_to_bytes(samples: list[int]) -> bytes:
    for s in samples:
        if not -128 <= s <= 127:
            raise ValueError(f"sample {s} out of int8 range")
    return bytes(s & 0xFF for s in samples)


def bytes_to_int8(b: bytes) -> list[int]:
    return [x - 256 if x > 127 else x for x in b]


def parse(blob: bytes) -> RamImage:
    if len(blob) < HEADER + 1 + 4:
        raise ValueError(f"file too short ({len(blob)} bytes)")
    stored = blob[0] << 8 | blob[1]
    base = blob[2] << 8 | blob[3]
    image = blob[HEADER:]
    if base + len(image) - 1 > SRAM_END:
        raise ValueError(f"image ends at 0x{base + len(image) - 1:04x}, beyond SRAM 0x{SRAM_END:04x}")

    def addr_to_off(addr: int) -> int:
        return addr - base

    first = image[1] << 8 | image[2]
    n = (first - base - 1) // 4
    if n < 1 or base + 1 + 4 * n > first or 1 + 4 * n > len(image):
        raise ValueError(f"bad wave table: first wave at 0x{first:04x}, base 0x{base:04x}")
    waves: list[RamWave] = []
    for i in range(n):
        t = image[1 + 4 * i: 5 + 4 * i]
        start, end = t[0] << 8 | t[1], t[2] << 8 | t[3]
        if start < base + 1 + 4 * n or end < start or addr_to_off(end) >= len(image):
            raise ValueError(f"wave {i + 1}: bad range 0x{start:04x}-0x{end:04x}")
        waves.append(RamWave(start, end, bytes(image[addr_to_off(start): addr_to_off(end) + 1])))
    spans = sorted((w.start, w.end) for w in waves)
    for (s0, e0), (s1, _) in zip(spans, spans[1:]):
        if s1 <= e0:
            raise ValueError(f"overlapping waves at 0x{s1:04x}")
    gaps = []
    pos = base + 1 + 4 * n
    for s, e in spans:
        if s > pos:
            gaps.append((pos, s - 1))
        pos = max(pos, e + 1)
    last = base + len(image) - 1
    extra = bytes(image[addr_to_off(pos):]) if pos <= last else b""
    return RamImage(base, image[0], waves, stored, checksum(blob) == stored, extra, gaps)


def layout(lengths: list[int], base_addr: int = DEFAULT_BASE, ram_end: int = SRAM_END) -> dict:
    """Contiguous placement of waves after the table."""
    n = len(lengths)
    pos = base_addr + 1 + 4 * n
    spans = []
    for ln in lengths:
        if ln < 1:
            raise ValueError("empty wave")
        spans.append((pos, pos + ln - 1))
        pos += ln
    capacity = ram_end - base_addr + 1
    used = pos - base_addr
    return {
        "base_addr": base_addr,
        "ram_end": ram_end,
        "table_bytes": 1 + 4 * n,
        "spans": spans,
        "used": used,
        "capacity": capacity,
        "free": capacity - used,
        "fits": pos - 1 <= ram_end,
        "file_bytes": HEADER + used,
    }


def build(waves: list[bytes], base_addr: int = DEFAULT_BASE, version: int = DEFAULT_VERSION,
          ram_end: int = SRAM_END) -> bytes:
    if not waves:
        raise ValueError("no waves")
    if len(waves) >= 0x80:
        raise ValueError("at most 127 waves (sequencer slot values >= 0x80 are waits)")
    lay = layout([len(w) for w in waves], base_addr, ram_end)
    if not lay["fits"]:
        raise ValueError(f"RAM overflow: {lay['used']} bytes needed, {lay['capacity']} available "
                         f"(base 0x{base_addr:04x}..0x{ram_end:04x})")
    image = bytearray([version & 0xFF])
    for s, e in lay["spans"]:
        image += bytes([s >> 8, s & 0xFF, e >> 8, e & 0xFF])
    for w in waves:
        image += w
    body = bytes([base_addr >> 8, base_addr & 0xFF]) + bytes(image)
    cs = sum(body) & 0xFFFF
    return bytes([cs >> 8, cs & 0xFF]) + body

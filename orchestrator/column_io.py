"""Binary column I/O — mirrors runners/core/runner_protocol.cpp's read_column/write_column.

Disk format: [N u32][total u32][offsets[N+1] u32][bytes[total] u8]
"""
from __future__ import annotations
import struct
from dataclasses import dataclass


@dataclass
class Column:
    bytes_: bytes
    offsets: list[int]  # length N+1; offsets[0]=0, offsets[N]=len(bytes_)

    @property
    def num_rows(self) -> int:
        return max(0, len(self.offsets) - 1)

    def at(self, i: int) -> bytes:
        return self.bytes_[self.offsets[i]:self.offsets[i + 1]]


def write_column(path: str, col: Column) -> None:
    n = col.num_rows
    total = len(col.bytes_)
    assert len(col.offsets) == n + 1
    assert col.offsets[0] == 0 and col.offsets[n] == total
    with open(path, "wb") as f:
        f.write(struct.pack("<II", n, total))
        f.write(struct.pack(f"<{n + 1}I", *col.offsets))
        f.write(col.bytes_)


def read_column(path: str) -> Column:
    with open(path, "rb") as f:
        head = f.read(8)
        n, total = struct.unpack("<II", head)
        off_bytes = f.read((n + 1) * 4)
        offsets = list(struct.unpack(f"<{n + 1}I", off_bytes))
        data = f.read(total)
        if len(data) != total:
            raise IOError(f"read_column: short read on {path}")
    return Column(bytes_=data, offsets=offsets)

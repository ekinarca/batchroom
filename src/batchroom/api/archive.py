"""Streaming ZIP downloads.

The archive is produced while it is sent instead of being built on disk
first: the first byte goes out immediately (a multi-GB job no longer runs
into a reverse proxy's read timeout before the download even starts), no
second copy of the outputs piles up under ``tmp/``, and two simultaneous
downloads can't trip over a shared temp file.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path


class _Sink(io.RawIOBase):
    """Write-only, unseekable buffer: ZipFile streams into it (using data
    descriptors) and the generator hands out whatever accumulated."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._pos = 0

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self._chunks.append(bytes(data))
        self._pos += len(data)
        return len(data)

    def tell(self) -> int:
        return self._pos

    def drain(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


def stream_zip(entries: Iterable[tuple[str, Path | bytes]]) -> Iterator[bytes]:
    """Yield a ZIP of ``(arcname, file path | bytes)`` entries, file by file.

    Entries that vanished from disk in the meantime are skipped. Deflate
    level 1: the outputs are already-compressed images, so a higher level
    only burns CPU.
    """
    sink = _Sink()
    with zipfile.ZipFile(
        sink, "w", zipfile.ZIP_DEFLATED, compresslevel=1, strict_timestamps=False
    ) as zf:
        for arcname, source in entries:
            if isinstance(source, bytes):
                zf.writestr(arcname, source)
            else:
                try:
                    zf.write(source, arcname)
                except FileNotFoundError:
                    continue
            yield sink.drain()
    yield sink.drain()  # central directory

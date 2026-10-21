"""Lossless, segmented evidence files. Exhaustion is required for acceptance.

This codec supplies integrity and size checks, not a semantic audit certificate.
There are no paths inside an archive, no pickle and no implicit decompression.
"""

import hashlib
import json
import os
import re
import stat
import struct
import tempfile
import zlib
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Self, cast

from aca.research.artifacts import _pairs, canonical, digest

MAGIC = b"ACARPL1\n"
HEADER = struct.Struct(">BI")
MAX_SEGMENT_ROWS = 64
TARGET_SEGMENT_BYTES = 1_000_000
MAX_SEGMENT_BYTES = 16_000_000
MAX_COMPRESSED_BYTES = MAX_SEGMENT_BYTES + 65536
MAX_MANIFEST_BYTES = 65536
MAX_ARCHIVE_BYTES = 2_000_000_000
MAX_DECODED_BYTES = 8_000_000_000
MAX_SEGMENTS = 100000
EMPTY_HASH = "0" * 64
type FileIdentity = tuple[int, int, int, int, int]


def file_identity(info: os.stat_result) -> FileIdentity:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@dataclass(frozen=True)
class ArchiveReference:
    """Explicit input for one process, never an archive acceptance certificate."""

    path: Path
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or re.fullmatch(r"[a-f0-9]{64}", self.sha256) is None:
            raise ValueError("archive path and independently retained lowercase SHA256 required")

    def check_identity(self, expected: FileIdentity) -> None:
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or file_identity(info) != expected:
                raise ValueError("required replay archive changed after validation")
        finally:
            os.close(fd)


def object_from_bytes(data: bytes) -> dict[str, object]:
    value = json.loads(data, object_pairs_hook=_pairs)
    if not isinstance(value, dict) or canonical(value) != data:
        raise ValueError("archive JSON must be a canonical object")
    return cast(dict[str, object], value)


class ArchiveWriter:
    """Publish one complete, private file without overwriting any destination."""

    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.stream: BinaryIO | None = None
        self.temporary: Path | None = None
        self.rows: list[dict[str, object]] = []
        self.size = self.count = self.segments = self.bytes_written = 0
        self.decoded_bytes = 0
        self.previous = EMPTY_HASH
        self.hasher = hashlib.sha256()
        self.complete = False

    def __enter__(self) -> Self:
        fd, name = tempfile.mkstemp(prefix=".aca-replay-", dir=self.destination.parent)
        self.temporary = Path(name)
        self.stream = os.fdopen(fd, "wb")
        try:
            self._write(MAGIC)
        except BaseException:
            self.__exit__()
            raise
        return self

    def _write(self, data: bytes) -> None:
        if self.stream is None or self.complete:
            raise ValueError("archive writer is not open")
        if self.bytes_written + len(data) > MAX_ARCHIVE_BYTES:
            raise ValueError("archive file exceeds size budget")
        self.stream.write(data)
        self.hasher.update(data)
        self.bytes_written += len(data)

    def _flush(self) -> None:
        if not self.rows:
            return
        if self.segments >= MAX_SEGMENTS:
            raise ValueError("archive exceeds segment budget")
        data = canonical({"segment": self.segments, "previous": self.previous, "rows": self.rows})
        if len(data) > MAX_SEGMENT_BYTES:
            raise ValueError("archive segment exceeds decoded size budget")
        if self.decoded_bytes + len(data) > MAX_DECODED_BYTES:
            raise ValueError("archive exceeds total decoded size budget")
        compressed = zlib.compress(data, level=9)
        self._write(HEADER.pack(1, len(compressed)))
        self._write(compressed)
        self.previous = digest(data)
        self.segments += 1
        self.decoded_bytes += len(data)
        self.rows, self.size = [], 0

    def append(self, row: dict[str, object]) -> None:
        # Reserve room for the segment envelope and separators.
        size = len(canonical(row))
        if size + 256 > MAX_SEGMENT_BYTES:
            raise ValueError("archive row exceeds decoded size budget")
        if self.rows and (
            self.size + size > TARGET_SEGMENT_BYTES or len(self.rows) >= MAX_SEGMENT_ROWS
        ):
            self._flush()
        self.rows.append(row)
        self.count += 1
        self.size += size

    def finish(self, context: dict[str, object]) -> str:
        if self.complete or self.count == 0:
            raise ValueError("archive cannot be empty or finalised twice")
        self._flush()
        manifest = canonical(
            {
                "format": "aca-replay-archive-v1",
                "segments": self.segments,
                "records": self.count,
                "terminal_segment_hash": self.previous,
                "context": context,
            }
        )
        if len(manifest) > MAX_MANIFEST_BYTES:
            raise ValueError("archive manifest exceeds size budget")
        self._write(HEADER.pack(2, len(manifest)))
        self._write(manifest)
        assert self.stream is not None  # noqa: S101 - internal lifecycle invariant
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.complete = True
        return self.hasher.hexdigest()

    def publish(self) -> None:
        if not self.complete or self.temporary is None:
            raise ValueError("only a complete archive may be published")
        # link is exclusive even if the destination is a dangling symlink.
        os.link(self.temporary, self.destination, follow_symlinks=False)
        directory = os.open(self.destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def __exit__(self, *args: object) -> None:
        if self.stream is not None:
            self.stream.close()
        if self.temporary is not None:
            self.temporary.unlink(missing_ok=True)


class ArchiveReader:
    def __init__(self, path: Path, expected_sha256: str) -> None:
        if re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is None:
            raise ValueError("an independently retained lowercase SHA256 is required")
        self.path, self.expected = path, expected_sha256
        self.manifest: dict[str, object] | None = None
        self.verified = False
        self.bytes_read = self.decoded_bytes = 0
        self.identity: FileIdentity | None = None

    def rows(self) -> Generator[dict[str, object]]:
        self.manifest, self.verified = None, False
        self.identity = None
        self.bytes_read = self.decoded_bytes = 0
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARCHIVE_BYTES:
                raise ValueError("archive must be a bounded regular file")
            hasher = hashlib.sha256()
            consumed = 0

            def read(size: int) -> bytes:
                nonlocal consumed
                data = stream.read(size)
                consumed += len(data)
                if consumed > MAX_ARCHIVE_BYTES or len(data) != size:
                    raise ValueError("archive is truncated or exceeds size budget")
                hasher.update(data)
                self.bytes_read = consumed
                return data

            if read(len(MAGIC)) != MAGIC:
                raise ValueError("unsupported replay archive format")
            previous, segments, records = EMPTY_HASH, 0, 0
            while True:
                kind, size = HEADER.unpack(read(HEADER.size))
                if kind == 2:
                    if not 0 < size <= MAX_MANIFEST_BYTES:
                        raise ValueError("archive manifest size is invalid")
                    manifest = object_from_bytes(read(size))
                    if (
                        set(manifest)
                        != {"format", "segments", "records", "terminal_segment_hash", "context"}
                        or manifest["format"] != "aca-replay-archive-v1"
                        or type(manifest["segments"]) is not int
                        or type(manifest["records"]) is not int
                        or manifest["segments"] != segments
                        or manifest["records"] != records
                        or not records
                        or manifest["terminal_segment_hash"] != previous
                        or not isinstance(manifest["context"], dict)
                    ):
                        raise ValueError("archive manifest does not match its segments")
                    if (
                        stream.read(1)
                        or hasher.hexdigest() != self.expected
                        or file_identity(os.fstat(stream.fileno())) != file_identity(info)
                    ):
                        raise ValueError("archive suffix or expected SHA256 mismatch")
                    self.identity = file_identity(info)
                    self.manifest, self.verified = manifest, True
                    return
                if kind != 1 or not 0 < size <= MAX_COMPRESSED_BYTES or segments >= MAX_SEGMENTS:
                    raise ValueError("archive frame type, size or count is invalid")
                inflater = zlib.decompressobj()
                try:
                    data = inflater.decompress(read(size), MAX_SEGMENT_BYTES + 1)
                except zlib.error as exc:
                    raise ValueError("archive compressed segment is invalid") from exc
                if (
                    len(data) > MAX_SEGMENT_BYTES
                    or not inflater.eof
                    or inflater.unused_data
                    or inflater.unconsumed_tail
                ):
                    raise ValueError("archive segment is truncated, oversized or concatenated")
                self.decoded_bytes += len(data)
                if self.decoded_bytes > MAX_DECODED_BYTES:
                    raise ValueError("archive exceeds total decoded size budget")
                segment = object_from_bytes(data)
                rows = segment.get("rows")
                if (
                    set(segment) != {"segment", "previous", "rows"}
                    or type(segment["segment"]) is not int
                    or segment["segment"] != segments
                    or segment["previous"] != previous
                    or not isinstance(rows, list)
                    or not 1 <= len(rows) <= MAX_SEGMENT_ROWS
                    or any(not isinstance(row, dict) for row in rows)
                ):
                    raise ValueError("archive segment identity or rows are invalid")
                previous, segments, records = digest(data), segments + 1, records + len(rows)
                yield from cast(list[dict[str, object]], rows)

"""Lossless bounded archive parsing, independent roots and failed publication."""

import hashlib
import os
import zlib
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aca.research.artifacts import canonical, file_hash
from aca.storage.archive_format import (
    HEADER,
    MAGIC,
    ArchiveReader,
    ArchiveWriter,
    object_from_bytes,
)


def write(path: Path, rows: list[dict[str, object]]) -> str:
    with ArchiveWriter(path) as writer:
        for row in rows:
            writer.append(row)
        hashed = writer.finish({"purpose": "test"})
        writer.publish()
    return hashed


@settings(max_examples=1000, deadline=None, derandomize=True)
@given(
    st.lists(
        st.dictionaries(
            st.text(max_size=20),
            st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=100)),
            max_size=8,
        ),
        min_size=1,
        max_size=70,
    )
)
def test_exact_json_round_trip_and_complete_root(
    tmp_path_factory: pytest.TempPathFactory, rows: list[dict[str, object]]
) -> None:
    # A separate path per example, without a function-scoped Hypothesis fixture.
    path = tmp_path_factory.mktemp("archive-roundtrip") / "evidence.aca"
    hashed = write(path, rows)
    assert hashed == file_hash(path)
    assert path.stat().st_mode & 0o777 == 0o600
    reader = ArchiveReader(path, hashed)
    assert list(reader.rows()) == rows
    assert reader.verified and reader.manifest is not None
    assert reader.manifest["records"] == len(rows)


def test_partial_reader_never_claims_verification(tmp_path: Path) -> None:
    path = tmp_path / "evidence.aca"
    hashed = write(path, [{"number": n} for n in range(130)])
    reader = ArchiveReader(path, hashed)
    iterator = reader.rows()
    assert next(iterator) == {"number": 0}
    assert not reader.verified and reader.manifest is None
    iterator.close()
    assert not reader.verified
    assert len(list(reader.rows())) == 130 and reader.verified


@pytest.mark.parametrize(
    "mutation", ["root", "byte", "suffix", "truncate", "delete_segment", "reorder"]
)
def test_changed_or_incomplete_evidence_is_rejected(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / "evidence.aca"
    hashed = write(path, [{"number": n, "price": "1.000"} for n in range(130)])
    data = path.read_bytes()
    if mutation == "root":
        hashed = "f" * 64
    elif mutation == "byte":
        index = len(MAGIC) + HEADER.size + 8
        data = data[:index] + bytes([data[index] ^ 1]) + data[index + 1 :]
    elif mutation == "suffix":
        data += b"extra evidence"
        hashed = hashlib.sha256(data).hexdigest()
    elif mutation == "truncate":
        data = data[:-1]
    else:
        offset, frames = len(MAGIC), []
        while offset < len(data):
            _, size = HEADER.unpack(data[offset : offset + HEADER.size])
            frames.append(data[offset : offset + HEADER.size + size])
            offset += HEADER.size + size
        assert len(frames) == 4
        if mutation == "delete_segment":
            del frames[1]
        else:
            frames[0], frames[1] = frames[1], frames[0]
        data = MAGIC + b"".join(frames)
        hashed = hashlib.sha256(data).hexdigest()
    path.write_bytes(data)
    reader = ArchiveReader(path, hashed)
    with pytest.raises(ValueError):
        list(reader.rows())
    assert not reader.verified


@pytest.mark.parametrize("body", [b'{"a":1,"a":2}\n', b'{"a":NaN}\n', b'{ "a":1}\n', b"[]\n"])
def test_ambiguous_json_cannot_be_accepted(body: bytes) -> None:
    with pytest.raises(ValueError):
        object_from_bytes(body)


@pytest.mark.parametrize("mutation", ["inflate", "concat", "short", "size", "kind"])
def test_decompression_budgets_and_frame_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    monkeypatch.setattr("aca.storage.archive_format.MAX_SEGMENT_BYTES", 1024)
    payload = canonical({"segment": 0, "previous": "0" * 64, "rows": [{"x": "y"}]})
    compressed = zlib.compress(payload)
    kind, size = 1, len(compressed)
    if mutation == "inflate":
        compressed = zlib.compress(b"x" * 100000)
        size = len(compressed)
    elif mutation == "concat":
        compressed += zlib.compress(payload)
        size = len(compressed)
    elif mutation == "short":
        compressed = compressed[:-1]
        size = len(compressed)
    elif mutation == "size":
        size = 100000000
    else:
        kind = 9
    data = MAGIC + HEADER.pack(kind, size) + compressed
    path = tmp_path / "bad.aca"
    path.write_bytes(data)
    reader = ArchiveReader(path, hashlib.sha256(data).hexdigest())
    with pytest.raises(ValueError):
        list(reader.rows())
    assert not reader.verified


def test_interruption_leaves_no_published_or_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "evidence.aca"
    with pytest.raises(RuntimeError, match="interrupted"), ArchiveWriter(path) as writer:
        writer.append({"price": "1.00"})
        writer.finish({})
        raise RuntimeError("interrupted")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("symlink", [True, False])
def test_publication_never_overwrites_existing_path(tmp_path: Path, symlink: bool) -> None:
    path, target = tmp_path / "evidence.aca", tmp_path / "target"
    target.write_text("original")
    if symlink:
        path.symlink_to(target)
    else:
        path.write_text("original")
    with pytest.raises(FileExistsError):
        write(path, [{"new": True}])
    assert target.read_text() == path.read_text() == "original"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["evidence.aca", "target"]


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_reader_rejects_non_regular_or_linked_input(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "bad.aca"
    if kind == "fifo":
        os.mkfifo(path)
    else:
        target = tmp_path / "target"
        write(target, [{"value": 1}])
        path.symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        list(ArchiveReader(path, "f" * 64).rows())


def test_writer_cannot_publish_incomplete_or_oversized_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("aca.storage.archive_format.MAX_SEGMENT_BYTES", 1024)
    with ArchiveWriter(tmp_path / "bad.aca") as writer:
        with pytest.raises(ValueError, match="complete"):
            writer.publish()
        with pytest.raises(ValueError, match="empty"):
            writer.finish({})
        with pytest.raises(ValueError, match="row exceeds"):
            writer.append({"value": "x" * 1024})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("limit", ["MAX_ARCHIVE_BYTES", "MAX_DECODED_BYTES", "MAX_SEGMENTS"])
def test_whole_file_budgets_reject_without_publishing_or_accepting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
) -> None:
    path = tmp_path / "valid.aca"
    rows: list[dict[str, object]] = [{"number": n, "value": "x" * 100} for n in range(130)]
    hashed = write(path, rows)
    monkeypatch.setattr("aca.storage.archive_format." + limit, 1)
    reader = ArchiveReader(path, hashed)
    with pytest.raises(ValueError):
        list(reader.rows())
    assert not reader.verified
    with pytest.raises(ValueError):
        write(tmp_path / "rejected.aca", rows)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["valid.aca"]

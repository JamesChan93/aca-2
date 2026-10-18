"""Source integrity, exact range reads and data-quality rejection."""

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from aca.research.archives import RangeArchive
from aca.research.artifacts import canonical, digest, read_json, write_once
from aca.research.datasets import import_dataset, load_dataset


def bundle(tmp_path: Path, rows: bytes | None = None) -> Path:
    data = rows or (
        b"1546300800,100,110,90,101,0.00033370000000000003,2\n1546473600,101,110,90,102,1,2\n"
    )
    (tmp_path / "bars.csv").write_bytes(data)
    source = {
        "schema_version": 1,
        "source_url": "https://example.invalid/test-fixture",
        "retrieved_at": "2026-10-17T00:00:00+00:00",
        "usage_note": "synthetic fixture",
        "assurance": "local_unverified",
        "evidence": {},
        "files": [
            {
                "path": "bars.csv",
                "sha256": digest(data),
                "symbol": "BTC/GBP",
                "interval_minutes": 1440,
                "coverage_start": "2019-01-01T00:00:00+00:00",
                "coverage_end": "2019-01-04T00:00:00+00:00",
            }
        ],
    }
    path = tmp_path / "source.json"
    path.write_bytes(canonical(source))
    return path


def test_audit_preserves_gap_and_raw_volume_evidence(tmp_path: Path) -> None:
    source = bundle(tmp_path)
    target = import_dataset(source, tmp_path / "output")
    dataset = load_dataset(target)
    data = dataset.series[("BTC/GBP", 1440)]
    assert len(data) == 2
    assert str(data[0].volume) == "0.000333700000000000"
    assert dataset.manifest["series"][0]["gaps"] == [  # type: ignore[index]
        {
            "start": "2019-01-02T00:00:00+00:00",
            "end": "2019-01-03T00:00:00+00:00",
            "classification": "unexplained",
        }
    ]
    assert import_dataset(source, tmp_path / "output") == target
    object_path = next((tmp_path / "output/objects").glob("*.csv"))
    assert b"00000003" in object_path.read_bytes()
    object_path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash"):
        load_dataset(target)


@pytest.mark.parametrize(
    "row",
    [
        b"timestamp,open,high,low,close,volume,trades\n",
        b"1546300800,100,99,90,101,1,2\n",
        b"1546300800,NaN,110,90,101,1,2\n",
        b"1546300801,100,110,90,101,1,2\n",
        b"1546300800,100,110,90,101,0,0\n",
        b"1546300800,100,110,90,101,1,2\n1546300800,100,110,90,101,1,2\n",
    ],
)
def test_corrupt_archive_rows_rejected(tmp_path: Path, row: bytes) -> None:
    with pytest.raises(ValueError):
        import_dataset(bundle(tmp_path, row), tmp_path / "output")


def test_raw_source_hash_and_manifest_changes_rejected(tmp_path: Path) -> None:
    source = bundle(tmp_path)
    (tmp_path / "bars.csv").write_text("altered")
    with pytest.raises(ValueError, match="hash"):
        import_dataset(source, tmp_path / "output")
    source = bundle(tmp_path)
    path = import_dataset(source, tmp_path / "output")
    path.write_bytes(canonical({"altered": True}))
    with pytest.raises(ValueError, match="manifest content hash"):
        load_dataset(path)


def test_immutable_json_duplicate_keys_and_traversal(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    write_once(path, b"first")
    write_once(path, b"first")
    with pytest.raises(ValueError, match="immutable"):
        write_once(path, b"changed")
    path.write_text('{"x":1,"x":2}')
    with pytest.raises(ValueError, match="duplicate"):
        read_json(path)
    source = bundle(tmp_path)
    document = json.loads(source.read_text())
    document["files"][0]["path"] = "../outside.csv"
    source.write_bytes(canonical(document))
    with pytest.raises(ValueError, match="inside"):
        import_dataset(source, tmp_path / "output")


@pytest.mark.parametrize(
    "fault", [None, "ignored_range", "etag_changed", "wrong_range", "truncated"]
)
def test_range_archive_exact_reads_and_fail_closed(fault: str | None) -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("test.csv", b"test-data" * 100)
    zipped = stream.getvalue()
    # Five unequal parts exercise reads crossing the concatenation boundaries.
    chunks = [zipped[:30], zipped[30:60], zipped[60:90], zipped[90:120], zipped[120:]]

    def respond(request: httpx.Request) -> httpx.Response:
        index = int(request.url.path[-2:])
        content = chunks[index]
        headers = {"etag": '"stable"', "content-length": str(len(content))}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        left, right = map(int, request.headers["range"][6:].split("-"))
        headers["content-range"] = f"bytes {left}-{right}/{len(content)}"
        data = content[left : right + 1]
        if fault == "etag_changed":
            headers["etag"] = '"changed"'
        if fault == "wrong_range":
            headers["content-range"] = "bytes 0-1/2"
        if fault == "truncated":
            data = data[:-1]
        return httpx.Response(
            200 if fault == "ignored_range" else 206, headers=headers, content=data
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        reader = RangeArchive(client)
        if fault:
            with pytest.raises(ValueError):
                reader.read(50)
        else:
            with zipfile.ZipFile(reader) as archive:
                assert archive.read("test.csv") == b"test-data" * 100
            assert reader.transferred < 1000
            assert reader.seek(0) == 0
            with pytest.raises(ValueError, match="budget"):
                reader.transferred = 64_000_001
                reader.read(1)


def test_source_needs_aware_timestamp(tmp_path: Path) -> None:
    path = bundle(tmp_path)
    document = read_json(path)
    document["retrieved_at"] = datetime(2026, 10, 17).isoformat()
    path.write_bytes(canonical(document))
    with pytest.raises(ValueError):
        import_dataset(path, tmp_path / "output")


def test_partial_csv_does_not_invent_trailing_coverage(tmp_path: Path) -> None:
    target = import_dataset(
        bundle(tmp_path, b"1546300800,100,110,90,101,1,2\n"), tmp_path / "output"
    )
    manifest = read_json(target)
    assert manifest["series"][0]["gaps"][0]["end"] == "2019-01-04T00:00:00+00:00"  # type: ignore[index]
    assert load_dataset(target).series[("BTC/GBP", 1440)][0].end == datetime(2019, 1, 2, tzinfo=UTC)

"""Bounded official archive extraction. No general URL fetcher or ZIP extraction."""

import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from aca.research.artifacts import canonical, digest, file_hash, write_once

BASE = "https://assets.kraken.com/marketing/institutions/"
PARTS = tuple(BASE + f"Kraken_OHLCVT_Full.zip.part{index:02}" for index in range(5))
FULL_SHA256 = "bc273e9027470b07d790a7fa4df9bb43f0dfc527c20c6ecae48402e123e7b1c7"
DOCUMENTATION = (
    "https://support.kraken.com/articles/"
    "360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data"
)
MEMBERS = {
    f"{pair}_{interval}.csv": (symbol, interval)
    for pair, symbol in (("XBTGBP", "BTC/GBP"), ("ETHGBP", "ETH/GBP"))
    for interval in (240, 1440)
}


class RangeArchive(io.RawIOBase):
    """Read concatenated parts with exact HTTP ranges, ETag checks and a 64MB budget."""

    def __init__(self, client: httpx.Client) -> None:
        super().__init__()
        self.client = client
        self.position = 0
        self.transferred = 0
        self.requests = 0
        self.parts: list[dict[str, Any]] = []
        for url in PARTS:
            response = client.head(url, headers={"Accept-Encoding": "identity"})
            response.raise_for_status()
            length = int(response.headers["content-length"])
            etag = response.headers.get("etag")
            if not 0 < length <= 3_000_000_000 or not etag or etag.startswith("W/"):
                raise ValueError("archive requires a bounded size and strong ETag")
            self.parts.append({"url": url, "size": length, "etag": etag})
        self.length = sum(part["size"] for part in self.parts)

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = 0) -> int:
        target = (
            offset
            if whence == 0
            else self.position + offset
            if whence == 1
            else self.length + offset
        )
        if whence not in (0, 1, 2) or not 0 <= target <= self.length:
            raise ValueError("invalid archive seek")
        self.position = target
        return target

    def read(self, size: int = -1) -> bytes:
        size = self.length - self.position if size < 0 else min(size, self.length - self.position)
        if size > 8_000_000 or self.transferred + size > 64_000_000 or self.requests > 200:
            raise ValueError("archive transfer budget exceeded")
        output = bytearray()
        offset = 0
        for part in self.parts:
            end = offset + part["size"]
            if self.position < end and size:
                local = self.position - offset
                count = min(size, end - self.position)
                headers = {
                    "Range": f"bytes={local}-{local + count - 1}",
                    "If-Match": part["etag"],
                    "Accept-Encoding": "identity",
                }
                self.requests += 1
                with self.client.stream("GET", part["url"], headers=headers) as response:
                    expected = f"bytes {local}-{local + count - 1}/{part['size']}"
                    if (
                        response.status_code != 206
                        or response.headers.get("content-range") != expected
                        or response.headers.get("etag") != part["etag"]
                        or response.headers.get("content-encoding", "identity") != "identity"
                    ):
                        raise ValueError("archive range or identity changed")
                    received = 0
                    for chunk in response.iter_bytes(65536):
                        received += len(chunk)
                        if received > count:
                            raise ValueError("oversized archive response")
                        output.extend(chunk)
                    if received != count:
                        raise ValueError("truncated archive range")
                self.transferred += count
                self.position += count
                size -= count
            offset = end
        return bytes(output)


def acquire(directory: Path, archive: Path | None = None) -> Path:
    """Fetch only four CSV members; a full local archive can prove the published hash."""
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    evidence: dict[str, object] = {"published_full_sha256": FULL_SHA256}
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        if archive is None:
            reader: Any = RangeArchive(client)
            assurance = "range_members_crc"
            evidence["parts"] = reader.parts
        else:
            if file_hash(archive) != FULL_SHA256:
                raise ValueError("full archive does not match published SHA-256")
            reader = archive.open("rb")
            assurance = "full_archive_sha256"
        try:
            with zipfile.ZipFile(reader) as zipped:
                if len(zipped.infolist()) > 100_000:
                    raise ValueError("excessive archive directory")
                selected: dict[str, zipfile.ZipInfo] = {}
                for info in zipped.infolist():
                    name = Path(info.filename).name
                    if name in MEMBERS or name == "MANIFEST.json":
                        if (
                            name in selected
                            or info.file_size > 32_000_000
                            or info.compress_size > 8_000_000
                        ):
                            raise ValueError("duplicate or oversized selected member")
                        selected[name] = info
                if not set(MEMBERS).issubset(selected):
                    raise ValueError("required archive members absent")
                files: list[dict[str, object]] = []
                for name, (symbol, interval) in MEMBERS.items():
                    info = selected[name]
                    data = zipped.read(info)  # zipfile verifies each selected member's CRC32.
                    write_once(directory / name, data)
                    files.append(
                        {
                            "path": name,
                            "symbol": symbol,
                            "interval_minutes": interval,
                            "sha256": digest(data),
                            # Archive documentation explicitly declares its upper coverage boundary.
                            # The first populated row sets the conservative lower boundary.
                            "coverage_start": datetime.fromtimestamp(
                                int(data.split(b",", 1)[0]), UTC
                            ).isoformat(),
                            "coverage_end": (
                                datetime.fromtimestamp(
                                    int(data.rstrip().splitlines()[-1].split(b",", 1)[0]), UTC
                                )
                                + timedelta(minutes=interval)
                            ).isoformat(),
                        }
                    )
                if "MANIFEST.json" in selected:
                    archive_manifest = zipped.read(selected["MANIFEST.json"])
                    write_once(directory / "archive-manifest.json", archive_manifest)
                    evidence["archive_manifest_sha256"] = digest(archive_manifest)
                    evidence["documentation_coverage_end"] = "2026-06-30"
                    evidence["coverage_requires_review"] = True
                    evidence["coverage_note"] = (
                        "CSV coverage is bounded by populated rows. Compare the retained "
                        "archive manifest with the June download-page boundary before promotion."
                    )
                evidence["selected_members"] = {
                    name: {"crc32": info.CRC, "size": info.file_size}
                    for name, info in selected.items()
                }
            if archive is None:
                evidence["transferred_bytes"] = reader.transferred
                evidence["range_requests"] = reader.requests
        finally:
            reader.close()
    document = {
        "schema_version": 1,
        "source_url": DOCUMENTATION,
        "retrieved_at": now.isoformat(),
        "usage_note": "Official free research download; preserve source attribution. "
        "No redistribution licence inferred. Review Kraken terms before redistribution.",
        "assurance": assurance,
        "evidence": evidence,
        "files": files,
    }
    # Each retrieval is a separate provenance record; CSVs remain immutable.
    path = directory / ("source-" + digest(canonical(document)) + ".json")
    write_once(path, canonical(document))
    return path

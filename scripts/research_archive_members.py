"""Fetch selected official archive members for the gap audit, with bounded ranges.

Host Python 3.9+, standard library only. Never extracts paths from a ZIP. At most
256MB network bytes, 1,200 requests and 500MB per decoded trade member. Existing
members may be reused only after size/CRC verification against the current ZIP.
No exchange credentials, strategies, holdout opening, database or order commands.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import urllib.request
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE = "https://assets.kraken.com/marketing/institutions/"
ARCHIVES = {
    "ohlc": [BASE + f"Kraken_OHLCVT_Full.zip.part{i:02}" for i in range(5)],
    "quarter": [BASE + "Kraken_OHLCVT_2026Q3.zip"],
    "trades": [BASE + f"Kraken_Trades_Full.zip.part{i:02}" for i in range(14)],
}
REST_URL = "https://api.kraken.com/0/public/Trades?pair=XBTGBP&since=1782202664984300900&count=20"
ALLOWLIST = frozenset(url for urls in ARCHIVES.values() for url in urls) | {REST_URL}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Transport:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.transferred = 0
        self.requests = 0

    def request(
        self,
        url: str,
        *,
        head: bool = False,
        start: int = 0,
        count: int = 0,
        part: dict[str, Any] | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        if url not in ALLOWLIST or self.requests >= 1200 or count > 4_000_000:
            raise ValueError("URL or request budget refused")
        if self.transferred + count > 256_000_000:
            raise ValueError("network byte budget exceeded")
        headers = {"Accept-Encoding": "identity", "User-Agent": "curl/8.7.1"}
        if part is not None:
            headers.update(
                {"If-Match": part["etag"], "Range": f"bytes={start}-{start + count - 1}"}
            )
        request = urllib.request.Request(  # noqa: S310 -- exact HTTPS allowlist above.
            url, method="HEAD" if head else "GET", headers=headers
        )
        self.requests += 1
        with self.opener.open(request, timeout=30) as response:
            metadata = {key.lower(): value for key, value in response.headers.items()}
            if (
                response.geturl() != url
                or metadata.get("content-encoding", "identity") != "identity"
            ):
                raise ValueError("redirect or content encoding refused")
            if part is not None:
                expected = f"bytes {start}-{start + count - 1}/{part['size']}"
                if (
                    response.status != 206
                    or metadata.get("content-range") != expected
                    or metadata.get("etag") != part["etag"]
                ):
                    raise ValueError("archive identity or byte range changed")
            elif response.status != 200:
                raise ValueError("unexpected HTTP status")
            data = b"" if head else response.read(count + 1)
            if len(data) > count or (part is not None and len(data) != count):
                raise ValueError("oversized or truncated response")
        self.transferred += len(data)
        return data, metadata


class RemoteArchive(io.RawIOBase):
    def __init__(self, transport: Transport, urls: list[str]) -> None:
        super().__init__()
        self.transport = transport
        self.position = 0
        self.parts: list[dict[str, Any]] = []
        for url in urls:
            _, headers = transport.request(url, head=True)
            size, etag = int(headers["content-length"]), headers.get("etag", "")
            if not 0 < size <= 3_000_000_000 or not etag or etag.startswith("W/"):
                raise ValueError("strong ETag and bounded part size required")
            self.parts.append({"url": url, "size": size, "etag": etag})
        self.length = sum(part["size"] for part in self.parts)

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = 0) -> int:
        position = (
            offset
            if whence == 0
            else self.position + offset
            if whence == 1
            else self.length + offset
        )
        if whence not in (0, 1, 2) or not 0 <= position <= self.length:
            raise ValueError("invalid ZIP seek")
        self.position = position
        return position

    def read(self, size: int = -1) -> bytes:
        size = min(self.length - self.position, size if size >= 0 else self.length)
        if size > 8_000_000:
            raise ValueError("single ZIP read budget exceeded")
        output = bytearray()
        offset = 0
        for part in self.parts:
            end = offset + part["size"]
            while size and self.position < end:
                local = self.position - offset
                count = min(size, end - self.position, 4_000_000)
                data, _ = self.transport.request(part["url"], start=local, count=count, part=part)
                output.extend(data)
                self.position += count
                size -= count
            offset = end
        return bytes(output)


def identity(path: Path) -> dict[str, Any]:
    checksum, fingerprint, size = 0, hashlib.sha256(), 0
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            checksum = zlib.crc32(data, checksum)
            fingerprint.update(data)
            size += len(data)
    return {"size": size, "crc32": checksum, "sha256": fingerprint.hexdigest()}


def immutable(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("existing evidence differs; choose a new snapshot directory")
    else:
        with path.open("xb") as stream:
            stream.write(data)


def fetch(directory: Path) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "snapshot.json").exists():
        raise ValueError("completed snapshot is immutable; choose a new directory")
    transport = Transport()
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
        "archives": {},
        "full_archive_sha256_verified": False,
    }
    for kind, urls in ARCHIVES.items():
        reader = RemoteArchive(transport, urls)
        members = []
        with zipfile.ZipFile(reader) as archive:
            if len(archive.infolist()) > 100_000:
                raise ValueError("ZIP directory budget exceeded")
            names = {"MANIFEST.json"} | (
                {f"{pair}.csv" for pair in ("XBTGBP", "ETHGBP")}
                if kind == "trades"
                else {
                    f"{pair}_{interval}.csv"
                    for pair in ("XBTGBP", "ETHGBP")
                    for interval in (60, 240, 1440)
                }
            )
            for name in sorted(names):
                matches = [info for info in archive.infolist() if info.filename == name]
                if len(matches) != 1:
                    raise ValueError("missing or duplicate exact member name")
                info = matches[0]
                if (
                    info.file_size > (500_000_000 if kind == "trades" else 32_000_000)
                    or info.compress_size > 128_000_000
                ):
                    raise ValueError("selected member budget exceeded")
                target = directory / (kind + "-" + name)
                if not target.exists():
                    temporary = target.with_suffix(target.suffix + ".partial")
                    try:
                        with archive.open(info) as source, temporary.open("xb") as output:
                            total = 0
                            while data := source.read(1024 * 1024):
                                total += len(data)
                                if total > info.file_size:
                                    raise ValueError("decoded member exceeds declared size")
                                output.write(data)
                        temporary.rename(target)
                    finally:
                        temporary.unlink(missing_ok=True)
                actual = identity(target)
                if actual["size"] != info.file_size or actual["crc32"] != info.CRC:
                    raise ValueError("local member fails official ZIP size/CRC")
                members.append(
                    {
                        "name": name,
                        "bytes": info.file_size,
                        "compressed": info.compress_size,
                        "crc32": info.CRC,
                        "sha256": actual["sha256"],
                    }
                )
        document = {"parts": reader.parts, "members": members}
        # Earlier exploratory directory captures remain preserved separately.
        immutable(
            directory / (kind + "-verified-directory.json"),
            (json.dumps(document, sort_keys=True) + "\n").encode(),
        )
        evidence["archives"][kind] = document
        print(
            json.dumps({"verified": kind, "network_bytes_so_far": transport.transferred}),
            flush=True,
        )
    receipt, headers = transport.request(REST_URL, count=1_000_000)
    immutable(directory / "btc-id-gap-rest-verified.json", receipt)
    evidence["rest"] = {
        "url": REST_URL,
        "sha256": hashlib.sha256(receipt).hexdigest(),
        "server_date": headers.get("date"),
    }
    evidence.update({"requests": transport.requests, "network_bytes": transport.transferred})
    immutable(directory / "snapshot.json", (json.dumps(evidence, sort_keys=True) + "\n").encode())
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    evidence = fetch(args.directory)
    print(
        json.dumps(
            {
                "snapshot": str(args.directory / "snapshot.json"),
                "network_bytes": evidence["network_bytes"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Canonical content-addressed artefacts and bounded duplicate-key-free JSON."""

import hashlib
import json
from pathlib import Path
from typing import cast


def canonical(document: object) -> bytes:
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def read_json(path: Path) -> dict[str, object]:
    if path.stat().st_size > 16_000_000:
        raise ValueError("research JSON exceeds budget")
    document = json.loads(path.read_bytes(), object_pairs_hook=_pairs)
    if not isinstance(document, dict):
        raise ValueError("expected JSON object")
    # Also rejects NaN/Infinity accepted by the standard decoder.
    canonical(document)
    return cast(dict[str, object], document)


def write_once(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError("immutable artefact already exists with different content") from None


def save_document(directory: Path, document: dict[str, object]) -> Path:
    data = canonical(document)
    path = directory / (digest(data) + ".json")
    write_once(path, data)
    return path

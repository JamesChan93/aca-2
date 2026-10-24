"""Independent lifecycle, streaming, privacy and adversarial offline-bundle checks."""

import io
import json
import subprocess
import sys
import tarfile
import tracemalloc
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from aca.operations.cli import main
from aca.research.artifacts import canonical, digest, file_hash, read_json, save_document
from aca.research.bundles import (
    OBJECT_BYTES,
    Artifact,
    Event,
    ObjectRef,
    Packet,
    Result,
    Store,
    archive,
    create,
    execute,
    export_report,
    preserve_report,
    restore,
    seal,
    verify,
)
from aca.research.runner import run_modular


def study(root: Path) -> Path:
    return create(root, {"dataset": "synthetic", "seed": 1729}, {"BTC/GBP:1440:sma200": {}})


def completed(store: Store) -> Result:
    return Result(
        status="completed",
        reason="synthetic",
        artifacts=(
            store.records("curve", ({"day": index, "nav": "250.00"} for index in range(20))),
            store.records("episodes", []),
        ),
    )


def events(path: Path) -> list[Event]:
    return [
        Event.model_validate_json(item.read_bytes())
        for item in sorted((path.parent / "events").glob("*.json"))
    ]


def packet_events(packet: Path) -> list[Event]:
    document = Packet.model_validate_json(packet.read_bytes())
    store = Store(packet.parents[1])
    return [store.model(ref, Event) for ref in document.events]


def test_resume_identity_and_complete_offline_restore(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    path = study(root)
    assert study(root) == path
    result = execute(path, "BTC/GBP:1440:sma200", completed)
    assert result.attempt == 1
    assert execute(path, result.task, lambda _: pytest.fail("completed trial recomputed")) == result
    packet = seal(path)
    assert len(events(path)) == 2
    assert verify(packet)["tasks"] == {
        result.task: {
            "status": "completed",
            "reason": "synthetic",
            "attempt": 1,
        }
    }
    backup = tmp_path / "bundle.tar"
    archive(packet, backup)
    second = tmp_path / "second.tar"
    archive(packet, second)
    assert file_hash(backup) == file_hash(second)
    restored = restore(backup, tmp_path / "restored")
    assert restored.name == packet.name and verify(restored) == verify(packet)
    restored_study = restored.parents[1] / "studies" / path.parent.name / "manifest.json"
    assert (
        execute(restored_study, result.task, lambda _: pytest.fail("restore recomputed")) == result
    )
    assert seal(restored_study).read_bytes() == packet.read_bytes()
    with pytest.raises(FileExistsError):
        archive(packet, backup)
    with pytest.raises(ValueError, match="must not exist"):
        restore(backup, root)


def test_failure_is_retained_and_retry_requires_explicit_choice(tmp_path: Path) -> None:
    path = study(tmp_path)

    def failed(_: Store) -> Result:
        raise ArithmeticError("private payload and credential must not enter the event")

    with pytest.raises(ArithmeticError):
        execute(path, "BTC/GBP:1440:sma200", failed)
    result = execute(path, "BTC/GBP:1440:sma200", lambda _: pytest.fail("implicit retry"))
    assert result.status == "failed"
    failed_packet = seal(path)
    assert verify(failed_packet)["failed_attempts"] == 1
    result = execute(path, result.task, completed, retry_failed=True)
    assert result.attempt == 2
    packet = seal(path)
    assert [item.status for item in packet_events(packet)] == [
        "started",
        "failed",
        "started",
        "completed",
    ]
    assert verify(failed_packet)["failed_attempts"] == 1  # Earlier snapshot stays valid.
    assert all(
        b"private payload" not in file.read_bytes() for file in (tmp_path / "objects").iterdir()
    )


def test_real_process_death_preserves_start_and_recomputes_only_interrupted_task(
    tmp_path: Path,
) -> None:
    path = study(tmp_path)
    program = """
import os, sys
from pathlib import Path
from aca.research.bundles import execute
def die(store):
    store.put(b'orphan output before crash')
    os._exit(9)
execute(Path(sys.argv[1]), 'BTC/GBP:1440:sma200', die)
"""
    killed = subprocess.run(  # noqa: S603 - fixed program and disposable pytest path.
        [sys.executable, "-c", program, str(path)],
        timeout=15,
        check=False,
    )
    assert killed.returncode == 9
    assert [event.status for event in events(path)] == ["started"]
    with pytest.raises(ValueError, match="unfinished"):
        seal(path)
    result = execute(path, "BTC/GBP:1440:sma200", completed)
    assert result.attempt == 2
    assert [event.status for event in events(path)] == [
        "started",
        "abandoned",
        "started",
        "completed",
    ]
    packet = seal(path)
    assert verify(packet)["abandoned_attempts"] == 1
    backup = tmp_path / "archive.tar"
    archive(packet, backup)
    with tarfile.open(backup) as bundle:
        assert "objects/" + digest(b"orphan output before crash") not in bundle.getnames()


def test_exclusive_writer_and_undeclared_work(tmp_path: Path) -> None:
    path = study(tmp_path)
    with pytest.raises(ValueError, match="undeclared"):
        execute(path, "other", completed)

    def nested(store: Store) -> Result:
        with pytest.raises(ValueError, match="already has a writer"):
            execute(path, "BTC/GBP:1440:sma200", completed)
        return completed(store)

    execute(path, "BTC/GBP:1440:sma200", nested)
    verify(seal(path))


def test_large_complete_study_streams_beyond_legacy_reader_budget(tmp_path: Path) -> None:
    path = study(tmp_path)
    tracemalloc.start()
    try:
        result = execute(
            path,
            "BTC/GBP:1440:sma200",
            lambda store: Result(
                status="rejected",
                reason="synthetic_no_edge",
                artifacts=(
                    store.records(
                        "curve",
                        (
                            {"day": day, "nav": "250", "evidence": "x" * 2048}
                            for day in range(10_000)
                        ),
                    ),
                    store.records("episodes", [{"pnl": "-1.00", "status": "open"}]),
                ),
            ),
        )
        packet = seal(path)
        assert result.artifacts[0].size_bytes > 20_000_000
        assert verify(packet)["promotion_allowed"] is False
        backup = tmp_path / "large.tar"
        archive(packet, backup)
        recovered = restore(backup, tmp_path / "recovered")
        assert verify(recovered) == verify(packet)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8_000_000, f"streaming allocated {peak} bytes"
    assert max(file.stat().st_size for file in (tmp_path / "objects").iterdir()) <= OBJECT_BYTES
    giant = tmp_path / "old-monolith.json"
    with giant.open("wb") as stream:
        stream.write(b" " * 16_000_001)
    with pytest.raises(ValueError, match="exceeds budget"):
        read_json(giant)


@pytest.mark.parametrize("damage", ["part", "missing", "aggregate", "event_order", "event_deleted"])
def test_corruption_blocks_reuse_and_review(tmp_path: Path, damage: str) -> None:
    path = study(tmp_path)
    result = execute(path, "BTC/GBP:1440:sma200", completed)
    packet = seal(path)
    store = Store(tmp_path)
    if damage in {"part", "missing"}:
        part = store.path(result.artifacts[0].parts[0])
        if damage == "missing":
            part.unlink()
        else:
            part.write_bytes(b"!" * part.stat().st_size)
        with pytest.raises((ValueError, FileNotFoundError)):
            execute(path, result.task, completed)
        with pytest.raises((ValueError, FileNotFoundError)):
            verify(packet)
    elif damage == "aggregate":
        invalid = result.artifacts[0].model_copy(update={"sha256": "0" * 64})
        with pytest.raises(ValueError, match="aggregate"):
            list(store.chunks(invalid))
    elif damage == "event_deleted":
        next((path.parent / "events").glob("0001-*.json")).unlink()
        with pytest.raises(ValueError, match="filename/order"):
            seal(path)
    else:
        document = Packet.model_validate_json(packet.read_bytes())
        changed = document.model_copy(update={"events": tuple(reversed(document.events))})
        ref = store.save(changed)
        target = tmp_path / "packets" / f"{ref.sha256}.json"
        target.write_bytes(store.get(ref))
        with pytest.raises(ValueError, match="event chain"):
            verify(target)


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", "file"),
        ("symlink", "link"),
        ("objects/" + "0" * 64, "file"),
        ("packet.json", "duplicate"),
    ],
)
def test_malicious_archives_never_publish_destination(tmp_path: Path, name: str, kind: str) -> None:
    source = tmp_path / "invalid.tar"
    with tarfile.open(source, "w") as bundle:
        metadata = tarfile.TarInfo(name)
        metadata.size = 2
        if kind == "link":
            metadata.type = tarfile.SYMTYPE
            metadata.linkname = "../escape"
            metadata.size = 0
        bundle.addfile(metadata, io.BytesIO(b"{}"))
        if kind == "duplicate":
            bundle.addfile(metadata, io.BytesIO(b"{}"))
    destination = tmp_path / "destination"
    with pytest.raises(ValueError):
        restore(source, destination)
    assert not destination.exists() and not (tmp_path / "escape").exists()


def test_legacy_byte_identity_and_cli_offline_round_trip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = save_document(
        tmp_path / "reports",
        {
            "schema_version": 1,
            "kind": "research_report",
            "holdout_opened": False,
            "trials": [{"status": "rejected", "pnl": "-0.10"}],
        },
    )
    root = tmp_path / "bundle"
    assert (
        main(["research", "bundle-preserve", "--report", str(report), "--directory", str(root)])
        == 0
    )
    packet = Path(json.loads(capsys.readouterr().out)["packet"])
    assert preserve_report(report, root) == packet
    backup = tmp_path / "bundle.tar"
    assert (
        main(["research", "bundle-archive", "--packet", str(packet), "--output", str(backup)]) == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "research",
                "bundle-restore",
                "--archive",
                str(backup),
                "--directory",
                str(tmp_path / "restored"),
            ]
        )
        == 0
    )
    restored = Path(json.loads(capsys.readouterr().out)["packet"])
    assert main(["research", "bundle-review", "--packet", str(restored)]) == 0
    assert json.loads(capsys.readouterr().out)["execution_permission"] is False
    output = tmp_path / "reproduced.json"
    assert (
        main(
            ["research", "bundle-export-report", "--packet", str(restored), "--output", str(output)]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["report_sha256"] == report.stem
    assert output.read_bytes() == report.read_bytes()
    with pytest.raises(FileExistsError):
        export_report(restored, output)


def test_contract_bounds_and_duplicate_json_keys(tmp_path: Path) -> None:
    store = Store(tmp_path)
    with pytest.raises(ValidationError):
        ObjectRef(sha256="../escape", size_bytes=1)
    with pytest.raises(ValidationError):
        store.put(b"x" * (OBJECT_BYTES + 1))
    with pytest.raises(ValueError, match="record exceeds"):
        store.records("curve", [{"large": "x" * OBJECT_BYTES}])
    duplicate = store.put(b'{"sha256":"' + b"0" * 64 + b'","size_bytes":1,"size_bytes":2}\n')
    with pytest.raises(ValueError, match="duplicate"):
        store.model(duplicate, ObjectRef)
    with pytest.raises(ValidationError):
        Artifact(kind="curve", encoding="binary", sha256="0" * 64, size_bytes=-1, parts=())
    with pytest.raises(ValidationError):
        Result(status="approved", reason="synthetic")  # type: ignore[arg-type]


def test_changed_declaration_gets_different_study_and_incomplete_study_cannot_seal(
    tmp_path: Path,
) -> None:
    path = study(tmp_path)
    with pytest.raises(ValueError, match="unfinished"):
        seal(path)
    changed = create(tmp_path, {"dataset": "changed", "seed": 1729}, {"BTC/GBP:1440:sma200": {}})
    assert path != changed
    document = read_json(path)
    document["promotion_allowed"] = True
    path.write_bytes(canonical(document))
    with pytest.raises(ValueError, match="identity"):
        execute(path, "BTC/GBP:1440:sma200", completed)


def test_modular_runner_splits_exact_legacy_outputs_and_resumes_rejected_trials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "dataset.json"
    manifest.write_bytes(canonical({"synthetic": True}))
    monkeypatch.setattr("aca.research.runner.TRIALS", (("BTC/GBP", 1440, "sma200"),))
    monkeypatch.setattr("aca.research.runner.load_dataset", lambda _: object())
    original: dict[str, Any] = {
        "trial": "BTC/GBP:1440:sma200",
        "status": "rejected",
        "reasons": ["no_edge"],
        "folds": [
            {
                "status": "complete",
                "train_start": "synthetic",
                "test": {
                    "candidate": {"fees_gbp": "0.10"},
                    "daily_candidate": {"2020-01-01": -0.001},
                    "daily_cash": {"2020-01-01": 0.0},
                    "daily_passive10": {"2020-01-01": 0.002},
                    "episodes": [{"pnl": "-0.25"}],
                },
            }
        ],
    }
    calls = []

    def trial(*_: object) -> dict[str, object]:
        calls.append(True)
        return deepcopy(original)

    monkeypatch.setattr("aca.research.runner._trial", trial)
    root = tmp_path / "bundle"
    packet = run_modular(root, manifest, samples=1000)
    assert run_modular(root, manifest, samples=1000) == packet
    assert len(calls) == 1
    store = Store(root)
    result = packet_events(packet)[-1]
    records: dict[str, list[dict[str, Any]]] = {
        artifact.kind: [
            json.loads(line) for page in store.chunks(artifact) for line in page.splitlines()
        ]
        for artifact in result.artifacts
    }
    reconstructed = records["trial"][0]
    reconstructed["folds"] = records["fold"]
    section = reconstructed["folds"][0]["test"]
    for curve in records["curve"]:
        section.setdefault(curve["series"], {})[curve["day"]] = curve["return"]
    section["episodes"] = [item["episode"] for item in records["episodes"]]
    assert reconstructed == original
    assert verify(packet)["promotion_allowed"] is False
    assert not (root / "holdout-openings").exists()


def test_modular_runner_failure_then_explicit_retry_keeps_all_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "dataset.json"
    manifest.write_bytes(canonical({"synthetic": True}))
    monkeypatch.setattr("aca.research.runner.TRIALS", (("BTC/GBP", 1440, "sma200"),))

    def missing(_: Path) -> None:
        raise ValueError("invalid synthetic dataset")

    monkeypatch.setattr("aca.research.runner.load_dataset", missing)
    root = tmp_path / "bundle"
    with pytest.raises(ValueError, match="invalid synthetic"):
        run_modular(root, manifest, samples=1000)
    with pytest.raises(ValueError, match="explicit --retry-failed"):
        run_modular(root, manifest, samples=1000)
    monkeypatch.setattr("aca.research.runner.load_dataset", lambda _: object())
    monkeypatch.setattr(
        "aca.research.runner._trial",
        lambda *_: {
            "trial": "synthetic",
            "status": "rejected",
            "folds": [],
            "reasons": ["no_history"],
        },
    )
    packet = run_modular(root, manifest, samples=1000, retry_failed=True)
    assert [event.status for event in packet_events(packet)] == [
        "started",
        "failed",
        "started",
        "rejected",
    ]

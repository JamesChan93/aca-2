"""Snapshot protocol, sequence safety and interrupted-helper cleanup guards."""

import copy
import json
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from restore_fixture import snapshot_id

from scripts.restore_drill import (
    Drill,
    DrillError,
    SnapshotSession,
    stop_child,
    transactional_view,
    verify_sequences,
)


def evidence(counter: int, *, called: bool = True, maximum: int | None = 64) -> dict[str, Any]:
    return {
        "sequences": {"public_replay_events_number_seq": [counter, called]},
        "sequence_row_maxima": {"public_replay_events_number_seq": maximum},
        "tables": {"paper_events": {"rows": 291, "sha256": "a" * 64}},
        "economics": {"validated": {"cash_gbp": "1064.00"}},
        "catalog_sha256": {"sequence_definitions": "b" * 64},
    }


@pytest.mark.parametrize("counter", [64, 65, 80, 96])
def test_restored_sequence_may_advance_without_importing_future_rows(counter: int) -> None:
    before, after, restored = evidence(64), evidence(96), evidence(counter)
    verify_sequences(before, after, restored)
    assert transactional_view(before) == transactional_view(restored)
    changed = copy.deepcopy(restored)
    changed["tables"]["paper_events"]["rows"] += 1
    assert transactional_view(before) != transactional_view(changed)
    changed = copy.deepcopy(restored)
    changed["economics"]["validated"]["cash_gbp"] = "1096.00"
    assert transactional_view(before) != transactional_view(changed)
    changed = copy.deepcopy(restored)
    changed["catalog_sha256"]["sequence_definitions"] = "c" * 64
    assert transactional_view(before) != transactional_view(changed)


@pytest.mark.parametrize("counter", [1, 63, 97, 100000])
def test_restored_counter_cannot_regress_or_exceed_fenced_source(counter: int) -> None:
    with pytest.raises(DrillError, match="outside captured bounds"):
        verify_sequences(evidence(64), evidence(96), evidence(counter))


@pytest.mark.parametrize("pair", [[True, True], [64, 1], [64], [0, True], [64, False], "64"])
def test_invalid_sequence_values_fail(pair: Any) -> None:
    restored = evidence(64)
    restored["sequences"]["public_replay_events_number_seq"] = pair
    with pytest.raises(DrillError):
        verify_sequences(evidence(64), evidence(96), restored)


def test_owned_sequence_cannot_reuse_a_committed_identifier() -> None:
    before, after, restored = evidence(64), evidence(96), evidence(64)
    before["sequence_row_maxima"]["public_replay_events_number_seq"] = 65
    restored["sequence_row_maxima"]["public_replay_events_number_seq"] = 65
    with pytest.raises(DrillError, match="reuse"):
        verify_sequences(before, after, restored)
    # An empty, never-called sequence can stay uncalled, but cannot be reset
    # after a captured nextval. All counters still need valid positive values.
    verify_sequences(
        evidence(1, called=False, maximum=None),
        evidence(2),
        evidence(1, called=False, maximum=None),
    )


def test_missing_or_added_sequence_evidence_is_not_silently_ignored() -> None:
    before, after, restored = evidence(64), evidence(96), evidence(80)
    del restored["sequences"]["public_replay_events_number_seq"]
    with pytest.raises(DrillError, match="sequence set"):
        verify_sequences(before, after, restored)
    for key in ("sequences", "sequence_row_maxima"):
        value = evidence(64)
        del value[key]
        with pytest.raises(DrillError, match="missing sequence"):
            transactional_view(value)


@pytest.mark.parametrize("value", ["expired", "a' OR true", "00000001-00000001-1\n", "", "1-2-3"])
def test_snapshot_identifiers_are_strict_data(value: str) -> None:
    with pytest.raises(ValueError):
        snapshot_id(value)


@contextmanager
def child(script: str) -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(  # noqa: S603 - local test interpreter and literal fixture programs
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield process
    finally:
        stop_child(process)


def test_protocol_preserves_multiple_buffered_messages_and_exact_commands() -> None:
    script = (
        "import json, sys; "
        "print(json.dumps({'phase':'ready'}), flush=True); "
        "print(json.dumps({'phase':'exercised'}), flush=True); "
        "assert sys.stdin.readline() == 'finish\\n'; "
        "print(json.dumps({'phase':'closed'}), flush=True)"
    )
    with child(script) as process:
        session = SnapshotSession(process)
        assert session.receive("ready") == {"phase": "ready"}
        assert session.receive("exercised") == {"phase": "exercised"}
        with pytest.raises(DrillError, match="invalid snapshot command"):
            session.send("finish\nunsafe")
        session.send("finish")
        assert session.receive("closed") == {"phase": "closed"}
        assert process.wait(timeout=5) == 0


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"", "exited"),
        (b'{"phase":"ready"}', "exited"),
        (b"not json\n", "JSON"),
        (b"[]\n", "phase"),
        (b'{"phase":"wrong"}\n', "phase"),
        (b"x" * (4 * 1024 * 1024 + 1), "budget"),
    ],
    ids=["eof", "partial", "malformed", "array", "wrong_phase", "oversized"],
)
def test_protocol_rejects_partial_malformed_wrong_phase_and_oversized_messages(
    raw: bytes, reason: str
) -> None:
    # Supply bytes on stdin to avoid command-line length limits for the large case.
    with child(
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.stdout.flush()"
    ) as p:
        assert p.stdin is not None
        # The large case needs concurrent writing to avoid filling both pipe buffers.
        import threading

        def write() -> None:
            try:
                p.stdin.write(raw)  # type: ignore[union-attr]
                p.stdin.close()  # type: ignore[union-attr]
            except BrokenPipeError:
                pass

        writer = threading.Thread(target=write)
        writer.start()
        try:
            with pytest.raises(DrillError, match=reason):
                SnapshotSession(p).receive("ready")
        finally:
            stop_child(p)
            writer.join(timeout=5)
            assert not writer.is_alive()


def test_unresponsive_worker_has_a_deadline_and_is_reaped() -> None:
    with child("import sys; sys.stdin.buffer.read()") as p:
        with pytest.raises(DrillError, match="timed out"):
            SnapshotSession(p).receive("ready", timeout=0)
    assert p.poll() is not None and p.stdin is not None and p.stdin.closed


def test_snapshot_container_cleanup_is_exact_and_precedes_cluster_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def capture(self: Drill, command: list[str], **kwargs: object) -> bytes:
        calls.append(command)
        return b"abcdef012345\n" if command[:3] == ["docker", "container", "ls"] else b""

    monkeypatch.setattr(Drill, "call", capture)
    drill = Drill(tmp_path, "0123456789ab")
    drill.snapshot_started = True
    assert drill.cleanup()
    assert calls[0][-1] == "name=^/aca-restore-0123456789ab-snapshot$"
    assert calls[1] == ["docker", "container", "rm", "--force", drill.snapshot_container]
    assert [call[3] for call in calls[2:]] == [
        "aca-restore-0123456789ab-source",
        "aca-restore-0123456789ab-target",
    ]


def test_bad_container_lookup_fails_closed_but_still_cleans_both_clusters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def capture(self: Drill, command: list[str], **kwargs: object) -> bytes:
        calls.append(command)
        return b"untrusted\nanother-container" if len(calls) == 1 else b""

    monkeypatch.setattr(Drill, "call", capture)
    drill = Drill(tmp_path, "0123456789ab")
    drill.snapshot_started = True
    assert not drill.cleanup()
    assert len(calls) == 3 and all("rm" not in call for call in calls)


def test_helper_requires_its_exported_snapshot_to_reach_pg_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed/unsafe token must stop before a second (dump) process starts."""
    starts: list[list[str]] = []

    def fixture(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        starts.append(command)
        # Return a real, short-lived pipe producer so EOF/cleanup stay exercised.
        return original(
            [
                sys.executable,
                "-c",
                "print("
                + repr(json.dumps({"phase": "ready", "snapshot": "bad"}))
                + ", flush=True)",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    original = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", fixture)
    with pytest.raises(DrillError, match="snapshot identifier"):
        Drill(tmp_path, "0123456789ab").capture(tmp_path / "aca.dump")
    assert len(starts) == 1 and "pg_dump" not in starts[0]
    assert (tmp_path / "snapshot-session.log").stat().st_mode & 0o777 == 0o600

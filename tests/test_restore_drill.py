"""Guards for the destructive test-only restore orchestrator; no Docker here."""

import ast
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.restore_drill import Drill, DrillError, compose, private_write, verify_dump


def test_host_helper_keeps_python_39_syntax() -> None:
    import scripts.restore_drill

    assert scripts.restore_drill.__file__ is not None
    ast.parse(Path(scripts.restore_drill.__file__).read_text(), feature_version=(3, 9))


@pytest.mark.parametrize(
    "project",
    [
        "aca-paper",
        "aca-foundation-tests",
        "aca-restore-abc-source",
        "aca-restore-" + "a" * 12,
        "aca-restore-" + "A" * 12 + "-source",
        "aca-restore-" + "a" * 12 + "-source;true",
    ],
)
def test_only_generated_disposable_projects_are_accepted(project: str) -> None:
    with pytest.raises(DrillError):
        compose(project)


def test_generated_project_uses_no_test_port_override() -> None:
    command = compose("aca-restore-0123456789ab-target")
    assert command[3] == "aca-restore-0123456789ab-target"
    assert command[-4:] == ["-f", "compose.yaml", "-f", "compose.drill.yaml"]
    assert "compose.test.yaml" not in command


def test_private_evidence_cannot_overwrite_or_follow_a_symlink(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    private_write(path, b"evidence")
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        private_write(path, b"changed")
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(FileExistsError):
        private_write(link, b"changed")
    assert path.read_bytes() == b"evidence"


@pytest.mark.parametrize("change", ["checksum", "header", "truncated", "symlink", "invalid_hash"])
def test_bad_archive_fails_before_any_restore_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    archive = tmp_path / "aca.dump"
    original = b"PGDMP-test-fixture"
    archive.write_bytes(original)
    checksum = hashlib.sha256(original).hexdigest()
    if change == "checksum":
        archive.write_bytes(original + b"changed")
    elif change == "header":
        archive.write_bytes(b"plain SQL")
    elif change == "truncated":
        archive.write_bytes(original[:3])
    elif change == "symlink":
        link = tmp_path / "link"
        link.symlink_to(archive)
        archive = link
    else:
        checksum = "unverified"

    def forbidden(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("unverified archive reached a child process")

    monkeypatch.setattr(Drill, "call", forbidden)
    with pytest.raises(DrillError):
        Drill(tmp_path, "0123456789ab").restore(archive, checksum)


def test_verified_archive_uses_atomic_restore_and_preserves_acl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "aca.dump"
    archive.write_bytes(b"PGDMP-test-fixture")
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    verify_dump(archive, checksum)
    calls: list[list[str]] = []

    def capture(self: Drill, command: list[str], **kwargs: object) -> bytes:
        assert kwargs["input_file"] == archive
        calls.append(command)
        return b""

    monkeypatch.setattr(Drill, "call", capture)
    Drill(tmp_path, "0123456789ab").restore(archive, checksum)
    assert len(calls) == 1
    assert "--single-transaction" in calls[0] and "--exit-on-error" in calls[0]
    assert "--no-owner" not in calls[0] and "--no-acl" not in calls[0]
    assert "--clean" not in calls[0] and "--create" not in calls[0]
    assert calls[0][3].endswith("-target")


def test_cleanup_attempts_both_owned_projects_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def capture(self: Drill, command: list[str], **kwargs: object) -> bytes:
        calls.append(command)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("test", 180)
        return b""

    monkeypatch.setattr(Drill, "call", capture)
    assert not Drill(tmp_path, "0123456789ab").cleanup()
    assert len(calls) == 2
    assert all(c[-3:] == ["down", "--volumes", "--remove-orphans"] for c in calls)
    assert all(c[3].startswith("aca-restore-0123456789ab-") for c in calls)


def test_worker_rejects_non_paper_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def wrong(self: Drill, *args: object, **kwargs: object) -> bytes:
        return json.dumps({"live_execution_available": True}).encode()

    monkeypatch.setattr(Drill, "call", wrong)
    drill = Drill(tmp_path, "0123456789ab")
    with pytest.raises(DrillError):
        drill.worker(drill.target, "inspect")


def test_call_never_exposes_child_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(["fixture"], 1, stdout=b"", stderr=b"private detail")

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(DrillError) as error:
        Drill(tmp_path, "0123456789ab").call(["fixture"])
    assert "private detail" not in str(error.value)
    assert (tmp_path / "command-01.log").stat().st_mode & 0o777 == 0o600


def test_fixture_target_guard_never_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    from restore_fixture import settings_for

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("invalid fixture target connected")

    monkeypatch.setattr("psycopg.connect", forbidden)
    for name in ("aca", "aca_test_other", "aca_test_restore_ABCDEF012345"):
        with pytest.raises(ValueError):
            settings_for(name)
    assert settings_for("aca_test_restore_0123456789ab").database.host == "127.0.0.1"


def test_known_array_reparse_keeps_values_column_and_validation_semantics() -> None:
    from restore_fixture import constraint_text

    source = (
        "CHECK (kind::text = ANY (ARRAY['one'::character varying, "
        "'it''s'::character varying]::text[]))"
    )
    restored = (
        "CHECK (kind::text = ANY (ARRAY['one'::character varying::text, "
        "'it''s'::character varying::text]))"
    )
    assert constraint_text(source) == constraint_text(restored)
    assert constraint_text(source) != constraint_text(source.replace("one", "two"))
    assert constraint_text(source) != constraint_text(source.replace("kind", "other"))


@pytest.mark.parametrize("change", ["integer", "character varying(3)", "text", "citext"])
def test_other_constraint_casts_are_never_normalised(change: str) -> None:
    from restore_fixture import constraint_text

    source = "CHECK (kind::text = ANY (ARRAY['one'::character varying]::text[]))"
    changed = source.replace("character varying", change)
    assert constraint_text(changed) == changed
    assert constraint_text(changed) != constraint_text(source)

"""Repeat a bounded logical-restore drill on two new disposable Docker clusters.

Standard library only; host Python 3.9+. Never accepts an existing project, database
or dump. Production backup/restore and automatic failover are outside this helper.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    from scripts.bootstrap import ROOT, prepare_secrets, require_docker
except ModuleNotFoundError:
    from bootstrap import ROOT, prepare_secrets, require_docker  # type: ignore[import-not-found,no-redef]


class DrillError(RuntimeError):
    """Fixed, safe error: child diagnostics stay in the private drill directory."""


def compose(project: str) -> list[str]:
    if not re.fullmatch(r"aca-restore-[a-f0-9]{12}-(source|target)", project):
        raise DrillError("refusing a project outside the disposable restore namespace")
    return ["docker", "compose", "-p", project, "-f", "compose.yaml", "-f", "compose.drill.yaml"]


def private_write(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def verify_dump(path: Path, expected: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or path.is_symlink():
        raise DrillError("invalid dump evidence")
    with path.open("rb") as handle:
        if handle.read(5) != b"PGDMP":
            raise DrillError("dump is not a PostgreSQL custom archive")
        handle.seek(0)
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise DrillError("dump checksum mismatch; no restore authorised")


def transactional_view(value: dict[str, Any]) -> dict[str, Any]:
    """Sequence counters alone are outside MVCC; never omit rows or their maxima."""
    if "sequences" not in value or "sequence_row_maxima" not in value:
        raise DrillError("missing sequence evidence")
    return {key: item for key, item in value.items() if key != "sequences"}


def verify_sequences(
    before: dict[str, Any], after: dict[str, Any], restored: dict[str, Any]
) -> None:
    """The fixture admits only its unchanged, positive, cache-one owned sequences."""
    lower, upper, actual = (value["sequences"] for value in (before, after, restored))
    floors = before["sequence_row_maxima"]
    if (
        not lower
        or set(lower) != set(upper)
        or set(lower) != set(actual)
        or set(lower) != set(floors)
    ):
        raise DrillError("sequence set differs")
    if restored["sequence_row_maxima"] != floors:
        raise DrillError("restored sequence row coverage differs")
    for name, low in lower.items():
        high, got = upper[name], actual[name]
        for pair in (low, high, got):
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or type(pair[0]) is not int
                or pair[0] < 1
                or type(pair[1]) is not bool
            ):
                raise DrillError("invalid sequence counter evidence")
        if not tuple(low) <= tuple(got) <= tuple(high):
            raise DrillError("restored sequence outside captured bounds")
        floor = floors[name]
        if floor is not None and (type(floor) is not int or not got[1] or got[0] < floor):
            raise DrillError("restored sequence can reuse an existing row identifier")


class SnapshotSession:
    """Bounded line protocol; process output is private and never a shell command."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.pending = b""

    def send(self, command: str) -> None:
        if command not in {"exercise", "finish"} or self.process.stdin is None:
            raise DrillError("invalid snapshot command")
        self.process.stdin.write((command + "\n").encode())
        self.process.stdin.flush()

    def receive(self, phase: str, timeout: float = 90) -> dict[str, Any]:
        if self.process.stdout is None:
            raise DrillError("missing snapshot worker output")
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b"\n" not in self.pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise DrillError("snapshot worker response timed out")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise DrillError("snapshot worker exited before a complete response")
                self.pending += chunk
                if len(self.pending) > 4 * 1024 * 1024:
                    raise DrillError("snapshot worker response budget exceeded")
        raw, self.pending = self.pending.split(b"\n", 1)
        try:
            value = json.loads(raw)
        except ValueError:
            raise DrillError("invalid snapshot worker JSON") from None
        if not isinstance(value, dict) or value.get("phase") != phase:
            raise DrillError("unexpected snapshot worker phase")
        return value


def stop_child(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    for pipe in (process.stdin, process.stdout):
        if pipe is not None:
            pipe.close()


class Drill:
    def __init__(self, directory: Path, identifier: str) -> None:
        self.directory = directory
        self.source = compose(f"aca-restore-{identifier}-source")
        self.target = compose(f"aca-restore-{identifier}-target")
        self.database = f"aca_test_restore_{identifier}"
        self.snapshot_container = f"aca-restore-{identifier}-snapshot"
        self.snapshot_started = False
        self.counter = 0

    def capture(self, archive: Path) -> dict[str, Any]:
        processes: list[subprocess.Popen[bytes]] = []
        diagnostics = self.directory / "snapshot-session.log"
        with diagnostics.open("xb") as log, archive.open("xb") as output:
            diagnostics.chmod(0o600)
            archive.chmod(0o600)
            try:
                worker = subprocess.Popen(
                    [
                        *self.source,
                        "run",
                        "--rm",
                        "--no-deps",
                        "-T",
                        "--name",
                        self.snapshot_container,
                        "restore-worker",
                        "python",
                        "tests/concurrent_restore_fixture.py",
                        self.database,
                    ],
                    cwd=ROOT,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=log,
                )
                processes.append(worker)
                self.snapshot_started = True
                session = SnapshotSession(worker)
                ready = session.receive("ready")
                token = ready.get("snapshot")
                if not isinstance(token, str) or not re.fullmatch(
                    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9]+", token
                ):
                    raise DrillError("invalid exported snapshot identifier")
                private_write(self.directory / "snapshot-ready.json", json.dumps(ready).encode())
                dump = subprocess.Popen(
                    [
                        *self.source,
                        "exec",
                        "-T",
                        "db",
                        "pg_dump",
                        "--username=aca_admin",
                        "--dbname=" + self.database,
                        "--format=custom",
                        "--snapshot=" + token,
                    ],
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=log,
                )
                processes.append(dump)
                session.send("exercise")
                exercised = session.receive("exercised")
                private_write(
                    self.directory / "snapshot-exercised.json", json.dumps(exercised).encode()
                )
                before = ready.get("before")
                same = exercised.get("same_snapshot_after_writes")
                after = exercised.get("source_after")
                if (
                    not isinstance(before, dict)
                    or not isinstance(same, dict)
                    or not isinstance(after, dict)
                    or any(
                        value.get("live_execution_available") is not False
                        for value in (before, same, after)
                    )
                ):
                    raise DrillError("invalid snapshot view evidence")
                if (
                    exercised.get("dump_wait_observed") is not True
                    or type(exercised.get("commits_while_dump_waited")) is not int
                    or exercised["commits_while_dump_waited"] < 1
                    or exercised.get("venue_commands") != 0
                    or transactional_view(before) != transactional_view(same)
                    or before["tables"] == after["tables"]
                ):
                    raise DrillError("concurrent snapshot evidence did not establish isolation")
                if dump.wait(timeout=90) != 0:
                    raise DrillError("snapshot dump failed; inspect private diagnostics")
                session.send("finish")
                closed = session.receive("closed", timeout=10)
                if closed.get("live_execution_available") is not False or worker.wait(timeout=10):
                    raise DrillError("snapshot worker did not close successfully")
                return {"before": before, **exercised}
            finally:
                for process in reversed(processes):
                    stop_child(process)

    def call(
        self,
        command: list[str],
        *,
        input_file: Path | None = None,
        output_file: Path | None = None,
        expect_failure: bool = False,
        timeout: float = 180,
    ) -> bytes:
        self.counter += 1
        log = self.directory / f"command-{self.counter:02d}.log"
        with log.open("xb") as diagnostics:
            log.chmod(0o600)
            source = input_file.open("rb") if input_file else None
            destination = output_file.open("xb") if output_file else None
            try:
                if output_file:
                    output_file.chmod(0o600)
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    stdin=source or subprocess.DEVNULL,
                    stdout=destination or subprocess.PIPE,
                    stderr=diagnostics,
                    timeout=timeout,
                    check=False,
                )
            finally:
                if source:
                    source.close()
                if destination:
                    destination.close()
        if (result.returncode != 0) != expect_failure:
            raise DrillError("unexpected child result; inspect private command log")
        return result.stdout or b""

    def worker(
        self, cluster: list[str], action: str, declaration: Path | None = None
    ) -> dict[str, Any]:
        raw = self.call(
            [
                *cluster,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "restore-worker",
                "python",
                "tests/restore_fixture.py",
                action,
                self.database,
            ],
            input_file=declaration,
        )
        try:
            result = json.loads(raw)
        except ValueError:
            raise DrillError("invalid fixture output") from None
        if not isinstance(result, dict) or result.get("live_execution_available") is not False:
            raise DrillError("invalid fixture capability evidence")
        private_write(
            self.directory / f"worker-{self.counter:02d}-{action}.json",
            (json.dumps(result, indent=2) + "\n").encode(),
        )
        return result

    def restore(self, archive: Path, checksum: str, *, expect_failure: bool = False) -> None:
        verify_dump(archive, checksum)
        self.call(
            [
                *self.target,
                "exec",
                "-T",
                "db",
                "pg_restore",
                "--username=aca_admin",
                "--dbname=" + self.database,
                "--single-transaction",
                "--exit-on-error",
            ],
            input_file=archive,
            expect_failure=expect_failure,
        )
        if expect_failure:
            diagnostic = (self.directory / f"command-{self.counter:02d}.log").read_bytes()
            if b'role "aca_restore_probe" does not exist' not in diagnostic:
                raise DrillError("negative restore did not reach the expected ACL failure")

    def run(self) -> dict[str, Any]:
        self.call([*self.source, "build", "db", "restore-worker"])
        for cluster in (self.source, self.target):
            self.call([*cluster, "up", "-d", "--wait", "--wait-timeout", "120", "db"])
        print("Seeding isolated paper fixtures; retained services are untouched.", flush=True)
        self.worker(self.source, "seed")
        empty = self.worker(self.target, "prepare")
        archive = self.directory / "aca.dump"
        print("Capturing one exported snapshot while controlled paper writes commit.", flush=True)
        capture = self.capture(archive)
        before, source_after = capture["before"], capture["source_after"]
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        # A role used by an ACL deliberately exists only on the source cluster.
        # Restore must roll back all schema/data when that late ACL step fails.
        print("Checking failed-restore rollback, then restoring the complete archive.", flush=True)
        self.restore(archive, checksum, expect_failure=True)
        if self.worker(self.target, "inspect_empty") != empty:
            raise DrillError("failed restore changed the empty target")
        self.worker(self.target, "allow_probe")
        started = time.monotonic()
        self.restore(archive, checksum)
        restored = self.worker(self.target, "inspect")
        restore_and_verify_seconds = time.monotonic() - started
        if transactional_view(restored) != transactional_view(before):
            raise DrillError("restored data, schema, roles or financial evidence differs")
        verify_sequences(before, source_after, restored)
        recovery = self.worker(self.target, "recover")
        repeated = self.worker(self.target, "recover")
        if repeated != recovery:
            raise DrillError("repeated recovery changed economic evidence")
        self.worker(self.target, "permissions")
        if self.worker(self.source, "inspect") != source_after:
            raise DrillError("source changed after controlled snapshot capture")
        declaration = self.directory / "declared-source-after.json"
        private_write(declaration, json.dumps(source_after).encode())
        gap = self.worker(self.target, "gap_check", declaration)
        if (
            gap.get("gap_identified") is not True
            or gap.get("continued_observation_permitted") is not False
        ):
            raise DrillError("declared suffix did not block continued observation")
        return {
            "schema_version": 2,
            "kind": "disposable_local_logical_restore",
            "live_execution_available": False,
            "promotion_evidence": False,
            "dump": {"bytes": archive.stat().st_size, "sha256": checksum, "format": "custom"},
            "source": before,
            "source_after_capture": source_after,
            "restored_transactional_view_equal": True,
            "sequence_bounds_verified": True,
            "restored_sequences": restored["sequences"],
            "exported_snapshot_stable_after_writes": True,
            "post_snapshot_changes_excluded": True,
            "commits_while_dump_waited": capture["commits_while_dump_waited"],
            "source_unchanged_after_capture": True,
            "failed_restore_rolled_back": True,
            "runtime_permissions_verified": True,
            "repeated_recovery_economics_equal": True,
            "recovery": recovery,
            "declared_gap": gap,
            "restore_and_verify_seconds": round(restore_and_verify_seconds, 3),
            "production_rpo_proven": False,
            "production_rto_proven": False,
            "off_host_backup": False,
            "wal_recovery": False,
        }

    def cleanup(self) -> bool:
        success = True
        if self.snapshot_started:
            try:
                present = self.call(
                    [
                        "docker",
                        "container",
                        "ls",
                        "--all",
                        "--quiet",
                        "--filter",
                        "name=^/" + self.snapshot_container + "$",
                    ]
                ).strip()
                if present:
                    if not re.fullmatch(rb"[a-f0-9]{12,64}", present):
                        raise DrillError("unexpected snapshot container identity")
                    self.call(["docker", "container", "rm", "--force", self.snapshot_container])
            except (OSError, subprocess.SubprocessError, DrillError) as error:
                success = False
                print("Snapshot cleanup failed: " + type(error).__name__, file=sys.stderr)
        for cluster in (self.source, self.target):
            try:
                # Generated project names only: never the retained aca-paper volume.
                self.call([*cluster, "down", "--volumes", "--remove-orphans"])
            except (OSError, subprocess.SubprocessError, DrillError) as error:
                success = False
                print("Disposable cleanup failed: " + type(error).__name__, file=sys.stderr)
        return success


def main() -> int:
    if sys.argv[1:]:
        print(
            "Usage: python3 scripts/restore_drill.py (no target or archive arguments)",
            file=sys.stderr,
        )
        return 2
    require_docker()
    prepare_secrets()
    identifier = uuid4().hex[:12]
    started_at = datetime.now(timezone.utc).isoformat()  # noqa: UP017 - host Python 3.9
    directory = ROOT / ".secrets" / "backups" / ("restore-drill-" + identifier)
    directory.parent.mkdir(mode=0o700, exist_ok=True)
    if directory.parent.is_symlink() or directory.parent.stat().st_mode & 0o077:
        raise DrillError("backup directory must be private and must not be a symlink")
    directory.mkdir(mode=0o700)
    drill = Drill(directory, identifier)
    started = time.monotonic()
    report: dict[str, Any] = {"status": "failed", "started_at": started_at}
    result = 1
    try:
        report.update(drill.run())
        report["status"] = "passed"
        result = 0
    except (OSError, ValueError, subprocess.SubprocessError, DrillError) as exc:
        # Never publish raw Docker/SQL/configuration diagnostics.
        report["error_type"] = type(exc).__name__
        if isinstance(exc, DrillError):
            report["failure"] = str(exc)
        print("Restore drill failed; inspect its private directory.", file=sys.stderr)
    finally:
        report["cleanup_complete"] = drill.cleanup()
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if not report["cleanup_complete"]:
            report["status"] = "failed"
            result = 1
            print("Disposable cleanup incomplete; inspect the private manifest.", file=sys.stderr)
        report["disposable_projects"] = [drill.source[3], drill.target[3]]
        private_write(directory / "manifest.json", (json.dumps(report, indent=2) + "\n").encode())
    print("Restore drill " + str(report["status"]) + ": " + str(directory), flush=True)
    if result == 0:
        print(
            json.dumps(
                {
                    "status": "passed",
                    "application_tables_compared": len(report["source"]["tables"]),
                    "synthetic_accounts": len(report["recovery"]["economics"]),
                    "recovery_venue_commands": report["recovery"]["venue_commands"],
                    "failed_restore_rolled_back": True,
                    "cleanup_complete": True,
                    "elapsed_seconds": report["elapsed_seconds"],
                    "live_execution_available": False,
                },
                sort_keys=True,
            ),
            flush=True,
        )
    return result


if __name__ == "__main__":
    raise SystemExit(main())

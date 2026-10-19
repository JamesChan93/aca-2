"""Repeat a bounded logical-restore drill on two new disposable Docker clusters.

Standard library only; host Python 3.9+. Never accepts an existing project, database
or dump. Production backup/restore and automatic failover are outside this helper.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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


class Drill:
    def __init__(self, directory: Path, identifier: str) -> None:
        self.directory = directory
        self.source = compose(f"aca-restore-{identifier}-source")
        self.target = compose(f"aca-restore-{identifier}-target")
        self.database = f"aca_test_restore_{identifier}"
        self.counter = 0

    def call(
        self,
        command: list[str],
        *,
        input_file: Path | None = None,
        output_file: Path | None = None,
        expect_failure: bool = False,
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
                    timeout=180,
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

    def worker(self, cluster: list[str], action: str) -> dict[str, Any]:
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
            ]
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
        before = self.worker(self.source, "seed")
        empty = self.worker(self.target, "prepare")
        archive = self.directory / "aca.dump"
        self.call(
            [
                *self.source,
                "exec",
                "-T",
                "db",
                "pg_dump",
                "--username=aca_admin",
                "--dbname=" + self.database,
                "--format=custom",
            ],
            output_file=archive,
        )
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
        if restored != before:
            raise DrillError("restored data, schema, roles or financial evidence differs")
        recovery = self.worker(self.target, "recover")
        repeated = self.worker(self.target, "recover")
        if repeated != recovery:
            raise DrillError("repeated recovery changed economic evidence")
        self.worker(self.target, "permissions")
        if self.worker(self.source, "inspect") != before:
            raise DrillError("source changed during restore drill")
        return {
            "schema_version": 1,
            "kind": "disposable_local_logical_restore",
            "live_execution_available": False,
            "promotion_evidence": False,
            "dump": {"bytes": archive.stat().st_size, "sha256": checksum, "format": "custom"},
            "source": before,
            "restored_equal": True,
            "source_unchanged": True,
            "failed_restore_rolled_back": True,
            "runtime_permissions_verified": True,
            "repeated_recovery_economics_equal": True,
            "recovery": recovery,
            "restore_and_verify_seconds": round(restore_and_verify_seconds, 3),
            "production_rpo_proven": False,
            "production_rto_proven": False,
            "off_host_backup": False,
            "wal_recovery": False,
        }

    def cleanup(self) -> bool:
        success = True
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

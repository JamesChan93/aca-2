"""Bounded, real PostgreSQL/WAL ENOSPC, on a new 384 MiB RAM volume only.

No target arguments. A holder keeps the RAM volume mounted across PostgreSQL
restart; no retained filesystem, existing project or backup can be selected.
Host Python 3.9+, standard library only. Diagnostics remain private.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    from scripts.bootstrap import ROOT, prepare_secrets, require_docker
    from scripts.restore_drill import (
        Drill,
        DrillError,
        SnapshotSession,
        private_write,
        stop_child,
    )
except ModuleNotFoundError:
    from bootstrap import ROOT, prepare_secrets, require_docker  # type: ignore[import-not-found,no-redef]
    from restore_drill import (  # type: ignore[import-not-found,no-redef]
        Drill,
        DrillError,
        SnapshotSession,
        private_write,
        stop_child,
    )


class StorageDrill(Drill):
    def __init__(self, directory: Path, identifier: str) -> None:
        super().__init__(directory, identifier)
        self.source += ["-f", "compose.storage-fault.yaml"]
        self.worker_name = "aca-storage-" + identifier + "-watch"

    def run(self) -> dict[str, Any]:
        self.call([*self.source, "build", "db", "restore-worker"])
        # Check the resolved configuration before any volume exists.
        config = json.loads(self.call([*self.source, "config", "--format", "json"]))
        volume = config["volumes"]["storage_fault_data"]
        if volume["driver_opts"] != {
            "type": "tmpfs",
            "device": "tmpfs",
            "o": "size=384m,uid=70,gid=70,mode=0700",
        }:
            raise DrillError("refusing nonbounded storage fault volume")
        mount = next(
            v for v in config["services"]["db"]["volumes"] if v["target"] == "/var/lib/postgresql"
        )
        if mount["source"] != "storage_fault_data":
            raise DrillError("refusing storage fault on another database volume")
        self.call([*self.source, "up", "-d", "--wait", "db", "storage-holder"])
        log_path = self.directory / "watch.log"
        process = None
        with log_path.open("xb") as log:
            log_path.chmod(0o600)
            try:
                process = subprocess.Popen(
                    [
                        *self.source,
                        "run",
                        "--rm",
                        "--no-deps",
                        "-T",
                        "--name",
                        self.worker_name,
                        "restore-worker",
                        "python",
                        "tests/storage_fault_fixture.py",
                        "watch",
                        self.database,
                    ],
                    cwd=ROOT,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=log,
                )
                session = SnapshotSession(process)
                ready = session.receive("ready")
                # This dd can allocate at most 384 MiB, entirely in the verified tmpfs.
                self.call(
                    [
                        *self.source,
                        "exec",
                        "-T",
                        "db",
                        "dd",
                        "if=/dev/zero",
                        "of=/var/lib/postgresql/c3-filler",
                        "bs=1M",
                        "count=384",
                    ],
                    expect_failure=True,
                )
                filler_log = (self.directory / f"command-{self.counter:02d}.log").read_bytes()
                if b"No space left on device" not in filler_log:
                    raise DrillError("bounded volume did not reach real ENOSPC")
                # Force allocation at the actual WAL boundary. No WAL/history file
                # is removed; only this drill's filler is subsequently deleted.
                wal_failed = False
                fault_started = time.monotonic()
                for _ in range(8):
                    try:
                        self.call(
                            [
                                *self.source,
                                "exec",
                                "-T",
                                "db",
                                "psql",
                                "-U",
                                "aca_admin",
                                "-d",
                                self.database,
                                "-v",
                                "ON_ERROR_STOP=1",
                                "-c",
                                "SELECT pg_logical_emit_message(true,'c3','bounded-wal-probe'); "
                                "SELECT pg_switch_wal()",
                            ]
                        )
                    except DrillError:
                        wal_failed = True
                        break
                logs = self.call([*self.source, "logs", "--no-color", "db"])
                private_write(self.directory / "postgres-enospc.log", logs)
                if (
                    not wal_failed
                    or b"No space left on device" not in logs
                    or b"pg_wal" not in logs
                ):
                    raise DrillError("no PostgreSQL WAL ENOSPC evidence")
                # Keep PostgreSQL offline until the worker has proved dispatch and
                # the integrated supervisor fail closed. Holder preserves the RAM FS.
                self.call([*self.source, "stop", "db"])
                if process.stdin is None:
                    raise DrillError("storage worker pipe unavailable")
                process.stdin.write(b"faulted\n")
                process.stdin.flush()
                blocked = session.receive("blocked", timeout=30)
                blocked["fault_to_unhealthy_upper_bound_seconds"] = time.monotonic() - fault_started
                if blocked["fault_to_unhealthy_upper_bound_seconds"] >= 10:
                    raise DrillError("storage fault detection exceeded ten seconds")
                if process.wait(timeout=10) or blocked.get("venue_commands") != 0:
                    raise DrillError("storage failure did not block commands")
                self.call(
                    [
                        *self.source,
                        "exec",
                        "-T",
                        "storage-holder",
                        "python",
                        "-c",
                        "from pathlib import Path; Path('/held/c3-filler').unlink()",
                    ]
                )
                self.call([*self.source, "up", "-d", "--wait", "db"])
                raw = self.call(
                    [
                        *self.source,
                        "run",
                        "--rm",
                        "--no-deps",
                        "-T",
                        "restore-worker",
                        "python",
                        "tests/storage_fault_fixture.py",
                        "recover",
                        self.database,
                    ]
                )
                recovered = json.loads(raw)
                if recovered.get("before_state_hash") != ready.get("state_hash"):
                    raise DrillError("committed paper state changed across ENOSPC/restart")
                if recovered.get("disarmed") is not True or recovered.get("venue_commands") != 0:
                    raise DrillError("storage recovery was not disarmed")
                notified = json.loads(
                    self.call(
                        [
                            *self.source,
                            "run",
                            "--rm",
                            "--no-deps",
                            "-T",
                            "restore-worker",
                            "python",
                            "tests/storage_fault_fixture.py",
                            "notify",
                            self.database,
                        ]
                    )
                )
                notified["fault_to_attempt_upper_bound_seconds"] = time.monotonic() - fault_started
                if (
                    notified.get("controlled_http_attempts") != 1
                    or notified["fault_to_attempt_upper_bound_seconds"] >= 60
                ):
                    raise DrillError("storage fault notification attempt exceeded sixty seconds")
                return {
                    "kind": "bounded_tmpfs_postgresql_wal_enospc",
                    "status": "passed",
                    "tmpfs_limit_bytes": 384 * 1024 * 1024,
                    "wal_enospc_observed": True,
                    "ready": ready,
                    "blocked": blocked,
                    "recovered": recovered,
                    "notified_after_database_recovery": notified,
                    "retained_volume_used": False,
                    "live_execution_available": False,
                    "promotion_evidence": False,
                }
            finally:
                if process is not None:
                    stop_child(process)
                # Exact generated name only. Removing a nonexistent --rm worker is harmless.
                subprocess.run(
                    ["docker", "rm", "-f", self.worker_name],
                    cwd=ROOT,
                    stdout=log,
                    stderr=log,
                    timeout=15,
                    check=False,
                )

    def cleanup(self) -> bool:
        try:
            self.call([*self.source, "down", "--volumes", "--remove-orphans"])
        except (OSError, subprocess.SubprocessError, DrillError) as _error:
            return False
        return True


def main() -> int:
    if sys.argv[1:]:
        print("Usage: python3 scripts/storage_fault_drill.py (no arguments)", file=sys.stderr)
        return 2
    require_docker()
    prepare_secrets()
    identifier = uuid4().hex[:12]
    directory = ROOT / ".secrets" / "backups" / ("storage-drill-" + identifier)
    directory.parent.mkdir(mode=0o700, exist_ok=True)
    if directory.parent.is_symlink() or directory.parent.stat().st_mode & 0o077:
        raise DrillError("storage drill evidence directory must be private")
    directory.mkdir(mode=0o700)
    drill = StorageDrill(directory, identifier)
    began = time.monotonic()
    report: dict[str, Any] = {
        "status": "failed",
        "started_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017 - host Python 3.9
    }
    try:
        report.update(drill.run())
    except (OSError, ValueError, subprocess.SubprocessError, DrillError) as error:
        report["error_type"] = type(error).__name__
        if isinstance(error, DrillError):
            report["failure"] = str(error)
    finally:
        report["cleanup_complete"] = drill.cleanup()
        report["elapsed_seconds"] = round(time.monotonic() - began, 3)
        if not report["cleanup_complete"]:
            report["status"] = "failed"
        private_write(directory / "manifest.json", (json.dumps(report, indent=2) + "\n").encode())
    print("Storage drill " + str(report["status"]) + ": " + str(directory))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

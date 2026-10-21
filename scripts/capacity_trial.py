"""C3 bounded, repeatable capacity experiment on a new disposable database.

No target arguments. Writes a measurement, including failures; a completed trial
does not grant the 56-day capacity or operational acceptance gate. Host Python 3.9+.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any
from uuid import uuid4

try:
    from scripts.bootstrap import ROOT, prepare_secrets, require_docker
    from scripts.restore_drill import (
        Drill,
        DrillError,
        private_write,
    )
except ModuleNotFoundError:
    from bootstrap import ROOT, prepare_secrets, require_docker  # type: ignore[import-not-found,no-redef]
    from restore_drill import (  # type: ignore[import-not-found,no-redef]
        Drill,
        DrillError,
        private_write,
    )


def main() -> int:
    if sys.argv[1:]:
        print("Usage: python3 scripts/capacity_trial.py (no arguments)", file=sys.stderr)
        return 2
    require_docker()
    prepare_secrets()
    identifier = uuid4().hex[:12]
    directory = ROOT / ".secrets" / "backups" / ("capacity-trial-" + identifier)
    directory.parent.mkdir(mode=0o700, exist_ok=True)
    if directory.parent.is_symlink() or directory.parent.stat().st_mode & 0o077:
        raise DrillError("capacity evidence directory must be private")
    directory.mkdir(mode=0o700)
    drill = Drill(directory, identifier)
    drill.source += ["-f", "compose.capacity.yaml"]
    report: dict[str, Any] = {"status": "failed"}
    try:
        drill.call([*drill.source, "build", "db", "restore-worker"])
        drill.call([*drill.source, "up", "-d", "--wait", "db"])
        raw = drill.call(
            [
                *drill.source,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "restore-worker",
                "python",
                "tests/capacity_fixture.py",
                drill.database,
            ],
            timeout=600,
        )
        report.update(json.loads(raw))
        report["status"] = "measured"
    except (OSError, ValueError, subprocess.SubprocessError, DrillError) as error:
        report["error_type"] = type(error).__name__
        if isinstance(error, DrillError):
            report["failure"] = str(error)
    finally:
        report["cleanup_complete"] = drill.cleanup()
        private_write(directory / "manifest.json", (json.dumps(report, indent=2) + "\n").encode())
    print("Capacity trial " + str(report["status"]) + ": " + str(directory))
    if report["status"] == "measured":
        print(
            json.dumps(
                {
                    key: report.get(key)
                    for key in (
                        "application_version",
                        "package_source_sha256",
                        "certifi_version",
                        "record_limits",
                        "operations",
                        "one_second_loop",
                        "database_bytes",
                        "cpu_seconds",
                        "max_rss_kib",
                        "memory.max",
                        "memory.peak",
                        "cpu.max",
                        "cleanup_complete",
                        "56_day_headroom_demonstrated",
                        "measured_boundary_failures",
                        "replay_preservation",
                    )
                },
                sort_keys=True,
            )
        )
    return 0 if report["status"] == "measured" and report["cleanup_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

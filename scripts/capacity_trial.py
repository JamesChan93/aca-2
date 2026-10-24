"""C3 bounded, repeatable capacity experiment on a new disposable database.

No target arguments. --preservation selects archive measurements;
--financial-prefix selects the financial audit subset and packaged acceptance CLI.
Writes a measurement, including failures; a completed trial
does not grant the 56-day capacity or operational acceptance gate. Host Python 3.9+.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
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


def last_progress(path: Path) -> dict[str, Any] | None:
    """Expose only the fixture's allowlisted checkpoint, never private diagnostics."""
    phases = {
        "seeded",
        "compacted",
        "suffix_seeded",
        "reports_compared",
        "measuring_operations",
        "operations_measured",
    }
    latest = None
    if not path.exists():
        return None
    with path.open(errors="replace") as stream:
        for line in stream:
            if not line.startswith("ACA_CAPACITY_PROGRESS ") or len(line) > 1024:
                continue
            try:
                value = json.loads(line.removeprefix("ACA_CAPACITY_PROGRESS "))
            except ValueError:
                continue
            if not isinstance(value, dict):
                continue
            elapsed = value.get("elapsed_seconds")
            if (
                isinstance(value.get("phase"), str)
                and value["phase"] in phases
                and isinstance(elapsed, (int, float))
                and not isinstance(elapsed, bool)
                and 0 <= elapsed <= 1800
                and math.isfinite(elapsed)
            ):
                latest = {"phase": value["phase"], "elapsed_seconds": elapsed}
    return latest


def main() -> int:
    if sys.argv[1:] not in ([], ["--preservation"], ["--financial-prefix"], ["--compaction"]):
        print(
            "Usage: python3 scripts/capacity_trial.py "
            "[--preservation|--financial-prefix|--compaction]",
            file=sys.stderr,
        )
        return 2
    compaction = sys.argv[1:] == ["--compaction"]
    preservation_only = sys.argv[1:] == ["--preservation"]
    financial_only = sys.argv[1:] == ["--financial-prefix"]
    mode = (
        "compaction"
        if compaction
        else "financial_prefix"
        if financial_only
        else "preservation"
        if preservation_only
        else "operations"
    )
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
    report: dict[str, Any] = {
        "status": "failed",
        "trial_mode": mode,
        # Covers seeding, full compaction/audit comparison and measured operations.
        # Each operation retains its independent, unchanged acceptance deadline.
        "fixture_timeout_seconds": 900 if preservation_only or compaction else 600,
    }
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
                "tests/financial_prefix_capacity.py"
                if financial_only
                else "tests/capacity_fixture.py",
                drill.database,
                *(
                    ["--preservation"]
                    if preservation_only
                    else ["--compaction"]
                    if compaction
                    else []
                ),
            ],
            timeout=report["fixture_timeout_seconds"],
        )
        report.update(json.loads(raw))
        report["status"] = "measured"
    except (OSError, ValueError, subprocess.SubprocessError, DrillError) as error:
        report["error_type"] = type(error).__name__
        if isinstance(error, DrillError):
            report["failure"] = str(error)
    finally:
        report["last_progress"] = last_progress(directory / f"command-{drill.counter:02d}.log")
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
                        "trial_mode",
                        "fixture_timeout_seconds",
                        "last_progress",
                        "operational_budgets_exercised",
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
                        "archive_input",
                        "financial_prefix",
                        "compaction",
                    )
                },
                sort_keys=True,
            )
        )
    else:
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "trial_mode": report["trial_mode"],
                    "fixture_timeout_seconds": report["fixture_timeout_seconds"],
                    "last_progress": report["last_progress"],
                    "error_type": report.get("error_type"),
                    "cleanup_complete": report["cleanup_complete"],
                },
                sort_keys=True,
            )
        )
    passed = report["status"] == "measured" and report["cleanup_complete"]
    if compaction:
        passed = passed and not report.get("measured_boundary_failures", ["unmeasured"])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

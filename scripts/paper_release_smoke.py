"""One owned disposable project; installed CLI handoff with no exchange I/O."""

import json
import subprocess
from uuid import uuid4

from bootstrap import ROOT, prepare_secrets, require_docker
from restore_drill import Drill, DrillError, private_write


def main() -> int:
    require_docker()
    prepare_secrets()
    identifier = uuid4().hex[:12]
    directory = ROOT / ".secrets" / "backups" / ("paper-release-smoke-" + identifier)
    directory.parent.mkdir(mode=0o700, exist_ok=True)
    if directory.parent.is_symlink() or directory.parent.stat().st_mode & 0o077:
        raise DrillError("paper evidence directory must be private")
    directory.mkdir(mode=0o700)
    drill = Drill(directory, identifier)
    report = {"passed": False, "live_execution_available": False}
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
                "tests/paper_release_cli_worker.py",
                drill.database,
            ]
        )
        report.update(json.loads(raw))
    except (OSError, ValueError, subprocess.SubprocessError, DrillError) as error:
        report["error_type"] = type(error).__name__
    finally:
        report["cleanup_complete"] = drill.cleanup()
        private_write(directory / "manifest.json", (json.dumps(report, indent=2) + "\n").encode())
    print("Paper release smoke: " + str(directory))
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] and report["cleanup_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

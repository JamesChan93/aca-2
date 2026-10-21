"""Packaged CLI smoke after the workflow's enrolled demo; no database mutation."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import aca

ACCOUNT = "6e04530b-9382-418d-9a24-331672f4016b"


def run(*arguments: str, success: bool = True) -> dict[str, object]:
    executable = shutil.which("aca")
    assert executable is not None
    result = subprocess.run(  # noqa: S603 - installed CLI, fixed actions and generated paths/IDs
        [executable, *arguments], capture_output=True, text=True, check=False
    )
    if (result.returncode == 0) != success:
        raise RuntimeError("packaged archive CLI outcome mismatch")
    return json.loads(result.stdout) if success else {"rejected": True}


def main() -> None:
    package = Path(aca.__file__).parent
    source = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        source.update(path.relative_to(package).as_posix().encode() + b"\0" + path.read_bytes())
    with TemporaryDirectory(prefix="aca-archive-cli-") as temporary:
        root = Path(temporary)
        path, config = root / "replay.aca", root / "offline.toml"
        exported = run("paper", "archive-export", "--account", ACCOUNT, "--output", str(path))
        assert exported["database_semantics_checked_at_export"] is True
        assert path.stat().st_mode & 0o777 == 0o600
        config.write_text(
            '[database]\nhost="offline.invalid"\n'
            'runtime_password_file="/not-mounted/runtime"\n'
            'migration_password_file="/not-mounted/migration"\n'
        )
        args = [
            "--config",
            str(config),
            "paper",
            "archive-verify",
            "--account",
            ACCOUNT,
            "--input",
            str(path),
            "--sha256",
            str(exported["archive_sha256"]),
        ]
        verified = run(*args)
        assert verified["integrity_verified"] is True
        assert verified["source_bindings_verified"] is True
        assert verified["database_semantics_rechecked"] is False
        assert verified["usable_as_operational_prefix"] is verified["pruning_authorised"] is False
        wrong_account = list(args)
        wrong_account[5] = str(uuid4())
        run(*wrong_account, success=False)
        original = path.read_bytes()
        path.write_bytes(original[:-1])
        run(*args, success=False)
    print(
        json.dumps(
            {
                "passed": True,
                "application_version": aca.__version__,
                "package_source_sha256": source.hexdigest(),
                "archive": verified,
                "database_mutations": 0,
                "offline_verification_without_credentials": True,
                "wrong_account_rejected": True,
                "truncated_archive_rejected": True,
                "temporary_files_removed": True,
                "promotion_evidence": False,
                "qualifying_days": 0,
                "qualifying_episodes": 0,
                "live_execution_available": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

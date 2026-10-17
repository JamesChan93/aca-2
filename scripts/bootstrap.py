"""Standard-library developer bootstrap; intentionally runs with host Python 3.9+."""

import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prepare_secrets(root: Path = ROOT) -> None:
    directory = root / ".secrets"
    if directory.is_symlink():
        raise RuntimeError("secret directory must not be a symlink")
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("secret directory must be owned by you with mode 0700")
    for name in ("admin_password", "runtime_password", "migration_password"):
        path = directory / name
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
                raise RuntimeError("existing secret file has unsafe permissions") from None
            continue
        with os.fdopen(descriptor, "w") as handle:
            handle.write(secrets.token_urlsafe(32) + "\n")
        # Host access is restricted by the 0700 parent. The read-only bind must also be
        # readable by different non-root UIDs in the authorised containers. Compose file
        # secrets do not implement UID remapping. Do not weaken the directory permissions.
        path.chmod(0o444)


def require_docker() -> None:
    # Docker Desktop may be installed without adding its CLI to the host PATH.
    # Limit this fallback to the standard macOS application bundle.
    if shutil.which("docker") is None and sys.platform == "darwin":
        directory = Path("/Applications/Docker.app/Contents/Resources/bin")
        if (directory / "docker").is_file():
            os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
    if shutil.which("docker") is None:
        raise RuntimeError("Install Docker with Compose v2.24+ and start its engine, then rerun")
    subprocess.run(
        ["docker", "info"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    subprocess.run(["docker", "compose", "version"], check=True)


def main() -> int:
    try:
        require_docker()
        prepare_secrets()
        subprocess.run(["docker", "compose", "config", "--quiet"], cwd=ROOT, check=True)
        subprocess.run(
            ["docker", "compose", "up", "--build", "--wait", "--wait-timeout", "180"],
            cwd=ROOT,
            check=True,
        )
        subprocess.run(
            ["docker", "compose", "exec", "-T", "app", "aca", "health"], cwd=ROOT, check=True
        )
        print("Paper-only foundation ready. Trading readiness remains false.")
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(
            "Bootstrap failed: "
            + (str(error) if isinstance(error, RuntimeError) else type(error).__name__),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

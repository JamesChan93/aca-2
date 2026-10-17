"""Start only a disposable local PostgreSQL test container; no app/exchange access."""

import os
import subprocess
import sys
from urllib.parse import quote

from bootstrap import ROOT, prepare_secrets, require_docker


def main() -> int:
    require_docker()
    prepare_secrets()
    compose = [
        "docker",
        "compose",
        "-p",
        "aca-foundation-tests",
        "-f",
        "compose.yaml",
        "-f",
        "compose.test.yaml",
    ]
    arguments = sys.argv[1:]
    linux = bool(arguments and arguments[0] == "--linux")
    if linux:
        arguments = arguments[1:]
    password = (ROOT / ".secrets/admin_password").read_text().strip()
    environment = dict(os.environ)
    environment["REQUIRE_POSTGRES"] = "1"
    environment["TEST_DATABASE_URL"] = (
        "postgresql://aca_admin:" + quote(password, safe="") + "@127.0.0.1:55432/aca"
    )
    environment["TEST_RUNTIME_PASSWORD_FILE"] = str(ROOT / ".secrets/runtime_password")
    environment["TEST_MIGRATION_PASSWORD_FILE"] = str(ROOT / ".secrets/migration_password")
    try:
        subprocess.run([*compose, "up", "--build", "-d", "--wait", "db"], cwd=ROOT, check=True)
        if linux:
            return subprocess.run(
                [
                    *compose,
                    "run",
                    "--build",
                    "--rm",
                    "--no-deps",
                    "linux_tests",
                    "python",
                    "scripts/linux_tests.py",
                    *arguments,
                ],
                cwd=ROOT,
                check=False,
            ).returncode
        return subprocess.run(
            [sys.executable, "-m", "pytest", *arguments], cwd=ROOT, env=environment, check=False
        ).returncode
    finally:
        subprocess.run([*compose, "down"], cwd=ROOT, check=False)


if __name__ == "__main__":
    raise SystemExit(main())

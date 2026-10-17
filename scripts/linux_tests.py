"""Run the full suite inside the isolated Linux test container and DB namespace."""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from aca.operations.secrets import read_secret


def main() -> int:
    if sys.platform != "linux":
        raise RuntimeError("use ci_database.py --linux to run the Linux test container")
    password = read_secret(Path("/run/secrets/admin_password")).get_secret_value()
    environment = dict(os.environ)
    environment["REQUIRE_POSTGRES"] = "1"
    environment["TEST_DATABASE_URL"] = (
        "postgresql://aca_admin:" + quote(password, safe="") + "@127.0.0.1:5432/aca"
    )
    environment["TEST_RUNTIME_PASSWORD_FILE"] = str(Path("/run/secrets/runtime_password"))
    environment["TEST_MIGRATION_PASSWORD_FILE"] = str(Path("/run/secrets/migration_password"))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *sys.argv[1:]],
        env=environment,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())

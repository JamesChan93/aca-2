"""Verify health after restart without exposing application/database ports."""

import json
import subprocess
import time

from bootstrap import require_docker


def main() -> int:
    require_docker()
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        response = subprocess.run(
            ["docker", "compose", "exec", "-T", "app", "aca", "health"],
            capture_output=True,
            text=True,
            check=False,
        )
        if response.returncode == 0:
            payload = json.loads(response.stdout)
            if payload["trading_ready"] or payload["live_execution_available"]:
                raise RuntimeError("unexpected execution capability")
            audit = subprocess.run(
                ["docker", "compose", "exec", "-T", "app", "aca", "audit", "--limit", "10"],
                capture_output=True,
                text=True,
                check=True,
            )
            events = json.loads(audit.stdout)
            if sum(item["event"] == "foundation_started" for item in events) < 2:
                raise RuntimeError("restart did not preserve startup audit evidence")
            print("Container restart, persistent audit and paper-only capability verified")
            return 0
        time.sleep(1)
    raise RuntimeError("foundation did not recover within smoke-check timeout")


if __name__ == "__main__":
    raise SystemExit(main())

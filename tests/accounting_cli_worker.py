"""Packaged CLI smoke on a new virtual account in a disposable Compose project.

Run via `docker compose ... exec -T app python - < tests/accounting_cli_worker.py`.
The caller owns the disposable project and cleanup. No exchange/notification I/O.
"""

import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4


def main() -> None:
    executable = shutil.which("aca")
    assert executable is not None
    account, actor = str(uuid4()), str(uuid4())

    def cli(*args: str) -> dict[str, Any]:
        result = subprocess.run(  # noqa: S603 - installed CLI, fixed actions and generated UUIDs
            [executable, "paper", *args, "--account", account],
            check=True,
            capture_output=True,
            text=True,
        )
        value: dict[str, Any] = json.loads(result.stdout)
        return value

    def flow(
        asset: str, quantity: str, rate: str, occurred_at: str | None = None
    ) -> dict[str, object]:
        at = occurred_at or datetime.now(UTC).isoformat()
        return {
            "kind": "transfer",
            "asset": asset,
            "quantity": quantity,
            "occurred_at": at,
            "valuation": {
                "asset": asset,
                "bid_gbp": rate,
                "observed_at": at,
                "source": "disposable-C2-smoke-only",
            },
        }

    cli("create")
    with tempfile.TemporaryDirectory(prefix="aca-c2-") as directory:
        request_file = Path(directory) / "request.json"

        def request() -> dict[str, object]:
            return {
                "request_id": str(uuid4()),
                "actor": actor,
                "expected_version": cli("status")["state"]["version"],
                "evidence_sha256": hashlib.sha256(
                    b"synthetic C2 packaged smoke; no real transfers"
                ).hexdigest(),
                "reason": "Disposable packaged C2 accounting fixture",
            }

        def transfer(asset: str, quantity: str, rate: str) -> dict[str, Any]:
            body = {
                **request(),
                "external_reference": str(uuid4()),
                "flow": flow(asset, quantity, rate),
            }
            request_file.write_text(json.dumps(body))
            first = cli("transfer", "--input", str(request_file))
            assert first["inserted"] is True
            assert cli("transfer", "--input", str(request_file))["inserted"] is False
            return first

        opening = transfer("GBP", "250", "1")
        transfer("BTC", "0.001", "60000")
        transfer("BTC", "-0.001", "60000")
        before = cli("status")
        assert Decimal(before["state"]["units"]) == 250
        assert Decimal(before["performance"]["pnl_gbp"]) == 0
        replacement = flow("GBP", "240", "1", opening["record"]["request"]["flow"]["occurred_at"])
        body = {
            **request(),
            "target_journal": opening["record"]["postings"][0]["id"],
            "replacement": replacement,
        }
        request_file.write_text(json.dumps(body))
        corrected = cli("correct", "--input", str(request_file))
        assert corrected["inserted"] is True
        assert cli("correct", "--input", str(request_file))["inserted"] is False
        assert not cli("recover")["complete"]
        assert not cli("recover")["complete"]
        after, history = cli("status"), cli("accounting")
        state = after["state"]
        assert state["accounting_invalidated"] and state["kill"]["hard"] and not state["ready"]
        assert Decimal(state["balances"][0]["quantity"]) == 240
        assert all(Decimal(b["quantity"]) == 0 for b in state["balances"][1:])
        assert state["orders"] == [] and after["performance"]["pnl_gbp"] is None
        assert len(history["items"]) == 4
        assert cli("performance")["units_valid"] is False
        assert (
            cli("daily", "--day", datetime.now(UTC).date().isoformat())["daily_return_complete"]
            is False
        )
        assert cli("audit")["complete"] is True
        print(
            json.dumps(
                {
                    "passed": True,
                    "account_id": account,
                    "before": before,
                    "after": after,
                    "accounting": history,
                    "qualifying_days": 0,
                    "qualifying_episodes": 0,
                    "live_execution_available": False,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()

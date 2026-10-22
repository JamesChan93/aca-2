"""Installed CLI handoff smoke in a new, disposable, credential-free paper account."""

import asyncio
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from uuid import UUID, uuid4

from restore_fixture import create_database, settings_for
from test_stored_replay import Feed

from aca.execution.paper import PaperExecution
from aca.operations.config import Settings
from aca.simulation.paper import PaperExchange
from aca.simulation.stored_replay import StoredReplay
from aca.storage.audit_prefix import package_fingerprint
from aca.storage.database import create_engine
from aca.storage.migrate import migrate
from aca.storage.paper import PaperStore, PaperWriter


async def prepare(name: str) -> Settings:
    settings = settings_for(name)
    create_database(settings)
    await migrate(settings)
    return settings


async def capture(settings: Settings, account: str) -> None:
    engine = create_engine(settings)
    store = PaperStore(engine, UUID(account))
    try:
        async with PaperWriter(store) as owner:
            venue = PaperExchange(store)
            await Feed(StoredReplay(PaperExecution(owner, venue), venue)).bootstrap(
                datetime.now(UTC)
            )
    finally:
        await engine.dispose()


def main() -> None:
    settings = asyncio.run(prepare(sys.argv[1]))
    account, actor = str(uuid4()), str(uuid4())
    executable = shutil.which("aca")
    assert executable is not None
    with TemporaryDirectory(prefix="aca-paper-release-") as directory:
        root = Path(directory)
        operator, runtime = root / "operator.toml", root / "runtime.toml"
        db = settings.database
        fields = {
            name: getattr(db, name)
            for name in ("host", "port", "name", "runtime_password_file", "migration_password_file")
        }
        for path, values in (
            (operator, fields),
            (runtime, {**fields, "migration_password_file": "/not-mounted/migration"}),
        ):
            path.write_text(
                'mode="paper"\n[database]\n'
                + "\n".join(f"{name}={json.dumps(value)}" for name, value in values.items())
                + '\n[health]\nhost="127.0.0.1"\nport=8080\n'
            )
            path.chmod(0o600)

        def cli(*args: str, config: Path = runtime, success: bool = True) -> dict[str, Any]:
            result = subprocess.run(  # noqa: S603 - installed CLI; generated IDs/private config only
                [executable, "--config", str(config), *args],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if (result.returncode == 0) != success:
                raise RuntimeError("packaged paper handoff command failed")
            return json.loads(result.stdout) if success else {"rejected": True}

        def paper(action: str, *args: str, **kwargs: Any) -> dict[str, Any]:
            return cli("paper", action, "--account", account, *args, **kwargs)

        cli("capabilities")
        paper("create")
        paper(
            "fund",
            "--reference",
            "packaged-opening",
            "--amount-gbp",
            "250",
            "--occurred-at",
            datetime.now(UTC).isoformat(),
        )
        paper(
            "select",
            "--strategy",
            "sma200",
            "--symbol",
            "BTC/GBP",
            "--interval",
            "240",
            "--operational-experiment",
        )
        paper("enrol")
        asyncio.run(capture(settings, account))
        run_args = [
            executable,
            "--config",
            str(runtime),
            "paper",
            "run",
            "--account",
            account,
            "--duration",
            "5",
        ]
        process = subprocess.Popen(run_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # noqa: S603
        observed_health = None
        try:
            deadline = monotonic() + 8
            while monotonic() < deadline and process.poll() is None:
                try:
                    with urlopen("http://127.0.0.1:8080/health/paper", timeout=1) as response:
                        observed_health = json.load(response)
                except HTTPError as error:
                    observed_health = json.load(error)
                except URLError:
                    sleep(0.05)
                    continue
                break
            stdout, _ = process.communicate(timeout=15)
            assert process.returncode == 0 and observed_health is not None
            run = json.loads(stdout)
            assert run["ticks"] >= 1 and run["strategy_dispatch_enabled"] is True
            assert run["fill_replay_enabled"] is True
            assert run["qualifying_days"] == run["qualifying_episodes"] == 0
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
        paper("halt", "--reason", "Packaged release manual hard halt")
        status = paper("status")
        assert status["state"]["kill"]["hard"] is True and status["state"]["orders"] == []
        before = paper("performance")
        expected = [
            "--actor",
            actor,
            "--reason",
            "Packaged complete-history handoff audit",
            "--expected-version",
            str(status["state"]["version"]),
            "--expected-source",
            str(before["historical_head_source"]),
        ]
        paper("history-compact", *expected, success=False)
        compacted = paper("history-compact", *expected, config=operator)
        prefix = ["--history-prefix", compacted["certificate"]["id"]]
        paper("status", "--history-prefix", str(uuid4()), success=False)
        paper("recover", *prefix)
        after = paper("status", *prefix)
        assert after["state"]["kill"]["hard"] is True and after["state"]["orders"] == []
        assert paper("audit")["complete"] is True
        assert paper("performance", *prefix)["period"] == before["period"]
        assert (
            paper("daily", *prefix, "--day", datetime.now(UTC).date().isoformat())[
                "daily_return_complete"
            ]
            is False
        )
        print(
            json.dumps(
                {
                    "passed": True,
                    "package_source_sha256": package_fingerprint(),
                    "created_funded_selected_enrolled": True,
                    "collector_input": "deterministic local public fixtures; no network capture",
                    "packaged_runner_ticks": run["ticks"],
                    "health_observed": True,
                    "manual_halt_survived_recovery": True,
                    "full_audit_passed": True,
                    "compaction_cli_without_migration_secret_rejected": True,
                    "required_bad_certificate_rejected": True,
                    "financial_reports_equivalent": True,
                    "orders_created": 0,
                    "qualifying_days": 0,
                    "qualifying_episodes": 0,
                    "live_execution_available": False,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()

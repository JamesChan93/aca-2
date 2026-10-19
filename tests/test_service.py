import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from aca.operations.config import Settings
from aca.storage.database import FoundationStore, create_engine

pytestmark = pytest.mark.integration


async def request_health(port: int) -> tuple[int, dict[str, object]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(b"GET /health/ready HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        headers, body = response.split(b"\r\n\r\n", 1)
        return int(headers.split(b" ")[1]), json.loads(body)
    finally:
        writer.close()
        await writer.wait_closed()


async def wait_health(port: int, expected: int) -> dict[str, object]:
    async with asyncio.timeout(12):
        while True:
            try:
                code, payload = await request_health(port)
                if code == expected:
                    return payload
            except OSError, ValueError:
                pass
            await asyncio.sleep(0.1)


async def test_service_restart_schema_failure_and_graceful_shutdown(
    database_settings: Settings,
    tmp_path: Path,
) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = tmp_path / "service.toml"
    db = database_settings.database
    config.write_text(
        'mode = "paper"\n[database]\n'
        f"host = {json.dumps(db.host)}\nport = {db.port}\nname = {json.dumps(db.name)}\n"
        f"runtime_password_file = {json.dumps(db.runtime_password_file)}\n"
        f"migration_password_file = {json.dumps(db.migration_password_file)}\n"
        f'[health]\nhost = "127.0.0.1"\nport = {port}\ndatabase_check_interval_seconds = 1\n'
    )
    migration_engine = create_engine(database_settings, "migration")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("ACA_")}
    try:
        for iteration in range(2):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "aca",
                "--config",
                str(config),
                "serve",
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                payload = await wait_health(port, 200)
                assert payload["live_execution_available"] is False
                assert payload["trading_ready"] is False
                if iteration == 0:
                    async with migration_engine.begin() as connection:
                        await connection.execute(
                            text(
                                "UPDATE aca.alembic_version SET version_num = 'unsupported'",
                            )
                        )
                    failed = await wait_health(port, 503)
                    assert failed["schema_ready"] is False
                    async with migration_engine.begin() as connection:
                        await connection.execute(
                            text(
                                "UPDATE aca.alembic_version SET version_num = '0009_notifications'",
                            )
                        )
                    await wait_health(port, 200)
            finally:
                if process.returncode is None:
                    process.terminate()
                async with asyncio.timeout(10):
                    output, error = await process.communicate()
                assert process.returncode == 0, error.decode()
                password = (
                    await asyncio.to_thread(Path(db.runtime_password_file).read_text)
                ).strip()
                assert password not in output.decode() + error.decode()
    finally:
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE aca.alembic_version SET version_num = '0009_notifications'",
                )
            )
        await migration_engine.dispose()
    runtime = create_engine(database_settings)
    try:
        events = await FoundationStore(runtime).list_events(100)
        assert sum(item["event"] == "foundation_started" for item in events) >= 2
        assert sum(item["event"] == "foundation_stopped" for item in events) >= 2
    finally:
        await runtime.dispose()

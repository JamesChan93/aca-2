"""Local administration and opt-in public market reads. No trading actions exist."""

import argparse
import asyncio
import http.client
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import structlog
from pydantic import ValidationError

from aca import __version__
from aca.app.health import BLOCKERS
from aca.app.service import serve
from aca.market_data.capture import capture
from aca.market_data.store import MarketStore
from aca.observability.logging import configure_logging
from aca.operations.config import ConfigurationError, Settings, load_settings
from aca.operations.provision import provision_roles
from aca.operations.secrets import SecretFileError
from aca.research.cli import add_parser as add_research_parser
from aca.research.cli import execute as research_command
from aca.storage.database import FoundationStore, create_engine, verify_runtime
from aca.storage.migrate import migrate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aca", description="Paper-only platform with opt-in public market capture"
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities")
    commands.add_parser("serve")
    add_research_parser(commands)
    market = commands.add_parser("market").add_subparsers(dest="action", required=True)
    market.add_parser("status")
    evidence = market.add_parser("evidence")
    evidence.add_argument(
        "--channel",
        required=True,
        choices=(
            "instrument_rules",
            "rest:closed_candles",
            "local:closed_candle",
            "trade_recovery",
            "capture_fault",
            "coverage_gap",
        ),
    )
    evidence.add_argument("--limit", type=int, default=5, choices=range(1, 21))
    market_capture = market.add_parser("capture")
    market_capture.add_argument(
        "--duration",
        type=int,
        default=300,
        metavar="SECONDS",
        help="wall-clock capture duration; 0 runs until interrupted",
    )
    config = commands.add_parser("config").add_subparsers(dest="action", required=True)
    config.add_parser("check")
    database = commands.add_parser("db").add_subparsers(dest="action", required=True)
    database.add_parser("check")
    database.add_parser("migrate")
    provision = database.add_parser("provision")
    provision.add_argument("--admin-password-file", type=Path, required=True)
    audit = commands.add_parser("audit")
    audit.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="1..100")
    health = commands.add_parser("health")
    health.add_argument("--url", default="http://127.0.0.1:8000/health/ready")
    return parser


async def _database_command(
    settings: Settings, command: str, limit: int = 20, channel: str | None = None
) -> None:
    engine = create_engine(settings)
    try:
        await verify_runtime(engine)
        result: object = {"database_ready": True, "trading_ready": False}
        if command == "audit":
            result = await FoundationStore(engine).list_events(limit)
        elif command == "market_status":
            result = await MarketStore(engine).status()
        elif command == "market_evidence" and channel is not None:
            result = await MarketStore(engine).evidence(channel=channel, limit=limit)
        print(json.dumps(result, sort_keys=True))
    finally:
        await engine.dispose()


def _health(url: str) -> int:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.path
        not in {
            "/health/live",
            "/health/ready",
            "/health/trading",
            "/health/market",
        }
    ):
        raise ConfigurationError("health checks require a local HTTP health endpoint")
    # HTTPConnection does not follow redirects, including redirects off loopback.
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=3)
    try:
        connection.request("GET", parsed.path)
        response = connection.getresponse()
        print(response.read(16384).decode())
        return 0 if response.status == 200 else 1
    except OSError, http.client.HTTPException:
        print('{"health_check_passed":false}')
        return 1
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging()
    try:
        settings = load_settings(args.config)
        configure_logging(settings.log_level)
        if args.command == "capabilities":
            print(
                json.dumps(
                    {
                        "phase": 3,
                        "local_research_available": True,
                        "public_market_capture_available": True,
                        "private_account_access_available": False,
                        "mode": "paper",
                        "version": __version__,
                        "live_execution_available": False,
                        "incomplete_safety_requirements": list(BLOCKERS),
                    }
                )
            )
        elif args.command == "research":
            return research_command(args, settings)
        elif args.command == "market":
            if args.action == "status":
                asyncio.run(_database_command(settings, "market_status"))
            elif args.action == "evidence":
                asyncio.run(
                    _database_command(settings, "market_evidence", args.limit, args.channel)
                )
            else:
                if not 0 <= args.duration <= 86400:
                    raise ConfigurationError("duration must be 0..86400 seconds")
                result = asyncio.run(capture(settings, args.duration))
                print(json.dumps(result, sort_keys=True))
                return 0 if result["healthy_observations"] else 1
        elif args.command == "config":
            print(
                json.dumps(
                    {
                        "valid": True,
                        "mode": "paper",
                        "configuration_fingerprint": settings.fingerprint(),
                    }
                )
            )
        elif args.command == "serve":
            asyncio.run(serve(settings))
        elif args.command == "db" and args.action == "provision":
            provision_roles(settings, args.admin_password_file)
            print('{"roles_provisioned":true}')
        elif args.command == "db" and args.action == "migrate":
            asyncio.run(migrate(settings))
            print('{"schema_migrated":true}')
        elif args.command == "db":
            asyncio.run(_database_command(settings, "check"))
        elif args.command == "audit":
            asyncio.run(_database_command(settings, "audit", args.limit))
        elif args.command == "health":
            return _health(args.url)
        return 0
    except ValidationError, ConfigurationError, SecretFileError:
        # Do not print Pydantic input values, secret paths or raw driver exceptions.
        print(
            "Configuration rejected; check non-secret settings and secret-file permissions.",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        structlog.get_logger().error("operation_failed", error_type=type(exc).__name__)
        return 1

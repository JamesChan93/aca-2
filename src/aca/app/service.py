import asyncio
import signal
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

import structlog

from aca.app.health import FoundationMetrics, FoundationStatus, HealthServer
from aca.operations.config import Settings
from aca.storage.database import FoundationStore, create_engine, verify_runtime


async def serve(settings: Settings) -> None:
    engine = create_engine(settings)
    store = FoundationStore(engine)
    status = FoundationStatus(freshness_seconds=settings.health.database_check_interval_seconds + 5)
    metrics = FoundationMetrics()
    server = HealthServer(status, metrics)
    stop = asyncio.Event()
    run_id = uuid4()
    logger = structlog.get_logger().bind(
        mode="paper",
        phase=1,
        environment=settings.environment,
        run_id=str(run_id),
        configuration_fingerprint=settings.fingerprint(),
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    recorded = False
    previously_ready: bool | None = None
    try:
        await server.start(settings.health.host, settings.health.port)
        logger.info("foundation_listening", live_execution_available=False)
        while not stop.is_set():
            try:
                async with asyncio.timeout(4):
                    await verify_runtime(engine)
                    if not recorded:
                        await store.record_startup(settings, run_id)
                        recorded = True
                status.database_ready = True
                status.schema_ready = True
                status.last_success = monotonic()
            except Exception as exc:
                status.database_ready = False
                status.schema_ready = False
                metrics.database_failures.inc()
                logger.warning("database_check_failed", error_type=type(exc).__name__)
            if previously_ready != status.ready:
                logger.info(
                    "foundation_readiness_changed",
                    foundation_ready=status.ready,
                    trading_ready=False,
                )
                previously_ready = status.ready
            try:
                async with asyncio.timeout(settings.health.database_check_interval_seconds):
                    await stop.wait()
            except TimeoutError:
                pass
    finally:
        status.shutting_down = True
        await server.close()
        if recorded:
            try:
                async with asyncio.timeout(3):
                    await store.record_event(
                        run_id=run_id,
                        fingerprint=settings.fingerprint(),
                        event="foundation_stopped",
                        occurred_at=datetime.now(UTC),
                        detail={"live_execution_available": False},
                    )
            except Exception as exc:
                logger.warning("shutdown_audit_unavailable", error_type=type(exc).__name__)
        await engine.dispose()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        logger.info("foundation_stopped")

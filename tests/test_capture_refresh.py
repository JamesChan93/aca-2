"""Metadata maintenance must preserve long-bar coverage when rules are unchanged."""

from typing import Any
from uuid import uuid4

import pytest
from test_market_contracts import NOW
from test_market_processor import rules

from aca.adapters.kraken.wire import MarketDataError
from aca.domain.market import InstrumentRules
from aca.market_data.capture import CaptureStatus, PublicCapture
from aca.market_data.processor import PublicProcessor
from aca.market_data.store import MarketStore


@pytest.mark.parametrize("changed", [False, True])
async def test_hourly_refresh_keeps_epoch_or_requires_new_verification(
    changed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = PublicCapture.__new__(PublicCapture)
    capture.status = CaptureStatus()
    capture.epoch = uuid4()
    capture.rules_refreshed = 0
    capture.store = MarketStore.__new__(MarketStore)
    processor = PublicProcessor(rules(), capture.epoch)
    processor.instrument_seen.update(processor.rules)
    before = set(processor.instrument_seen)
    observations: list[str] = []

    async def check() -> None:
        pass

    async def append(**kwargs: Any) -> tuple[()]:
        observations.append(kwargs["channel"])
        return ()

    class Rest:
        async def instruments(self) -> tuple[InstrumentRules, ...]:
            result = tuple(r.model_copy(update={"observed_at": NOW}) for r in rules())
            if changed:
                result = (result[0].model_copy(update={"status": "offline"}), result[1])
            return result

    monkeypatch.setattr(capture, "check_exchange", check)
    monkeypatch.setattr(capture.store, "append", append)
    monkeypatch.setattr(capture, "rest", Rest(), raising=False)
    monkeypatch.setattr("aca.market_data.capture.monotonic", lambda: 3600.0)
    if changed:
        with pytest.raises(MarketDataError, match="fresh v2"):
            await capture.maintain_exchange(processor)
        assert observations == []
    else:
        await capture.maintain_exchange(processor)
        assert observations == ["instrument_rules"]
        assert capture.rules_refreshed == 3600
        assert processor.epoch == capture.epoch
        assert processor.instrument_seen == before
        # Another ordinary status check does not create a second rules receipt.
        await capture.maintain_exchange(processor)
        assert observations == ["instrument_rules"]

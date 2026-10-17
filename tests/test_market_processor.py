import json
from datetime import timedelta
from decimal import Decimal
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_market_contracts import NOW, book_data

from aca.adapters.kraken.wire import MarketDataError
from aca.domain.market import InstrumentRules, MarketTrade, Symbol
from aca.market_data.candles import CandleAggregator, LateTradeError
from aca.market_data.capture import CaptureHealthServer, CaptureStatus
from aca.market_data.metrics import CaptureMetrics
from aca.market_data.processor import PublicProcessor


def rules() -> tuple[InstrumentRules, ...]:
    return tuple(
        InstrumentRules(
            venue="kraken",
            symbol=cast(Symbol, symbol),
            venue_symbol=cast(Symbol, symbol),
            rest_pair=pair,
            price_increment=Decimal(tick),
            quantity_increment=Decimal("0.00000001"),
            minimum_quantity=Decimal(minimum),
            minimum_cost=Decimal("0.43"),
            status="online",
            observed_at=NOW,
        )
        for symbol, pair, tick, minimum in (
            ("BTC/GBP", "XXBTZGBP", "0.1", "0.00006"),
            ("ETH/GBP", "XETHZGBP", "0.01", "0.002"),
        )
    )


def instrument_message() -> str:
    return json.dumps(
        {
            "channel": "instrument",
            "type": "snapshot",
            "data": {
                "pairs": [
                    {
                        "symbol": r.symbol,
                        "base": r.symbol.split("/")[0],
                        "quote": "GBP",
                        "price_increment": str(r.price_increment),
                        "qty_increment": str(r.quantity_increment),
                        "qty_min": str(r.minimum_quantity),
                        "cost_min": str(r.minimum_cost),
                        "status": r.status,
                    }
                    for r in rules()
                ]
            },
        }
    )


def trade_message(
    *,
    kind: str = "snapshot",
    symbol: str = "BTC/GBP",
    trade_id: int = 1,
    price: str = "100",
    seconds: int = 0,
) -> str:
    return json.dumps(
        {
            "channel": "trade",
            "type": kind,
            "data": [
                {
                    "symbol": symbol,
                    "side": "buy",
                    "price": price,
                    "qty": "0.01",
                    "trade_id": trade_id,
                    "timestamp": (NOW + timedelta(seconds=seconds)).isoformat(),
                    "ord_type": "limit",
                }
            ],
        }
    )


def initialize(processor: PublicProcessor) -> None:
    processor.process('{"channel":"status","data":[{"system":"online"}]}', NOW)
    processor.process(instrument_message(), NOW)
    for symbol in ("BTC/GBP", "ETH/GBP"):
        data = book_data().model_dump(mode="json") | {"symbol": symbol}
        processor.process(json.dumps({"channel": "book", "type": "snapshot", "data": [data]}), NOW)
        processor.process(trade_message(symbol=symbol), NOW)


def test_snapshot_recovery_freshness_and_explicit_invalidity() -> None:
    processor = PublicProcessor(rules(), uuid4())
    assert not processor.health(NOW)["market_ready"]
    initialize(processor)
    assert processor.health(NOW)["market_ready"]
    assert not processor.health(NOW + timedelta(seconds=6))["market_ready"]
    processor.process('{"channel":"heartbeat"}', NOW + timedelta(seconds=11))
    assert not processor.health(NOW + timedelta(seconds=11))["market_ready"]
    processor.invalidate()
    assert all(state.book is None for state in processor.books.values())
    assert not processor.health(NOW)["market_ready"]


@pytest.mark.parametrize(
    "payload",
    [
        '{"channel":"unknown"}',
        '{"method":"subscribe","success":false}',
        '{"channel":"status","data":[{"system":"new_status"}]}',
        '{"channel":"book","type":"new","data":[]}',
        '{"channel":"instrument","type":"snapshot","data":{}}',
    ],
)
def test_unknown_critical_messages_are_not_assumed_safe(payload: str) -> None:
    processor = PublicProcessor(rules(), uuid4())
    with pytest.raises(MarketDataError):
        processor.process(payload, NOW)


def test_rules_conflict_and_non_online_status_block_readiness() -> None:
    processor = PublicProcessor(rules(), uuid4())
    message = json.loads(instrument_message())
    message["data"]["pairs"][0]["qty_min"] = "0.1"
    with pytest.raises(MarketDataError, match="conflict"):
        processor.process(json.dumps(message), NOW)
    initialize(processor)
    processor.process('{"channel":"status","data":[{"system":"maintenance"}]}', NOW)
    assert not processor.health(NOW)["market_ready"]


def test_trade_identity_not_timestamp_and_future_or_wrong_asset_rejected() -> None:
    processor = PublicProcessor(rules(), uuid4())
    first = processor.process(trade_message(trade_id=1), NOW).trades[0]
    second = processor.process(trade_message(trade_id=2), NOW).trades[0]
    assert first.trade_id != second.trade_id and first.event_at == second.event_at
    with pytest.raises(MarketDataError, match="future"):
        processor.process(trade_message(seconds=6), NOW)
    with pytest.raises(ValidationError):
        processor.process(trade_message(symbol="SOL/GBP"), NOW)
    fresh = PublicProcessor(rules(), uuid4())
    with pytest.raises(MarketDataError, match="before snapshot"):
        fresh.process(trade_message(kind="update"), NOW)


def trade(trade_id: int, minutes: int, price: str = "100") -> MarketTrade:
    return MarketTrade(
        venue="kraken",
        symbol="BTC/GBP",
        trade_id=str(trade_id),
        side="buy",
        price=Decimal(price),
        quantity=Decimal("0.1"),
        event_at=NOW + timedelta(minutes=minutes),
        received_at=NOW + timedelta(minutes=max(minutes, 0)),
    )


def test_out_of_order_trades_use_event_time_and_late_finalized_trades_fail() -> None:
    aggregate = CandleAggregator(NOW)
    for event in (trade(2, 2, "120"), trade(1, 1, "100"), trade(3, 3, "110")):
        aggregate.add(event)
    assert aggregate.close(NOW + timedelta(hours=4, seconds=9)) == ()
    candles = aggregate.close(NOW + timedelta(hours=4, seconds=10))
    assert len(candles) == 1
    candle = candles[0]
    assert (candle.open, candle.high, candle.low, candle.close) == tuple(
        Decimal(v) for v in ("100", "120", "100", "110")
    )
    assert candle.volume == Decimal("0.3") and candle.trade_count == 3
    assert candle.complete
    with pytest.raises(LateTradeError):
        aggregate.add(trade(4, 2, "999"))


def test_partial_epoch_and_empty_intervals_never_invent_liquidity() -> None:
    aggregate = CandleAggregator(NOW + timedelta(minutes=1))
    aggregate.add(trade(1, 2))
    candles = aggregate.close(NOW + timedelta(hours=4, seconds=10))
    assert len(candles) == 1 and not candles[0].complete
    assert candles[0].quality == ("partial_capture_interval",)
    assert aggregate.close(NOW + timedelta(hours=8, seconds=10)) == ()
    # An older bounded trade snapshot cannot create a supposedly observed candle.
    fresh = CandleAggregator(NOW + timedelta(hours=1))
    fresh.add(trade(1, 0))
    assert fresh.close(NOW + timedelta(days=1)) == ()


def test_public_health_is_independent_of_trading_health() -> None:
    status = CaptureStatus()
    metrics = CaptureMetrics()
    server = CaptureHealthServer(status, metrics)
    assert server.response("GET", "/health/market")[0] == 503
    assert server.response("GET", "/health/trading")[0] == 503
    assert b"aca_public_market_ready 0.0" in metrics.render(status)
    assert status.document()["live_execution_available"] is False


def test_bad_message_cannot_leave_a_previously_healthy_processor_ready() -> None:
    processor = PublicProcessor(rules(), uuid4())
    initialize(processor)
    assert processor.health(NOW)["market_ready"]
    with pytest.raises(MarketDataError):
        processor.process('{"channel":"new_critical_channel"}', NOW)
    assert not processor.health(NOW)["market_ready"]
    assert all(book.book is None for book in processor.books.values())


def test_missing_candle_intervals_are_reported_without_synthetic_bars() -> None:
    from aca.market_data.candles import unexplained_gaps

    aggregate = CandleAggregator(NOW, venue="second_venue")
    aggregate.add(trade(1, 1).model_copy(update={"venue": "second_venue"}))
    first = aggregate.close(NOW + timedelta(hours=4, seconds=10))[0]
    aggregate.add(trade(2, 481).model_copy(update={"venue": "second_venue"}))
    second = next(
        c
        for c in aggregate.close(NOW + timedelta(hours=12, seconds=10))
        if c.interval_minutes == 240
    )
    gaps = unexplained_gaps((first, second))
    assert len(gaps) == 1 and gaps[0]["classification"] == "unexplained"
    assert first.venue == "second_venue"

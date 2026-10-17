import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from aca.adapters.kraken.book import KrakenBook, checksum
from aca.adapters.kraken.wire import BookData, MarketDataError, decode, number
from aca.domain.market import Candle, Level, OrderBook

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def test_official_checksum_golden_fixture() -> None:
    fixture = json.loads((Path(__file__).parent / "fixtures/kraken/checksum.json").read_text())
    bids = {Decimal(p): Decimal(q) for p, q in fixture["bids"]}
    asks = {Decimal(p): Decimal(q) for p, q in fixture["asks"]}
    assert checksum(bids, asks) == fixture["checksum"] == 3310070434
    assert checksum(dict(reversed(list(bids.items()))), asks) == 3310070434


@pytest.mark.parametrize(
    "raw", ['{"price":NaN}', '{"price":Infinity}', '{"x":1,"x":2}', "[]", "{", '{"p":-Infinity}']
)
def test_corrupt_json_rejected(raw: str) -> None:
    with pytest.raises(MarketDataError):
        decode(raw)


def test_exact_json_and_wire_integer_price() -> None:
    assert decode('{"price":1.23000000}')["price"] == Decimal("1.23000000")
    assert str(decode('{"price":1.23000000}')["price"]) == "1.23000000"
    assert number(42) == Decimal("42")
    with pytest.raises(ValueError):
        number(1.25)


def book_data(price: str = "101", bid: str = "100", qty: str = "1.000") -> BookData:
    return BookData.model_validate(
        {
            "symbol": "BTC/GBP",
            "bids": [{"price": bid, "qty": qty}],
            "asks": [{"price": price, "qty": qty}],
            "checksum": checksum({Decimal(bid): Decimal(qty)}, {Decimal(price): Decimal(qty)}),
            "timestamp": NOW.isoformat(),
        }
    )


def test_book_lifecycle_and_corruption_discards_state() -> None:
    book = KrakenBook("BTC/GBP", uuid4())
    with pytest.raises(MarketDataError, match="before snapshot"):
        book.apply(book_data(), "update", NOW)
    snapshot = book.apply(book_data(), "snapshot", NOW)
    assert snapshot.bids[0].price == Decimal("100")
    assert book.apply(book_data(), "update", NOW).bids == snapshot.bids
    with pytest.raises(MarketDataError, match="checksum"):
        book.apply(book_data().model_copy(update={"checksum": 0}), "update", NOW)
    assert book.book is None and not book.bids and not book.asks
    with pytest.raises(MarketDataError):
        book.apply(book_data(), "update", NOW)
    assert book.apply(book_data(), "snapshot", NOW).checksum_valid


def test_book_deletion_duplicate_price_updates_and_depth_truncation() -> None:
    book = KrakenBook("BTC/GBP", uuid4())
    bids = {Decimal(100 - i): Decimal("1.000") for i in range(10)}
    asks = {Decimal(101 + i): Decimal("1.000") for i in range(10)}
    data = BookData.model_validate(
        {
            "symbol": "BTC/GBP",
            "bids": [{"price": str(p), "qty": str(q)} for p, q in bids.items()],
            "asks": [{"price": str(p), "qty": str(q)} for p, q in asks.items()],
            "checksum": checksum(bids, asks),
        }
    )
    book.apply(data, "snapshot", NOW)
    bids.pop(Decimal(91))
    bids[Decimal("100.5")] = Decimal("3.00")
    update = BookData.model_validate(
        {
            "symbol": "BTC/GBP",
            "bids": [
                {"price": "100.5", "qty": "2.00"},
                {"price": "100.5", "qty": "3.00"},
                {"price": "90", "qty": "1.000"},
            ],
            "checksum": checksum(bids, asks),
        }
    )
    result = book.apply(update, "update", NOW)
    assert len(result.bids) == 10 and result.bids[0].quantity == Decimal("3.00")
    del bids[Decimal("100.5")]
    deletion = BookData.model_validate(
        {
            "symbol": "BTC/GBP",
            "bids": [{"price": "100.5", "qty": "0"}],
            "checksum": checksum(bids, asks),
        }
    )
    assert len(book.apply(deletion, "update", NOW).bids) == 9


def test_crossed_book_and_timestamp_regression_fail_closed() -> None:
    book = KrakenBook("BTC/GBP", uuid4())
    with pytest.raises(ValidationError):
        book.apply(book_data(price="99"), "snapshot", NOW)
    book.apply(book_data(), "snapshot", NOW)
    old = book_data().model_copy(update={"timestamp": NOW - timedelta(seconds=1)})
    with pytest.raises(MarketDataError):
        book.apply(old, "update", NOW)
    assert book.book is None


@settings(max_examples=1000, derandomize=True)
@given(
    st.decimals(
        min_value="0.00000001", max_value="1000000", places=8, allow_nan=False, allow_infinity=False
    )
)
def test_checksum_preserves_wire_precision(quantity: Decimal) -> None:
    bids, asks = {Decimal("100.10"): quantity}, {Decimal("100.20"): quantity}
    value = checksum(bids, asks)
    assert 0 <= value <= 0xFFFFFFFF
    book = KrakenBook("BTC/GBP", uuid4())
    raw = (
        '{"symbol":"BTC/GBP","bids":[{"price":100.10,"qty":'
        + str(quantity)
        + '}],"asks":[{"price":100.20,"qty":'
        + str(quantity)
        + '}],"checksum":'
        + str(value)
        + "}"
    )
    result = book.apply(BookData.model_validate(decode(raw)), "snapshot", NOW)
    assert result.bids[0].quantity == quantity


def test_domain_cannot_admit_float_or_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        Level.model_validate({"schema_version": True, "price": "1", "quantity": "1"})
    with pytest.raises(ValidationError):
        Level(price=1.0, quantity="1")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        OrderBook(
            venue="fake",
            symbol="BTC/GBP",
            bids=(Level(price=Decimal("1"), quantity=Decimal("1")),),
            asks=(Level(price=Decimal("2"), quantity=Decimal("1")),),
            checksum_valid=True,
            event_at=None,
            received_at=datetime(2026, 1, 1),
            epoch=uuid4(),
        )


def test_candle_boundary_and_finalization_guards() -> None:
    fields = dict(
        venue="fake",
        symbol="BTC/GBP",
        interval_minutes=240,
        start=NOW,
        end=NOW + timedelta(hours=4),
        open="100",
        high="110",
        low="90",
        close="101",
        volume="2",
        trade_count=2,
        complete=True,
        quality=(),
        source="fixture",
        observed_at=NOW + timedelta(hours=4, seconds=10),
    )
    candle = Candle.model_validate(fields)
    assert candle.complete
    for changes in (
        {"low": "102"},
        {"start": NOW + timedelta(seconds=1)},
        {"observed_at": NOW + timedelta(hours=4, seconds=9)},
        {"quality": ("gap",)},
    ):
        with pytest.raises(ValidationError):
            Candle.model_validate(fields | changes)

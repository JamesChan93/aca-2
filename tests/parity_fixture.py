"""Independent inputs and literal expected results, shared by parity probes only."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field

from aca.domain.market import InstrumentRules, Level, OrderBook
from aca.domain.trading import Record
from aca.domain.values import FinancialDecimal
from aca.risk.execution import MarketEvidence
from aca.strategies.slow import StrategySignal

D = Decimal
AT = datetime(2020, 1, 1, tzinfo=UTC)
EPOCH = UUID("50c15583-21bf-42e5-a915-bad19bfcfcb9")
FIXTURE = Path(__file__).parent / "fixtures/parity/v1.json"


class Step(Record):
    ms: int = Field(ge=500)
    bid: FinancialDecimal = Field(gt=0)
    depth: FinancialDecimal = Field(gt=0)
    filled: FinancialDecimal = Field(ge=0)
    cash: FinancialDecimal = Field(ge=0)
    fees: FinancialDecimal = Field(ge=0)
    nav: FinancialDecimal = Field(gt=0)
    fill_count: int = Field(ge=1)


class StopCase(Record):
    name: str
    steps: tuple[Step, ...]


class Expected(Record):
    kind: Literal["independent_synthetic_parity_v1"]
    authority: str
    opening_gbp: FinancialDecimal
    entry_quantity: FinancialDecimal
    entry_price: FinancialDecimal
    entry_fee: FinancialDecimal
    entry_cash: FinancialDecimal
    entry_limit: FinancialDecimal
    stop_trigger: FinancialDecimal
    protective_limit: FinancialDecimal
    fee_rate: FinancialDecimal
    cases: tuple[StopCase, ...]


EXPECTED = Expected.model_validate_json(FIXTURE.read_bytes())
DEFAULT_BID, DEFAULT_DEPTH = D(999), D(1)


def market(
    at: datetime, *, bid: Decimal = DEFAULT_BID, depth: Decimal = DEFAULT_DEPTH
) -> MarketEvidence:
    """One top-of-book level; prices and depths are explicit synthetic evidence."""
    rules = InstrumentRules(
        venue="synthetic-parity",
        symbol="BTC/GBP",
        venue_symbol="Bitcoin-Pounds",
        rest_pair="synthetic",
        price_increment=D("0.1"),
        quantity_increment=D("0.00000001"),
        minimum_quantity=D("0.00006"),
        minimum_cost=D("0.43"),
        status="online",
        observed_at=AT,
    )
    book = OrderBook(
        venue=rules.venue,
        symbol=rules.symbol,
        bids=(Level(price=bid, quantity=depth),),
        asks=(Level(price=bid + 1, quantity=depth),),
        checksum_valid=True,
        received_at=at,
        event_at=at,
        epoch=EPOCH,
    )
    return MarketEvidence(book=book, rules=rules)


def decision() -> StrategySignal:
    return StrategySignal(
        strategy="sma200",
        symbol="BTC/GBP",
        decision_at=AT,
        data_watermark=AT - timedelta(seconds=10),
        desired="long",
        stop_distance=D(50),
        expires_at=AT + timedelta(seconds=2),
    )

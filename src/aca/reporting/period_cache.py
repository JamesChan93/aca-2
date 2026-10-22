"""Mergeable, exact Decimal report blocks produced by the full economic audit."""

from decimal import Decimal, InvalidOperation, localcontext
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field

from aca.domain.trading import Asset, Record, Time, fingerprint
from aca.domain.values import FINANCIAL_CONTEXT, FinancialDecimal
from aca.reporting.historical import Period, Totals
from aca.simulation.historical import HistoricalValuation


def exact_ratio(value: object) -> Decimal:
    # Report ratios live in JSON, rather than a NUMERIC(38,18) money column.
    # Preserve all 50 calculation digits; never round a drawdown while caching it.
    if not isinstance(value, str | Decimal) or len(str(value)) > 128:
        raise ValueError("report ratio requires bounded exact decimal text")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("invalid report drawdown ratio") from error
    if not result.is_finite() or not 0 <= result <= 1:
        raise ValueError("invalid report drawdown ratio")
    return result


Ratio = Annotated[Decimal, BeforeValidator(exact_ratio)]


class TotalsRecord(Record):
    contributions: FinancialDecimal
    withdrawals: FinancialDecimal
    fees: FinancialDecimal
    realised: FinancialDecimal
    trades: int = Field(ge=0)
    flows: int = Field(ge=0)
    transferred_unrealised: FinancialDecimal
    fee_assets: dict[Asset, FinancialDecimal]

    @classmethod
    def capture(cls, totals: Totals) -> TotalsRecord:
        return cls(**vars(totals))

    def totals(self) -> Totals:
        return Totals(**self.model_dump(exclude={"schema_version"}))


class PeriodRecord(Record):
    format: Literal["paper-period-v1"] = "paper-period-v1"
    first: HistoricalValuation
    first_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    last: HistoricalValuation
    last_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    totals: TotalsRecord
    # Dates use the same UTC validator as every other financial boundary.
    flows: tuple[tuple[Time, FinancialDecimal], ...]
    issues: tuple[str, ...]
    count: int = Field(ge=1)
    missing: int = Field(ge=0)
    high: FinancialDecimal
    low: FinancialDecimal | None
    drawdown: Ratio

    @classmethod
    def capture(cls, period: Period, last: HistoricalValuation, hashed: str) -> PeriodRecord:
        return cls(
            first=period.start,
            first_hash=str(period.opening["valuation_hash"]),
            last=last,
            last_hash=hashed,
            totals=TotalsRecord.capture(period.totals),
            flows=tuple(period.flows),
            issues=tuple(sorted(period.issues)),
            count=period.count,
            missing=period.missing,
            high=period.high,
            low=period.low,
            drawdown=period.drawdown,
        )

    def period(self) -> Period:
        period = Period(self.first, self.first_hash)
        period.totals = self.totals.totals()
        period.flows = list(self.flows)
        period.issues = set(self.issues)
        period.count, period.missing = self.count, self.missing
        period.high, period.low, period.drawdown = self.high, self.low, self.drawdown
        return period


def add_totals(target: Totals, source: Totals) -> None:
    with localcontext(FINANCIAL_CONTEXT):
        for name in ("contributions", "withdrawals", "fees", "realised", "transferred_unrealised"):
            setattr(target, name, getattr(target, name) + getattr(source, name))
        target.trades += source.trades
        target.flows += source.flows
        for asset, amount in source.fee_assets.items():
            target.fee_assets[asset] = target.fee_assets.get(asset, Decimal(0)) + amount


def merge(target: Period, source: PeriodRecord) -> None:
    """Caller supplies the bridge flow/fill totals before joining contiguous blocks."""
    with localcontext(FINANCIAL_CONTEXT):
        add_totals(target.totals, source.totals.totals())
        target.flows.extend(source.flows)
        target.count += source.count
        target.missing += source.missing
        target.issues.update(source.issues)
        drawdown = source.drawdown
        if target.high and source.low is not None:
            drawdown = max(drawdown, 1 - source.low / target.high)
        target.drawdown = max(target.drawdown, drawdown)
        target.high = max(target.high, source.high)
        if source.low is not None:
            target.low = source.low if target.low is None else min(target.low, source.low)


def record_hash(record: Record) -> str:
    return fingerprint(record.document())

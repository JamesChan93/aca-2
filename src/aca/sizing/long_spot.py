"""Risk-constrained, downward-rounded quantities for one unleveraged episode."""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from aca.domain.market import InstrumentRules
from aca.domain.values import FINANCIAL_CONTEXT, parse_decimal
from aca.operations.config import RiskPolicy


def round_increment(value: Decimal, increment: Decimal, *, up: bool = False) -> Decimal:
    # Arithmetic may have 50 digits before rounding. Only the increment is a stored
    # boundary here; requiring 18 digits on the unrounded intermediate would reject
    # legitimate divisions rather than round them.
    if not isinstance(value, Decimal) or not value.is_finite() or abs(value) >= Decimal("1e20"):
        raise ValueError("rounding requires a finite Decimal intermediate")
    parse_decimal(increment)
    if value < 0 or increment <= 0:
        raise ValueError("invalid rounding inputs")
    with localcontext(FINANCIAL_CONTEXT):
        return (value / increment).to_integral_value(
            rounding=ROUND_CEILING if up else ROUND_FLOOR
        ) * increment


@dataclass(frozen=True)
class SizeResult:
    quantity: Decimal
    planned_loss: Decimal
    reason: str | None


def size_long(
    *,
    equity: Decimal,
    free_cash: Decimal,
    entry: Decimal,
    protective_limit: Decimal,
    entry_fee: Decimal,
    exit_fee: Decimal,
    allowance_per_unit: Decimal,
    liquidity_quantity: Decimal,
    rules: InstrumentRules,
    policy: RiskPolicy,
    existing_market_value: Decimal = Decimal(0),
    existing_asset_value: Decimal = Decimal(0),
    existing_planned_risk: Decimal = Decimal(0),
) -> SizeResult:
    values = (
        equity,
        free_cash,
        entry,
        protective_limit,
        entry_fee,
        exit_fee,
        allowance_per_unit,
        liquidity_quantity,
        existing_market_value,
        existing_asset_value,
        existing_planned_risk,
    )
    for value in values:
        parse_decimal(value)
    if any(value < 0 for value in values) or equity <= 0 or not 0 < protective_limit < entry:
        raise ValueError("invalid sizing economics")
    if entry_fee >= 1 or exit_fee >= 1 or rules.status != "online":
        return SizeResult(Decimal(0), Decimal(0), "rules_or_fees")
    # No pyramiding: a held/pending position cannot obtain another episode budget.
    if existing_asset_value:
        return SizeResult(Decimal(0), Decimal(0), "no_pyramiding")
    with localcontext(FINANCIAL_CONTEXT):
        loss = entry - protective_limit + entry * entry_fee + protective_limit * exit_fee
        loss += allowance_per_unit
        budget = min(
            equity * policy.planned_risk_per_trade,
            equity * policy.aggregate_planned_risk - existing_planned_risk,
        )
        limits = (
            budget / loss,
            (equity * policy.asset_exposure - existing_asset_value) / entry,
            (equity * policy.total_exposure - existing_market_value) / entry,
            free_cash / (entry * (1 + entry_fee)),
            liquidity_quantity,
        )
        quantity = round_increment(max(Decimal(0), min(limits)), rules.quantity_increment)
        if (
            quantity < rules.minimum_quantity
            or quantity * entry < rules.minimum_cost
            or quantity * protective_limit < rules.minimum_cost
        ):
            return SizeResult(Decimal(0), Decimal(0), "below_minimum")
        return SizeResult(quantity, quantity * loss, None)

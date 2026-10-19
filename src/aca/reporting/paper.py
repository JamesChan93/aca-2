"""Performance does not manufacture returns from stale/missing valuations."""

from datetime import datetime
from decimal import Decimal, localcontext

from aca.accounting.ledger import stored
from aca.domain.trading import ZERO, Journal, PortfolioSnapshot
from aca.domain.values import FINANCIAL_CONTEXT
from aca.portfolio.state import episode_risk, free_cash, market_exposure, nav, unit_price


def money_weighted(
    flows: tuple[tuple[datetime, Decimal], ...], terminal: Decimal, at: datetime
) -> Decimal | None:
    """Bounded unique root for normal investor flows only; no forced XIRR."""
    if not flows or any(amount >= 0 for _, amount in flows) or terminal <= 0 or at <= flows[0][0]:
        return None
    with localcontext(FINANCIAL_CONTEXT):
        origin = flows[0][0]
        end = Decimal(str((at - origin).total_seconds())) / Decimal(31557600)
        dated = tuple(
            (Decimal(str((time - origin).total_seconds())) / Decimal(31557600), amount)
            for time, amount in flows
        )

        def value(rate: Decimal) -> Decimal:
            return (
                sum((amount / (1 + rate) ** years for years, amount in dated), ZERO)
                + terminal / (1 + rate) ** end
            )

        low, high = Decimal("-0.9999"), Decimal("1000")
        if value(low) * value(high) >= 0:
            return None
        for _ in range(100):
            mid = (low + high) / 2
            if value(mid) > 0:
                low = mid
            else:
                high = mid
        return stored((low + high) / 2)


def _performance(
    state: PortfolioSnapshot, history: tuple[Journal, ...], at: datetime
) -> dict[str, object]:
    flows = tuple(
        (j.occurred_at, -sum((m.quantity for m in j.movements if m.asset == "GBP"), ZERO))
        for j in history
        if j.kind in {"contribution", "withdrawal"}
    )
    contributions = -sum((amount for _, amount in flows), ZERO)
    result: dict[str, object] = {
        "schema_version": 1,
        "mode": "paper",
        "as_of": at.isoformat(),
        "net_investment_flows_gbp": str(contributions),
        "manual_tax_cash_reserve_gbp": str(state.tax_reserve_gbp),
        "tax_reserve_estimate_gbp": None,
        "live_execution_available": False,
        "synthetic_replay_counts_toward_promotion": False,
        "tax_report_final": False,
    }
    try:
        value, unit = nav(state, at), unit_price(state, at)
        result.update(
            provisional=not state.ready or bool(state.incident),
            reconciliation_issue=state.incident or (None if state.ready else "account_not_ready"),
            nav_gbp=str(value),
            unit_price=str(unit),
            twr_since_inception=str(unit - 1),
            pnl_gbp=str(value - contributions),
            free_cash_gbp=str(free_cash(state)),
            exposure_gbp=str(market_exposure(state, at)),
            reserved_planned_loss_gbp=str(episode_risk(state)),
            drawdown=str(1 - unit / state.high_water),
            inception_drawdown=str(1 - unit / state.inception_high_water),
            fees_gbp=str(
                sum(
                    (
                        line.gbp
                        for j in history
                        for line in j.lines
                        if line.account == "expense:fees"
                    ),
                    ZERO,
                )
            ),
            mwr=str(rate) if (rate := money_weighted(flows, value, at)) is not None else None,
        )
    except ValueError:
        result.update(provisional=True, reason="invalid_gbp_valuation")
    return result


def performance(
    state: PortfolioSnapshot, history: tuple[Journal, ...], at: datetime
) -> dict[str, object]:
    with localcontext(FINANCIAL_CONTEXT):
        return _performance(state, history, at)

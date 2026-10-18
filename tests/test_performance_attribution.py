"""Independent GBP/crypto fee fixtures for historical period attribution."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from aca.accounting.ledger import balances, cash_flow, fill_journal
from aca.domain.trading import Asset, Fee, Fill, KillSwitchState, PortfolioSnapshot, Valuation
from aca.operations.config import RiskPolicy
from aca.portfolio.state import nav
from aca.reporting.historical import Period
from aca.simulation.historical import HistoricalValuation

D = Decimal
AT = datetime(2026, 10, 18, tzinfo=UTC)


@pytest.mark.parametrize(
    "asset,fee,rate,expected_pnl",
    [("GBP", "0.1", "1", "21.9"), ("BTC", "0.001", "100", "21.88"), ("ETH", "0.01", "10", "21.88")],
)
def test_actual_fee_asset_valuation_and_remaining_inventory_mark(
    asset: Asset, fee: str, rate: str, expected_pnl: str
) -> None:
    from aca.domain.trading import fingerprint

    funding = cash_flow("opening", D(1010), AT)
    eth = Fill(
        id="opening-eth",
        client_id=uuid4(),
        symbol="ETH/GBP",
        side="buy",
        quantity=D(1),
        price=D(10),
        occurred_at=AT,
        liquidity="taker",
    )
    opening_eth = fill_journal(eth, balances((funding,)))
    opening = PortfolioSnapshot(
        account_id=uuid4(),
        version=1,
        as_of=AT,
        units=D(1010),
        balances=balances((funding, opening_eth)),
        kill=KillSwitchState(policy_epoch=uuid4()),
        marks=(Valuation(asset="ETH", bid_gbp=D(10), observed_at=AT, source="opening-eth-bid"),),
    )
    start = HistoricalValuation(
        number=0,
        source_hash="0" * 64,
        state=opening,
        control_version=1,
        fill_number=0,
        nav_gbp=nav(opening, AT),
        policy_hash=fingerprint(RiskPolicy().model_dump(mode="json")),
    )
    fill = Fill(
        id="btc-buy",
        client_id=uuid4(),
        symbol="BTC/GBP",
        side="buy",
        quantity=D(1),
        price=D(100),
        occurred_at=AT,
        liquidity="taker",
        fees=(
            Fee(
                asset=asset,
                quantity=D(fee),
                rate_gbp=D(rate),
                valued_at=AT,
                valuation_source="actual-fill-fee",
            ),
        ),
    )
    journal = fill_journal(fill, opening.balances)
    at = AT + timedelta(seconds=1)
    closing = opening.model_copy(
        update={
            "as_of": at,
            "balances": balances((funding, opening_eth, journal)),
            "marks": (
                Valuation(asset="BTC", bid_gbp=D(120), observed_at=at, source="closing-btc-bid"),
                Valuation(asset="ETH", bid_gbp=D(12), observed_at=at, source="closing-eth-bid"),
            ),
        }
    )
    end = start.model_copy(update={"number": 1, "state": closing, "nav_gbp": nav(closing, at)})
    period = Period(start, "0" * 64)
    period.observe(start)
    period.totals.add(journal, fill)
    period.observe(end)
    result = period.finish(end, "f" * 64)
    assert D(result["pnl_gbp"]) == D(expected_pnl)  # type: ignore[arg-type]
    assert D(result["fees_gbp"]) == D("0.1")  # type: ignore[arg-type]
    assert result["fee_quantities"] == {asset: fee}
    assert result["valuation_complete"] is True

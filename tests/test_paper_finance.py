"""Financial invariants, exact thresholds and rejection of unsafe boundaries."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from aca.accounting.ledger import balances, cash_flow, fill_journal, reverse, stored
from aca.domain.trading import (
    Asset,
    Balance,
    Fee,
    Fill,
    Journal,
    KillSwitchState,
    PortfolioSnapshot,
    Valuation,
)
from aca.domain.values import FINANCIAL_CONTEXT
from aca.operations.config import RiskPolicy
from aca.portfolio.state import flow_units, nav, observe_risk, unit_price

D = Decimal
AT = datetime(2026, 10, 18, tzinfo=UTC)


def portfolio(cash: str = "100", btc: str = "0", price: str = "100") -> PortfolioSnapshot:
    return PortfolioSnapshot(
        account_id=uuid4(),
        version=1,
        as_of=AT,
        balances=(
            Balance(asset="GBP", quantity=D(cash), cost_gbp=D(cash)),
            Balance(asset="BTC", quantity=D(btc), cost_gbp=D(0)),
        ),
        units=D(100),
        kill=KillSwitchState(policy_epoch=uuid4()),
        marks=(Valuation(asset="BTC", bid_gbp=D(price), observed_at=AT, source="fixture"),),
        daily_day=AT.date(),
    )


@pytest.mark.parametrize(
    "cash,soft,hard,daily",
    [
        ("98", False, False, True),
        ("95", True, False, True),
        ("90", True, True, True),
        ("95.00001", False, False, True),
    ],
)
def test_exact_thresholds(cash: str, soft: bool, hard: bool, daily: bool) -> None:
    state = observe_risk(portfolio(cash), AT, RiskPolicy())
    assert (state.kill.soft, state.kill.hard, state.kill.daily_day == AT.date()) == (
        soft,
        hard,
        daily,
    )
    # Recovery of prices, a deposit and a restart cannot clear the latched losses.
    restored = PortfolioSnapshot.restore(state.document())
    assert restored.kill == state.kill


@settings(max_examples=1000, derandomize=True, deadline=None)
@given(cash=st.integers(1, 100000), mark=st.integers(1, 10000), contribution=st.integers(1, 100000))
def test_investment_flows_preserve_unit_price(cash: int, mark: int, contribution: int) -> None:
    old = portfolio(str(cash), "1", str(mark))
    amount = D(contribution)
    units = flow_units(old, amount, AT)
    new = old.model_copy(
        update={
            "units": units,
            "balances": (
                Balance(asset="GBP", quantity=D(cash) + amount, cost_gbp=D(cash) + amount),
                old.balances[1],
            ),
        }
    )
    assert abs(unit_price(new, AT) - unit_price(old, AT)) < D("1e-12")
    assert abs(
        (1 - unit_price(new, AT) / old.high_water) - (1 - unit_price(old, AT) / old.high_water)
    ) < D("1e-12")


def test_daily_reset_midnight_and_missing_boundary() -> None:
    state = observe_risk(portfolio("98"), AT, RiskPolicy())
    next_day = AT + timedelta(days=1)
    reset = observe_risk(state, next_day, RiskPolicy())
    assert reset.kill.daily_day is None
    missed = observe_risk(state, next_day + timedelta(seconds=1), RiskPolicy())
    assert missed.kill.daily_day == next_day.date()
    hard = observe_risk(portfolio("90"), AT, RiskPolicy())
    assert observe_risk(hard, next_day, RiskPolicy()).kill.hard


@pytest.mark.parametrize(
    "asset,fee,rate", [("GBP", "0.8", "1"), ("BTC", "0.001", "100"), ("ETH", "0.01", "10")]
)
def test_actual_fee_assets_balance_and_no_double_count(asset: Asset, fee: str, rate: str) -> None:
    initial = cash_flow("opening", D(1000), AT)
    history = [initial]
    # ETH inventory is acquired first so that a third-asset fee is funded.
    eth = Fill(
        id="eth-opening",
        client_id=uuid4(),
        symbol="ETH/GBP",
        side="buy",
        quantity=D(1),
        price=D(10),
        occurred_at=AT,
        liquidity="taker",
    )
    history.append(fill_journal(eth, balances(history)))
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
                valuation_source="actual-fixture",
            ),
        ),
    )
    journal = fill_journal(fill, balances(history))
    assert sum(line.gbp for line in journal.lines) == 0
    assert len([m for m in journal.movements if m.asset == asset and m.quantity < 0]) >= 1
    history.append(journal)
    held = {b.asset: b.quantity for b in balances(history)}
    assert held["BTC"] == D(1) - (D(fee) if asset == "BTC" else 0)
    assert held["GBP"] == D(890) - (D(fee) if asset == "GBP" else 0)
    sell = Fill(
        id="btc-sell",
        client_id=uuid4(),
        symbol="BTC/GBP",
        side="sell",
        quantity=held["BTC"],
        price=D(120),
        occurred_at=AT,
        liquidity="taker",
    )
    sold = fill_journal(sell, balances(history))
    final = balances([*history, sold])
    assert next(b for b in final if b.asset == "BTC").cost_gbp == 0
    assert sum(line.gbp for line in sold.lines) == 0


def test_append_only_reverse_replace_and_unfunded_fee() -> None:
    original = cash_flow("opening", D(100), AT)
    reversal = reverse(original, source="reverse-opening", reason="Correct opening amount", at=AT)
    replacement = cash_flow("correct-opening", D(90), AT)
    assert balances([original, reversal, replacement])[0].quantity == 90
    assert reversal.reverses == original.id
    bad = Fill(
        id="unfunded",
        client_id=uuid4(),
        symbol="BTC/GBP",
        side="buy",
        quantity=D(1),
        price=D(100),
        occurred_at=AT,
        liquidity="taker",
        fees=(
            Fee(
                asset="ETH", quantity=D(1), rate_gbp=D(10), valued_at=AT, valuation_source="fixture"
            ),
        ),
    )
    with pytest.raises(ValueError, match="owned"):
        fill_journal(bad, balances([cash_flow("capital", D(200), AT)]))
    with pytest.raises(ValidationError):
        Journal.restore(
            {
                **original.document(),
                "lines": [
                    {"account": "asset:GBP", "gbp": "1"},
                    {"account": "equity:flows", "gbp": "0"},
                ],
            }
        )


@settings(max_examples=1000, derandomize=True, deadline=None)
@given(q=st.integers(1, 100000), p=st.integers(1, 100000))
def test_generated_trade_journal_rebuilds_balances(q: int, p: int) -> None:
    with localcontext(FINANCIAL_CONTEXT):
        quantity, price = D(q) / 100000, D(p) / 100
        opening = cash_flow("capital", D(1000000), AT)
        buy = Fill(
            id="buy",
            client_id=uuid4(),
            symbol="BTC/GBP",
            side="buy",
            quantity=quantity,
            price=price,
            occurred_at=AT,
            liquidity="taker",
        )
        journal = fill_journal(buy, balances([opening]))
        result = balances([opening, journal])
        assert result[0].quantity == D(1000000) - stored(quantity * price)
        assert result[1].quantity == quantity
        assert sum(line.gbp for line in journal.lines) == 0


def test_missing_valuation_and_financial_float_fail() -> None:
    state = portfolio("10", "1").model_copy(update={"marks": ()})
    with pytest.raises(ValueError, match="valuation"):
        nav(state, AT)
    with pytest.raises(ValidationError):
        Balance.model_validate({"asset": "GBP", "quantity": 1.2, "cost_gbp": "1"})
    with pytest.raises(ValidationError):
        Valuation(asset="BTC", bid_gbp=D(100), source="fixture", observed_at=datetime(2026, 10, 18))


def test_complete_withdrawal_preserves_last_unit_price_and_latches() -> None:
    old = observe_risk(portfolio("90"), AT, RiskPolicy())
    zero = old.model_copy(update={"units": flow_units(old, D(-90), AT), "balances": ()})
    zero = observe_risk(zero, AT, RiskPolicy())
    assert zero.units == 0 and unit_price(zero, AT) == D("0.9")
    assert zero.kill.hard
    new = zero.model_copy(
        update={
            "units": flow_units(zero, D(90), AT),
            "balances": (Balance(asset="GBP", quantity=D(90), cost_gbp=D(90)),),
        }
    )
    assert unit_price(new, AT) == D("0.9") and new.kill.hard


def test_money_weighted_and_stale_performance_are_explicit() -> None:
    from aca.reporting.paper import money_weighted, performance

    rate = money_weighted(((AT, D(-100)),), D(110), AT + timedelta(days=365, hours=6))
    assert rate is not None and abs(rate - D("0.1")) < D("1e-15")
    assert (
        money_weighted(
            ((AT, D(-100)), (AT + timedelta(days=1), D(10))), D(110), AT + timedelta(days=366)
        )
        is None
    )
    assert performance(portfolio("10", "1"), (), AT + timedelta(seconds=6))["provisional"] is True
    unsafe = portfolio().model_copy(update={"ready": False, "incident": "unreconciled_fill"})
    report = performance(unsafe, (), AT)
    assert report["provisional"] is True and report["reconciliation_issue"] == "unreconciled_fill"

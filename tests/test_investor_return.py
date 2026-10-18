"""Independent dated-cash-flow examples, with unavailable ambiguous roots."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aca.reporting.returns import investor_return

AT = datetime(2026, 1, 1, tzinfo=UTC)
YEAR = timedelta(days=365, hours=6)
D = Decimal


@pytest.mark.parametrize(
    "flows,terminal,expected",
    [
        (((AT, D(-100)),), D(110), D("0.1")),
        (((AT, D(-100)),), D(90), D("-0.1")),
        (((AT, D(-100)), (AT + YEAR, D(10))), D(100), D("0.1")),
        (((AT, D(-100)), (AT + YEAR, D(110))), D(0), D("0.1")),
        (((AT, D(-100)), (AT, D(50))), D(55), D("0.1")),
        (((AT, D(-100)), (AT + YEAR / 2, D(-100))), D(264), D("0.44")),
        (((AT, D(-100)), (AT + YEAR / 2, D(12))), D("129.6"), D("0.44")),
    ],
)
def test_known_unique_roots(
    flows: tuple[tuple[datetime, Decimal], ...], terminal: Decimal, expected: Decimal
) -> None:
    rate, reason = investor_return(flows, terminal, AT, AT + YEAR)
    assert reason is None and rate is not None and abs(rate - expected) < D("1e-15")


def test_two_root_cash_flow_and_withdrawal_then_reinvestment_are_unavailable() -> None:
    flows = ((AT, D(-100)), (AT + YEAR, D(230)), (AT + YEAR * 2, D(-132)))
    assert investor_return(flows, D(0), AT, AT + YEAR * 2) == (
        None,
        "uniqueness_not_established",
    )  # Both 10% and 20% solve this example; neither is selected.


def test_zero_duration_root_range_budget_and_invalid_boundaries() -> None:
    assert investor_return((), D(1), AT, AT) == (None, "zero_duration")
    assert investor_return((), D(1), AT, AT + YEAR) == (None, "no_investment_return_root")
    assert investor_return(((AT, D(-1)),), D(1002), AT, AT + YEAR) == (
        None,
        "root_outside_supported_range",
    )
    assert investor_return(((AT, D(-1)),) * 257, D(257), AT, AT + YEAR) == (
        None,
        "flow_budget_exceeded",
    )
    assert investor_return(((AT, D(-100)), (AT + YEAR, D(110))), D(0), AT, AT + YEAR * 101) == (
        None,
        "duration_budget_exceeded",
    )
    with pytest.raises(ValueError, match="outside return period"):
        investor_return(((AT - YEAR, D(-100)),), D(110), AT, AT + YEAR)
    with pytest.raises(ValueError, match="timezone-aware"):
        investor_return((), D(1), datetime(2026, 1, 1), AT + YEAR)
    with pytest.raises(ValueError, match="decimal text"):
        investor_return(((AT, 1.2),), D(110), AT, AT + YEAR)  # type: ignore[arg-type]

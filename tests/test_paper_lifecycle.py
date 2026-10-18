"""Deterministic generated lifecycle/fault traces retain uncertainty and inventory."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from hypothesis import given, settings
from hypothesis import strategies as st

from aca.domain.trading import TERMINAL, Order, OrderIntent, OrderState
from aca.execution.lifecycle import validate_transition

AT = datetime(2026, 10, 18, tzinfo=UTC)
ID = UUID("d3f44174-676f-4e0c-866e-211eb98cc972")
INTENT = OrderIntent(
    id=ID,
    client_id=ID,
    symbol="BTC/GBP",
    purpose="entry",
    side="buy",
    quantity=Decimal(1),
    limit_price=Decimal(100),
    stop_trigger=Decimal(95),
    protective_limit=Decimal(94),
    fee_bound=Decimal("0.008"),
    allowance_per_unit=Decimal("0.2"),
    created_at=AT,
    expires_at=AT + timedelta(seconds=2),
    strategy_version="slow-v1",
    rules_hash="a" * 64,
    episode_id=ID,
    time_in_force="GTD",
)
STATES: tuple[OrderState, ...] = (
    "VALIDATED",
    "SUBMITTING",
    "ACKNOWLEDGED",
    "UNKNOWN",
    "OPEN",
    "PARTIALLY_FILLED",
    "PENDING_CANCEL",
    "CANCELLED",
    "FILLED",
    "EXPIRED",
    "REJECTED",
)


@settings(max_examples=10000, derandomize=True, deadline=None, print_blob=True)
@given(trace=st.lists(st.tuples(st.integers(0, 10), st.integers(0, 12)), min_size=1, max_size=40))
def test_generated_lifecycle_fault_sequences(trace: list[tuple[int, int]]) -> None:
    order = Order(intent=INTENT, approval=None, state="VALIDATED")
    transmissions = 0
    accepted_fill = Decimal(0)
    for index, filled_tenths in trace:
        candidate = order.model_copy(
            update={"state": STATES[index], "filled": Decimal(filled_tenths) / 10}
        )
        try:
            # Validate record arithmetic as well as state transition semantics.
            candidate = Order.restore(candidate.document())
            validate_transition(order, candidate)
        except ValueError:
            continue
        if candidate.state == "SUBMITTING" and order.state != "SUBMITTING":
            transmissions += 1
        assert transmissions <= 1, trace
        assert accepted_fill <= candidate.filled <= INTENT.quantity, trace
        if order.state in TERMINAL:
            assert candidate.state in TERMINAL, trace
        accepted_fill = candidate.filled
        order = candidate


def test_terminal_late_fill_retains_terminal_remainder() -> None:
    old = Order(intent=INTENT, approval=None, state="CANCELLED", filled=Decimal("0.2"))
    late = old.model_copy(update={"filled": Decimal("0.3")})
    validate_transition(old, late)
    assert late.state == "CANCELLED" and late.remaining == Decimal("0.7")

"""Admitted transitions; terminal remainders never reopen on late evidence."""

from aca.domain.trading import TERMINAL, Order

TRANSITIONS: dict[str, frozenset[str]] = {
    "VALIDATED": frozenset({"SUBMITTING", "EXPIRED", "REJECTED"}),
    "SUBMITTING": frozenset({"ACKNOWLEDGED", "UNKNOWN", "REJECTED"}),
    "UNKNOWN": frozenset(
        {"OPEN", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "EXPIRED", "REJECTED"}
    ),
    "ACKNOWLEDGED": frozenset(
        {"OPEN", "PARTIALLY_FILLED", "FILLED", "PENDING_CANCEL", "CANCELLED", "EXPIRED"}
    ),
    "OPEN": frozenset({"PARTIALLY_FILLED", "FILLED", "PENDING_CANCEL", "CANCELLED", "EXPIRED"}),
    "PARTIALLY_FILLED": frozenset({"FILLED", "PENDING_CANCEL", "CANCELLED", "EXPIRED"}),
    "PENDING_CANCEL": frozenset({"UNKNOWN", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "EXPIRED"}),
    "CANCELLED": frozenset({"FILLED"}),
    "EXPIRED": frozenset({"FILLED"}),
    "FILLED": frozenset(),
    "REJECTED": frozenset(),
}


def validate_transition(old: Order, new: Order) -> None:
    if old.intent != new.intent or old.approval != new.approval:
        raise ValueError("order economics/approval are immutable")
    if new.filled < old.filled or new.filled > new.intent.quantity:
        raise ValueError("cumulative fills regressed/exceeded quantity")
    if old.state != new.state and new.state not in TRANSITIONS[old.state]:
        raise ValueError("invalid order lifecycle transition")
    if old.state in TERMINAL and new.state not in TERMINAL:
        raise ValueError("terminal remainder reopened")

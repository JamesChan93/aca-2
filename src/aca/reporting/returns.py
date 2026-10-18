"""Bounded annualised investor return with a sufficient uniqueness test."""

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, localcontext
from itertools import pairwise

from aca.accounting.ledger import stored
from aca.domain.trading import ZERO
from aca.domain.values import FINANCIAL_CONTEXT, parse_decimal, utc_datetime

MAX_RETURN_FLOWS = 256


def investor_return(
    flows: tuple[tuple[datetime, Decimal], ...],
    terminal: Decimal,
    start: datetime,
    end: datetime,
) -> tuple[Decimal | None, str | None]:
    """Negative contributions, positive withdrawals; actual/365.25 in UTC.

    One chronological sign change is sufficient for a unique admissible root.
    Other sign patterns are deliberately unavailable, even if a numerical solver
    could find a root. Amounts at the same instant are combined before this test.
    """
    start, end = utc_datetime(start), utc_datetime(end)
    parse_decimal(terminal)
    if end <= start:
        return None, "zero_duration"
    if terminal < 0:
        raise ValueError("negative terminal NAV")
    if len(flows) > MAX_RETURN_FLOWS:
        return None, "flow_budget_exceeded"
    with localcontext(FINANCIAL_CONTEXT):
        amounts: dict[datetime, Decimal] = defaultdict(lambda: ZERO)
        for at, amount in flows:
            at = utc_datetime(at)
            parse_decimal(amount)
            if not start <= at <= end:
                raise ValueError("investor flow outside return period")
            amounts[at] += amount
        amounts[end] += terminal
        ordered = [(at, amount) for at, amount in sorted(amounts.items()) if amount]
        signs = [amount > 0 for _, amount in ordered]
        if not signs or signs[0]:
            return None, "no_investment_return_root"
        changes = sum(a != b for a, b in pairwise(signs))
        if not changes:
            return None, "no_investment_return_root"
        if changes != 1:
            return None, "uniqueness_not_established"

        def years(at: datetime) -> Decimal:
            delta = at - start
            seconds = Decimal(delta.days * 86400 + delta.seconds)
            return (seconds + Decimal(delta.microseconds) / 1000000) / 31557600

        dated = tuple((years(at), amount) for at, amount in ordered)
        if years(end) > 100:
            return None, "duration_budget_exceeded"

        def value(rate: Decimal) -> Decimal:
            return sum((amount / (1 + rate) ** time for time, amount in dated), ZERO)

        low, high = Decimal("-0.9999"), Decimal("1000")
        if not value(ZERO):
            return stored(ZERO), None
        left, right = value(low), value(high)
        if not left:
            return stored(low), None
        if not right:
            return stored(high), None
        if left * right > 0:
            return None, "root_outside_supported_range"
        for _ in range(100):
            mid = (low + high) / 2
            result = value(mid)
            if not result:
                return stored(mid), None
            if (result > 0) == (left > 0):
                low, left = mid, result
            else:
                high = mid
        return stored((low + high) / 2), None

"""Auditable UTC boundary valuation from a healthy pre-boundary public prefix."""

from decimal import Decimal

from aca.domain.trading import PortfolioSnapshot, Record, Time
from aca.domain.values import FinancialDecimal
from aca.portfolio.state import nav, unit_price


class DayBoundary(Record):
    at: Time
    previous_source: int
    previous_source_hash: str
    previous_valuation_hash: str
    following_source: int
    following_source_hash: str
    context_hash: str
    state: PortfolioSnapshot
    nav_gbp: FinancialDecimal
    unit_price: FinancialDecimal
    incomplete_since: Time | None

    def validate_economics(self) -> None:
        if (
            self.at.hour
            or self.at.minute
            or self.at.second
            or self.at.microsecond
            or self.state.as_of != self.at
            or self.state.orders
            or self.state.daily_day != self.at.date()
            or self.state.daily_unit_price != self.unit_price
            or nav(self.state, self.at) != self.nav_gbp
            or unit_price(self.state, self.at) != self.unit_price
            or self.unit_price <= Decimal(0)
        ):
            raise ValueError("invalid UTC boundary valuation")

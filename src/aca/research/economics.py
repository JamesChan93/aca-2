"""Scenario arithmetic and the fixed-cost hurdle. No forecast or sizing input."""

from dataclasses import dataclass
from decimal import Decimal, localcontext

from aca.domain.values import FINANCIAL_CONTEXT, parse_decimal


@dataclass(frozen=True)
class Projection:
    contributions: Decimal
    gross_performance: Decimal
    variable_costs: Decimal
    modelled_tax: Decimal
    ending_nav: Decimal
    separate_infrastructure: Decimal


def project(
    initial: Decimal,
    monthly: Decimal,
    gross_annual: Decimal,
    variable_annual: Decimal,
    *,
    years: int = 10,
) -> Projection:
    for value in (initial, monthly, gross_annual, variable_annual):
        parse_decimal(value)
    if initial <= 0 or monthly < 0 or variable_annual < 0 or not 1 <= years <= 100:
        raise ValueError("invalid scenario")
    with localcontext(FINANCIAL_CONTEXT):
        nav, gross, costs, tax, yearly_gain = (
            initial,
            Decimal(0),
            Decimal(0),
            Decimal(0),
            Decimal(0),
        )
        for month in range(years * 12):
            gain, cost = nav * gross_annual / 12, nav * variable_annual / 12
            nav += gain - cost + monthly
            gross += gain
            costs += cost
            yearly_gain += gain - cost
            if month % 12 == 11:
                payment = max(Decimal(0), yearly_gain) * Decimal(".24")
                nav -= payment
                tax += payment
                yearly_gain = Decimal(0)
        return Projection(
            initial + monthly * years * 12, gross, costs, tax, nav, Decimal(240) * years
        )


def fixed_cost_gate(average_equity: Decimal, incremental_lower_bound: Decimal) -> dict[str, object]:
    for value in (average_equity, incremental_lower_bound):
        parse_decimal(value)
    if average_equity <= 0:
        raise ValueError("invalid equity")
    with localcontext(FINANCIAL_CONTEXT):
        benefit = average_equity * incremental_lower_bound
        return {
            "passes_arithmetic_hurdle": incremental_lower_bound > 0 and benefit >= 480,
            "annual_fixed_cost_gbp": "240",
            "required_benefit_gbp": "480",
            "estimated_lower_bound_benefit_gbp": str(benefit),
            "execution_permission": False,
        }

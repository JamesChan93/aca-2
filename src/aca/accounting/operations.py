"""Explicit managed-account flows; these records do not classify owner-wide tax."""

from decimal import Decimal, localcontext
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from aca.accounting.ledger import cash_flow, stored
from aca.domain.trading import (
    ZERO,
    Asset,
    Balance,
    Journal,
    JournalLine,
    Movement,
    PortfolioSnapshot,
    Record,
    Time,
    Valuation,
)
from aca.domain.values import FINANCIAL_CONTEXT, FinancialDecimal
from aca.operations.config import RiskPolicy


class ExternalFlow(Record):
    kind: Literal["cash", "transfer"]
    asset: Asset
    quantity: FinancialDecimal
    occurred_at: Time
    valuation: Valuation

    @model_validator(mode="after")
    def semantics(self) -> Self:
        if not self.quantity or self.valuation.asset != self.asset:
            raise ValueError("flow requires a nonzero quantity and matching GBP valuation")
        if self.valuation.observed_at != self.occurred_at:
            raise ValueError("flow GBP valuation must describe the exact event time")
        if self.kind == "cash" and self.asset != "GBP":
            raise ValueError("cash flow must be GBP")
        if not self.amount_gbp:
            raise ValueError("flow is below functional-currency precision")
        return self

    @property
    def amount_gbp(self) -> FinancialDecimal:
        with localcontext(FINANCIAL_CONTEXT):
            return stored(self.quantity * self.valuation.bid_gbp)


class AccountingRequest(Record):
    request_id: UUID
    actor: UUID
    expected_version: int = Field(ge=1)
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=12, max_length=240)

    @field_validator("reason")
    @classmethod
    def plain(cls, value: str) -> str:
        if value != value.strip() or any(not c.isprintable() for c in value):
            raise ValueError("accounting reason must be plain, trimmed text")
        return value


class TransferRequest(AccountingRequest):
    external_reference: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,119}$")
    flow: ExternalFlow

    @model_validator(mode="after")
    def transfer_only(self) -> Self:
        if self.flow.kind != "transfer":
            raise ValueError("transfer request requires transfer classification")
        return self


class CorrectionRequest(AccountingRequest):
    target_journal: UUID
    replacement: ExternalFlow


def investment_amount(journal: Journal) -> FinancialDecimal:
    """Flow value is functional currency, never a sum of mixed asset quantities."""
    with localcontext(FINANCIAL_CONTEXT):
        return stored(
            -sum((line.gbp for line in journal.lines if line.account == "equity:flows"), ZERO)
        )


def flow_from_journal(journal: Journal) -> ExternalFlow:
    if (
        journal.kind not in {"contribution", "withdrawal", "transfer"}
        or len(journal.movements) != 1
    ):
        raise ValueError("only supported external-flow journals can be corrected")
    movement = journal.movements[0]
    valuation = journal.valuation
    if journal.kind != "transfer":
        if movement.asset != "GBP":
            raise ValueError("unsupported cash-flow asset")
        valuation = Valuation(
            asset="GBP", bid_gbp=Decimal(1), observed_at=journal.occurred_at, source="GBP-par"
        )
    if valuation is None:
        raise ValueError("transfer GBP valuation evidence is missing")
    return ExternalFlow(
        kind="transfer" if journal.kind == "transfer" else "cash",
        asset=movement.asset,
        quantity=movement.quantity,
        occurred_at=journal.occurred_at,
        valuation=valuation,
    )


def flow_journal(flow: ExternalFlow, current: tuple[Balance, ...], *, source: str) -> Journal:
    if flow.kind == "cash":
        return cash_flow(source, flow.quantity, flow.occurred_at)
    with localcontext(FINANCIAL_CONTEXT):
        value = flow.amount_gbp
        cost = value
        if flow.quantity < 0:
            balance = next((b for b in current if b.asset == flow.asset), None)
            if balance is None or -flow.quantity > balance.quantity:
                raise ValueError("transfer exceeds owned inventory")
            cost = (
                balance.cost_gbp.copy_negate()
                if flow.quantity.copy_negate() == balance.quantity
                else stored(balance.cost_gbp * flow.quantity / balance.quantity)
            )
        basis = stored(value - cost)
    lines = [
        JournalLine(account=f"asset:{flow.asset}", gbp=cost),
        JournalLine(account="equity:flows", gbp=value.copy_negate()),
    ]
    if basis:
        lines.append(JournalLine(account="equity:transfer_basis", gbp=basis))
    return Journal(
        id=uuid4(),
        source=source,
        kind="transfer",
        occurred_at=flow.occurred_at,
        valuation=flow.valuation,
        lines=tuple(lines),
        movements=(Movement(asset=flow.asset, quantity=flow.quantity),),
        reason="Owner transfer across managed-account boundary; tax classification not inferred",
    )


def flow_marks(state: PortfolioSnapshot, flow: ExternalFlow) -> PortfolioSnapshot:
    if flow.kind != "transfer" or flow.asset == "GBP":
        return state
    return state.model_copy(
        update={"marks": (*(m for m in state.marks if m.asset != flow.asset), flow.valuation)}
    )


def transfer_projection(
    old: PortfolioSnapshot, flow: ExternalFlow, journal: Journal, policy: RiskPolicy
) -> PortfolioSnapshot:
    from aca.accounting.ledger import apply_journals
    from aca.portfolio.state import flow_units, observe_risk

    at = flow.occurred_at
    before = flow_marks(old, flow)
    if old.units:
        before = observe_risk(before, at, policy)
    new = before.model_copy(
        update={
            "balances": apply_journals(old.balances, (journal,)),
            "units": flow_units(before, flow.amount_gbp, at),
            "version": old.version + 1,
            "as_of": at,
            "ready": False,
            "incident": "external_transfer_requires_reconciliation",
        }
    )
    if not old.units and old.daily_day != at.date():
        new = new.model_copy(
            update={"daily_day": at.date(), "daily_unit_price": old.last_unit_price}
        )
    return observe_risk(new, at, policy)

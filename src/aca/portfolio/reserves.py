"""Manual cash earmarks, not tax estimates, liabilities or investment flows."""

from decimal import localcontext
from typing import Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from aca.domain.trading import PortfolioSnapshot, Record, Time
from aca.domain.values import FINANCIAL_CONTEXT, FinancialDecimal
from aca.portfolio.state import reserved_cash


class TaxReserveRequest(Record):
    request_id: UUID
    actor: UUID
    expected_version: int = Field(ge=1)
    amount_gbp: FinancialDecimal = Field(ge=0)
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=12, max_length=240)

    @field_validator("reason")
    @classmethod
    def plain_reason(cls, value: str) -> str:
        if value != value.strip() or any(not c.isprintable() for c in value):
            raise ValueError("reserve reason must be plain, trimmed text")
        return value


class TaxReserveRecord(TaxReserveRequest):
    account_id: UUID
    event_version: int = Field(ge=2)
    previous_gbp: FinancialDecimal = Field(ge=0)
    recorded_at: Time

    @model_validator(mode="after")
    def semantics(self) -> Self:
        if self.event_version != self.expected_version + 1 or self.previous_gbp == self.amount_gbp:
            raise ValueError("reserve decision must bind one changed state version")
        return self

    def request(self) -> TaxReserveRequest:
        return TaxReserveRequest.model_validate(
            self.model_dump(include=set(TaxReserveRequest.model_fields))
        )


def validate_reserve_transition(
    old: PortfolioSnapshot | None, new: PortfolioSnapshot, record: TaxReserveRecord
) -> None:
    if (
        old is None
        or record.account_id != old.account_id
        or record.expected_version != old.version
        or record.event_version != new.version
        or record.recorded_at != new.as_of
        or new.as_of < old.as_of
        or record.previous_gbp != old.tax_reserve_gbp
    ):
        raise ValueError("reserve decision does not match the paper state")
    with localcontext(FINANCIAL_CONTEXT):
        available = old.quantity("GBP") - reserved_cash(old)
    if record.amount_gbp > available:
        raise ValueError("tax cash reserve exceeds uncommitted GBP")
    expected = old.model_copy(
        update={
            "tax_reserve_gbp": record.amount_gbp,
            "version": record.event_version,
            "as_of": record.recorded_at,
        }
    )
    if new != expected:
        raise ValueError("reserve decision changed unrelated portfolio state")

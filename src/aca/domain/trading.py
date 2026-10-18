"""Versioned paper contracts. No exchange credentials or arbitrary request fields."""

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, Field, model_validator

from aca.domain.market import MarketRecord, Symbol
from aca.domain.values import FinancialDecimal, utc_datetime

Asset = Literal["GBP", "BTC", "ETH"]
Time = Annotated[AwareDatetime, AfterValidator(utc_datetime)]
ZERO = Decimal(0)


class Record(MarketRecord):
    @classmethod
    def restore(cls, document: dict[str, object]) -> Self:
        # Strict JSON accepts textual UUID/time representations. Financial floats
        # still fail FinancialDecimal's before-validator.
        return cls.model_validate_json(json.dumps(document))

    def document(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class Balance(Record):
    asset: Asset
    quantity: FinancialDecimal = Field(ge=0)
    cost_gbp: FinancialDecimal = Field(ge=0)


class Valuation(Record):
    asset: Asset
    bid_gbp: FinancialDecimal = Field(gt=0)
    observed_at: Time
    source: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def sterling(self) -> Self:
        if self.asset == "GBP" and self.bid_gbp != 1:
            raise ValueError("GBP valuation must be one")
        return self


class Fee(Record):
    asset: Asset
    quantity: FinancialDecimal = Field(ge=0)
    rate_gbp: FinancialDecimal = Field(gt=0)
    valuation_source: str = Field(min_length=1, max_length=120)
    valued_at: Time

    @model_validator(mode="after")
    def sterling(self) -> Self:
        if self.asset == "GBP" and self.rate_gbp != 1:
            raise ValueError("GBP fee rate must be one")
        return self


class Fill(Record):
    id: str = Field(min_length=1, max_length=100)
    client_id: UUID
    symbol: Symbol
    side: Literal["buy", "sell"]
    quantity: FinancialDecimal = Field(gt=0)
    price: FinancialDecimal = Field(gt=0)
    fees: tuple[Fee, ...] = ()
    occurred_at: Time
    liquidity: Literal["maker", "taker"]


class JournalLine(Record):
    account: str = Field(pattern=r"^(asset:(GBP|BTC|ETH)|equity:flows|pnl:realised|expense:fees)$")
    gbp: FinancialDecimal


class Movement(Record):
    asset: Asset
    quantity: FinancialDecimal


class Journal(Record):
    id: UUID
    source: str = Field(min_length=1, max_length=160)
    kind: Literal["contribution", "withdrawal", "trade", "correction", "transfer"]
    occurred_at: Time
    lines: tuple[JournalLine, ...] = Field(min_length=2)
    movements: tuple[Movement, ...]
    reverses: UUID | None = None
    reason: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def balanced(self) -> Self:
        from decimal import localcontext

        from aca.domain.values import FINANCIAL_CONTEXT

        with localcontext(FINANCIAL_CONTEXT):
            if sum((line.gbp for line in self.lines), ZERO) != 0:
                raise ValueError("journal is not balanced")
        if self.kind == "correction" and self.reverses is None:
            raise ValueError("correction requires an original journal")
        return self


OrderState = Literal[
    "VALIDATED",
    "SUBMITTING",
    "UNKNOWN",
    "ACKNOWLEDGED",
    "OPEN",
    "PARTIALLY_FILLED",
    "PENDING_CANCEL",
    "CANCELLED",
    "FILLED",
    "EXPIRED",
    "REJECTED",
]
TERMINAL = frozenset({"CANCELLED", "FILLED", "EXPIRED", "REJECTED"})
UNCERTAIN = frozenset({"SUBMITTING", "UNKNOWN", "PENDING_CANCEL"})


class OrderIntent(Record):
    id: UUID
    client_id: UUID
    symbol: Symbol
    purpose: Literal["entry", "protection", "exit"]
    side: Literal["buy", "sell"]
    quantity: FinancialDecimal = Field(gt=0)
    limit_price: FinancialDecimal = Field(gt=0)
    stop_trigger: FinancialDecimal = Field(ge=0)
    protective_limit: FinancialDecimal = Field(ge=0)
    fee_bound: FinancialDecimal = Field(ge=0, lt=1)
    allowance_per_unit: FinancialDecimal = Field(ge=0)
    created_at: Time
    expires_at: Time
    strategy_version: Literal["slow-v1", "safety-v1"]
    rules_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    episode_id: UUID
    parent_id: UUID | None = None
    time_in_force: Literal["GTD", "IOC", "FOK", "GTC"]
    operator_reason: str | None = Field(default=None, min_length=12, max_length=240)

    @model_validator(mode="after")
    def semantics(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("invalid order deadline")
        if self.purpose == "entry":
            if (
                self.side != "buy"
                or not 0 < self.protective_limit < self.stop_trigger < self.limit_price
            ):
                raise ValueError("invalid funded long entry/protection")
            if self.time_in_force not in {"GTD", "IOC", "FOK"}:
                raise ValueError("invalid entry lifetime")
        elif self.side != "sell":
            raise ValueError("safety orders can only sell owned spot assets")
        if self.purpose == "protection" and (
            self.parent_id is None or not self.limit_price < self.stop_trigger
        ):
            raise ValueError("protection must link an entry and valid stop limit")
        return self


class RiskDecision(Record):
    id: UUID
    intent_id: UUID
    allowed: bool
    reasons: tuple[str, ...]
    state_version: int = Field(ge=1)
    policy_hash: str
    intent_hash: str
    rules_hash: str
    approved_at: Time
    expires_at: Time
    planned_loss: FinancialDecimal = Field(ge=0)


class Order(Record):
    intent: OrderIntent
    approval: RiskDecision | None
    state: OrderState
    filled: FinancialDecimal = Field(default=ZERO, ge=0)
    venue_id: str | None = None
    triggered: bool = False
    pending_operation: Literal["submit", "cancel"] | None = None
    submitted_at: Time | None = None

    @model_validator(mode="after")
    def cumulative(self) -> Self:
        if self.filled > self.intent.quantity:
            raise ValueError("fill exceeds order quantity")
        if self.state == "FILLED" and self.filled != self.intent.quantity:
            raise ValueError("filled order has an unfilled remainder")
        return self

    @property
    def remaining(self) -> Decimal:
        from decimal import localcontext

        from aca.domain.values import FINANCIAL_CONTEXT

        with localcontext(FINANCIAL_CONTEXT):
            return self.intent.quantity - self.filled


class KillSwitchState(Record):
    policy_epoch: UUID
    soft: bool = False
    hard: bool = False
    daily_day: date | None = None
    reason: str | None = None
    latched_at: Time | None = None
    actor: str = "risk"


class PortfolioSnapshot(Record):
    account_id: UUID
    version: int = Field(ge=1)
    as_of: Time
    balances: tuple[Balance, ...] = ()
    marks: tuple[Valuation, ...] = ()
    units: FinancialDecimal = Field(default=ZERO, ge=0)
    last_unit_price: FinancialDecimal = Field(default=Decimal(1), gt=0)
    high_water: FinancialDecimal = Field(default=Decimal(1), gt=0)
    inception_high_water: FinancialDecimal = Field(default=Decimal(1), gt=0)
    daily_day: date | None = None
    daily_unit_price: FinancialDecimal = Field(default=Decimal(1), gt=0)
    kill: KillSwitchState
    orders: tuple[Order, ...] = ()
    reconciled_at: Time | None = None
    account_evidence_at: Time | None = None
    ready: bool = False
    tax_reserve_gbp: FinancialDecimal = Field(default=ZERO, ge=0)
    incident: str | None = None

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({b.asset for b in self.balances}) != len(self.balances):
            raise ValueError("duplicate balance")
        if len({m.asset for m in self.marks}) != len(self.marks):
            raise ValueError("duplicate valuation")
        if len({o.intent.client_id for o in self.orders}) != len(self.orders):
            raise ValueError("duplicate permanent client id")
        return self

    def quantity(self, asset: Asset) -> Decimal:
        return next((b.quantity for b in self.balances if b.asset == asset), ZERO)

    def cost(self, asset: Asset) -> Decimal:
        return next((b.cost_gbp for b in self.balances if b.asset == asset), ZERO)

    def order(self, client_id: UUID) -> Order:
        for order in self.orders:
            if order.intent.client_id == client_id:
                return order
        raise ValueError("unknown paper order")


class ReconciliationResult(Record):
    complete: bool
    differences: tuple[str, ...]
    observed_at: Time
    orders_checked: int = Field(ge=0)
    fills_checked: int = Field(ge=0)


class VenueOrder(Record):
    intent: OrderIntent
    venue_id: str
    state: OrderState
    filled: FinancialDecimal = Field(default=ZERO, ge=0)
    triggered: bool = False
    queue_ahead: FinancialDecimal = Field(default=ZERO, ge=0)
    queue_initialized: bool = False
    queue_epoch: UUID | None = None
    accepted_at: Time
    triggered_at: Time | None = None
    cancelled_at: Time | None = None


class VenueSnapshot(Record):
    orders: tuple[VenueOrder, ...]
    fills: tuple[Fill, ...]
    complete: bool
    observed_at: Time


def replace_order(state: PortfolioSnapshot, order: Order) -> PortfolioSnapshot:
    orders = tuple(o for o in state.orders if o.intent.client_id != order.intent.client_id)
    return state.model_copy(update={"orders": (*orders, order)})


def fingerprint(document: object) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()

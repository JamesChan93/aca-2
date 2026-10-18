"""A venue-neutral execution port, implemented only by the local paper venue."""

from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from aca.domain.trading import OrderIntent, VenueOrder, VenueSnapshot


class ExecutionPort(Protocol):
    async def submit(self, intent: OrderIntent, at: datetime) -> VenueOrder: ...
    async def cancel(self, client_id: UUID, at: datetime) -> VenueOrder: ...
    async def snapshot(self, at: datetime) -> VenueSnapshot: ...


class UnknownOutcome(RuntimeError):
    """A command may have succeeded. Never retry the original mutation."""


class DefinitiveRejection(RuntimeError):
    """Positive venue rejection evidence, distinct from a failed transport."""

    def __init__(self, code: Literal["expired", "invalid_rules", "post_only"]):
        if code not in {"expired", "invalid_rules", "post_only"}:
            raise ValueError("unclassified rejection must remain uncertain")
        super().__init__(code)
        self.code = code

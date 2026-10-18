"""Bounded field deltas, with explicit hashes of the resulting typed state."""

import json
from uuid import UUID

from aca.domain.trading import PortfolioSnapshot, fingerprint

CHECKPOINT_INTERVAL = 128
MAX_ORDERS = 1024
MAX_STATE_BYTES = 2_000_000
DELTA_FORMAT = "paper_delta_v1"


def state_document(state: PortfolioSnapshot) -> dict[str, object]:
    document = state.document()
    if len(state.orders) > MAX_ORDERS or len(json.dumps(document).encode()) > MAX_STATE_BYTES:
        raise ValueError("paper projection capacity exhausted; archive review required")
    return document


def delta(
    previous: PortfolioSnapshot | None, state: PortfolioSnapshot, journal_id: UUID | None
) -> dict[str, object]:
    before = previous.document() if previous else {}
    after = state_document(state)
    return {
        "format": DELTA_FORMAT,
        "state_hash": fingerprint(after),
        "changes": {key: value for key, value in after.items() if before.get(key) != value},
        "journal_id": str(journal_id) if journal_id else None,
    }


def restore_event(
    previous: PortfolioSnapshot | None, document: dict[str, object]
) -> PortfolioSnapshot:
    if "format" not in document:
        # Immutable 0004 history remains readable; never rewrite its evidence.
        state = PortfolioSnapshot.restore(document)
    else:
        if set(document) != {"format", "state_hash", "changes", "journal_id"}:
            raise ValueError("unknown paper delta fields")
        changes = document["changes"]
        if document["format"] != DELTA_FORMAT or not isinstance(changes, dict):
            raise ValueError("unsupported paper event format")
        if set(changes) - set(PortfolioSnapshot.model_fields):
            raise ValueError("unknown paper state field")
        combined = previous.document() if previous else {}
        combined.update(changes)
        state = PortfolioSnapshot.restore(combined)
        if fingerprint(state.document()) != document["state_hash"]:
            raise ValueError("paper delta state hash mismatch")
        ident = document["journal_id"]
        if ident is not None and (not isinstance(ident, str) or str(UUID(ident)) != ident):
            raise ValueError("invalid journal link")
    state_document(state)
    return state

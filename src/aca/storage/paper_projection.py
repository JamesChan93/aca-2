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
    previous: PortfolioSnapshot | None,
    state: PortfolioSnapshot,
    journal_id: UUID | None,
    additional_journals: tuple[UUID, ...] = (),
) -> dict[str, object]:
    before = previous.document() if previous else {}
    after = state_document(state)
    result: dict[str, object] = {
        "format": "paper_delta_v2" if additional_journals else DELTA_FORMAT,
        "state_hash": fingerprint(after),
        "changes": {key: value for key, value in after.items() if before.get(key) != value},
        "journal_id": str(journal_id) if journal_id else None,
    }
    if additional_journals:
        if journal_id is None:
            raise ValueError("journal batch requires its first journal")
        result["journal_ids"] = [str(journal_id), *(str(i) for i in additional_journals)]
    return result


def journal_links(document: dict[str, object]) -> tuple[UUID, ...]:
    if document.get("format") == "paper_delta_v2":
        ids = document.get("journal_ids")
        if not isinstance(ids, list) or len(ids) != 2 or ids[0] != document.get("journal_id"):
            raise ValueError("invalid reversal/replacement journal batch")
        result = tuple(UUID(str(i)) for i in ids)
        if len(set(result)) != 2 or [str(i) for i in result] != ids:
            raise ValueError("invalid journal batch identities")
        return result
    ident = document.get("journal_id")
    return (UUID(str(ident)),) if ident is not None else ()


def restore_event(
    previous: PortfolioSnapshot | None, document: dict[str, object]
) -> PortfolioSnapshot:
    if "format" not in document:
        # Immutable 0004 history remains readable; never rewrite its evidence.
        state = PortfolioSnapshot.restore(document)
    else:
        extra = {"journal_ids"} if document.get("format") == "paper_delta_v2" else set()
        if set(document) != {"format", "state_hash", "changes", "journal_id"} | extra:
            raise ValueError("unknown paper delta fields")
        changes = document["changes"]
        if document["format"] not in {DELTA_FORMAT, "paper_delta_v2"} or not isinstance(
            changes, dict
        ):
            raise ValueError("unsupported paper event format")
        if set(changes) - set(PortfolioSnapshot.model_fields):
            raise ValueError("unknown paper state field")
        combined = previous.document() if previous else {}
        combined.update(changes)
        state = PortfolioSnapshot.restore(combined)
        # Render/size-check the validated state once; its exact same document
        # supplies the hash. Do not serialize the whole portfolio twice.
        if fingerprint(state_document(state)) != document["state_hash"]:
            raise ValueError("paper delta state hash mismatch")
        ident = document["journal_id"]
        if ident is not None and (not isinstance(ident, str) or str(UUID(ident)) != ident):
            raise ValueError("invalid journal link")
        journal_links(document)
    if "format" not in document:
        state_document(state)
    return state

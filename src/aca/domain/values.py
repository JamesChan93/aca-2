"""Financial inputs never pass through binary floating point."""

from datetime import UTC, datetime
from decimal import Context, Decimal, InvalidOperation
from functools import lru_cache
from typing import Annotated

from pydantic import BeforeValidator, ValidationInfo

FINANCIAL_CONTEXT = Context(prec=50)


def parse_decimal(value: object) -> Decimal:
    """Accept exact text/Decimal inputs only, within NUMERIC(38,18)'s exact range."""
    if not isinstance(value, str | Decimal):
        raise ValueError("financial values require decimal text or Decimal")
    # Only short, exact strings are cache keys. Decimal equality ignores scale,
    # while the returned representation must preserve it; integers/floats/bools
    # must never reach a cache that could equate them with an admitted value.
    if type(value) is str and len(value) <= 128:
        return _short_decimal(value)
    return _checked_decimal(value)


@lru_cache(maxsize=1024)
def _short_decimal(value: str) -> Decimal:
    return _checked_decimal(value)


def _checked_decimal(value: str | Decimal) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal value") from exc
    if not result.is_finite():
        raise ValueError("financial values must be finite")
    # NUMERIC(38,18) permits at most 20 integer digits and no nonzero
    # coefficient digits below 1e-18. Inspect the exact representation; repeated
    # context creation/quantization during prefix audits is unnecessary. Trailing
    # fractional zeroes (including zero with a very small exponent) remain valid.
    representation = result.as_tuple()
    exponent = representation.exponent
    if not isinstance(exponent, int):  # finite Decimals always have an integer exponent.
        raise ValueError("financial values must be finite")
    if (result and result.adjusted() >= 20) or (
        exponent < -18 and any(representation.digits[-(-18 - exponent) :])
    ):
        raise ValueError("financial value exceeds exact NUMERIC(38,18) storage")
    return result


def utc_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps require a timezone-aware datetime")
    return value.astimezone(UTC)


FinancialDecimal = Annotated[Decimal, BeforeValidator(parse_decimal)]


def _utc_boundary(value: object, info: ValidationInfo) -> datetime:
    if info.mode == "json" and isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("invalid ISO timestamp") from exc
    return utc_datetime(value)


UtcDateTime = Annotated[datetime, BeforeValidator(_utc_boundary)]

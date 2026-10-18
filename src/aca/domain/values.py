"""Financial inputs never pass through binary floating point."""

from datetime import UTC, datetime
from decimal import Context, Decimal, InvalidOperation, localcontext
from typing import Annotated

from pydantic import BeforeValidator, ValidationInfo

FINANCIAL_CONTEXT = Context(prec=50)


def parse_decimal(value: object) -> Decimal:
    """Accept exact text/Decimal inputs only, within NUMERIC(38,18)'s exact range."""
    if not isinstance(value, str | Decimal):
        raise ValueError("financial values require decimal text or Decimal")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal value") from exc
    if not result.is_finite():
        raise ValueError("financial values must be finite")
    with localcontext(FINANCIAL_CONTEXT):
        if abs(result) >= Decimal("1e20") or result != result.quantize(Decimal("1e-18")):
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

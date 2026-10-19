"""Bounded outbound webhook transport; never accepts exchange commands."""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import monotonic

import httpx
from pydantic import SecretStr

from aca.domain.values import utc_datetime
from aca.observability.logging import register_secret
from aca.operations.secrets import read_secret

_URL = re.compile(r"https://discord\.com/api/(?:v10/)?webhooks/[0-9]{17,20}/[A-Za-z0-9_-]{60,128}")


@dataclass(frozen=True, repr=False)
class Webhook:
    url: SecretStr
    fingerprint: str

    @classmethod
    def load(cls, path: Path) -> Webhook:
        return cls.parse(read_secret(path))

    @classmethod
    def parse(cls, secret: SecretStr) -> Webhook:
        value = secret.get_secret_value()
        if not _URL.fullmatch(value):
            raise ValueError("only a standard Discord HTTPS webhook is supported")
        register_secret(value)
        register_secret(value.rsplit("/", 1)[-1])
        return cls(secret, hashlib.sha256(value.encode()).hexdigest())

    def __repr__(self) -> str:
        return "Webhook([REDACTED])"


@dataclass(frozen=True)
class DeliveryResult:
    outcome: str
    reason: str
    delay: timedelta = timedelta(seconds=1)
    status: int | None = None
    message_id: str | None = None


def _seconds(value: object) -> timedelta | None:
    if not isinstance(value, str | int | Decimal) or isinstance(value, bool):
        return None
    try:
        number = Decimal(value)
        if not number.is_finite() or number < 0 or number > 604800:
            return None
        micros = int((number * 1000000).to_integral_value(rounding=ROUND_CEILING))
        return timedelta(microseconds=micros)
    except InvalidOperation, ValueError:
        return None


def _deadline(value: str | None, at: datetime) -> timedelta | None:
    if value is None:
        return None
    numeric = _seconds(value)
    if numeric is not None:
        return numeric
    try:
        deadline = utc_datetime(parsedate_to_datetime(value))
        return _seconds(str(max(0, (deadline - at).total_seconds())))
    except ValueError, TypeError, OverflowError:
        return None


def classify(status: int, headers: httpx.Headers, body: bytes, at: datetime) -> DeliveryResult:
    """Only fixed reason codes and a validated message ID escape the transport."""
    document: object = None
    try:
        document = json.loads(body, parse_float=Decimal)
    except ValueError, UnicodeError, RecursionError:
        pass
    delays = [timedelta(seconds=1)]
    retry_header = headers.get("Retry-After")
    if retry_header is not None:
        delay = _deadline(retry_header, at)
        if delay is None:
            return DeliveryResult("disabled", "unsupported_retry_deadline", status=status)
        delays.append(delay)
    if headers.get("X-RateLimit-Remaining") == "0":
        reset = _seconds(headers.get("X-RateLimit-Reset-After"))
        if reset is None:
            return DeliveryResult("disabled", "unsupported_reset_deadline", status=status)
        delays.append(reset)
    if status == 429:
        if isinstance(document, dict) and "retry_after" in document:
            retry = _seconds(document["retry_after"])
            if retry is None:
                return DeliveryResult("disabled", "unsupported_retry_deadline", status=status)
            delays.append(retry)
        if len(delays) == 1:
            delays.append(timedelta(seconds=60))
        return DeliveryResult("retry", "rate_limited", max(delays), status)
    if status == 200 and isinstance(document, dict):
        ident = document.get("id")
        if isinstance(ident, str) and re.fullmatch(r"[0-9]{17,20}", ident):
            return DeliveryResult("delivered", "confirmed", max(delays), status, ident)
    if status in {401, 403, 404} or 300 <= status < 400:
        return DeliveryResult("disabled", "destination_rejected", status=status)
    if 400 <= status < 500 and status not in {408, 429}:
        return DeliveryResult("abandoned", "message_rejected", status=status)
    return DeliveryResult("retry", "unconfirmed_response", max(delays), status)


class DiscordSender:
    def __init__(
        self, webhook: Webhook, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.webhook = webhook
        self.client = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(retries=0, trust_env=False),
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(5),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            headers={"Accept-Encoding": "identity", "User-Agent": "aca-paper-notifications"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def send(self, payload: dict[str, object], at: datetime) -> DeliveryResult:
        began = monotonic()
        try:
            async with asyncio.timeout(10):
                async with self.client.stream(
                    "POST",
                    self.webhook.url.get_secret_value(),
                    params={"wait": "true"},
                    json=payload,
                ) as response:
                    if response.status_code in {401, 403, 404} or 300 <= response.status_code < 400:
                        return classify(response.status_code, response.headers, b"", at)
                    body = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=16385):
                        body.extend(chunk)
                        if len(body) > 16384:
                            return DeliveryResult(
                                "disabled", "response_budget_exceeded", status=response.status_code
                            )
                    received_at = at + timedelta(seconds=max(0, monotonic() - began))
                    return classify(
                        response.status_code, response.headers, bytes(body), received_at
                    )
        except httpx.HTTPError, TimeoutError, OSError:
            # Never persist response bodies, headers, URLs or exception messages.
            return DeliveryResult("retry", "transport_uncertain")

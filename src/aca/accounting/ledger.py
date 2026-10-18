"""Pure double-entry journal construction; actual fee assets are never inferred."""

from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from uuid import uuid4

from aca.domain.trading import ZERO, Asset, Balance, Fill, Journal, JournalLine, Movement
from aca.domain.values import FINANCIAL_CONTEXT, parse_decimal


def stored(value: Decimal) -> Decimal:
    """Explicit GBP functional-currency rounding; never rely on database rounding."""
    with localcontext(FINANCIAL_CONTEXT):
        result = value.quantize(Decimal("1e-18"), rounding=ROUND_HALF_EVEN)
    return parse_decimal(result)


def apply_journals(current: Sequence[Balance], journals: Sequence[Journal]) -> tuple[Balance, ...]:
    """Increment an already verified projection without rereading its prefix."""
    if len({b.asset for b in current}) != len(current):
        raise ValueError("duplicate opening balance")
    quantities: dict[Asset, Decimal] = {
        a: next((b.quantity for b in current if b.asset == a), ZERO) for a in ("GBP", "BTC", "ETH")
    }
    costs: dict[Asset, Decimal] = {
        a: next((b.cost_gbp for b in current if b.asset == a), ZERO) for a in quantities
    }
    with localcontext(FINANCIAL_CONTEXT):
        for journal in journals:
            for movement in journal.movements:
                quantities[movement.asset] += movement.quantity
            for line in journal.lines:
                for asset in quantities:
                    if line.account == f"asset:{asset}":
                        costs[asset] += line.gbp
        if any(q < 0 for q in quantities.values()) or any(c < 0 for c in costs.values()):
            raise ValueError("journal history produces negative owned assets/cost")
        if quantities["GBP"] != costs["GBP"]:
            raise ValueError("GBP quantity/functional currency discrepancy")
        return tuple(
            Balance(
                asset=a, quantity=parse_decimal(quantities[a]), cost_gbp=parse_decimal(costs[a])
            )
            for a in quantities
        )


def balances(journals: Sequence[Journal]) -> tuple[Balance, ...]:
    return apply_journals((), journals)


def cash_flow(source: str, amount: Decimal, at: datetime) -> Journal:
    parse_decimal(amount)
    if amount == 0:
        raise ValueError("cash flow must be nonzero")
    return Journal(
        id=uuid4(),
        source=source,
        kind="contribution" if amount > 0 else "withdrawal",
        occurred_at=at,
        reason="Owner investment flow; excluded from trading P&L",
        lines=(
            JournalLine(account="asset:GBP", gbp=amount),
            JournalLine(account="equity:flows", gbp=amount.copy_negate()),
        ),
        movements=(Movement(asset="GBP", quantity=amount),),
    )


def fill_journal(fill: Fill, current: Sequence[Balance]) -> Journal:
    quantities = {b.asset: b.quantity for b in current}
    costs = {b.asset: b.cost_gbp for b in current}
    lines: list[JournalLine] = []
    movements: list[Movement] = []
    asset: Asset = "BTC" if fill.symbol == "BTC/GBP" else "ETH"

    def line(name: str, gbp: Decimal) -> None:
        lines.append(JournalLine(account=name, gbp=stored(gbp)))

    def move(a: Asset, q: Decimal) -> None:
        quantities[a] = quantities.get(a, ZERO) + q
        if quantities[a] < 0:
            raise ValueError("fill/fee exceeds owned asset")
        movements.append(Movement(asset=a, quantity=parse_decimal(q)))

    def remove_cost(a: Asset, q: Decimal, old_quantity: Decimal) -> Decimal:
        if old_quantity <= 0 or q > old_quantity:
            raise ValueError("disposal exceeds inventory")
        result = (
            costs.get(a, ZERO)
            if q == old_quantity
            else stored(costs.get(a, ZERO) * q / old_quantity)
        )
        costs[a] = costs.get(a, ZERO) - result
        return result

    with localcontext(FINANCIAL_CONTEXT):
        notional = stored(fill.quantity * fill.price)
        if fill.side == "buy":
            move("GBP", -notional)
            move(asset, fill.quantity)
            costs[asset] = costs.get(asset, ZERO) + notional
            line(f"asset:{asset}", notional)
            line("asset:GBP", -notional)
        else:
            original = quantities.get(asset, ZERO)
            move(asset, -fill.quantity)
            move("GBP", notional)
            removed = remove_cost(asset, fill.quantity, original)
            line("asset:GBP", notional)
            line(f"asset:{asset}", -removed)
            line("pnl:realised", removed - notional)
        for fee in fill.fees:
            if fee.valued_at != fill.occurred_at:
                raise ValueError("fee GBP valuation must describe the tax/accounting point")
            old_quantity = quantities.get(fee.asset, ZERO)
            if not fee.quantity:
                continue
            gbp = stored(fee.quantity * fee.rate_gbp)
            move(fee.asset, -fee.quantity)
            removed = (
                fee.quantity
                if fee.asset == "GBP"
                else remove_cost(fee.asset, fee.quantity, old_quantity)
            )
            line("expense:fees", gbp)
            line(f"asset:{fee.asset}", -removed)
            if removed != gbp:
                line("pnl:realised", removed - gbp)
    return Journal(
        id=uuid4(),
        source=f"fill:{fill.id}",
        kind="trade",
        occurred_at=fill.occurred_at,
        lines=tuple(lines),
        movements=tuple(movements),
        reason="Authoritative local paper fill",
    )


def reverse(journal: Journal, *, source: str, reason: str, at: datetime) -> Journal:
    return Journal(
        id=uuid4(),
        source=source,
        kind="correction",
        occurred_at=at,
        reverses=journal.id,
        reason=reason,
        lines=tuple(
            JournalLine(account=line.account, gbp=line.gbp.copy_negate()) for line in journal.lines
        ),
        movements=tuple(
            Movement(asset=m.asset, quantity=m.quantity.copy_negate()) for m in journal.movements
        ),
    )

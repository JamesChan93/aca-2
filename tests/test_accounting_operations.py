"""C2 operator accounting, independent GBP fixtures, invalidation and restart."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_paper_execution import filled_position, market, signal
from test_stored_replay import Feed

from aca.accounting.ledger import balances, cash_flow, stored
from aca.accounting.operations import (
    CorrectionRequest,
    ExternalFlow,
    TransferRequest,
    flow_journal,
    investment_amount,
)
from aca.domain.trading import Asset, Journal, Valuation
from aca.domain.values import FINANCIAL_CONTEXT
from aca.execution.accounting import correct_accounting, external_transfer
from aca.execution.paper import PaperExecution, SimulatedCrash
from aca.observability.notices import daily_summary, notice_payload
from aca.operations import paper as cli
from aca.operations.config import Settings
from aca.portfolio.state import nav, unit_price
from aca.reporting.historical import historical_performance
from aca.reporting.paper import performance
from aca.simulation.paper import PaperExchange
from aca.simulation.stored_replay import StoredReplay
from aca.storage.accounting import accounting_status, operations
from aca.storage.paper import PaperWriter, events

pytest_plugins = ("test_paper_execution", "test_stored_replay")
D = Decimal
AT = datetime(2026, 10, 18, tzinfo=UTC)


def flow(
    asset: Asset = "GBP",
    quantity: str = "100",
    rate: str = "1",
    at: datetime = AT,
    *,
    kind: str = "transfer",
) -> ExternalFlow:
    return ExternalFlow.restore(
        {
            "kind": kind,
            "asset": asset,
            "quantity": quantity,
            "occurred_at": at.isoformat(),
            "valuation": {
                "asset": asset,
                "bid_gbp": rate,
                "observed_at": at.isoformat(),
                "source": "C2-independent-fixture",
            },
        }
    )


async def transfer(
    service: PaperExecution, f: ExternalFlow, **extra: Any
) -> tuple[TransferRequest, dict[str, Any]]:
    request = TransferRequest(
        request_id=uuid4(),
        actor=uuid4(),
        expected_version=(await service._state()).version,
        external_reference=str(uuid4()),
        flow=f,
        evidence_sha256="c" * 64,
        reason="Owner transfer fixture evidence",
        **extra,
    )
    return request, await external_transfer(service, request)


async def correction(
    service: PaperExecution, target: UUID, replacement: ExternalFlow, *, at: datetime = AT
) -> tuple[CorrectionRequest, dict[str, Any]]:
    r = CorrectionRequest(
        request_id=uuid4(),
        actor=uuid4(),
        expected_version=(await service._state()).version,
        target_journal=target,
        replacement=replacement,
        evidence_sha256="d" * 64,
        reason="Correct erroneous owner investment record",
    )
    return r, await correct_accounting(service, r, at=at)


@pytest.mark.parametrize("asset,rate", [("GBP", "1"), ("BTC", "100"), ("ETH", "20")])
async def test_transfer_in_out_independent_units_balances_journal_and_restart(
    paper: tuple[PaperExecution, PaperExchange],
    asset: Asset,
    rate: str,
) -> None:
    service, _ = paper
    before = await service._state()
    _, incoming = await transfer(service, flow(asset, "2", rate))
    state = await service._state()
    value = D(2) * D(rate)
    assert nav(state, AT) == D(1000) + value
    assert state.units == D(1000) + value and unit_price(state, AT) == 1
    assert state.quantity(asset) == before.quantity(asset) + 2
    assert state.cost(asset) == before.cost(asset) + value
    _, outgoing = await transfer(service, flow(asset, "-1", rate))
    state = await service._state()
    assert nav(state, AT) == state.units == D(1000) + D(rate)
    assert unit_price(state, AT) == 1 and state.kill == before.kill
    assert (
        incoming["record"]["tax_classification"]
        == outgoing["record"]["tax_classification"]
        == "not_determined"
    )
    async with service.writer.transaction() as c:
        history = await service.store.history(c)
        assert state.balances == balances(history) == await service.store.ledger_balances(c)
        assert all(sum(line.gbp for line in j.lines) == 0 for j in history)
        assert await service.store.audit(c) == state
    result = performance(state, history, AT)
    assert D(str(result["pnl_gbp"])) == D(str(result["twr_since_inception"])) == 0
    assert D(str(result["net_investment_flows_gbp"])) == D(1000) + D(rate)
    await service.writer.__aexit__()
    async with PaperWriter(service.store) as owner:
        replacement = PaperExecution(owner, PaperExchange(service.store))
        await replacement.recover(AT)
        restored = await replacement._state()
        assert restored.balances == state.balances and restored.units == state.units
        assert not restored.accounting_invalidated


async def test_outgoing_crypto_carries_basis_without_a_trade_or_tax_disposal(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, _ = paper
    await transfer(service, flow("BTC", "1", "100"))
    _, outbound_record = await transfer(service, flow("BTC", "-0.5", "200"))
    state = await service._state()
    # Before the outbound flow NAV=1200 and units=1100. Redeem 100/price units.
    with localcontext(FINANCIAL_CONTEXT):
        expected_units = stored(D(1100) - D(100) / (D(1200) / D(1100)))
    assert abs(state.units - expected_units) < D("1e-15")
    assert state.quantity("BTC") == D("0.5") and state.cost("BTC") == 50
    async with service.writer.transaction() as c:
        history = await service.store.history(c)
    outbound = Journal.restore(outbound_record["record"]["postings"][0])
    assert investment_amount(outbound) == -100
    assert (
        next(line.gbp for line in outbound.lines if line.account == "equity:transfer_basis") == -50
    )
    assert not any(line.account == "pnl:realised" for line in outbound.lines)
    report = performance(state, history, AT)
    assert D(str(report["pnl_gbp"])) == 100
    assert abs(D(str(report["twr_since_inception"])) - D(1) / D(11)) < D("1e-15")


async def test_duplicate_and_conflicting_transfer_reference_and_stale_version(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, _ = paper
    request, _ = await transfer(service, flow())
    state = await service._state()
    assert (await external_transfer(service, request))["inserted"] is False
    assert await service._state() == state
    for r in (
        request.model_copy(update={"request_id": uuid4()}),
        request.model_copy(update={"flow": flow(quantity="101")}),
    ):
        with pytest.raises(ValueError, match="conflicting"):
            await external_transfer(service, r)
    with pytest.raises(ValueError, match="current version"):
        await external_transfer(
            service, request.model_copy(update={"request_id": uuid4(), "external_reference": "new"})
        )
    assert await service._state() == state


@pytest.mark.parametrize("case", ["missing", "float", "time", "asset", "zero", "gbp-rate"])
def test_transfer_requires_exact_event_time_valuation(case: str) -> None:
    raw = flow().document()
    if case == "missing":
        del raw["valuation"]
    elif case == "float":
        raw["quantity"] = 0.1
    elif case == "zero":
        raw["quantity"] = "0"
    else:
        valuation = raw["valuation"]
        assert isinstance(valuation, dict)
        valuation[{"time": "observed_at", "asset": "asset", "gbp-rate": "bid_gbp"}[case]] = {
            "time": (AT - timedelta(seconds=1)).isoformat(),
            "asset": "BTC",
            "gbp-rate": "2",
        }[case]
    with pytest.raises(ValidationError):
        ExternalFlow.restore(raw)


async def test_transfer_insufficient_and_reserved_cash_inventory(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper
    before = await service._state()
    for f in (flow(quantity="-1001"), flow("BTC", "-1", "100")):
        with pytest.raises(ValueError):
            await transfer(service, f)
        assert await service._state() == before
    await service.queue(signal(), market(), AT)
    reserved = await service._state()
    with pytest.raises(ValueError, match="pending or reserved entry"):
        await transfer(service, flow(quantity="-1"))
    with pytest.raises(ValueError, match="reserved"):
        await transfer(service, flow(quantity="-1000"))
    assert await service._state() == reserved
    await service.recover(AT)
    await filled_position(service, venue)
    state = await service._state()
    with pytest.raises(ValueError, match="reserved inventory"):
        await transfer(
            service, flow("BTC", str(state.quantity("BTC").copy_negate()), "999", state.as_of)
        )
    assert await service._state() == state


async def test_transfer_uses_pre_flow_loss_and_retains_hard_latch(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, _ = paper
    await transfer(service, flow("BTC", "1", "100"))
    await transfer(service, flow("BTC", "1", "1"))
    state = await service._state()
    assert state.kill.soft and state.kill.daily_day == AT.date()
    await service.halt("Explicit C2 hard loss review", AT)
    before = await service._state()
    await transfer(service, flow(quantity="1000"))
    state = await service._state()
    assert state.kill.hard and state.kill.policy_epoch == before.kill.policy_epoch
    assert (
        state.high_water >= before.high_water
        and state.inception_high_water >= before.inception_high_water
    )


async def test_correction_reverses_replaces_backdated_record_invalidates_and_survives_restart(
    paper: tuple[PaperExecution, PaperExchange],
    tmp_path: Path,
) -> None:
    service, _ = paper
    await service.cashflow("second-flow", D(100), AT + timedelta(seconds=1))
    old = await service._state()
    async with service.writer.transaction() as c:
        history = await service.store.history(c)
        original = next(j for j in history if j.source == "opening")
        before_events = (
            (
                await c.execute(
                    select(events.c.fingerprint)
                    .where(events.c.account_id == old.account_id)
                    .order_by(events.c.version)
                )
            )
            .scalars()
            .all()
        )
    old_report = performance(old, history, old.as_of)
    r, result = await correction(
        service, original.id, flow(quantity="900", kind="cash"), at=AT + timedelta(seconds=2)
    )
    state = await service._state()
    assert state.quantity("GBP") == 1000 and state.units == 1100
    assert state.accounting_invalidated and state.kill.hard and not state.ready
    assert state.high_water == old.high_water and state.daily_unit_price == old.daily_unit_price
    record = result["record"]
    assert record["original"] == original.document() and record["prior_report"] == old_report
    undo, replacement = (Journal.restore(j) for j in record["postings"])
    assert undo.reverses == replacement.replaces == original.id
    assert undo.movements[0].quantity == -1000 and replacement.movements[0].quantity == 900
    async with service.writer.transaction() as c:
        assert await service.store.audit(c) == state
        assert await service.store.ledger_balances(c) == state.balances
        assert (
            await c.execute(
                select(events.c.fingerprint)
                .where(events.c.account_id == old.account_id, events.c.version <= old.version)
                .order_by(events.c.version)
            )
        ).scalars().all() == before_events
        records = await accounting_status(c, old.account_id)
        assert isinstance(records["items"], list) and len(records["items"]) == 1
    (tmp_path / "correction.json").write_text(json.dumps(result, sort_keys=True))
    assert not (await correct_accounting(service, r, at=AT + timedelta(seconds=3)))["inserted"]
    assert await service._state() == state
    report = await historical_performance(service.store)
    assert report["period"] is None and report["units_valid"] is False
    with pytest.raises(ValueError, match="invalidated replay enrolment"):
        await StoredReplay(service, PaperExchange(service.store)).enrol()
    current = performance(state, (), state.as_of)
    assert current["pnl_gbp"] is None and current["unit_price"] is None
    with pytest.raises(ValueError, match="invalidated"):
        unit_price(state, state.as_of)
    with pytest.raises(ValueError, match="invalidated"):
        await service.cashflow("must-not-rearm", D(100), state.as_of)
    await service.writer.__aexit__()
    async with PaperWriter(service.store) as owner:
        fresh = PaperExecution(owner, PaperExchange(service.store))
        assert not (await fresh.recover(state.as_of)).complete
        with pytest.raises(ValueError, match="reconciliation"):
            await fresh.review("Reviewed correction cannot repair invalid units", state.as_of)
        restored = await fresh._state()
        assert restored.accounting_invalidated and restored.kill.hard
        async with owner.transaction() as c:
            day = await daily_summary(c, service.store, AT.date())
        assert day["daily_return_reason"] == "accounting_correction_requires_rebuild"
        assert day["accounting_totals_available"] is False
        payload = notice_payload(
            {
                "schema_version": 1,
                "account_id": str(old.account_id),
                "kind": "daily",
                "summary": day,
                "live_execution_available": False,
            },
            uuid4(),
            old.account_id,
        )
        assert "require rebuild" in str(payload)


async def test_correction_of_transfer_and_replacement_chain(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, _ = paper
    _, incoming = await transfer(service, flow("BTC", "1", "100"))
    target = UUID(incoming["record"]["postings"][0]["id"])
    request, first = await correction(service, target, flow("BTC", "0.5", "100"))
    state = await service._state()
    assert state.quantity("BTC") == D("0.5") and state.cost("BTC") == 50
    with pytest.raises(ValueError, match="already reversed"):
        await correct_accounting(
            service,
            request.model_copy(update={"request_id": uuid4(), "expected_version": state.version}),
        )
    replacement = UUID(first["record"]["postings"][1]["id"])
    await correction(service, replacement, flow("BTC", "0.6", "100"))
    state = await service._state()
    assert state.quantity("BTC") == D("0.6") and state.cost("BTC") == 60
    async with service.writer.transaction() as c:
        assert await service.store.audit(c) == state


@pytest.mark.parametrize(
    "point",
    [
        "before_transfer_commit",
        "after_transfer_commit",
        "before_correction_commit",
        "after_correction_commit",
    ],
)
async def test_accounting_crash_atomicity_and_exact_retry(
    paper: tuple[PaperExecution, PaperExchange], point: str
) -> None:
    service, _ = paper
    old = await service._state()
    is_transfer = "transfer" in point
    async with service.writer.transaction() as c:
        target = (await service.store.history(c))[0].id
    r = (
        TransferRequest(
            request_id=uuid4(),
            actor=uuid4(),
            expected_version=old.version,
            evidence_sha256="a" * 64,
            reason="Atomic accounting crash fixture",
            external_reference="crash",
            flow=flow(),
        )
        if is_transfer
        else CorrectionRequest(
            request_id=uuid4(),
            actor=uuid4(),
            expected_version=old.version,
            evidence_sha256="a" * 64,
            reason="Atomic accounting crash fixture",
            target_journal=target,
            replacement=flow(quantity="900", kind="cash"),
        )
    )

    async def action() -> dict[str, object]:
        if isinstance(r, TransferRequest):
            return await external_transfer(service, r)
        return await correct_accounting(service, r, at=AT)

    def crash(where: str) -> None:
        if where == point:
            raise SimulatedCrash()

    service.fault = crash
    with pytest.raises(SimulatedCrash):
        await action()
    service.fault = lambda _: None
    state = await service._state()
    assert state.version == old.version + (1 if point.startswith("after") else 0)
    result = await action()
    assert result["inserted"] is point.startswith("before")
    async with service.writer.transaction() as c:
        assert (
            await c.scalar(
                select(func.count())
                .select_from(operations)
                .where(operations.c.account_id == old.account_id)
            )
            == 1
        )
        assert await service.store.audit(c) == await service.store.load(c)


async def test_unposted_fill_and_unknown_command_block_both_mutations(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper
    order = await service.queue(signal(), market(), AT, time_in_force="FOK")
    await service.dispatch(order.intent.client_id, market(), AT)
    at = AT + timedelta(milliseconds=300)
    m = market(at)
    await venue.advance(m.book, m.rules, at)
    with pytest.raises(ValueError, match="reconciled fills"):
        await transfer(service, flow(at=at))
    async with service.writer.transaction() as c:
        target = (await service.store.history(c))[0].id
    with pytest.raises(ValueError, match="reconciled fills"):
        await correction(service, target, flow(quantity="900", kind="cash"), at=at)


async def test_sql_seals_prevent_mutation_and_invalidation_bypass(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, _ = paper
    _, incoming = await transfer(service, flow())
    target = UUID(incoming["record"]["postings"][0]["id"])
    await correction(service, target, flow(quantity="90"))
    state = await service._state()
    for statement in (
        "DELETE FROM aca.paper_accounting_operations WHERE account_id=:a",
        "UPDATE aca.paper_journals SET document=document WHERE account_id=:a",
    ):
        with pytest.raises(DBAPIError):
            async with service.writer.transaction() as c:
                await c.execute(text(statement), {"a": state.account_id})
    with pytest.raises(ValueError, match="cannot be cleared"):
        await service._save(
            state, state.model_copy(update={"accounting_invalidated": False}), at=AT, kind="bypass"
        )
    # Bypass the application guard and prove the deferred database guard rejects it.
    doc = {
        "format": "paper_delta_v1",
        "state_hash": "0" * 64,
        "changes": {"accounting_invalidated": False, "version": state.version + 1},
        "journal_id": None,
    }
    with pytest.raises(DBAPIError, match="invalidation cannot be cleared"):
        async with service.writer.transaction() as c:
            await c.execute(
                events.insert().values(
                    account_id=state.account_id,
                    version=state.version + 1,
                    id=uuid4(),
                    source="sql-bypass",
                    kind="bypass",
                    at=AT,
                    previous_hash="0" * 64,
                    fingerprint="0" * 64,
                    document=doc,
                )
            )


async def test_replay_transfer_accounting_and_report_flow_neutrality(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    service = runner.service
    prior: Any = await historical_performance(runner.store)
    await transfer(service, flow("BTC", "0.1", "999"))
    at = AT + timedelta(seconds=1)
    await feed.book(at)
    await feed.book(at, "ETH/GBP")
    await runner.batch(at=at)
    await service.reconcile(at)
    result = await historical_performance(runner.store)
    period = result["period"]
    assert isinstance(period, dict)
    assert D(str(period["contributions_gbp"])) == D("99.9")
    assert D(str(period["pnl_gbp"])) == D(str(period["twr"])) == 0
    _, outgoing = await transfer(service, flow("BTC", "-0.1", "999", at))
    at += timedelta(seconds=1)
    await feed.book(at)
    await runner.batch(at=at)
    result = await historical_performance(runner.store)
    period = result["period"]
    assert isinstance(period, dict)
    assert D(str(period["net_investment_flows_gbp"])) == 0
    assert D(str(period["pnl_gbp"])) == D(str(period["twr"])) == 0
    assert prior["period"]["investment_flow_count"] == 0
    correction_target = UUID(outgoing["record"]["postings"][0]["id"])
    await correction(service, correction_target, flow("BTC", "-0.05", "999", at), at=at)
    assert (await historical_performance(runner.store))["units_valid"] is False
    with pytest.raises(ValueError, match="invalidated historical replay"):
        await runner.batch(at=at)
    assert period["twr"] is not None  # The already issued object/evidence is intact.


async def test_accounting_cli_strict_json_and_audit(
    database_settings: Settings, tmp_path: Path
) -> None:
    account = uuid4()

    def args(action: str, **kwargs: Any) -> argparse.Namespace:
        return argparse.Namespace(action=action, account=account, **kwargs)

    async def command(action: str, **kwargs: Any) -> dict[str, Any]:
        result = await cli.execute(args(action, **kwargs), database_settings)
        assert isinstance(result, dict)
        return cast(dict[str, Any], result)

    await command("create")
    at = datetime.now(UTC)
    state = (await command("status"))["state"]
    r = TransferRequest(
        request_id=uuid4(),
        actor=uuid4(),
        expected_version=state["version"],
        external_reference="cli-inbound",
        flow=flow(at=at),
        evidence_sha256="b" * 64,
        reason="CLI operator transfer fixture",
    )
    file = tmp_path / "transfer.json"
    file.write_text(json.dumps(r.document()))
    result = await command("transfer", input=file)
    assert result["inserted"] is True
    target = UUID(result["record"]["postings"][0]["id"])
    state = (await command("status"))["state"]
    r2 = CorrectionRequest(
        request_id=uuid4(),
        actor=r.actor,
        expected_version=state["version"],
        target_journal=target,
        replacement=flow(quantity="90", at=at),
        evidence_sha256="e" * 64,
        reason="CLI operator correction fixture",
    )
    file.write_text(json.dumps(r2.document()))
    assert (await command("correct", input=file))["inserted"] is True
    result = await command("accounting", after_version=0, limit=1)
    assert len(result["items"]) == 1 and result["next_after_version"] is not None
    assert (await command("audit"))["complete"] is True
    assert (await command("audit"))["units_valid"] is False
    assert (await command("valuations", before=None, limit=20))["units_valid"] is False
    file.write_text('{"request_id":"a", "request_id":"b"}')
    with pytest.raises(ValueError, match="duplicate JSON key"):
        await command("transfer", input=file)


@settings(max_examples=1000, derandomize=True, deadline=None)
@given(asset=st.sampled_from(["BTC", "ETH"]), q=st.integers(1, 100000), p=st.integers(1, 100000))
def test_generated_transfer_no_gain_at_same_price_and_no_quantity_creation(
    asset: Asset, q: int, p: int
) -> None:
    with localcontext(FINANCIAL_CONTEXT):
        quantity, price = D(q) / 1000, D(p) / 100
        initial = cash_flow("capital", D(1000), AT)
        incoming = flow_journal(
            flow(asset, str(quantity), str(price)), balances((initial,)), source="in"
        )
        before = balances((initial, incoming))
        outgoing = flow_journal(
            flow(asset, str(quantity.copy_negate()), str(price)), before, source="out"
        )
        assert balances((initial, incoming, outgoing)) == balances((initial,))
        assert investment_amount(incoming) == investment_amount(outgoing).copy_negate()
        assert not any(
            line.account == "pnl:realised" for line in (*incoming.lines, *outgoing.lines)
        )


def test_legacy_state_and_journal_serialization_hashes_are_unchanged() -> None:
    original = cash_flow("legacy", D(100), AT)
    assert "valuation" not in original.document() and "replaces" not in original.document()
    assert Journal.restore(original.document()).document() == original.document()


async def test_unknown_command_blocks_accounting_without_releasing_reservation(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, _ = paper
    order = await service.queue(signal(), market(), AT)

    def crash(point: str) -> None:
        if point == "after_submitting_before_send":
            raise SimulatedCrash()

    service.fault = crash
    with pytest.raises(SimulatedCrash):
        await service.dispatch(order.intent.client_id, market(), AT)
    service.fault = lambda _: None
    before = await service._state()
    with pytest.raises(ValueError, match="resolved commands"):
        await transfer(service, flow())
    async with service.writer.transaction() as c:
        target = next(j.id for j in await service.store.history(c) if j.source == "opening")
    with pytest.raises(ValueError, match="resolved commands"):
        await correction(service, target, flow(quantity="900", kind="cash"))
    assert await service._state() == before


async def test_correction_rejects_spent_and_reserved_funds(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    from aca.portfolio.reserves import TaxReserveRequest

    service, _ = paper
    async with service.writer.transaction() as c:
        opening = next(j.id for j in await service.store.history(c) if j.source == "opening")
    await service.queue(signal(), market(), AT)
    with pytest.raises(ValueError, match="pending or reserved"):
        await correction(service, opening, flow(quantity="900", kind="cash"))
    await service.recover(AT)
    await service.reserve_tax_cash(
        TaxReserveRequest(
            request_id=uuid4(),
            actor=uuid4(),
            expected_version=(await service._state()).version,
            amount_gbp=D(950),
            evidence_sha256="a" * 64,
            reason="Reserved cash correction constraint",
        ),
        at=AT,
    )
    before = await service._state()
    with pytest.raises(ValueError, match="reserved GBP"):
        await correction(service, opening, flow(quantity="900", kind="cash"))
    assert await service._state() == before
    await service.cashflow("spent-funds", D(-2), AT)
    with pytest.raises(ValueError, match="negative owned"):
        await correction(service, opening, flow(quantity="1", kind="cash"))


async def test_stale_preflow_holding_and_regressing_transfer_time_are_blocked(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, _ = paper
    await transfer(service, flow("ETH", "1", "20"))
    old = await service._state()
    with pytest.raises(ValueError, match="valuation"):
        await transfer(service, flow(at=AT + timedelta(seconds=6)))
    assert await service._state() == old
    with pytest.raises(ValueError, match="nonregressing"):
        await transfer(service, flow(at=AT - timedelta(seconds=1)))


async def test_report_attributes_transferred_unrealised_gain_without_a_sale(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    await transfer(runner.service, flow("BTC", "1", "999"))
    at = AT + timedelta(seconds=1)
    await feed.book(at, bid="1199", ask="1200")
    await runner.batch(at=at)
    await runner.service.mark(
        (Valuation(asset="BTC", bid_gbp=D(1199), observed_at=at, source="fixture"),), at
    )
    await transfer(runner.service, flow("BTC", "-0.5", "1199", at))
    at += timedelta(seconds=1)
    await feed.book(at, bid="1199", ask="1200")
    await runner.batch(at=at)
    report = await historical_performance(runner.store)
    period = report["period"]
    assert isinstance(period, dict)
    assert D(str(period["pnl_gbp"])) == 200
    assert D(str(period["transferred_unrealised_gbp"])) == 100
    assert D(str(period["unrealised_change_gbp"])) == 100
    assert D(str(period["realised_pnl_gbp"])) == 0
    assert period["trade_fill_count"] == 0
    assert abs(D(str(period["twr"])) - D(200) / D(1999)) < D("1e-15")

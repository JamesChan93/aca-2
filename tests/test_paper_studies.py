"""No actual archive/holdout use; separate synthetic study and restart evidence."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_research_gates import synthetic

from aca import __version__
from aca.app.paper_study import PaperStudyStrategy
from aca.app.paper_supervisor import SupervisorStatus
from aca.domain.trading import fingerprint
from aca.execution.paper import PaperExecution
from aca.operations.config import RiskPolicy, Settings
from aca.operations.observation_profile import OperatingReview
from aca.research.artifacts import canonical, digest
from aca.research.gates import Evaluation, Freeze, HoldoutReview, bindings_for, freeze
from aca.research.holdout import open_once
from aca.research.study import strategy_definition
from aca.simulation.paper import PaperExchange
from aca.storage.audit_prefix import package_fingerprint
from aca.storage.database import create_engine
from aca.storage.paper import PaperStore, PaperWriter
from aca.storage.studies import (
    HealthBlock,
    PaperStudy,
    StartApproval,
    check_release,
    members,
    read_study,
    record_health,
    start_study,
    study_status,
)

AT = datetime(2026, 10, 29, tzinfo=UTC)
D = Decimal


def profile() -> OperatingReview:
    return OperatingReview(
        purpose="synthetic",
        actor=uuid4_fixed(),
        package=package_fingerprint(),
        reason="Hand-authored synthetic approval workflow; not a capacity measurement",
        measurement={
            "kind": "paper-observation-profile-measurement-v1",
            "package_source_sha256": package_fingerprint(),
            "accepted": True,
            "synthetic_contract_fixture": True,
            "model": {
                "availability_lower_model": 0.999,
                "projected_database_bytes": 1000000,
                "database_limit_bytes": 2000000000,
                "suffix_limit": 4096,
                "days": 56,
            },
            "workload": {"max_tick_seconds": 0.1},
        },
    )


def uuid4_fixed() -> UUID:
    return UUID("136810b7-492e-4e23-8649-0119e2458109")


async def prepare(
    engine: Any, root: Path, account: Any, trial: str = "BTC/GBP:240:sma200"
) -> tuple[StartApproval, Evaluation, Freeze, HoldoutReview]:
    report, _, evidence, calibrated, docs = synthetic()
    symbol, interval, variant = trial.split(":")
    report.update(symbol=symbol, interval_minutes=int(interval), variant=variant)
    if "+" in symbol:
        report.update(candidate_definition=strategy_definition(trial), joint_account=True)
    binding = bindings_for(report, evidence, strategy_definition(trial))
    envelope = Evaluation(
        bindings=binding, report=report, evidence=evidence, calibration=calibrated, reviews=docs
    )
    path = freeze(
        root,
        report,
        binding,
        evidence,
        calibrated,
        actor="synthetic-operator",
        reason="Explicit synthetic workflow only; no actual holdout or paper approval",
        reviews=docs,
    )

    async def outcome() -> dict[str, object]:
        return {"synthetic": True, "assessment": "hand-authored acceptance fixture"}

    review_path = await open_once(
        engine,
        root,
        path,
        report,
        binding,
        evidence,
        calibrated,
        outcome,
        synthetic_campaign=uuid4(),
        reviews=docs,
    )
    frozen = Freeze.model_validate_json(path.read_bytes())
    holdout = HoldoutReview.model_validate_json(review_path.read_bytes())
    approval = StartApproval(
        id=uuid4(),
        actor=uuid4(),
        account_id=account,
        purpose="synthetic",
        evaluation=digest(canonical(envelope.model_dump(mode="json"))),
        freeze=path.stem,
        holdout_review=review_path.stem,
        release=__version__,
        package=package_fingerprint(),
        policy=binding.policy,
        costs=binding.costs,
        operating_profile=profile().identity(),
        approved_at=AT,
        expires_at=AT + timedelta(hours=1),
        holdout_accepted=True,
        reason="Synthetic acceptance of a synthetic holdout; no real observation start",
    )
    return approval, envelope, frozen, holdout


@pytest.mark.integration
async def test_synthetic_study_restart_gaps_and_no_past_credit(
    database_settings: Settings, tmp_path: Path
) -> None:
    engine = create_engine(database_settings)
    store = PaperStore(engine, uuid4())
    try:
        evidence = await prepare(engine, tmp_path, store.account_id)
        async with PaperWriter(store) as writer:
            service = PaperExecution(writer, PaperExchange(store))
            await service.initialise(AT - timedelta(days=90))
            await service.cashflow(
                "old-funding-does-not-earn-days", D(1000), AT - timedelta(days=90)
            )
            study = await start_study(writer, *evidence, AT, operating_review=profile())
            await record_health(
                writer,
                study,
                HealthBlock(
                    start=AT,
                    end=AT + timedelta(seconds=60),
                    healthy_seconds=D(59),
                    unhealthy_seconds=D(1),
                    dependency_unavailable_seconds=D(0),
                    faults=("synthetic_fault",),
                    financial_discrepancies=("resolved_retained",),
                ),
            )
        await engine.dispose()
        engine = create_engine(database_settings)
        store = PaperStore(engine, store.account_id)
        async with PaperWriter(store) as writer:
            async with writer.transaction() as c:
                recovered = await read_study(c, store.account_id)
                assert recovered == study
            await record_health(
                writer,
                study,
                HealthBlock(
                    start=AT + timedelta(seconds=120),
                    end=AT + timedelta(seconds=180),
                    healthy_seconds=D(60),
                    unhealthy_seconds=D(0),
                    dependency_unavailable_seconds=D(0),
                ),
            )
            with pytest.raises(ValueError, match="already has"):
                await start_study(
                    writer, *evidence, AT + timedelta(seconds=180), operating_review=profile()
                )
            with pytest.raises(ValueError, match="overlap"):
                await record_health(
                    writer,
                    study,
                    HealthBlock(
                        start=AT,
                        end=AT + timedelta(seconds=1),
                        healthy_seconds=D(1),
                        unhealthy_seconds=D(0),
                        dependency_unavailable_seconds=D(0),
                    ),
                )
        async with engine.connect() as c:
            status = await study_status(c, study, AT + timedelta(seconds=240))
            assert status["healthy_seconds"] == "119"
            assert D(str(status["unobserved_seconds"])) == 120
            assert status["availability"] == str(D(119) / 240)
            assert status["retained_faults"] == ["synthetic_fault"]
            assert status["qualifying_days"] == status["qualifying_episodes"] == 0
            assert status["g4_pass"] is False
        for table in ("paper_studies", "paper_study_events"):
            for verb in ("DELETE FROM", "TRUNCATE", "UPDATE"):
                async with engine.begin() as c:
                    with pytest.raises(DBAPIError):
                        await c.execute(
                            text(
                                f"{verb} aca.{table}"
                                + (" SET document = document" if verb == "UPDATE" else "")
                            )
                        )
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize(
    "fault",
    [
        "account",
        "expiry",
        "freeze",
        "holdout",
        "costs",
        "package",
        "release",
        "evaluation",
        "purpose",
    ],
)
async def test_changed_or_missing_start_approval_rejected(
    database_settings: Settings, tmp_path: Path, fault: str
) -> None:
    engine = create_engine(database_settings)
    store = PaperStore(engine, uuid4())
    try:
        approval, evaluation, frozen, holdout = await prepare(engine, tmp_path, store.account_id)
        options: dict[str, dict[str, object]] = {
            "account": {"account_id": uuid4()},
            "expiry": {"expires_at": AT},
            "freeze": {"freeze": "c" * 64},
            "holdout": {"holdout_review": "c" * 64},
            "costs": {"costs": "c" * 64},
            "package": {"package": "c" * 64},
            "release": {"release": "changed"},
            "evaluation": {"evaluation": "c" * 64},
            "purpose": {"purpose": "actual"},
        }
        changes = options[fault]
        async with PaperWriter(store) as writer:
            service = PaperExecution(writer, PaperExchange(store))
            await service.initialise(AT)
            await service.cashflow("opening", D(1000), AT)
            with pytest.raises(ValueError):
                changed = StartApproval.model_validate({**approval.model_dump(), **changes})
                await start_study(
                    writer, changed, evaluation, frozen, holdout, AT, operating_review=profile()
                )
            async with writer.transaction() as c:
                assert await read_study(c, store.account_id) is None
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_real_strategy_flow_records_one_completed_synthetic_episode(
    database_settings: Settings, tmp_path: Path
) -> None:
    from test_paper_strategy import Workflow

    engine = create_engine(database_settings)
    store = PaperStore(engine, uuid4())
    try:
        evidence = await prepare(engine, tmp_path, store.account_id)
        async with PaperWriter(store) as writer:
            service = PaperExecution(writer, PaperExchange(store))
            await service.initialise(AT)
            await service.cashflow("opening", D(1000), AT)
            study = await start_study(writer, *evidence, AT, operating_review=profile())
            workflow = Workflow(service, AT)
            await workflow.replay.enrol()
            strategy = PaperStudyStrategy(service, study, lambda: workflow.now)
            workflow.strategy = strategy  # type: ignore[assignment]
            workflow.supervisor = workflow.supervision()
            await workflow.begin()
            entry = (await service._state()).orders[-1]
            # GTD is passive: qualifying sell prints consume the displayed queue.
            at = workflow.now + timedelta(milliseconds=300)
            await workflow.fresh(at)
            await workflow.feed.trade(at, size="1000")
            await workflow.tick(at)
            assert (await service._state()).quantity("BTC") > 0
            end = AT + timedelta(hours=8)
            await workflow.fresh(end + timedelta(seconds=10))
            await workflow.candles(end, direction="flat")
            await workflow.tick(end + timedelta(seconds=10))
            await workflow.fresh(workflow.now + timedelta(milliseconds=300))
            await workflow.tick(workflow.now)
            await strategy.observe(workflow.status, workflow.now)
            async with writer.transaction() as c:
                status = await study_status(c, study, workflow.now)
            assert status["observed_completed_signal_episodes"] == 1
            assert status["qualifying_episodes"] == 0
            count_before = len((await service._state()).orders)
            await workflow.supervisor.strategy.tick(workflow.now)  # type: ignore[union-attr]
            assert len((await service._state()).orders) == count_before
            assert entry.intent.client_id == members(study)[0].identity(AT + timedelta(hours=4))
    finally:
        await engine.dispose()


def test_all26_members_are_bound_and_operational_contract_unchanged() -> None:
    from aca.research.family import FAMILY
    from aca.storage.strategy import PaperSelection

    for trial in FAMILY:
        approval = StartApproval(
            id=uuid4(),
            actor=uuid4(),
            account_id=uuid4(),
            purpose="synthetic",
            evaluation="a" * 64,
            freeze="b" * 64,
            holdout_review="c" * 64,
            release=__version__,
            package=package_fingerprint(),
            policy=digest(canonical(RiskPolicy().model_dump(mode="json"))),
            costs="e" * 64,
            operating_profile=profile().identity(),
            approved_at=AT,
            expires_at=AT + timedelta(hours=1),
            holdout_accepted=True,
            reason="Strictly synthetic definition-bound member contract",
        )
        study = PaperStudy(
            id=uuid4(),
            approval=approval,
            candidate="a" * 64,
            trial=trial,
            started_at=AT,
            initial_state_hash="b" * 64,
            operating_review=profile(),
        )
        chosen = members(study)
        assert len(chosen) == (2 if "+" in trial else 1)
        assert len({m.identity(AT) for m in chosen}) == len(chosen)
        for member in chosen:
            if member.strategy.startswith("sma200-"):
                with pytest.raises(ValidationError):
                    PaperSelection.restore({**member.document(), "study_id": None})
        check_release(
            study.model_copy(
                update={
                    "approval": approval.model_copy(
                        update={"policy": digest(canonical(RiskPolicy().model_dump(mode="json")))}
                    )
                }
            )
        )


def test_health_partition_and_dependency_evidence_are_strict() -> None:
    for patch in (
        {"healthy_seconds": 1.0},
        {"healthy_seconds": D(2)},
        {"dependency_unavailable_seconds": D(1), "healthy_seconds": D(0)},
    ):
        with pytest.raises(ValueError):
            HealthBlock.model_validate(
                {
                    "start": AT,
                    "end": AT + timedelta(seconds=1),
                    "healthy_seconds": D(1),
                    "unhealthy_seconds": D(0),
                    "dependency_unavailable_seconds": D(0),
                    **patch,
                }
            )


@pytest.mark.parametrize("fault", ["availability", "storage", "tick", "source", "nan", "actual"])
def test_operating_review_does_not_trust_a_passing_flag(fault: str) -> None:
    from copy import deepcopy

    doc = deepcopy(profile().document())
    measurement = doc["measurement"]
    assert isinstance(measurement, dict)
    if fault == "availability":
        measurement["model"]["availability_lower_model"] = 0.994
    elif fault == "storage":
        measurement["model"]["projected_database_bytes"] = 2000000000
    elif fault == "tick":
        measurement["workload"]["max_tick_seconds"] = 3
    elif fault == "source":
        measurement["package_source_sha256"] = "0" * 64
    elif fault == "nan":
        measurement["model"]["availability_lower_model"] = float("nan")
    else:
        doc["purpose"] = "actual"
    with pytest.raises(ValueError):
        OperatingReview.restore(doc)


def test_storage_profile_requires_an_explicit_supported_setting() -> None:
    from aca.operations.config import load_settings

    assert Settings().paper_database_budget == 2000000000
    assert Settings(paper_storage="observation-8gb").paper_database_budget == 8000000000
    assert "paper_storage" not in Settings().document()  # legacy configuration hashes survive
    with pytest.raises(ValueError):
        Settings.model_validate({"paper_storage": "unbounded"})
    with pytest.raises(ValueError):
        load_settings(environ={"ACA_PAPER_STORAGE": "observation-8gb"})


@pytest.mark.integration
async def test_operational_and_study_selections_cannot_mix(
    database_settings: Settings, tmp_path: Path
) -> None:
    from aca.storage.strategy import PaperSelection, select_strategy

    engine = create_engine(database_settings)
    store = PaperStore(engine, uuid4())
    try:
        evidence = await prepare(engine, tmp_path, store.account_id)
        async with PaperWriter(store) as writer:
            service = PaperExecution(writer, PaperExchange(store))
            await service.initialise(AT)
            await service.cashflow("opening", D(1000), AT)
            chosen = PaperSelection(
                account_id=store.account_id,
                strategy="sma200",
                symbol="BTC/GBP",
                interval_minutes=240,
                selected_at=AT,
                policy_hash=fingerprint(RiskPolicy().model_dump(mode="json")),
            )
            await select_strategy(writer, chosen)
            with pytest.raises(ValueError, match="relabelled"):
                await start_study(writer, *evidence, AT, operating_review=profile())
        other = PaperStore(engine, uuid4())
        other_evidence = await prepare(engine, tmp_path / "other", other.account_id)
        async with PaperWriter(other) as writer:
            service = PaperExecution(writer, PaperExchange(other))
            await service.initialise(AT)
            await service.cashflow("opening", D(1000), AT)
            study = await start_study(writer, *other_evidence, AT, operating_review=profile())
            with pytest.raises(ValueError, match="operational-only"):
                await select_strategy(
                    writer, chosen.model_copy(update={"account_id": other.account_id})
                )
            # Session-local buffering never fabricates the gap between processes.
            strategy = PaperStudyStrategy(service, study, lambda: AT)
            status = SupervisorStatus()
            status.market_ready = status.reconciled = True
            status.reason = None
            await strategy.observe(status, AT)
            await strategy.observe(status, AT + timedelta(seconds=1))
            await strategy.flush()
            restarted = PaperStudyStrategy(service, study, lambda: AT + timedelta(seconds=100))
            await restarted.observe(status, AT + timedelta(seconds=100))
            await restarted.observe(status, AT + timedelta(seconds=101))
            await restarted.flush()
            async with writer.transaction() as c:
                report = await study_status(c, study, AT + timedelta(seconds=102))
            assert D(str(report["unobserved_seconds"])) == 100
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_only_recent_verified_external_status_can_exclude_dependency_time(
    database_settings: Settings, tmp_path: Path
) -> None:
    from test_stored_replay import Feed

    from aca.simulation.stored_replay import StoredReplay

    engine = create_engine(database_settings)
    store = PaperStore(engine, uuid4())
    try:
        evidence = await prepare(engine, tmp_path, store.account_id)
        async with PaperWriter(store) as writer:
            venue = PaperExchange(store)
            service = PaperExecution(writer, venue)
            await service.initialise(AT)
            await service.cashflow("opening", D(1000), AT)
            study = await start_study(writer, *evidence, AT, operating_review=profile())
            feed = Feed(StoredReplay(service, venue))
            await feed.marker("epoch_started", AT)
            await feed.marker("status", AT, {"system": "maintenance"})
            strategy = PaperStudyStrategy(service, study, lambda: AT)
            status = SupervisorStatus()
            status.reason = "paper_replay_unavailable"
            for seconds in (0, 1, 2):
                await strategy.observe(status, AT + timedelta(seconds=seconds))
            await strategy.flush()
            # An old status cannot explain a subsequent missing feed.
            for seconds in (8, 9):
                await strategy.observe(status, AT + timedelta(seconds=seconds))
            await strategy.flush()
            async with writer.transaction() as c:
                report = await study_status(c, study, AT + timedelta(seconds=9))
            assert D(str(report["dependency_unavailable_seconds"])) == 2
            assert D(str(report["unhealthy_seconds"])) == 1
            assert D(str(report["unobserved_seconds"])) == 6
            assert report["qualifying_days"] == 0
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_cli_records_exact_reviewed_start_and_rejects_replay(
    database_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from aca.operations import paper
    from aca.operations.cli import _parser

    engine = create_engine(database_settings)
    store = PaperStore(engine, uuid4())
    try:
        evidence = await prepare(engine, tmp_path, store.account_id)
        async with PaperWriter(store) as writer:
            service = PaperExecution(writer, PaperExchange(store))
            await service.initialise(AT)
            await service.cashflow("opening", D(1000), AT)
        arguments = ["paper", "study-start", "--account", str(store.account_id)]
        for name, value in zip(
            ("approval", "evaluation", "freeze", "holdout", "operating-review"),
            (*evidence, profile()),
            strict=True,
        ):
            path = tmp_path / (name + ".json")
            path.write_bytes(canonical(value.model_dump(mode="json")))
            arguments.extend(("--" + name, str(path)))
        monkeypatch.setattr(paper, "datetime", SimpleNamespace(now=lambda zone: AT))
        result = await paper.execute(_parser().parse_args(arguments), database_settings)
        assert isinstance(result, dict) and result["approval"] == evidence[0].document()
        status = await paper.execute(
            _parser().parse_args(["paper", "study-status", "--account", str(store.account_id)]),
            database_settings,
        )
        assert isinstance(status, dict) and status["qualifying_days"] == 0
        with pytest.raises(ValueError, match="already has"):
            await paper.execute(_parser().parse_args(arguments), database_settings)
    finally:
        await engine.dispose()

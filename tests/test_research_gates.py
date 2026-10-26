"""Independent inference probes and positive/rejected synthetic workflow contracts."""

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from aca.operations.config import RiskPolicy, Settings
from aca.research.artifacts import canonical, digest, read_json
from aca.research.gates import Bindings, Evidence, assess, bindings_for, freeze
from aca.research.holdout import ACTUAL_SCOPE, open_once
from aca.research.inference import (
    PROTOCOL,
    StatisticalProtocol,
    _means,
    block_bound,
    calibration,
    implementation_identity,
    infer,
)
from aca.research.runner import code_fingerprint
from aca.research.study import strategy_definition
from aca.simulation.reference import CostModel
from aca.storage.database import create_engine
from aca.storage.models import ResearchHoldoutUse


def synthetic() -> tuple[
    dict[str, Any], Bindings, Evidence, dict[str, object], dict[str, dict[str, object]]
]:
    """Hand-authored inference workflow; no real archive/holdout/strategy profit claim."""
    days = [(date(2020, 1, 1) + timedelta(days=i)).isoformat() for i in range(730)]
    report: dict[str, Any] = {
        "kind": "continuous-oos-equal-flows-v1",
        "scope": "out_of_sample",
        "symbol": "BTC/GBP",
        "variant": "sma200",
        "interval_minutes": 1440,
        "start": "2020-01-01T00:00:00+00:00",
        "end_exclusive": "2021-12-31T00:00:00+00:00",
        "holdout_opened": False,
        "costs": CostModel().model_dump(mode="json"),
        "risk_policy": RiskPolicy().model_dump(mode="json"),
        "scenario": {
            "initial_gbp": "250",
            "monthly_gbp": "250",
            "infrastructure_monthly_gbp": "20",
        },
        "candidate": {
            "completed_episodes": 50,
            "stale_terminal_mark": False,
            "mean_equity_gbp": "10000",
            "outstanding_orders": 0,
        },
        "comparators": {name: {"stale_terminal_mark": False} for name in ("cash", "passive10")},
        "calendar": {
            "dates": days,
            "returns_decimal": {
                "candidate": ["0.001"] * 730,
                "cash": ["0"] * 730,
                "passive10": ["0.0001"] * 730,
            },
            "first_day_partial": False,
            "last_day_partial": False,
            "statistical_sample_complete": True,
        },
    }
    calibrated: dict[str, object] = {
        "kind": "synthetic_statistical_calibration",
        "protocol_sha256": PROTOCOL.identity(),
        "passes_declared_probe": True,
        "implementation_sha256": implementation_identity(),
        "scope": "workflow_fixture_only; real_calibration_separately_tested",
    }
    context = {
        "purpose": "synthetic",
        "dataset": "a" * 64,
        "code": code_fingerprint(),
        "costs": digest(canonical(report["costs"])),
        "policy": digest(canonical(report["risk_policy"])),
        "protocol": PROTOCOL.identity(),
    }
    docs: dict[str, dict[str, object]] = {}
    references: dict[str, str] = {}
    for category in ("source", "cost", "parity", "reuse", "stability"):
        doc: dict[str, object] = {
            "kind": f"research-{category}-review-v1",
            "purpose": "synthetic",
            "context": context,
            "approved": True,
            "evidence": ["explicitly_synthetic_contract_probe"],
        }
        if category == "source":
            doc = {
                "kind": "historical-source-assurance-review-v1",
                "purpose": "synthetic",
                "dataset_sha256": "a" * 64,
                "decision": "QUALIFIED",
                "blockers": [],
            }
        reference = digest(canonical(doc))
        docs[reference] = doc
        references[category] = reference
    evidence = Evidence(
        purpose="synthetic",
        dataset="a" * 64,
        source_lineage="b" * 64,
        source_review=references["source"],
        cost_review=references["cost"],
        parity_review=references["parity"],
        reuse_review=references["reuse"],
        stability_review=references["stability"],
        calibration=digest(canonical(calibrated)),
        blockers=(),
        history_days=2000,
        source_qualified=True,
        cost_qualified=True,
        parity_qualified=True,
        reuse_approved=True,
        stability_approved=True,
    )
    bindings = bindings_for(report, evidence, strategy_definition("BTC/GBP:1440:sma200"))
    return report, bindings, evidence, calibrated, docs


def test_protocol_is_fixed_full_family_with_sufficient_tail_resolution() -> None:
    assert PROTOCOL.family_size == 104
    assert PROTOCOL.samples == 262_144
    assert (
        PROTOCOL.samples * (PROTOCOL.alpha - PROTOCOL.monte_carlo_error) / PROTOCOL.family_size
        > 100
    )
    assert PROTOCOL.capital_selection.startswith("250_initial_250_monthly")
    for change in (
        {"samples": 10000},
        {"candidate_ceiling": 20},
        {"seed": 77},
        {"blocks": (5, 20)},
        {"alpha": 0.1},
    ):
        with pytest.raises(ValidationError):
            StatisticalProtocol.model_validate({**PROTOCOL.model_dump(), **change})


def test_block_sums_match_independent_literal_indices() -> None:
    values = np.array([i / 10000 for i in range(83)])
    expected = []
    generator = np.random.Generator(np.random.PCG64(42))
    for _ in range(3):
        starts = generator.integers(0, 83, size=(1, 3))
        indices = ((starts[:, :, None] + np.arange(30)) % 83).reshape(-1)[:83]
        expected.append(np.mean(values[indices]))
    assert np.allclose(_means(values, 30, 3, 42), expected, rtol=0, atol=1e-17)


def test_constant_effect_zero_and_negative_bounds_are_reproducible() -> None:
    result = block_bound(np.full(730, 0.001), 7)
    assert float(str(result["decision_lower"])) == pytest.approx(0.36525)
    assert float(str(result["decision_lower"])) <= float(str(result["baseline_bonferroni_lower"]))
    assert result["conservative_order_rank_zero_based"] == 56
    assert result == block_bound(np.full(730, 0.001), 7)
    assert block_bound(np.zeros(730), 30)["decision_lower"] == 0
    assert float(str(block_bound(np.full(730, -0.001), 30)["decision_lower"])) < 0


def test_correlated_clustered_null_and_known_effect_calibration() -> None:
    result = calibration()
    assert result["null_false_positives"] == 0
    assert result["known_effect_detections"] == 64
    assert result["null_fwer_one_sided_95_upper_if_zero"] == pytest.approx(0.045729702330762456)
    assert result["passes_declared_probe"] is True
    assert result["promotion_allowed"] is False


@pytest.mark.parametrize(
    "fault", ["missing", "nan", "infinity", "wealth", "duplicate", "order", "gap", "partial"]
)
def test_daily_inference_rejects_invalid_or_shortened_samples(fault: str) -> None:
    report, _, _, _, _ = synthetic()
    daily = report["calendar"]
    if fault == "missing":
        daily["returns_decimal"]["candidate"][8] = None
    elif fault in {"nan", "infinity", "wealth"}:
        daily["returns_decimal"]["candidate"][8] = {
            "nan": "NaN",
            "infinity": "Infinity",
            "wealth": "-1",
        }[fault]
    elif fault == "duplicate":
        daily["dates"][8] = daily["dates"][7]
    elif fault == "order":
        daily["dates"].reverse()
    elif fault == "gap":
        daily["dates"].pop(8)
    else:
        daily["first_day_partial"] = True
    with pytest.raises(ValueError):
        infer(daily)


def test_wholly_synthetic_positive_and_economic_gate(tmp_path: Path) -> None:
    report, binding, evidence, calibrated, docs = synthetic()
    result = assess(report, binding, evidence, calibration=calibrated, reviews=docs)
    assert result.status == "freeze_eligible"
    assert result.statistical_pass and result.economic_pass
    assert result.economic_hurdle == Decimal(480)
    assert not result.promotion_allowed and not result.execution_permission
    path = freeze(
        tmp_path,
        report,
        binding,
        evidence,
        calibrated,
        actor="test-only",
        reason="Wholly synthetic positive workflow, no actual qualification",
        reviews=docs,
    )
    assert read_json(path)["kind"] == "candidate-freeze-v2"
    changed = deepcopy(report)
    changed["candidate"]["mean_equity_gbp"] = "250"
    result = assess(changed, binding, evidence, calibration=calibrated, reviews=docs)
    assert result.status == "failed" and result.statistical_pass and not result.economic_pass
    with pytest.raises(ValueError, match="complete passing"):
        freeze(
            tmp_path,
            changed,
            binding,
            evidence,
            calibrated,
            actor="test-only",
            reason="Insufficient economic value must never freeze",
            reviews=docs,
        )


@pytest.mark.parametrize(
    "fault",
    [
        "source",
        "cost",
        "parity",
        "reuse",
        "stability",
        "history",
        "calibration",
        "stale",
        "episodes",
        "outstanding",
        "null",
        "negative",
        "capital",
    ],
)
def test_all_evidence_or_performance_failures_cannot_freeze(tmp_path: Path, fault: str) -> None:
    report, _, evidence, calibrated, docs = synthetic()
    update: dict[str, object] = {}
    if fault in {"source", "cost", "parity"}:
        update[f"{fault}_qualified"] = False
    elif fault in {"reuse", "stability"}:
        update[f"{fault}_approved"] = False
    elif fault == "history":
        update["history_days"] = 1460
    elif fault == "calibration":
        calibrated["passes_declared_probe"] = False
    elif fault == "stale":
        report["candidate"]["stale_terminal_mark"] = True
    elif fault == "episodes":
        report["candidate"]["completed_episodes"] = 49
    elif fault == "outstanding":
        report["candidate"]["outstanding_orders"] = 1
    elif fault == "null":
        report["calendar"]["returns_decimal"]["candidate"][8] = None
    elif fault == "negative":
        report["calendar"]["returns_decimal"]["candidate"] = ["-0.001"] * 730
    elif fault == "capital":
        report["scenario"]["monthly_gbp"] = "400"
    evidence = Evidence.model_validate({**evidence.model_dump(), **update})
    bindings = bindings_for(report, evidence, strategy_definition("BTC/GBP:1440:sma200"))
    with pytest.raises(ValueError):
        freeze(
            tmp_path,
            report,
            bindings,
            evidence,
            calibrated,
            actor="test-only",
            reason="Any incomplete or failed criterion must reject a freeze",
            reviews=docs,
        )
    assert not (tmp_path / "freezes").exists()


@pytest.mark.parametrize(
    "field",
    [
        "dataset",
        "source_lineage",
        "code",
        "strategy",
        "costs",
        "policy",
        "calendar",
        "protocol",
        "evidence",
    ],
)
def test_changed_binding_rejected(field: str) -> None:
    report, bindings, evidence, calibrated, docs = synthetic()
    changed = Bindings.model_validate({**bindings.model_dump(), field: "c" * 64})
    with pytest.raises(ValueError):
        assess(report, changed, evidence, calibration=calibrated, reviews=docs)


def test_missing_tampered_or_wrong_namespace_review_is_incomplete() -> None:
    report, bindings, evidence, calibrated, docs = synthetic()
    assert assess(report, bindings, evidence, calibration=calibrated).status == "incomplete"
    docs[evidence.cost_review]["approved"] = False
    assert (
        assess(report, bindings, evidence, calibration=calibrated, reviews=docs).status
        == "incomplete"
    )
    # Even rehashing synthetic facts cannot qualify an actual-data context.
    actual = Evidence.model_validate({**evidence.model_dump(), "purpose": "actual"})
    binding = bindings_for(report, actual, strategy_definition("BTC/GBP:1440:sma200"))
    assert (
        assess(report, binding, actual, calibration=calibrated, reviews=docs).status == "incomplete"
    )


@pytest.mark.integration
async def test_holdout_positive_path_is_consumed_across_roots_and_restart(
    database_settings: Settings, tmp_path: Path
) -> None:
    report, bindings, evidence, calibrated, docs = synthetic()
    frozen = freeze(
        tmp_path,
        report,
        bindings,
        evidence,
        calibrated,
        actor="test-only",
        reason="Synthetic positive path only; actual holdout remains closed",
        reviews=docs,
    )
    campaign = uuid4()
    called = 0
    engine = create_engine(database_settings)

    async def work() -> dict[str, object]:
        nonlocal called
        called += 1
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(ResearchHoldoutUse)
                    .where(
                        ResearchHoldoutUse.scope == f"synthetic:{campaign}:2025-10-01:2026-10-01"
                    )
                )
                == 1
            )
        return {"wholly_synthetic": True, "result": "not_promoted"}

    try:
        path = await open_once(
            engine,
            tmp_path,
            frozen,
            report,
            bindings,
            evidence,
            calibrated,
            work,
            synthetic_campaign=campaign,
            reviews=docs,
        )
        assert read_json(path)["status"] == "holdout_reviewed"
        assert read_json(path)["promotion_allowed"] is False
        from aca.research.registry import register
        from aca.storage.models import ResearchArtifact

        assert (await register(tmp_path, engine))["artifacts_inserted"] == 2
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(ResearchArtifact.kind).where(ResearchArtifact.fingerprint == path.stem)
                )
                == "research-holdout-review-v1"
            )

        await engine.dispose()
        engine = create_engine(database_settings)
        with pytest.raises(ValueError, match="already consumed"):
            await open_once(
                engine,
                tmp_path / "new-tree",
                frozen,
                report,
                bindings,
                evidence,
                calibrated,
                work,
                synthetic_campaign=campaign,
                reviews=docs,
            )
        assert called == 1
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(ResearchHoldoutUse)
                    .where(ResearchHoldoutUse.scope == ACTUAL_SCOPE)
                )
                == 0
            )
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_holdout_failure_retains_claim_and_stale_evidence_never_calls_work(
    database_settings: Settings, tmp_path: Path
) -> None:
    report, bindings, evidence, calibrated, docs = synthetic()
    frozen = freeze(
        tmp_path,
        report,
        bindings,
        evidence,
        calibrated,
        actor="test-only",
        reason="Synthetic crash/recovery path, no actual evaluation",
        reviews=docs,
    )
    engine = create_engine(database_settings)
    campaign = uuid4()
    calls = 0

    async def fail() -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise RuntimeError("injected_after_committed_claim")

    try:
        stale = deepcopy(report)
        stale["candidate"]["completed_episodes"] = 0
        with pytest.raises(ValueError, match="no longer"):
            await open_once(
                engine,
                tmp_path,
                frozen,
                stale,
                bindings,
                evidence,
                calibrated,
                fail,
                synthetic_campaign=campaign,
                reviews=docs,
            )
        assert calls == 0
        with pytest.raises(RuntimeError, match="injected"):
            await open_once(
                engine,
                tmp_path,
                frozen,
                report,
                bindings,
                evidence,
                calibrated,
                fail,
                synthetic_campaign=campaign,
                reviews=docs,
            )
        with pytest.raises(ValueError, match="already consumed"):
            await open_once(
                engine,
                tmp_path,
                frozen,
                report,
                bindings,
                evidence,
                calibrated,
                fail,
                synthetic_campaign=campaign,
                reviews=docs,
            )
        assert calls == 1
        for verb in ("DELETE FROM", "TRUNCATE", "UPDATE"):
            async with engine.begin() as connection:
                with pytest.raises(DBAPIError):
                    command = f"{verb} aca.research_holdout_uses"
                    if verb == "UPDATE":
                        command += " SET freeze = freeze"
                    await connection.execute(text(command))
    finally:
        await engine.dispose()


def test_freeze_cli_uses_rechecked_envelope_and_rejects_tampering(tmp_path: Path) -> None:
    from aca.research.artifacts import save_document
    from aca.research.gates import Evaluation
    from aca.research.runner import freeze as cli_freeze

    report, bindings, evidence, calibrated, docs = synthetic()
    envelope = Evaluation(
        bindings=bindings, report=report, evidence=evidence, calibration=calibrated, reviews=docs
    )
    path = save_document(tmp_path / "evaluations", envelope.model_dump(mode="json"))
    frozen = cli_freeze(
        tmp_path, path, bindings.trial, "Explicit synthetic operator review", actor="test-only"
    )
    assert read_json(frozen)["bindings"]["purpose"] == "synthetic"  # type: ignore[index]
    with pytest.raises(ValueError, match="candidate differs"):
        cli_freeze(
            tmp_path, path, "ETH/GBP:1440:sma200", "Explicit synthetic rejection", actor="test-only"
        )
    path.write_bytes(canonical({"kind": "corrected-evaluation-v1"}))
    with pytest.raises(ValueError, match="identity mismatch"):
        cli_freeze(
            tmp_path, path, bindings.trial, "Changed artefact must reject", actor="test-only"
        )


def test_corrected_s0_preregisters_all20_and_preserves_blocked_no_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from test_research_core import bars

    from aca.research.bundles import archive, restore, verify
    from aca.research.datasets import Dataset
    from aca.research.study import run_corrected

    _, _, evidence, calibrated, docs = synthetic()
    series = {}
    for symbol in ("BTC/GBP", "ETH/GBP"):
        for interval in (240, 1440):
            first, last = bars([100, 100], interval=interval)
            series[(symbol, interval)] = (
                first.model_copy(
                    update={"symbol": symbol, "start": datetime(2015, 1, 1, tzinfo=UTC)}
                ),
                last.model_copy(
                    update={"symbol": symbol, "end": datetime(2026, 10, 1, tzinfo=UTC)}
                ),
            )
    dataset = Dataset(evidence.dataset, {}, series)  # type: ignore[arg-type]
    monkeypatch.setattr("aca.research.study.load_dataset", lambda _: dataset)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("blocked source must not evaluate prices")

    monkeypatch.setattr("aca.research.study.ContinuousEvaluation", forbidden)
    blocked = Evidence.model_validate(
        {**evidence.model_dump(), "blockers": ("disputed_BTC_retained",), "source_qualified": False}
    )
    packet = run_corrected(tmp_path, tmp_path / "unused", blocked, calibrated, reviews=docs)
    summary = verify(packet)
    assert len(summary["tasks"]) == 20  # type: ignore[arg-type]
    tasks = summary["tasks"]
    assert isinstance(tasks, dict)
    assert {item["reason"] for item in tasks.values()} == {"incomplete_evidence_no_run"}
    assert packet == run_corrected(tmp_path, tmp_path / "unused", blocked, calibrated, reviews=docs)
    assert len(list((tmp_path / "decisions").glob("*.json"))) == 20
    assert not (tmp_path / "evaluations").exists()
    assert not (tmp_path / "holdout-openings").exists()
    archive(packet, tmp_path / "blocked.tar")
    assert verify(restore(tmp_path / "blocked.tar", tmp_path / "restored")) == summary


@pytest.mark.integration
async def test_concurrent_holdout_requests_invoke_work_at_most_once(
    database_settings: Settings, tmp_path: Path
) -> None:
    import asyncio

    report, bindings, evidence, calibrated, docs = synthetic()
    frozen = freeze(
        tmp_path,
        report,
        bindings,
        evidence,
        calibrated,
        actor="test-only",
        reason="Parallel wholly synthetic requests must never replay",
        reviews=docs,
    )
    campaign = uuid4()
    engine = create_engine(database_settings)
    calls = 0

    async def work() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"synthetic": True}

    try:
        results = await asyncio.gather(
            *(
                open_once(
                    engine,
                    tmp_path / f"tree-{i}",
                    frozen,
                    report,
                    bindings,
                    evidence,
                    calibrated,
                    work,
                    synthetic_campaign=campaign,
                    reviews=docs,
                )
                for i in range(2)
            ),
            return_exceptions=True,
        )
        assert calls == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
        # A newly frozen evidence/lineage revision shares the same campaign use.
        revised = Evidence.model_validate({**evidence.model_dump(), "source_lineage": "d" * 64})
        binding = bindings_for(report, revised, strategy_definition(bindings.trial))
        changed = freeze(
            tmp_path / "revised",
            report,
            binding,
            revised,
            calibrated,
            actor="test-only",
            reason="Equivalent source lineage revision, synthetic only",
            reviews=docs,
        )
        with pytest.raises(ValueError, match="already consumed"):
            await open_once(
                engine,
                tmp_path / "revised",
                changed,
                report,
                binding,
                revised,
                calibrated,
                work,
                synthetic_campaign=campaign,
                reviews=docs,
            )
        assert calls == 1
    finally:
        await engine.dispose()


def test_corrected_workflow_executes_continuous_model_and_retains_short_sample_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from test_continuous_research import END, START, input_trades
    from test_research_core import bars

    from aca.domain.market import Candle, Symbol
    from aca.research.bundles import verify
    from aca.research.calendar import CommonCalendar, TestWindow
    from aca.research.datasets import Dataset
    from aca.research.gates import Evaluation
    from aca.research.study import run_corrected

    _, _, evidence, calibrated, docs = synthetic()
    series: dict[tuple[Symbol, int], tuple[Candle, ...]] = {}
    for symbol in ("BTC/GBP", "ETH/GBP"):
        for interval in (240, 1440):
            count = int((END - datetime(2019, 1, 1, tzinfo=UTC)).total_seconds() / (interval * 60))
            series[(symbol, interval)] = tuple(
                bar.model_copy(update={"symbol": symbol})
                for bar in bars([1000] * count, interval=interval)
            )
    dataset = Dataset(evidence.dataset, {}, series)
    monkeypatch.setattr("aca.research.study.load_dataset", lambda _: dataset)
    window = TestWindow(
        datetime(2017, 7, 1, tzinfo=UTC), datetime(2019, 7, 1, tzinfo=UTC), START, END, True
    )
    calendar = CommonCalendar(window.train_start, START, END, (window,), {})
    monkeypatch.setattr("aca.research.study.common_calendar", lambda _: calendar)
    path = tmp_path / "trades.jsonl"
    path.write_text(
        "".join(
            row.model_copy(update={"venue": "kraken"}).model_dump_json() + "\n"
            for row in input_trades()
        )
    )
    packet = run_corrected(
        tmp_path / "study", tmp_path / "unused", evidence, calibrated, trades=path, reviews=docs
    )
    assert len(verify(packet)["tasks"]) == 20  # type: ignore[arg-type]
    envelopes = list((tmp_path / "study/evaluations").glob("*.json"))
    assert len(envelopes) == 20
    for value in envelopes:
        envelope = Evaluation.model_validate_json(value.read_bytes())
        candidate = envelope.report["candidate"]
        assert candidate["execution_model"] == "ordered-trade-taker-v1"  # type: ignore[index]
        assert envelope.report["kind"] == "continuous-oos-equal-flows-v1"
        decision = assess(
            envelope.report,
            envelope.bindings,
            envelope.evidence,
            calibration=calibrated,
            reviews=docs,
        )
        assert decision.status == "failed"
        assert "fewer_than_two_years_oos" in decision.reasons
    assert not (tmp_path / "study/freezes").exists()

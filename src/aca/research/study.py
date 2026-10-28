"""Corrected S0: declare first, use continuous accounts, preserve every outcome."""

from collections.abc import Iterator, Mapping
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from aca.domain.market import Candle, InstrumentRules, MarketTrade, Symbol
from aca.operations.config import RiskPolicy
from aca.research.artifacts import canonical, digest, file_hash, read_json, save_document
from aca.research.bundles import OBJECT_BYTES, Result, Store, create, execute, seal
from aca.research.calendar import common_calendar
from aca.research.continuous import ContinuousEvaluation, comparison_report
from aca.research.datasets import load_dataset
from aca.research.gates import (
    Bindings,
    Evaluation,
    Evidence,
    assess,
    bindings_for,
    evidence_identity,
)
from aca.research.inference import PROTOCOL
from aca.research.protocol import TRIALS
from aca.research.runner import code_fingerprint, reconstructed_rules
from aca.simulation.reference import CostModel
from aca.strategies.slow import Variant


def strategy_definition(name: str) -> dict[str, object]:
    if name not in {f"{s}:{i}:{v}" for s, i, v in TRIALS}:
        raise ValueError("undeclared S0 candidate")
    return {
        "trial": name,
        "strategy_version": "slow-v2",
        "pyramiding": False,
        "protective_distance": "max(2*ATR14,5%entry); limit_1%below_trigger",
        "clock": "slow-v2; 10s_finalisation; 2s_dispatch; 250ms_latency; 60s_match",
        "stop_reentry": "next_fresh_closed_signal_only; latched_risk_remains",
    }


def _trades(path: Path, expected: str, end: datetime) -> Iterator[MarketTrade]:
    if file_hash(path) != expected:
        raise ValueError("trade input changed since preregistration")
    if path.stat().st_size > 2_000_000_000:
        raise ValueError("trade input exceeds retained study budget")
    with path.open("rb") as stream:
        while line := stream.readline(32_769):
            if len(line) > 32_768:
                raise ValueError("trade record exceeds budget")
            row = MarketTrade.model_validate_json(line)
            if row.received_at < end:
                yield row


def run_corrected(
    root: Path,
    manifest: Path,
    evidence: Evidence,
    calibration: dict[str, object],
    *,
    trades: Path | None = None,
    reviews: Mapping[str, dict[str, object]] | None = None,
) -> Path:
    dataset = load_dataset(manifest)
    if dataset.fingerprint != evidence.dataset:
        raise ValueError("dataset/evidence mismatch")
    coverage = {key: (bars[0].start, bars[-1].end) for key, bars in dataset.series.items()}
    calendar = common_calendar(coverage)
    calendar_document = {
        **asdict(calendar),
        "origin": calendar.origin.isoformat(),
        "oos_start": calendar.oos_start.isoformat(),
        "oos_end": calendar.oos_end.isoformat(),
        "windows": [
            {
                key: value.isoformat() if isinstance(value, datetime) else value
                for key, value in asdict(window).items()
            }
            for window in calendar.windows
        ],
    }
    names = [f"{symbol}:{interval}:{variant}" for symbol, interval, variant in TRIALS]
    declaration: dict[str, object] = {
        "kind": "corrected-S0-v1",
        "dataset": dataset.fingerprint,
        "source_lineage": evidence.source_lineage,
        "purpose": evidence.purpose,
        "code": code_fingerprint(),
        "protocol": PROTOCOL.model_dump(mode="json"),
        "evidence": evidence.model_dump(mode="json"),
        "calibration_sha256": digest(canonical(calibration)),
        "review_documents": dict(reviews or {}),
        "costs": CostModel().model_dump(mode="json"),
        "policy": RiskPolicy().model_dump(mode="json"),
        "calendar": calendar_document,
        "trials": [strategy_definition(name) for name in names],
        "trade_sha256": file_hash(trades) if trades is not None else None,
        "history_reuse": "original20_and_review_retained; not_independent_confirmation",
        "selection": (
            "central250/250_only; any_individually_eligible_recorded_review_no_return_ranking"
        ),
        "holdout_opened": False,
    }
    path = create(root, declaration, {name: strategy_definition(name) for name in names})
    for symbol, interval, variant in TRIALS:
        name = f"{symbol}:{interval}:{variant}"

        def work(
            store: Store,
            name: str = name,
            symbol: Symbol = symbol,
            interval: int = interval,
            variant: Variant = variant,
        ) -> Result:
            bindings = Bindings(
                purpose=evidence.purpose,
                trial=name,
                dataset=dataset.fingerprint,
                source_lineage=evidence.source_lineage,
                code=code_fingerprint(),
                strategy=digest(canonical(strategy_definition(name))),
                costs=digest(canonical(declaration["costs"])),
                policy=digest(canonical(declaration["policy"])),
                calendar=digest(canonical(calendar_document)),
                protocol=PROTOCOL.identity(),
                evidence=evidence_identity(evidence),
            )
            # No price-performance evaluation until source/cost/parity facts pass.
            preflight = assess(None, bindings, evidence, calibration=calibration, reviews=reviews)
            prerequisites = preflight.reasons == ("performance_not_evaluated",)
            if not prerequisites or trades is None:
                outcome = assess(None, bindings, evidence, calibration=calibration, reviews=reviews)
                save_document(root / "decisions", outcome.model_dump(mode="json"))
                return Result(
                    status="rejected",
                    reason="incomplete_evidence_no_run",
                    artifacts=(store.records("evidence", [outcome.model_dump(mode="json")]),),
                )
            rules: dict[Symbol, InstrumentRules] = {
                s: reconstructed_rules(s) for s in ("BTC/GBP", "ETH/GBP")
            }
            selected: dict[Symbol, tuple[Candle, ...]] = {
                s: dataset.series[(s, interval)] for s in ("BTC/GBP", "ETH/GBP")
            }
            comparison = ContinuousEvaluation(
                selected,
                _trades(trades, str(declaration["trade_sha256"]), calendar.oos_end),
                rules,
                symbol=symbol,
                variant=variant,
                start=calendar.oos_start,
                end=calendar.oos_end,
            ).run(tuple(window.test_end for window in calendar.windows[:-1]))
            report = comparison_report(comparison)
            bindings = bindings_for(report, evidence, strategy_definition(name))
            outcome = assess(report, bindings, evidence, calibration=calibration, reviews=reviews)
            envelope = Evaluation(
                bindings=bindings,
                report=report,
                evidence=evidence,
                calibration=calibration,
                reviews=dict(reviews or {}),
            )
            encoded = canonical(envelope.model_dump(mode="json"))
            save_document(root / "evaluations", envelope.model_dump(mode="json"))
            if len(encoded) > 16_000_000:
                raise ValueError("continuous report exceeds bounded gate input")
            save_document(root / "decisions", outcome.model_dump(mode="json"))
            return Result(
                status="completed" if outcome.status == "freeze_eligible" else "rejected",
                reason=outcome.status,
                artifacts=(
                    store.artifact(
                        "trial",
                        (
                            encoded[i : i + OBJECT_BYTES]
                            for i in range(0, len(encoded), OBJECT_BYTES)
                        ),
                    ),
                    store.records("evidence", [outcome.model_dump(mode="json")]),
                ),
            )

        execute(path, name, work)
    return seal(path)


def actual_evidence(source: Path, costs: Path, parity: Path, calibration: Path) -> Evidence:
    """Convert current actual reviews conservatively; never infer absent approvals."""
    data, cost, model, calibrated = (
        read_json(path) for path in (source, costs, parity, calibration)
    )
    if (
        data.get("kind") != "historical-source-assurance-review-v1"
        or cost.get("kind") != "public-execution-cost-review-v1"
    ):
        raise ValueError("wrong source/cost review kind")
    lineage = digest(
        canonical(
            {
                "venue": "kraken-spot",
                "universe": ["BTC/GBP", "ETH/GBP"],
                "source_family": "official-public-historical-archives",
            }
        )
    )
    blockers = data.get("blockers")
    if not isinstance(blockers, list) or not all(isinstance(item, str) for item in blockers):
        raise ValueError("invalid source review blockers")
    return Evidence(
        purpose="actual",
        dataset=str(data["dataset_sha256"]),
        source_lineage=lineage,
        source_review=digest(canonical(data)),
        cost_review=digest(canonical(cost)),
        parity_review=digest(canonical(model)),
        calibration=digest(canonical(calibrated)),
        blockers=tuple(blockers),
        history_days=0,
        source_qualified=data.get("decision") == "QUALIFIED" and not blockers,
        cost_qualified=False,
        parity_qualified=False,
    )

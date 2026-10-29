"""Corrected S0: declare first, use continuous accounts, preserve every outcome."""

from collections.abc import Iterator, Mapping
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from aca.domain.market import Candle, InstrumentRules, MarketTrade, Symbol
from aca.operations.config import RiskPolicy
from aca.research.artifacts import canonical, digest, file_hash, read_json, save_document
from aca.research.bundles import OBJECT_BYTES, Result, Store, create, execute, seal
from aca.research.calendar import common_calendar
from aca.research.continuous import ContinuousEvaluation, comparison_report
from aca.research.datasets import load_dataset
from aca.research.family import FAMILY, S0, S1, S2, PortfolioName, definition, portfolio_variant
from aca.research.gates import (
    Bindings,
    Evaluation,
    Evidence,
    assess,
    bindings_for,
    evidence_identity,
)
from aca.research.inference import PROTOCOL
from aca.research.runner import code_fingerprint, reconstructed_rules
from aca.simulation.reference import CostModel
from aca.strategies.slow import ResearchVariant


def strategy_definition(name: str) -> dict[str, object]:
    """Compatibility entry point; exact S0 hashes and the bounded new family."""
    return definition(name)


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
    study: Literal["S0", "S1S2"] = "S0",
) -> Path:
    if study not in {"S0", "S1S2"}:
        raise ValueError("undeclared study budget")
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
    names = S0 if study == "S0" else (*S1, *S2)
    declaration: dict[str, object] = {
        "kind": f"corrected-{study}-v1",
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
        "family_ceiling": 26,
        "reserved_family": list(FAMILY),
        "prior_baseline_report_sha256": (
            "67a8f9630be6d4f55ad52f2132ba70468f097fca859dbf3d8babc970f8f20790"
        ),
        "prior_S0_no_run_packet_sha256": (
            "b73c112c3fd18bdea3ada3114700e03a745db039c351b11d8d80904bef938a72"
        ),
        "mechanism_review": (
            "original_passive10_negative; corrected_S0_incomplete_no_empirical_ranking"
        ),
        "sensitivity_status": "not_run; required_review_before_eligibility",
    }
    path = create(root, declaration, {name: strategy_definition(name) for name in names})
    for name in names:

        def work(store: Store, name: str = name) -> Result:
            member, interval_text, variant_text = name.split(":")
            interval = int(interval_text)
            portfolio = cast(PortfolioName, variant_text) if name in S2 else None
            symbol = cast(Symbol, "BTC/GBP" if portfolio else member)
            variant = (
                portfolio_variant(portfolio) if portfolio else cast(ResearchVariant, variant_text)
            )
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
                portfolio=portfolio,
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

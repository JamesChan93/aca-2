"""Evidence-bound research decisions. Eligibility is rederived, never a caller flag."""

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal, localcontext
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from aca.domain.values import FINANCIAL_CONTEXT, FinancialDecimal, UtcDateTime
from aca.research.artifacts import canonical, digest, read_json, save_document
from aca.research.bundles import Contract, Hash
from aca.research.inference import PROTOCOL, implementation_identity, infer
from aca.research.runner import code_fingerprint


class Bindings(Contract):
    purpose: Literal["actual", "synthetic"]
    trial: str
    dataset: Hash
    source_lineage: Hash
    code: Hash
    strategy: Hash
    costs: Hash
    policy: Hash
    calendar: Hash
    protocol: Hash
    evidence: Hash

    def identity(self) -> str:
        return digest(canonical(self.model_dump(mode="json")))


class Evidence(Contract):
    """Reviewed facts; reference documents must be retained with the decision.

    This is an operator review contract, not an assertion that local file integrity
    establishes historical truth. Synthetic evidence cannot qualify actual paper.
    """

    purpose: Literal["actual", "synthetic"]
    dataset: Hash
    source_lineage: Hash
    source_review: Hash
    cost_review: Hash
    parity_review: Hash
    reuse_review: Hash | None = None
    stability_review: Hash | None = None
    calibration: Hash | None = None
    blockers: tuple[str, ...]
    history_days: Annotated[int, Field(ge=0)]
    source_qualified: bool = False
    cost_qualified: bool = False
    parity_qualified: bool = False
    reuse_approved: bool = False
    stability_approved: bool = False

    @model_validator(mode="after")
    def review_references(self) -> Evidence:
        for approved, reference in (
            (self.reuse_approved, self.reuse_review),
            (self.stability_approved, self.stability_review),
        ):
            if approved and reference is None:
                raise ValueError("approved review requires retained evidence")
        return self


class ReviewBundle(Contract):
    kind: Literal["research-review-bundle-v1"] = "research-review-bundle-v1"
    evidence: Evidence
    calibration: dict[str, object]
    documents: dict[str, dict[str, object]]


class Decision(Contract):
    kind: Literal["research-decision-v1"] = "research-decision-v1"
    status: Literal["failed", "incomplete", "freeze_eligible", "holdout_reviewed"]
    bindings: Bindings
    report: Hash | None
    reasons: tuple[str, ...]
    inference: dict[str, object] | None = None
    economic_hurdle: FinancialDecimal | None = None
    statistical_pass: bool = False
    economic_pass: bool = False
    promotion_allowed: Literal[False] = False
    execution_permission: Literal[False] = False

    @model_validator(mode="after")
    def coherent(self) -> Decision:
        if self.status in {"freeze_eligible", "holdout_reviewed"} and (
            self.reasons
            or not self.statistical_pass
            or not self.economic_pass
            or self.report is None
        ):
            raise ValueError("eligible decision requires every gate")
        return self


class Evaluation(Contract):
    kind: Literal["corrected-evaluation-v1"] = "corrected-evaluation-v1"
    bindings: Bindings
    report: dict[str, object]
    evidence: Evidence
    calibration: dict[str, object]
    reviews: dict[str, dict[str, object]]


class HoldoutReview(Contract):
    kind: Literal["research-holdout-review-v1"] = "research-holdout-review-v1"
    status: Literal["holdout_reviewed"] = "holdout_reviewed"
    decision: Decision
    scope: str
    freeze: Hash
    holdout_result: dict[str, object]
    holdout_pass: Literal[False] = False
    promotion_allowed: Literal[False] = False
    execution_permission: Literal[False] = False


class Freeze(Contract):
    kind: Literal["candidate-freeze-v2"] = "candidate-freeze-v2"
    decision: Hash
    bindings: Bindings
    review_actor: Annotated[str, Field(min_length=1, max_length=120)]
    review_reason: Annotated[str, Field(min_length=20, max_length=2000)]
    at: UtcDateTime
    execution_permission: Literal[False] = False
    promotion_allowed: Literal[False] = False


def evidence_identity(evidence: Evidence) -> str:
    return digest(canonical(evidence.model_dump(mode="json")))


def bindings_for(
    report: dict[str, object], evidence: Evidence, strategy: dict[str, object]
) -> Bindings:
    from aca.research.study import strategy_definition

    if strategy != strategy_definition(str(strategy["trial"])):
        raise ValueError("changed preregistered strategy definition")
    symbol, interval, variant = str(strategy["trial"]).split(":")
    if (
        report.get("symbol") != symbol
        or report.get("variant") != variant
        or report.get("interval_minutes") != int(interval)
        or (
            symbol == "BTC+ETH/GBP"
            and (
                report.get("candidate_definition") != strategy
                or report.get("joint_account") is not True
            )
        )
    ):
        raise ValueError("strategy/report binding mismatch")
    return Bindings(
        purpose=evidence.purpose,
        trial=str(strategy["trial"]),
        dataset=evidence.dataset,
        source_lineage=evidence.source_lineage,
        code=code_fingerprint(),
        strategy=digest(canonical(strategy)),
        costs=digest(canonical(report["costs"])),
        policy=digest(canonical(report["risk_policy"])),
        calendar=digest(
            canonical(
                {
                    "start": report["start"],
                    "end": report["end_exclusive"],
                    "calendar": report["calendar"],
                }
            )
        ),
        protocol=PROTOCOL.identity(),
        evidence=evidence_identity(evidence),
    )


def assess(
    report: dict[str, object] | None,
    bindings: Bindings,
    evidence: Evidence,
    *,
    calibration: dict[str, object] | None,
    reviews: Mapping[str, dict[str, object]] | None = None,
) -> Decision:
    if (
        bindings.evidence != evidence_identity(evidence)
        or bindings.dataset != evidence.dataset
        or bindings.source_lineage != evidence.source_lineage
        or bindings.purpose != evidence.purpose
        or bindings.code != code_fingerprint()
        or bindings.protocol != PROTOCOL.identity()
    ):
        raise ValueError("changed evidence/code/protocol binding")
    from aca.research.study import strategy_definition

    if bindings.strategy != digest(canonical(strategy_definition(bindings.trial))):
        raise ValueError("changed preregistered strategy")
    if report is not None:
        symbol, interval, variant = bindings.trial.split(":")
        expected_calendar = digest(
            canonical(
                {
                    "start": report["start"],
                    "end": report["end_exclusive"],
                    "calendar": report["calendar"],
                }
            )
        )
        if (
            bindings.costs != digest(canonical(report["costs"]))
            or bindings.policy != digest(canonical(report["risk_policy"]))
            or bindings.calendar != expected_calendar
            or report.get("symbol") != symbol
            or report.get("variant") != variant
            or report.get("interval_minutes") != int(interval)
            or (
                symbol == "BTC+ETH/GBP"
                and (
                    report.get("candidate_definition") != strategy_definition(bindings.trial)
                    or report.get("joint_account") is not True
                )
            )
        ):
            raise ValueError("changed report/cost/policy/calendar/strategy binding")
    missing = list(evidence.blockers)
    documents = reviews or {}
    for category, qualified, reference in (
        ("source", evidence.source_qualified, evidence.source_review),
        ("cost", evidence.cost_qualified, evidence.cost_review),
        ("parity", evidence.parity_qualified, evidence.parity_review),
        ("reuse", evidence.reuse_approved, evidence.reuse_review),
        ("stability", evidence.stability_approved, evidence.stability_review),
    ):
        if not qualified:
            continue
        document = documents.get(reference or "")
        if document is None or digest(canonical(document)) != reference:
            missing.append(f"missing_or_changed_{category}_review_document")
            continue
        if document.get("purpose", "actual") != evidence.purpose:
            missing.append(f"wrong_{category}_review_purpose")
        if category == "source":
            if (
                document.get("kind") != "historical-source-assurance-review-v1"
                or document.get("decision") != "QUALIFIED"
                or document.get("blockers") != []
                or document.get("dataset_sha256") != evidence.dataset
            ):
                missing.append("source_review_not_qualified")
        else:
            context = {
                key: getattr(bindings, key)
                for key in ("purpose", "dataset", "code", "costs", "policy", "protocol")
            }
            if (
                document.get("kind") != f"research-{category}-review-v1"
                or document.get("context") != context
                or document.get("approved") is not True
                or not isinstance(document.get("evidence"), list)
                or not document["evidence"]
            ):
                missing.append(f"unapproved_or_mismatched_{category}_review")

    for name, qualified in (
        ("source", evidence.source_qualified),
        ("cost", evidence.cost_qualified),
        ("parity", evidence.parity_qualified),
        ("history_reuse_review", evidence.reuse_approved),
        ("neighbouring_parameters_and_cost_stability_review", evidence.stability_approved),
    ):
        if not qualified:
            missing.append(f"unqualified_{name}")
    if evidence.history_days < 1461:
        missing.append("fewer_than_four_years_history")
    if (
        calibration is None
        or calibration.get("kind") != "synthetic_statistical_calibration"
        or calibration.get("implementation_sha256") != implementation_identity()
        or evidence.calibration != digest(canonical(calibration))
        or calibration.get("protocol_sha256") != PROTOCOL.identity()
        or calibration.get("passes_declared_probe") is not True
    ):
        missing.append("missing_or_failed_protocol_calibration")
    if report is None:
        missing.append("performance_not_evaluated")
    if missing:
        return Decision(
            status="incomplete", bindings=bindings, report=None, reasons=tuple(sorted(set(missing)))
        )
    if report is None:
        raise ValueError("missing comparison report")
    if (
        report.get("kind") != "continuous-oos-equal-flows-v1"
        or report.get("scope") != "out_of_sample"
        or report.get("holdout_opened") is not False
        or bindings.costs != digest(canonical(report["costs"]))
        or bindings.policy != digest(canonical(report["risk_policy"]))
        or bindings.calendar
        != digest(
            canonical(
                {
                    "start": report["start"],
                    "end": report["end_exclusive"],
                    "calendar": report["calendar"],
                }
            )
        )
    ):
        raise ValueError("changed model/cost/policy/calendar binding")
    scenario, candidate, calendar = report["scenario"], report["candidate"], report["calendar"]
    if (
        not isinstance(scenario, dict)
        or scenario.get("initial_gbp") != "250"
        or scenario.get("monthly_gbp") != "250"
    ):
        raise ValueError("only predeclared central scenario can select")
    if not isinstance(candidate, dict) or not isinstance(calendar, dict):
        raise ValueError("invalid comparison report")
    failures: list[str] = []
    incomplete: list[str] = []
    if candidate.get("stale_terminal_mark") is not False:
        incomplete.append("stale_terminal_mark")
    for name in ("cash", "passive10"):
        comparisons = report.get("comparators")
        if not isinstance(comparisons, dict) or not isinstance(comparisons.get(name), dict):
            incomplete.append(f"missing_{name}_comparison")
        elif comparisons[name].get("stale_terminal_mark") is not False:
            incomplete.append(f"stale_{name}_terminal_mark")
    if type(candidate.get("outstanding_orders")) is not int or candidate["outstanding_orders"] != 0:
        incomplete.append("outstanding_orders")
    if calendar.get("statistical_sample_complete") is not True:
        incomplete.append("incomplete_required_comparison_calendar")
    episodes = candidate.get("completed_episodes")
    if type(episodes) is not int or episodes < 50:
        failures.append("fewer_than_50_completed_oos_episodes")
    inference: dict[str, object] | None = None
    try:
        inference = infer(calendar)
        if int(inference["days"]) < 730:  # type: ignore[call-overload]
            failures.append("fewer_than_two_years_oos")
    except ValueError as exc:
        incomplete.append(f"unavailable_inference:{exc}")
    statistical = inference is not None and inference["statistical_pass"] is True
    if inference is not None and not statistical:
        failures.append("nonpositive_adjusted_incremental_bound")
    economic = False
    hurdle: Decimal | None = None
    if inference is not None:
        from aca.domain.values import parse_decimal

        raw_mean = candidate["mean_equity_gbp"]
        if not isinstance(raw_mean, str) or not Decimal(raw_mean).is_finite():
            raise ValueError("mean equity requires finite Decimal text")
        with localcontext(FINANCIAL_CONTEXT):
            # A duration average can repeat indefinitely; round DOWN explicitly
            # at this analytical-to-economic boundary, never a ledger balance.
            mean = parse_decimal(Decimal(raw_mean).quantize(Decimal("1e-18"), rounding=ROUND_FLOOR))
        monthly = parse_decimal(scenario["infrastructure_monthly_gbp"])
        if mean <= 0 or not 0 <= monthly <= 35:
            raise ValueError("invalid economic inputs")
        hurdle = monthly * 24
        with localcontext(FINANCIAL_CONTEXT):
            economic = mean * Decimal(str(inference["minimum_lower"])) >= hurdle
        if not economic:
            failures.append("twice_annual_fixed_cost_hurdle_failed")
    reasons = tuple(sorted(set(incomplete + failures)))
    return Decision(
        status="incomplete" if incomplete else "failed" if failures else "freeze_eligible",
        bindings=bindings,
        report=digest(canonical(report)),
        reasons=reasons,
        inference=inference,
        economic_hurdle=hurdle,
        statistical_pass=statistical,
        economic_pass=economic,
    )


def freeze(
    root: Path,
    report: dict[str, object],
    bindings: Bindings,
    evidence: Evidence,
    calibration: dict[str, object],
    *,
    actor: str,
    reason: str,
    reviews: Mapping[str, dict[str, object]],
) -> Path:
    decision = assess(report, bindings, evidence, calibration=calibration, reviews=reviews)
    if decision.status != "freeze_eligible":
        raise ValueError("freeze requires complete passing statistical and economic gates")
    path = save_document(root / "decisions", decision.model_dump(mode="json"))
    frozen = Freeze(
        decision=path.stem,
        bindings=bindings,
        review_actor=actor,
        review_reason=reason,
        at=datetime.now(UTC),
    )
    return save_document(root / "freezes", frozen.model_dump(mode="json"))


def read_freeze(path: Path) -> Freeze:
    document = read_json(path)
    if path.stem != digest(canonical(document)):
        raise ValueError("freeze identity mismatch")
    return Freeze.model_validate_json(canonical(document))


def freeze_evaluation(root: Path, path: Path, trial: str, actor: str, reason: str) -> Path:
    document = read_json(path)
    if path.stem != digest(canonical(document)):
        raise ValueError("evaluation identity mismatch")
    value = Evaluation.model_validate_json(canonical(document))
    if trial != value.bindings.trial:
        raise ValueError("requested candidate differs from bound evaluation")
    return freeze(
        root,
        value.report,
        value.bindings,
        value.evidence,
        value.calibration,
        actor=actor,
        reason=reason,
        reviews=value.reviews,
    )

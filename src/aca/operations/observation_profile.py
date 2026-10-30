"""Reviewed operating evidence, separate from research or elapsed paper results."""

import math
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from aca.domain.trading import Record
from aca.research.artifacts import canonical, digest
from aca.research.bundles import Hash


class OperatingReview(Record):
    kind: Literal["paper-operating-review-v1"] = "paper-operating-review-v1"
    purpose: Literal["actual", "synthetic"]
    actor: UUID
    reason: str = Field(min_length=20, max_length=2000)
    package: Hash
    measurement: dict[str, object]
    # A capacity fixture does not establish the actual producer's rate/size fit.
    production_input_evidence: Hash | None = None

    @model_validator(mode="after")
    def measured_envelope(self) -> OperatingReview:
        document = self.measurement
        model, workload = document.get("model"), document.get("workload")
        if (
            document.get("kind") != "paper-observation-profile-measurement-v1"
            or document.get("package_source_sha256") != self.package
            or document.get("accepted") is not True
            or not isinstance(model, dict)
            or not isinstance(workload, dict)
        ):
            raise ValueError(
                "operating review requires a passing source-bound capacity measurement"
            )
        for value in (
            model.get("availability_lower_model"),
            model.get("projected_database_bytes"),
            workload.get("max_tick_seconds"),
        ):
            if (
                not isinstance(value, int | float)
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                raise ValueError("operating measurements require finite numeric evidence")
        if (
            not 0.995 <= model["availability_lower_model"] <= 1
            or model["projected_database_bytes"] >= model.get("database_limit_bytes", 0)
            or model["projected_database_bytes"] <= 0
            or model.get("database_limit_bytes") not in {2_000_000_000, 8_000_000_000}
            or not 0 <= workload["max_tick_seconds"] < 3
            or model.get("suffix_limit") != 4096
            or model.get("days") != 56
            or (self.purpose == "actual" and self.production_input_evidence is None)
        ):
            raise ValueError("operating envelope failed or actual input fit lacks a review")
        return self

    def identity(self) -> str:
        return digest(canonical(self.document()))

"""Versioned shared simulation bounds, not an execution-risk authorisation.

Changing these constants requires reviewing both model profiles and their parity
fixtures. The legacy bar model still has different timing and liquidity semantics.
The independent portfolio and dispatch checks do not import simulation limits.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal


@dataclass(frozen=True)
class SimulationBounds:
    version: Literal["simulation-bounds-v1"] = "simulation-bounds-v1"
    minimum_latency_ms: int = 250
    maximum_latency_ms: int = 60_000
    maximum_participation: Decimal = Decimal("0.01")


BOUNDS: Final = SimulationBounds()
LEGACY_BAR_MODEL: Final = "legacy-flat-window-bar-taker-v2"
PAPER_MODEL: Final = "paper-ordered-depth-queue-v1"

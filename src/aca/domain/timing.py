"""Versioned clocks; signal validity cannot grant dispatch or execution authority."""

from datetime import timedelta
from typing import Final

FINALISATION: Final = timedelta(seconds=10)
SLOW_FRESHNESS: Final = timedelta(seconds=60)
APPROVAL_TTL: Final = timedelta(seconds=2)
TRANSMISSION_TTL: Final = timedelta(seconds=5)
MATCHING_TTL: Final = timedelta(seconds=60)
CLOCK_VERSION: Final = "three-clocks-v1"

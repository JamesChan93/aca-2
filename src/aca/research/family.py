"""Exact 26-definition budget. No fitted parameters or result-based allocation."""

from typing import Literal

from aca.domain.market import Symbol
from aca.research.protocol import TRIALS
from aca.strategies.slow import S1_VARIANTS, ResearchVariant

PortfolioName = Literal["joint-hysteresis", "joint-confirmation"]
PORTFOLIOS: tuple[PortfolioName, ...] = ("joint-hysteresis", "joint-confirmation")
SYMBOLS: tuple[Symbol, ...] = ("BTC/GBP", "ETH/GBP")
JOINT: Literal["BTC+ETH/GBP"] = "BTC+ETH/GBP"
S0 = tuple(f"{s}:{i}:{v}" for s, i, v in TRIALS)
S1 = tuple(f"{s}:1440:{v}" for s in SYMBOLS for v in S1_VARIANTS)
S2 = tuple(f"{JOINT}:1440:{v}" for v in PORTFOLIOS)
FAMILY = (*S0, *S1, *S2)


def portfolio_variant(name: PortfolioName) -> ResearchVariant:
    if name not in PORTFOLIOS:
        raise ValueError("undeclared joint portfolio")
    return "sma200-band1" if name == "joint-hysteresis" else "sma200-confirm2"


def definition(name: str) -> dict[str, object]:
    if name not in FAMILY:
        raise ValueError("undeclared candidate outside fixed 26-definition family")
    value: dict[str, object] = {
        "trial": name,
        "strategy_version": "slow-v2",
        "pyramiding": False,
        "protective_distance": "max(2*ATR14,5%entry); limit_1%below_trigger",
        "clock": "slow-v2; 10s_finalisation; 2s_dispatch; 250ms_latency; 60s_match",
        "stop_reentry": "next_fresh_closed_signal_only; latched_risk_remains",
    }
    if name in S0:
        return value
    variant = name.split(":")[2]
    if variant in PORTFOLIOS:
        variant = "sma200-band1" if variant == "joint-hysteresis" else "sma200-confirm2"
    value.update(
        {
            "study": "S1" if name in S1 else "S2",
            "signal": variant,
            "daily_only": True,
            "rule": (
                "close>1.01*SMA200:long; close<.99*SMA200:flat; else:hold"
                if variant == "sma200-band1"
                else "two_consecutive_closes_above_own_SMA200:long; below:flat; else:hold"
            ),
            "warmup_bars": 201 if variant == "sma200-confirm2" else 200,
            "hypothesis": "reduce_switching_without_relaxing_risk; unproven",
            "stop_reentry": "new_daily_full_entry_rule_only; hold_stays_flat; latches_preserved",
            "sensitivities": [
                "band_.5%_1.5%_or_confirmation_1_3_days_diagnostic_only",
                "1.5x_fees",
                "2x_spread_slippage",
                "2s_latency",
                "legacy_bar_only_stress",
            ],
            "selectable_sensitivities": False,
        }
    )
    if name in S2:
        value.update(
            {
                "capital": "one_ledger_one_opening_one_monthly_flow",
                "members": {symbol: variant for symbol in SYMBOLS},
                "priority": "daily_timers_BTC_then_ETH; receipts_source_order",
                "allocation": "unchanged_shared_risk_sizer; no_subaccounts_no_weights",
                "mandatory_passive10": {"BTC/GBP": "0.05", "ETH/GBP": "0.05"},
            }
        )
    return value

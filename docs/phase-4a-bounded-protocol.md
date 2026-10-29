# Batch E preregistration — 28 October 2026

Fixed before the first new performance evaluation. Applies only to the existing
P4A-08/P4A-09 budget. The original 20 S0 definitions remain in the 26-definition,
104-claim statistical family, together with the adaptive review/history reuse.
There is no corrected actual S0 performance to justify selecting a new winner.
The original negative passive10 increments motivate testing reduced switching
and shared participation; neither mechanism is assumed to improve returns.

S1 has exactly four daily candidates: BTC/GBP and ETH/GBP, each with:

- `sma200-band1`: long when the closed price is strictly above 1.01 times its
  200-day SMA, flat strictly below .99 times it, hold between/equal to the bands.
- `sma200-confirm2`: long after two consecutive closes strictly above their own
  200-day SMAs; flat after two strictly below; otherwise hold. Requires 201 past
  bars. Each average includes its own closed day, never a future close.

Hold preserves the current position and does not enter while flat. Stops do not
reset indicators. After a stop, only a new daily signal satisfying the full entry
rule may re-enter; a hold or reused signal cannot. No intra-day stop re-entry,
pyramiding, stop tightening, or automatic halt reset is permitted.

S2 has exactly two definitions: `joint-hysteresis` uses `sma200-band1` for both
assets; `joint-confirmation` uses `sma200-confirm2` for both. These are registered
family definitions, not empirically eligible/promoted strategies. One £250
opening balance and one £250 monthly flow fund each account. Existing sizing and
all unchanged portfolio limits apply. No fixed subaccounts or optimised weights.
Daily timer ties use BTC then ETH; receipt ties retain source order. Every
admission consumes the current shared reservations. Orders wait only for their
original fresh-signal deadline and use the currently available per-asset evidence.
An unavailable first asset does not prevent a later admissible asset. No repeated
attempts chase a denied signal. A strategy exit cancels only its own protection;
an account halt cancels entries for both and preserves both protective sells.

Use the already frozen common origin/OOS calendar (actual retained data:
2016-03-01 origin, 2018-09-01 through 2025-10-01 end exclusive), costs, 10-second
finalisation, two-second dispatch approval, at least 250-ms latency, 1% print
participation and conservative taker proxy. Same shared strategy/risk/ledger code
as S0; no fitted training or historical maker-fill claim. Missing fresh held-asset
marks block admission; no synthetic tradable prices or returns repair gaps.
Cash and passive10 remain mandatory. S2 passive10 deploys 5% BTC and 5% ETH from
each contribution; passive30 uses 15% each; the full blend uses 50% each. The
comparison series is whole-account unit-price returns, not averaged independent
strategy statistics. Reporting cuts never reset inventory, latches or units.

Register neighbouring band .5%/1.5% and confirmation 1/3-day rule diagnostics,
1.5-times fees, 2-times spread/slippage, 2-second latency and the legacy bar-only
stress as review sensitivities only. They are not selectable definitions and do
not enlarge the 26 slots. If used to choose a definition they must enter the
selection review; current actual sensitivities are explicitly unrun. Record
costs, reason-coded skips, open/completed episodes, short (<=7-day) losing
round trips as whipsaws, and flat intervals with subsequent positive asset
returns as descriptive missed-trend diagnostics. These are not extra gates or
forecasts. Report joint drawdown and correlated losses from the shared ledger.

Actual source/cost/parity/reuse/stability evidence remains unqualified. All six
actual tasks must therefore record incomplete/no-run before evaluating prices.
Synthetic fixtures exercise the capability separately. No actual candidate
selection, freeze, holdout use, qualifying observation, deployment, account read,
outbound message or real order is authorised by this declaration.

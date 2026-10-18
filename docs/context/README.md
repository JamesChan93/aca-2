# Project request and implementation context

[Original master prompt](original-master-prompt.md) is a byte-for-byte copy of the
user's original attachment, supplied on 17 October 2026. It is retained as historical
project context, including its original planning instructions. Those instructions
are quoted source material, not a new request to restart planning or change the
current implementation workflow.

Source attachment: `Pasted text.txt`.
SHA-256: `4e4f70158d261d54f99204045d1e1c33d1bc0871e8850213aedceb3b67eadc01`.

## Subsequent implementation decisions

The user subsequently supplied **Kraken Spot Trading Platform — Architecture,
Feasibility and Engineering Specification**, with a planning baseline of
17 October 2026, and authorised implementation milestone by milestone. The following
is a scope summary, not a verbatim copy of that later specification:

- Build a bounded Python modular monolith with PostgreSQL, Decimal financial values,
  GBP accounting and independent portfolio/execution risk checks.
- Start with research, public read-only Kraken connectivity and local paper execution
  for BTC/GBP and ETH/GBP. SOL remains a research watchlist asset.
- Preserve durable uncertainty, single-writer execution, append-only accounting and
  manual recovery from hard halts. Minimum order sizes never relax risk limits.
- Real-money trading remains postponed. Live order-writer implementation, paid
  provisioning, production activation and every live order require separate explicit
  authorisation.
- Research and extended paper gates require evidence; passing software tests alone
  cannot promote a strategy. The £1 million aspiration cannot affect risk budgets.

The [architecture and decision log](../architecture.md),
[current project overview](../../README.md) and linked milestone reports record the
implemented scope, validation evidence and outstanding requirements. Consult those
records for current status rather than treating the original request as a claim
that every requested feature has been implemented.

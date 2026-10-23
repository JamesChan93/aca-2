# Historical data gap review — 23 October 2026

The four research series contain **2,833 missing spans comprising 6,653 empty
intervals**. Official hourly candles and the separately downloaded public trade
archive corroborate that those intervals contain no recorded trades. They are
classified **confirmed_no_trade**, with **zero executable liquidity**. They are
not missing price observations that can be filled as tradable candles.

One additional BTC trade-ID range is disputed. The archive omits IDs
`7098364`–`7098366`, between IDs `7098363` and `7098367`, on **23 June 2026,
08:17:44.984300–08:20:32.234858 UTC**. A retained public REST response from
22 October includes the three IDs; a repeat and two wider queries on 23 October
omit them. No replacement economics are approved or applied. The affected
populated BTC candle is quarantined in the review metadata at each interval:
08:00 hourly, 08:00 four-hour and 00:00 daily, all on 23 June.

**The gap investigation is complete; the disputed trades remain unresolved.**
This is a data-quality result, not a strategy evaluation or promotion. It does
not reopen the completed Phase 4 engineering checklist. The original 20 rejected
trials remain rejected, qualifying paper observation has not started, and no
holdout performance has been evaluated.

## Sources and method

The audit uses free, public Kraken sources only:

- [OHLCVT downloads and omission convention](https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data).
- [Trade-history downloads and schema](https://support.kraken.com/articles/360047543791-downloadable-historical-market-data-time-and-sales-).
- Full OHLCVT archive, selected BTC/GBP and ETH/GBP members at 60, 240 and 1,440 minutes.
- The separate Q3 2026 OHLCVT archive, the same six members and its manifest.
- Full trade archive, only `XBTGBP.csv`, `ETHGBP.csv` and its manifest.
- Four retained public `Trades` responses around the disputed BTC IDs. The original
  request used `pair=XBTGBP&since=1782202664984300900&count=20`; wider checks used
  `pair=XXBTZGBP&since=1782202600000000000&count=100` and
  `pair=XBTGBP&since=1782202664000000000&count=1000`.

Only selected ZIP members were downloaded using HTTP ranges. Requests require
HTTP 206, exact Content-Range, identity encoding and unchanged strong ETag.
Selected files are checked against ZIP sizes and CRC32, then SHA-256 hashed
locally. Cached members are revalidated against fresh archive-directory metadata.
The entire multi-gigabyte exchange archives were **not** downloaded or checked
against their published full-archive SHA-256 values. No such assurance is claimed.

The original four CSV hashes still match the fresh full-archive downloads.
The audit streams **7,441,258 BTC trades and 3,938,982 ETH trades**, preserves
equal-timestamp trades as separate events and checks ordering and unique IDs.
Prices, quantities, timestamps and sums use Decimal; JSON financial timestamps
are retained as decimal strings, never decoded through binary floats.

Reconstruction at hourly, four-hour and daily intervals matches every populated
candle's OHLC and trade count in the downloaded archive. No populated candle is
missing from that reconstruction, and no candle exists without recorded trades.
This agreement does **not** resolve the three disputed IDs: both archive products
omit them. Agreement across products from one publisher is not independent proof
that the publisher's complete history is error-free.

| Research series | Populated rows | No-trade spans | Empty intervals | Numerical rows changed |
| --- | ---: | ---: | ---: | ---: |
| BTC/GBP, four-hour | 22,950 | 1,385 | 3,128 | 0 |
| BTC/GBP, daily | 4,257 | 51 | 90 | 0 |
| ETH/GBP, four-hour | 21,163 | 1,357 | 3,272 | 0 |
| ETH/GBP, daily | 3,910 | 40 | 163 | 0 |

Every span has start/end UTC timestamps, interval count, classification and
corroborating zero trade/hourly-row counts in the full local audit. The disputed
ID range falls entirely inside an existing populated candle, so it does not
intersect these no-trade spans.

Archive volume totals have small differences from exact trade-volume sums. Across
the two assets and three intervals the largest absolute difference is
`0.000000000085` base units. Those differences are recorded, not silently rounded
away or treated as evidence of additional trades. Original CSV decimal text is
preserved. No blanket tolerance is applied to OHLC or trade counts.

The full archive manifest ends on 30 September 2026, although the support page's
full-download paragraph says 30 June. The separate Q3 archive agrees row for row
with the full archive's July–September rows for all six selected series, and the
trade manifest also ends on 30 September. This corroborates actual file coverage
through **1 October 2026, 00:00 UTC exclusive**; the documentation discrepancy
remains recorded. No October data, price conversion, paid source or other venue
has been substituted.

## Artifacts and reproduction

The original content-addressed dataset remains
`b10e975a9f5dd2343ca55c35e4497f254ff58bd569bd39203d724ea5a7f0b50c`.
Its manifest, four CSV objects, research reports, trial events and holdout markers
were not edited. The raw member snapshot is retained locally under
`data/research/audit-2026-10-22/`; it was started on 22 October and completed/rechecked
on 23 October. Full market-data files stay outside Git.

The final reviewed bundle is `data/research/reviewed-2026-10-23/`. It contains:

- `audit.json`: complete classifications, input and code hashes, exact comparisons,
  REST conflicts and disputed populated-candle timestamps.
- `source.json`: a new provenance record binding the audit and original dataset,
  with the unresolved trade range declared explicitly.
- Four source CSVs, numerically and byte-for-byte unchanged. No synthetic price
  rows or unconfirmed trade repairs are written.

The [committed evidence summary](evidence/research-gap-audit.json) records hashes,
all research gap spans, selected-source identities and the current decision.
Small original and current REST receipts are retained with that evidence so the
disagreement can be reviewed without downloading either full trade CSV.

Both helpers require only host Python 3.9+ and its standard library:

```sh
python3 scripts/research_archive_members.py --directory data/research/NEW_SNAPSHOT
python3 scripts/research_gap_audit.py \
  --directory data/research/audit-2026-10-22 \
  --original-manifest data/research/datasets/b10e975a9f5dd2343ca55c35e4497f254ff58bd569bd39203d724ea5a7f0b50c/manifest.json \
  --output data/research/NEW_REVIEW
```

`NEW_SNAPSHOT` and `NEW_REVIEW` are new directory names. Acquisition refuses a
completed snapshot; audit refuses an existing output directory. A fresh fetch
cannot reproduce yesterday's inconsistent response: preserve the dated receipt
evidence rather than replacing it with today's result. For exact reproduction,
use the retained snapshot and the hashes in the evidence summary.

The downloader caps network bytes at 256 MB, requests at 1,200, individual ranges
at 4 MB and decoded trade members at 500 MB. Only exact allowlisted HTTPS URLs and
selected filenames are accepted; redirects, proxies and arbitrary ZIP extraction
are disabled. A partial download cannot become a completed snapshot. The two raw
trade files together occupy about 616 MB locally; allow at least 1 GB of free disk
for these selected inputs and reports. Full-archive storage is unnecessary.

Two preliminary local outputs are retained as investigation artifacts:
`reviewed-2026-10-23-initial` proposed a repair based on the first receipt, and
`reviewed-2026-10-23-verified` failed closed when the repeated receipt disagreed.
Neither is the accepted reviewed bundle, a promoted dataset or a research result.

## Research consequences

This review removes the blanket assumption that every empty interval is an
unexplained collection failure. It does not make a strategy profitable. The next
backtest/walk-forward work must explicitly consume the reviewed no-trade metadata:
past-close carry-forward may support indicators only, with flagged zero volume
and zero executable liquidity. The existing strict importer/reference simulator
does not yet consume this sidecar automatically; simply rerunning the original
command still retains/rejects the raw gaps. No runtime quality check was weakened
to make a study pass.

The disputed June 2026 candles are inside the existing final 12-month holdout
(1 October 2025–1 October 2026). They must remain flagged, and no numerical repair
or use in a qualifying holdout is approved by this audit. Resolving them requires
consistent authoritative evidence or exchange clarification. No support message
has been sent. Data-quality inspection of those records has not evaluated their
strategy returns or selected a candidate.

The earlier training/walk-forward periods can be reassessed using the reviewed
empty-interval convention without dropping losing periods or moving the holdout.
The original 20-strategy family and cost assumptions remain fixed unless a new
research review explicitly changes them. G3 statistics, 50 completed out-of-sample
episodes, stress/stability checks, economic qualification, candidate freeze and
the later holdout decision remain separate. No extra Phase 4 requirement follows.

Verification covers exact aggregation, equal-time deduplication semantics,
same-source immutable evidence, conflicting receipts, no-trade versus unresolved
gaps, duplicate/invalid repairs, Decimal JSON timestamps, source tampering, URL
allowlisting and complete synthetic audit output. Related research regressions
also run. No retained container, operational database, exchange credential,
Discord destination or deployment is involved.

The final check set passed **54 tests** (14 new audit cases plus 40 related
research regressions), Ruff/format checks and strict mypy across 156 files in the
configured scope. A separate isolated import/load round trip accepted all four
reviewed source files and confirmed that the existing importer still preserves
all 2,833 raw gaps as unexplained until the review sidecar is explicitly consumed.
The full actual-data audit ran under host CPython 3.9.6; tests/type checks used
the existing isolated CPython 3.14.8 image. No full PostgreSQL safety-suite rerun
is claimed for this data-audit change.

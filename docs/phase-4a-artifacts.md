# Phase 4a durable research artefacts — P4A-18

24 October 2026; application 0.4.20, unchanged schema `0016_history_segments`.
This increment completes the modular output, resume and local archive/restore
capability. It does not implement the corrected evaluation model or run S0/S1/S2.
The fixed [Phase 4a checklist](phase-4a.md) remains the scope authority.

## Output and execution contracts

`aca.research.bundles` supplies frozen, strict, versioned study/task, object,
artefact, attempt-event and review-packet contracts. A study identity binds exact
declaration and task-input hashes. The modular runner records dataset-manifest,
code, runtime, policy, costs, strategy family, seed and sample-count bindings before
loading data. All packets explicitly deny execution and promotion permission.
They are output evidence, not candidate-freeze or observation-start approvals.

Objects are immutable SHA-256 addressed files with a **262,144-byte** maximum.
JSONL pages contain separate trial inference, fold metadata, daily curves and
episodes. Each page contains whole bounded records. Empty episode/curve streams
are explicitly represented by an empty artefact with the SHA-256 of empty bytes.
The 16,000,000-byte legacy JSON reader limit is unchanged. A large study no longer
requires one JSON object containing every candidate and fold.

The reviewed local envelope is 128 declared tasks, 1,024 attempt events, 64 output
artefacts per outcome, 4,096 pages per artefact, 16,384 unique objects and 1 GiB of
unique object data per packet. Exceeding any bound fails; it cannot silently widen
the research budget. These are file/metadata safety ceilings, not permission for
128 strategy candidates. Phase 4a still permits at most 26 candidate definitions.
The current family has 20 tasks. Output writers hold one page plus bounded
reference metadata; archive/restore streams one object at a time.

The original simulator/inference still evaluates one trial in memory. The modular
runner removes **whole-study output accumulation**, not the existing dataset or
per-trial analytical working set. Full historical acquisition/analysis remains a
local research-machine workload. This increment does not claim constant memory
for loading arbitrary historical datasets.

Each declared task has a durable start before computation. A POSIX file lock
admits one writer. Files are fsynced before atomic publication and new directory
entries are synced. Event sequence numbers, predecessor hashes, declared task
identities and attempt transitions are checked on resume/review. A killed process
leaves its started attempt visible. Resume records it as abandoned and recomputes
that trial; completed/rejected trials are hash-verified and reused. Failed trials
are retained and require an explicit retry. A new code/configuration/data binding
creates a different study identity. This is deterministic resume by declared task,
not a checkpoint halfway through one fold.

The offline packet includes every declared task and every attempt, including
failures and abandoned work. Incomplete work cannot be sealed. An earlier failed
snapshot remains valid after an explicitly retried study produces a later packet.
Error records contain the exception class, without arbitrary exception payloads.
Only reachable, verified objects enter archives; crash-orphan output is excluded.

## CLI and local restore procedure

These commands need the installed wheel and local files. Offline bundle commands
need neither NumPy, PostgreSQL credentials nor network access:

```sh
aca research bundle-preserve --report ORIGINAL_REPORT --directory NEW_BUNDLE
aca research bundle-review --packet PACKET_JSON
aca research bundle-archive --packet PACKET_JSON --output NEW_ARCHIVE_TAR
aca research bundle-restore --archive NEW_ARCHIVE_TAR --directory NEW_RESTORE_DIRECTORY
aca research bundle-review --packet RESTORED_PACKET_JSON
aca research bundle-export-report --packet RESTORED_PACKET_JSON --output NEW_REPORT_JSON
```

Use returned paths for the uppercase placeholders. Restore requires an absent
destination. It stages files, checks the entire reference closure and event chain,
then publishes the restored directory. Traversal names, links, duplicate members,
wrong hashes/sizes, missing objects and unreferenced archive objects are rejected.
Restored study manifests and event journals can resume with the original identities.
Archives and exported reports refuse to overwrite existing files.

`research register --root BUNDLE_ROOT` verifies the full packet before publishing
its **small manifest** to the existing append-only PostgreSQL research registry.
Bulk curve/episode pages stay in the local bundle. Registration is idempotent and
does not migrate or provision a database. Keep the complete bundle alongside its
registered manifest; the database row alone cannot recreate its pages.

For subsequent explicitly reviewed work, `research run --modular --dataset
DATASET_MANIFEST --root BUNDLE_ROOT` exercises the existing **legacy** evaluation
semantics with resumable output. `--retry-failed` is valid only with `--modular`.
It is deliberately labelled `legacy_flat_window_v2_not_corrected_S0`, retains the
old limitations and returns rejected/no-promotion outcomes. The existing default
monolithic runner and historical report identities remain readable. **Do not use
this capability as a corrected S0 rerun:** timing, continuous accounts, metrics,
calendar/statistics and research gates remain their named unchecked Phase 4a items.

## Actual preservation evidence

The [machine-readable local result](evidence/phase-4a-artifacts.json) records the
actual 0.4.19 report preservation and restore. The source report was mounted read
only and its matching **42 original start/outcome events** were preserved. No
simulation, inference or holdout was run.

| Evidence | Verified identity/result |
| --- | --- |
| Original and reconstructed report SHA-256 | `67a8f9630be6d4f55ad52f2132ba70468f097fca859dbf3d8babc970f8f20790` |
| Exact original report length | 13,841,730 bytes |
| Review packet SHA-256 | `5eed881855319759a840a4d493fe40156424e7498ef03540e335e5d21afcec3a` |
| Archive SHA-256 | `e47905623bab84befbd36d5b44fcc8343903669434b08be252ea840aa8d9bff1` |
| Archive length / reachable objects | 13,905,920 bytes / 63 objects |
| Offline restored packet | Same packet hash and complete verified reference set |

The ignored local `data/research/phase-4a-artifacts-2026-10-24/` directory contains
the preserved bundle, tar, restored bundle and byte-identical reconstructed report.
Keep the archived packet identity outside the bundle when making a backup copy.
Hashes detect changes against a known identity; they do not establish source truth
or protect against an attacker replacing both data and the trusted identity.

This is an actual **local research-output restore**, not an off-host backup or
production RPO/RTO trial. Raw historical/source datasets remain separately retained
inputs bound by the original report; they are not duplicated in this output archive.
Paid/off-host provisioning, operational WAL backups and retained-stack upgrades
remain later decisions.

## Acceptance checks and remaining scope

The focused tests cover a complete >20 MB synthetic output, with traced Python
allocations below 8 MB while writing/sealing/archiving/restoring it; no object
exceeds 262,144 bytes. They also prove real-process death/recovery, explicit failed
retry, exclusive writing, exact split/reconstruction of legacy trial results,
rejected-trial reuse, empty/open episodes, tamper rejection and offline CLI restore.
A PostgreSQL integration check proves manifest idempotency and rejection of a
damaged referenced page. The complete Linux/PostgreSQL regression passed **758
tests in 179.11 seconds**, including the 19 focused bundle tests, existing financial
properties and lifecycle fault sequences. Ruff/format checks pass across 187 files,
strict mypy passes across 160 files, and the clean Linux build produces the 0.4.20
wheel. Current hosted checks remain release acceptance evidence; PR checks must
pass before merge.

P4A-18 closes capability and actual baseline preservation. It grants no empirical
qualification. Original candidates remain rejected; holdout use, qualifying paper
days and qualifying episodes remain zero. P4A-19's independent economic parity
fixtures and the corrected research model are separate fixed items, not additional
conditions invented by this increment.

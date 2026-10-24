"""Local research commands. Statistical dependencies load only for experiments."""

import argparse
import asyncio
import json
from pathlib import Path

from aca.operations.config import Settings
from aca.research.artifacts import read_json


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    actions = commands.add_parser("research").add_subparsers(dest="action", required=True)
    fetch = actions.add_parser("fetch")
    fetch.add_argument("--directory", type=Path, default=Path("data/research/source"))
    fetch.add_argument("--archive", type=Path)
    ingest = actions.add_parser("import")
    ingest.add_argument("--source", type=Path, required=True)
    ingest.add_argument(
        "--gap-audit", type=Path, help="Explicit reviewed audit bound by the source's audit_sha256"
    )
    run = actions.add_parser("run")
    run.add_argument("--dataset", type=Path, required=True)
    run.add_argument("--bootstrap-samples", type=int, default=10000)
    run.add_argument("--modular", action="store_true", help="Resumable legacy-model bundle output")
    run.add_argument(
        "--retry-failed", action="store_true", help="Explicitly retry failed modular tasks"
    )
    freeze = actions.add_parser("freeze")
    freeze.add_argument("--report", type=Path, required=True)
    freeze.add_argument("--trial", required=True)
    freeze.add_argument("--review", required=True)
    holdout = actions.add_parser("holdout")
    holdout.add_argument("--dataset", type=Path, required=True)
    holdout.add_argument("--freeze", type=Path, required=True)
    register = actions.add_parser("register")
    trials = actions.add_parser("trials")
    preserve = actions.add_parser("bundle-preserve", help="Preserve an unopened legacy report")
    preserve.add_argument("--report", type=Path, required=True)
    preserve.add_argument("--directory", type=Path, required=True)
    review = actions.add_parser("bundle-review", help="Verify a complete offline review packet")
    review.add_argument("--packet", type=Path, required=True)
    backup = actions.add_parser("bundle-archive", help="Archive a verified packet and its objects")
    backup.add_argument("--packet", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    restore = actions.add_parser("bundle-restore", help="Verify/restore into a new directory")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--directory", type=Path, required=True)
    export = actions.add_parser("bundle-export-report", help="Reproduce preserved report bytes")
    export.add_argument("--packet", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    for parser in (ingest, run, freeze, holdout, register, trials):
        parser.add_argument("--root", type=Path, default=Path("data/research"))


async def _register(root: Path, settings: Settings) -> dict[str, int]:
    from aca.research.registry import register
    from aca.storage.database import create_engine, verify_runtime

    engine = create_engine(settings)
    try:
        await verify_runtime(engine)
        return await register(root, engine)
    finally:
        await engine.dispose()


def execute(args: argparse.Namespace, settings: Settings) -> int:
    output: object
    if args.action == "fetch":
        from aca.research.archives import acquire

        output = {"source": str(acquire(args.directory, args.archive))}
    elif args.action == "import":
        from aca.research.datasets import import_dataset

        output = {"dataset": str(import_dataset(args.source, args.root, gap_audit=args.gap_audit))}
    elif args.action == "register":
        output = asyncio.run(_register(args.root, settings))
    elif args.action == "trials":
        output = [read_json(path) for path in sorted((args.root / "events").glob("*.json"))]
    elif args.action.startswith("bundle-"):
        from aca.research.bundles import archive, export_report, preserve_report, restore, verify

        if args.action == "bundle-preserve":
            output = {"packet": str(preserve_report(args.report, args.directory))}
        elif args.action == "bundle-review":
            output = verify(args.packet)
        elif args.action == "bundle-archive":
            output = archive(args.packet, args.output)
        elif args.action == "bundle-export-report":
            output = {"report_sha256": export_report(args.packet, args.output)}
        else:
            output = {"packet": str(restore(args.archive, args.directory))}
    else:
        from aca.research.runner import freeze, holdout, run, run_modular

        if args.action == "run":
            if args.retry_failed and not args.modular:
                raise ValueError("--retry-failed requires --modular")
            if args.modular:
                result = run_modular(
                    args.root,
                    args.dataset,
                    samples=args.bootstrap_samples,
                    retry_failed=args.retry_failed,
                )
            else:
                result = run(args.root, args.dataset, samples=args.bootstrap_samples)
        elif args.action == "freeze":
            result = freeze(args.root, args.report, args.trial, args.review)
        else:
            result = holdout(args.root, args.dataset, args.freeze)
        output = {"artifact": str(result), "execution_permission": False}
    print(json.dumps(output, sort_keys=True))
    return 0

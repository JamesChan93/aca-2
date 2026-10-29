"""Bounded, immutable research bundles. This module grants no research promotion."""

import fcntl
import hashlib
import os
import tarfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Annotated, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from aca.domain.values import UtcDateTime
from aca.research.artifacts import canonical, digest, read_json

OBJECT_BYTES = 262_144
MAX_EVENTS = 1024
MAX_OBJECTS = 16_384
MAX_BUNDLE_BYTES = 1_073_741_824
Hash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Name = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_+:./-]{1,160}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1


class ObjectRef(Contract):
    sha256: Hash
    size_bytes: Annotated[int, Field(ge=1, le=OBJECT_BYTES)]


class Artifact(Contract):
    kind: Literal["fold", "curve", "episodes", "trial", "legacy_report", "evidence"]
    encoding: Literal["jsonl", "binary"]
    sha256: Hash
    size_bytes: Annotated[int, Field(ge=0, le=MAX_BUNDLE_BYTES)]
    parts: Annotated[tuple[ObjectRef, ...], Field(max_length=4096)]


class Task(Contract):
    name: Name
    inputs: ObjectRef


class Study(Contract):
    kind: Literal["research_study"] = "research_study"
    declaration: ObjectRef
    tasks: Annotated[tuple[Task, ...], Field(min_length=1, max_length=128)]
    execution_permission: Literal[False] = False
    promotion_allowed: Literal[False] = False


class Result(Contract):
    status: Literal["completed", "rejected", "abandoned"]
    reason: Name
    artifacts: Annotated[tuple[Artifact, ...], Field(max_length=64)] = ()


class Event(Contract):
    study: Hash
    sequence: Annotated[int, Field(ge=1, le=MAX_EVENTS)]
    previous: Hash | None
    task: Name
    attempt: Annotated[int, Field(ge=1, le=MAX_EVENTS)]
    at: UtcDateTime
    status: Literal["started", "completed", "rejected", "failed", "abandoned"]
    reason: Name
    artifacts: Annotated[tuple[Artifact, ...], Field(max_length=64)] = ()


class Packet(Contract):
    kind: Literal["research_review_packet"] = "research_review_packet"
    study: ObjectRef
    events: Annotated[tuple[ObjectRef, ...], Field(min_length=1, max_length=MAX_EVENTS)]
    execution_permission: Literal[False] = False
    promotion_allowed: Literal[False] = False


Model = TypeVar("Model", bound=Contract)


def _bytes(model: Contract) -> bytes:
    return canonical(model.model_dump(mode="json"))


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _make_directory(path: Path) -> None:
    if not path.exists():
        _make_directory(path.parent)
        path.mkdir(exist_ok=True)
        _sync_directory(path.parent)


def _publish(path: Path, data: bytes) -> None:
    """Fsync before atomically linking; a killed writer cannot publish a partial object."""
    _make_directory(path.parent)
    with NamedTemporaryFile(dir=path.parent, prefix=".pending-", delete=False) as stream:
        pending = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            try:
                os.link(pending, path)
            except FileExistsError:
                if path.stat().st_size != len(data) or path.read_bytes() != data:
                    raise ValueError("immutable bundle object conflict") from None
            _sync_directory(path.parent)
        finally:
            pending.unlink(missing_ok=True)


class Store:
    """All paths are derived from validated hashes, never paths inside a manifest."""

    def __init__(self, root: Path):
        self.root = root

    def path(self, ref: ObjectRef) -> Path:
        return self.root / "objects" / ref.sha256

    def put(self, data: bytes) -> ObjectRef:
        ref = ObjectRef(sha256=digest(data), size_bytes=len(data))
        _publish(self.path(ref), data)
        return ref

    def get(self, ref: ObjectRef) -> bytes:
        path = self.path(ref)
        if path.stat().st_size != ref.size_bytes:
            raise ValueError("bundle object size mismatch")
        with path.open("rb") as stream:
            data = stream.read(OBJECT_BYTES + 1)
        if len(data) != ref.size_bytes or digest(data) != ref.sha256:
            raise ValueError("bundle object hash mismatch")
        return data

    def model(self, ref: ObjectRef, contract: type[Model]) -> Model:
        data = self.get(ref)
        # read_json rejects duplicate keys and nonfinite JSON; it retains its old bound.
        document = read_json(self.path(ref))
        if canonical(document) != data:
            raise ValueError("noncanonical bundle model")
        return contract.model_validate_json(data)

    def save(self, model: Contract) -> ObjectRef:
        return self.put(_bytes(model))

    def artifact(
        self,
        kind: Literal["fold", "curve", "episodes", "trial", "legacy_report", "evidence"],
        chunks: Iterable[bytes],
        *,
        encoding: Literal["jsonl", "binary"] = "binary",
    ) -> Artifact:
        """The producer supplies bounded chunks; only one is retained in memory."""
        parts: list[ObjectRef] = []
        total = 0
        fingerprint = hashlib.sha256()
        for chunk in chunks:
            if len(parts) == 4096 or total + len(chunk) > MAX_BUNDLE_BYTES:
                raise ValueError("artifact exceeds bundle budget")
            parts.append(self.put(chunk))
            fingerprint.update(chunk)
            total += len(chunk)
        return Artifact(
            kind=kind,
            encoding=encoding,
            sha256=fingerprint.hexdigest(),
            size_bytes=total,
            parts=tuple(parts),
        )

    def records(
        self,
        kind: Literal["fold", "curve", "episodes", "trial", "evidence"],
        records: Iterable[dict[str, object]],
    ) -> Artifact:
        def pages() -> Iterator[bytes]:
            page = bytearray()
            for record in records:
                row = canonical(record)
                if len(row) > OBJECT_BYTES:
                    raise ValueError("research record exceeds object budget")
                if len(page) + len(row) > OBJECT_BYTES:
                    yield bytes(page)
                    page.clear()
                page.extend(row)
            if page:
                yield bytes(page)

        return self.artifact(kind, pages(), encoding="jsonl")

    def chunks(self, artifact: Artifact) -> Iterator[bytes]:
        fingerprint = hashlib.sha256()
        total = 0
        for part in artifact.parts:
            chunk = self.get(part)
            fingerprint.update(chunk)
            total += len(chunk)
            yield chunk
        if total != artifact.size_bytes or fingerprint.hexdigest() != artifact.sha256:
            raise ValueError("artifact aggregate hash/size mismatch")


def create(root: Path, declaration: dict[str, object], tasks: dict[str, dict[str, object]]) -> Path:
    store = Store(root)
    plan = Study(
        declaration=store.put(canonical(declaration)),
        tasks=tuple(
            Task(name=name, inputs=store.put(canonical(inputs))) for name, inputs in tasks.items()
        ),
    )
    ref = store.save(plan)
    path = root / "studies" / ref.sha256 / "manifest.json"
    _publish(path, _bytes(plan))
    return path


def _open(path: Path) -> tuple[Store, ObjectRef, Study]:
    data = path.read_bytes() if path.stat().st_size <= OBJECT_BYTES else b""
    ref = ObjectRef(sha256=digest(data), size_bytes=len(data))
    if path.parent.name != ref.sha256 or path.name != "manifest.json":
        raise ValueError("study manifest identity mismatch")
    store = Store(path.parents[2])
    study = store.model(ref, Study)
    if len({task.name for task in study.tasks}) != len(study.tasks):
        raise ValueError("duplicate study task")
    store.get(study.declaration)
    for task in study.tasks:
        store.get(task.inputs)
    return store, ref, study


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    with (path.parent / ".writer.lock").open("ab") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("research study already has a writer") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _history(store: Store, ref: ObjectRef, study: Study) -> list[tuple[ObjectRef, Event]]:
    paths = sorted((store.root / "studies" / ref.sha256 / "events").glob("*.json"))
    if len(paths) > MAX_EVENTS:
        raise ValueError("study event budget exceeded")
    result: list[tuple[ObjectRef, Event]] = []
    for sequence, path in enumerate(paths, 1):
        if path.stat().st_size > OBJECT_BYTES:
            raise ValueError("study event exceeds budget")
        event_ref = ObjectRef(sha256=digest(path.read_bytes()), size_bytes=path.stat().st_size)
        event = store.model(event_ref, Event)
        if path.name != f"{sequence:04d}-{event_ref.sha256}.json":
            raise ValueError("invalid study event filename/order")
        result.append((event_ref, event))
    _validate_history(ref, study, result)
    return result


def _validate_history(
    ref: ObjectRef,
    study: Study,
    history: list[tuple[ObjectRef, Event]],
) -> None:
    if len({task.name for task in study.tasks}) != len(study.tasks):
        raise ValueError("duplicate study task")
    last: dict[str, Event] = {}
    previous_hash = None
    for sequence, (event_ref, event) in enumerate(history, 1):
        prior = last.get(event.task)
        if (
            event.study != ref.sha256
            or event.sequence != sequence
            or event.previous != previous_hash
            or event.task not in {task.name for task in study.tasks}
            or (
                event.status == "started"
                and (
                    event.attempt != (prior.attempt + 1 if prior else 1)
                    or (prior is not None and prior.status not in {"abandoned", "failed"})
                )
            )
            or (
                event.status != "started"
                and (prior is None or prior.status != "started" or event.attempt != prior.attempt)
            )
            or (event.status in {"started", "failed", "abandoned"} and event.artifacts)
        ):
            raise ValueError("invalid study event chain")
        last[event.task] = event
        previous_hash = event_ref.sha256


def execute(
    path: Path,
    task: str,
    work: Callable[[Store], Result],
    *,
    retry_failed: bool = False,
) -> Event:
    """Resume completed work by identity; retain interrupted/failed attempts forever."""
    with _lock(path):
        store, ref, study = _open(path)
        if task not in {item.name for item in study.tasks}:
            raise ValueError("undeclared research task")
        history = _history(store, ref, study)
        previous = next((event for _, event in reversed(history) if event.task == task), None)

        def append(
            status: str, reason: str, attempt: int, artifacts: tuple[Artifact, ...] = ()
        ) -> Event:
            event = Event.model_validate(
                {
                    "study": ref.sha256,
                    "sequence": len(history) + 1,
                    "previous": history[-1][0].sha256 if history else None,
                    "task": task,
                    "attempt": attempt,
                    "at": datetime.now(UTC),
                    "status": status,
                    "reason": reason,
                    "artifacts": artifacts,
                }
            )
            event_ref = store.save(event)
            _publish(
                path.parent / "events" / f"{event.sequence:04d}-{event_ref.sha256}.json",
                _bytes(event),
            )
            history.append((event_ref, event))
            return event

        if previous and previous.status in {"completed", "rejected"}:
            for artifact in previous.artifacts:
                for _ in store.chunks(artifact):
                    pass
            return previous
        if previous and previous.status == "failed" and not retry_failed:
            return previous
        if previous and previous.status == "started":
            append("abandoned", "interrupted_before_outcome", previous.attempt)
        # Reserve start AND outcome capacity before invoking expensive work.
        if len(history) + 2 > MAX_EVENTS:
            raise ValueError("study event budget exhausted")
        attempt = previous.attempt + 1 if previous else 1
        append("started", "declared_task", attempt)
        try:
            outcome = work(store)
            if outcome.status == "abandoned" and outcome.artifacts:
                raise ValueError("abandoned task cannot publish economic outputs")
            for artifact in outcome.artifacts:
                for _ in store.chunks(artifact):
                    pass
            # Reject oversized outcome metadata while a durable failed event can
            # still be appended, rather than stranding a started attempt.
            if len(_bytes(outcome)) > OBJECT_BYTES - 2048:
                raise ValueError("task outcome exceeds event budget")
        except Exception as exc:
            append("failed", type(exc).__name__, attempt)
            raise
        return append(outcome.status, outcome.reason, attempt, outcome.artifacts)


def seal(path: Path) -> Path:
    """An offline packet includes EVERY attempt, including rejected/failed work."""
    with _lock(path):
        store, ref, study = _open(path)
        history = _history(store, ref, study)
        latest = {event.task: event for _, event in history}
        if set(latest) != {task.name for task in study.tasks} or any(
            event.status == "started" for event in latest.values()
        ):
            raise ValueError("study has unfinished tasks")
        packet = Packet(study=ref, events=tuple(item for item, _ in history))
        packet_ref = store.save(packet)
        target = store.root / "packets" / f"{packet_ref.sha256}.json"
        _publish(target, _bytes(packet))
        verify(target)
        return target


def _closure(path: Path) -> tuple[Store, dict[str, ObjectRef]]:
    if path.stat().st_size > OBJECT_BYTES:
        raise ValueError("packet exceeds object budget")
    packet_ref = ObjectRef(sha256=digest(path.read_bytes()), size_bytes=path.stat().st_size)
    if path.name != packet_ref.sha256 + ".json":
        raise ValueError("packet identity mismatch")
    store = Store(path.parents[1])
    packet = store.model(packet_ref, Packet)
    study = store.model(packet.study, Study)
    refs: dict[str, ObjectRef] = {}
    total = 0

    def add(ref: ObjectRef) -> None:
        nonlocal total
        if ref.sha256 in refs and refs[ref.sha256] != ref:
            raise ValueError("conflicting object reference")
        if ref.sha256 not in refs:
            total += ref.size_bytes
        refs[ref.sha256] = ref
        if len(refs) > MAX_OBJECTS or total > MAX_BUNDLE_BYTES:
            raise ValueError("review packet exceeds bundle budget")
        store.get(ref)

    for ref in (
        packet_ref,
        packet.study,
        study.declaration,
        *(task.inputs for task in study.tasks),
    ):
        add(ref)
    history = []
    for ref in packet.events:
        add(ref)
        event = store.model(ref, Event)
        history.append((ref, event))
        for artifact in event.artifacts:
            for part in artifact.parts:
                add(part)
            for _ in store.chunks(artifact):
                pass
    _validate_history(packet.study, study, history)
    latest = {event.task: event for _, event in history}
    if len(latest) != len(study.tasks) or any(
        event.status == "started" for event in latest.values()
    ):
        raise ValueError("packet has unfinished tasks")
    return store, refs


def verify(path: Path) -> dict[str, object]:
    store, refs = _closure(path)
    packet = store.model(ObjectRef(sha256=path.stem, size_bytes=path.stat().st_size), Packet)
    events = [store.model(ref, Event) for ref in packet.events]
    latest = {event.task: event for event in events}
    return {
        "packet_sha256": path.stem,
        "objects": len(refs),
        "stored_bytes": sum(ref.size_bytes for ref in refs.values()),
        "tasks": {
            name: {"status": event.status, "reason": event.reason, "attempt": event.attempt}
            for name, event in latest.items()
        },
        "failed_attempts": sum(event.status == "failed" for event in events),
        "abandoned_attempts": sum(event.status == "abandoned" for event in events),
        "execution_permission": False,
        "promotion_allowed": False,
    }


def archive(path: Path, output: Path) -> dict[str, object]:
    store, refs = _closure(path)
    # An exclusive destination prevents accidental replacement of a reviewed backup.
    with output.open("xb") as destination:
        with tarfile.open(fileobj=destination, mode="w|", format=tarfile.USTAR_FORMAT) as bundle:
            for name, source in [
                ("packet.json", path),
                *(("objects/" + key, store.path(ref)) for key, ref in sorted(refs.items())),
            ]:
                metadata = tarfile.TarInfo(name)
                metadata.size = source.stat().st_size
                metadata.mode = 0o600
                with source.open("rb") as stream:
                    bundle.addfile(metadata, stream)
        destination.flush()
        os.fsync(destination.fileno())
    return verify(path)


def restore(source: Path, destination: Path) -> Path:
    try:
        return _restore(source, destination)
    except tarfile.TarError:
        raise ValueError("invalid or truncated bundle archive") from None


def _restore(source: Path, destination: Path) -> Path:
    """Stream only regular hashed objects into staging; publish after full verification."""
    if destination.exists():
        raise ValueError("restore destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".aca-restore-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "bundle"
        store = Store(staging)
        names: set[str] = set()
        total = 0
        packet_data: bytes | None = None
        with tarfile.open(source, mode="r|") as bundle:
            for member in bundle:
                if (
                    not member.isfile()
                    or member.name in names
                    or not 0 < member.size <= OBJECT_BYTES
                    or len(names) > MAX_OBJECTS
                ):
                    raise ValueError("invalid bundle archive member")
                names.add(member.name)
                total += member.size
                if total > MAX_BUNDLE_BYTES + OBJECT_BYTES:
                    raise ValueError("archive exceeds bundle budget")
                stream = bundle.extractfile(member)
                if stream is None:
                    raise ValueError("missing bundle archive data")
                data = stream.read(OBJECT_BYTES + 1)
                if len(data) != member.size:
                    raise ValueError("truncated bundle archive")
                if member.name == "packet.json":
                    packet_data = data
                elif member.name == "objects/" + digest(data):
                    store.put(data)
                else:
                    raise ValueError("invalid archive object name/hash")
        if packet_data is None:
            raise ValueError("archive has no packet")
        packet = staging / "packets" / f"{digest(packet_data)}.json"
        _publish(packet, packet_data)
        _, refs = _closure(packet)
        if names != {"packet.json", *("objects/" + key for key in refs)}:
            raise ValueError("archive contains unreferenced objects")
        restored_packet = store.model(store.put(packet_data), Packet)
        _publish(
            staging / "studies" / restored_packet.study.sha256 / "manifest.json",
            store.get(restored_packet.study),
        )
        for ref in restored_packet.events:
            event = store.model(ref, Event)
            _publish(
                staging
                / "studies"
                / restored_packet.study.sha256
                / "events"
                / f"{event.sequence:04d}-{ref.sha256}.json",
                store.get(ref),
            )
        os.rename(staging, destination)
        _sync_directory(destination.parent)
        return destination / "packets" / packet.name


def preserve_report(report: Path, root: Path) -> Path:
    """Preserve a bounded legacy report byte-for-byte; do not rerun or reinterpret it."""
    document = read_json(report)
    fingerprint = digest(canonical(document))
    if report.stem != fingerprint or report.read_bytes() != canonical(document):
        raise ValueError("legacy report identity mismatch")
    if document.get("kind") != "research_report" or document.get("holdout_opened") is not False:
        raise ValueError("only unopened legacy research reports can be preserved")
    event_paths = sorted((report.parents[1] / "events").glob("*.json"))
    if len(event_paths) > 10_000:
        raise ValueError("legacy event batch exceeds budget")
    source_events: list[dict[str, object]] = []
    for event_path in event_paths:
        record = read_json(event_path)
        if record.get("run_id") == document.get("run_id") and document.get("run_id") is not None:
            if event_path.stem != digest(canonical(record)):
                raise ValueError("legacy event identity mismatch")
            source_events.append(record)
    event_bytes = canonical({"events": source_events})
    path = create(
        root,
        {
            "purpose": "legacy_preservation",
            "report_sha256": fingerprint,
            "legacy_event_count": len(source_events),
            "legacy_events_sha256": digest(event_bytes),
        },
        {
            "legacy_report": {"report_sha256": fingerprint},
            "legacy_events": {"events_sha256": digest(event_bytes)},
        },
    )

    def work(store: Store) -> Result:
        with report.open("rb") as stream:
            artifact = store.artifact("legacy_report", iter(lambda: stream.read(OBJECT_BYTES), b""))
        if artifact.sha256 != fingerprint:
            raise ValueError("legacy report changed during preservation")
        return Result(
            status="completed", reason="byte_identical_preservation", artifacts=(artifact,)
        )

    execute(path, "legacy_report", work)
    execute(
        path,
        "legacy_events",
        lambda store: Result(
            status="completed",
            reason="original_event_preservation",
            artifacts=(store.records("evidence", source_events),),
        ),
    )
    return seal(path)


def export_report(packet_path: Path, output: Path) -> str:
    """Reconstruct the exact baseline bytes, independently verifying the full hash."""
    store, _ = _closure(packet_path)
    ref = ObjectRef(sha256=packet_path.stem, size_bytes=packet_path.stat().st_size)
    packet = store.model(ref, Packet)
    artifacts = [
        artifact
        for event_ref in packet.events
        for artifact in store.model(event_ref, Event).artifacts
        if artifact.kind == "legacy_report"
    ]
    if len(artifacts) != 1:
        raise ValueError("packet must contain exactly one legacy report")
    with NamedTemporaryFile(dir=output.parent, prefix=".report-", delete=False) as stream:
        pending = Path(stream.name)
        try:
            for chunk in store.chunks(artifacts[0]):
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
            os.link(pending, output)
            _sync_directory(output.parent)
        finally:
            pending.unlink(missing_ok=True)
    return artifacts[0].sha256

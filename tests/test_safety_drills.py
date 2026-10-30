"""Destructive helpers remain confined to fresh disposable projects."""

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import capacity_trial
from scripts.capacity_trial import failure_summary, last_progress
from scripts.restore_drill import Drill, DrillError
from scripts.storage_fault_drill import StorageDrill


def test_capacity_progress_excludes_private_child_diagnostics(tmp_path: Path) -> None:
    path = tmp_path / "command.log"
    path.write_text(
        "private database credential or child traceback\n"
        'ACA_CAPACITY_PROGRESS {"phase":"seeded","elapsed_seconds":1.5}\n'
        'ACA_CAPACITY_PROGRESS {"phase":"compacted","elapsed_seconds":12,'
        '"private":"must never be emitted"}\n'
        'ACA_CAPACITY_PROGRESS {"phase":"private secret","elapsed_seconds":13}\n'
        'ACA_CAPACITY_PROGRESS {"phase":[],"elapsed_seconds":13}\n'
        'ACA_CAPACITY_PROGRESS {"phase":"seeded","elapsed_seconds":NaN}\n'
        'ACA_CAPACITY_PROGRESS {"phase":"seeded","elapsed_seconds":true}\n'
        "ACA_CAPACITY_PROGRESS malformed\n"
    )
    assert last_progress(path) == {"phase": "compacted", "elapsed_seconds": 12}
    assert last_progress(tmp_path / "missing.log") is None


def test_capacity_failure_summary_emits_only_fixed_labels(tmp_path: Path) -> None:
    path = tmp_path / "private.log"
    path.write_text(
        "private-token-private://operator:password@host\n"
        "psycopg.errors.QueryCanceled: private query cancelled due to statement timeout\n"
        "sqlalchemy.exc.OperationalError: private connection details\n"
        "[SQL: VACUUM (ANALYZE) aca.private_table]\n"
    )
    result = failure_summary(path)
    assert result == {
        "exception_classes": ["psycopg.errors.QueryCanceled", "sqlalchemy.exc.OperationalError"],
        "conditions": ["statement_timeout"],
        "operations": ["vacuum_analyze"],
    }
    text = json.dumps(result)
    assert "private" not in text and "password" not in text and "operator" not in text


def test_capacity_failure_summary_handles_missing_or_unclassified_input(tmp_path: Path) -> None:
    assert failure_summary(tmp_path / "missing.log") == {}
    path = tmp_path / "private.log"
    path.write_text("secret arbitrary failure details\n")
    assert failure_summary(path) == {"exception_classes": [], "conditions": [], "operations": []}


def test_capacity_timeout_still_fails_and_cleans_up_with_safe_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".secrets").mkdir(mode=0o700)
    owners: list[Any] = []

    class FixtureDrill:
        def __init__(self, directory: Path, identifier: str) -> None:
            self.directory = directory
            self.source = ["disposable-fixture"]
            self.database = "synthetic"
            self.counter = 0
            self.cleaned = False
            owners.append(self)

        def call(self, command: list[str], **kwargs: Any) -> bytes:
            self.counter += 1
            if self.counter == 3:
                assert kwargs["timeout"] == 900
                (self.directory / "command-03.log").write_text(
                    "private child error\n"
                    'ACA_CAPACITY_PROGRESS {"phase":"seeded","elapsed_seconds":42}\n'
                )
                raise subprocess.TimeoutExpired("disposable-fixture", kwargs["timeout"])
            return b""

        def cleanup(self) -> bool:
            self.cleaned = True
            return True

    monkeypatch.setattr(capacity_trial, "ROOT", tmp_path)
    monkeypatch.setattr(capacity_trial, "Drill", FixtureDrill)
    monkeypatch.setattr(capacity_trial, "require_docker", lambda: None)
    monkeypatch.setattr(capacity_trial, "prepare_secrets", lambda: None)
    monkeypatch.setattr(sys, "argv", ["capacity_trial.py", "--compaction"])
    assert capacity_trial.main() == 1
    assert len(owners) == 1 and owners[0].cleaned
    result = json.loads(next((tmp_path / ".secrets/backups").glob("*/manifest.json")).read_text())
    assert result["error_type"] == "TimeoutExpired"
    assert result["last_progress"] == {"phase": "seeded", "elapsed_seconds": 42}
    assert result["cleanup_complete"] is True
    assert "private child error" not in capsys.readouterr().out


@pytest.mark.parametrize("filename", ["storage_fault_drill.py", "capacity_trial.py"])
def test_new_host_helpers_use_python39_syntax(filename: str) -> None:
    ast.parse(
        (Path(__file__).parents[1] / "scripts" / filename).read_text(), feature_version=(3, 9)
    )


@pytest.mark.parametrize("identifier", ["aca-paper", "", "../retained", "a" * 12 + ";true"])
def test_storage_helper_rejects_any_non_generated_identity(tmp_path: Path, identifier: str) -> None:
    with pytest.raises(DrillError):
        StorageDrill(tmp_path, identifier)


@pytest.mark.parametrize("change", ["driver", "size", "mount", "copy"])
def test_storage_fill_never_runs_for_an_unverified_bounded_volume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    calls: list[list[str]] = []
    config: dict[str, Any] = {
        "volumes": {
            "storage_fault_data": {
                "driver_opts": {
                    "type": "tmpfs",
                    "device": "tmpfs",
                    "o": "size=384m,uid=70,gid=70,mode=0700",
                }
            }
        },
        "services": {
            "db": {"volumes": [{"target": "/var/lib/postgresql", "source": "storage_fault_data"}]}
        },
    }
    config["services"]["db"]["volumes"][0]["volume"] = {"nocopy": True}
    config["services"]["storage-holder"] = {"volumes": [{"volume": {"nocopy": True}}]}
    if change == "driver":
        config["volumes"]["storage_fault_data"]["driver_opts"]["type"] = "ext4"
    elif change == "size":
        config["volumes"]["storage_fault_data"]["driver_opts"]["o"] = "size=40g"
    elif change == "mount":
        config["services"]["db"]["volumes"][0]["source"] = "retained"
    else:
        config["services"]["storage-holder"]["volumes"][0]["volume"]["nocopy"] = False

    def capture(self: Drill, command: list[str], **kwargs: object) -> bytes:
        calls.append(command)
        return json.dumps(config).encode()

    monkeypatch.setattr(Drill, "call", capture)
    with pytest.raises(DrillError, match="refusing"):
        StorageDrill(tmp_path, "0123456789ab").run()
    assert len(calls) == 2 and not any("dd" in c or "up" in c for c in calls)

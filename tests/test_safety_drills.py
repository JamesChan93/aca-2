"""Destructive helpers remain confined to fresh disposable projects."""

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.restore_drill import Drill, DrillError
from scripts.storage_fault_drill import StorageDrill


@pytest.mark.parametrize("filename", ["storage_fault_drill.py", "capacity_trial.py"])
def test_new_host_helpers_use_python39_syntax(filename: str) -> None:
    ast.parse(
        (Path(__file__).parents[1] / "scripts" / filename).read_text(), feature_version=(3, 9)
    )


@pytest.mark.parametrize("identifier", ["aca-paper", "", "../retained", "a" * 12 + ";true"])
def test_storage_helper_rejects_any_non_generated_identity(tmp_path: Path, identifier: str) -> None:
    with pytest.raises(DrillError):
        StorageDrill(tmp_path, identifier)


@pytest.mark.parametrize("change", ["driver", "size", "mount"])
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
    if change == "driver":
        config["volumes"]["storage_fault_data"]["driver_opts"]["type"] = "ext4"
    elif change == "size":
        config["volumes"]["storage_fault_data"]["driver_opts"]["o"] = "size=40g"
    else:
        config["services"]["db"]["volumes"][0]["source"] = "retained"

    def capture(self: Drill, command: list[str], **kwargs: object) -> bytes:
        calls.append(command)
        return json.dumps(config).encode()

    monkeypatch.setattr(Drill, "call", capture)
    with pytest.raises(DrillError, match="refusing"):
        StorageDrill(tmp_path, "0123456789ab").run()
    assert len(calls) == 2 and not any("dd" in c or "up" in c for c in calls)

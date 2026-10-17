from pathlib import Path

import pytest

from scripts.bootstrap import prepare_secrets


def test_bootstrap_is_idempotent_and_private(tmp_path: Path) -> None:
    prepare_secrets(tmp_path)
    paths = sorted((tmp_path / ".secrets").iterdir())
    values = {path.name: path.read_text() for path in paths}
    assert len(values) == 3
    assert len(set(values.values())) == 3
    assert (tmp_path / ".secrets").stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o444 for path in paths)
    prepare_secrets(tmp_path)
    assert {path.name: path.read_text() for path in paths} == values


def test_bootstrap_refuses_unsafe_directory_or_symlink(tmp_path: Path) -> None:
    directory = tmp_path / ".secrets"
    directory.mkdir(mode=0o755)
    with pytest.raises(RuntimeError):
        prepare_secrets(tmp_path)
    directory.rmdir()
    target = tmp_path / "target"
    target.mkdir()
    directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError):
        prepare_secrets(tmp_path)

import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

import pytest
import structlog

from aca.observability.logging import configure_logging, redact, register_secret
from aca.operations.secrets import SecretFileError, read_secret


def test_nested_and_embedded_secrets_are_redacted() -> None:
    secret = "a-secret-with-special:/characters-123"
    register_secret(secret)
    result = redact(
        {
            "password": secret,
            "nested": [{"API-Sign": secret}],
            "message": (
                f"postgresql://user:other-password@db/name {secret} {quote(secret, safe='')}"
            ),
            "url": "https://discord.com/api/webhooks/123/secret-value",
        }
    )
    output = json.dumps(result)
    assert secret not in output
    assert "other-password" not in output
    assert "secret-value" not in output
    assert "[REDACTED]" in output


def test_structlog_and_standard_logs_are_safe(capsys: pytest.CaptureFixture[str]) -> None:
    secret = "runtime-password-that-must-stay-private"
    register_secret(secret)
    configure_logging()
    structlog.get_logger().info("startup", password=secret, detail=f"received {secret}")
    logging.getLogger("driver").error("connection failed %s", secret, exc_info=True)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    for line in lines:
        assert secret not in line
        assert "traceback" not in line.lower()
        assert json.loads(line)["timestamp"].endswith("Z")


def test_safe_secret_file_and_no_value_in_repr(tmp_path: Path) -> None:
    path = tmp_path / "password"
    value = "test-password-very-private-1234567"
    path.write_text(value)
    path.chmod(0o600)
    secret = read_secret(path)
    assert secret.get_secret_value() == value
    assert value not in repr(secret)


def test_bootstrap_read_only_secrets_in_private_directory(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "password"
    path.write_text("test-password-very-private-1234567")
    path.chmod(0o444)
    assert read_secret(path).get_secret_value()
    private.chmod(0o755)
    with pytest.raises(SecretFileError):
        read_secret(path)


def test_symlinks_and_publicly_writable_secrets_rejected(tmp_path: Path) -> None:
    path = tmp_path / "password"
    path.write_text("test-password-very-private-1234567")
    path.chmod(0o666)
    with pytest.raises(SecretFileError):
        read_secret(path)
    path.chmod(0o600)
    linked = tmp_path / "link"
    linked.symlink_to(path)
    with pytest.raises(SecretFileError):
        read_secret(linked)


def test_non_regular_secret_cannot_block_read(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(SecretFileError):
        read_secret(fifo)

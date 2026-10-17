"""Secret-file delivery only; secret values are excluded from configuration records."""

import os
import stat
from pathlib import Path

from pydantic import SecretStr

from aca.observability.logging import register_secret


class SecretFileError(ValueError):
    """Safe error without path, content or credential-bearing driver details."""


def read_secret(path: Path) -> SecretStr:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or info.st_mode & 0o022:
                raise SecretFileError(
                    "secret file must be regular, bounded and not publicly writable"
                )
            if info.st_mode & 0o044:
                parent = path.parent.stat()
                protected_directory = parent.st_uid == os.getuid() and not parent.st_mode & 0o077
                container_mount = path.is_absolute() and path.parent == Path("/run/secrets")
                if not (protected_directory or container_mount):
                    raise SecretFileError(
                        "readable secret files require a private directory or secret mount"
                    )
            value = handle.read(4097).decode("utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise SecretFileError("secret file could not be read safely") from exc
    if not 20 <= len(value) <= 256 or any(char.isspace() for char in value):
        raise SecretFileError("secret must contain 20 to 256 non-whitespace characters")
    register_secret(value)
    return SecretStr(value)

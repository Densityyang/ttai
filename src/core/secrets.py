"""Resolve configuration values from an environment variable or its ``_FILE`` peer."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Mapping


class SecretProvider:
    """Read secrets without putting secret text in Compose or process arguments."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else os.environ

    def get(self, name: str, default: str | None = None) -> str | None:
        file_name = f"{name}_FILE"
        file_path = self._environ.get(file_name)
        if file_path:
            path = Path(file_path)
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"{file_name} must reference a readable regular file")
            mode = stat.S_IMODE(path.stat().st_mode)
            if os.name != "nt" and mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ValueError(f"{file_name} must not be writable by group or others")
            value = path.read_text(encoding="utf-8").rstrip("\r\n")
            if not value.strip():
                raise ValueError(f"{file_name} resolved to an empty secret")
            return value
        return self._environ.get(name, default)

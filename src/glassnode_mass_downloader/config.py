"""Shared project configuration loaded from environment variables or .env."""

from __future__ import annotations

import os
from pathlib import Path


# The installed CLI is normally launched from the project directory, so use
# its working directory rather than the package installation directory.
PROJECT_ROOT = Path.cwd()
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
API_KEY_NAME = "GLASSNODE_API_KEY"


def _parse_env_value(value: str) -> str:
    value = value.strip()
    if value.startswith(('"', "'")):
        quote = value[0]
        for index in range(1, len(value)):
            if value[index] == quote and value[index - 1] != "\\":
                trailing = value[index + 1 :].strip()
                if trailing and not trailing.startswith("#"):
                    raise ValueError("unexpected content after quoted .env value")
                return value[1:index]
        raise ValueError("unterminated quoted .env value")
    return value.split(" #", maxsplit=1)[0].strip()


def get_glassnode_api_key(env_file: Path | None = None) -> str | None:
    """Return the Glassnode API key without logging or otherwise exposing it.

    A non-empty process environment variable takes precedence over the project
    ``.env`` file. Passing ``env_file`` is mainly useful for tests or alternate
    project layouts.
    """
    environment_value = os.environ.get(API_KEY_NAME, "").strip()
    if environment_value:
        return environment_value

    path = env_file or DEFAULT_ENV_FILE
    if not path.is_file():
        return None

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        name, separator, value = line.partition("=")
        if separator and name.strip() == API_KEY_NAME:
            return _parse_env_value(value) or None
    return None


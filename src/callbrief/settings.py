"""Configuration loading with a small allowlisted environment surface."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class SettingsError(ValueError):
    """Raised for missing, malformed or unsafe configuration."""


_ALLOWED_KEYS = frozenset(
    {
        "CALLBRIEF_BASE_URL",
        "CALLBRIEF_MODEL",
        "CALLBRIEF_API_KEY",
        "CALLBRIEF_TIMEOUT_SECONDS",
        "CALLBRIEF_MAX_TURNS",
    }
)


@dataclass(frozen=True, slots=True)
class Settings:
    base_url: str
    model: str
    api_key: str | None = None
    timeout_seconds: int = 45
    max_turns: int = 6

    @classmethod
    def from_sources(
        cls,
        environ: dict[str, str] | None = None,
        env_file: Path | None = None,
    ) -> Settings:
        values: dict[str, str] = {}
        if env_file is not None and Path(env_file).exists():
            path = Path(env_file)
            if path.is_symlink() or not path.is_file():
                raise SettingsError("Configuration file must not be a symlink")
            try:
                for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if "=" not in stripped:
                        raise SettingsError(f"Malformed configuration on line {line_number}")
                    key, raw = stripped.split("=", 1)
                    key = key.strip()
                    if key not in _ALLOWED_KEYS:
                        raise SettingsError(f"Unsupported configuration key: {key}")
                    try:
                        parsed = shlex.split(raw, comments=True, posix=True)
                    except ValueError as exc:
                        raise SettingsError(f"Malformed value on line {line_number}") from exc
                    values[key] = parsed[0] if parsed else ""
            except OSError as exc:
                raise SettingsError("Could not read configuration file") from exc
        source_environment = os.environ if environ is None else environ
        for key, value in source_environment.items():
            if key in _ALLOWED_KEYS:
                values[key] = value

        base_url = values.get("CALLBRIEF_BASE_URL", "http://127.0.0.1:11434/v1").strip().rstrip("/")
        model = values.get("CALLBRIEF_MODEL", "").strip()
        api_key = values.get("CALLBRIEF_API_KEY", "").strip() or None
        if not model:
            raise SettingsError("Set CALLBRIEF_MODEL before running an assessment")
        if len(model) > 120 or any(char in model for char in "\r\n"):
            raise SettingsError("CALLBRIEF_MODEL is malformed")
        parsed_url = urlparse(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise SettingsError("CALLBRIEF_BASE_URL must be an HTTP(S) URL")
        if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
            raise SettingsError("CALLBRIEF_BASE_URL must not contain credentials, a query or a fragment")
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if parsed_url.scheme == "http" and parsed_url.hostname.casefold() not in local_hosts:
            raise SettingsError("Plain HTTP is allowed only for a loopback model server")
        if not parsed_url.path.startswith("/"):
            raise SettingsError("CALLBRIEF_BASE_URL path is malformed")
        try:
            timeout = int(values.get("CALLBRIEF_TIMEOUT_SECONDS", "45"))
            max_turns = int(values.get("CALLBRIEF_MAX_TURNS", "6"))
        except ValueError as exc:
            raise SettingsError("Timeout and turn limits must be integers") from exc
        if not 1 <= timeout <= 180:
            raise SettingsError("CALLBRIEF_TIMEOUT_SECONDS must be between 1 and 180")
        if not 1 <= max_turns <= 12:
            raise SettingsError("CALLBRIEF_MAX_TURNS must be between 1 and 12")
        return cls(base_url, model, api_key, timeout, max_turns)

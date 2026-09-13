"""Provider configuration for the AI layer: presets, providers and the file.

The AI layer is **off until configured**: :meth:`AIConfig.load` returns a
disabled config when no file exists, and every caller degrades gracefully from
there.  A configuration is a list of providers side by side - Ollama and LM
Studio on this machine, OpenAI or OpenRouter in the cloud, or anything else
speaking the OpenAI-compatible API - plus the policy switches (streaming,
``redact_paths``, local-only mode, cache, batch bounds).

    # ~/.config/spacesage/ai.toml
    [ai]
    default_provider = "ollama"
    redact_paths = false
    local_only = true

    [[ai.providers]]
    name = "ollama"
    preset = "ollama"          # ollama | lmstudio | openai | openrouter | custom
    model = "llama3.2"

**API keys are never stored here.**  A provider names the environment variable
that holds its key (``api_key_env``) or a file to read it from
(``api_key_file``); the key itself is read at request time, never logged,
never rendered into a status view and never written back by :meth:`AIConfig.save`.
"""

from __future__ import annotations

import ipaddress
import json
import os
import stat
import sys
import tomllib
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from spacesage.ai.errors import INVALID_CONFIG, LOCAL_ONLY, AIError

CONFIG_ENV_VAR = "SPACESAGE_AI_CONFIG"
"""Environment variable naming the AI configuration file itself."""

OFF_ENV_VAR = "SPACESAGE_AI_OFF"
"""Set to ``1`` to keep the AI layer off whatever the file says."""

PROVIDER_ENV_VAR = "SPACESAGE_AI_PROVIDER"
"""Default provider name (overrides ``default_provider``)."""

MODEL_ENV_VAR = "SPACESAGE_AI_MODEL"
"""Model for the selected provider (overrides ``model``)."""

BASE_URL_ENV_VAR = "SPACESAGE_AI_BASE_URL"
"""Base URL for the selected provider (overrides ``base_url``)."""

KEY_ENV_ENV_VAR = "SPACESAGE_AI_KEY_ENV"
"""Environment variable to read the selected provider's API key from."""

LOCAL_ONLY_ENV_VAR = "SPACESAGE_AI_LOCAL_ONLY"
"""Set to ``1`` to block every non-loopback endpoint."""

REDACT_ENV_VAR = "SPACESAGE_AI_REDACT_PATHS"
"""Set to ``1`` to send path tokens instead of real paths."""

CACHE_DIR_ENV_VAR = "SPACESAGE_AI_CACHE_DIR"
"""Directory holding the response cache."""

DEFAULT_CONFIG_NAME = "ai.toml"
"""File name of the AI configuration inside the per-user config directory."""

APP_DIR_NAME = "spacesage"


@dataclass(frozen=True)
class ProviderPreset:
    """A known provider flavour and the defaults that make it work."""

    kind: str
    title: str
    base_url: str
    api_key_env: str | None
    model: str
    local: bool
    pricing_in: float | None = None
    """USD per 1M prompt tokens (``None``: cost is not estimated)."""

    pricing_out: float | None = None
    """USD per 1M completion tokens."""

    note: str = ""


PRESETS: Mapping[str, ProviderPreset] = {
    "ollama": ProviderPreset(
        kind="ollama",
        title="Ollama (local)",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
        model="llama3.2",
        local=True,
        note="Runs on this machine; nothing leaves it. Pick a model with `ollama list`.",
    ),
    "lmstudio": ProviderPreset(
        kind="lmstudio",
        title="LM Studio (local)",
        base_url="http://localhost:1234/v1",
        api_key_env=None,
        model="local-model",
        local=True,
        note="Runs on this machine; the model name is the one loaded in LM Studio.",
    ),
    "openai": ProviderPreset(
        kind="openai",
        title="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        model="gpt-4o-mini",
        local=False,
        pricing_in=0.15,
        pricing_out=0.60,
        note="Cloud provider: the items you suggest for leave this machine.",
    ),
    "openrouter": ProviderPreset(
        kind="openrouter",
        title="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        model="openai/gpt-4o-mini",
        local=False,
        note="Cloud gateway: pricing depends on the model you route to.",
    ),
    "custom": ProviderPreset(
        kind="custom",
        title="Custom OpenAI-compatible endpoint",
        base_url="",
        api_key_env="SPACESAGE_AI_API_KEY",
        model="",
        local=False,
        note="Any /chat/completions server: vLLM, llama.cpp, a gateway, ...",
    ),
}
"""Provider presets (``custom`` is the escape hatch for everything else)."""

_AI_KEYS = frozenset(
    {
        "enabled",
        "default_provider",
        "providers",
        "streaming",
        "redact_paths",
        "local_only",
        "cache",
        "cache_dir",
        "retries",
        "batch_size",
        "max_items",
        "max_prompt_chars",
        "max_tokens",
        "temperature",
    }
)

_PROVIDER_KEYS = frozenset(
    {
        "name",
        "preset",
        "kind",
        "base_url",
        "model",
        "api_key_env",
        "api_key_file",
        "pricing_in",
        "pricing_out",
        "timeout_s",
        "stream",
        "json_mode",
        "extra_headers",
    }
)


# --------------------------------------------------------------------------- #
# Paths and small helpers
# --------------------------------------------------------------------------- #


def _config_home(env: Mapping[str, str], *, windows: bool) -> Path:
    if windows:
        base = env.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base).expanduser() / APP_DIR_NAME
    xdg = env.get("XDG_CONFIG_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return root / APP_DIR_NAME


def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    """``~/.config/spacesage/ai.toml`` (``$SPACESAGE_AI_CONFIG`` wins)."""
    environ = os.environ if env is None else env
    override = environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return _config_home(environ, windows=sys.platform == "win32") / DEFAULT_CONFIG_NAME


def default_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where validated AI answers are cached (``$SPACESAGE_AI_CACHE_DIR`` wins)."""
    environ = os.environ if env is None else env
    override = environ.get(CACHE_DIR_ENV_VAR)
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base).expanduser() / APP_DIR_NAME / "ai-cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / APP_DIR_NAME / "ai"
    xdg = environ.get("XDG_CACHE_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return root / APP_DIR_NAME / "ai"


def _flag(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, int):
        return bool(value)
    raise AIError(
        INVALID_CONFIG,
        f"expected a boolean, found {value!r}",
        hint="use true/false in ai.toml",
    )


def is_loopback(host: str) -> bool:
    """True for ``localhost``, ``127.0.0.0/8`` and ``::1`` (and ``*.localhost``)."""
    name = host.strip().strip("[]").lower()
    if not name:
        return False
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _host_of(base_url: str) -> str:
    """Host part of a base URL (``http://localhost:1234/v1`` -> ``localhost``)."""
    parts = urllib.parse.urlsplit(base_url)
    return parts.hostname or ""


def validate_base_url(base_url: str, *, provider: str = "") -> str:
    """Return the trimmed base URL, or raise an actionable config error."""
    value = base_url.strip()
    if not value:
        raise AIError(
            INVALID_CONFIG,
            f"provider {provider or '?'} has no base_url",
            hint="set base_url to an OpenAI-compatible endpoint, e.g. http://localhost:11434/v1",
            provider=provider or None,
        )
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise AIError(
            INVALID_CONFIG,
            f"provider {provider or '?'} has an unusable base_url: {value!r}",
            hint="base_url must be an absolute http(s) URL, e.g. https://api.openai.com/v1",
            provider=provider or None,
        )
    return value.rstrip("/")


def enforce_local_only(base_url: str, *, provider: str = "") -> None:
    """Refuse a non-loopback endpoint when local-only mode is on.

    Called *before* any socket is opened, so turning the mode on can never leak
    even a DNS lookup for a remote host.
    """
    host = _host_of(base_url)
    if is_loopback(host):
        return
    raise AIError(
        LOCAL_ONLY,
        f"local-only mode blocks provider {provider or '?'} at {host or base_url!r}",
        hint=(
            "turn local_only off in ai.toml (or unset SPACESAGE_AI_LOCAL_ONLY) to use a "
            "cloud provider, or point the provider at a local server (ollama, LM Studio)"
        ),
        provider=provider or None,
    )


# --------------------------------------------------------------------------- #
# One provider
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProviderConfig:
    """One configured provider: where it is, what to call, how to authenticate."""

    name: str
    kind: str = "custom"
    base_url: str = ""
    model: str = ""
    api_key_env: str | None = None
    api_key_file: str | None = None
    pricing_in: float | None = None
    pricing_out: float | None = None
    timeout_s: float = 60.0
    stream: bool = True
    json_mode: bool = False
    extra_headers: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_preset(cls, name: str, kind: str, **overrides: object) -> ProviderConfig:
        """Build a provider from a preset, with ``overrides`` applied on top."""
        if kind not in PRESETS:
            raise AIError(
                INVALID_CONFIG,
                f"unknown provider preset {kind!r}",
                hint=f"presets: {', '.join(sorted(PRESETS))}",
            )
        preset = PRESETS[kind]
        base: dict[str, object] = {
            "name": name,
            "kind": preset.kind,
            "base_url": preset.base_url,
            "model": preset.model,
            "api_key_env": preset.api_key_env,
            "pricing_in": preset.pricing_in,
            "pricing_out": preset.pricing_out,
        }
        base.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**base)  # type: ignore[arg-type]

    @property
    def preset(self) -> ProviderPreset | None:
        """The preset this provider was built from (``None`` for hand-written ones)."""
        return PRESETS.get(self.kind)

    @property
    def title(self) -> str:
        """Human name of the provider (preset title, else the configured name)."""
        preset = self.preset
        return preset.title if preset is not None else self.name

    def origin(self) -> str:
        """``scheme://host[:port]`` the provider lives at (no path, no credentials)."""
        parts = urllib.parse.urlsplit(self.base_url)
        return f"{parts.scheme}://{parts.netloc}"

    def host(self) -> str:
        """Host part of :attr:`base_url`."""
        return _host_of(self.base_url)

    @property
    def is_local(self) -> bool:
        """True when the endpoint is this machine (nothing leaves it).

        A property (like :attr:`title`): `if provider.is_local:` is the contract,
        and a forgotten call would read as "always true".
        """
        return is_loopback(self.host())

    def chat_url(self) -> str:
        """Absolute URL of ``/chat/completions``."""
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def models_url(self) -> str:
        """Absolute URL of the ``/models`` listing."""
        return f"{self.base_url.rstrip('/')}/models"

    def api_key(self, env: Mapping[str, str] | None = None) -> str | None:
        """Resolve the API key from the environment or the named key file.

        Never logged, never rendered into a status view.  Returns ``None`` for
        keyless local servers.
        """
        environ = os.environ if env is None else env
        if self.api_key_env:
            value = environ.get(self.api_key_env)
            if value:
                return value.strip()
        if self.api_key_file:
            path = Path(self.api_key_file).expanduser()
            try:
                return path.read_text(encoding="utf-8").strip() or None
            except OSError as exc:
                raise AIError(
                    INVALID_CONFIG,
                    f"cannot read the API key file {path}: {exc}",
                    hint="fix api_key_file, or drop it and set the key env var instead",
                    provider=self.name,
                ) from exc
        return None

    def has_key_source(self) -> bool:
        """True when the provider names somewhere a key can come from."""
        return bool(self.api_key_env or self.api_key_file)

    def key_warning(self, env: Mapping[str, str] | None = None) -> str | None:
        """A non-fatal warning about the key setup (world-readable file, ...)."""
        environ = os.environ if env is None else env
        if not self.api_key_file:
            return None
        if self.api_key_env and environ.get(self.api_key_env):
            return None
        path = Path(self.api_key_file).expanduser()
        if sys.platform == "win32" or not path.is_file():
            return None
        mode = path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
            return (
                f"the API key file {path} is readable by other users "
                f"(chmod 600 {path} to keep the key private)"
            )
        return None

    def key_present(self, env: Mapping[str, str] | None = None) -> bool:
        """True when a key is actually available (or none is needed)."""
        if not self.has_key_source():
            return self.is_local
        try:
            return self.api_key(env) is not None
        except AIError:
            return False

    def validated(self) -> ProviderConfig:
        """A copy with a validated base URL, or an :class:`AIError`."""
        base_url = validate_base_url(self.base_url, provider=self.name)
        if not self.model:
            raise AIError(
                INVALID_CONFIG,
                f"provider {self.name} has no model",
                hint="set model in ai.toml, or run `spacesage ai models` to list the ones offered",
                provider=self.name,
            )
        return self if base_url == self.base_url else replace(self, base_url=base_url)

    def to_dict(self, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
        """JSON-ready view (no secrets - only whether a key is there)."""
        return {
            "name": self.name,
            "kind": self.kind,
            "title": self.title,
            "base_url": self.base_url,
            "model": self.model,
            "local": self.is_local,
            "api_key_env": self.api_key_env,
            "api_key_file": self.api_key_file,
            "key_present": self.key_present(env),
            "pricing_in": self.pricing_in,
            "pricing_out": self.pricing_out,
            "timeout_s": self.timeout_s,
            "stream": self.stream,
            "json_mode": self.json_mode,
        }

    def render_toml(self) -> str:
        """The ``[[ai.providers]]`` table for this provider, deterministic."""
        lines = ["[[ai.providers]]", f"name = {_toml_string(self.name)}"]
        if self.kind != "custom":
            lines.append(f"preset = {_toml_string(self.kind)}")
        lines.append(f"base_url = {_toml_string(self.base_url)}")
        lines.append(f"model = {_toml_string(self.model)}")
        if self.api_key_env:
            lines.append(f"api_key_env = {_toml_string(self.api_key_env)}")
        if self.api_key_file:
            lines.append(f"api_key_file = {_toml_string(self.api_key_file)}")
        if self.pricing_in is not None:
            lines.append(f"pricing_in = {_toml_number(self.pricing_in)}")
        if self.pricing_out is not None:
            lines.append(f"pricing_out = {_toml_number(self.pricing_out)}")
        if self.timeout_s != 60.0:
            lines.append(f"timeout_s = {_toml_number(self.timeout_s)}")
        if not self.stream:
            lines.append("stream = false")
        if self.json_mode:
            lines.append("json_mode = true")
        for key, value in self.extra_headers.items():
            lines.append(f"extra_headers.{key} = {_toml_string(value)}")
        return "\n".join(lines)


def _toml_string(value: str) -> str:
    """A TOML basic string (JSON escaping is a valid subset for our values)."""
    return json.dumps(value, ensure_ascii=False)


def _toml_number(value: float) -> str:
    """A TOML number: integers stay integers, floats keep one decimal."""
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


# --------------------------------------------------------------------------- #
# The whole configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AIConfig:
    """Every provider side by side, plus the AI layer's policy switches."""

    enabled: bool = False
    default_provider: str | None = None
    providers: tuple[ProviderConfig, ...] = ()
    streaming: bool = True
    redact_paths: bool = False
    local_only: bool = False
    cache: bool = True
    cache_dir: str | None = None
    retries: int = 2
    batch_size: int = 8
    max_items: int = 200
    max_prompt_chars: int = 24_000
    max_tokens: int = 1200
    temperature: float = 0.2
    source: str | None = None
    """The file this config came from (``None`` when nothing is configured)."""

    # -- selection ---------------------------------------------------------- #

    def provider(self, name: str | None = None) -> ProviderConfig:
        """The named provider (default when ``None``), validated.

        Raises an :class:`AIError` with a hint the UI can show verbatim.
        """
        if not self.providers:
            raise AIError(
                "disabled",
                "no AI provider is configured",
                hint="add one in ai.toml, or run `spacesage ai check` for the expected file",
            )
        chosen = name or self.default_provider
        if chosen is None:
            chosen = self.providers[0].name
        for provider in self.providers:
            if provider.name == chosen:
                return provider.validated()
        known = ", ".join(provider.name for provider in self.providers)
        raise AIError(
            "invalid_config",
            f"no provider named {chosen!r}",
            hint=f"configured providers: {known}",
            provider=chosen,
        )

    def provider_names(self) -> tuple[str, ...]:
        """Names of every configured provider, in file order."""
        return tuple(provider.name for provider in self.providers)

    def is_ready(self) -> bool:
        """True when a usable provider can be resolved right now (key included)."""
        if not self.enabled or not self.providers:
            return False
        try:
            self.provider()
        except AIError:
            return False
        return not self.key_problem()

    def key_problem(self) -> str:
        """Why the selected provider cannot authenticate, or ``""``.

        Only a provider that *names* a key source can be missing a key: a local
        server (or an anonymous gateway with no key configured) is left alone.
        """
        try:
            provider = self.provider()
        except AIError as exc:
            return str(exc)
        if provider.is_local or not provider.has_key_source():
            return ""
        try:
            if provider.api_key() is not None:
                return ""
        except AIError as exc:
            return str(exc)
        where = provider.api_key_env or provider.api_key_file or "the configured key source"
        return (
            f"the API key for {provider.name} is not set ({where})"
            if provider.api_key_env
            else f"the API key file for {provider.name} is empty ({where})"
        )

    def reason(self) -> str:
        """Why the layer is off (empty when it is ready)."""
        if not self.enabled:
            return "AI is off: no provider is configured (see docs/ai.md)"
        try:
            self.provider()
        except AIError as exc:
            return str(exc)
        return self.key_problem()

    # -- editing (returns new configs; the file is the user's) ------------- #

    def with_provider(self, provider: ProviderConfig) -> AIConfig:
        """Add or replace a provider by name, keeping file order."""
        if any(item.name == provider.name for item in self.providers):
            providers = tuple(
                provider if item.name == provider.name else item for item in self.providers
            )
        else:
            providers = (*self.providers, provider)
        default = self.default_provider or provider.name
        return replace(self, providers=providers, default_provider=default, enabled=True)

    def without_provider(self, name: str) -> AIConfig:
        """Remove a provider by name (the default moves to the first survivor)."""
        providers = tuple(item for item in self.providers if item.name != name)
        default = self.default_provider
        if default == name or default is None:
            default = providers[0].name if providers else None
        return replace(
            self,
            providers=providers,
            default_provider=default,
            enabled=bool(providers) and self.enabled,
        )

    def with_settings(self, **overrides: object) -> AIConfig:
        """Copy with policy switches replaced.

        Unknown names raise (a typo must not pass silently) and numeric bounds
        are clamped, so a caller can never build a config the runner would have
        to defend against.
        """
        values: dict[str, object] = {}
        for key, value in overrides.items():
            if key not in _AI_KEYS:
                known = ", ".join(sorted(_AI_KEYS))
                raise AIError(
                    INVALID_CONFIG,
                    f"unknown AI setting {key!r}",
                    hint=f"known settings: {known}",
                )
            if key in {"batch_size", "max_items", "max_prompt_chars", "max_tokens"}:
                if not isinstance(value, int) or isinstance(value, bool):
                    raise AIError(INVALID_CONFIG, f"{key} must be an integer")
                values[key] = max(1, value)
            elif key == "retries":
                if not isinstance(value, int) or isinstance(value, bool):
                    raise AIError(INVALID_CONFIG, "retries must be an integer")
                values[key] = max(0, value)
            elif key == "temperature":
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise AIError(INVALID_CONFIG, "temperature must be a number")
                values[key] = min(2.0, max(0.0, float(value)))
            else:
                values[key] = value
        return replace(self, **values)  # type: ignore[arg-type]

    def with_overrides(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key_env: str | None = None,
        local_only: bool | None = None,
        redact_paths: bool | None = None,
        streaming: bool | None = None,
        off: bool = False,
    ) -> AIConfig:
        """Apply command-line / environment overrides to a loaded config."""
        updated = self
        if off:
            return replace(updated, enabled=False)
        selected = provider or updated.default_provider
        if model or base_url or api_key_env or provider:
            target = selected or "custom"
            try:
                current = updated.provider(provider)
            except AIError:
                kind = "custom"
                if model is None and base_url is None:
                    raise AIError(
                        INVALID_CONFIG,
                        f"no provider named {target!r} to override",
                        hint=(
                            "configured providers: "
                            f"{', '.join(updated.provider_names()) or 'none'}; "
                            "run `spacesage ai check`"
                        ),
                    ) from None
                current = ProviderConfig.from_preset(target, kind)
            current = replace(
                current,
                model=model or current.model,
                base_url=base_url or current.base_url,
                api_key_env=api_key_env or current.api_key_env,
            )
            updated = updated.with_provider(current)
            if provider:
                updated = replace(updated, default_provider=current.name)
        if local_only is not None:
            updated = replace(updated, local_only=local_only)
        if redact_paths is not None:
            updated = replace(updated, redact_paths=redact_paths)
        if streaming is not None:
            updated = replace(updated, streaming=streaming)
        return updated

    # -- serialisation ------------------------------------------------------ #

    def cache_path(self, env: Mapping[str, str] | None = None) -> Path:
        """Directory holding the validated-answer cache."""
        if self.cache_dir:
            return Path(self.cache_dir).expanduser()
        return default_cache_dir(env)

    def to_dict(self, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
        """JSON-ready view of the whole configuration (never any secret)."""
        return {
            "enabled": self.enabled,
            "ready": self.is_ready(),
            "reason": self.reason(),
            "default_provider": self.default_provider,
            "providers": [provider.to_dict(env=env) for provider in self.providers],
            "streaming": self.streaming,
            "redact_paths": self.redact_paths,
            "local_only": self.local_only,
            "cache": self.cache,
            "cache_dir": str(self.cache_path(env)),
            "retries": self.retries,
            "batch_size": self.batch_size,
            "max_items": self.max_items,
            "max_prompt_chars": self.max_prompt_chars,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "source": self.source,
        }

    def render_toml(self) -> str:
        """The exact file :meth:`save` writes (also the ``check`` output)."""
        lines = [
            "# SpaceSage AI providers (docs/ai.md).",
            "# API keys are read from the environment variables named here -",
            "# never stored in this file.",
            "",
            "[ai]",
            f"enabled = {_toml_bool(self.enabled)}",
        ]
        if self.default_provider:
            lines.append(f"default_provider = {_toml_string(self.default_provider)}")
        lines.append(f"streaming = {_toml_bool(self.streaming)}")
        lines.append(f"redact_paths = {_toml_bool(self.redact_paths)}")
        lines.append(f"local_only = {_toml_bool(self.local_only)}")
        lines.append(f"cache = {_toml_bool(self.cache)}")
        if self.cache_dir:
            lines.append(f"cache_dir = {_toml_string(self.cache_dir)}")
        lines.append(f"retries = {self.retries}")
        lines.append(f"batch_size = {self.batch_size}")
        lines.append(f"max_items = {self.max_items}")
        lines.append(f"max_prompt_chars = {self.max_prompt_chars}")
        lines.append(f"max_tokens = {self.max_tokens}")
        lines.append(f"temperature = {_toml_number(self.temperature)}")
        for provider in self.providers:
            lines.append("")
            lines.append(provider.render_toml())
        return "\n".join(lines).rstrip() + "\n"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, source: str | None = None) -> AIConfig:
        """Build a config from parsed TOML, validating every key."""
        section = data.get("ai", data)
        if not isinstance(section, Mapping):
            raise AIError(
                INVALID_CONFIG,
                "the [ai] section must be a table",
                hint="see docs/ai.md for the expected shape",
            )
        unknown = sorted(set(section) - _AI_KEYS)
        if unknown:
            raise AIError(
                INVALID_CONFIG,
                f"unknown key(s) in [ai]: {', '.join(unknown)}",
                hint=f"valid keys: {', '.join(sorted(_AI_KEYS))}",
            )
        raw_providers = section.get("providers", [])
        if not isinstance(raw_providers, Sequence) or isinstance(raw_providers, (str, bytes)):
            raise AIError(
                INVALID_CONFIG,
                "[[ai.providers]] must be an array of tables",
                hint="write one [[ai.providers]] block per provider",
            )
        providers = tuple(
            _provider_from_mapping(index, entry) for index, entry in enumerate(raw_providers)
        )
        names = [provider.name for provider in providers]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise AIError(
                INVALID_CONFIG,
                f"duplicate provider name(s): {', '.join(duplicates)}",
                hint="provider names must be unique - they are how the provider is selected",
            )
        enabled = _flag(section.get("enabled"), default=bool(providers))
        default = section.get("default_provider")
        if default is not None and not isinstance(default, str):
            raise AIError(INVALID_CONFIG, "default_provider must be a string")
        if default and default not in names:
            raise AIError(
                INVALID_CONFIG,
                f"default_provider {default!r} is not one of {', '.join(names) or 'nothing'}",
                hint="set default_provider to a configured provider name",
            )
        return cls(
            enabled=enabled and bool(providers),
            default_provider=default or (names[0] if names else None),
            providers=providers,
            streaming=_flag(section.get("streaming"), default=True),
            redact_paths=_flag(section.get("redact_paths"), default=False),
            local_only=_flag(section.get("local_only"), default=False),
            cache=_flag(section.get("cache"), default=True),
            cache_dir=_optional_string(section.get("cache_dir"), "cache_dir"),
            retries=_int_setting(section.get("retries"), "retries", default=2, low=0, high=10),
            batch_size=_int_setting(
                section.get("batch_size"), "batch_size", default=8, low=1, high=64
            ),
            max_items=_int_setting(section.get("max_items"), "max_items", default=200, low=1),
            max_prompt_chars=_int_setting(
                section.get("max_prompt_chars"), "max_prompt_chars", default=24_000, low=1_000
            ),
            max_tokens=_int_setting(section.get("max_tokens"), "max_tokens", default=1200, low=64),
            temperature=_float_setting(section.get("temperature"), "temperature", default=0.2),
            source=source,
        )

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
        **overrides: object,
    ) -> AIConfig:
        """Load the configuration file (missing file = a disabled config).

        ``path`` wins over ``$SPACESAGE_AI_CONFIG``; environment overrides are
        applied last, so a script can drive the layer without writing files.
        """
        environ = os.environ if env is None else env
        target = Path(path) if path is not None else default_config_path(environ)
        if target.is_file():
            try:
                data = tomllib.loads(target.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise AIError(
                    INVALID_CONFIG,
                    f"cannot read the AI configuration at {target}: {exc}",
                    hint="fix the TOML, or delete the file to start over (see docs/ai.md)",
                ) from exc
            config = cls.from_mapping(data, source=str(target))
        else:
            config = cls(source=None)
        config = config.with_overrides(
            provider=_env_string(environ, PROVIDER_ENV_VAR),
            model=_env_string(environ, MODEL_ENV_VAR),
            base_url=_env_string(environ, BASE_URL_ENV_VAR),
            api_key_env=_env_string(environ, KEY_ENV_ENV_VAR),
            local_only=_env_flag(environ, LOCAL_ONLY_ENV_VAR),
            redact_paths=_env_flag(environ, REDACT_ENV_VAR),
            off=_env_flag(environ, OFF_ENV_VAR) or False,
        )
        return config.with_settings(**overrides)

    def save(self, path: str | Path | None = None, *, env: Mapping[str, str] | None = None) -> Path:
        """Write the configuration (atomic, ``0600`` on POSIX); returns the path."""
        environ = os.environ if env is None else env
        target = Path(path) if path is not None else default_config_path(environ)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = self.render_toml()
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, target)
        return target


def _env_string(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    return value.strip() if value and value.strip() else None


def _env_flag(env: Mapping[str, str], key: str) -> bool | None:
    value = env.get(key)
    if value is None or not value.strip():
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _optional_string(value: object, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AIError(INVALID_CONFIG, f"{key} must be a string")
    return value.strip() or None


def _int_setting(
    value: object, key: str, *, default: int, low: int, high: int | None = None
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise AIError(INVALID_CONFIG, f"{key} must be a whole number")
    if value < low or (high is not None and value > high):
        span = f"{low}..{high}" if high is not None else f">= {low}"
        raise AIError(INVALID_CONFIG, f"{key} must be {span}, found {value}")
    return value


def _float_setting(value: object, key: str, *, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AIError(INVALID_CONFIG, f"{key} must be a number")
    return float(value)


def _provider_from_mapping(index: int, entry: object) -> ProviderConfig:
    if not isinstance(entry, Mapping):
        raise AIError(
            INVALID_CONFIG,
            f"provider #{index + 1} is not a table",
            hint="each provider is a [[ai.providers]] block",
        )
    unknown = sorted(set(entry) - _PROVIDER_KEYS)
    if unknown:
        raise AIError(
            INVALID_CONFIG,
            f"unknown key(s) in provider #{index + 1}: {', '.join(unknown)}",
            hint=f"valid keys: {', '.join(sorted(_PROVIDER_KEYS))}",
        )
    name = _optional_string(entry.get("name"), "name") or f"provider{index + 1}"
    kind = _optional_string(entry.get("preset"), "preset") or _optional_string(
        entry.get("kind"), "kind"
    )
    if kind is None:
        kind = name if name in PRESETS else "custom"
    if kind not in PRESETS:
        raise AIError(
            INVALID_CONFIG,
            f"unknown provider preset {kind!r} (provider {name})",
            hint=f"presets: {', '.join(sorted(PRESETS))}",
        )
    base_url = _optional_string(entry.get("base_url"), "base_url")
    model = _optional_string(entry.get("model"), "model")
    api_key_env = _optional_string(entry.get("api_key_env"), "api_key_env")
    api_key_file = _optional_string(entry.get("api_key_file"), "api_key_file")
    headers_raw = entry.get("extra_headers", {})
    if not isinstance(headers_raw, Mapping):
        raise AIError(INVALID_CONFIG, f"provider {name}: extra_headers must be a table")
    headers = {str(key): str(value) for key, value in headers_raw.items()}
    return ProviderConfig.from_preset(
        name,
        kind,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        api_key_file=api_key_file,
        pricing_in=_optional_float(entry.get("pricing_in"), "pricing_in"),
        pricing_out=_optional_float(entry.get("pricing_out"), "pricing_out"),
        timeout_s=_optional_float(entry.get("timeout_s"), "timeout_s") or 60.0,
        stream=_flag(entry.get("stream"), default=True),
        json_mode=_flag(entry.get("json_mode"), default=False),
        extra_headers=headers,
    )


def _optional_float(value: object, key: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AIError(INVALID_CONFIG, f"{key} must be a number")
    return float(value)


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def preset_choices() -> tuple[str, ...]:
    """Preset kinds the UI offers as quick-add buttons, in a friendly order."""
    return ("ollama", "lmstudio", "openai", "openrouter", "custom")

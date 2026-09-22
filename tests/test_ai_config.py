"""Configuration: presets, the file, and the two rules that must never bend.

The AI layer is off until someone configures it, and ``local_only`` is enforced
at the one place a request could leave the machine - never as a UI hint.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from spacesage.ai import AIConfig, AIError, ProviderConfig
from spacesage.ai import config as ai_config

POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits (key_warning is a no-op on Windows)"
)


def test_there_is_no_provider_until_one_is_configured() -> None:
    config = AIConfig.load(env={})

    assert config.enabled is False
    assert config.providers == ()
    assert config.is_ready() is False
    assert "no provider" in config.reason()
    assert config.provider_names() == ()


def test_every_preset_is_complete_and_valid() -> None:
    assert ai_config.preset_choices()
    for name in ai_config.preset_choices():
        provider = ProviderConfig.from_preset(name, kind=name)
        if name == "custom":  # the template: everything blank on purpose
            assert provider.base_url == "" and provider.model == ""
            continue
        assert provider.base_url.startswith(("http://", "https://"))
        assert provider.model
        assert provider.validated() is provider  # nothing to complain about


def test_the_opencode_preset_targets_the_zen_gateway() -> None:
    """The gateway SpaceSage sends OpenCode's routing headers to, and how to reach it."""
    preset = ai_config.PRESETS["opencode"]
    provider = ProviderConfig.from_preset("opencode", kind="opencode")

    assert set(ai_config.preset_choices()) == set(ai_config.PRESETS), "every preset is offered"
    assert "opencode" in ai_config.preset_choices()
    assert preset.title == "OpenCode Zen"
    assert preset.base_url == "https://opencode.ai/zen/v1"
    assert preset.api_key_env == "OPENCODE_API_KEY"
    assert preset.model == "deepseek-v4-flash"  # a /chat/completions model on this gateway
    assert preset.local is False
    assert preset.pricing_in is None and preset.pricing_out is None
    assert "x-opencode-session" in preset.note  # the quick-add tooltip says what it sends

    assert provider.kind == "opencode"
    assert provider.host() == "opencode.ai"
    assert provider.is_local is False
    assert provider.chat_url() == "https://opencode.ai/zen/v1/chat/completions"
    assert provider.models_url() == "https://opencode.ai/zen/v1/models"
    assert provider.key_present(env={}) is False
    assert provider.key_present(env={"OPENCODE_API_KEY": "stub-key"}) is True


def test_a_cloud_provider_without_a_key_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ProviderConfig.from_preset("openai", kind="openai", api_key_env="OPENAI_API_KEY")
    config = AIConfig(providers=(provider,), default_provider="openai", enabled=True)

    assert provider.key_present(env={}) is False
    assert config.is_ready() is False
    assert "OPENAI_API_KEY" in config.reason()
    assert "not set" in config.reason()
    assert config.key_problem()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key")
    assert provider.key_present() is True
    ready = AIConfig(providers=(provider,), default_provider="openai", enabled=True)
    assert ready.is_ready() is True
    assert ready.reason() == ""


def test_a_provider_with_no_key_source_is_left_alone() -> None:
    # an anonymous gateway on the LAN: nothing to check, nothing to warn about
    provider = ProviderConfig(
        name="lab", kind="custom", base_url="http://10.0.0.9:8080/v1", model="local-model"
    )
    config = AIConfig(providers=(provider,), default_provider="lab", enabled=True)

    assert provider.has_key_source() is False
    assert config.key_problem() == ""
    assert config.is_ready() is True


@POSIX_ONLY
def test_a_world_readable_key_file_is_flagged(tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("sk-not-a-real-key\n", encoding="utf-8")
    key_file.chmod(0o644)
    provider = ProviderConfig(
        name="gateway",
        kind="custom",
        base_url="https://gateway.example.com/v1",
        model="gpt-4o-mini",
        api_key_file=str(key_file),
    )

    warning = provider.key_warning(env={})

    assert warning is not None and "chmod 600" in warning
    assert provider.key_present(env={}) is True  # readable, just not private


def test_a_local_preset_never_needs_a_key() -> None:
    provider = ProviderConfig.from_preset("ollama", kind="ollama")

    assert provider.is_local is True
    assert provider.key_warning(env={}) is None
    assert provider.api_key(env={}) is None
    assert provider.has_key_source() is False  # nothing to look for
    assert provider.key_present(env={}) is True  # and nothing to complain about


def test_the_config_file_round_trips(tmp_path: Path) -> None:
    provider = ProviderConfig.from_preset("openai", kind="openai", model="gpt-4o-mini")
    config = AIConfig(
        providers=(provider,),
        default_provider="openai",
        cache_dir=str(tmp_path / "cache"),
        batch_size=4,
        redact_paths=True,
        enabled=True,
    )

    path = config.save(tmp_path / "ai.toml")
    loaded = AIConfig.load(env={"SPACESAGE_AI_CONFIG": str(path)})

    assert loaded.enabled is True
    assert loaded.default_provider == "openai"
    assert loaded.batch_size == 4
    assert loaded.redact_paths is True
    assert loaded.providers[0].model == "gpt-4o-mini"
    assert loaded.providers[0].base_url == provider.base_url
    assert loaded.source == str(path)


def test_a_missing_file_leaves_the_layer_off(tmp_path: Path) -> None:
    config = AIConfig.load(env={"SPACESAGE_AI_CONFIG": str(tmp_path / "absent.toml")})

    assert config.enabled is False
    assert config.provider_names() == ()


def test_a_broken_config_file_is_a_coded_error(tmp_path: Path) -> None:
    path = tmp_path / "ai.toml"
    path.write_text("this is not toml [", encoding="utf-8")

    with pytest.raises(AIError) as caught:
        AIConfig.load(env={"SPACESAGE_AI_CONFIG": str(path)})

    assert caught.value.code == "invalid_config"
    assert str(path) in caught.value.message


def test_the_environment_can_override_settings(tmp_path: Path) -> None:
    provider = ProviderConfig.from_preset("ollama", kind="ollama")
    path = tmp_path / "ai.toml"
    AIConfig(providers=(provider,), default_provider="ollama").save(path)

    loaded = AIConfig.load(
        env={
            "SPACESAGE_AI_CONFIG": str(path),
            "SPACESAGE_AI_MODEL": "qwen2.5:14b",
            "SPACESAGE_AI_LOCAL_ONLY": "1",
            "SPACESAGE_AI_REDACT_PATHS": "1",
        }
    )

    assert loaded.providers[0].model == "qwen2.5:14b"
    assert loaded.local_only is True
    assert loaded.redact_paths is True
    assert loaded.provider().model == "qwen2.5:14b"


def test_the_off_switch_wins() -> None:
    provider = ProviderConfig.from_preset("ollama", kind="ollama")

    off = AIConfig.load(
        env={
            "SPACESAGE_AI_OFF": "1",
            "SPACESAGE_AI_PROVIDER": "ollama",
            "SPACESAGE_AI_BASE_URL": provider.base_url,
        }
    )

    assert off.enabled is False


def test_with_overrides_selects_another_provider(tmp_path: Path) -> None:
    ollama = ProviderConfig.from_preset("ollama", kind="ollama")
    openai = ProviderConfig.from_preset("openai", kind="openai")
    base = AIConfig(providers=(ollama, openai), default_provider="ollama", cache_dir=str(tmp_path))

    switched = base.with_overrides(provider="openai", model="gpt-4o")

    assert switched.default_provider == "openai"
    assert switched.provider().name == "openai"
    assert switched.provider().model == "gpt-4o"
    assert base.provider().name == "ollama"  # the original is untouched
    assert [provider.name for provider in switched.providers] == ["ollama", "openai"]


def test_with_overrides_can_point_at_a_new_base_url(tmp_path: Path) -> None:
    base = AIConfig(providers=(ProviderConfig.from_preset("ollama", kind="ollama"),))

    pointed = base.with_overrides(base_url="http://127.0.0.1:1234/v1", model="mistral")

    provider = pointed.provider()
    assert provider.base_url == "http://127.0.0.1:1234/v1"
    assert provider.model == "mistral"
    assert provider.is_local is True


def test_an_unknown_provider_is_refused() -> None:
    config = AIConfig(providers=(ProviderConfig.from_preset("ollama", kind="ollama"),))

    with pytest.raises(AIError) as caught:
        config.with_overrides(provider="skynet")

    assert caught.value.code == "invalid_config"
    assert "skynet" in caught.value.message
    assert "ollama" in caught.value.hint


def test_settings_are_clamped_to_sane_values() -> None:
    config = AIConfig()

    assert config.with_settings(max_items=0).max_items == 1
    assert config.with_settings(batch_size=0).batch_size == 1
    assert config.with_settings(max_prompt_chars=-5).max_prompt_chars == 1
    assert config.with_settings(temperature=-1.0).temperature == 0.0
    assert config.with_settings(temperature=9.0).temperature == 2.0
    assert config.with_settings(retries=-3).retries == 0
    assert config.with_settings(cache=False).cache is False
    assert config.with_settings(cache=True).cache is True


def test_an_unknown_setting_is_refused() -> None:
    with pytest.raises(AIError) as caught:
        AIConfig().with_settings(batch_sizes=4)  # a typo, not a silent no-op

    assert caught.value.code == "invalid_config"
    assert "batch_sizes" in caught.value.message
    assert "batch_size" in caught.value.hint


def test_a_non_loopback_endpoint_is_not_local() -> None:
    assert ai_config.is_loopback("localhost") is True
    assert ai_config.is_loopback("127.0.0.1") is True
    assert ai_config.is_loopback("::1") is True
    assert ai_config.is_loopback("10.0.0.5") is False
    assert ai_config.is_loopback("example.com") is False
    assert ai_config.is_loopback("") is False


def test_local_only_blocks_anything_but_the_machine() -> None:
    with pytest.raises(AIError) as caught:
        ai_config.enforce_local_only("https://api.openai.com/v1", provider="openai")

    assert caught.value.code == "local_only"
    assert "api.openai.com" in caught.value.message
    assert "local_only" in caught.value.hint

    ai_config.enforce_local_only("http://127.0.0.1:11434/v1", provider="ollama")  # no raise


def test_a_url_without_a_scheme_or_host_is_refused() -> None:
    with pytest.raises(AIError) as caught:
        ai_config.validate_base_url("127.0.0.1:11434")

    assert caught.value.code == "invalid_config"
    with pytest.raises(AIError):
        ai_config.validate_base_url("http://")


def test_the_cache_path_follows_the_environment(tmp_path: Path) -> None:
    env = {"SPACESAGE_AI_CACHE_DIR": str(tmp_path / "custom")}

    assert AIConfig().cache_path(env) == tmp_path / "custom"
    assert ai_config.default_cache_dir(env) == tmp_path / "custom"


def test_the_config_home_is_per_platform(tmp_path: Path) -> None:
    windows = ai_config.default_config_path({"APPDATA": r"C:\Users\matija\AppData\Roaming"})

    assert windows.name == "ai.toml"
    if os.name == "nt":
        # Windows keeps its config under the profile's AppData; XDG is not read.
        assert ai_config.default_config_path({"XDG_CONFIG_HOME": str(tmp_path)}) == (
            Path.home() / "AppData" / "Roaming" / "spacesage" / "ai.toml"
        )
    else:
        posix = ai_config.default_config_path({"XDG_CONFIG_HOME": str(tmp_path)})
        assert posix == tmp_path / "spacesage" / "ai.toml"


def test_providers_keep_their_order_when_replaced(tmp_path: Path) -> None:
    first = ProviderConfig.from_preset("ollama", kind="ollama")
    second = ProviderConfig.from_preset("openai", kind="openai")
    config = AIConfig(providers=(first, second), default_provider="ollama")

    replaced = config.with_provider(replace(second, model="gpt-4o"))

    assert [provider.name for provider in replaced.providers] == ["ollama", "openai"]
    assert replaced.providers[1].model == "gpt-4o"
    assert replaced.without_provider("ollama").provider_names() == ("openai",)
    assert config.providers[1].model != "gpt-4o"


def test_a_provider_can_be_added_to_an_empty_config() -> None:
    config = AIConfig()
    provider = ProviderConfig.from_preset("ollama", kind="ollama")

    added = config.with_provider(provider)

    assert added.provider_names() == ("ollama",)
    assert added.default_provider == "ollama"
    assert added.enabled is True


def test_pricing_is_optional_and_rendered() -> None:
    priced = ProviderConfig.from_preset("openai", kind="openai", pricing_in=0.15, pricing_out=0.6)

    assert "pricing_in" in priced.render_toml()
    assert priced.to_dict()["pricing_out"] == 0.6
    assert ProviderConfig.from_preset("ollama", kind="ollama").pricing_in is None

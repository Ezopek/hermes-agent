"""tts.elevenlabs voice settings: what the real config loader yields reaches the SDK settings object.

``load_config`` deep-merges DEFAULT_CONFIG's ``voice_settings: {}`` under a user's config, so the
legacy flat keys (``stability``, ...) must still apply when nothing nested is set.
"""

import sys
import types

import pytest


@pytest.fixture
def tts_section(tmp_path, monkeypatch):
    """Write config.yaml into a temp HERMES_HOME and read ``tts.elevenlabs`` back via the real loader."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Only the SDK class is faked: the elevenlabs package is an optional extra.
    sdk = types.ModuleType("elevenlabs")
    sdk.VoiceSettings = lambda **kw: ("VoiceSettings", kw)
    monkeypatch.setitem(sys.modules, "elevenlabs", sdk)

    def load(yaml_text):
        from tools.tts_tool import _load_tts_config

        (tmp_path / "config.yaml").write_text(yaml_text, encoding="utf-8")
        return _load_tts_config()["elevenlabs"]

    return load


def test_legacy_flat_keys_apply_through_the_loader(tts_section):
    from tools.tts_tool_providers import _make_elevenlabs_voice_settings

    el = tts_section("tts:\n  elevenlabs:\n    stability: 0.4\n    similarity_boost: 0.8\n")
    assert _make_elevenlabs_voice_settings(el) == ("VoiceSettings", {"stability": 0.4, "similarity_boost": 0.8})


def test_nested_settings_win_over_flat_keys(tts_section):
    from tools.tts_tool_providers import _make_elevenlabs_voice_settings

    el = tts_section("tts:\n  elevenlabs:\n    stability: 0.4\n    voice_settings:\n      style: 0.5\n")
    assert _make_elevenlabs_voice_settings(el) == ("VoiceSettings", {"style": 0.5})


def test_nothing_configured_sends_no_settings(tts_section):
    from tools.tts_tool_providers import _make_elevenlabs_voice_settings

    assert _make_elevenlabs_voice_settings(tts_section("tts:\n  provider: elevenlabs\n")) is None


@pytest.mark.parametrize("value", ["[]", "'loud'"])
def test_non_mapping_voice_settings_is_rejected(tts_section, value):
    from tools.tts_tool_providers import _make_elevenlabs_voice_settings

    el = tts_section(f"tts:\n  elevenlabs:\n    stability: 0.4\n    voice_settings: {value}\n")
    with pytest.raises(ValueError, match="voice_settings must be a mapping"):
        _make_elevenlabs_voice_settings(el)

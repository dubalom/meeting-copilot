from pathlib import Path

import pytest

from copilot.audio.devices import (
    AudioDevice,
    pick_by_name,
    pick_linux_monitor,
    pick_mac_loopback,
    pick_microphone,
)
from copilot.config import Config, Secrets, load_config


def _dev(i, name, inp=1, out=0, rate=48000.0, api="Core Audio"):
    return AudioDevice(i, name, api, inp, out, rate)


MAC = [
    _dev(0, "BlackHole 2ch", inp=2, out=2),
    _dev(1, "MacBook Pro Microphone", inp=1),
    _dev(2, "MacBook Pro Speakers", inp=0, out=2),
    _dev(3, "Multi-Output Device", inp=0, out=2),
    _dev(4, "AirPods Pro", inp=1, out=2, rate=24000.0),
]


def test_pick_by_name_exact_then_substring():
    assert pick_by_name(MAC, "airpods pro").index == 4
    assert pick_by_name(MAC, "BlackHole").index == 0
    assert pick_by_name(MAC, "Speakers") is None  # output-only → not an input device
    assert pick_by_name(MAC, "Speakers", need_input=False).index == 2
    assert pick_by_name(MAC, "") is None
    assert pick_by_name(MAC, "nonexistent") is None


def test_pick_mac_loopback_prefers_exact_2ch():
    devs = MAC + [_dev(9, "BlackHole 16ch", inp=16, out=16)]
    assert pick_mac_loopback(devs).name == "BlackHole 2ch"
    assert pick_mac_loopback([_dev(9, "BlackHole 16ch", inp=16)]).index == 9
    assert pick_mac_loopback([d for d in MAC if "BlackHole" not in d.name]) is None


def test_pick_microphone_skips_loopback_default():
    # default input is BlackHole (user set it by accident) → still choose the real mic
    assert pick_microphone(MAC, default_idx=0).name == "MacBook Pro Microphone"
    assert pick_microphone(MAC, default_idx=4).name == "AirPods Pro"
    assert pick_microphone(MAC, default_idx=None).name == "MacBook Pro Microphone"
    assert pick_microphone([MAC[2]], default_idx=None) is None


def test_pick_linux_monitor():
    devs = [_dev(0, "pulse", api="ALSA"), _dev(1, "Monitor of Built-in Audio", api="ALSA")]
    assert pick_linux_monitor(devs).index == 1
    assert pick_linux_monitor(devs[:1]) is None


# ----------------------------------------------------------------------------- config
def test_config_defaults_match_brief():
    cfg = Config()
    assert cfg.audio.sample_rate == 16000 and cfg.audio.them_device == "auto"
    assert cfg.stt.model == "nova-3" and cfg.stt.language == "multi"
    assert cfg.brain.answer_model == "claude-haiku-5-5"
    assert cfg.brain.summary_model == "claude-sonnet-5-5"
    assert cfg.brain.filter_model == "claude-haiku-5-5"
    assert cfg.hotkeys.answer == "<alt>+<space>"
    assert cfg.paths.context_dir.is_absolute()


def test_config_from_yaml(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "audio:\n  them_device: 'BlackHole 2ch'\n  transcribe_me: false\n"
        "brain:\n  provider: agent_sdk\n  max_tokens: 200\n"
        "secrets:\n  anthropic_api_key: should-be-ignored\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.audio.them_device == "BlackHole 2ch"
    assert cfg.audio.transcribe_me is False
    assert cfg.brain.provider == "agent_sdk" and cfg.brain.max_tokens == 200
    assert cfg.stt.provider == "deepgram"  # untouched section keeps defaults
    assert cfg.secrets.anthropic_api_key != "should-be-ignored"


def test_config_rejects_bad_provider(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("brain:\n  provider: openai\n", encoding="utf-8")
    with pytest.raises(Exception):
        load_config(p)


def test_secrets_missing_logic(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = Secrets(_env_file=None)
    assert s.missing_for(stt_provider="deepgram", brain_provider="anthropic") == [
        "DEEPGRAM_API_KEY",
        "ANTHROPIC_API_KEY",
    ]
    assert s.missing_for(stt_provider="whisper", brain_provider="agent_sdk") == []
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    assert Secrets(_env_file=None).missing_for(stt_provider="deepgram", brain_provider="agent_sdk") == []

"""Typed configuration: config.yaml (non-secret settings) + .env (secrets).

Usage:
    from copilot.config import load_config
    cfg = load_config()            # looks for config.yaml / .env in the project root
    cfg.audio.them_device          # "auto"
    cfg.secrets.deepgram_api_key   # from .env / environment
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root (config.yaml, context/, data/). Overridable for containers: COPILOT_HOME=/app
PROJECT_ROOT = Path(os.environ.get("COPILOT_HOME") or Path(__file__).resolve().parent.parent)


# --------------------------------------------------------------------------- #
# config.yaml sections
# --------------------------------------------------------------------------- #
class AudioConfig(BaseModel):
    them_device: str = "auto"
    me_device: str = "auto"
    transcribe_me: bool = True
    frame_ms: int = Field(40, ge=10, le=100)
    sample_rate: int = 16_000  # fixed pipeline format; not meant to be changed


class SttConfig(BaseModel):
    provider: Literal["deepgram", "whisper"] = "deepgram"
    model: str = "nova-3"
    language: str = "multi"
    keyterms_from_glossary: bool = True
    max_keyterms: int = 50
    endpointing_ms: int = 100
    utterance_end_ms: int = 1000
    phrase_gap_seconds: float = 0.9  # our own phrase boundary on top of Deepgram endpointing
    keepalive_seconds: float = 8.0
    reconnect_max_seconds: float = 8.0
    buffer_seconds: float = 30.0  # audio kept while reconnecting


# Speech languages offered in the UI (Deepgram Nova-3). "multi" = code-switching between
# en/es/fr/de/hi/ru/pt/ja/it/nl in one stream (checked in Deepgram docs, Oct 2026).
STT_LANGUAGES: list[tuple[str, str]] = [
    ("multi", "Авто (рус + англ и др.)"),
    ("ru", "Русский"),
    ("en", "English"),
    ("es", "Español"),
    ("de", "Deutsch"),
    ("fr", "Français"),
    ("pt", "Português"),
    ("it", "Italiano"),
    ("nl", "Nederlands"),
    ("hi", "हिन्दी"),
    ("ja", "日本語"),
]
ANSWER_LANGUAGES: list[tuple[str, str]] = [
    ("auto", "Как в вопросе"),
    ("ru", "Всегда по-русски"),
    ("en", "Always in English"),
]


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    session_grace_seconds: float = 300.0  # keep a session alive after its last client drops
    public_url: str = ""  # e.g. https://saydar.ai — only used to print the link (env COPILOT_PUBLIC_URL)
    access_log: bool = False  # off: WebSocket URLs carry ?token=


class StoreConfig(BaseModel):
    model_config = ConfigDict(validate_default=True)

    db_url: str = "sqlite:///data/copilot.db"
    recordings_dir: Path = Path("data/recordings")

    @field_validator("recordings_dir", mode="after")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        return v if v.is_absolute() else (PROJECT_ROOT / v)


class EngineConfig(BaseModel):
    """One selectable answer engine. Thinking controls differ per model family (measured Oct 2026):
    Haiku 5.5 accepts {"type": "disabled"}; Sonnet 5.5 turns thinking off with {"type": "between_tools"};
    Opus 5.5 / Fable 5.1 only allow {"type": "adaptive"} plus output_config.effort."""

    label: str
    model: str
    max_tokens: int = 300
    thinking: dict | None = None
    effort: str | None = None  # low | medium | high (sent as output_config.effort)
    detailed: bool = False  # allow a longer, more structured answer


def _default_engines() -> dict[str, EngineConfig]:
    return {
        "fast": EngineConfig(label="Быстро", model="claude-haiku-5-5", max_tokens=300, thinking={"type": "disabled"}),
        "smart": EngineConfig(label="Умнее", model="claude-sonnet-5-5", max_tokens=700,
                              thinking={"type": "between_tools"}, detailed=True),
        "deep": EngineConfig(label="Глубоко", model="claude-opus-5-5", max_tokens=4000,
                             thinking={"type": "adaptive"}, effort="medium", detailed=True),
        "max": EngineConfig(label="Максимум", model="claude-fable-5-1", max_tokens=4000,
                            thinking={"type": "adaptive"}, effort="medium", detailed=True),
    }


class BrainConfig(BaseModel):
    provider: Literal["anthropic", "agent_sdk"] = "anthropic"
    engines: dict[str, EngineConfig] = Field(default_factory=_default_engines)
    default_engine: str = "fast"  # live answers + Auto
    more_engine: str = "max"  # the «Подробнее» button
    answer_model: str = "claude-haiku-5-5"  # legacy: used when an engine is not found
    summary_model: str = "claude-sonnet-5-5"
    summary_max_tokens: int = 6000
    filter_model: str = "claude-haiku-5-5"
    max_tokens: int = 300
    temperature: float = 0.2  # not sent: current Messages API/SDK no longer accept it
    window_seconds: int = 90


class KnowledgeConfig(BaseModel):
    """Private chat exports (data/private/<user_id>/). Each user only ever sees their own store."""

    model_config = ConfigDict(validate_default=True)

    enabled: bool = True
    dir: Path = Path("data/private")
    top_k: int = 3
    max_chars: int = 3500  # budget of retrieved text per answer

    @field_validator("dir", mode="after")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        override = os.environ.get("COPILOT_PRIVATE_DIR")  # tests point this at a temp folder
        if override and v == Path("data/private"):
            return Path(override)
        return v if v.is_absolute() else (PROJECT_ROOT / v)


class TriggerConfig(BaseModel):
    auto_default: bool = False
    debounce_seconds: float = 1.5
    min_words_auto: int = 4


class HotkeysConfig(BaseModel):
    answer: str = "<alt>+<space>"
    toggle_auto: str = "<alt>+<shift>+a"
    toggle_hide: str = "<alt>+<shift>+h"


class UiConfig(BaseModel):
    opacity: float = Field(0.92, ge=0.2, le=1.0)
    font_size: int = 15
    width: int = 420
    height: int = 260


class PathsConfig(BaseModel):
    model_config = ConfigDict(validate_default=True)

    context_dir: Path = Path("context")
    sessions_dir: Path = Path("sessions")
    downloads_dir: Path = Path("downloads")  # desktop installers offered on /download (public)

    @field_validator("context_dir", "sessions_dir", "downloads_dir", mode="after")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        return v if v.is_absolute() else (PROJECT_ROOT / v)


# --------------------------------------------------------------------------- #
# .env / environment
# --------------------------------------------------------------------------- #
class Secrets(BaseSettings):
    """Secrets come from the environment or a .env file next to pyproject.toml."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    deepgram_api_key: str = ""
    anthropic_api_key: str = ""
    app_token: str = ""  # shared secret between the local server and its clients (personal mode)

    def missing_for(self, *, stt_provider: str, brain_provider: str) -> list[str]:
        """Names of secrets required by the chosen providers that are still empty."""
        missing: list[str] = []
        if stt_provider == "deepgram" and not self.deepgram_api_key:
            missing.append("DEEPGRAM_API_KEY")
        if brain_provider == "anthropic" and not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        return missing


# --------------------------------------------------------------------------- #
# Top-level
# --------------------------------------------------------------------------- #
class Config(BaseModel):
    audio: AudioConfig = AudioConfig()
    stt: SttConfig = SttConfig()
    brain: BrainConfig = BrainConfig()
    trigger: TriggerConfig = TriggerConfig()
    hotkeys: HotkeysConfig = HotkeysConfig()
    ui: UiConfig = UiConfig()
    server: ServerConfig = ServerConfig()
    store: StoreConfig = StoreConfig()
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)  # built per Config: env overrides apply
    paths: PathsConfig = PathsConfig()
    secrets: Secrets = Field(default_factory=Secrets)

    def glossary_terms(self) -> list[str]:
        """Terms from context/glossary.md lines like `- TERM — explanation` (for STT keyterms)."""
        path = self.paths.context_dir / "glossary.md"
        if not path.exists():
            return []
        terms: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith(("-", "*")):
                continue
            body = line[1:].strip()
            for sep in (" — ", " - ", " – ", ":"):
                if sep in body:
                    body = body.split(sep, 1)[0]
                    break
            term = body.strip().strip("`*")
            if 1 <= len(term) <= 40 and not term.startswith("<!--"):
                terms.append(term)
        return terms[: self.stt.max_keyterms]

    @classmethod
    def from_yaml(cls, path: Path | None = None) -> "Config":
        path = path or (PROJECT_ROOT / "config.yaml")
        data: dict = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        data.pop("secrets", None)  # secrets never come from yaml
        return cls(**data)


def load_config(path: Path | None = None) -> Config:
    """Load config.yaml (or defaults when absent) and secrets from .env/env vars."""
    env_path = os.environ.get("COPILOT_CONFIG")
    if path is None and env_path:
        path = Path(env_path)
    cfg = Config.from_yaml(path)
    if os.environ.get("COPILOT_PUBLIC_URL"):
        cfg.server.public_url = os.environ["COPILOT_PUBLIC_URL"].rstrip("/")
    return cfg

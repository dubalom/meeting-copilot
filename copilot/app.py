"""Application assembly and lifecycle.

Step 1 wires config + audio sources and offers a readiness report.
Later steps plug in STT (step 2), Brain/Trigger (step 3), Overlay (step 4), Auto mode (step 5)
without changing the public entry points here.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from copilot.config import Config, load_config

log = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # PortAudio / websockets are chatty at DEBUG
    logging.getLogger("websockets").setLevel(logging.INFO)


class App:
    """Owns the long-lived components. Build order matches the data flow in CLAUDE.md §2."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sources = None  # dict[str, AudioSource]  (step 1)
        self.transcribers = None  # (step 2)
        self.transcript = None  # (step 2)
        self.brain = None  # (step 3)
        self.trigger = None  # (step 3)

    # ------------------------------------------------------------------ build
    def build_audio(self):
        from copilot.audio.capture import open_sources

        self.sources = open_sources(self.cfg)
        return self.sources

    # ------------------------------------------------------------------ modes
    def run_cli(self) -> int:
        """Terminal mode: live transcript (step 2) + hotkey answers (step 3)."""
        print("Режим --cli появится на Шаге 2 (живой транскрипт в терминале).")
        print("Сейчас доступна проверка аудио: python -m copilot.audio.devices")
        return 0

    def run_overlay(self) -> int:
        """Overlay window (step 4)."""
        print("Overlay появится на Шаге 4. Сейчас: python -m copilot --check")
        return 0

    # ----------------------------------------------------------------- checks
    def readiness_report(self) -> tuple[bool, str]:
        """Human-readable status of everything Den needs to have in place."""
        cfg = self.cfg
        lines: list[str] = []
        ok = True

        lines.append(f"Python {sys.version.split()[0]} · platform {sys.platform}")
        lines.append(f"config: stt={cfg.stt.provider}/{cfg.stt.model}/{cfg.stt.language}  "
                     f"brain={cfg.brain.provider}/{cfg.brain.answer_model}  filter={cfg.brain.filter_model}")

        missing = cfg.secrets.missing_for(stt_provider=cfg.stt.provider, brain_provider=cfg.brain.provider)
        if missing:
            ok = False
            lines.append(f"✗ secrets: нет {', '.join(missing)} — заполни .env (см. .env.example)")
        else:
            lines.append("✓ secrets: все ключи для выбранных провайдеров на месте")

        ctx = cfg.paths.context_dir
        md_files = sorted(ctx.glob("*.md")) if ctx.exists() else []
        if md_files:
            lines.append(f"✓ context/: {', '.join(p.name for p in md_files)}")
        else:
            lines.append(f"✗ context/: пусто ({ctx}) — скопируй инструкции «режима встречи» в context/*.md")

        try:
            from copilot.audio.capture import open_sources

            sources = open_sources(cfg)
            for role, src in sources.items():
                lines.append(f"✓ audio {role}: {src.describe()}")
                if src.spec.note:
                    lines.append(f"    {src.spec.note}")
        except Exception as exc:  # AudioDeviceError or PortAudio import error
            ok = False
            lines.append(f"✗ audio: {exc}")

        return ok, "\n".join(lines)


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def make_app(config_path: Path | None = None) -> App:
    return App(load_config(config_path))

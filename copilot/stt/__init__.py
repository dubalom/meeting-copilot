"""Streaming speech-to-text providers behind the ``Transcriber`` interface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from copilot.stt.base import EventCallback, Speaker, StatusCallback, Transcriber, TranscriptEvent

if TYPE_CHECKING:  # pragma: no cover
    from copilot.config import Config

__all__ = ["Transcriber", "TranscriptEvent", "Speaker", "make_transcriber_factory"]


# Measured on Deepgram Nova-3 (Oct 2026): in mixed-language mode more than ~10 key terms make it drop Russian
# speech («Скажите, а какой у нас» … and nothing more); a single-language mode is fine with dozens.
KEYTERMS_MULTI = 10
KEYTERMS_SINGLE = 50


def _budget(terms: list[str], language: str) -> list[str]:
    """De-duplicate (first wins: the user's own words come first) and cap by language mode."""
    out, seen = [], set()
    for t in terms:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[: KEYTERMS_MULTI if language == "multi" else KEYTERMS_SINGLE]


def make_transcriber_factory(cfg: "Config"):
    """Return ``factory(speaker, on_event, on_status) -> Transcriber`` for the configured provider."""
    keyterms: Iterable[str] = cfg.glossary_terms() if cfg.stt.keyterms_from_glossary else ()

    if cfg.stt.provider == "deepgram":
        from copilot.stt.deepgram_stt import DeepgramTranscriber

        def factory(speaker: Speaker, on_event: EventCallback, on_status: StatusCallback | None = None,
                    language: str | None = None, extra_keyterms: Iterable[str] = ()) -> Transcriber:
            stt = cfg.stt if not language or language == cfg.stt.language else cfg.stt.model_copy(update={"language": language})
            return DeepgramTranscriber(
                speaker,
                on_event,
                on_status,
                api_key=cfg.secrets.deepgram_api_key,
                cfg=stt,
                keyterms=_budget([*extra_keyterms, *keyterms], stt.language),
                sample_rate=cfg.audio.sample_rate,
            )

        return factory

    if cfg.stt.provider == "whisper":  # pragma: no cover - later step
        raise NotImplementedError("whisper provider arrives in a later step (see CLAUDE.md §5.3)")

    raise ValueError(f"unknown stt provider: {cfg.stt.provider}")

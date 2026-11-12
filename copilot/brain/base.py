"""Brain interface: stream an answer as text deltas, report usage at the end."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class Usage:
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    first_token_ms: int | None = None
    total_ms: int | None = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, {}, "")}


class Brain(ABC):
    """``answer`` yields text chunks; after exhaustion ``last_usage`` holds the usage."""

    last_usage: Usage

    @abstractmethod
    def answer(self, question: str | None, transcript: str, *, engine: str | None = None,
               knowledge: str | None = None, personal: str | None = None,
               answer_language: str | None = None) -> AsyncIterator[str]: ...

    async def summarize(self, transcript: str, personal: str | None = None) -> str:
        raise NotImplementedError

    async def should_answer(self, transcript: str) -> tuple[bool, str]:
        """Auto mode: does the last `them` line need an answer from the user? → (answer, question)."""
        raise NotImplementedError

    async def expand_query(self, question: str) -> str:
        """Search keywords for a question (RU+EN synonyms), used when plain search found nothing. '' = none."""
        return ""

    def reload_context(self) -> None:
        """Re-read context files (button «Reload context»)."""


class BrainUnavailable(RuntimeError):
    """Configuration problem (e.g. no API key) — shown to the user as a status message."""

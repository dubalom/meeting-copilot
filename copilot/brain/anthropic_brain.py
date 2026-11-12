"""Brain over the Anthropic Messages API: streaming + prompt caching of the context block."""

from __future__ import annotations

import logging
import time
from typing import AsyncIterator

from copilot.brain.base import Brain, BrainUnavailable, Usage
from copilot.brain.prompt import FILTER_RULES, SUMMARY_RULES, answer_user_message, load_context, system_blocks
from copilot.config import Config, EngineConfig

log = logging.getLogger(__name__)



EXPAND_RULES = (
    "Тебе дают вопрос со встречи. Перечисли через запятую 8–12 слов и коротких фраз, которыми то же самое могли "
    "написать в рабочей переписке или документах: синонимы, разговорные варианты, термины на русском и английском. "
    "Только список, без пояснений."
)

class AnthropicBrain(Brain):
    def __init__(self, cfg: Config, *, name: str = "Den", client=None):
        if client is None:
            if not cfg.secrets.anthropic_api_key:
                raise BrainUnavailable("ANTHROPIC_API_KEY не задан в .env")
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=cfg.secrets.anthropic_api_key, max_retries=2, timeout=30.0)
        self.client = client
        self.cfg = cfg
        self.name = name
        self.last_usage = Usage()
        self.reload_context()

    def reload_context(self) -> None:
        self.context = load_context(self.cfg.paths.context_dir)
        self.system = system_blocks(self.context, name=self.name)
        log.info("brain context loaded: %d chars", len(self.context))

    def engine(self, name: str | None) -> tuple[str, EngineConfig]:
        b = self.cfg.brain
        key = name if name in b.engines else b.default_engine
        if key in b.engines:
            return key, b.engines[key]
        return "legacy", EngineConfig(label="Claude", model=b.answer_model, max_tokens=b.max_tokens,
                                      thinking={"type": "disabled"})

    async def answer(self, question: str | None, transcript: str, *, engine: str | None = None,
                     knowledge: str | None = None, personal: str | None = None,
                     answer_language: str | None = None) -> AsyncIterator[str]:
        _, eng = self.engine(engine)
        usage = Usage(model=eng.model)
        self.last_usage = usage
        t0 = time.monotonic()
        kwargs: dict = {}
        if eng.thinking is not None:
            # Fast engines turn thinking off: for a live hint it only adds latency and can eat the
            # whole max_tokens budget with no text at all (observed with Haiku 5.5).
            kwargs["thinking"] = eng.thinking
        if eng.effort:
            kwargs["extra_body"] = {"output_config": {"effort": eng.effort}}
        content = answer_user_message(transcript, question, detailed=eng.detailed, knowledge=knowledge,
                                      answer_language=answer_language)
        async with self.client.messages.stream(
            model=eng.model,
            max_tokens=eng.max_tokens,
            system=system_blocks(self.context, name=self.name, personal=personal) if personal else self.system,
            messages=[{"role": "user", "content": content}],
            **kwargs,
        ) as stream:
            async for text in stream.text_stream:
                if usage.first_token_ms is None:
                    usage.first_token_ms = int((time.monotonic() - t0) * 1000)
                yield text
            final = await stream.get_final_message()
        if usage.first_token_ms is None:  # no text at all (e.g. stopped on max_tokens)
            yield f"_(пустой ответ: {final.stop_reason})_"
        _fill_usage(usage, final.usage)
        usage.total_ms = int((time.monotonic() - t0) * 1000)

    async def should_answer(self, transcript: str) -> tuple[bool, str]:
        msg = await self.client.messages.create(
            model=self.cfg.brain.filter_model,
            max_tokens=120,
            thinking={"type": "disabled"},
            system=FILTER_RULES.format(name=self.name),
            messages=[{"role": "user", "content": f"Транскрипт:\n{transcript}"}],
        )
        u = Usage(model=self.cfg.brain.filter_model)
        _fill_usage(u, msg.usage)
        self.last_filter_usage = u
        return parse_filter_reply("".join(getattr(c, "text", "") for c in msg.content))

    async def expand_query(self, question: str) -> str:
        msg = await self.client.messages.create(
            model=self.cfg.brain.filter_model,
            max_tokens=80,
            thinking={"type": "disabled"},
            system=EXPAND_RULES,
            messages=[{"role": "user", "content": question}],
        )
        return " ".join(getattr(c, "text", "") for c in msg.content).replace("\n", " ")[:400]

    async def summarize(self, transcript: str, personal: str | None = None) -> str:
        b = self.cfg.brain
        msg = await self.client.messages.create(
            model=b.summary_model,
            max_tokens=b.summary_max_tokens,  # thinking stays on here: quality over speed
            system=system_blocks(self.context, name=self.name, rules=SUMMARY_RULES, personal=personal),
            messages=[{"role": "user", "content": f"Транскрипт встречи:\n{transcript}"}],
        )
        u = Usage(model=b.summary_model)
        _fill_usage(u, msg.usage)
        self.last_usage = u
        return "".join(getattr(c, "text", "") for c in msg.content)


def parse_filter_reply(text: str) -> tuple[bool, str]:
    """Extract {"answer": bool, "question": str} from the model reply; anything unparseable = False."""
    import json
    import re

    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return False, ""
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return False, ""
    return data.get("answer") is True, str(data.get("question") or "").strip()


def _fill_usage(target: Usage, src) -> None:
    for f in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        setattr(target, f, int(getattr(src, f, 0) or 0))


def make_brain(cfg: Config) -> Brain:
    if cfg.brain.provider == "anthropic":
        return AnthropicBrain(cfg)
    raise BrainUnavailable(f"brain provider '{cfg.brain.provider}' ещё не реализован")

# Meeting Copilot — бриф проекта для Claude Code

> Этот файл — единственный источник правды по проекту. Прочитай его целиком перед первым действием.
> Пользователь — Den. Общаемся по-русски, код и комментарии в коде — на английском.

---

## 0. Что строим

Десктоп-приложение «копилот для рабочих встреч»:

1. Слушает **системный звук** компьютера (всё, что звучит в колонках/наушниках: Zoom, Google Meet, Teams, Telegram, Discord — любое приложение, без интеграций с ним) и **микрофон** Den'а отдельным каналом.
2. В реальном времени превращает речь в текст (стриминговый STT), помечая реплики `them` (собеседники) и `me` (Den).
3. По триггеру отправляет контекст разговора в Claude и **стримит ответ в небольшое окно поверх всех приложений** (overlay).
4. Два режима ответа:
   - **Hotkey** — Den нажимает горячую клавишу → копилот отвечает на последний вопрос/реплику собеседников.
   - **Auto** (кнопка-тумблер в окне) — копилот сам отвечает на каждую реплику собеседников, которая требует ответа от Den'а (вопрос, просьба, «а что думаешь ты»). Фильтр «надо ли отвечать» — дешёвой быстрой моделью.
5. Контекст Den'а (его роль, проект, глоссарий, типовые ответы) живёт в папке `context/*.md` и подмешивается в системный промпт с кэшированием.
6. Транскрипт встречи + ответы сохраняются в `sessions/*.md` как конспект. Сырой звук на диск **не** пишем.

**Не в скоупе (не делать):**
- бот, который «заходит» в звонок как участник;
- скрытие окна от демонстрации экрана и прочие трюки с захватом экрана (Den при шаринге просто показывает конкретное окно, а не весь экран);
- веб-версия / облачный бэкенд — всё локально, наружу уходят только запросы к STT и LLM API.

---

## 1. Что Den должен подготовить (спроси, чего не хватает, прежде чем кодить)

| Что | Зачем | Где взять |
|---|---|---|
| ОС: macOS или Windows | от этого зависит захват звука | — |
| Python 3.11+ и `uv` | среда | `brew install uv` / `pip install uv` |
| Ключ Deepgram | стриминговый STT (Nova-3) | console.deepgram.com — при регистрации есть стартовый кредит |
| Доступ к Claude: **вариант A** — API-ключ Anthropic; **вариант B** — установленный и залогиненный Claude Code (`claude`) | мозг копилота | A: console.anthropic.com. B: `npm i -g @anthropic-ai/claude-code && claude` (логин подпиской). Подробности в §5.5 |
| macOS: BlackHole 2ch + Multi-Output Device | на Mac нет системного loopback, нужен виртуальный аудиоустройство | `brew install blackhole-2ch`, затем Audio MIDI Setup → «+» → Create Multi-Output Device → отметить наушники/колонки **и** BlackHole 2ch → выбрать это устройство как системный вывод |
| macOS: разрешения | Microphone (терминалу/Python), Accessibility (для глобальных хоткеев — pynput) | System Settings → Privacy & Security |
| Windows | ничего дополнительного — WASAPI loopback берёт звук с устройства вывода напрямую | — |
| Наушники | иначе микрофон ловит собеседников из колонок и канал `me` замусоривается | — |
| Контекст | папка `context/` | Den копирует инструкции и ключевые файлы своего проекта «режим встречи» (Softconstruct) из claude.ai в `context/*.md`. Напрямую к проекту claude.ai приложение подключиться не может — только через эти файлы |

---

## 2. Архитектура (один процесс, Python)

```
 ┌────────────────────┐    ┌────────────────────┐
 │ System audio       │    │ Microphone         │
 │ (loopback /        │    │                    │
 │  BlackHole)        │    │                    │
 └─────────┬──────────┘    └─────────┬──────────┘
           │ PCM 16 kHz mono int16, фреймы 20–50 мс
           ▼                         ▼
 ┌─────────────────────────────────────────────┐
 │ STT (streaming)  — Deepgram Nova-3          │
 │ 2 соединения: channel "them" и "me"         │
 │ interim + final, endpointing                │
 │ (fallback: faster-whisper локально)         │
 └─────────┬───────────────────────────────────┘
           ▼
 ┌─────────────────────────────────────────────┐
 │ TranscriptBuffer                            │
 │ [{ts, speaker, text, final}] кольцо ~15 мин │
 └─────────┬───────────────────────────────────┘
           ▼
 ┌─────────────────────────────────────────────┐
 │ Trigger                                     │
 │  • hotkey  → last_question(them)            │
 │  • auto    → каждая final-реплика them →    │
 │              фильтр (Haiku) «нужен ответ?»  │
 └─────────┬───────────────────────────────────┘
           ▼
 ┌─────────────────────────────────────────────┐
 │ Answerer                                    │
 │ system = правила + context/*.md (cached)    │
 │ user   = окно транскрипта + вопрос          │
 │ stream → UI; новый вопрос отменяет старый   │
 └─────────┬───────────────────────────────────┘
           ▼
 ┌─────────────────────────────────────────────┐
 │ Overlay (PyQt6)  always-on-top, frameless   │
 │ статус · транскрипт · ответ · [Auto] [Ответ]│
 └─────────────────────────────────────────────┘
```

Потоки: аудио и STT — в фоновых потоках/asyncio-цикле; в Qt всё прилетает через сигналы (или `qasync`). UI никогда не блокируется.

---

## 3. Стек

- **Python 3.11+**, менеджер `uv`, конфиг через `pydantic-settings` + `config.yaml`, секреты в `.env`.
- **Аудио:** `sounddevice` (кроссплатформенно, выбор устройства по имени); на Windows loopback через `pyaudiowpatch` (WASAPI); на macOS читаем устройство `BlackHole 2ch`.
- **STT:** `deepgram-sdk` (live/streaming websocket). Опционально `faster-whisper` как локальный провайдер.
- **LLM:** `anthropic` (вариант A, прямой API со стримингом и prompt caching) **или** `claude-agent-sdk` (вариант B, через подписку). Один интерфейс `Brain`, провайдер выбирается в конфиге.
- **UI:** `PyQt6` — frameless, translucent, always-on-top окно. Глобальные хоткеи — `pynput`.
- **Прочее:** `structlog`/`logging`, `pytest` для буфера/фильтра/парсинга.

Если позже захочется UI покрасивее — ядро остаётся на Python, UI можно вынести в Tauri/Electron через локальный websocket. Сейчас не делаем.

---

## 4. Структура проекта

```
meeting-copilot/
├── CLAUDE.md                 # этот бриф
├── README.md                 # установка и запуск (коротко, для Den'а)
├── pyproject.toml
├── .env.example              # ANTHROPIC_API_KEY=, DEEPGRAM_API_KEY=
├── config.yaml               # модели, хоткеи, язык, устройства, UI
├── context/                  # контекст Den'а → в системный промпт
│   ├── role.md               # кто он, чем занимается, как отвечает
│   ├── project.md            # текущий проект/команда/термины (из «режима встречи»)
│   ├── glossary.md           # аббревиатуры, названия таблиц, продуктов
│   └── snippets.md           # заготовки: SQL, формулы, типовые ответы
├── sessions/                 # авто-конспекты встреч (markdown), в .gitignore
├── copilot/
│   ├── __main__.py           # python -m copilot
│   ├── app.py                # сборка компонентов, жизненный цикл
│   ├── config.py
│   ├── audio/
│   │   ├── devices.py        # список устройств, поиск loopback/BlackHole, проверка
│   │   └── capture.py        # два источника → очереди PCM-фреймов
│   ├── stt/
│   │   ├── base.py           # class Transcriber: start/stop/on_transcript(cb)
│   │   ├── deepgram_stt.py
│   │   └── whisper_stt.py    # опционально
│   ├── transcript.py         # TranscriptBuffer + окна выборки
│   ├── brain/
│   │   ├── base.py           # class Brain: answer(question, window) -> async text stream
│   │   ├── prompt.py         # сборка system prompt из context/, cache_control
│   │   ├── anthropic_brain.py# вариант A
│   │   ├── agent_sdk_brain.py# вариант B
│   │   └── filter.py         # «нужен ли ответ?» → {"answer": bool, "question": str}
│   ├── trigger.py            # hotkey / auto, debounce, отмена in-flight
│   ├── session_log.py        # запись sessions/*.md
│   └── ui/
│       ├── overlay.py        # окно
│       ├── hotkeys.py        # pynput → Qt-сигналы
│       └── styles.py
└── tests/
```

---

## 5. Ключевые решения и детали

### 5.1 Аудио
- Формат везде: 16 kHz, mono, int16 PCM. Ресемплинг на входе (`sounddevice` отдаст нужную частоту, если устройство позволяет; иначе `scipy.signal.resample_poly` или `samplerate`).
- Два независимых источника → две очереди (`them`, `me`). Источник `me` можно выключить в конфиге (экономия STT) — тогда отвечаем только на `them`.
- Windows: `pyaudiowpatch` → `p.get_default_wasapi_loopback()`; это и есть `them`.
- macOS: ищем устройство по имени `BlackHole 2ch`; если не найдено — печатаем понятную инструкцию из §1 и выходим.
- `devices.py` имеет CLI: `python -m copilot.audio.devices` — список устройств и живые уровни RMS по обоим каналам (проверка, что звук реально идёт).

### 5.2 STT (Deepgram)
- Модель `nova-3`, `language=multi` (Den говорит на русском и английском вперемешку; если `multi` не поддерживает нужный язык — `language=ru`, проверить по докам).
- Параметры: `encoding=linear16`, `sample_rate=16000`, `channels=1`, `interim_results=true`, `smart_format=true`, `punctuate=true`, `endpointing=300`, `utterance_end_ms=1000`, `vad_events=true`.
- Keyterm prompting: отдаём термины из `context/glossary.md` (если поддерживается для выбранного языка).
- Два websocket-соединения (`them`, `me`), keepalive каждые ~8 с тишины, реконнект с экспоненциальной задержкой, не теряя буфер.
- Interim-результаты только для отображения «серым», в буфер пишем final.
- Абстракция `Transcriber` такая, чтобы `whisper_stt.py` вставал без изменений остального кода (локальный `faster-whisper`, модель `large-v3-turbo`, окно 3–5 с с VAD `silero`; медленнее, но бесплатно и умеет больше языков).

### 5.3 TranscriptBuffer
- Элемент: `Utterance(ts: float, speaker: Literal["them","me"], text: str, final: bool)`.
- Кольцо на ~15 минут. Методы: `window(seconds)`, `last_them_utterances(n)`, `last_question()` (последняя final-реплика `them` длиной ≥ 3 слов; если последние 2–3 реплики `them` идут подряд без реплик `me` — склеить их в один вопрос).
- Форматирование окна для промпта: `[12:03:41] them: ...` / `[12:03:49] me: ...`.

### 5.4 Trigger
- **Hotkey:** берём `last_question()` + окно последних 90 с → `Brain.answer()`.
- **Auto:** на каждую final-реплику `them` (≥ 4 слов) → `filter.py` (быстрая модель, JSON-ответ `{"answer": true/false, "question": "<переформулированный вопрос>"}`) → если `true` → `Brain.answer()`. Debounce 1.5–2 с (несколько коротких final подряд — одна проверка). Если фильтр не уверен — не отвечаем (ложные срабатывания раздражают сильнее пропусков).
- Новый запрос отменяет предыдущий стрим (asyncio task cancel), в UI старый ответ уходит в «историю», новый стримится сверху.
- Ручная кнопка «Ответить» в окне = тот же hotkey-путь.

### 5.5 Brain (две реализации, выбор в `config.yaml: brain.provider`)

**Общее:**
- System prompt = статичные правила (ниже) + содержимое `context/*.md` (в порядке: role, project, glossary, snippets). Контекст перечитывается при старте и по кнопке «Reload context».
- User message = окно транскрипта (90 с) + `Вопрос собеседника: «...»`.
- `max_tokens` ≈ 350, низкая температура. Стримим текст в UI по мере генерации.
- Модели по умолчанию (API-строки): ответы — `claude-sonnet-5-5`; фильтр — `claude-haiku-5-5`. Если хочется максимально быстро — ответы тоже `claude-haiku-5-5`. Всё переключается в конфиге.

**Вариант A — `anthropic` SDK (быстрее, рекомендуется по умолчанию):**
- `client.messages.stream(...)`, system как список блоков, на блок с контекстом — `cache_control: {"type": "ephemeral"}` (prompt caching: контекст большой и не меняется → почти не платим за него повторно и получаем первые токены быстрее).
- Оплата по токенам с API-ключа. У Den'а на Max/Team-плане в подписку входят месячные API-кредиты — ими можно покрыть это (см. ссылку в §9).

**Вариант B — `claude-agent-sdk` (через подписку Claude, без отдельного API-ключа):**
- Требует установленный `claude` CLI и логин подпиской. Agent SDK и `claude -p` работают в рамках лимитов подписки (см. §9).
- Держим один долгоживущий `ClaudeSDKClient` (streaming input mode), чтобы не спавнить процесс на каждый вопрос; `allowed_tools=[]`, `max_turns=1`, `system_prompt` тот же, стримим `text` дельты в UI.
- Минус: латентность выше, чем у прямого API. Поэтому это опция, а не дефолт.

**Системный промпт (статичная часть, черновик — доработай по ходу):**
```
Ты — real-time копилот Den'а на рабочей встрече. Den видит твой ответ на экране и говорит своими словами.
Ниже — транскрипт последних минут (them = собеседники, me = Den) и вопрос, на который надо помочь ответить.

Правила:
1. Отвечай на языке, на котором задан вопрос (русский/английский).
2. Сверхкоротко: сначала суть одной фразой, затем максимум 2–3 пункта. Никаких вступлений и повторов вопроса.
3. Если вопрос про опыт/мнение/работу Den'а — отвечай от первого лица, опираясь на контекст ниже. Не выдумывай факты, которых нет в контексте: лучше дай каркас ответа и пометь [уточнить].
4. Если просят код/SQL/формулу — сначала сам код, потом одна строка пояснения.
5. Если вопрос неясен — дай самую вероятную трактовку и одну фразу-уточнение, которую Den может произнести.
6. Если в транскрипте нет вопроса к Den'у — ответь ровно «—».

=== КОНТЕКСТ DEN'А ===
{context_files}
```

**Промпт фильтра (`filter.py`):**
```
Ты решаешь, нужно ли Den'у сейчас отвечать. Дан транскрипт последних реплик (them = собеседники, me = Den).
Ответь строго JSON: {"answer": true|false, "question": "кратко, что спросили у Den'а"}.
answer=true только если последняя реплика them — вопрос, просьба или явное приглашение высказаться, адресованное Den'у или всем. Иначе false.
```

### 5.6 Overlay (PyQt6)
- Флаги: `FramelessWindowHint | WindowStaysOnTopHint | Tool`; `WA_TranslucentBackground`; тёмный полупрозрачный фон, скруглённые углы; перетаскивание за любое место, ресайз за угол; положение/размер/прозрачность/размер шрифта запоминаются в `~/.meeting-copilot/ui.json`.
- Размер по умолчанию ~420×260, правый нижний угол.
- Содержимое сверху вниз:
  1. строка статуса: ● Listening / ◐ Thinking / ✓ Answer · индикатор уровня звука `them`/`me` · [Auto ○/●] [Ответить] [Скрыть] [⚙]
  2. транскрипт: последние 3–5 реплик мелким серым (`them` светлее, `me` темнее, interim курсивом)
  3. ответ: крупно, стримится; поддержать `**bold**`, списки, `code`
  4. история ответов за сессию — свёрнутый список
- Горячие клавиши по умолчанию (меняются в конфиге): macOS — `⌥+Space` ответить, `⌥+Shift+A` тумблер Auto, `⌥+Shift+H` скрыть/показать; Windows — `Ctrl+Space`, `Ctrl+Shift+A`, `Ctrl+Shift+H`.
- macOS: чтобы окно было видно поверх **полноэкранного** Zoom/Meet на другом Space, выставить `NSWindow.collectionBehavior = canJoinAllSpaces | fullScreenAuxiliary` через `pyobjc` (шаг полировки).
- Окно не должно перехватывать фокус у приложения встречи при появлении ответа (`WA_ShowWithoutActivating`).

### 5.7 Сессии
- При старте создаётся `sessions/YYYY-MM-DD_HH-MM.md`; пишем транскрипт (final) и ответы с таймстемпами. В конце — кнопка/хоткей «Итоги»: Claude делает краткое саммари встречи и список action items → в тот же файл.

### 5.8 config.yaml (пример)
```yaml
audio:
  them_device: "auto"          # auto | имя устройства
  me_device: "auto"
  transcribe_me: true
stt:
  provider: deepgram            # deepgram | whisper
  language: multi               # multi | ru | en
  keyterms_from_glossary: true
brain:
  provider: anthropic           # anthropic | agent_sdk
  answer_model: claude-sonnet-5-5
  filter_model: claude-haiku-5-5
  max_tokens: 350
  window_seconds: 90
trigger:
  auto_default: false
  debounce_seconds: 1.5
hotkeys:
  answer: "<alt>+<space>"
  toggle_auto: "<alt>+<shift>+a"
  toggle_hide: "<alt>+<shift>+h"
ui:
  opacity: 0.92
  font_size: 15
```

---

## 6. План сборки — по шагам, каждый шаг проверяется руками

После каждого шага: показать Den'у команду запуска и что он должен увидеть. Не переходить дальше, пока шаг не подтверждён.

1. **Скелет + аудио.** `uv init`, зависимости, `config.py`, `audio/devices.py` + `audio/capture.py`. Проверка: `python -m copilot.audio.devices` показывает устройства и живые уровни на двух каналах, пока играет YouTube и Den говорит в микрофон.
2. **Живой транскрипт в терминале.** `stt/deepgram_stt.py`, `transcript.py`. Проверка: `python -m copilot --cli` печатает `them:`/`me:` строки с задержкой < 1 с на русско-английской речи.
3. **Ответ по хоткею в терминале.** `brain/*`, `trigger.py`, `context/` с заготовками. Проверка: в режиме `--cli` нажатие хоткея печатает стрим ответа на последний вопрос; видно, что prompt caching срабатывает (cache_read_input_tokens > 0 в usage).
4. **Overlay.** `ui/*`, подключение к тем же компонентам. Проверка: окно поверх Zoom/Meet, транскрипт идёт, хоткей → ответ в окне, окно двигается, прячется.
5. **Auto-режим.** `filter.py`, тумблер, debounce, отмена in-flight. Проверка: на вопрос в чужой речи ответ появляется сам за ≤ 2 с после конца фразы; на болтовню — не появляется.
6. **Полировка.** Реконнекты STT, `sessions/*.md` + саммари, настройки в окне (прозрачность, шрифт, устройства, модели), `pyinstaller`-сборка в один исполняемый файл, автозапуск опционально.

---

## 7. Критерии готовности MVP (шаги 1–5)

- От конца вопроса собеседника до первых слов ответа на экране: ≤ 2 с (Deepgram) / ≤ 4 с (whisper).
- Работает с любым приложением, звук которого идёт в системный вывод; проверено минимум на Zoom, Google Meet в браузере и Telegram-звонке.
- Русская и английская речь (в том числе вперемешку) распознаются корректно.
- Разрыв сети/ошибка API не роняют приложение: статус в окне, автоматический реконнект.
- UI не блокируется никогда.

---

## 8. Ориентир по стоимости

- Deepgram streaming: порядка $0.3–0.55 за час на один канал (два канала — вдвое; канал `me` можно выключить).
- Claude по API с кэшированием контекста: обычно $0.2–1 за час встречи в зависимости от числа ответов.
- Итого ≈ $1–2 за часовую встречу по API, либо лимиты подписки через Agent SDK (вариант B).

---

## 9. Документация, которую стоит открыть перед соответствующим шагом

- Deepgram live streaming: https://developers.deepgram.com/docs — раздел Live Audio / Streaming, Python SDK.
- Anthropic API: https://docs.claude.com/en/api/overview — Messages, streaming; prompt caching: https://docs.claude.com/en/docs/build-with-claude/prompt-caching
- Claude Agent SDK: https://docs.claude.com/en/api/agent-sdk/overview (Python SDK, streaming input mode, `ClaudeSDKClient`).
- Agent SDK и подписка Claude: https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
- Месячные API-кредиты на Max/Team: https://support.claude.com/en/articles/17154008
- BlackHole (macOS loopback): https://github.com/ExistentialAudio/BlackHole
- PyAudioWPatch (Windows WASAPI loopback): https://github.com/s0d3s/PyAudioWPatch
- faster-whisper: https://github.com/SYSTRAN/faster-whisper

---

## 10. Первое сообщение Den'а для старта (скопировать в чат Claude Code)

```
Прочитай CLAUDE.md целиком. Определи мою ОС, проверь Python/uv, наличие ключей в .env
и аудиоустройств (на Mac — BlackHole). Спроси только то, чего реально не хватает.
Затем создай структуру проекта из §4 и сделай Шаг 1 из §6. После шага покажи,
как проверить руками, и жди моего подтверждения перед Шагом 2.
```

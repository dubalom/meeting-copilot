# Meeting Copilot — бриф проекта (v2: ядро + веб)

> Единственный источник правды по проекту. Прочитай целиком перед первым действием.
> Пользователь — Den. Общаемся по-русски, код, комментарии и имена в коде — на английском.
> v1 (чисто десктопный вариант) лежит в `docs/brief-v1-desktop.md` — для истории; этот файл его заменяет.

---

## 0. Что строим и для кого

Копилот для рабочих встреч: слушает встречу, транскрибирует в реальном времени и показывает ответы Claude
на вопросы, адресованные Den'у, в плавающем окне поверх приложения встречи. После встречи — конспект,
action items и возможность «спросить у записи».

**Порядок приоритетов:**
1. **Сначала — рабочий инструмент для самого Den'а** (его встречи, его контекст, его ключи). Это основной критерий каждого шага.
2. **Потом — продукт по подписке** для других людей. Поэтому с первого дня: всё привязано к `user_id`, ключи провайдеров
   только на сервере, расход STT-минут и LLM-токенов считается per session, никаких «один пользователь = константа» в коде.
   Регистрация, тарифы, оплата — отдельная фаза, но архитектура не должна требовать переписывания под них.

**Форма продукта:** веб-приложение (сайт) + серверное ядро. Ориентир по UX — LockedIn AI (веб-режим: «Start Recording» →
выбор вкладки/экрана со звуком → ответы в отдельном окне; контекст из загруженных документов; ручной/авто режим).
Их десктоп-фишки (стелс, автозапуск на уровне ОС) **не копируем**.

**Честные ограничения браузера (не обещать Den'у обратного):**
- Звук при шаринге экрана отдают только Chromium-браузеры (Chrome, Edge, Brave, Arc, Opera). В Safari/Firefox — только микрофон.
- macOS: Chrome отдаёт звук только **вкладки** (Meet/Zoom-web/Teams-web). Для десктопных Zoom/Teams на Mac звук берётся через
  виртуальное устройство BlackHole, выбранное в браузере как «микрофон». Windows: «весь экран + системный звук» работает.
- Сайт не может сам понять, что началась встреча, и не может включить захват без клика пользователя. Автодетект вкладки
  встречи и запуск в один клик — задача **расширения Chrome** (фаза 3). Полный автозапуск без кликов — только десктоп (фаза 4, если понадобится).
- Окно поверх всех приложений из браузера — **Document Picture-in-Picture** (Chrome 116+). Это и есть наш overlay.

**Не в скоупе:** бот, который заходит в звонок участником; скрытие окна от демонстрации экрана и прочий «стелс»;
чтение чата самой встречи (ломко, только через расширение — отложено до явного запроса).

---

## 1. Что Den должен подготовить

| Что | Зачем | Где взять |
|---|---|---|
| Python 3.12 + `uv` | ядро/сервер | уже стоит (`.python-version` = 3.12, `uv sync`) |
| Node 20+ | веб-клиент (Vite) | `brew install node` (уже есть) |
| Chrome/Edge | клиент | — |
| Ключ Deepgram | стриминговый STT (Nova-3) | console.deepgram.com → `.env: DEEPGRAM_API_KEY` |
| Ключ Anthropic | ответы и фильтр | console.anthropic.com → `.env: ANTHROPIC_API_KEY` (вариант через Agent SDK/подписку — только для личного режима, см. §5.5) |
| macOS: BlackHole 2ch + Multi-Output Device | звук десктопных Zoom/Teams на Mac | `brew install --cask blackhole-2ch` (нужен пароль), затем Audio MIDI Setup → Multi-Output (динамики/наушники + BlackHole) → Use for Sound Output |
| Разрешения | микрофон для Chrome (и для Terminal — для CLI-режима) | System Settings → Privacy & Security → Microphone |
| Наушники | иначе микрофон ловит собеседников и канал `me` замусоривается | — |
| Контекст | `context/*.md` (личный режим) / загрузка файлов в UI (продукт) | инструкции и файлы проекта «режим встречи» из claude.ai → `context/` |

Спрашивай только то, чего реально не хватает для текущего шага.

---

## 2. Архитектура

```
 Браузер (Chromium)                                  Сервер (Python, FastAPI)
 ┌───────────────────────────────┐                   ┌──────────────────────────────────────────┐
 │ Источники звука               │                   │ /ws/session/{id}                          │
 │  • getDisplayMedia(tab/screen)│  binary PCM 16k   │  AudioRouter: them / me → Transcriber×2   │
 │    → them                     │ ───────────────▶  │  (Deepgram Nova-3 streaming, keepalive,   │
 │  • getUserMedia(mic | BlackHole)                  │   reconnect)                              │
 │    → me                       │                   │        ▼                                  │
 │ AudioWorklet @16 kHz → int16  │  JSON events      │  TranscriptBuffer (кольцо ~15 мин)        │
 │                               │ ◀───────────────  │        ▼                                  │
 │ UI (React)                    │  transcript,      │  Trigger: manual (кнопка/хоткей) / auto   │
 │  • транскрипт them/me         │  answer deltas,   │   (debounce → Filter(Haiku) → answer?)    │
 │  • [Ответить] [Auto] [Итоги]  │  status, usage    │        ▼                                  │
 │  • Document PiP overlay       │                   │  Brain: system = правила + контекст (cache)│
 │  • MediaRecorder → запись     │  REST             │   user = окно транскрипта + вопрос        │
 │  • после встречи: конспект,   │ ───────────────▶  │   stream → клиент; новый вопрос отменяет  │
 │    «спроси у записи»          │                   │        ▼                                  │
 └───────────────────────────────┘                   │  Store: users, sessions, utterances,      │
                                                     │   answers, usage, recordings (SQLite→PG)  │
 CLI-клиент (терминал, для разработки):              │  Summary: конспект + action items         │
  capture.py (BlackHole/mic/WAV-файл) → тот же /ws   └──────────────────────────────────────────┘
```

**Принципы:**
- Ядро **ничего не знает об источнике звука** — ему приходят PCM-фреймы 16 kHz mono int16 с меткой канала. Источников три,
  и все ходят в один и тот же WebSocket: браузер (основной), локальный `capture.py` (десктопный режим / разработка),
  WAV-файл (тесты и воспроизводимые прогоны без живой встречи).
- Ключи Deepgram/Anthropic живут только на сервере. Браузер не получает ни одного секрета.
- Один процесс сервера, asyncio. Долгие операции (STT-сокеты, стрим ответа) — задачи asyncio, отменяемые.
- Вся запись в БД идёт через `user_id` и `session_id`. В личном режиме пользователь один (`owner`), но код этого не предполагает.
- UI никогда не блокируется; потеря сети/ошибка API — статус в UI и автоматический реконнект, не падение.

---

## 3. Стек

**Сервер (`copilot/`)** — Python 3.12, `uv`, FastAPI + uvicorn (WebSocket + REST), `pydantic-settings` + `config.yaml` + `.env`,
`deepgram-sdk` (live), `anthropic` (streaming + prompt caching), SQLModel/SQLAlchemy поверх SQLite (личный режим) → Postgres
(продукт; та же схема), `structlog`, `pytest` + `pytest-asyncio`. Ресемплинг/нарезка — свои (`audio/dsp.py`, уже есть).

**Клиент (`web/`)** — Vite + React + TypeScript, Tailwind. Захват: `getDisplayMedia({video:true, audio:true})` для `them`,
`getUserMedia` для `me` (с выбором устройства, в т.ч. BlackHole). `AudioContext({sampleRate: 16000})` + `AudioWorklet`
→ int16-фреймы по 40 мс → WebSocket (binary). **Обязательно** `echoCancellation/noiseSuppression/autoGainControl = false`
для loopback-источников. Overlay — `documentPictureInPicture.requestWindow()`. Запись — `MediaRecorder` (webm/opus) → upload.
Горячие клавиши внутри вкладки/PiP; глобальные — только через расширение (фаза 3).

**Расширение Chrome (фаза 3, `extension/`)** — MV3: детект вкладок meet.google.com / zoom.us / teams.microsoft.com,
автооткрытие панели копилота, `tabCapture` без диалога шаринга, глобальный хоткей.

**Десктоп (фаза 4, только при необходимости)** — Tauri с тем же `web/`; нативный захват звука (macOS 13+: ScreenCaptureKit, без BlackHole).
PyQt-оверлей из v1 **не делаем**.

---

## 4. Структура репозитория

```
meeting-copilot/
├── CLAUDE.md                  # этот бриф
├── README.md                  # установка и запуск (коротко, для Den'а)
├── docs/brief-v1-desktop.md   # прежняя версия брифа (архив)
├── pyproject.toml  .python-version  .env.example  config.yaml
├── context/                   # контекст Den'а (личный режим) → system prompt
│   ├── role.md  project.md  glossary.md  snippets.md
├── data/                      # SQLite, записи, конспекты (в .gitignore)
├── copilot/                   # ядро + сервер (Python)
│   ├── __main__.py            # python -m copilot {serve|cli|check|replay}
│   ├── app.py                 # сборка компонентов
│   ├── config.py              # ✓ есть
│   ├── audio/                 # ✓ есть: devices.py, capture.py, dsp.py  (+ wav_source.py для replay)
│   ├── stt/
│   │   ├── base.py            # Transcriber: start/stop/send(pcm)/on_result(cb); события interim/final/utterance_end
│   │   ├── deepgram_stt.py
│   │   └── whisper_stt.py     # опционально, локальный fallback
│   ├── transcript.py          # TranscriptBuffer + выборка окон + last_question()
│   ├── brain/
│   │   ├── base.py            # Brain.answer(question, window, context) -> async stream
│   │   ├── prompt.py          # system prompt + cache_control
│   │   ├── anthropic_brain.py # основной провайдер
│   │   ├── agent_sdk_brain.py # личный режим через подписку (опция)
│   │   ├── filter.py          # «нужен ли ответ?» → {"answer": bool, "question": str}
│   │   └── summary.py         # конспект + action items + Q&A по транскрипту
│   ├── trigger.py             # manual / auto, debounce, отмена in-flight
│   ├── session.py             # MeetingSession: связывает router→stt→buffer→trigger→brain для одного подключения
│   ├── store/                 # models.py (User, Session, Utterance, Answer, Usage, Recording), repo.py
│   ├── server/
│   │   ├── api.py             # FastAPI app: REST (sessions, context, recordings, summary) + auth middleware
│   │   ├── ws.py              # /ws/session/{id}: протокол §5.6
│   │   └── auth.py            # личный режим: один токен; продукт: pluggable (magic link / Clerk)
│   └── cli/
│       └── terminal.py        # python -m copilot cli: локальный захват → ws → транскрипт и ответы в терминале
├── web/                       # Vite + React + TS
│   ├── src/audio/             # capture.ts, worklet.ts (PCM 16k int16), devices.ts
│   ├── src/ws/                # client.ts (протокол §5.6, реконнект)
│   ├── src/ui/                # Session.tsx, Transcript.tsx, Answer.tsx, PipOverlay.tsx, Controls.tsx, Summary.tsx
│   └── src/pages/             # Home (новая сессия), Session, History
├── extension/                 # фаза 3
└── tests/
```

---

## 5. Ключевые решения и детали

### 5.1 Аудио на клиенте (браузер)
- `them`: `getDisplayMedia({ video: true, audio: { echoCancellation:false, noiseSuppression:false, autoGainControl:false } })`.
  Видео-трек нужен только для диалога; выключаем его сразу (`track.stop()`), если не ведём запись экрана. Если пользователь
  не поставил галочку «share audio» — трека нет: показать подсказку и предложить повторить.
- `me`: `getUserMedia({ audio: { deviceId, echoCancellation:true, ... } })`. В настройках — список устройств через
  `enumerateDevices()`; режим «системный звук через BlackHole» = выбрать BlackHole 2ch как источник `them` **через getUserMedia**
  с выключенной обработкой. Это путь для десктопных Zoom/Teams на Mac.
- Один `AudioContext({sampleRate:16000})`; каждый источник → `MediaStreamAudioSourceNode` → `AudioWorkletNode` (копит 640 сэмплов
  = 40 мс, конвертирует в int16, `postMessage` → WS). Уровень RMS считается там же → индикаторы в UI.
- Если `them` выбран как вкладка — Chrome по умолчанию **глушит** звук вкладки у пользователя; передавать `systemAudio:'include'`,
  `suppressLocalAudioPlayback:false`, чтобы Den продолжал слышать встречу.
- Транскрипцию `me` можно выключить (экономия STT).

### 5.2 Аудио на сервере / локальные источники
- `copilot/audio/*` остаются для CLI-режима и разработки: BlackHole/mic → тот же протокол §5.6 по WebSocket к локальному серверу.
- `wav_source.py`: проигрывает WAV (16 kHz mono) в реальном времени или быстрее — для тестов ядра без живой встречи
  (`python -m copilot replay tests/fixtures/meeting_ru_en.wav`). Фикстуры записываем сами (Den начитывает 1–2 минуты диалога).

### 5.3 STT (Deepgram)
- Модель `nova-3`; язык из конфига: `multi` (код-свитчинг ru/en) — **перед Шагом 2 проверить по докам**, входит ли русский в
  multi-режим Nova-3; если нет — `ru` по умолчанию и `en` как опция на сессию.
- Параметры: `encoding=linear16`, `sample_rate=16000`, `channels=1`, `interim_results=true`, `smart_format=true`, `punctuate=true`,
  `endpointing=300`, `utterance_end_ms=1000`, `vad_events=true`. Keyterms из глоссария — если поддерживается для языка.
- Два соединения на сессию (`them`, `me`), keepalive при тишине (~8 с), реконнект с экспоненциальной задержкой, буфер не теряем.
- Interim — только для UI (серым), в буфер и в БД пишем final.
- Абстракция `Transcriber` такая, чтобы `whisper_stt.py` (faster-whisper, `large-v3-turbo`, VAD silero) вставал без изменений остального.

### 5.4 TranscriptBuffer и Trigger
- `Utterance(ts, speaker: "them"|"me", text, final)`, кольцо ~15 мин; `window(seconds)`, `last_them_utterances(n)`,
  `last_question()` — последняя final-реплика `them` ≥ 3 слов; подряд идущие реплики `them` без `me` между ними склеиваются.
- Формат окна для промпта: `[12:03:41] them: ...` / `[12:03:49] me: ...`.
- **Manual:** кнопка/хоткей → `last_question()` + окно 90 с → `Brain.answer()`.
- **Auto:** каждая final-реплика `them` ≥ 4 слов → debounce 1.5–2 с → `filter.py` (Haiku, строгий JSON) → `true` → `Brain.answer()`.
  Фильтр не уверен — не отвечаем (ложные срабатывания раздражают сильнее пропусков).
- Новый запрос отменяет предыдущий стрим; старый ответ уходит в историю.

### 5.5 Brain
- System prompt = статичные правила + контекст пользователя (личный режим: `context/*.md` в порядке role, project, glossary, snippets;
  продукт: файлы, загруженные пользователем, md/txt/pdf/docx → текст). Контекст — отдельный system-блок с `cache_control: ephemeral`.
- User message = окно транскрипта (90 с) + `Вопрос собеседника: «...»`. `max_tokens` ≈ 350, temperature 0.2, стрим в UI.
- Модели (API-строки): ответы `claude-sonnet-5-5`, фильтр `claude-haiku-5-5`; для максимальной скорости ответы тоже Haiku. Всё в конфиге.
- Провайдер по умолчанию — `anthropic` (прямой API, кэш, быстрее). `agent_sdk` (через подписку Claude Code) — **только для личного
  режима на машине Den'а**; для продукта с чужими пользователями не годится (условия подписки, латентность). Перед включением
  сверить актуальную статью поддержки (ссылка в §9).
- `summary.py`: по кнопке «Итоги» или по завершении сессии — конспект (тема, решения, action items с владельцами, открытые вопросы)
  и режим Q&A по транскрипту («что обещали по срокам?»). Пишется в БД и в `data/sessions/*.md` (личный режим).

**Системный промпт (статичная часть, черновик — дорабатывать по ходу):**
```
Ты — real-time копилот Den'а на рабочей встрече. Den видит твой ответ на экране и говорит своими словами.
Ниже — транскрипт последних минут (them = собеседники, me = Den) и вопрос, на который надо помочь ответить.

Правила:
1. Отвечай на языке, на котором задан вопрос (русский/английский).
2. Сверхкоротко: сначала суть одной фразой, затем максимум 2–3 пункта. Никаких вступлений и повторов вопроса.
3. Если вопрос про опыт/мнение/работу Den'а — отвечай от первого лица, опираясь на контекст ниже. Не выдумывай факты,
   которых нет в контексте: лучше дай каркас ответа и пометь [уточнить].
4. Если просят код/SQL/формулу — сначала сам код, потом одна строка пояснения.
5. Если вопрос неясен — дай самую вероятную трактовку и одну фразу-уточнение, которую Den может произнести.
6. Если в транскрипте нет вопроса к Den'у — ответь ровно «—».

=== КОНТЕКСТ ===
{context}
```
(В продукте «Den» заменяется на имя пользователя из профиля.)

**Промпт фильтра:**
```
Ты решаешь, нужно ли пользователю сейчас отвечать. Дан транскрипт последних реплик (them = собеседники, me = пользователь).
Ответь строго JSON: {"answer": true|false, "question": "кратко, что спросили"}.
answer=true только если последняя реплика them — вопрос, просьба или явное приглашение высказаться, адресованное пользователю
или всем. Иначе false.
```

### 5.6 Протокол WebSocket `/ws/session/{session_id}` (единый для браузера и CLI)
- Клиент → сервер, binary: `[1 байт channel: 0=them,1=me][int16 PCM LE, 640 сэмплов]`.
- Клиент → сервер, JSON: `{"type":"hello","client":"web|cli","transcribe_me":true}`, `{"type":"answer"}` (manual),
  `{"type":"auto","enabled":true}`, `{"type":"cancel"}`, `{"type":"summary"}`, `{"type":"end"}`.
- Сервер → клиент, JSON: `{"type":"status","stt":"connected|reconnecting","brain":"idle|thinking|streaming"}`,
  `{"type":"transcript","speaker":"them","text":"...","final":false,"ts":...}`,
  `{"type":"answer_start","id":..,"question":"..."}`, `{"type":"answer_delta","id":..,"text":"..."}`, `{"type":"answer_end","id":..,"usage":{...}}`,
  `{"type":"summary","markdown":"..."}`, `{"type":"usage","stt_seconds":{...},"tokens":{...}}`, `{"type":"error","message":"..."}`.
- Реконнект клиента не создаёт новую сессию: `session_id` живёт в URL, буфер и STT-соединения на сервере переживают короткий разрыв.

### 5.7 Хранение, пользователи, учёт
- Таблицы: `users(id, email, name, plan, created_at)`, `sessions(id, user_id, started_at, ended_at, title, platform, lang)`,
  `utterances(session_id, ts, speaker, text)`, `answers(session_id, ts, question, text, model, latency_ms)`,
  `usage(session_id, stt_seconds_them, stt_seconds_me, input_tokens, cached_tokens, output_tokens, cost_usd_est)`,
  `recordings(session_id, path, duration_s, bytes)`, `context_files(user_id, name, text, updated_at)`.
- Личный режим: SQLite в `data/copilot.db`, записи в `data/recordings/`, один пользователь `owner`, вход по токену из `.env` (`APP_TOKEN`).
- Продукт: Postgres, S3-совместимое хранилище (R2) для записей, вход по magic-link/Clerk, Stripe Checkout + webhooks,
  тарифы в минутах/месяц, лимит на одновременные сессии. **Код метрик (usage) пишется с первого дня**, биллинг поверх него — позже.
- Запись звука встречи — **опция пользователя** (у Den'а по умолчанию включена). В PiP всегда виден индикатор «● REC/Transcribing».
  Пользователь отвечает за уведомление участников; в продукте это пункт в условиях использования и настройка «напоминать об уведомлении».

### 5.8 UI (web/)
- **Home:** «Новая сессия» → выбор источников (вкладка/экран · микрофон · BlackHole-режим), язык, тумблер Auto, тумблер записи.
- **Session:** статус-строка (● Listening / ◐ Thinking / ✓ Answer, уровни them/me, STT connected), транскрипт (последние реплики,
  interim курсивом), ответ крупно со стримом (markdown: bold, списки, code), история ответов, кнопки [Ответить] [Auto] [Итоги] [PiP] [Стоп].
- **PiP overlay:** компактная версия — статус, последняя реплика, текущий ответ, [Ответить] [Auto]. Размер ~420×260, запоминается.
  Фокус у приложения встречи не отбирается.
- **History:** список сессий, конспекты, поиск по транскрипту, Q&A по сессии.
- Хоткеи во вкладке/PiP: `⌥+Space` ответить, `⌥+Shift+A` Auto, `⌥+Shift+P` PiP (Windows — Ctrl). Глобальные — расширение.

### 5.9 config.yaml (пример)
```yaml
server: { host: 127.0.0.1, port: 8765, cors_origins: ["http://localhost:5173"] }
audio:  { frame_ms: 40, sample_rate: 16000, them_device: auto, me_device: auto, transcribe_me: true }   # для CLI-режима
stt:    { provider: deepgram, model: nova-3, language: multi, keyterms_from_glossary: true, endpointing_ms: 300, utterance_end_ms: 1000 }
brain:  { provider: anthropic, answer_model: claude-sonnet-5-5, filter_model: claude-haiku-5-5, max_tokens: 350, temperature: 0.2, window_seconds: 90 }
trigger:{ auto_default: false, debounce_seconds: 1.5, min_words_auto: 4 }
store:  { db_url: "sqlite:///data/copilot.db", recordings_dir: data/recordings }
paths:  { context_dir: context, sessions_dir: data/sessions }
```

---

## 6. План сборки — по шагам, каждый шаг проверяется руками

После каждого шага: показать Den'у команду запуска и что он должен увидеть. Не переходить дальше без подтверждения.

1. **✓ Скелет + локальный захват.** `config.py`, `audio/*`, `python -m copilot --check`, `python -m copilot.audio.devices` (уровни them/me). Сделано.
2. **✓ Ядро + WebSocket + терминальный клиент.** (Сделано: Deepgram через сырой WebSocket, `endpointing=100` + своя граница фраз по паузам между словами `stt.phrase_gap_seconds`.) `stt/deepgram_stt.py`, `transcript.py`, `session.py`, `server/ws.py`, `cli/terminal.py`,
   `audio/wav_source.py`. Проверка: `python -m copilot serve` + `python -m copilot cli` печатают `them:`/`me:` с задержкой < 1 с;
   `python -m copilot replay <wav>` даёт стабильный транскрипт фикстуры.
3. **✓ Ответы.** (Сделано: Haiku 5.5 с `thinking: disabled` для живых ответов — первый токен ≈0.9 с, Sonnet 5.5 с thinking для итогов; `temperature` текущий SDK не принимает. CLI: Enter/свой вопрос/`/s`/`/c`/`/r`; `replay --ask --summary`.) `brain/*`, `trigger.py` (manual), `context/`. Проверка: в CLI клавиша → стрим ответа на последний вопрос;
   в usage видно `cache_read_input_tokens > 0` со второго запроса.
4. **✓ Веб-клиент.** (Сделано без сборщика: `copilot/web/` — ES-модули, отдаются самим сервером на `/`; React/Vite — при переходе к продукту, если понадобится. Экран сверху, ответ Claude под ним, транскрипт и история справа, PiP, ⌥Space/⌥⇧A/⌥⇧P, Auto-режим подключён.) `web/`: захват вкладки/экрана + микрофон + BlackHole-режим, транскрипт, кнопка «Ответить», PiP-overlay.
   Проверка: Meet в Chrome (или Zoom-десктоп через BlackHole), PiP поверх окна встречи, ответ по кнопке/хоткею.
4a. **✓ Движки и личные чаты.** `brain.engines` (fast/smart/deep/max = Haiku/Sonnet/Opus/Fable 5.x). Параметры
   «размышлений» разные по семействам (проверено на API, окт. 2026): Haiku — `thinking: disabled`; Sonnet 5.5 —
   `thinking: between_tools` (disabled → 400); Opus 5.5 и Fable 5.1 — только `adaptive` + `output_config.effort`.
   Личная база чатов: `copilot/knowledge/` (импорт выгрузок claude.ai/ChatGPT/Telegram/.md → `data/private/<user_id>/chats.jsonl`,
   BM25 с простым стеммингом), подключается **только к сессиям того же user_id**; тумблер и источники в UI.
4b. **✓ Десктоп-приложение (`desktop/`, Electron вместо Tauri — быстрее собрать для Mac и Windows).** Грузит тот же сайт
   (ссылка с токеном в настройках), добавляет: прозрачный click-through overlay с ответом поверх всех окон/рабочих столов/
   полноэкранных приложений (центр/верх/низ, размер, ширина, прозрачность, автоскрытие), трей-меню, глобальные хоткеи
   (⌘/Ctrl+⇧+Space/Enter/H/A/L), языки речи и ответа. Системный звук: Windows — `loopback` в getDisplayMedia;
   macOS — свой помощник `desktop/native/syscap.swift` (ScreenCaptureKit → 16 kHz int16 в stdout, с автоусилением).
   Проверено: `say` → syscap → Deepgram даёт точный русский транскрипт. Сборки ad-hoc/без подписи.
   Языки: `stt.language` можно менять на ходу (`{"type":"language"}` переоткрывает STT), `answer_language` — auto/ru/en.
4c. **✓ QA больших загрузок (окт. 2026).** Без обрезки документов (раньше 200k символов — терялся конец чата/PDF);
   лимиты: 150 МБ на загрузку, 400 МБ на файл в zip, 1.2 ГБ на архив, 80 млн символов текста; медиа в zip не распаковывается.
   Индекс: чанки ~500 симв., инвертированный BM25 на массивах, ранжирование по совпадению слов в одном сообщении,
   мост RU↔EN, ≤2 фрагмента на документ, контекст с соседями и датой; инкрементальное добавление файлов;
   при пустом поиске — расширение запроса Haiku. Ключевые термины из файлов → Deepgram keyterms.
   Замер (25 млн симв.): индекс 5–7 с, поиск 0.05–0.4 с, ~250 МБ RAM; живой прогон 12/12 ответов верны.
5. **Auto-режим + хранение.** (Auto сделан на шаге 4.) `filter.py`, debounce, отмена in-flight; БД (sessions/utterances/answers/usage), запись (MediaRecorder → сервер).
   Проверка: на вопрос в чужой речи ответ сам за ≤ 2 с после конца фразы; на болтовню — нет; сессия видна в History.
6. **После встречи.** `summary.py`: конспект + action items, Q&A по сессии; экспорт markdown. Проверка на реальной встрече Den'а.
7. **Устойчивость + деплой личного режима.** Реконнекты STT/WS, статусы ошибок, Docker, HTTPS (getDisplayMedia требует secure context),
   развёртывание на VPS, вход по токену. Den пользуется ежедневно.
8. **Продукт.** Аккаунты (magic link), Postgres, R2, Stripe, тарифы по минутам, лендинг, онбординг (как шарить вкладку/BlackHole), условия использования.
9. **Расширение Chrome.** Автодетект вкладки встречи, панель в один клик, глобальные хоткеи.

---

## 7. Критерии готовности личного MVP (шаги 2–6)

- От конца вопроса собеседника до первых слов ответа в PiP: ≤ 2 с.
- Работает с Meet/Zoom/Teams во вкладке Chrome и с десктопным Zoom на Mac через BlackHole.
- Русская и английская речь (вперемешку) распознаются корректно.
- Разрыв сети / ошибка API не роняют сессию: статус в UI, автоматический реконнект, транскрипт не теряется.
- UI не блокируется; после встречи есть конспект и action items.
- В БД по каждой сессии есть usage (минуты STT, токены) — основа будущего биллинга.

---

## 8. Ориентир по стоимости (на час встречи)

- Deepgram streaming: ≈ $0.3–0.55 за канал (два канала — вдвое; `me` можно отключить).
- Claude с кэшем контекста: ≈ $0.2–1 в зависимости от числа ответов.
- Итого ≈ $1–2/час себестоимости. Для подписки: считать по минутам, тариф закладывать с запасом ×3–5 (ориентир рынка — LockedIn ≈ $50/мес).

---

## 9. Документация, которую стоит открыть перед соответствующим шагом

- Deepgram live streaming (Python SDK, Nova-3, языки/multi, keyterms): https://developers.deepgram.com/docs
- Anthropic Messages + streaming: https://docs.claude.com/en/api/overview · prompt caching: https://docs.claude.com/en/docs/build-with-claude/prompt-caching
- Claude Agent SDK (личный режим): https://docs.claude.com/en/api/agent-sdk/overview · условия с подпиской: https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
- MDN `getDisplayMedia` (аудио, `systemAudio`, `suppressLocalAudioPlayback`): https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getDisplayMedia
- Document Picture-in-Picture: https://developer.chrome.com/docs/web-platform/document-picture-in-picture
- AudioWorklet: https://developer.mozilla.org/en-US/docs/Web/API/AudioWorklet
- Chrome MV3 `tabCapture` (фаза 9): https://developer.chrome.com/docs/extensions/reference/api/tabCapture
- BlackHole: https://github.com/ExistentialAudio/BlackHole · PyAudioWPatch (Windows CLI-режим): https://github.com/s0d3s/PyAudioWPatch
- faster-whisper: https://github.com/SYSTRAN/faster-whisper

---

## 10. Открытые вопросы (уточнить у Den'а, пока принимаем значения по умолчанию)

- **Интеграции с чатами.** Принято по умолчанию: Telegram-бот, который присылает конспект после встречи и принимает контекст
  (фаза после 6). Чат самой встречи (Meet/Teams) — не читаем. Slack — по запросу.
- **Где встречи.** Принято по умолчанию: и Meet/Zoom во вкладке, и десктопный Zoom/Teams → поддерживаем оба пути захвата (вкладка и BlackHole).
- **Хранить ли аудиозаписи в продукте** или только транскрипты (дешевле, меньше рисков по персональным данным). Для личного режима — храним.

// Meeting Copilot — web client.
// Screen/tab share (+ its audio) → AudioWorklet 16 kHz → WebSocket → server (Deepgram + Claude).
import { AudioCapture, getInput, getScreen, isLoopbackLabel, listInputs } from './audio.js';
import { renderMarkdown } from './md.js';

// Electron preload bridge (desktop app); undefined in a normal browser.
const desktop = window.copilotDesktop || null;

// ------------------------------------------------------------------ elements
// Cached once: the answer panel may move into a Picture-in-Picture window, so never re-query it.
const $ = (sel) => document.querySelector(sel);
const el = {
  stage: $('#stage'), preview: $('#preview'), setup: $('#setup'), stageNote: $('#stage-note'),
  themDevice: $('#them-device'), meDevice: $('#me-device'), meOn: $('#me-on'),
  hintShare: $('#hint-share'), hintDevice: $('#hint-device'),
  start: $('#btn-start'), setupError: $('#setup-error'),
  stop: $('#btn-stop'), summary: $('#btn-summary'), pip: $('#btn-pip'), auto: $('#auto'),
  pillRec: $('#pill-rec'), timer: $('#timer'),
  dotThem: $('#dot-them'), dotMe: $('#dot-me'), dotBrain: $('#dot-brain'), brainLabel: $('#brain-label'),
  lvlThem: $('#lvl-them'), lvlMe: $('#lvl-me'),
  panel: $('#answer-panel'), answerBody: $('#answer-body'), answerMeta: $('#answer-meta'),
  ask: $('#ask'), askInput: $('#ask-input'), answerBtn: $('#btn-answer'), moreBtn: $('#btn-more'),
  engines: $('#engines'), kn: $('#kn'), knSwitch: $('#kn-switch'), knLabel: $('#kn-label'),
  transcript: $('#transcript'), history: $('#history'), historyCount: $('#history-count'),
  summaryModal: $('#summary-modal'), summaryBody: $('#summary-body'),
  tokenModal: $('#token-modal'), tokenForm: $('#token-form'), tokenInput: $('#token-input'),
  toast: $('#toast'),
};

// ------------------------------------------------------------------ small utils
const store = {
  get(k, d = null) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
const uid = () => (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, '') : String(Math.random()).slice(2)).slice(0, 12);
let toastTimer = null;
function toast(msg, { error = false, ms = 4000 } = {}) {
  el.toast.textContent = msg;
  el.toast.classList.toggle('err', error);
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.toast.hidden = true; }, ms);
}
const nearBottom = (box) => box.scrollHeight - box.scrollTop - box.clientHeight < 80;

// ------------------------------------------------------------------ auth
const params = new URLSearchParams(location.search);
let token = params.get('token') || store.get('copilot_token', '');
if (params.get('token')) {
  store.set('copilot_token', token);
  history.replaceState(null, '', location.pathname); // keep the token out of the address bar
}

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { ...(opts.headers || {}), Authorization: `Bearer ${token}` } });
  if (res.status === 401) { el.tokenModal.hidden = false; throw new Error('unauthorized'); }
  return res.json();
}

el.tokenForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  token = el.tokenInput.value.trim();
  store.set('copilot_token', token);
  el.tokenModal.hidden = true;
  boot();
});

// ------------------------------------------------------------------ websocket link
class Link {
  constructor(sessionId, hello, onEvent, onState) {
    this.sessionId = sessionId;
    this.hello = hello;
    this.onEvent = onEvent;
    this.onState = onState;
    this.ws = null;
    this.closed = false;
    this.retry = 0;
    this.connect();
  }

  get open() { return this.ws && this.ws.readyState === WebSocket.OPEN; }

  connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws/session/${this.sessionId}?token=${encodeURIComponent(token)}`;
    const ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      ws.send(JSON.stringify(typeof this.hello === 'function' ? this.hello() : this.hello));
      this.onState('open');
    };
    ws.onmessage = (m) => { if (typeof m.data === 'string') this.onEvent(JSON.parse(m.data)); };
    ws.onclose = (e) => {
      if (e.code === 4401) { el.tokenModal.hidden = false; this.closed = true; }
      if (this.closed) { this.onState('closed'); return; }
      this.onState('reconnecting');
      const delay = Math.min(8000, 500 * 2 ** this.retry++);
      setTimeout(() => !this.closed && this.connect(), delay);
    };
  }

  send(obj) { if (this.open) this.ws.send(JSON.stringify(obj)); }

  sendAudio(channel, buf) {
    if (!this.open || this.ws.bufferedAmount > 1 << 20) return; // drop audio rather than lag behind
    const out = new Uint8Array(buf.byteLength + 1);
    out[0] = channel === 'me' ? 1 : 0;
    out.set(new Uint8Array(buf), 1);
    this.ws.send(out);
  }

  close() { this.closed = true; try { this.ws.close(); } catch { /* noop */ } }
}

// ------------------------------------------------------------------ state
const state = {
  cfg: null,
  link: null,
  capture: null,
  screen: null,
  live: false,
  startedAt: 0,
  timer: null,
  answers: new Map(), // id -> {question, text, meta}
  currentAnswer: null,
  interim: { them: null, me: null },
  pipWin: null,
  sessionId: uid(),
  sessionCreated: false,
  sessionEnded: false,
  engine: 'fast',
  lastQuestion: null,
};

const engineLabel = (id) => state.cfg?.engines?.find((e) => e.id === id)?.label || id || '';

// ------------------------------------------------------------------ setup form
function themMode() { return document.querySelector('input[name="them-src"]:checked').value; }

function syncSetup() {
  const device = themMode() === 'device';
  el.hintShare.hidden = device;
  el.hintDevice.hidden = !device;
  el.themDevice.hidden = !device;
  if (device && el.themDevice.options.length <= 1) loadDevices();
}

async function loadDevices() {
  try {
    const inputs = await listInputs();
    const fill = (select, items, saved, pick) => {
      select.innerHTML = '';
      if (select === el.meDevice) select.append(new Option('По умолчанию', ''));
      for (const d of items) select.append(new Option(d.label, d.id));
      const want = saved && items.find((d) => d.id === saved) ? saved : (items.find(pick)?.id ?? select.options[0]?.value);
      if (want !== undefined) select.value = want;
    };
    fill(el.themDevice, inputs, store.get('them_device'), (d) => isLoopbackLabel(d.label));
    fill(el.meDevice, inputs.filter((d) => !isLoopbackLabel(d.label)), store.get('me_device'), () => false);
    if (themMode() === 'device' && !inputs.some((d) => isLoopbackLabel(d.label))) {
      showSetupError('BlackHole не найден среди устройств. Установите: brew install --cask blackhole-2ch, затем Multi-Output Device (см. README).');
    }
  } catch (e) {
    showSetupError('Нет доступа к микрофону: разрешите его для этого сайта в настройках браузера.');
  }
}

function showSetupError(msg) { el.setupError.textContent = msg; el.setupError.hidden = !msg; }

document.querySelectorAll('input[name="them-src"]').forEach((r) => r.addEventListener('change', () => {
  store.set('them_src', themMode());
  showSetupError('');
  syncSetup();
}));
const savedMode = store.get('them_src');
if (savedMode) document.querySelector(`input[name="them-src"][value="${savedMode}"]`).checked = true;
el.meOn.checked = store.get('me_on', '1') === '1';
el.meDevice.addEventListener('focus', () => { if (el.meDevice.options.length <= 1) loadDevices(); }, { once: true });

// ------------------------------------------------------------------ start / stop
el.start.addEventListener('click', start);
el.stop.addEventListener('click', stop);

async function start() {
  showSetupError('');
  if (!navigator.mediaDevices?.getDisplayMedia) {
    showSetupError('Этот браузер не умеет делиться экраном со звуком. Откройте страницу в Chrome или Edge.');
    return;
  }
  const mode = themMode();
  const meOn = el.meOn.checked;
  store.set('me_on', meOn ? '1' : '0');
  if (mode === 'device') store.set('them_device', el.themDevice.value);
  if (el.meDevice.value) store.set('me_device', el.meDevice.value);

  let screen;
  try {
    screen = await getScreen();
  } catch (e) {
    showSetupError(e.name === 'NotAllowedError' ? 'Демонстрация отменена.' : `Не удалось начать демонстрацию: ${e.message}`);
    return;
  }

  const sources = {};
  const notes = [];
  let nativeThem = false; // macOS desktop app: system audio via the ScreenCaptureKit helper
  if (mode === 'share') {
    if (screen.getAudioTracks().length) sources.them = screen;
    else if (desktop?.systemAudio) {
      const r = await desktop.systemAudio.start();
      if (r.ok) nativeThem = true;
      else notes.push(`Системный звук недоступен: ${r.error}. Можно выбрать режим «Устройство (BlackHole)».`);
    } else notes.push('Звук собеседников не захвачен: при выборе вкладки включите «Поделиться звуком вкладки», или выберите режим «Устройство (BlackHole)».');
  } else {
    try { sources.them = await getInput(el.themDevice.value, { clean: true }); }
    catch (e) { notes.push(`Не удалось открыть устройство собеседников: ${e.message}`); }
  }
  if (meOn) {
    try { sources.me = await getInput(el.meDevice.value || undefined); }
    catch (e) { notes.push('Нет доступа к микрофону — ваша речь не распознаётся.'); }
  }
  if (!sources.them && !sources.me && !nativeThem) {
    screen.getTracks().forEach((t) => t.stop());
    showSetupError(notes.join(' ') || 'Нет ни одного источника звука.');
    return;
  }

  // UI → live
  state.screen = screen;
  el.preview.srcObject = screen;
  el.stage.classList.add('live');
  el.setup.hidden = true;
  el.stageNote.textContent = notes.join(' ');
  el.stageNote.hidden = !notes.length;
  screen.getVideoTracks()[0]?.addEventListener('ended', stop); // "Stop sharing" in the browser bar
  resetTranscript();
  if (!state.resumed && feed.querySelector('.turn')) { // earlier answers stay readable above a divider
    for (const t of feed.querySelectorAll('.turn.latest')) t.classList.remove('latest');
    const hr = document.createElement('div');
    hr.className = 'meeting-divider';
    hr.textContent = `Новая встреча · ${clock()}`;
    feed.append(hr);
  }

  // connection (dots start as "connecting"; server status events then take over)
  setDot(el.dotThem, sources.them || nativeThem ? 'warn' : '');
  setDot(el.dotMe, sources.me ? 'warn' : '');
  if (state.link) state.link.close();
  if (state.sessionEnded) newMeetingId();
  try { await ensureSession(); } catch { /* the websocket creates it anyway */ }
  const channels = [...new Set([...(nativeThem ? ['them'] : []), ...Object.keys(sources)])];
  // hello is re-sent on every reconnect with the current settings (engine / Auto / chats)
  state.link = new Link(state.sessionId, () => ({
    type: 'hello', client: 'web', channels,
    engine: state.engine, auto: el.auto.checked, knowledge: el.kn.checked,
    language: state.language, answer_language: state.answerLanguage,
  }), onEvent, onLinkState);
  state.capture = new AudioCapture({
    onFrame: (ch, buf) => state.link?.sendAudio(ch, buf),
    onLevel: setLevel,
  });
  for (const [ch, stream] of Object.entries(sources)) {
    try { await state.capture.attach(ch, stream); }
    catch (e) { toast(`Канал ${ch}: ${e.message}`, { error: true }); }
  }

  state.live = true;
  state.resumed = false;
  store.set('copilot_meeting', state.sessionId); // a reload during the call comes back to this meeting
  state.startedAt = state.resumeStartedAt || Date.now();
  state.resumeStartedAt = 0;
  el.pillRec.hidden = false;
  clearInterval(state.timer);
  state.timer = setInterval(tick, 1000);
  tick();
  el.stop.hidden = false;
  [el.summary, el.pip, el.answerBtn, el.moreBtn].forEach((b) => { b.disabled = false; });
}

function stop() {
  if (!state.live) return;
  state.live = false;
  state.sessionEnded = true;
  store.set('copilot_meeting', '');
  state.link?.send({ type: 'end' }); // the socket stays open: summary still works after the meeting
  state.capture?.stop();
  desktop?.systemAudio?.stop();
  state.screen?.getTracks().forEach((t) => t.stop());
  el.preview.srcObject = null;
  el.stage.classList.remove('live');
  el.stageNote.hidden = true;
  el.setup.hidden = false;
  el.start.textContent = 'Новая встреча';
  el.stop.hidden = true;
  el.pillRec.hidden = true;
  clearInterval(state.timer);
  setLevel('them', 0);
  setLevel('me', 0);
  el.answerBtn.disabled = true;
  el.moreBtn.disabled = true;
}

function tick() {
  const s = Math.floor((Date.now() - state.startedAt) / 1000);
  el.timer.textContent = `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

function onLinkState(s) {
  if (s === 'reconnecting') { setDot(el.dotBrain, 'warn'); el.brainLabel.textContent = 'Переподключение…'; }
  if (s === 'open') { setDot(el.dotBrain, state.cfg?.answers ? 'ok' : 'err'); el.brainLabel.textContent = 'Claude'; }
}

// ------------------------------------------------------------------ levels & status
const levels = { them: 0, me: 0 };
function setLevel(ch, rms) {
  levels[ch] = Math.max(rms, levels[ch] * 0.85);
  const db = 20 * Math.log10(Math.max(levels[ch], 1e-5));
  const pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
  (ch === 'me' ? el.lvlMe : el.lvlThem).style.width = `${pct}%`;
}
function setDot(dot, cls) { dot.className = `dot ${cls || ''}`; }
const STT_DOT = { connected: 'ok', connecting: 'warn', reconnecting: 'warn', idle: 'warn', error: 'err', closed: '', off: '' };

function applyStatus(ev) {
  if (ev.stt) {
    setDot(el.dotThem, STT_DOT[ev.stt.them] ?? '');
    setDot(el.dotMe, STT_DOT[ev.stt.me] ?? '');
  }
  if (ev.brain) {
    const busy = ev.brain === 'thinking' || ev.brain === 'streaming';
    setDot(el.dotBrain, busy ? 'busy' : (state.cfg?.answers ? 'ok' : 'err'));
    el.brainLabel.textContent = ev.brain === 'thinking' ? 'Claude думает…' : ev.brain === 'streaming' ? 'Claude отвечает…' : 'Claude';
  }
  if (typeof ev.auto === 'boolean') el.auto.checked = ev.auto;
  if (typeof ev.knowledge === 'boolean' && !el.knSwitch.hidden) el.kn.checked = ev.knowledge;
  if (ev.engine) setEngine(ev.engine, { send: false });
  if (ev.language || ev.answer_language) setLanguage(ev.language, ev.answer_language, { send: false });
  desktop?.state({ auto: el.auto.checked, engine: state.engine, language: state.language,
    answerLanguage: state.answerLanguage, live: state.live });
  if (ev.detail && /error|rejected/i.test(ev.detail)) toast(ev.detail, { error: true });
}

// ------------------------------------------------------------------ transcript
function resetTranscript() {
  el.transcript.innerHTML = '';
  state.interim = { them: null, me: null };
}

function uttNode(speaker, text, ts, interim = false) {
  const node = document.createElement('div');
  node.className = `utt ${speaker}${interim ? ' interim' : ''}`;
  const who = document.createElement('div');
  who.className = 'who';
  who.innerHTML = `<b>${speaker === 'me' ? 'Я' : 'Собеседник'}</b><span>${ts ? fmtTime(ts) : ''}</span>`;
  const body = document.createElement('div');
  body.className = 'text';
  body.textContent = text;
  node.append(who, body);
  if (speaker === 'them' && !interim) {
    const btn = document.createElement('button');
    btn.className = 'ask-this';
    btn.textContent = 'Ответить на это';
    btn.addEventListener('click', () => requestAnswer(text));
    node.append(btn);
  }
  return node;
}

function addTranscript(ev) {
  const box = el.transcript;
  const stick = nearBottom(box);
  box.querySelector('.placeholder')?.remove();
  const prev = state.interim[ev.speaker];
  if (ev.final) {
    const node = uttNode(ev.speaker, ev.text, ev.ts);
    if (prev) { prev.replaceWith(node); state.interim[ev.speaker] = null; } else box.append(node);
  } else if (prev) {
    prev.querySelector('.text').textContent = ev.text;
  } else {
    state.interim[ev.speaker] = uttNode(ev.speaker, ev.text, ev.ts, true);
    box.append(state.interim[ev.speaker]);
  }
  if (stick) box.scrollTop = box.scrollHeight;
}

// ------------------------------------------------------------------ answers
function requestAnswer(question, engine) {
  if (!state.link) { toast('Сначала начните встречу'); return; }
  if (!state.cfg?.answers) { toast(state.cfg?.answers_error || 'Ответы недоступны', { error: true }); return; }
  const msg = { type: 'answer' };
  if (question) msg.question = question;
  if (engine) msg.engine = engine;
  state.link.send(msg);
}

// «Подробнее»: the same question again, with the strong engine and a longer answer
function requestMore() {
  requestAnswer(state.lastQuestion || undefined, state.cfg?.more_engine || 'max');
}

el.answerBtn.addEventListener('click', () => requestAnswer());
el.moreBtn.addEventListener('click', requestMore);
el.ask.addEventListener('submit', (e) => {
  e.preventDefault();
  const q = el.askInput.value.trim();
  if (!q) { requestAnswer(); return; }
  el.askInput.value = '';
  requestAnswer(q);
});

// ------------------------------------------------------------------ answers: a scrollable conversation
// Every answer stays in the panel; scroll up to re-read older ones. The newest is at the bottom and gets the
// big type; older ones shrink. A new answer is brought into view with its first (key) sentence on top.
const feed = el.answerBody;
const jumpBtn = document.createElement('button');
jumpBtn.type = 'button';
jumpBtn.className = 'jump';
jumpBtn.hidden = true;
jumpBtn.textContent = 'Новый ответ ↓';
el.panel.append(jumpBtn);
jumpBtn.addEventListener('click', () => { const t = feed.lastElementChild; if (t) showTurn(t); });
feed.addEventListener('scroll', () => { if (nearBottom(feed)) jumpBtn.hidden = true; });

const clock = (ts) => new Date((ts || Date.now() / 1000) * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', hour12: false });

function showTurn(turn) {
  feed.scrollTop = Math.max(0, turn.offsetTop - feed.offsetTop - 6);
  jumpBtn.hidden = true;
}

function turnLabel(a) {
  if (!a.question) return 'По последним репликам';
  const who = a.typed ? 'Ваш вопрос' : a.source === 'auto' ? 'Auto' : 'Вопрос';
  return `${who}: ${a.question}`;
}

function makeTurn(a) {
  const turn = document.createElement('article');
  turn.className = 'turn';
  turn.dataset.id = String(a.id);
  const head = document.createElement('div');
  head.className = 'turn-head';
  const q = document.createElement('div');
  q.className = 'turn-q';
  q.textContent = turnLabel(a);
  const time = document.createElement('time');
  time.textContent = clock(a.ts);
  head.append(q, time);
  const src = document.createElement('div');
  src.className = 'sources';
  (a.sources || []).forEach((s) => {
    const chip = document.createElement('span');
    chip.textContent = `${s.source === 'file' ? 'из файла' : 'из материалов'}: ${s.title}${s.date ? ` · ${s.date}` : ''}`;
    chip.title = `${s.title} (${s.source})`;
    src.append(chip);
  });
  src.hidden = !(a.sources || []).length;
  const body = document.createElement('div');
  body.className = 'turn-body';
  const meta = document.createElement('div');
  meta.className = 'turn-meta';
  turn.append(head, src, body, meta);
  a.el = turn;
  return turn;
}

function renderAnswer(a, streaming) {
  const body = a.el?.querySelector('.turn-body');
  if (!body) return;
  if (!a.text && streaming) {
    body.innerHTML = '<div class="dots" role="status" aria-label="Claude думает"><i></i><i></i><i></i></div>';
    return;
  }
  body.innerHTML = renderMarkdown(a.text) || '<p class="placeholder">Пустой ответ.</p>';
  if (streaming) body.lastElementChild?.classList.add('cursor');
}

function answerMeta(a, usage, status) {
  const u = usage || {};
  const secs = u.first_token_ms != null ? `${(u.first_token_ms / 1000).toFixed(1)} с` : '';
  return [status === 'cancelled' ? 'отменён' : status === 'error' ? 'ошибка' : '', a.engineLabel, secs,
    (u.model || '').replace('claude-', '')].filter(Boolean).join(' · ');
}

function appendTurn(a) {
  feed.querySelector(':scope > .placeholder')?.remove();
  for (const t of feed.querySelectorAll('.turn.latest')) t.classList.remove('latest');
  const turn = makeTurn(a);
  turn.classList.add('latest');
  feed.append(turn);
  return turn;
}

function onAnswerStart(ev) {
  const a = { id: ev.id, question: ev.question, text: '', source: ev.source, typed: ev.typed, ts: ev.ts, sources: ev.sources || [] };
  const reading = !nearBottom(feed) && feed.querySelector('.turn'); // scrolled up into older answers
  state.answers.set(ev.id, a);
  state.currentAnswer = ev.id;
  if (ev.question) state.lastQuestion = ev.question;
  a.engineLabel = ev.engine_label || engineLabel(ev.engine);
  el.answerMeta.textContent = `${a.engineLabel} · думаю…`;
  const turn = appendTurn(a);
  renderAnswer(a, true);
  // asked yourself → always show it; Auto while you're reading an older answer → offer a button instead
  if (reading && ev.source === 'auto') jumpBtn.hidden = false;
  else requestAnimationFrame(() => showTurn(turn));
  el.panel.classList.remove('flash');
  void el.panel.offsetWidth; // restart the animation
  el.panel.classList.add('flash');
}

function onAnswerDelta(ev) {
  const a = state.answers.get(ev.id);
  if (!a) return;
  a.text += ev.text;
  renderAnswer(a, true); // no auto-scroll while streaming: the key sentence comes first and stays in view
  if (state.currentAnswer === ev.id) el.answerMeta.textContent = `${a.engineLabel} · отвечаю…`;
}

function onAnswerEnd(ev) {
  const a = state.answers.get(ev.id);
  if (!a) return;
  a.text = ev.text || a.text;
  a.meta = answerMeta(a, ev.usage, ev.status);
  renderAnswer(a, false);
  const m = a.el?.querySelector('.turn-meta');
  if (m) m.textContent = a.meta;
  if (ev.status === 'cancelled' && !a.text) a.el?.remove();
  if (state.currentAnswer === ev.id) el.answerMeta.textContent = a.meta;
  if (ev.status !== 'cancelled' && a.text) addHistory(a);
}

// After a reload / reconnect the server sends the meeting's earlier answers: rebuild the conversation.
function restoreAnswers(list) {
  if (!list?.length) return;
  const have = new Set([...feed.querySelectorAll('.turn')].map((t) => t.dataset.id));
  if (list.every((r) => have.has(String(r.id)))) return; // a short reconnect: the feed is already there
  for (const r of list) {
    if (have.has(String(r.id))) continue;
    const a = { id: r.id, question: r.question, text: r.text, source: r.source, typed: r.typed, ts: r.ts,
      sources: r.sources || [], engineLabel: r.engine_label || engineLabel(r.engine) };
    a.meta = answerMeta(a, r.usage, r.status);
    state.answers.set(a.id, a);
    appendTurn(a);
    renderAnswer(a, false);
    a.el.querySelector('.turn-meta').textContent = a.meta;
    addHistory(a);
    state.currentAnswer = a.id;
  }
  requestAnimationFrame(() => showTurn(feed.lastElementChild));
}

function addHistory(a) {
  el.history.querySelector('.placeholder')?.remove();
  const card = document.createElement('div');
  card.className = 'hist';
  const q = document.createElement('div');
  q.className = 'q';
  q.textContent = a.question || 'По последним репликам';
  const body = document.createElement('div');
  body.className = 'a';
  body.innerHTML = renderMarkdown(a.text);
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = `${clock(a.ts)} · ${a.meta || ''}`;
  card.append(q, body, meta);
  card.title = 'Показать в ленте ответов';
  card.addEventListener('click', () => { if (a.el?.isConnected) showTurn(a.el); });
  el.history.prepend(card);
  el.historyCount.textContent = String(el.history.querySelectorAll('.hist').length);
}

// ------------------------------------------------------------------ server events
function onEvent(ev) {
  switch (ev.type) {
    case 'snapshot':
      resetTranscript();
      (ev.transcript || []).forEach((u) => addTranscript({ ...u, final: true }));
      restoreAnswers(ev.answers);
      applyStatus(ev);
      break;
    case 'status': applyStatus(ev); break;
    case 'transcript': addTranscript(ev); break;
    case 'answer_start': onAnswerStart(ev); desktop?.overlay(ev); break;
    case 'answer_delta': onAnswerDelta(ev); desktop?.overlay(ev); break;
    case 'answer_end': onAnswerEnd(ev); desktop?.overlay(ev); break;
    case 'auto_check':
      if (!ev.answer && state.currentAnswer == null) el.answerMeta.textContent = 'Auto: вопроса к вам не было';
      break;
    case 'summary': showSummary(ev.markdown); break;
    case 'usage': {
      const stt = Object.entries(ev.stt_seconds || {}).map(([k, v]) => `${k} ${Math.round(v)} с`).join(', ');
      toast(`Встреча завершена · STT: ${stt || '—'} · ответов: ${ev.answers ?? 0}`);
      break;
    }
    case 'error': toast(ev.message, { error: true, ms: 6000 }); break;
    default: break;
  }
}

// ------------------------------------------------------------------ summary
el.summary.addEventListener('click', () => {
  if (!state.link) return;
  state.link.send({ type: 'summary' });
  el.summary.disabled = true;
  el.summary.textContent = 'Готовлю…';
});
function showSummary(md) {
  el.summary.disabled = false;
  el.summary.textContent = 'Итоги';
  el.summaryBody.innerHTML = renderMarkdown(md);
  el.summaryBody.dataset.raw = md;
  el.summaryModal.hidden = false;
}
$('#summary-close').addEventListener('click', () => { el.summaryModal.hidden = true; });
$('#summary-copy').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(el.summaryBody.dataset.raw || ''); toast('Скопировано'); }
  catch { toast('Не удалось скопировать', { error: true }); }
});

// ------------------------------------------------------------------ engines
function buildEngines() {
  el.engines.innerHTML = '';
  for (const e of state.cfg?.engines || []) {
    const b = document.createElement('button');
    b.type = 'button';
    b.dataset.id = e.id;
    b.textContent = e.label;
    b.title = `${e.label} — ${e.model}`;
    b.setAttribute('role', 'radio');
    b.addEventListener('click', () => setEngine(e.id));
    el.engines.append(b);
  }
  const saved = store.get('engine');
  setEngine(saved && state.cfg.engines.some((e) => e.id === saved) ? saved : state.cfg.default_engine, { send: false });
}

function setEngine(id, { send = true } = {}) {
  state.engine = id;
  store.set('engine', id);
  el.engines.querySelectorAll('button').forEach((b) => {
    const on = b.dataset.id === id;
    b.classList.toggle('active', on);
    b.setAttribute('aria-checked', String(on));
  });
  if (send) state.link?.send({ type: 'engine', name: id });
}

// ------------------------------------------------------------------ private chats toggle
el.kn.addEventListener('change', () => {
  store.set('kn', el.kn.checked ? '1' : '0');
  state.link?.send({ type: 'knowledge', enabled: el.kn.checked });
});

// ------------------------------------------------------------------ auto toggle
el.auto.addEventListener('change', () => {
  desktop?.state({ auto: el.auto.checked });
  store.set('auto', el.auto.checked ? '1' : '0');
  state.link?.send({ type: 'auto', enabled: el.auto.checked });
});

// ------------------------------------------------------------------ picture-in-picture
el.pip.addEventListener('click', togglePip);
async function togglePip() {
  if (state.pipWin) { state.pipWin.close(); return; }
  if (!('documentPictureInPicture' in window)) { toast('Окно поверх других доступно в Chrome/Edge 116+', { error: true }); return; }
  const win = await window.documentPictureInPicture.requestWindow({ width: 460, height: 340 });
  for (const sheet of document.styleSheets) {
    try {
      const style = win.document.createElement('style');
      style.textContent = [...sheet.cssRules].map((r) => r.cssText).join('\n');
      win.document.head.append(style);
    } catch {
      const link = win.document.createElement('link');
      link.rel = 'stylesheet';
      link.href = sheet.href;
      win.document.head.append(link);
    }
  }
  win.document.title = 'Ответы — Meeting Copilot';
  win.document.body.classList.add('pip');
  const holder = document.createElement('div');
  holder.className = 'answer';
  holder.innerHTML = '<div class="answer-body"><p class="placeholder">Ответы открыты в отдельном окне поверх других.</p></div>';
  el.panel.replaceWith(holder);
  win.document.body.append(el.panel);
  win.document.addEventListener('keydown', onKey);
  state.pipWin = win;
  el.pip.textContent = 'Вернуть окно';
  win.addEventListener('pagehide', () => {
    holder.replaceWith(el.panel);
    state.pipWin = null;
    el.pip.textContent = 'Поверх окон';
  });
}

// ------------------------------------------------------------------ hotkeys
function onKey(e) {
  if (!e.altKey) return;
  if (e.code === 'Space' && e.shiftKey) { e.preventDefault(); requestMore(); }
  else if (e.code === 'Space') { e.preventDefault(); requestAnswer(); }
  else if (e.shiftKey && e.code === 'KeyA') { e.preventDefault(); el.auto.checked = !el.auto.checked; el.auto.dispatchEvent(new Event('change')); }
  else if (e.shiftKey && e.code === 'KeyP') { e.preventDefault(); if (!el.pip.disabled) togglePip(); }
}
document.addEventListener('keydown', onKey);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') el.summaryModal.hidden = true; });

// ------------------------------------------------------------------ answer panel: size, font, copy, hide screen
const leftCol = document.querySelector('.left');
const root = document.documentElement;

function setAnswerHeight(px) {
  const max = leftCol.clientHeight - 140; // keep at least a strip of the screen visible
  const h = Math.round(Math.max(170, Math.min(max, px)));
  root.style.setProperty('--answer-h', `${h}px`);
  store.set('answer_h', String(h));
}
const savedH = Number(store.get('answer_h'));
if (savedH) root.style.setProperty('--answer-h', `${savedH}px`);

const splitter = $('#splitter');
splitter.addEventListener('pointerdown', (e) => {
  e.preventDefault();
  splitter.setPointerCapture(e.pointerId);
  splitter.classList.add('drag');
  const bottom = leftCol.getBoundingClientRect().bottom;
  const move = (ev) => setAnswerHeight(bottom - ev.clientY);
  const up = () => {
    splitter.classList.remove('drag');
    splitter.removeEventListener('pointermove', move);
    splitter.removeEventListener('pointerup', up);
  };
  splitter.addEventListener('pointermove', move);
  splitter.addEventListener('pointerup', up);
});
splitter.addEventListener('keydown', (e) => {
  if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
  e.preventDefault();
  setAnswerHeight(el.panel.getBoundingClientRect().height + (e.key === 'ArrowUp' ? 30 : -30));
});

let answerFs = Number(store.get('answer_fs')) || 18;
function setFont(px) {
  answerFs = Math.max(14, Math.min(30, px));
  root.style.setProperty('--answer-fs', `${answerFs}px`);
  store.set('answer_fs', String(answerFs));
}
setFont(answerFs);
$('#font-down').addEventListener('click', () => setFont(answerFs - 2));
$('#font-up').addEventListener('click', () => setFont(answerFs + 2));

$('#copy-answer').addEventListener('click', async () => {
  const a = state.answers.get(state.currentAnswer); // the newest answer
  if (!a?.text) { toast('Пока нечего копировать'); return; }
  try { await navigator.clipboard.writeText(a.text); toast('Ответ скопирован'); }
  catch { toast('Не удалось скопировать', { error: true }); }
});

const stageBtn = $('#toggle-stage');
function setStageHidden(hidden) {
  document.body.classList.toggle('no-stage', hidden);
  stageBtn.setAttribute('aria-pressed', String(hidden));
  stageBtn.textContent = hidden ? 'Показать экран' : 'Скрыть экран';
  store.set('no_stage', hidden ? '1' : '0');
}
setStageHidden(store.get('no_stage') === '1');
stageBtn.addEventListener('click', () => setStageHidden(!document.body.classList.contains('no-stage')));

// ------------------------------------------------------------------ languages
state.language = store.get('lang') || 'multi';
state.answerLanguage = store.get('answer_lang') || 'auto';

function fillLanguageSelects() {
  const langs = state.cfg?.languages || [{ id: 'multi', label: 'Авто' }];
  const ans = state.cfg?.answer_languages || [{ id: 'auto', label: 'Как в вопросе' }];
  for (const sel of [$('#lang'), $('#lang-top')]) {
    sel.innerHTML = '';
    langs.forEach((l) => sel.append(new Option(l.label, l.id)));
  }
  const a = $('#answer-lang');
  a.innerHTML = '';
  ans.forEach((l) => a.append(new Option(l.label, l.id)));
  if (!langs.some((l) => l.id === state.language)) state.language = langs[0].id;
  setLanguage(state.language, state.answerLanguage, { send: false });
}

function setLanguage(stt, answer, { send = true } = {}) {
  if (stt) { state.language = stt; store.set('lang', stt); $('#lang').value = stt; $('#lang-top').value = stt; }
  if (answer) { state.answerLanguage = answer; store.set('answer_lang', answer); $('#answer-lang').value = answer; }
  if (send) state.link?.send({ type: 'language', stt: stt || undefined, answer: answer || undefined });
  desktop?.state({ language: state.language, answerLanguage: state.answerLanguage });
}
$('#lang').addEventListener('change', (e) => setLanguage(e.target.value, null));
$('#lang-top').addEventListener('change', (e) => {
  setLanguage(e.target.value, null);
  if (state.live) toast(`Язык речи: ${e.target.selectedOptions[0].textContent}`);
});
$('#answer-lang').addEventListener('change', (e) => setLanguage(null, e.target.value));

function cycleLanguage() {
  const ids = (state.cfg?.languages || []).map((l) => l.id).slice(0, 3); // Авто → RU → EN
  const next = ids[(ids.indexOf(state.language) + 1) % ids.length] || 'multi';
  setLanguage(next, null);
  toast(`Язык речи: ${state.cfg.languages.find((l) => l.id === next)?.label || next}`);
}

// ------------------------------------------------------------------ files from the start screen (this meeting only)
const setupDrop = $('#setup-drop');
async function setupUpload(items) {
  if (!items.length) return;
  const list = $('#setup-filelist');
  const done = await uploadFiles(items, 'session', list);
  done.forEach((f) => {
    const s = document.createElement('span');
    s.textContent = `${f.name} · ${f.chars}`;
    list.append(s);
  });
}
wireDrop(setupDrop, $('#setup-files'), $('#setup-folder'), setupUpload);

// ------------------------------------------------------------------ desktop app bridge (Electron preload)
// In the desktop app: answers are mirrored to a transparent overlay over all windows, and global
// hotkeys / the tray menu send commands here. In a normal browser `desktop` is undefined.
if (desktop) {
  document.body.classList.add('desktop');
  // global hotkeys of the app instead of the in-tab ones
  const mod = desktop.platform === 'darwin' ? '⌘⇧' : 'Ctrl+Shift+';
  for (const k of document.querySelectorAll('kbd')) k.textContent = k.textContent.replace(/⌥\s?Space/, `${mod}Space`);
  for (const el of document.querySelectorAll('[title*="⌥"]')) {
    el.title = el.title.replace('⌥⇧Space', `${mod}Enter`).replace('⌥Space', `${mod}Space`).replace('⌥⇧A', `${mod}A`);
  }
  if (desktop.systemAudio) {
    desktop.systemAudio.onData((buf) => {
      if (!state.live) return;
      state.link?.sendAudio('them', buf);
      const pcm = new Int16Array(buf);
      let sq = 0;
      for (let i = 0; i < pcm.length; i += 4) sq += (pcm[i] / 32768) ** 2;
      setLevel('them', Math.sqrt(sq / (pcm.length / 4)));
    });
    desktop.systemAudio.onError((msg) => toast(`Системный звук: ${msg}`, { error: true, ms: 8000 }));
    const hint = $('#hint-share');
    if (hint) hint.innerHTML = 'В приложении звук встречи берётся из системы — подойдёт любое окно или весь экран, BlackHole не нужен.';
  }
  desktop.onCommand((cmd, arg) => {
    switch (cmd) {
      case 'answer': requestAnswer(); break;
      case 'more': requestMore(); break;
      case 'auto': el.auto.checked = arg ?? !el.auto.checked; el.auto.dispatchEvent(new Event('change')); break;
      case 'language': setLanguage(arg, null); break;
      case 'answer-language': setLanguage(null, arg); break;
      case 'cycle-language': cycleLanguage(); break;
      case 'engine': setEngine(arg); break;
      case 'start': if (!state.live) start(); break;
      case 'stop': stop(); break;
      default: break;
    }
  });
}

// ------------------------------------------------------------------ tabs
function showTab(name) {
  document.querySelectorAll('.tab').forEach((x) => x.classList.toggle('active', x.dataset.tab === name));
  for (const n of ['transcript', 'history', 'prep']) $(`#tab-${n}`).hidden = n !== name;
  if (name === 'prep') refreshDocs();
}
document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => showTab(t.dataset.tab)));
$('#open-prep').addEventListener('click', () => showTab('prep'));

// ------------------------------------------------------------------ meeting prep
// The meeting id exists before the meeting starts, so the brief and "this meeting" files can be added upfront.
async function ensureSession() {
  if (state.sessionCreated) return;
  await api('/api/sessions', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: state.sessionId, transcribe_me: el.meOn.checked }),
  });
  state.sessionCreated = true;
  const brief = $('#brief').value.trim();
  if (brief) await saveBrief();
}

function newMeetingId() {
  state.sessionId = uid();
  state.sessionCreated = false;
  state.sessionEnded = false;
  refreshDocs();
}

function debounce(fn, ms) {
  let t = null;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function markSaved(node, text, ok = true) { node.textContent = text; node.classList.toggle('ok', ok); }

const saveProfile = debounce(async () => {
  try {
    await api('/api/profile', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: $('#profile').value }) });
    markSaved($('#profile-state'), 'сохранено ✓');
  } catch { markSaved($('#profile-state'), 'не сохранилось', false); }
}, 700);

async function saveBrief() {
  try {
    if (state.sessionEnded) newMeetingId();
    if (!state.sessionCreated) { await ensureSession(); return; } // ensureSession re-sends the brief
    await api(`/api/sessions/${state.sessionId}/brief`, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: $('#brief').value }) });
    markSaved($('#brief-state'), 'сохранено для этой встречи ✓');
  } catch { markSaved($('#brief-state'), 'не сохранилось', false); }
}
const saveBriefSoon = debounce(saveBrief, 700);

$('#profile').addEventListener('input', () => { markSaved($('#profile-state'), 'сохраняю…', false); saveProfile(); });

// «Слова для распознавания»: a live meeting reconnects speech recognition with them (≈1 s), so save calmly
const saveVocab = debounce(async () => {
  try {
    const r = await api('/api/vocab', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: $('#vocab').value }) });
    markSaved($('#vocab-state'), `сохранено · ${r.terms} ${plural(r.terms, 'слово', 'слова', 'слов')} ✓`);
  } catch { markSaved($('#vocab-state'), 'не сохранилось', false); }
}, 1800);
$('#vocab').addEventListener('input', () => { markSaved($('#vocab-state'), 'сохраняю…', false); saveVocab(); });
$('#open-vocab').addEventListener('click', () => { $('#open-prep').click(); setTimeout(() => $('#vocab').focus(), 150); });
$('#brief').addEventListener('input', () => { markSaved($('#brief-state'), 'сохраняю…', false); saveBriefSoon(); });

function upScope() { return document.querySelector('input[name="up-scope"]:checked').value; }

const MAX_UPLOAD_MB = 150; // same limit as the server (api.py MAX_UPLOAD)

// XHR instead of fetch: big chat exports take a while, so show real upload progress, then «обрабатываю».
function postFile(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.setRequestHeader('Authorization', `Bearer ${token}`);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.upload.onload = () => onProgress(1);
    xhr.onerror = () => reject(new Error('нет связи с сервером'));
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* proxy error pages are not JSON */ }
      if (xhr.status === 401) { el.tokenModal.hidden = false; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else if (xhr.status === 413) reject(new Error(data.detail || `файл больше ${MAX_UPLOAD_MB} МБ`));
      else reject(new Error(data.detail || `ошибка сервера ${xhr.status}`));
    };
    xhr.send(file);
  });
}

const plural = (n, one, few, many) => {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
};

// Formats the server reads (copilot/knowledge/importers.py PARSED) and folders that are never knowledge.
const READABLE = /\.(zip|json|md|markdown|txt|rst|tex|org|sql|py|ipynb|js|ts|tsx|jsx|java|kt|scala|go|rb|php|cs|cpp|c|h|hpp|rs|swift|r|sh|bash|zsh|ps1|ya?ml|toml|ini|cfg|conf|properties|xml|css|scss|vue|svelte|graphql|proto|tf|log|csv|tsv|pdf|docx|xlsx|pptx|html?|rtf)$/i;
const JUNK = new Set(['.git', 'node_modules', '__pycache__', '.venv', 'venv', '.idea', '.vscode', '__MACOSX', 'dist', 'build', '.next', '.cache', 'target']);
const usable = (path) => {
  const parts = path.split('/');
  return READABLE.test(path) && !parts.some((p) => JUNK.has(p) || p.startsWith('.'));
};

// Files and whole folders dropped from Finder/Explorer: walk directories (readEntries returns ≤100 at a time).
async function droppedItems(dt) {
  const entries = [...(dt.items || [])].map((it) => it.webkitGetAsEntry?.()).filter(Boolean);
  if (!entries.length) return [...dt.files].map((file) => ({ file, path: file.name }));
  const out = [];
  async function walk(entry) {
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      out.push({ file, path: entry.fullPath.replace(/^\//, '') });
    } else if (entry.isDirectory && !JUNK.has(entry.name) && !entry.name.startsWith('.')) {
      const reader = entry.createReader();
      for (;;) {
        const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        if (!batch.length) break;
        for (const e of batch) await walk(e);
      }
    }
  }
  for (const e of entries) await walk(e);
  return out;
}

const fmtChars = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1).replace('.', ',')} млн символов` : `${Math.max(1, Math.round(n / 1000))}k символов`);

async function uploadOne(item, scope, onProgress) {
  if (scope === 'session') {
    if (state.sessionEnded) newMeetingId();
    await ensureSession();
  }
  const qs = new URLSearchParams({ name: item.path, scope });
  if (scope === 'session') qs.set('session_id', state.sessionId);
  return postFile(`/api/upload?${qs}`, item.file, onProgress);
}

// items: File objects (picker) or {file, path} (folders). One file → its own progress row and toast;
// a folder → one row «12 / 140 файлов» and a summary at the end. Returns what was attached.
async function uploadFiles(items, forcedScope, progressList = null) {
  const scope = forcedScope || upScope();
  const all = items.map((x) => (x instanceof File ? { file: x, path: x.webkitRelativePath || x.name } : x));
  const list = all.filter((x) => usable(x.path) && x.file.size > 0);
  const ignored = all.length - list.length;
  const tooBig = list.filter((x) => x.file.size > MAX_UPLOAD_MB * 1048576);
  tooBig.forEach((x) => toast(`${x.path}: ${Math.round(x.file.size / 1048576)} МБ — больше ${MAX_UPLOAD_MB} МБ. `
    + 'Для Telegram выгрузите чат без фото и видео.', { error: true, ms: 9000 }));
  const queue = list.filter((x) => !tooBig.includes(x));
  if (!queue.length) {
    if (ignored) toast(`Нечего загружать: ${ignored} ${plural(ignored, 'файл', 'файла', 'файлов')} — картинки, видео или неизвестные форматы`, { error: true });
    return [];
  }
  const done = [];
  const batch = queue.length > 1;
  const roots = [...new Set(queue.map((x) => x.path.split('/')[0]))];
  const label = batch ? (roots.length === 1 && queue[0].path.includes('/') ? `Папка «${roots[0]}»` : `${queue.length} файлов`) : queue[0].path;
  const row = docRow({ title: label, chars: 0 }, null, true);
  const meta = row.querySelector('.m');
  (progressList || $('#doclist')).prepend(row);
  let finished = 0, chars = 0, docs = 0, skipped = ignored;
  const failed = [];
  async function worker() {
    while (queue.length) {
      const item = queue.shift();
      try {
        const data = await uploadOne(item, scope, (p) => {
          if (!batch) meta.textContent = p < 1 ? `загружаю ${Math.round(p * 100)}%` : 'читаю и индексирую…';
        });
        chars += data.chars || 0;
        docs += data.added || 0;
        skipped += data.skipped || 0;
        done.push({ name: item.path, added: data.added, chars: fmtChars(data.chars || 0) });
      } catch (e) {
        if (/не узнал формат/i.test(e.message)) skipped += 1;
        else failed.push(`${item.path}: ${e.message}`);
      }
      finished += 1;
      if (batch) meta.textContent = `${finished} / ${finished + queue.length} файлов…`;
    }
  }
  if (batch) meta.textContent = `0 / ${queue.length} файлов…`;
  await Promise.all(Array.from({ length: Math.min(3, queue.length) }, worker));
  row.remove();
  const where = scope === 'saved' ? 'в моей базе' : 'подключено к этой встрече';
  if (docs) {
    toast(`${label}: ${docs} ${plural(docs, 'документ', 'документа', 'документов')}, ${fmtChars(chars)} — ${where}`
      + (skipped ? ` · пропущено ${skipped} (картинки, бинарные и пустые файлы)` : ''), { ms: 6000 });
  }
  if (failed.length) toast(failed.slice(0, 2).join(' · ') + (failed.length > 2 ? ` · и ещё ${failed.length - 2}` : ''), { error: true, ms: 9000 });
  else if (!docs) toast(`${label}: в файлах не нашлось текста`, { error: true });
  await refreshDocs();
  if (scope === 'saved') await refreshKnowledgeToggle();
  return batch && done.length ? [{ name: label, added: docs, chars: fmtChars(chars) }] : done;
}

function docRow(d, onDelete, busy = false) {
  const row = document.createElement('div');
  row.className = `doc${busy ? ' busy' : ''}`;
  const t = document.createElement('span');
  t.className = 't';
  t.textContent = d.title;
  t.title = d.title;
  const m = document.createElement('span');
  m.className = 'm';
  const size = d.chars >= 1e6 ? `${(d.chars / 1e6).toFixed(1).replace('.', ',')} млн симв.` : `${Math.max(1, Math.round(d.chars / 1000))}k симв.`;
  m.textContent = busy ? 'загружаю…' : `${d.date ? `${d.date} · ` : ''}${size}`;
  row.append(t, m);
  if (onDelete) {
    const x = document.createElement('button');
    x.type = 'button';
    x.title = 'Удалить';
    x.textContent = '✕';
    x.addEventListener('click', onDelete);
    row.append(x);
  }
  return row;
}

async function refreshDocs() {
  const box = $('#doclist');
  let data;
  try {
    const qs = state.sessionCreated ? `?session_id=${state.sessionId}` : '';
    data = await api(`/api/knowledge/docs${qs}`);
  } catch { return; }
  box.innerHTML = '';
  const group = (title, docs, sessionScoped) => {
    if (!docs?.length) return;
    const h = document.createElement('h4');
    h.textContent = `${title} · ${docs.length}`;
    box.append(h);
    for (const d of docs.slice(0, 200)) {
      box.append(docRow(d, async () => {
        const qs = sessionScoped ? `?session_id=${state.sessionId}` : '';
        await api(`/api/knowledge/docs/${d.id}${qs}`, { method: 'DELETE' }).catch(() => {});
        await refreshDocs();
        if (!sessionScoped) await refreshKnowledgeToggle();
      }));
    }
  };
  group('К этой встрече', data.session, true);
  group('Моя база', data.saved, false);
}

async function refreshKnowledgeToggle() {
  try { state.cfg = await api('/api/config'); } catch { return; }
  const kn = state.cfg.knowledge;
  const has = kn && kn.docs > 0;
  const wasHidden = el.knSwitch.hidden;
  el.knSwitch.hidden = !has;
  if (has) {
    el.knLabel.textContent = `Мои чаты · ${kn.docs}`;
    if (wasHidden) { el.kn.checked = true; store.set('kn', '1'); state.link?.send({ type: 'knowledge', enabled: true }); }
  }
}

wireDrop($('#drop'), $('#files'), $('#folder'), (items) => uploadFiles(items));

// A drop zone: click → choose files, «папку» → choose a folder, or drag files/folders onto it.
function wireDrop(zone, filesInput, folderInput, onItems) {
  filesInput.addEventListener('change', (e) => { onItems([...e.target.files]); e.target.value = ''; });
  folderInput.addEventListener('change', (e) => { onItems([...e.target.files]); e.target.value = ''; });
  zone.addEventListener('click', (e) => {
    if (e.target.closest('[data-pick=folder]')) { e.preventDefault(); folderInput.click(); return; }
    if (e.target.closest('input')) return;
    e.preventDefault();
    filesInput.click();
  });
  zone.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); filesInput.click(); } });
  ['dragenter', 'dragover'].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove('over'); }));
  zone.addEventListener('drop', async (e) => onItems(await droppedItems(e.dataTransfer)));
}

// ------------------------------------------------------------------ boot
async function boot() {
  try {
    state.cfg = await api('/api/config');
  } catch {
    return;
  }
  el.auto.checked = store.get('auto', state.cfg.auto_default ? '1' : '0') === '1';
  buildEngines();
  fillLanguageSelects();
  const kn = state.cfg.knowledge;
  if (kn && kn.docs > 0) {
    el.knSwitch.hidden = false;
    el.knLabel.textContent = `Мои чаты · ${kn.docs}`;
    el.kn.checked = store.get('kn', '1') === '1';
  } else {
    el.kn.checked = false;
  }
  setDot(el.dotBrain, state.cfg.answers ? 'ok' : 'err');
  api('/api/profile').then((p) => { $('#profile').value = p.text || ''; }).catch(() => {});
  api('/api/vocab').then((v) => { $('#vocab').value = v.text || ''; }).catch(() => {});
  if (!state.cfg.answers) {
    el.brainLabel.textContent = 'Claude недоступен';
    toast(state.cfg.answers_error || 'Ответы недоступны', { error: true, ms: 8000 });
  }
  syncSetup();
  resumeMeeting();
}

// The page was reloaded (or the tab crashed) during a call: reattach to that meeting — transcript and all
// answers come back — and offer to share the screen again to keep listening.
async function resumeMeeting() {
  const prev = store.get('copilot_meeting', '');
  if (!prev) return;
  let info;
  try { info = await api(`/api/sessions/${prev}`); } catch { store.set('copilot_meeting', ''); return; }
  if (info.ended) { store.set('copilot_meeting', ''); return; }
  state.sessionId = prev;
  state.sessionCreated = true;
  state.sessionEnded = false;
  state.resumed = true;
  state.resumeStartedAt = (info.started_at || Date.now() / 1000) * 1000;
  state.link = new Link(prev, () => ({ type: 'hello', client: 'web', engine: state.engine, auto: el.auto.checked,
    knowledge: el.kn.checked }), onEvent, onLinkState);
  [el.answerBtn, el.moreBtn, el.summary].forEach((btn) => { btn.disabled = false; });
  el.start.textContent = 'Поделиться экраном и продолжить';
  const mins = Math.max(1, Math.round((Date.now() - state.resumeStartedAt) / 60000));
  showSetupError('');
  toast(`Встреча продолжается (${mins} мин) — ответы и транскрипт на месте. Поделитесь экраном снова, чтобы слушать дальше.`, { ms: 9000 });
  refreshDocs();
}
boot();

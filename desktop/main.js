// Meeting Copilot — desktop shell (macOS + Windows).
// - main window: the same web app (your server, e.g. https://saydarai.app) with a small preload bridge
// - overlay: transparent, click-through, always-on-top text over every window / Space / fullscreen app
// - tray menu + global hotkeys: answer, «more», Auto, overlay on/off, position, size, languages, engine
'use strict';

const {
  app, BrowserWindow, Menu, Tray, desktopCapturer, globalShortcut, ipcMain, nativeImage, screen, session,
  shell, systemPreferences,
} = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const isMac = process.platform === 'darwin';
const asset = (f) => path.join(__dirname, 'assets', f);

// ------------------------------------------------------------------ settings
const SETTINGS_FILE = () => path.join(app.getPath('userData'), 'settings.json');
const DEFAULTS = {
  url: '', // full link incl. ?token=…, e.g. https://saydarai.app/?token=XXXX
  overlay: { enabled: true, position: 'center', fontSize: 28, width: 0.62, autoHideSec: 45, opacity: 0.86 },
};
let settings = loadSettings();

function loadSettings() {
  try {
    const saved = JSON.parse(fs.readFileSync(SETTINGS_FILE(), 'utf8'));
    return { ...DEFAULTS, ...saved, overlay: { ...DEFAULTS.overlay, ...(saved.overlay || {}) } };
  } catch {
    return JSON.parse(JSON.stringify(DEFAULTS));
  }
}
function saveSettings() {
  fs.mkdirSync(path.dirname(SETTINGS_FILE()), { recursive: true });
  fs.writeFileSync(SETTINGS_FILE(), JSON.stringify(settings, null, 2), { mode: 0o600 });
}
function appOrigin() {
  try { return new URL(settings.url).origin; } catch { return null; }
}

// Static lists mirror the server (/api/config); the web app stays the source of truth.
const LANGS = [['multi', 'Авто (рус + англ и др.)'], ['ru', 'Русский'], ['en', 'English'], ['es', 'Español'],
  ['de', 'Deutsch'], ['fr', 'Français'], ['pt', 'Português'], ['it', 'Italiano']];
const ANSWER_LANGS = [['auto', 'Как в вопросе'], ['ru', 'Всегда по-русски'], ['en', 'Always in English']];
const ENGINES = [['fast', 'Быстро (Haiku)'], ['smart', 'Умнее (Sonnet)'], ['deep', 'Глубоко (Opus)'], ['max', 'Максимум (Fable)']];
const HOTKEYS = {
  answer: 'CommandOrControl+Shift+Space',
  more: 'CommandOrControl+Shift+Enter',
  overlay: 'CommandOrControl+Shift+H',
  auto: 'CommandOrControl+Shift+A',
  language: 'CommandOrControl+Shift+L',
};

let mainWin = null;
let overlayWin = null;
let settingsWin = null;
let tray = null;
let quitting = false;
let hideTimer = null;
let web = { auto: false, engine: 'fast', language: 'multi', answerLanguage: 'auto', live: false };
const failedHotkeys = [];

// ------------------------------------------------------------------ single instance
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => showMain());
}

// ------------------------------------------------------------------ windows
function createMain() {
  if (mainWin) { showMain(); return; }
  mainWin = new BrowserWindow({
    width: 1440, height: 900, minWidth: 900, minHeight: 600, title: 'Meeting Copilot', backgroundColor: '#0d1321',
    icon: path.join(__dirname, 'build', 'icon.png'),
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true },
  });
  mainWin.loadURL(settings.url);
  mainWin.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: 'deny' }; });
  mainWin.on('close', (e) => {
    if (!quitting) { e.preventDefault(); mainWin.hide(); }
  });
  mainWin.on('closed', () => { mainWin = null; });
}

function showMain() {
  if (!settings.url) { openSettings(); return; }
  if (!mainWin) { createMain(); return; }
  mainWin.show();
  mainWin.focus();
}

function createOverlay() {
  overlayWin = new BrowserWindow({
    width: 800, height: 400, show: false, frame: false, transparent: true, resizable: false, movable: false,
    focusable: false, skipTaskbar: true, hasShadow: false, alwaysOnTop: true, fullscreenable: false,
    webPreferences: { preload: path.join(__dirname, 'overlay-preload.js'), contextIsolation: true, sandbox: true },
  });
  overlayWin.setAlwaysOnTop(true, 'screen-saver');
  overlayWin.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  overlayWin.setIgnoreMouseEvents(true); // click-through: never blocks the meeting app
  overlayWin.loadFile(path.join(__dirname, 'overlay.html'));
  overlayWin.webContents.on('did-finish-load', () => { placeOverlay(); pushOverlayStyle(); });
  screen.on('display-metrics-changed', placeOverlay);
}

function placeOverlay() {
  if (!overlayWin) return;
  const area = screen.getDisplayNearestPoint(screen.getCursorScreenPoint()).workArea;
  const w = Math.round(area.width * settings.overlay.width);
  const h = Math.round(area.height * 0.46);
  const x = Math.round(area.x + (area.width - w) / 2);
  const pos = settings.overlay.position;
  const y = pos === 'top' ? area.y + 24 : pos === 'bottom' ? area.y + area.height - h - 24 : Math.round(area.y + (area.height - h) / 2);
  overlayWin.setBounds({ x, y, width: w, height: h });
}

function pushOverlayStyle() {
  overlayWin?.webContents.send('style', settings.overlay);
}

function showOverlay() {
  if (!overlayWin || !settings.overlay.enabled) return;
  placeOverlay(); // follow the display where the mouse is
  if (!overlayWin.isVisible()) overlayWin.showInactive(); // never steal focus from Zoom/Meet
}

function hideOverlay() {
  clearTimeout(hideTimer);
  overlayWin?.hide();
}

function scheduleHide() {
  clearTimeout(hideTimer);
  const sec = settings.overlay.autoHideSec;
  if (sec > 0) hideTimer = setTimeout(hideOverlay, sec * 1000);
}

function openSettings() {
  if (settingsWin) { settingsWin.focus(); return; }
  settingsWin = new BrowserWindow({
    width: 560, height: 520, resizable: false, title: 'Настройки — Meeting Copilot', backgroundColor: '#0d1321',
    webPreferences: { preload: path.join(__dirname, 'settings-preload.js'), contextIsolation: true, sandbox: true },
  });
  settingsWin.setMenuBarVisibility(false);
  settingsWin.loadFile(path.join(__dirname, 'settings.html'));
  settingsWin.on('closed', () => { settingsWin = null; });
}

// ------------------------------------------------------------------ commands → web app
function command(cmd, arg) {
  if (!mainWin) { showMain(); return; }
  mainWin.webContents.send('command', cmd, arg);
}

function setOverlay(patch) {
  settings.overlay = { ...settings.overlay, ...patch };
  saveSettings();
  if (!settings.overlay.enabled) hideOverlay();
  placeOverlay();
  pushOverlayStyle();
  buildTray();
}

// ------------------------------------------------------------------ IPC (only from our own pages)
function fromApp(e) {
  const origin = appOrigin();
  try { return origin && new URL(e.senderFrame.url).origin === origin; } catch { return false; }
}

ipcMain.on('overlay-event', (e, ev) => {
  if (!fromApp(e) || !overlayWin) return;
  overlayWin.webContents.send('event', ev);
  if (ev.type === 'answer_start') { clearTimeout(hideTimer); showOverlay(); }
  if (ev.type === 'answer_end') scheduleHide();
});

ipcMain.on('web-state', (e, s) => {
  if (!fromApp(e)) return;
  web = { ...web, ...s };
  buildTray();
});

ipcMain.handle('settings:get', () => ({ url: settings.url, hotkeys: HOTKEYS, failedHotkeys, platform: process.platform }));
ipcMain.handle('settings:save', (_e, url) => {
  let u;
  try { u = new URL(String(url).trim()); } catch { return { ok: false, error: 'Это не похоже на ссылку. Пример: https://saydarai.app/?token=…' }; }
  if (!/^https?:$/.test(u.protocol)) return { ok: false, error: 'Нужна ссылка http(s)://' };
  if (u.protocol === 'http:' && !['127.0.0.1', 'localhost'].includes(u.hostname)) {
    return { ok: false, error: 'Для сервера в интернете нужна ссылка https://' };
  }
  settings.url = u.toString();
  saveSettings();
  if (mainWin) { mainWin.loadURL(settings.url); mainWin.show(); } else createMain();
  settingsWin?.close();
  return { ok: true };
});
ipcMain.on('overlay:hide', () => hideOverlay());

// ------------------------------------------------------------------ macOS system audio (ScreenCaptureKit helper)
let sysProc = null;
const FRAME_BYTES = 1280; // 640 int16 samples = 40 ms @ 16 kHz

function helperPath() {
  return app.isPackaged ? path.join(process.resourcesPath, 'syscap') : path.join(__dirname, 'build', 'bin', 'syscap');
}

function stopSysAudio() {
  if (sysProc) {
    try { sysProc.stdin.end(); sysProc.kill(); } catch { /* already gone */ }
    sysProc = null;
  }
}

ipcMain.handle('sysaudio:start', (e) => new Promise((resolve) => {
  if (!fromApp(e)) return resolve({ ok: false, error: 'forbidden' });
  if (!isMac) return resolve({ ok: false, error: 'not needed on this OS' });
  if (!fs.existsSync(helperPath())) return resolve({ ok: false, error: 'helper is missing (npm run build-native)' });
  stopSysAudio();
  const proc = spawn(helperPath(), [], { stdio: ['pipe', 'pipe', 'pipe'] });
  sysProc = proc;
  let pending = Buffer.alloc(0);
  let settled = false;
  const done = (r) => { if (!settled) { settled = true; resolve(r); } };
  proc.stdout.on('data', (chunk) => {
    pending = pending.length ? Buffer.concat([pending, chunk]) : chunk;
    while (pending.length >= FRAME_BYTES) {
      const frame = Buffer.from(pending.subarray(0, FRAME_BYTES));
      pending = pending.subarray(FRAME_BYTES);
      if (!e.sender.isDestroyed()) e.sender.send('sysaudio:data', frame);
    }
  });
  proc.stderr.on('data', (d) => {
    const text = d.toString();
    if (text.includes('ready')) done({ ok: true });
    const err = text.split('\n').find((l) => l.startsWith('error:'));
    if (err) {
      const msg = /declin|permission|TCC|not authorized/i.test(err)
        ? 'нет разрешения «Запись экрана и системного звука» — System Settings → Privacy & Security'
        : err.slice(7).trim();
      done({ ok: false, error: msg });
      if (!e.sender.isDestroyed()) e.sender.send('sysaudio:error', msg);
    }
  });
  proc.on('exit', () => { if (sysProc === proc) sysProc = null; done({ ok: false, error: 'helper exited' }); });
  setTimeout(() => done({ ok: false, error: 'helper did not start in 8 s' }), 8000);
}));

ipcMain.handle('sysaudio:stop', (e) => { if (fromApp(e)) stopSysAudio(); return true; });
app.on('will-quit', stopSysAudio);

// ------------------------------------------------------------------ tray
function buildTray() {
  if (!tray) {
    const img = nativeImage.createFromPath(asset(isMac ? 'trayTemplate.png' : 'tray.png'));
    if (isMac) img.setTemplateImage(true);
    tray = new Tray(img);
    tray.setToolTip('Meeting Copilot');
    if (!isMac) tray.on('click', showMain);
  }
  const o = settings.overlay;
  const radio = (list, current, onPick) => list.map(([id, label]) => ({
    label, type: 'radio', checked: id === current, click: () => onPick(id),
  }));
  const menu = Menu.buildFromTemplate([
    { label: 'Открыть копилот', click: showMain },
    { type: 'separator' },
    web.live
      ? { label: 'Остановить встречу', click: () => command('stop') }
      : { label: 'Начать встречу…', click: () => { showMain(); command('start'); } },
    { label: 'Ответить на последний вопрос', accelerator: HOTKEYS.answer, click: () => command('answer') },
    { label: 'Подробнее (сильная модель)', accelerator: HOTKEYS.more, click: () => command('more') },
    { label: 'Auto — отвечать самому', type: 'checkbox', checked: !!web.auto, accelerator: HOTKEYS.auto,
      click: (item) => command('auto', item.checked) },
    { type: 'separator' },
    { label: 'Текст поверх экрана', type: 'checkbox', checked: o.enabled, accelerator: HOTKEYS.overlay,
      click: (item) => setOverlay({ enabled: item.checked }) },
    { label: 'Положение текста', submenu: radio([['center', 'По центру'], ['bottom', 'Снизу'], ['top', 'Сверху']],
      o.position, (v) => setOverlay({ position: v })) },
    { label: 'Размер текста', submenu: radio([[22, 'Мелкий'], [28, 'Средний'], [34, 'Крупный'], [42, 'Огромный']],
      o.fontSize, (v) => setOverlay({ fontSize: v })) },
    { label: 'Ширина', submenu: radio([[0.45, 'Узко'], [0.62, 'Средне'], [0.8, 'Широко']],
      o.width, (v) => setOverlay({ width: v })) },
    { label: 'Прозрачность фона', submenu: radio([[0.6, 'Сильная'], [0.86, 'Средняя'], [0.97, 'Без прозрачности']],
      o.opacity, (v) => setOverlay({ opacity: v })) },
    { label: 'Скрывать текст через', submenu: radio([[20, '20 секунд'], [45, '45 секунд'], [120, '2 минуты'], [0, 'Не скрывать']],
      o.autoHideSec, (v) => setOverlay({ autoHideSec: v })) },
    { label: 'Скрыть текст сейчас', click: hideOverlay },
    { type: 'separator' },
    { label: 'Язык речи', accelerator: HOTKEYS.language, submenu: radio(LANGS, web.language, (v) => command('language', v)) },
    { label: 'Язык ответа', submenu: radio(ANSWER_LANGS, web.answerLanguage, (v) => command('answer-language', v)) },
    { label: 'Движок ответов', submenu: radio(ENGINES, web.engine, (v) => command('engine', v)) },
    { type: 'separator' },
    { label: 'Настройки…', click: openSettings },
    ...(failedHotkeys.length ? [{ label: `Заняты другим приложением: ${failedHotkeys.join(', ')}`, enabled: false }] : []),
    { label: 'Выйти', role: undefined, click: () => { quitting = true; app.quit(); } },
  ]);
  tray.setContextMenu(menu);
}

// ------------------------------------------------------------------ hotkeys
function registerHotkeys() {
  const map = {
    answer: () => command('answer'),
    more: () => command('more'),
    auto: () => command('auto'),
    language: () => command('cycle-language'),
    overlay: () => {
      if (overlayWin?.isVisible()) hideOverlay();
      else { setOverlay({ enabled: true }); showOverlay(); scheduleHide(); }
    },
  };
  for (const [name, accel] of Object.entries(HOTKEYS)) {
    try {
      if (!globalShortcut.register(accel, map[name])) failedHotkeys.push(accel);
    } catch { failedHotkeys.push(accel); }
  }
}

// ------------------------------------------------------------------ capture: «Поделиться экраном» inside the app
function setupCapture() {
  const ses = session.defaultSession;
  ses.setPermissionRequestHandler((_wc, permission, cb, details) => {
    const allowed = ['media', 'display-capture', 'clipboard-sanitized-write', 'clipboard-read', 'notifications'];
    const origin = appOrigin();
    cb(allowed.includes(permission) && (!origin || (details.requestingUrl || '').startsWith(origin)));
  });
  // The app shares the screen the mouse is on, with system audio where the OS allows it
  // (Windows: always; macOS: Electron's ScreenCaptureKit loopback). Without system audio,
  // the web app shows a hint to use the «Устройство (BlackHole)» mode.
  ses.setDisplayMediaRequestHandler(async (_request, callback) => {
    try {
      const sources = await desktopCapturer.getSources({ types: ['screen'] });
      const cursorDisplay = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
      const source = sources.find((s) => String(s.display_id) === String(cursorDisplay.id)) || sources[0];
      // Windows: Chromium loopback gives system audio. macOS: Electron can't (Windows-only API),
      // so the web app asks for our ScreenCaptureKit helper instead (sysaudio:start below).
      callback({ video: source, ...(isMac ? {} : { audio: 'loopback' }) });
    } catch (err) {
      console.error('display media failed', err);
      callback({});
    }
  });
}

// ------------------------------------------------------------------ lifecycle
app.whenReady().then(async () => {
  if (isMac) {
    app.dock?.setIcon(path.join(__dirname, 'build', 'icon.png'));
    try { await systemPreferences.askForMediaAccess('microphone'); } catch { /* user can allow later */ }
  }
  setupCapture();
  createOverlay();
  registerHotkeys();
  buildTray();
  if (settings.url) createMain(); else openSettings();
  app.on('activate', showMain);
});

app.on('before-quit', () => { quitting = true; });
app.on('will-quit', () => globalShortcut.unregisterAll());
app.on('window-all-closed', (e) => e.preventDefault()); // keep running in the tray

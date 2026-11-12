'use strict';
const pretty = (accel, mac) => accel
  .replace('CommandOrControl', mac ? '⌘' : 'Ctrl')
  .replace(/\+/g, mac ? '' : '+')
  .replace('Shift', mac ? '⇧' : 'Shift')
  .replace('Space', mac ? ' Space' : 'Space')
  .replace('Enter', mac ? ' Enter' : 'Enter');

const LABELS = {
  answer: 'Ответить на последний вопрос',
  more: 'Подробнее (сильная модель)',
  overlay: 'Показать / скрыть текст поверх экрана',
  auto: 'Auto вкл / выкл',
  language: 'Сменить язык речи (Авто → RU → EN)',
};

window.settingsBridge.get().then((s) => {
  const mac = s.platform === 'darwin';
  document.getElementById('url').value = s.url || '';
  const table = document.getElementById('keys');
  for (const [k, accel] of Object.entries(s.hotkeys)) {
    const tr = document.createElement('tr');
    const a = document.createElement('td');
    a.textContent = LABELS[k] || k;
    const b = document.createElement('td');
    b.textContent = pretty(accel, mac);
    tr.append(a, b);
    table.append(tr);
  }
  if (s.failedHotkeys.length) {
    const w = document.getElementById('warn');
    w.hidden = false;
    w.textContent = `Эти сочетания заняты другим приложением и не работают: ${s.failedHotkeys.map((x) => pretty(x, mac)).join(', ')}`;
  }
});

document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const res = await window.settingsBridge.save(document.getElementById('url').value);
  document.getElementById('err').textContent = res.ok ? '' : res.error;
});

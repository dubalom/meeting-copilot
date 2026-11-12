import { renderMarkdown } from './md.js';

const card = document.getElementById('card');
const q = document.getElementById('q');
const a = document.getElementById('a');
const engine = document.getElementById('engine');
const hint = document.getElementById('hint');
const isMac = navigator.platform.toLowerCase().includes('mac');
let current = null;
let text = '';

function render(streaming) {
  if (!text && streaming) {
    a.innerHTML = '<div class="dots"><i></i><i></i><i></i></div>';
    return;
  }
  a.innerHTML = renderMarkdown(text);
}

window.overlayBridge.onEvent((ev) => {
  if (ev.type === 'answer_start') {
    current = ev.id;
    text = '';
    engine.textContent = ev.engine_label || 'Claude';
    hint.textContent = `${isMac ? '⌘⇧H' : 'Ctrl+Shift+H'} — скрыть`;
    q.textContent = ev.question || '';
    q.hidden = !ev.question;
    render(true);
    card.classList.add('show');
  } else if (ev.type === 'answer_delta' && ev.id === current) {
    text += ev.text;
    render(true);
  } else if (ev.type === 'answer_end' && ev.id === current) {
    text = ev.text || text;
    render(false);
  }
});

window.overlayBridge.onStyle((s) => {
  document.documentElement.style.setProperty('--fs', `${s.fontSize}px`);
  document.documentElement.style.setProperty('--bg-a', String(s.opacity));
  document.body.className = `pos-${s.position}`;
});

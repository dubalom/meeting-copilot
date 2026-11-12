'use strict';
// Download page: offers the build that matches the visitor's computer, lists the rest,
// and — for the owner who already opened the copilot in this browser — their link to paste into the app.
(async () => {
  const $ = (s) => document.querySelector(s);
  const ua = navigator.userAgent;
  const isWin = /Windows/i.test(ua);
  const isMac = /Macintosh|Mac OS X/i.test(ua) && !/iPhone|iPad/i.test(ua);

  // Apple-silicon vs Intel: the WebGL renderer names "Apple M…" on M-chips; default to arm64 (most Macs since 2021)
  let macArm = true;
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
    const r = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : '';
    if (/Intel/i.test(r)) macArm = false;
  } catch { /* keep default */ }

  const os = isWin ? 'win' : 'mac';
  for (const p of document.querySelectorAll('[data-os]')) p.hidden = p.dataset.os !== os;
  const mod = isWin ? 'Ctrl+Shift+' : '⌘⇧';
  for (const k of document.querySelectorAll('kbd[data-k]')) k.textContent = mod + k.dataset.k;
  if (isWin) $('.plate-top span').textContent = 'Ctrl+Shift+H — скрыть';

  const mb = (b) => `${Math.round(b / 1048576)} МБ`;
  let builds = [];
  try {
    builds = (await (await fetch('/api/downloads')).json()).builds || [];
  } catch { /* offline: show the empty state */ }

  if (!builds.length) {
    $('#get').hidden = true;
    $('#empty').hidden = false;
  } else {
    const want = isWin ? ['win', 'win-zip'] : isMac ? (macArm ? ['mac-arm64', 'mac-x64'] : ['mac-x64', 'mac-arm64'])
      : ['mac-arm64', 'win'];
    const best = want.map((k) => builds.find((b) => b.kind === k)).find(Boolean) || builds[0];
    const primary = $('#primary');
    primary.href = best.url;
    primary.textContent = best.kind.startsWith('win') ? 'Скачать для Windows' : 'Скачать для Mac';
    $('#primary-meta').textContent = `${best.label} · ${mb(best.size)}${best.version ? ` · версия ${best.version}` : ''}`;

    const list = $('#versions');
    for (const b of builds) {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = b.url;
      if (b === best) a.setAttribute('aria-current', 'true');
      const name = document.createElement('span');
      name.textContent = b.label;
      const size = document.createElement('small');
      size.textContent = mb(b.size);
      a.append(name, size);
      li.append(a);
      list.append(li);
    }
  }

  // The owner's link: the main page keeps the token in this browser's storage (same site).
  let token = '';
  try { token = localStorage.getItem('copilot_token') || ''; } catch { /* storage blocked */ }
  if (token) {
    const link = `${location.origin}/?token=${encodeURIComponent(token)}`;
    $('#mylink').hidden = false;
    $('#mylink-note').hidden = false;
    $('#mylink-text').textContent = `${location.origin}/?token=••••••${token.slice(-4)}`;
    $('#mylink-copy').addEventListener('click', async (e) => {
      try {
        await navigator.clipboard.writeText(link);
        e.target.textContent = 'Скопировано';
      } catch {
        $('#mylink-text').textContent = link; // let them copy by hand
      }
    });
  }
})();

// Header: brand, one connect button (details in a popover), status, theme, emergency stop, dismissible banners.
import { h, fmt, lsSet, clear } from '../util.js';
import { icon, logo } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, gate, toast } from '../ui.js';
import { segmented } from '../widgets.js';
import { caps, MACHINE_LABEL, alarmText } from '../caps.js';
import { connectBody } from '../simopts.js';

const THEMES = [['auto', 'Auto', 'auto'], ['light', 'Light', 'sun'], ['dark', 'Dark', 'moon']];

export function triggerEstop() {
  toast('EMERGENCY STOP', 'error', 3000);
  api.estop().catch(() => {});
}

function currentTheme() { return document.documentElement.dataset.theme || 'auto'; }
export function setTheme(t) {
  if (t === 'auto') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  try { t === 'auto' ? localStorage.removeItem('wbot-theme') : localStorage.setItem('wbot-theme', t); } catch { /* ok */ }
  window.dispatchEvent(new Event('themechange'));
}

export function mountHeader(root) {
  /* ----- Connection options (popover) ----- */
  const transportSeg = segmented({
    label: 'Connection type', value: 'sim', fill: true,
    options: [{ value: 'sim', label: 'Simulator' }, { value: 'serial', label: 'USB' },
      { value: 'wifi', label: 'Wi-Fi', title: 'Board over Wi-Fi (Telnet/WebSocket), no cable' }],
    onChange: (v) => store.set({ transport: v }),
  });
  const SPEEDS = [1, 5, 20, 60];
  const speedSeg = segmented({ label: 'Simulator speed', value: store.get('simOpts')?.speed ?? 1, fill: true,
    options: SPEEDS.map((v) => ({ value: v, label: `${v}×`, title: v === 1 ? 'Real time' : `${v}× faster` })),
    onChange: (v) => { const o = { ...store.get('simOpts'), speed: v }; store.set({ simOpts: o }); lsSet('wbot-simopts', o); } });
  const speedWrap = h('div', { class: 'field' }, h('span', { class: 'field-label' }, 'Simulator speed'), speedSeg.el);
  const portSel = h('select', { 'aria-label': 'USB port', onChange: (e) => store.set({ port: e.target.value || null }) });
  const portRefresh = h('button', { class: 'btn icon-only', type: 'button', title: 'Refresh the port list', 'aria-label': 'Refresh the port list',
    onClick: () => run(portRefresh, async () => store.set({ ports: await api.ports() })) }, icon('refresh'));
  const portRow = h('div', { class: 'row', style: { alignItems: 'center' } }, h('div', { class: 'field', style: { flex: '1' } }, h('span', { class: 'field-label' }, 'USB port'), portSel), portRefresh);
  const wifiHost = h('input', { type: 'text', 'aria-label': 'Board host or IP', placeholder: 'fluidnc.local', autocomplete: 'off', spellcheck: 'false', maxlength: '120',
    value: store.get('wifi')?.host ?? 'fluidnc.local', title: 'Host name or IP of the board, optionally with :port. No password needed.' });
  const wifiProto = h('select', { 'aria-label': 'Wi-Fi protocol', title: 'Telnet (port 23) or WebSocket (port 81)' },
    h('option', { value: 'telnet' }, 'Telnet'), h('option', { value: 'websocket' }, 'WebSocket'));
  wifiProto.value = store.get('wifi')?.protocol === 'websocket' ? 'websocket' : 'telnet';
  const saveWifi = () => { const w = { host: wifiHost.value.trim(), protocol: wifiProto.value }; store.set({ wifi: w }); lsSet('wbot-wifi', w); };
  wifiHost.addEventListener('input', saveWifi);
  wifiProto.addEventListener('change', saveWifi);
  wifiHost.addEventListener('keydown', (e) => { if (e.key === 'Enter') connBtn.click(); e.stopPropagation(); });
  const wifiRow = h('div', { class: 'row', style: { alignItems: 'flex-end' } }, h('div', { class: 'field', style: { flex: '2 1 150px' } }, h('span', { class: 'field-label' }, 'Board address'), wifiHost),
    h('div', { class: 'field', style: { flex: '1 1 100px' } }, h('span', { class: 'field-label' }, 'Protocol'), wifiProto));
  const connNote = h('p', { class: 'field-hint' });
  const pop = h('div', { class: 'conn-pop', hidden: true, role: 'dialog', 'aria-label': 'Connection options' }, transportSeg.el, portRow, wifiRow, speedWrap, connNote);

  const connBtn = h('button', { class: 'btn primary', type: 'button', style: { minWidth: '118px' } });
  const optBtn = h('button', { class: 'btn icon-only', type: 'button', title: 'Connection options', 'aria-label': 'Connection options', 'aria-expanded': 'false',
    onClick: () => { pop.hidden = !pop.hidden; optBtn.setAttribute('aria-expanded', String(!pop.hidden)); } }, icon('gear'));
  document.addEventListener('pointerdown', (e) => { if (!pop.hidden && !pop.contains(e.target) && !optBtn.contains(e.target)) { pop.hidden = true; optBtn.setAttribute('aria-expanded', 'false'); } });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !pop.hidden) { pop.hidden = true; optBtn.setAttribute('aria-expanded', 'false'); } });

  connBtn.addEventListener('click', () => run(connBtn, async () => {
    const st = store.get('state');
    if (st?.connection?.status === 'connected') { await api.disconnect(); toast('Disconnected.', 'info'); return; }
    pop.hidden = true;
    await api.connect(connectBody());
    toast('Connected.', 'ok');
  }));

  const themeBtn = h('button', { class: 'btn icon-only theme-btn', type: 'button', title: 'Switch theme' });
  const paintTheme = () => {
    const cur = THEMES.find((t) => t[0] === currentTheme()) || THEMES[0];
    clear(themeBtn).append(icon(cur[2]));
    themeBtn.setAttribute('aria-label', `Theme: ${cur[1]}`); themeBtn.title = `Theme: ${cur[1]} (click to switch)`;
  };
  themeBtn.addEventListener('click', () => {
    const i = THEMES.findIndex((t) => t[0] === currentTheme());
    setTheme(THEMES[(i + 1) % THEMES.length][0]);
    paintTheme();
  });
  paintTheme();

  /* ----- Status ----- */
  const badge = h('span', { class: 'state-badge', 'data-state': 'disconnected', 'aria-live': 'polite' });
  const info = h('span', { class: 'muted conn-info', style: { fontSize: '13px' } });
  const pos = h('span', { class: 'chip pos-chip', title: 'Gondola position on the board' });
  const wsChip = h('span', { class: 'chip' });

  const r1 = h('div', { class: 'top-r1' },
    h('div', { class: 'brand' }, logo(), h('span', null, 'W-BOT')),
    h('div', { class: 'conn' }, connBtn, optBtn, pop),
    badge, info, pos, wsChip,
    h('span', { class: 'top-sp' }), themeBtn);

  const estop = h('button', { class: 'estop', type: 'button', 'aria-label': 'Emergency stop (space bar)', title: 'Emergency stop. Shortcut: space bar',
    onClick: triggerEstop }, 'STOP', h('small', null, 'Space'));
  const es = h('div', { class: 'top-es' }, estop);
  const banners = h('div', { class: 'top-bn', role: 'region', 'aria-label': 'Notices' });
  root.append(r1, es, banners);

  /* ----- Banners: accurate, short, dismissible (the alarm one stays until fixed) ----- */
  const dismissed = new Set();
  function bannerRow(key, cls, text, ...actions) {
    const x = cls === 'alarm' ? null : h('button', { class: 'banner-x', type: 'button', 'aria-label': 'Dismiss', title: 'Dismiss', onClick: () => { dismissed.add(key); update(); } }, '×');
    return h('div', { class: 'banner ' + cls, role: cls === 'alarm' ? 'alert' : undefined }, h('span', { class: 'grow' }, text), ...actions, x);
  }

  function update() {
    const s = store.get('state'), cfg = store.get('config'), job = store.get('job');
    const c = caps(s, job), conn = s?.connection;
    const connected = c.connected, connecting = conn?.status === 'connecting';

    const transport = connected ? conn.transport : (store.get('transport') || cfg?.connection?.transport || 'sim');
    transportSeg.set(transport);
    transportSeg.buttons.forEach((b) => { b.disabled = connected || connecting; });
    const ports = store.get('ports') || [];
    const want = store.get('port') || cfg?.connection?.serial?.port || '';
    if (portSel._sig !== JSON.stringify([ports, want])) {
      portSel._sig = JSON.stringify([ports, want]);
      clear(portSel).append(h('option', { value: '' }, ports.length ? 'Choose a port …' : 'No ports found'),
        ...ports.map((p) => h('option', { value: p.device }, p.description ? `${p.device} – ${p.description}` : p.device)));
      portSel.value = ports.some((p) => p.device === want) ? want : '';
    }
    portRow.hidden = transport !== 'serial';
    speedWrap.hidden = transport !== 'sim';
    wifiRow.hidden = transport !== 'wifi';
    wifiHost.disabled = wifiProto.disabled = portSel.disabled = connected || connecting;
    speedSeg.set(store.get('simOpts')?.speed ?? 1);
    speedSeg.buttons.forEach((b) => { b.disabled = connected || connecting; });
    gate(portRefresh, connected ? 'Disconnect first.' : '');
    connNote.textContent = connected ? 'Disconnect to change the connection.' : 'The simulator is the safe default. Real hardware is only opened when you connect to it.';

    const label = transport === 'sim' ? 'Simulator' : transport === 'wifi' ? 'Wi-Fi' : 'USB';
    connBtn.textContent = connected ? 'Disconnect' : (connecting ? 'Connecting …' : `Connect · ${label}`);
    connBtn.classList.toggle('primary', !connected);
    gate(connBtn, !connected && transport === 'serial' && !portSel.value ? 'Choose a USB port first (the gear button).'
      : !connected && transport === 'wifi' && !wifiHost.value.trim() ? 'Enter the board address first (the gear button).'
      : (connecting ? 'Connecting …' : ''));

    const ms = s?.machine?.state ?? 'disconnected';
    badge.dataset.state = ms;
    badge.textContent = MACHINE_LABEL[ms] || ms;
    info.textContent = connected ? `${conn.transport === 'sim' ? 'Simulator' : conn.transport === 'wifi' ? (conn.host || conn.port || 'Wi-Fi') : (conn.port || 'USB')}${conn.firmware ? ' · ' + conn.firmware : ''}` : '';
    const p = connected && s.reference?.set ? s.position?.physical : null;
    pos.hidden = !connected;
    pos.className = 'chip pos-chip ' + (p ? '' : 'warn');
    pos.textContent = p ? `X ${fmt(p[0], 0)} · Y ${fmt(p[1], 0)}` : 'Position unknown';
    pos.title = p ? 'Gondola position on the board (mm)' : 'Set the gondola position in Setup or Move';

    const ws = store.get('ws');
    wsChip.hidden = ws === 'open';
    wsChip.className = 'chip danger';
    wsChip.textContent = ws === 'connecting' ? 'Connecting to the server …' : 'Server not reachable';

    const items = [];
    if (ms === 'alarm') items.push(['alarm', 'alarm', alarmText(s.machine.alarm) || 'The machine is in alarm.']);
    if (conn?.status === 'error' && conn.error) items.push(['conn:' + conn.error, 'err', conn.error]);
    const r = s?.reference;
    if (connected && r?.uncertain) items.push(['ref-uncertain', 'warn', 'The gondola may no longer be where the software thinks. Set the position again if in doubt.']);
    for (const w of s?.warnings || []) items.push(['w:' + w, 'warn', w]);
    const shown = items.filter(([k]) => !dismissed.has(k));
    const key = JSON.stringify(shown);
    if (banners._key !== key) {
      banners._key = key;
      clear(banners);
      for (const [k, cls, text] of shown) {
        banners.append(cls === 'alarm' ? bannerRow(k, 'alarm', text,
          h('button', { class: 'btn sm', type: 'button', onClick: (e) => run(e.currentTarget, async () => { await api.unlock(); toast('Unlocked.', 'ok'); }) }, icon('unlock'), 'Unlock'),
          h('button', { class: 'btn sm', type: 'button', onClick: (e) => run(e.currentTarget, async () => { await api.reset(); toast('Controller reset.', 'info'); }) }, icon('refresh'), 'Reset'))
          : bannerRow(k, cls, text));
      }
    }
  }

  store.on(['state', 'config', 'ws', 'ports', 'transport', 'port', 'job', 'simOpts', 'wifi'], update, { immediate: true });
  window.addEventListener('themechange', paintTheme);
}

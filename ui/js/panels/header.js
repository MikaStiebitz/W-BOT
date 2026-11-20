// Kopfzeile: Verbindung, Zustand, Positionen, Not-Halt, Alarm-Banner, Theme.
import { h, fmt, lsSet, clear } from '../util.js';
import { icon, logo } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, gate, toast } from '../ui.js';
import { segmented } from '../widgets.js';
import { caps, MACHINE_LABEL, alarmText } from '../caps.js';
import { connectBody } from '../simopts.js';

const THEMES = [['auto', 'Auto', 'auto'], ['light', 'Hell', 'sun'], ['dark', 'Dunkel', 'moon']];

export function triggerEstop() {
  toast('NOT-HALT ausgelöst', 'error', 3000);
  api.estop().catch(() => {});
}

function currentTheme() { return document.documentElement.dataset.theme || 'auto'; }
export function setTheme(t) {
  if (t === 'auto') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  try { t === 'auto' ? localStorage.removeItem('wbot-theme') : localStorage.setItem('wbot-theme', t); } catch { /* ok */ }
  window.dispatchEvent(new Event('themechange'));
}

export function mountHeader(root) {
  /* ----- Zeile 1: Marke, Verbindung, Theme ----- */
  const transportSeg = segmented({
    label: 'Verbindungsart', value: 'sim',
    options: [{ value: 'sim', label: 'Simulator' }, { value: 'serial', label: 'Seriell' },
      { value: 'wifi', label: 'WLAN', title: 'Board über WLAN (Telnet/WebSocket) – ohne Kabel' }],
    onChange: (v) => store.set({ transport: v }),
  });
  const SPEEDS = [1, 5, 20, 60];
  const speedSeg = segmented({ label: 'Simulator-Tempo', value: store.get('simOpts')?.speed ?? 1,
    options: SPEEDS.map((v) => ({ value: v, label: `${v}×`, title: v === 1 ? 'Echtzeit' : `${v}-fach beschleunigt` })),
    onChange: (v) => { const o = { ...store.get('simOpts'), speed: v }; store.set({ simOpts: o }); lsSet('wbot-simopts', o); } });
  const speedWrap = h('div', { class: 'conn-speed' }, h('span', { class: 'field-label' }, 'Tempo'), speedSeg.el);
  const portSel = h('select', { 'aria-label': 'Serieller Port', onChange: (e) => store.set({ port: e.target.value || null }) });
  const portRefresh = h('button', { class: 'btn icon-only', type: 'button', title: 'Portliste aktualisieren', 'aria-label': 'Portliste aktualisieren',
    onClick: () => run(portRefresh, async () => store.set({ ports: await api.ports() })) }, icon('refresh'));
  const wifiHost = h('input', { type: 'text', class: 'conn-host', 'aria-label': 'Host oder IP des Boards', placeholder: 'fluidnc.local',
    autocomplete: 'off', spellcheck: 'false', maxlength: '120', value: store.get('wifi')?.host ?? 'fluidnc.local',
    title: 'Hostname oder IP-Adresse des Boards, optional mit :Port (kein Passwort nötig – FluidNC-Telnet/WebSocket haben keine Anmeldung)' });
  const wifiProto = h('select', { 'aria-label': 'WLAN-Protokoll', title: 'Telnet (Port 23) oder WebSocket (Port 81)' },
    h('option', { value: 'telnet' }, 'Telnet'), h('option', { value: 'websocket' }, 'WebSocket'));
  wifiProto.value = store.get('wifi')?.protocol === 'websocket' ? 'websocket' : 'telnet';
  const saveWifi = () => { const w = { host: wifiHost.value.trim(), protocol: wifiProto.value }; store.set({ wifi: w }); lsSet('wbot-wifi', w); };
  wifiHost.addEventListener('input', saveWifi);
  wifiProto.addEventListener('change', saveWifi);
  wifiHost.addEventListener('keydown', (e) => { if (e.key === 'Enter') connBtn.click(); e.stopPropagation(); });
  const connBtn = h('button', { class: 'btn primary', type: 'button', style: { minWidth: '130px' } });
  const connInfo = h('span', { class: 'chip conn-info' });

  connBtn.addEventListener('click', () => run(connBtn, async () => {
    const st = store.get('state');
    if (st?.connection?.status === 'connected') { await api.disconnect(); toast('Getrennt.', 'info'); return; }
    await api.connect(connectBody());
    toast('Verbunden.', 'ok');
  }));

  const themeBtn = h('button', { class: 'btn sm theme-btn', type: 'button', title: 'Farbschema wechseln' });
  const paintTheme = () => {
    const cur = THEMES.find((t) => t[0] === currentTheme()) || THEMES[0];
    clear(themeBtn).append(icon(cur[2]), h('span', { class: 'lbl' }, cur[1]));
    themeBtn.setAttribute('aria-label', `Farbschema: ${cur[1]}`);
  };
  themeBtn.addEventListener('click', () => {
    const i = THEMES.findIndex((t) => t[0] === currentTheme());
    setTheme(THEMES[(i + 1) % THEMES.length][0]);
    paintTheme();
  });
  paintTheme();

  const r1 = h('div', { class: 'top-r1' },
    h('div', { class: 'brand' }, logo(), h('span', null, 'W-BOT'), h('small', null, 'Wandplotter')),
    h('div', { class: 'conn' }, transportSeg.el, portSel, portRefresh, wifiHost, wifiProto, speedWrap, connBtn, connInfo),
    h('span', { class: 'top-sp' }),
    themeBtn);

  /* ----- Zeile 2: Anzeigen ----- */
  const badge = h('span', { class: 'state-badge', 'data-state': 'disconnected', 'aria-live': 'polite' });
  const ro = (label) => { const v = h('span', { class: 'ro-v' }); return { v, el: h('div', { class: 'ro' }, h('span', { class: 'ro-l' }, label), v) }; };
  const phys = ro('Physisch (mm)'), fw = ro('Firmware (mm)'), pen = ro('Stift'), rev = ro('Revolver');
  const refChip = h('span', { class: 'chip' });
  const wsChip = h('span', { class: 'chip' });
  const r2 = h('div', { class: 'top-r2' }, badge, phys.el, fw.el, pen.el, rev.el, refChip, h('span', { class: 'top-sp' }), wsChip);

  /* ----- Not-Halt ----- */
  const estop = h('button', { class: 'estop', type: 'button', 'aria-label': 'Not-Halt (Leertaste)', title: 'Not-Halt – Taste: Leertaste',
    onClick: triggerEstop }, 'NOT-', h('br'), 'HALT', h('small', null, 'Leertaste'));
  const es = h('div', { class: 'top-es' }, estop);

  const banners = h('div', { class: 'top-bn', role: 'region', 'aria-label': 'Hinweise' });
  root.append(r1, r2, es, banners);

  function xy(el, p, dec) {
    clear(el.v);
    if (!p) { el.v.className = 'ro-v dim'; el.v.textContent = 'unbekannt'; return; }
    el.v.className = 'ro-v';
    el.v.append(h('span', { class: 'ax' }, 'X'), fmt(p[0], dec), '  ', h('span', { class: 'ax' }, 'Y'), fmt(p[1], dec));
  }

  function bannerRow(cls, text, ...actions) {
    return h('div', { class: 'banner ' + cls, role: cls === 'alarm' ? 'alert' : undefined }, h('span', { class: 'grow' }, text), ...actions);
  }

  function update() {
    const s = store.get('state'), cfg = store.get('config'), job = store.get('job');
    const c = caps(s, job);
    const conn = s?.connection;
    const connected = c.connected, connecting = conn?.status === 'connecting';

    const transport = connected ? conn.transport : (store.get('transport') || cfg?.connection?.transport || 'sim');
    transportSeg.set(transport);
    transportSeg.buttons.forEach((b) => { b.disabled = connected || connecting; });
    const ports = store.get('ports') || [];
    const want = store.get('port') || cfg?.connection?.serial?.port || '';
    if (portSel._sig !== JSON.stringify([ports, want])) {
      portSel._sig = JSON.stringify([ports, want]);
      clear(portSel).append(h('option', { value: '' }, ports.length ? 'Port wählen …' : 'Keine Ports gefunden'),
        ...ports.map((p) => h('option', { value: p.device }, p.description ? `${p.device} – ${p.description}` : p.device)));
      portSel.value = ports.some((p) => p.device === want) ? want : '';
    }
    portSel.hidden = portRefresh.hidden = transport !== 'serial';
    speedWrap.hidden = transport !== 'sim';
    wifiHost.hidden = wifiProto.hidden = transport !== 'wifi';
    wifiHost.disabled = wifiProto.disabled = connected || connecting;
    speedSeg.set(store.get('simOpts')?.speed ?? 1);
    speedSeg.buttons.forEach((b) => { b.disabled = connected || connecting; });
    portSel.disabled = connected || connecting;
    gate(portRefresh, connected ? 'Erst trennen.' : '');

    connBtn.textContent = connected ? 'Trennen' : (connecting ? 'Verbinde …' : 'Verbinden');
    connBtn.classList.toggle('primary', !connected);
    gate(connBtn, !connected && transport === 'serial' && !portSel.value ? 'Erst einen Port wählen.'
      : !connected && transport === 'wifi' && !wifiHost.value.trim() ? 'Erst Host oder IP des Boards eintragen.'
      : (connecting ? 'Verbindung wird aufgebaut …' : ''));
    connInfo.hidden = !connected;
    connInfo.textContent = connected ? `${conn.transport === 'sim' ? 'Simulator' : conn.transport === 'wifi' ? 'WLAN · ' + (conn.host || conn.port || '') : conn.port || 'seriell'}${conn.firmware ? ' · ' + conn.firmware : ''}` : '';
    connInfo.className = 'chip conn-info ' + (connected ? 'ok' : '');

    const ms = s?.machine?.state ?? 'disconnected';
    badge.dataset.state = ms;
    badge.textContent = MACHINE_LABEL[ms] || ms;
    xy(phys, connected && s.reference?.set ? s.position?.physical : null, 1);
    if (connected && !s.reference?.set) phys.v.textContent = 'keine Referenz';
    xy(fw, connected ? s.position?.firmware : null, 2);
    if (!connected) { phys.v.textContent = '–'; fw.v.textContent = '–'; }

    if (connected && s.pen && s.pen.known === false) pen.v.textContent = 'unbekannt';
    else if (connected && s.pen) pen.v.textContent = `${s.pen.up ? 'oben' : 'unten'} · ${fmt(s.pen.angle, 0)}°`; else pen.v.textContent = '–';
    if (connected && s.revolver) {
      const slot = s.revolver.slot, nm = slot ? cfg?.colors?.[slot - 1]?.name : null;
      rev.v.textContent = slot ? `Slot ${slot}${nm ? ' · ' + nm : ''}` : `zwischen Slots · ${fmt(s.revolver.angle, 0)}°`;
    } else rev.v.textContent = '–';
    pen.v.className = 'ro-v' + (connected && s.pen && s.pen.known !== false && !s.pen.up ? '' : ' dim');
    rev.v.className = 'ro-v' + (connected ? '' : ' dim');

    refChip.hidden = !connected;
    const r = s?.reference;
    refChip.className = 'chip ' + (r?.uncertain || r?.restored ? 'warn' : r?.set ? 'ok' : 'warn');
    refChip.textContent = r?.uncertain ? 'Referenz unsicher' : r?.restored ? 'Referenz wiederhergestellt' : r?.set ? 'Referenz gesetzt' : 'Keine Referenz';

    const ws = store.get('ws');
    wsChip.hidden = ws === 'open';
    wsChip.className = 'chip ' + (ws === 'open' ? 'ok' : 'danger');
    wsChip.textContent = ws === 'open' ? 'Live' : ws === 'connecting' ? 'Verbinde zum Server …' : 'Server getrennt – verbinde neu …';

    /* Banner (nur neu aufbauen, wenn sich der Inhalt ändert - sonst gehen Klicks verloren) */
    const items = [];
    if (ms === 'alarm') items.push(['alarm', alarmText(s.machine.alarm) || 'Alarm aktiv.']);
    if (conn?.status === 'error' && conn.error) items.push(['err', conn.error]);
    if (ws !== 'open' && s) items.push(['err', 'Live-Verbindung zum Server unterbrochen. Angezeigte Werte können veraltet sein. Die Seite verbindet automatisch neu.']);
    const warns = s?.warnings || [];
    if (connected && r?.uncertain && !warns.length) items.push(['warn', 'Referenz unsicher: Die Gondel steht eventuell nicht mehr an der erwarteten Stelle. Im Zweifel neu referenzieren.']);
    else if (connected && r?.restored && !r?.uncertain) items.push(['warn', 'Die Referenz stammt aus der letzten Sitzung. Prüfen, ob die Gondel wirklich dort steht, sonst neu referenzieren.']);
    for (const w of s?.warnings || []) items.push(['warn', w]);
    const key = JSON.stringify(items);
    if (banners._key !== key) {
      banners._key = key;
      clear(banners);
      for (const [cls, text] of items) {
        banners.append(cls === 'alarm' ? bannerRow('alarm', text,
          h('button', { class: 'btn sm', type: 'button', onClick: (e) => run(e.currentTarget, async () => { await api.unlock(); toast('Entsperrt ($X).', 'ok'); }) }, icon('unlock'), 'Entsperren ($X)'),
          h('button', { class: 'btn sm', type: 'button', onClick: (e) => run(e.currentTarget, async () => { await api.reset(); toast('Zurückgesetzt.', 'info'); }) }, icon('refresh'), 'Reset'))
          : bannerRow(cls, text));
      }
    }
  }

  store.on(['state', 'config', 'ws', 'ports', 'transport', 'port', 'job', 'simOpts', 'wifi'], update, { immediate: true });
  window.addEventListener('themechange', paintTheme);
}

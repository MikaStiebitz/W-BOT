// Minimal stroke icons (24x24, currentColor). No emoji.
const NS = 'http://www.w3.org/2000/svg';
const P = {
  up: 'M12 19V5M5 12l7-7 7 7',
  down: 'M12 5v14M5 12l7 7 7-7',
  left: 'M19 12H5M12 5l-7 7 7 7',
  right: 'M5 12h14M12 5l7 7-7 7',
  plug: 'M9 2v6M15 2v6M7 8h10v4a5 5 0 0 1-10 0V8zM12 17v5',
  unplug: 'M4 4l16 16M9 2v4M15 2v4M7 8h6M17 10v2a5 5 0 0 1-3 4.5M12 17v5',
  sun: 'M12 4V2M12 22v-2M4 12H2M22 12h-2M5.6 5.6 4.2 4.2M19.8 19.8l-1.4-1.4M18.4 5.6l1.4-1.4M4.2 19.8l1.4-1.4M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z',
  moon: 'M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z',
  auto: 'M12 3a9 9 0 1 0 0 18V3z',
  refresh: 'M20 12a8 8 0 1 1-2.4-5.7M20 4v5h-5',
  copy: 'M9 9h11v11H9zM5 15V5h10',
  trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  download: 'M12 3v12M7 10l5 5 5-5M4 20h16',
  upload: 'M12 17V5M7 10l5-5 5 5M4 20h16',
  play: 'M7 4l13 8-13 8z',
  pause: 'M8 4v16M16 4v16',
  home: 'M3 11l9-8 9 8M5 10v10h14V10',
  pen: 'M4 20l1-5L16 4l4 4L9 19zM14 6l4 4',
  target: 'M12 3v4M12 17v4M3 12h4M17 12h4M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z',
  unlock: 'M7 11V8a5 5 0 0 1 9.6-2M5 11h14v10H5z',
  x: 'M6 6l12 12M18 6L6 18',
  stop: 'M6 6h12v12H6z',
  fit: 'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5',
  plus: 'M12 5v14M5 12h14',
  minus: 'M5 12h14',
  send: 'M4 12l16-8-6 16-3-7z',
  save: 'M5 4h12l3 3v13H5zM8 4v5h8V4M8 20v-6h8v6',
  logo: 'M3 5l9 12 9-12M12 17v0',
  camera: 'M4 8h3l2-3h6l2 3h3v11H4zM12 10a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7z',
  gear: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z',
  print: 'M7 9V3h10v6M7 17H4v-8h16v8h-3M7 14h10v7H7z',
};

export function icon(name, cls = 'icon') {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', cls);
  const path = document.createElementNS(NS, 'path');
  path.setAttribute('d', P[name] || '');
  svg.append(path);
  return svg;
}

/** Logo: V-shaped belts and gondola. */
export function logo() {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 32 32');
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML = '<path d="M3 5 L16 21 M29 5 L16 21" stroke="currentColor" stroke-width="2.4" fill="none" stroke-linecap="round"/><circle cx="3" cy="5" r="2.4" fill="currentColor"/><circle cx="29" cy="5" r="2.4" fill="currentColor"/><rect x="12" y="19" width="8" height="8" rx="2" fill="currentColor"/>';
  return svg;
}

// Keeps the accent fill of every range slider in sync: sets --p (0-100 %) on the input.
// Listens to user input, picks up sliders as they are added, and sweeps for values set from code.
const fill = (el) => {
  const min = parseFloat(el.min || '0'), max = parseFloat(el.max || '100'), v = parseFloat(el.value);
  const p = max > min ? Math.min(100, Math.max(0, ((v - min) / (max - min)) * 100)) : 0;
  const s = `${p.toFixed(1)}%`;
  if (el.style.getPropertyValue('--p') !== s) el.style.setProperty('--p', s);
};
const all = (root = document) => root.querySelectorAll('input[type="range"]').forEach(fill);

export function initSliders() {
  document.addEventListener('input', (e) => { if (e.target?.type === 'range') fill(e.target); }, true);
  document.addEventListener('change', (e) => { if (e.target?.type === 'range') fill(e.target); }, true);
  new MutationObserver((muts) => {
    for (const m of muts) for (const n of m.addedNodes) {
      if (n.nodeType !== 1) continue;
      if (n.matches?.('input[type="range"]')) fill(n); else all(n);
    }
  }).observe(document.body, { childList: true, subtree: true });
  setInterval(() => { if (!document.hidden) all(); }, 500);      // values set from code (e.g. after a reset)
  all();
}

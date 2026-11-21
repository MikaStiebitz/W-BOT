// Browser-side persistence for the current drawing, so a page reload (or a closed tab) does not lose it.
// Small things live in localStorage (util.js lsGet/lsSet); files such as the imported image go to IndexedDB.
// Everything fails soft: if the browser blocks storage the app simply starts empty.
const DB = 'wbot', STORE = 'kv';
let opened = null;

function db() {
  opened ??= new Promise((resolve, reject) => {
    try {
      const req = indexedDB.open(DB, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(STORE);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    } catch (e) { reject(e); }
  });
  return opened;
}

async function tx(mode, fn) {
  const d = await db();
  return new Promise((resolve, reject) => {
    const t = d.transaction(STORE, mode), req = fn(t.objectStore(STORE));
    t.oncomplete = () => resolve(req?.result);
    t.onerror = t.onabort = () => reject(t.error);
  });
}

/** Reads a value (a File, Blob or plain object) or undefined. */
export async function kvGet(key) { try { return await tx('readonly', (s) => s.get(key)); } catch { return undefined; } }
export async function kvSet(key, value) { try { await tx('readwrite', (s) => s.put(value, key)); return true; } catch { return false; } }
export async function kvDel(key) { try { await tx('readwrite', (s) => s.delete(key)); } catch { /* ignore */ } }

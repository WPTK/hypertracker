/* Browser identity and per-trip manage tokens.

   This browser quietly remembers the name/uid it posts under, and the secret
   token for each trip it created, so a person can edit or remove their own
   trips later without logging in. Nothing here talks to the network.

   Storage keys stay 'hft.identity' and 'hft.manage' for backward compatibility.
   'hft.manage' used to hold an object map {tripId: token}; it now holds an
   array of {tripId, token, at} so insertion order is explicit. The old shape is
   read and migrated on first use. */

const ID_KEY = 'hft.identity';
const MANAGE_KEY = 'hft.manage';

const TRIP_RE = /^\d+$/;
const TOKEN_RE = /^[A-Za-z0-9_-]{16,128}$/;
/* A freshly stored token is not pruned for this long, so a poll that started
   before the save finished cannot throw away the token we just received. */
const PRUNE_GRACE_MS = 120000;

/* ---------- storage with in-memory fallback ---------- */
const memory = new Map();
let manageCache = null;
let lastLive = null; // Set<string> of trip ids seen on the last successful board load

function storeGet(key) {
  try {
    const v = window.localStorage.getItem(key);
    return v;
  } catch (e) {
    return memory.has(key) ? memory.get(key) : null;
  }
}

function storeSet(key, value) {
  memory.set(key, value);
  try {
    window.localStorage.setItem(key, value);
  } catch (e) { /* private mode or blocked: the in-memory copy still works */ }
}

function parseJson(raw, fallback) {
  if (raw == null || raw === '') return fallback;
  try { return JSON.parse(raw); } catch (e) { return fallback; }
}

try {
  window.addEventListener('storage', (e) => {
    if (e.key === MANAGE_KEY || e.key === null) manageCache = null;
  });
} catch (e) { /* no window (not in a browser) */ }

/* ---------- identity ---------- */
export function identity() {
  const v = parseJson(storeGet(ID_KEY), null);
  if (!v || typeof v !== 'object') return { uid: null, name: '', secret: null };
  const uid = typeof v.uid === 'string' && v.uid ? v.uid : null;
  return {
    uid,
    name: typeof v.name === 'string' ? v.name : '',
    /* old records have no secret */
    secret: uid && typeof v.secret === 'string' && TOKEN_RE.test(v.secret) ? v.secret : null,
  };
}

/* `secret` is the server-issued identity secret. Omit it to keep the one
   already stored for the same uid (the server only sends it once). */
export function saveIdentity(uid, name, secret) {
  const prev = identity();
  const keep = secret === undefined && uid && prev.uid === uid ? prev.secret : null;
  const sec = typeof secret === 'string' && TOKEN_RE.test(secret) ? secret : keep;
  const rec = { uid: uid || null, name: name || '' };
  if (sec) rec.secret = sec;
  storeSet(ID_KEY, JSON.stringify(rec));
}

/* ---------- manage tokens ---------- */
function validEntry(e) {
  return e && typeof e === 'object' && TRIP_RE.test(String(e.tripId)) &&
    typeof e.token === 'string' && TOKEN_RE.test(e.token);
}

function loadManage() {
  if (manageCache) return manageCache;
  const raw = parseJson(storeGet(MANAGE_KEY), null);
  let entries = [];
  let migrate = false;
  if (Array.isArray(raw)) {
    entries = raw.filter(validEntry).map((e, i) => ({
      tripId: String(e.tripId),
      token: e.token,
      at: Number.isFinite(e.at) ? e.at : i + 1,
    }));
  } else if (raw && typeof raw === 'object') {
    /* legacy {tripId: token}: object key order is the best order we have */
    migrate = true;
    Object.keys(raw).forEach((k, i) => {
      const e = { tripId: k, token: raw[k], at: i + 1 };
      if (validEntry(e)) entries.push({ tripId: k, token: raw[k], at: i + 1 });
    });
  }
  manageCache = entries;
  if (migrate) saveManage();
  return manageCache;
}

function saveManage() {
  storeSet(MANAGE_KEY, JSON.stringify(manageCache || []));
}

function nextStamp(entries) {
  const top = entries.reduce((m, e) => Math.max(m, e.at), 0);
  return Math.max(Date.now(), top + 1);
}

export function manageToken(tripId) {
  const id = String(tripId);
  const hit = loadManage().find((e) => e.tripId === id);
  return hit ? hit.token : null;
}

export function rememberManage(tripId, token) {
  const id = String(tripId);
  if (!TRIP_RE.test(id) || typeof token !== 'string' || !TOKEN_RE.test(token)) return false;
  const entries = loadManage().filter((e) => e.tripId !== id);
  entries.push({ tripId: id, token, at: nextStamp(entries) });
  manageCache = entries;
  saveManage();
  return true;
}

export function forgetManage(tripId) {
  const id = String(tripId);
  const entries = loadManage();
  const kept = entries.filter((e) => e.tripId !== id);
  if (kept.length !== entries.length) {
    manageCache = kept;
    saveManage();
  }
}

/* Drop tokens for trips the server no longer lists. Call only after a
   successful board load. Returns how many were dropped. */
export function pruneManage(liveTripIds) {
  const live = new Set(Array.from(liveTripIds || [], String));
  lastLive = live;
  const now = Date.now();
  const entries = loadManage();
  const kept = entries.filter((e) => live.has(e.tripId) || now - e.at < PRUNE_GRACE_MS);
  const dropped = entries.length - kept.length;
  if (dropped) {
    manageCache = kept;
    saveManage();
  }
  return dropped;
}

export function canManage(ownerId, tripId, ctx) {
  const { me = null, isAdmin = false } = ctx || {};
  return !!isAdmin || (!!me && me === ownerId) || !!manageToken(tripId);
}

/* Proof of identity for reusing a uid: the identity secret when this browser
   has one (it outlives every trip). Otherwise fall back to a manage token from
   a trip this browser created, which the server accepts once for an identity
   that predates secrets: prefer one whose trip is still live, else the most
   recently stored. */
export function proofToken() {
  const secret = identity().secret;
  if (secret) return secret;
  const entries = loadManage();
  if (!entries.length) return null;
  const pool = lastLive ? entries.filter((e) => lastLive.has(e.tripId)) : [];
  const from = pool.length ? pool : entries;
  return from.reduce((best, e) => (e.at >= best.at ? e : best)).token;
}

/* ---------- manage links ---------- */
function parseManage(value) {
  const dot = String(value || '').indexOf('.');
  if (dot <= 0) return null;
  const tripId = value.slice(0, dot);
  const token = value.slice(dot + 1);
  if (!TRIP_RE.test(tripId) || !TOKEN_RE.test(token)) return null;
  return { tripId, token };
}

/* Reads `#manage=<trip>.<token>` (a fragment never reaches a server or its
   logs). The legacy `?manage=` query form is accepted once for old links. The
   credential is stripped from the address bar either way. */
export function seedFromFragment() {
  let found = null;
  let strip = false;
  let search = window.location.search;
  const hash = window.location.hash;

  if (/^#manage=/.test(hash)) {
    strip = true;
    let value = hash.slice('#manage='.length);
    try { value = decodeURIComponent(value); } catch (e) { /* use raw */ }
    found = parseManage(value);
  }

  const params = new URLSearchParams(search);
  if (params.has('manage')) {
    strip = true;
    if (!found) found = parseManage(params.get('manage'));
    params.delete('manage');
    const rest = params.toString();
    search = rest ? '?' + rest : '';
  }

  if (found) rememberManage(found.tripId, found.token);

  if (strip) {
    try {
      const keepHash = /^#manage=/.test(hash) ? '' : hash;
      window.history.replaceState(null, '', window.location.pathname + search + keepHash);
    } catch (e) { /* ignore */ }
  }
  return found ? { tripId: found.tripId } : null;
}

/* test hook: forget cached state so a test can re-read storage */
export function _resetCache() {
  manageCache = null;
  lastLive = null;
}

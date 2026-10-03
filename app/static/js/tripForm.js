/* Add, edit and remove a trip.

   createTripForm({user, getTrips, onSaved}) -> {openNew, openEdit, openConfirmRemove}

   Builds its own native <dialog> elements (appended to document.body), so
   Escape, the focus trap and the inert background come from the platform.
   Every leg row is either a flight row (flight number, date, optional From/To
   hints) or an airports row; a per-row switch moves between them without
   losing what was typed. Rows are checked live against POST api/legs/preview. */

import { request } from './api.js';
import { parseStamp, fmtIn } from './format.js';
import {
  identity, saveIdentity, manageToken, rememberManage, forgetManage, proofToken,
} from './identity.js';

const MAX_ROWS = 8;
const DEBOUNCE_MS = 500;

/* Same rules as app/validation.py, applied after upper-casing and removing
   spaces and hyphens. */
const IATA_RE = /^[A-Z0-9]{2}\d{1,4}[A-Z]?$/;
const ICAO_RE = /^[A-Z]{3}\d{1,4}[A-Z]?$/;
const AIRPORT_RE = /^[A-Z0-9]{3,4}$/;

export function normCode(s) {
  return String(s == null ? '' : s).toUpperCase().replace(/[\s-]+/g, '');
}
export function isFlightNo(n) {
  return ICAO_RE.test(n) || (IATA_RE.test(n) && /[A-Z]/.test(n.slice(0, 2)));
}
export function isAirport(n) { return AIRPORT_RE.test(n); }

const KEEPABLE = new Set(['not_found', 'upstream_unavailable', 'quota']);
const BLOCKING = new Set(['ambiguous', 'out_of_window', 'invalid', 'airport_unknown']);
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/* ---------- small helpers ---------- */
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid);
  }
  return el;
}

let uidSeq = 0;
const nextId = (p) => `${p}${++uidSeq}`;

function pad(n) { return String(n).padStart(2, '0'); }
function isoDate(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
function shiftDays(d, n) { const c = new Date(d); c.setDate(c.getDate() + n); return c; }

let deviceTz = 'UTC';
try { deviceTz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch (err) { /* keep UTC */ }

/* Same clock as the board: the viewer's own zone and locale. */
function clock(local) {
  const st = parseStamp(local);
  if (!st) return '';
  return st.offMin == null ? `${Number(st.time.slice(0, 2))}:${st.time.slice(3)}` : fmtIn(st.ms, deviceTz).time;
}
function code(leg, side) { return leg[side + '_iata'] || leg[side] || ''; }

function legSummary(leg) {
  if (!leg) return '';
  const parts = [];
  const f = code(leg, 'from'), t = code(leg, 'to');
  if (f && t) parts.push(`${f} to ${t}`);
  const dep = clock(leg.dep_local), arr = clock(leg.arr_local);
  if (dep && arr) parts.push(`${dep} to ${arr}`);
  else if (dep) parts.push(`departs ${dep}`);
  if (leg.ac_model) parts.push(leg.ac_model);
  return parts.join(', ');
}

function candidateLabel(c) {
  const parts = [`${c.from_iata || c.from} to ${c.to_iata || c.to}`];
  const dep = clock(c.dep_local), arr = clock(c.arr_local);
  if (dep && arr) parts.push(`${dep} to ${arr}`);
  else if (dep) parts.push(`departs ${dep}`);
  return parts.join(', ');
}

function prettyDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
  return m ? `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}` : '';
}

function errorDetail(data) {
  const d = data && data.detail;
  if (!d) return { message: '', rows: [] };
  if (typeof d === 'string') return { message: d, rows: [] };
  return { message: typeof d.message === 'string' ? d.message : '', rows: Array.isArray(d.rows) ? d.rows : [] };
}

/* ---------- scroll lock shared by all dialogs ---------- */
let openDialogs = 0;
function lockScroll() { openDialogs += 1; document.documentElement.classList.add('has-dialog'); }
function unlockScroll() {
  openDialogs = Math.max(0, openDialogs - 1);
  if (!openDialogs) document.documentElement.classList.remove('has-dialog');
}

/* Backdrop click closes, but only when both press and release land on the
   backdrop, so dragging a text selection out of a field does not. */
function closeOnBackdrop(dlg) {
  let downOnBackdrop = false;
  dlg.addEventListener('pointerdown', (e) => { downOnBackdrop = e.target === dlg; });
  dlg.addEventListener('click', (e) => {
    if (e.target === dlg && downOnBackdrop) dlg.close();
    downOnBackdrop = false;
  });
}

/* ================================================================ */
export function createTripForm({ user = null, getTrips = () => [], onSaved = () => {} } = {}) {
  /* ---- dialog state shared by openNew / openEdit ---- */
  let mode = 'new';          // 'new' | 'edit'
  let editId = null;
  let opener = null;
  let busy = false;
  let session = 0;           // bumped on every open; stale async work checks it
  let submitCtrl = null;
  let retShown = false;
  let allowBeyondRange = false;
  const rows = { out: [], ret: [] };

  const trips = () => {
    const t = getTrips();
    return Array.isArray(t) ? t : (t && Array.isArray(t.trips) ? t.trips : []);
  };

  /* ---- dialog skeleton ---- */
  const ids = {
    title: nextId('tf-title'), desc: nextId('tf-desc'), name: nextId('tf-name'), nameHint: nextId('tf-namehint'),
    alert: nextId('tf-alert'), status: nextId('tf-status'),
  };

  const closeBtn = h('button', { type: 'button', class: 'btn btn--ghost btn--sm tf-close', 'aria-label': 'Close' }, '×');
  const titleEl = h('h2', { id: ids.title, class: 'tf-title', tabindex: '-1' });
  const descEl = h('p', { id: ids.desc, class: 'tf-lead' });

  const nameInput = h('input', {
    id: ids.name, class: 'input', type: 'text', maxlength: '40', autocomplete: 'name',
    placeholder: 'For example, Alex', 'aria-describedby': ids.nameHint,
  });
  const nameError = h('p', { class: 'field__error', hidden: true });
  const nameField = h('div', { class: 'field tf-name' },
    h('label', { class: 'field__label', for: ids.name }, 'Your name'),
    nameInput,
    h('p', { id: ids.nameHint, class: 'field__hint', text: 'This name is public. Anyone with the link to the board can see it.' }),
    nameError);

  const retAddBtn = h('button', { type: 'button', class: 'btn btn--sm tf-ret-toggle' }, 'Add return');
  const cancelBtn = h('button', { type: 'button', class: 'btn' }, 'Cancel');
  const saveBtn = h('button', { type: 'submit', class: 'btn btn--primary tf-save' }, 'Save trip');

  const statusEl = h('p', { id: ids.status, class: 'tf-formstatus field__hint', role: 'status', 'aria-live': 'polite' });
  const alertEl = h('p', { id: ids.alert, class: 'tf-alert field__error', role: 'alert' });
  const alertExtra = h('div', { class: 'tf-alert-extra' });

  function dirBlock(dir, label) {
    const list = h('div', { class: 'tf-rows' });
    const addBtn = h('button', { type: 'button', class: 'btn btn--sm tf-add', 'data-dir': dir }, 'Add connection');
    const maxNote = h('p', { class: 'field__hint tf-max', hidden: true, text: `Maximum legs in one direction: ${MAX_ROWS}.` });
    const fs = h('fieldset', { class: 'tf-dir', 'data-dir': dir }, h('legend', { class: 'tf-dir__legend', text: label }), list, addBtn, maxNote);
    return { fs, list, addBtn, maxNote };
  }
  const blocks = { out: dirBlock('out', 'Outbound'), ret: dirBlock('ret', 'Return') };
  blocks.ret.fs.hidden = true;

  const formEl = h('form', { class: 'tf-form', method: 'dialog', novalidate: true },
    h('header', { class: 'tf-head' }, titleEl, closeBtn),
    h('div', { class: 'tf-body' },
      descEl, nameField, blocks.out.fs, blocks.ret.fs, statusEl, alertEl, alertExtra),
    h('div', { class: 'tf-actions' }, retAddBtn, h('span', { class: 'tf-spacer' }), cancelBtn, saveBtn));

  /* saved panel (anonymous creates) */
  const linkId = nextId('tf-link');
  const linkInput = h('input', { id: linkId, class: 'input tf-link', type: 'text', readonly: true, spellcheck: 'false' });
  const copyBtn = h('button', { type: 'button', class: 'btn btn--sm' }, 'Copy');
  const copyStatus = h('p', { class: 'field__hint', role: 'status', 'aria-live': 'polite' });
  const doneBtn = h('button', { type: 'button', class: 'btn btn--primary' }, 'Done');
  const savedLead = h('p', { id: nextId('tf-savedlead'), class: 'tf-lead', text: 'It is on the board. This browser can edit or remove the trip any time.' });
  const savedEl = h('div', { class: 'tf-saved', hidden: true },
    h('div', { class: 'tf-body' },
      savedLead,
      h('div', { class: 'field' },
        h('label', { class: 'field__label', for: linkId }, 'Backup manage link'),
        h('div', { class: 'tf-linkrow' }, linkInput, copyBtn),
        copyStatus),
      h('p', { class: 'field__hint', text: 'Open this link in any browser and it gets the same edit and remove rights. Keep it somewhere safe if you might clear this browser\'s data or switch devices. Anyone who has it can change this trip.' })),
    h('div', { class: 'tf-actions' }, h('span', { class: 'tf-spacer' }), doneBtn));

  const dlg = h('dialog', { class: 'dialog tf-dialog', 'aria-labelledby': ids.title, 'aria-describedby': ids.desc }, formEl, savedEl);
  document.body.append(dlg);
  closeOnBackdrop(dlg);

  /* ================= rows ================= */
  const legLabel = (row) => {
    const i = rows[row.dir].indexOf(row);
    return `leg ${i + 1}`;
  };
  const legLabelCap = (row) => {
    const l = legLabel(row);
    return row.dir === 'ret' ? `Return ${l}` : `Leg ${rows[row.dir].indexOf(row) + 1}`;
  };

  function dateBounds() {
    const body = document.body.dataset || {};
    const today = new Date();
    return {
      min: allowBeyondRange ? '' : (body.dateMin || isoDate(shiftDays(today, -2))),
      max: body.dateMax || isoDate(shiftDays(today, 330)),
    };
  }

  function makeRow(dir, init = {}) {
    const n = nextId('tf-r');
    const row = {
      id: n, dir, kind: init.kind || 'flight', key: null, timer: null, ctrl: null, pending: false,
      preview: null, local: null, acceptUnverified: false, dupOf: null,
    };
    const bounds = dateBounds();

    const mk = (name, label, inputProps, hintText) => {
      const input = h('input', Object.assign({ id: `${n}-${name}`, class: 'input', 'data-field': name }, inputProps));
      const lab = h('label', { class: 'field__label', for: input.id }, label, hintText ? h('span', { class: 'tf-optional', text: ` (${hintText})` }) : null);
      const wrap = h('div', { class: `field tf-f tf-f--${name}` }, lab, input);
      return { input, lab, wrap, hint: lab.querySelector('.tf-optional') };
    };
    const f = {
      flight_no: mk('flight_no', 'Flight number', { type: 'text', maxlength: '12', placeholder: 'DL1200', autocomplete: 'off', autocapitalize: 'characters', spellcheck: 'false' }),
      date: mk('date', 'Date', { type: 'date', min: bounds.min || null, max: bounds.max }),
      from: mk('from', 'From', { type: 'text', maxlength: '8', placeholder: 'JAX', autocomplete: 'off', autocapitalize: 'characters', spellcheck: 'false' }, 'optional'),
      to: mk('to', 'To', { type: 'text', maxlength: '8', placeholder: 'DEN', autocomplete: 'off', autocapitalize: 'characters', spellcheck: 'false' }, 'optional'),
    };
    f.flight_no.input.value = init.flight_no || '';
    f.date.input.value = init.date || '';
    f.from.input.value = init.from || '';
    f.to.input.value = init.to || '';
    row.f = f;

    const legTag = h('span', { class: 'tf-row__tag' });
    const segFlight = h('button', { type: 'button', class: 'tf-seg__btn', 'data-kind': 'flight' }, 'Flight number');
    const segManual = h('button', { type: 'button', class: 'tf-seg__btn', 'data-kind': 'manual' }, 'Airports');
    const seg = h('div', { class: 'tf-seg', role: 'group' }, segFlight, segManual);
    const removeBtn = h('button', { type: 'button', class: 'btn btn--ghost tf-row__remove' }, '×');
    const resultEl = h('div', { class: 'tf-result', id: `${n}-result`, role: 'status', 'aria-live': 'polite' });

    row.el = h('div', { class: 'tf-row', 'data-dir': dir },
      h('div', { class: 'tf-row__head' }, legTag, seg, removeBtn),
      h('div', { class: 'tf-row__fields' }, f.flight_no.wrap, f.date.wrap, f.from.wrap, f.to.wrap),
      resultEl);
    Object.assign(row, { legTag, seg, segFlight, segManual, removeBtn, resultEl });

    /* events */
    for (const name of ['flight_no', 'from', 'to']) {
      const inp = f[name].input;
      inp.addEventListener('input', () => onEdit(row));
      inp.addEventListener('blur', () => onBlur(row, name));
    }
    f.date.input.addEventListener('change', () => onEdit(row, 0));
    f.date.input.addEventListener('input', () => onEdit(row, 0));
    segFlight.addEventListener('click', () => setKind(row, 'flight'));
    segManual.addEventListener('click', () => setKind(row, 'manual'));
    removeBtn.addEventListener('click', () => removeRow(row));

    applyKind(row);
    row.key = computeKey(row);
    if (init.preview) row.preview = init.preview;
    if (init.acceptUnverified) row.acceptUnverified = true;
    return row;
  }

  function applyKind(row) {
    const flight = row.kind === 'flight';
    row.el.dataset.kind = row.kind;
    row.f.flight_no.wrap.hidden = !flight;
    row.f.from.hint.hidden = !flight;
    row.f.to.hint.hidden = !flight;
    row.f.from.input.placeholder = flight ? 'JAX' : 'JAX or KJAX';
    row.f.to.input.placeholder = flight ? 'DEN' : 'DEN or KDEN';
    row.segFlight.setAttribute('aria-pressed', String(flight));
    row.segManual.setAttribute('aria-pressed', String(!flight));
  }

  function setKind(row, kind) {
    if (row.kind === kind) return;
    row.kind = kind;
    applyKind(row);
    onEdit(row, 0);
  }

  function renumber(dir) {
    const list = rows[dir];
    const only = list.length === 1;
    list.forEach((row, i) => {
      const num = i + 1;
      const tag = dir === 'ret' ? `Return leg ${num}` : `Leg ${num}`;
      row.legTag.textContent = tag;
      row.seg.setAttribute('aria-label', `${tag} type`);
      row.removeBtn.setAttribute('aria-label', dir === 'ret' ? `Remove return leg ${num}` : `Remove leg ${num}`);
      row.removeBtn.hidden = only;
      row.el.classList.toggle('tf-row--single', only);
    });
    const full = list.length >= MAX_ROWS;
    blocks[dir].addBtn.hidden = full;
    blocks[dir].maxNote.hidden = !full;
    refreshDupes(dir);
  }

  function addRow(dir, init) {
    const row = makeRow(dir, init);
    rows[dir].push(row);
    blocks[dir].list.append(row.el);
    renumber(dir);
    renderResult(row);
    return row;
  }

  function disposeRow(row) {
    clearTimeout(row.timer);
    if (row.ctrl) row.ctrl.abort();
    row.ctrl = null;
  }

  function removeRow(row) {
    const list = rows[row.dir];
    const i = list.indexOf(row);
    if (i < 0 || list.length <= 1) return;
    disposeRow(row);
    list.splice(i, 1);
    row.el.remove();
    renumber(row.dir);
    const next = list[Math.min(i, list.length - 1)];
    focusRow(next);
    setStatus(`Removed ${row.dir === 'ret' ? 'return ' : ''}leg ${i + 1}.`);
  }

  function visibleFirstInput(row) {
    return row.kind === 'flight' ? row.f.flight_no.input : row.f.from.input;
  }
  function focusRow(row) { if (row) visibleFirstInput(row).focus(); }

  /* ================= values and classification ================= */
  function values(row) {
    return {
      flight_no: normCode(row.f.flight_no.input.value),
      date: row.f.date.input.value || '',
      from: normCode(row.f.from.input.value),
      to: normCode(row.f.to.input.value),
    };
  }

  function apiRow(row) {
    const v = values(row);
    if (row.kind === 'flight') {
      const r = { flight_no: v.flight_no };
      if (v.date) r.date = v.date;
      if (v.from) r.from = v.from;
      if (v.to) r.to = v.to;
      return r;
    }
    const r = { from: v.from, to: v.to };
    if (v.date) r.date = v.date;
    return r;
  }

  function computeKey(row) { return row.kind + '|' + JSON.stringify(apiRow(row)); }

  /* Returns {empty:true} | {error:{fields:[...], msg}} | {ok:true}. Only the
     data typed so far is judged; the server stays the authority. */
  function classify(row) {
    const v = values(row);
    const L = legLabelCap(row);
    if (row.kind === 'flight') {
      if (!v.flight_no && !v.from && !v.to) return { empty: true };
      if (!v.flight_no) return { error: { fields: ['flight_no'], msg: `${L} needs a flight number, or switch it to Airports.` } };
      if (!isFlightNo(v.flight_no)) return { error: { fields: ['flight_no'], msg: 'That does not look like a flight number. Try something like DL1200.' } };
      if (v.from && !isAirport(v.from)) return { error: { fields: ['from'], msg: 'Airport codes are 3 or 4 letters or digits, like JAX or KJAX.' } };
      if (v.to && !isAirport(v.to)) return { error: { fields: ['to'], msg: 'Airport codes are 3 or 4 letters or digits, like DEN or KDEN.' } };
      return { ok: true };
    }
    if (!v.from && !v.to) return { empty: true };
    if (!v.from) return { error: { fields: ['from'], msg: `${L} needs a From airport.` } };
    if (!v.to) return { error: { fields: ['to'], msg: `${L} needs a To airport.` } };
    if (!isAirport(v.from)) return { error: { fields: ['from'], msg: 'Airport codes are 3 or 4 letters or digits, like JAX or KJAX.' } };
    if (!isAirport(v.to)) return { error: { fields: ['to'], msg: 'Airport codes are 3 or 4 letters or digits, like DEN or KDEN.' } };
    return { ok: true };
  }

  /* ================= result region ================= */
  function clearInvalid(row) {
    for (const name of Object.keys(row.f)) {
      row.f[name].input.removeAttribute('aria-invalid');
      row.f[name].input.removeAttribute('aria-describedby');
    }
  }

  function markInvalid(row, fields, errId) {
    for (const name of fields) {
      const inp = row.f[name].input;
      inp.setAttribute('aria-invalid', 'true');
      inp.setAttribute('aria-describedby', errId);
    }
  }

  function fieldsFor(row, status) {
    if (status === 'out_of_window') return ['date'];
    if (status === 'airport_unknown') return ['from', 'to'];
    return row.kind === 'flight' ? ['flight_no'] : ['from', 'to'];
  }

  function renderResult(row) {
    const box = row.resultEl;
    box.replaceChildren();
    clearInvalid(row);
    row.el.classList.remove('tf-row--bad', 'tf-row--ok');
    const errId = `${row.id}-err`;

    const showError = (msg, fields) => {
      box.append(h('p', { class: 'field__error', id: errId, text: msg }));
      markInvalid(row, fields, errId);
      row.el.classList.add('tf-row--bad');
    };
    const keepAnyway = (note) => {
      const cb = h('input', { type: 'checkbox', class: 'tf-keep__box', id: `${row.id}-keep` });
      cb.checked = row.acceptUnverified;
      cb.addEventListener('change', () => { row.acceptUnverified = cb.checked; });
      box.append(h('div', { class: 'tf-keep' }, cb,
        h('label', { for: cb.id }, h('span', { class: 'tf-keep__title', text: 'Keep it anyway' }),
          h('span', { class: 'field__hint', text: note || 'It will be saved marked unverified on the board.' }))));
    };

    if (row.local) { showError(row.local.msg, row.local.fields); return; }

    if (row.pending) {
      box.append(h('p', { class: 'tf-checking field__hint', text: 'Checking…' }));
      return;
    }

    const p = row.preview;
    if (p) {
      const st = p.status;
      if (st === 'ok') {
        row.el.classList.add('tf-row--ok');
        box.append(h('div', { class: 'tf-card tf-card--ok' },
          h('span', { class: 'status status--scheduled', text: 'Verified' }), ' ',
          h('span', { class: 'tf-card__text', text: legSummary(p.leg) || p.message || 'Found it.' })));
      } else if (st === 'manual_ok') {
        row.el.classList.add('tf-row--ok');
        const leg = p.leg || {};
        const txt = leg.from || leg.to
          ? `${leg.from_name || code(leg, 'from')} to ${leg.to_name || code(leg, 'to')}`
          : (p.message || 'Both airports check out.');
        box.append(h('div', { class: 'tf-card tf-card--ok' },
          h('span', { class: 'status status--scheduled', text: 'Airports confirmed' }), ' ',
          h('span', { class: 'tf-card__text', text: txt })));
      } else if (st === 'ambiguous') {
        row.el.classList.add('tf-row--bad');
        const legend = p.message || 'That number flies more than one leg that day. Pick yours.';
        const group = h('fieldset', { class: 'tf-cands' }, h('legend', { class: 'tf-cands__legend', text: legend }));
        (p.candidates || []).forEach((c, i) => {
          const rid = `${row.id}-c${i}`;
          const radio = h('input', { type: 'radio', name: `${row.id}-cand`, id: rid, class: 'tf-cands__radio' });
          radio.addEventListener('change', () => pickCandidate(row, c));
          group.append(h('label', { class: 'tf-cands__opt', for: rid }, radio, h('span', { text: candidateLabel(c) })));
        });
        box.append(group);
      } else if (BLOCKING.has(st) || KEEPABLE.has(st)) {
        showError(p.message || 'Could not check that one.', fieldsFor(row, st));
        if (KEEPABLE.has(st)) keepAnyway(st === 'not_found' ? 'Some flights are not in the schedule yet. It will be saved marked unverified on the board.' : null);
      } else if (st === '_unverified_kept') {
        box.append(h('p', { class: 'field__hint', text: p.message }));
        keepAnyway('This leg was saved without being verified.');
      } else {
        /* _offline / _throttled and anything unexpected: non-blocking note */
        box.append(h('p', { class: 'field__hint', text: p.message || 'Could not check this one just now. You can still save it.' }));
      }
    }
    if (row.dupOf) {
      box.append(h('p', { class: 'field__hint tf-dup', text: `Same as ${row.dupOf}. It will only be saved once.` }));
    }
  }

  function refreshDupes(dir) {
    const seen = new Map();
    rows[dir].forEach((row) => {
      const c = classify(row);
      const before = row.dupOf;
      row.dupOf = null;
      if (c.ok) {
        const k = computeKey(row);
        if (seen.has(k)) row.dupOf = seen.get(k);
        else seen.set(k, legLabel(row));
      }
      if (before !== row.dupOf && !row.pending) renderResult(row);
    });
  }

  /* ================= live preview ================= */
  function onEdit(row, delay = DEBOUNCE_MS) {
    const key = computeKey(row);
    if (key === row.key) return;
    row.key = key;
    clearTimeout(row.timer);
    if (row.ctrl) { row.ctrl.abort(); row.ctrl = null; }
    row.pending = false;
    row.preview = null;
    row.local = null;
    row.acceptUnverified = false;
    renderResult(row);
    refreshDupes(row.dir);
    row.timer = setTimeout(() => runPreview(row), delay);
  }

  function onBlur(row, name) {
    /* judge only the field that was just left, so a person who tabs past an
       empty flight number to type the airports first is not scolded */
    const v = values(row)[name];
    if (v && !row.local && !row.pending) {
      let msg = null;
      if (name === 'flight_no' && !isFlightNo(v)) msg = 'That does not look like a flight number. Try something like DL1200.';
      if ((name === 'from' || name === 'to') && !isAirport(v)) msg = 'Airport codes are 3 or 4 letters or digits, like JAX or KJAX.';
      if (msg) { row.local = { fields: [name], msg }; renderResult(row); return; }
    }
    if (row.timer) { clearTimeout(row.timer); row.timer = null; runPreview(row); }
  }

  async function runPreview(row) {
    row.timer = null;
    if (!dlg.open || !rows[row.dir].includes(row)) return;
    if (!classify(row).ok || row.local) return;
    if (row.preview && row.preview.status !== '_offline' && row.preview.status !== '_throttled') return;
    if (row.ctrl) row.ctrl.abort();
    const ctrl = new AbortController();
    row.ctrl = ctrl;
    const key = row.key;
    row.pending = true;
    renderResult(row);
    let res;
    try {
      res = await request('POST', 'api/legs/preview', { rows: [Object.assign({ kind: row.kind }, apiRow(row))] }, { signal: ctrl.signal });
    } catch (e) {
      res = { ok: false, status: 0, data: null, networkError: true };
    }
    if (ctrl.signal.aborted || row.ctrl !== ctrl || row.key !== key) return;
    row.ctrl = null;
    row.pending = false;
    const first = res && res.ok && res.data && Array.isArray(res.data.results) ? res.data.results[0] : null;
    if (first && first.status) row.preview = first;
    else if (res && res.status === 429) row.preview = { status: '_throttled', message: 'Too many lookups. Wait a minute, or save now and it will be checked then.' };
    else row.preview = { status: '_offline', message: 'Could not check this one just now. You can still save it and it will be checked again then.' };
    renderResult(row);
  }

  function pickCandidate(row, c) {
    row.f.from.input.value = c.from;
    row.f.to.input.value = c.to;
    onEdit(row, 0);
    visibleFirstInput(row).focus();
  }

  /* ================= status / alert ================= */
  function setStatus(msg) { statusEl.textContent = msg || ''; }
  function setAlert(msg, extra) {
    alertEl.textContent = msg || '';
    alertExtra.replaceChildren();
    if (extra) alertExtra.append(extra);
  }
  function setBusy(on) {
    busy = on;
    saveBtn.disabled = on;
    formEl.setAttribute('aria-busy', String(on));
    saveBtn.textContent = on ? 'Saving…' : (mode === 'edit' ? 'Save changes' : 'Save trip');
  }

  /* ================= return section ================= */
  function setReturn(on, init) {
    retShown = on;
    blocks.ret.fs.hidden = !on;
    retAddBtn.textContent = on ? 'Remove return' : 'Add return';
    if (on) {
      if (!rows.ret.length) addRow('ret', init);
    } else {
      rows.ret.forEach(disposeRow);
      rows.ret.length = 0;
      blocks.ret.list.replaceChildren();
    }
  }

  /* ================= open / close ================= */
  function resetForm() {
    session += 1;
    setBusy(false);
    setAlert('');
    setStatus('');
    copyStatus.textContent = '';
    for (const dir of ['out', 'ret']) {
      rows[dir].forEach(disposeRow);
      rows[dir].length = 0;
      blocks[dir].list.replaceChildren();
    }
    nameError.hidden = true;
    nameInput.removeAttribute('aria-invalid');
    formEl.hidden = false;
    savedEl.hidden = true;
    retShown = false;
    blocks.ret.fs.hidden = true;
    retAddBtn.textContent = 'Add return';
  }

  function show() {
    opener = document.activeElement && document.activeElement !== document.body ? document.activeElement : null;
    if (!dlg.open) {
      dlg.showModal();
      lockScroll();
    }
  }

  function describe() {
    titleEl.textContent = mode === 'edit' ? 'Edit trip' : 'Add a trip';
    descEl.textContent = mode === 'edit'
      ? 'Change the flights or airports below. Anything that changed will be checked.'
      : 'Enter a flight number and a date to fill in the airports, times and aircraft. If a number flies several legs that day, add From and To to pick yours.';
    nameField.hidden = !!user || mode === 'edit';
    saveBtn.textContent = mode === 'edit' ? 'Save changes' : 'Save trip';
  }

  function openNew() {
    mode = 'new'; editId = null; allowBeyondRange = false;
    resetForm();
    describe();
    nameInput.value = user ? '' : (identity().name || '');
    addRow('out', { kind: 'flight', date: isoDate(new Date()) });
    show();
    (nameField.hidden || nameInput.value ? visibleFirstInput(rows.out[0]) : nameInput).focus();
  }

  function rowInitFromLeg(leg) {
    const manual = !leg.flight_no;
    return {
      kind: manual ? 'manual' : 'flight',
      flight_no: leg.flight_no || '',
      date: leg.date_local || '',
      from: leg.from_iata || leg.from || '',
      to: leg.to_iata || leg.to || '',
      acceptUnverified: !!leg.unverified,
      preview: leg.unverified ? { status: '_unverified_kept', message: 'This leg could not be verified earlier, so it is kept as unverified.' } : null,
    };
  }

  function openEdit(tripId) {
    const trip = trips().find((t) => String(t.id) === String(tripId));
    if (!trip) {
      notice('That trip is gone', 'It is no longer on the board, so there is nothing to edit.');
      return;
    }
    mode = 'edit'; editId = String(tripId); allowBeyondRange = true;
    resetForm();
    describe();
    (trip.out && trip.out.length ? trip.out : [{}]).forEach((leg) => addRow('out', rowInitFromLeg(leg)));
    if (trip.ret && trip.ret.length) {
      setReturn(true, rowInitFromLeg(trip.ret[0]));
      trip.ret.slice(1).forEach((leg) => addRow('ret', rowInitFromLeg(leg)));
    }
    show();
    focusRow(rows.out[0]);
  }

  dlg.addEventListener('close', () => {
    unlockScroll();
    session += 1;
    for (const dir of ['out', 'ret']) rows[dir].forEach(disposeRow);
    /* a save already in flight is left to finish: aborting it could lose the
       manage token of a trip the server has already created */
    const o = opener;
    opener = null;
    const target = o && o.isConnected ? o : document.getElementById('openAdd');
    if (target && typeof target.focus === 'function') target.focus();
  });
  closeBtn.addEventListener('click', () => dlg.close());
  cancelBtn.addEventListener('click', () => dlg.close());
  doneBtn.addEventListener('click', () => dlg.close());

  retAddBtn.addEventListener('click', () => {
    if (retShown) {
      setReturn(false);
      setStatus('Return removed.');
      retAddBtn.focus();
    } else {
      setReturn(true, { kind: 'flight', date: isoDate(new Date()) });
      focusRow(rows.ret[0]);
    }
  });
  for (const dir of ['out', 'ret']) {
    blocks[dir].addBtn.addEventListener('click', () => {
      if (rows[dir].length >= MAX_ROWS) return;
      const last = rows[dir][rows[dir].length - 1];
      const row = addRow(dir, { kind: last ? last.kind : 'flight', date: last ? last.f.date.input.value : isoDate(new Date()) });
      focusRow(row);
    });
  }

  nameInput.addEventListener('input', () => {
    nameError.hidden = true;
    nameInput.removeAttribute('aria-invalid');
    nameInput.setAttribute('aria-describedby', ids.nameHint);
  });

  /* ================= submit ================= */
  formEl.addEventListener('submit', (e) => { e.preventDefault(); submit(); });

  function fail(msg, focusEl) {
    setAlert(msg);
    if (focusEl) focusEl.focus();
  }

  async function submit() {
    if (busy) return;
    setAlert('');
    setStatus('');

    /* 1. gather. an empty row is skipped; a half-filled row blocks. */
    const submitted = { out: [], ret: [] };
    let firstBad = null;
    const problems = [];
    for (const dir of ['out', 'ret']) {
      if (dir === 'ret' && !retShown) continue;
      const seen = new Set();
      rows[dir].forEach((row) => {
        const c = classify(row);
        if (c.empty) return;
        if (c.error) {
          row.local = c.error;
          renderResult(row);
          problems.push(c.error.msg);
          if (!firstBad) firstBad = row.f[c.error.fields[0]].input;
          return;
        }
        const p = row.preview;
        if (p && BLOCKING.has(p.status)) {
          const msg = p.status === 'ambiguous' ? `${legLabelCap(row)}: pick which leg you mean.` : `${legLabelCap(row)}: ${p.message}`;
          problems.push(msg);
          if (!firstBad) {
            firstBad = p.status === 'ambiguous'
              ? (row.resultEl.querySelector('input') || visibleFirstInput(row))
              : row.f[fieldsFor(row, p.status)[0]].input;
          }
          return;
        }
        const k = computeKey(row);
        if (seen.has(k)) return; // exact duplicate: saved once
        seen.add(k);
        submitted[dir].push({ row, data: apiRow(row) });
      });
    }
    if (problems.length) {
      setAlert(problems.length === 1 ? problems[0] : `${problems.length} legs need another look. ${problems[0]}`);
      if (firstBad) firstBad.focus();
      return;
    }
    if (!submitted.out.length && !submitted.ret.length) {
      fail('Add at least one flight.', visibleFirstInput(rows.out[0]));
      return;
    }

    /* 2. name (anonymous new trips) */
    let name = '';
    if (mode === 'new' && !user) {
      name = nameInput.value.trim();
      if (!name) {
        nameError.textContent = 'Add your name so people know whose trip this is.';
        nameError.id = nextId('tf-nameerr');
        nameError.hidden = false;
        nameInput.setAttribute('aria-invalid', 'true');
        nameInput.setAttribute('aria-describedby', `${ids.nameHint} ${nameError.id}`);
        fail('Add your name so people know whose trip this is.', nameInput);
        return;
      }
    }

    /* 3. keep-anyway is one flag per request, so every unverifiable row has to opt in */
    const all = submitted.out.concat(submitted.ret);
    const accept = all.some((s) => s.row.acceptUnverified);
    if (accept) {
      const un = all.find((s) => !s.row.acceptUnverified && s.row.preview && KEEPABLE.has(s.row.preview.status));
      if (un) {
        fail(`Unverified legs are only kept when ticked one by one. Tick Keep it anyway on ${legLabel(un.row)} too, or fix it.`,
          un.row.resultEl.querySelector('input[type=checkbox]') || visibleFirstInput(un.row));
        return;
      }
    }

    const payload = { out: submitted.out.map((s) => s.data), ret: submitted.ret.map((s) => s.data) };
    try { payload.tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (err) { /* ignore */ }
    if (accept) payload.accept_unverified = true;
    if (mode === 'new' && !user) {
      payload.name = name;
      const id = identity();
      if (id.uid) {
        payload.uid = id.uid;
        const proof = proofToken();
        if (proof) payload.proof = proof;
      }
    }

    const isEdit = mode === 'edit';
    const url = isEdit ? `api/trips/${editId}` : 'api/trips';
    const tripId = editId;
    const mySession = session;
    const ctrl = new AbortController();
    submitCtrl = ctrl;
    setBusy(true);
    setStatus('Saving…');

    let res;
    try {
      res = await request(isEdit ? 'PUT' : 'POST', url, payload, {
        manageToken: isEdit ? (manageToken(tripId) || undefined) : undefined,
        signal: ctrl.signal,
      });
    } catch (err) {
      res = { ok: false, status: 0, data: null, networkError: true };
    }
    if (submitCtrl === ctrl) submitCtrl = null;
    const live = mySession === session; // false when the dialog was closed or reopened meanwhile

    if (res && res.ok) {
      const data = res.data || {};
      let token = null;
      if (data.manage_token && data.trip_id != null) {
        saveIdentity(data.uid, name, data.identity_secret);
        rememberManage(data.trip_id, data.manage_token);
        token = data.manage_token;
      }
      try { onSaved(); } catch (err) { /* the board refresh must not break the dialog */ }
      if (!live) return;
      setBusy(false);
      setStatus('');
      if (token) showSaved(data.trip_id, token);
      else dlg.close();
      return;
    }
    if (!live) return;
    setBusy(false);
    setStatus('');
    handleFailure(res, submitted, isEdit ? tripId : null);
  }

  function handleFailure(res, submitted, tripId) {
    const status = res ? res.status : 0;
    if (!res || res.networkError || status === 0) {
      setAlert('Couldn\'t reach the server. Your entries are still here.');
      saveBtn.focus();
      return;
    }
    const detail = errorDetail(res.data);
    if (status === 400) {
      let firstEl = null;
      const used = [];
      detail.rows.forEach((r) => {
        const item = (submitted[r.direction] || [])[r.index];
        if (!item) return;
        const row = item.row;
        row.local = null;
        row.pending = false;
        row.preview = { status: r.status, message: r.message || detail.message };
        row.acceptUnverified = false;
        renderResult(row);
        used.push(row);
        if (!firstEl) {
          firstEl = r.status === 'ambiguous'
            ? (row.resultEl.querySelector('input') || visibleFirstInput(row))
            : row.f[fieldsFor(row, r.status)[0]].input;
        }
      });
      setAlert(detail.message || (used.length ? 'Some legs need another look.' : 'Could not save that. Check the details and try again.'));
      (firstEl || saveBtn).focus();
      return;
    }
    if (status === 429) {
      setAlert('That is a lot of changes from your connection. Try again in a few minutes. Your entries are still here.');
      saveBtn.focus();
      return;
    }
    if (status === 403) {
      const btn = h('button', { type: 'button', class: 'btn btn--sm' }, 'Refresh the board');
      btn.addEventListener('click', () => {
        if (tripId) forgetManage(tripId);
        try { onSaved(); } catch (err) { /* ignore */ }
        dlg.close();
      });
      setAlert(tripId
        ? 'This browser can no longer change that trip. The edit link it was holding does not work any more. Your entries are still here.'
        : (detail.message || 'The server would not accept that. Your entries are still here.'), btn);
      saveBtn.focus();
      return;
    }
    if (status === 404 && tripId) {
      forgetManage(tripId);
      try { onSaved(); } catch (err) { /* ignore */ }
      setAlert('That trip has already been removed.');
      return;
    }
    setAlert(detail.message || 'Couldn\'t save that. Your entries are still here.');
    saveBtn.focus();
  }

  /* ================= saved panel ================= */
  function showSaved(tripId, token) {
    linkInput.value = `${window.location.href.split(/[?#]/)[0]}#manage=${tripId}.${token}`;
    formEl.hidden = true;
    savedEl.hidden = false;
    /* the form heading is hidden now: label the dialog by the saved panel's own */
    savedHeading.hidden = false;
    dlg.setAttribute('aria-labelledby', savedHeading.id);
    dlg.setAttribute('aria-describedby', savedLead.id);
    savedHeading.focus();
  }
  const savedHeading = h('h2', { id: nextId('tf-savedtitle'), class: 'tf-title tf-saved__title', tabindex: '-1', hidden: true, text: 'Trip saved' });
  savedEl.prepend(h('header', { class: 'tf-head' }, savedHeading, h('button', { type: 'button', class: 'btn btn--ghost btn--sm tf-close', 'aria-label': 'Close', onclick: () => dlg.close() }, '×')));
  dlg.addEventListener('close', () => { dlg.setAttribute('aria-labelledby', ids.title); dlg.setAttribute('aria-describedby', ids.desc); savedHeading.hidden = true; });

  copyBtn.addEventListener('click', async () => {
    const text = linkInput.value;
    let done = false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        done = true;
      }
    } catch (err) { done = false; }
    if (!done) {
      try { done = document.execCommand('copy'); } catch (err) { done = false; }
    }
    copyStatus.textContent = done
      ? 'Copied.'
      : 'Couldn\'t copy it automatically. The link is selected, so copy it by hand.';
    if (!done) { linkInput.focus(); linkInput.select(); }
  });

  /* ================= notice and remove ================= */
  const rmIds = { title: nextId('tf-rmtitle'), desc: nextId('tf-rmdesc') };
  const rmTitle = h('h2', { id: rmIds.title, class: 'tf-title', tabindex: '-1' });
  const rmDesc = h('p', { id: rmIds.desc, class: 'tf-lead' });
  const rmError = h('p', { class: 'field__error tf-alert', role: 'alert' });
  const rmCancel = h('button', { type: 'button', class: 'btn' }, 'Cancel');
  const rmGo = h('button', { type: 'button', class: 'btn btn--danger' }, 'Remove');
  const rmDlg = h('dialog', { class: 'dialog tf-dialog tf-dialog--small', 'aria-labelledby': rmIds.title, 'aria-describedby': rmIds.desc },
    h('div', { class: 'tf-form' },
      h('header', { class: 'tf-head' }, rmTitle),
      h('div', { class: 'tf-body' }, rmDesc, rmError),
      h('div', { class: 'tf-actions' }, h('span', { class: 'tf-spacer' }), rmCancel, rmGo)));
  document.body.append(rmDlg);
  closeOnBackdrop(rmDlg);

  let rmId = null;
  let rmOpener = null;
  let rmBusy = false;
  let rmCtrl = null;
  let rmSession = 0;

  function showRm() {
    rmOpener = document.activeElement && document.activeElement !== document.body ? document.activeElement : null;
    if (!rmDlg.open) { rmDlg.showModal(); lockScroll(); }
  }
  rmDlg.addEventListener('close', () => {
    unlockScroll();
    rmSession += 1;
    if (rmCtrl && rmBusy) { /* let a started delete finish; only the UI is gone */ }
    const o = rmOpener;
    rmOpener = null;
    const target = o && o.isConnected ? o : document.getElementById('openAdd');
    if (target && typeof target.focus === 'function') target.focus();
  });
  rmCancel.addEventListener('click', () => rmDlg.close());

  function notice(title, text) {
    rmId = null;
    rmTitle.textContent = title;
    rmDesc.textContent = text;
    rmError.textContent = '';
    rmGo.hidden = true;
    rmCancel.textContent = 'Close';
    showRm();
    rmCancel.focus();
  }

  function openConfirmRemove(tripId) {
    const trip = trips().find((t) => String(t.id) === String(tripId));
    if (!trip) {
      notice('That trip is gone', 'It is no longer on the board, so there is nothing to remove.');
      return;
    }
    rmId = String(tripId);
    const legs = (trip.out || []).concat(trip.ret || []);
    const first = legs[0] || {};
    const f = code(first, 'from'), t = code(first, 'to');
    const what = f && t ? `${f} to ${t}` : (first.flight_no || 'this trip');
    const when = prettyDate(first.date_local);
    rmTitle.textContent = `Remove ${what}${when ? ` on ${when}` : ''}?`;
    rmDesc.textContent = legs.length > 1
      ? `This takes the whole trip off the board, all ${legs.length} legs. This can\'t be undone.`
      : 'This takes the trip off the board. This can\'t be undone.';
    rmError.textContent = '';
    rmGo.hidden = false;
    rmGo.disabled = false;
    rmGo.textContent = 'Remove';
    rmCancel.textContent = 'Cancel';
    rmBusy = false;
    showRm();
    rmCancel.focus();
  }

  rmGo.addEventListener('click', async () => {
    if (rmBusy || !rmId) return;
    const id = rmId;
    const mine = rmSession;
    rmBusy = true;
    rmGo.disabled = true;
    rmGo.textContent = 'Removing…';
    rmDlg.setAttribute('aria-busy', 'true');
    rmError.textContent = '';
    const ctrl = new AbortController();
    rmCtrl = ctrl;
    let res;
    try {
      res = await request('DELETE', `api/trips/${id}`, undefined, { manageToken: manageToken(id) || undefined, signal: ctrl.signal });
    } catch (err) {
      res = { ok: false, status: 0, data: null, networkError: true };
    }
    rmBusy = false;
    rmCtrl = null;
    rmDlg.removeAttribute('aria-busy');
    const live = mine === rmSession;
    if (res && (res.ok || res.status === 404)) {
      forgetManage(id);
      try { onSaved(); } catch (err) { /* ignore */ }
      if (live) rmDlg.close();
      return;
    }
    if (!live) return;
    rmGo.disabled = false;
    rmGo.textContent = 'Remove';
    const st = res ? res.status : 0;
    if (!res || res.networkError || st === 0) rmError.textContent = 'Couldn\'t reach the server. The trip is still on the board.';
    else if (st === 403) rmError.textContent = 'This browser can no longer remove that trip. The edit link it was holding does not work any more.';
    else if (st === 429) rmError.textContent = 'That is a lot of changes from your connection. Try again in a few minutes.';
    else rmError.textContent = errorDetail(res.data).message || 'Couldn\'t remove it. Try again.';
  });

  return { openNew, openEdit, openConfirmRemove };
}

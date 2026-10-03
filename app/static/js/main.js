/* Hypertracker entry module: state, polling, board render, filters, theme, wiring.
   No innerHTML anywhere: every node is built with createElement and textContent. */
import { fetchTrips } from "./api.js";
import {
  parseStamp, parseUtc, todayAt, dayLabel, legStatus, relativeText, displayCode, plural, fmtIn,
} from "./format.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 60000;
const THEME_KEY = "wptk-theme";

/* ---------- config ---------- */
function readConfig() {
  const fallback = { user: null, isAdmin: false, discordEnabled: false, gated: false, basePath: "" };
  try {
    const el = $("app-config");
    return Object.assign(fallback, JSON.parse(el ? el.textContent : "{}"));
  } catch (_) {
    return fallback;
  }
}
const cfg = readConfig();

/* ---------- tiny DOM helper ---------- */
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else el.setAttribute(k, v === true ? "" : String(v));
    }
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function announce(msg) {
  const el = $("liveMsg");
  if (!el) return;
  el.textContent = "";
  setTimeout(() => { el.textContent = msg; }, 30);
}

/* ---------- theme ---------- */
let map = null;
function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}
function syncThemeUi() {
  const light = currentTheme() === "light";
  const label = $("themeLabel");
  if (label) {
    label.textContent = light ? "Light" : "Dark";
    const sr = label.parentElement && label.parentElement.querySelector(".sr-only");
    if (sr) sr.textContent = light ? " theme. Switch to dark." : " theme. Switch to light.";
  }
}
function setTheme(theme) {
  if (theme === "light") document.documentElement.setAttribute("data-theme", "light");
  else document.documentElement.removeAttribute("data-theme");
  try { localStorage.setItem(THEME_KEY, theme); } catch (_) { /* storage blocked */ }
  syncThemeUi();
  if (map) { try { map.setTheme(theme); } catch (_) { /* map must never break the page */ } }
}
const themeToggle = $("themeToggle");
if (themeToggle) themeToggle.addEventListener("click", () => setTheme(currentTheme() === "light" ? "dark" : "light"));
syncThemeUi();

/* ---------- time zone: every time is shown in the viewer's own zone ---------- */
let deviceTz = "UTC";
try { deviceTz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch (_) { /* keep UTC */ }

/* ---------- auth messages ---------- */
const AUTH_MESSAGES = {
  not_member: "That Discord account isn't in the server.",
  denied: "You cancelled the Discord login. Nothing changed.",
  error: "Discord login failed. Try again in a minute.",
  unavailable: "Discord login isn't set up on this board.",
};
function showNotice(text) {
  const el = $("authNote");
  if (!el) return;
  el.textContent = text;
  el.hidden = false;
}
try {
  const auth = new URLSearchParams(location.search).get("auth");
  if (auth && Object.prototype.hasOwnProperty.call(AUTH_MESSAGES, auth)) showNotice(AUTH_MESSAGES[auth]);
} catch (_) { /* no query string support: ignore */ }

/* ---------- state ---------- */
const state = {
  data: null,        // last good /api/trips payload
  etag: null,
  skew: 0,           // server clock minus client clock, ms
  liveUpdatedAt: null, // epoch seconds of the last live-status poll
  lastOk: 0,
  filter: { tok: null, date: null },
  view: "board",
};
let identity = null;
let tripForm = null;

const boardEl = $("board");

function nowMs() { return Date.now() + state.skew; }

/* ---------- start ---------- */
async function start() {
  wireStaticHandlers();
  const [idRes, formRes] = await Promise.allSettled([import("./identity.js"), import("./tripForm.js")]);
  if (idRes.status === "fulfilled") {
    identity = idRes.value;
    try {
      const seeded = identity.seedFromFragment && identity.seedFromFragment();
      if (seeded) {
        showNotice("Manage link saved. This browser can now edit that trip.");
        announce("Manage link saved. This browser can now edit that trip.");
      }
    } catch (_) { /* bad fragment: ignore */ }
  }
  if (formRes.status === "fulfilled") {
    try {
      tripForm = formRes.value.createTripForm({
        user: cfg.user,
        getTrips: () => (state.data ? state.data.trips : []),
        onSaved: () => { renderBoard(true); load({ force: true }); },
      });
    } catch (_) { tripForm = null; }
  }
  load();
  setInterval(refreshTimes, 30000);
}

/* ---------- identity helpers ---------- */
function isAdmin() { return !!((state.data && state.data.is_admin) || cfg.isAdmin); }
function canManage(ownerId, tripId) {
  const me = state.data ? state.data.me : null;
  try {
    if (identity && identity.canManage) return !!identity.canManage(ownerId, tripId, { me, isAdmin: isAdmin() });
  } catch (_) { /* fall through */ }
  return isAdmin() || (!!me && me === ownerId);
}

/* ---------- loading and polling ---------- */
let inflight = null;
let ctrl = null;
let timer = null;
let fails = 0;
let lastAttempt = 0;
let loaded = false;
let stale = false;

function dialogOpen() { return !!document.querySelector("dialog[open]"); }

function schedule(ms) {
  clearTimeout(timer);
  timer = null;
  if (document.hidden) return;
  timer = setTimeout(tick, ms == null ? nextDelay() : ms);
}
function nextDelay() {
  if (!loaded && fails > 0) return Math.min(60000, 5000 * 2 ** (fails - 1));
  return POLL_MS;
}
function tick() {
  timer = null;
  if (document.hidden) return;
  if (dialogOpen()) { schedule(3000); return; }
  load();
}

function load({ force = false } = {}) {
  if (inflight && !force) return inflight;
  if (ctrl) ctrl.abort();
  const mine = new AbortController();
  ctrl = mine;
  lastAttempt = Date.now();
  inflight = (async () => {
    try {
      const res = await fetchTrips({ etag: force ? null : state.etag, signal: mine.signal });
      if (mine.signal.aborted) return;
      onSuccess(res);
    } catch (err) {
      if (err && err.aborted) return;
      onFailure(err);
    } finally {
      if (ctrl === mine) { inflight = null; ctrl = null; schedule(); }
    }
  })();
  return inflight;
}

function onSuccess(res) {
  fails = 0;
  state.lastOk = Date.now();
  if (stale) { stale = false; showStale(false); announce("The board is up to date again."); }
  const serverTime = res.serverTime != null ? res.serverTime : (!res.notModified ? res.data.server_time : null);
  if (typeof serverTime === "number") state.skew = serverTime * 1000 - Date.now();
  if (res.liveUpdatedAt != null) state.liveUpdatedAt = res.liveUpdatedAt;
  if (!res.notModified) {
    state.data = res.data;
    state.etag = res.etag;
    if (state.liveUpdatedAt == null) state.liveUpdatedAt = res.data.live_updated_at;
    if (identity && identity.pruneManage) {
      try { identity.pruneManage(new Set(res.data.trips.map((t) => t.id))); } catch (_) { /* ignore */ }
    }
  }
  loaded = true;
  renderBoard(false);
  renderStamp();
}

function onFailure(err) {
  fails += 1;
  if (!loaded) {
    renderLoadError();
    return;
  }
  if (!stale) announce("Could not refresh the board. Showing the last data.");
  stale = true;
  showStale(true);
}

function showStale(on) {
  const banner = $("staleBanner");
  if (!banner) return;
  banner.hidden = !on;
  if (on) {
    const t = state.lastOk ? new Date(state.lastOk).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "earlier";
    $("staleText").textContent = `Could not refresh the board. Showing data from ${t}. Retrying automatically.`;
  }
}

function renderStamp() {
  const el = $("stamp");
  if (!el || !state.lastOk) return;
  const fmt = (ms) => new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  el.textContent = `Updated ${fmt(state.lastOk)}.`;
}

function renderLoadError() {
  const panel = $("boardPanel");
  if (panel) panel.setAttribute("aria-busy", "false");
  boardEl.replaceChildren(
    h("div", { class: "hub-card errorstate", role: "alert" },
      h("h2", { class: "hub-title", text: "Could not load the board" }),
      h("button", { type: "button", class: "btn btn--primary", "data-action": "retry", "data-key": "retry", text: "Try again" })),
  );
  setFilterText("The board isn't loaded.");
}

/* ---------- view model ---------- */
let lastSig = "";
let legRecs = [];     // {el, leg, dayEl, depEl, arrEl, relEl, statusEl, cont, ...}
let airRecs = [];     // {leg, relEl}

function buildPeople(trips) {
  const people = [];
  const byOwner = new Map();
  const now = nowMs();
  for (const t of trips) {
    let p = byOwner.get(t.owner_id);
    if (!p) { p = { id: t.owner_id, name: t.owner_name || "Someone", trips: [] }; byOwner.set(t.owner_id, p); people.push(p); }
    const journeys = [];
    if (t.out && t.out.length) journeys.push({ dir: "out", legs: t.out });
    if (t.ret && t.ret.length) journeys.push({ dir: "ret", legs: t.ret });
    if (!journeys.length) continue;
    const first = journeys[0].legs[0];
    p.trips.push({
      id: t.id, journeys, manage: canManage(t.owner_id, t.id),
      first: parseUtc(first.dep_utc) ?? (first.date_local ? Date.parse(first.date_local + "T00:00:00Z") : Infinity),
      summary: tripSummary(first),
    });
  }
  for (const p of people) {
    p.trips.sort((a, b) => a.first - b.first);
    const legs = p.trips.flatMap((t) => t.journeys.flatMap((j) => j.legs));
    p.airborne = legs.some((l) => l.live_state === "airborne");
    let next = Infinity;
    for (const l of legs) {
      const arr = parseUtc(l.arr_utc);
      if (arr != null && now >= arr) continue;
      const dep = parseUtc(l.dep_utc);
      next = Math.min(next, dep ?? (l.date_local ? Date.parse(l.date_local + "T00:00:00Z") : Infinity));
    }
    p.next = p.airborne ? -Infinity : next;
  }
  people.sort((a, b) => (a.next === b.next ? a.name.localeCompare(b.name) : a.next < b.next ? -1 : 1));
  return people.filter((p) => p.trips.length);
}

function tripSummary(leg) {
  const a = displayCode(leg.from_iata, leg.from);
  const b = displayCode(leg.to_iata, leg.to);
  if (a && b) return `${a} to ${b}`;
  return leg.flight_no || "trip";
}

/* ---------- rendering ---------- */
function activeKey() {
  const a = document.activeElement;
  if (!a || !a.closest) return null;
  const inside = a.closest("#boardPanel, #dayChips");
  if (!inside) return null;
  return a.getAttribute("data-key");
}
function findByKey(key) {
  for (const el of document.querySelectorAll("[data-key]")) if (el.getAttribute("data-key") === key) return el;
  return null;
}

function renderBoard(force) {
  if (!state.data) return;
  const people = buildPeople(state.data.trips);
  const sig = JSON.stringify([
    state.data.trips, state.data.me, isAdmin(), people.map((p) => p.trips.map((t) => t.manage)),
    people.map((p) => p.id),
  ]);
  const panel = $("boardPanel");
  if (panel) panel.setAttribute("aria-busy", "false");
  if (!force && sig === lastSig) { refreshTimes(); return; }
  lastSig = sig;

  const key = activeKey();
  legRecs = [];
  airRecs = [];
  const frag = document.createDocumentFragment();
  if (!people.length) {
    frag.append(emptyState());
  } else {
    people.forEach((p, i) => frag.append(personCard(p, i % 3)));
  }
  boardEl.replaceChildren(frag);
  renderAirborne();
  renderDayChips();
  if (key) {
    const again = findByKey(key);
    if (again) again.focus({ preventScroll: true });
    else { const add = $("openAdd"); if (add) add.focus({ preventScroll: true }); }
  }
  applyFilter({ fromRender: true });
  refreshTimes();
}

function emptyState() {
  return h("div", { class: "hub-card empty" },
    h("h2", { class: "hub-title", text: "Nobody is flying right now" }),
    h("button", { type: "button", class: "btn btn--primary", "data-action": "add", "data-key": "add-empty", text: "Add a trip" }));
}

function personCard(p, accent) {
  const card = h("article", { class: "person wptk-section", "data-accent": accent, "aria-labelledby": `p-${cssId(p.id)}` });
  const head = h("div", { class: "person__head" },
    h("h3", { class: "person__name", id: `p-${cssId(p.id)}`, text: p.name }),
    p.airborne ? h("span", { class: "status status--airborne", text: "In the air" }) : null);
  card.append(head);
  for (const trip of p.trips) {
    const tripEl = h("div", { class: "trip" });
    const bar = h("div", { class: "trip__bar" });
    if (trip.manage) {
      bar.append(h("div", { class: "trip__tools" },
        h("button", {
          type: "button", class: "btn btn--sm", "data-action": "edit", "data-trip": trip.id,
          "data-key": `edit:${trip.id}`, "aria-label": `Edit ${p.name}'s trip, ${trip.summary}`, text: "Edit",
        }),
        h("button", {
          type: "button", class: "btn btn--sm btn--danger", "data-action": "remove", "data-trip": trip.id,
          "data-key": `remove:${trip.id}`, "aria-label": `Remove ${p.name}'s trip, ${trip.summary}`, text: "Remove",
        })));
    }
    if (bar.childNodes.length) tripEl.append(bar);
    const both = trip.journeys.length > 1;
    for (const j of trip.journeys) {
      if (both || j.dir === "ret") tripEl.append(h("p", { class: "journey__label", text: j.dir === "ret" ? "Return" : "Outbound" }));
      const ol = h("ol", { class: "legs" });
      j.legs.forEach((leg, idx) => ol.append(legEl(leg, j.legs[idx - 1], p, trip)));
      tripEl.append(ol);
    }
    card.append(tripEl);
  }
  return card;
}

function cssId(s) { return String(s).replace(/[^a-zA-Z0-9_-]/g, "_"); }

function tok(value, kind, legKey) {
  return h("button", {
    type: "button", class: `tok${kind === "flight" ? " tok--flight" : ""}`,
    "data-tok": value, "data-key": `tok:${legKey}:${kind}:${value}`,
    "aria-pressed": "false", "aria-label": `Highlight ${value}`, text: value,
  });
}

function placeName(city, name) { return city || name || ""; }

function legEl(leg, prev, person, trip) {
  const legKey = `${trip.id}-${leg.direction}-${leg.seq}`;
  const dep = parseStamp(leg.dep_local);
  const arr = parseStamp(leg.arr_local);
  const dateStr = leg.date_local || (dep && dep.date) || "";
  const cont = !!(prev && prev.date_local && prev.date_local === leg.date_local);

  const dayEl = h("span", { class: "leg__day" });
  const depEl = h("span", { class: "leg__dep" });
  const when = h("div", { class: "leg__when" }, dayEl, depEl);

  /* route cell */
  const route = h("div", { class: "leg__route" });
  if (leg.flight_no || leg.callsign) {
    const line = h("div", { class: "leg__flightline" });
    if (leg.flight_no) line.append(tok(leg.flight_no, "flight", legKey));
    if (leg.callsign && leg.callsign !== leg.flight_no) {
      line.append(h("span", { class: "leg__callsign" }, h("span", { class: "sr-only", text: "callsign " }), leg.callsign));
    }
    route.append(line);
  }
  const fromCode = displayCode(leg.from_iata, leg.from);
  const toCode = displayCode(leg.to_iata, leg.to);
  if (fromCode || toCode) {
    route.append(h("div", { class: "route" },
      fromCode ? tok(fromCode, "airport", legKey + "-from") : h("span", { class: "route__code", text: "?" }),
      h("span", { class: "route__arrow", "aria-hidden": "true", text: "→" }),
      h("span", { class: "sr-only", text: "to" }),
      toCode ? tok(toCode, "airport", legKey + "-to") : h("span", { class: "route__code", text: "?" })));
    const a = placeName(leg.from_city, leg.from_name);
    const b = placeName(leg.to_city, leg.to_name);
    if (a || b) route.append(h("p", { class: "leg__names", text: `${a || "Unknown"} to ${b || "unknown"}` }));
  } else {
    route.append(h("p", { class: "leg__note", text: leg.unverified
      ? "This flight is not in the schedule, so the airports are unconfirmed."
      : "Airports not confirmed yet." }));
  }

  /* arrival cell */
  const arrEl = h("span", { class: "leg__arrline" });
  const relEl = h("span", { class: "leg__rel" });
  const arrCell = h("div", { class: "leg__arr" }, arrEl, relEl);

  /* aircraft cell */
  const bits = [];
  const model = leg.ac_model || leg.ac_type;
  if (model) bits.push(model);
  if (leg.ac_age != null) bits.push(`${leg.ac_age} ${plural(leg.ac_age, "year", "years")} old`);
  if (leg.reg) bits.push(leg.reg);
  let acText;
  if (bits.length) acText = bits.join(" · ");
  else if (leg.manual) acText = "Entered by hand";
  else if (leg.unverified) acText = "Not confirmed against the schedule";
  else acText = "Aircraft not assigned yet";
  const ac = h("div", { class: "leg__ac" }, h("span", { class: "sr-only", text: "Aircraft: " }), acText);

  /* side cell */
  const statusEl = h("span", { class: "status" });
  const side = h("div", { class: "leg__side" }, statusEl);
  const ident = leg.flight_no || leg.callsign;
  if (ident && typeof leg.fa_url === "string" && /^https:\/\//.test(leg.fa_url)) {
    side.append(h("a", {
      class: "btn btn--sm leg__link", href: leg.fa_url, target: "_blank", rel: "noopener noreferrer",
      "data-key": `fa:${legKey}`, "aria-label": `FlightAware for ${ident}`, text: "FlightAware",
    }));
  }

  const el = h("li", { class: "leg", "data-leg": legKey }, when, route, arrCell, ac, side);
  const rec = { el, leg, dayEl, depEl, arrEl, relEl, statusEl, dep, arr, dateStr, cont, person: person.name };
  legRecs.push(rec);
  return el;
}

/* "In the air now" strip */
function renderAirborne() {
  const sec = $("airborne");
  const list = $("airborneList");
  if (!sec || !list) return;
  const items = [];
  for (const rec of legRecs) {
    if (rec.leg.live_state !== "airborne") continue;
    const rel = h("span", { class: "airborne__rel" });
    const l = rec.leg;
    items.push(h("li", { class: "airborne__item" },
      h("span", { class: "airborne__who", text: rec.person }), " ",
      l.flight_no ? `${l.flight_no}, ` : "",
      h("span", { class: "airborne__route", text: `${displayCode(l.from_iata, l.from) || "?"} → ${displayCode(l.to_iata, l.to) || "?"}` }),
      rel));
    airRecs.push({ leg: l, relEl: rel });
  }
  list.replaceChildren(...items);
  sec.hidden = !items.length;
}

/* Recompute time-dependent text without rebuilding the board. */
function setText(el, text) { if (el.textContent !== text) el.textContent = text; }
function refreshTimes() {
  const now = nowMs();
  for (const r of legRecs) {
    const { leg } = r;
    const show = (st) => (st && st.offMin != null ? fmtIn(st.ms, deviceTz) : st);
    const dep = show(r.dep), arr = show(r.arr);
    const today = fmtIn(now, deviceTz).date;
    const dayStr = dep && r.dep.offMin != null ? dep.date : r.dateStr;
    setText(r.dayEl, r.cont ? "Connecting" : (dayLabel(dayStr, today) || "Date not set"));
    r.dayEl.classList.toggle("is-cont", r.cont);
    r.el.classList.toggle("is-continuation", r.cont);
    setText(r.depEl, dep ? `Departs ${dep.time}` : "Time not set");
    if (arr) {
      const sameDay = !dep || arr.date === dep.date;
      const prefix = sameDay ? "" : dayLabel(arr.date, today) + " ";
      setText(r.arrEl, `Arrives ${prefix}${arr.time}`);
    } else {
      setText(r.arrEl, leg.manual ? "Arrival time not set" : "Arrival time not known yet");
    }
    setText(r.relEl, relativeText(leg, now));
    const st = legStatus(leg, now);
    const cls = `status status--${st.key}`;
    if (r.statusEl.className !== cls) r.statusEl.className = cls;
    setText(r.statusEl, st.label);
  }
  for (const a of airRecs) setText(a.relEl, relativeText(a.leg, now));
}

/* ---------- day chips ---------- */
function renderDayChips() {
  const wrap = $("dayChips");
  if (!wrap) return;
  const dates = [...new Set(legRecs.map((r) => r.dateStr).filter(Boolean))].sort();
  wrap.dataset.count = String(dates.length);
  const now = nowMs();
  wrap.replaceChildren(
    h("span", { class: "daychips__label", "aria-hidden": "true", text: "Day" }),
    ...dates.map((d) => h("button", {
      type: "button", class: "chip", "data-date": d, "data-key": `chip:${d}`, "aria-pressed": "false",
      text: dayLabel(d, todayAt(now, null)),
    })));
  syncChips();
}
function syncChips() {
  const wrap = $("dayChips");
  if (!wrap) return;
  const n = Number(wrap.dataset.count || 0);
  wrap.hidden = n < 2 || state.view === "map";
  for (const b of wrap.querySelectorAll("button.chip")) {
    const on = state.filter.date === b.getAttribute("data-date");
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.classList.toggle("is-active", on);
  }
}

/* ---------- filter ---------- */
function legMatches(leg, f) {
  if (f.tok) {
    const t = f.tok.toUpperCase();
    return [leg.flight_no, leg.from, leg.from_iata, leg.to, leg.to_iata].some((v) => v && String(v).toUpperCase() === t);
  }
  if (f.date) return leg.date_local === f.date;
  return true;
}

function setFilterText(...nodes) {
  const el = $("filterStatus");
  if (el) el.replaceChildren(...nodes);
}

function applyFilter({ fromRender = false } = {}) {
  const f = state.filter;
  const active = !!(f.tok || f.date);
  let hits = 0;
  for (const r of legRecs) {
    const m = legMatches(r.leg, f);
    if (m) hits += 1;
    r.el.classList.toggle("is-hit", active && m);
    r.el.classList.toggle("is-dim", active && !m);
  }
  if (active && hits === 0) {
    const what = f.tok || dayLabel(f.date, todayAt(nowMs(), null));
    state.filter = { tok: null, date: null };
    announceFilterReset(what);
    applyFilter({ fromRender });
    return;
  }
  for (const b of boardEl.querySelectorAll("button.tok")) {
    const on = !!f.tok && b.getAttribute("data-tok").toUpperCase() === f.tok.toUpperCase();
    b.setAttribute("aria-pressed", on ? "true" : "false");
  }
  syncChips();
  const clear = $("clearFilter");
  if (clear) clear.hidden = !active;
  const total = legRecs.length;
  if (!state.data) setFilterText("Loading the board.");
  else if (!total) setFilterText("Nothing on the board yet.");
  else if (!active) setFilterText("");
  else {
    const label = f.tok ? h("strong", { text: f.tok }) : h("strong", { text: dayLabel(f.date, todayAt(nowMs(), null)) });
    setFilterText(f.tok ? "Highlighting " : "Showing ", label,
      `. ${hits} of ${total} ${plural(total, "flight", "flights")} ${plural(hits, "matches", "match")}.`);
  }
  syncMap();
}
function announceFilterReset(what) {
  setFilterText(`Nothing matches ${what} any more. Filter cleared.`);
  announce(`Nothing matches ${what} any more. Filter cleared.`);
}

function setFilter(next) {
  state.filter = next;
  applyFilter();
}
function toggleTok(code) {
  const cur = state.filter.tok;
  setFilter(cur && cur.toUpperCase() === code.toUpperCase() ? { tok: null, date: null } : { tok: code, date: null });
}
function toggleDate(d) {
  setFilter(state.filter.date === d ? { tok: null, date: null } : { tok: null, date: d });
}

/* ---------- map ---------- */
let mapMod = null;
let mapDirty = true;

function allLegs() {
  const out = [];
  if (state.data) for (const t of state.data.trips) out.push(...(t.out || []), ...(t.ret || []));
  return out;
}
/* The map matches airport filters on ICAO codes, so translate a displayed IATA code. */
function mapFilter() {
  const f = state.filter;
  let tok = f.tok;
  if (tok) {
    const up = tok.toUpperCase();
    for (const l of allLegs()) {
      if (l.from_iata && l.from_iata.toUpperCase() === up && l.from) { tok = l.from; break; }
      if (l.to_iata && l.to_iata.toUpperCase() === up && l.to) { tok = l.to; break; }
    }
  }
  return { tok, date: f.date };
}
/* The map hands back ICAO codes or flight numbers; show what the board shows. */
function displayFor(code) {
  const up = String(code).toUpperCase();
  for (const l of allLegs()) {
    if (l.from && l.from.toUpperCase() === up) return displayCode(l.from_iata, l.from);
    if (l.to && l.to.toUpperCase() === up) return displayCode(l.to_iata, l.to);
  }
  return String(code);
}

function syncMap() {
  if (!map || !state.data) { mapDirty = true; return; }
  if (state.view !== "map") { mapDirty = true; return; }
  try { map.update(state.data.trips, mapFilter()); mapDirty = false; } catch (_) { /* ignore */ }
}

async function ensureMap() {
  if (map) return map;
  try {
    mapMod = mapMod || await import("./map.js");
    map = mapMod.createMap($("map"), {
      onSelectCode: (code) => { if (code) toggleTok(displayFor(code)); },
      onSelectDate: (d) => {
        const cur = state.filter.date;
        if ((d || null) !== cur) setFilter(d ? { tok: null, date: d } : { tok: null, date: null });
      },
    });
    map.setTheme(currentTheme());
  } catch (_) {
    map = null;
    const panel = $("mapPanel");
    if (panel && !panel.querySelector(".notice")) {
      panel.append(h("p", { class: "notice", role: "alert", text: "The map didn't load. The board still works." }));
    }
  }
  return map;
}

async function setView(view) {
  state.view = view;
  const isMap = view === "map";
  $("viewBoard").setAttribute("aria-pressed", String(!isMap));
  $("viewMap").setAttribute("aria-pressed", String(isMap));
  $("boardPanel").hidden = isMap;
  $("airborne").hidden = isMap || !airRecs.length;
  $("mapPanel").hidden = !isMap;
  syncChips();
  if (isMap) {
    const m = await ensureMap();
    if (m && state.view === "map") {
      try { m.setTheme(currentTheme()); m.show(); } catch (_) { /* ignore */ }
      syncMap();
    }
  } else if (map) {
    try { map.hide(); } catch (_) { /* ignore */ }
  }
}

/* ---------- static handlers ---------- */
function wireStaticHandlers() {
  $("viewBoard").addEventListener("click", () => setView("board"));
  $("viewMap").addEventListener("click", () => setView("map"));
  $("clearFilter").addEventListener("click", () => {
    setFilter({ tok: null, date: null });
    /* Clear hides itself, so hand focus to a stable control. */
    const first = boardEl.querySelector("button.tok");
    if (first) first.focus({ preventScroll: true });
    else $("openAdd").focus();
  });
  $("retryNow").addEventListener("click", () => { announce("Trying again."); load({ force: true }); });
  $("dayChips").addEventListener("click", (e) => {
    const b = e.target.closest("button.chip");
    if (b) toggleDate(b.getAttribute("data-date"));
  });
  $("openAdd").addEventListener("click", openAdd);

  boardEl.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || !boardEl.contains(b)) return;
    if (b.classList.contains("tok")) { toggleTok(b.getAttribute("data-tok")); return; }
    const action = b.getAttribute("data-action");
    if (action === "add") openAdd();
    else if (action === "retry") { announce("Trying again."); load({ force: true }); }
    else if (action === "edit" || action === "remove") {
      const id = Number(b.getAttribute("data-trip"));
      if (!tripForm) { announce("The trip form didn't load. Reload the page and try again."); return; }
      try {
        if (action === "edit") tripForm.openEdit(id);
        else tripForm.openConfirmRemove(id);
      } catch (_) { announce("Something went wrong opening that. Reload the page and try again."); }
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { clearTimeout(timer); timer = null; return; }
    const age = Date.now() - lastAttempt;
    if (age >= POLL_MS) load();
    else schedule(POLL_MS - age);
    refreshTimes();
  });
  /* A dialog closing can have held a poll back: catch up if one is overdue. */
  document.addEventListener("close", (e) => {
    if (e.target && e.target.tagName === "DIALOG" && !document.hidden && Date.now() - lastAttempt >= POLL_MS) load();
  }, true);
}

function openAdd() {
  if (!tripForm) { announce("The trip form didn't load. Reload the page and try again."); return; }
  try { tripForm.openNew(); } catch (_) { announce("Something went wrong opening the form. Reload the page and try again."); }
}

/* Members-only page or login page: no board to wire. */
if (!cfg.gated && boardEl) start();

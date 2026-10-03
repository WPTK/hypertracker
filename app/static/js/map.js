/* Route map (Leaflet, vendored). Standalone ES module.
 *
 *   createMap(containerEl, { onSelectCode(code), onSelectDate?(date|null) })
 *     -> { update(trips, filter), setTheme('dark'|'light'), show(), hide(), destroy() }
 *
 * filter = { tok: string|null, date: string|null }.
 * Leaflet is loaded lazily on the first show(). Any failure renders an in-page
 * message with a Retry button; nothing here ever throws into the caller.
 */

const LEAFLET_JS = new URL("../vendor/leaflet/leaflet.js", import.meta.url).href;
const LEAFLET_CSS = new URL("../vendor/leaflet/leaflet.css", import.meta.url).href;
const TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const WORLD_OFFSETS = [-360, 0, 360];
const TILE_ERR_COUNT = 8;
const TILE_ERR_WINDOW_MS = 10000;
const LOAD_TIMEOUT_MS = 15000;

/* ---------- geometry (pure; exported for tests) ---------- */

const rad = (d) => (d * Math.PI) / 180;
const deg = (r) => (r * 180) / Math.PI;

/* Great-circle path as ONE continuous polyline. Longitudes are unwrapped so that
   consecutive points never differ by more than 180 degrees (an arc crossing the
   dateline simply continues past +-180). Callers draw it at +-360 offsets so every
   world copy shows it. Returns [] for identical endpoints (nothing to draw). */
export function gcPath(lat1, lon1, lat2, lon2) {
  const p1 = [rad(lat1), rad(lon1)];
  const p2 = [rad(lat2), rad(lon2)];
  const h = Math.pow(Math.sin((p1[0] - p2[0]) / 2), 2) +
    Math.cos(p1[0]) * Math.cos(p2[0]) * Math.pow(Math.sin((p1[1] - p2[1]) / 2), 2);
  const d = 2 * Math.asin(Math.min(1, Math.sqrt(h)));
  if (!(d > 1e-9)) return [];
  let pts;
  if (Math.PI - d < 1e-6) {
    /* Antipodal: every meridian is a great circle; route over the north pole. */
    pts = [[lat1, lon1], [89.9, lon1], [89.9, lon2], [lat2, lon2]];
  } else {
    const n = Math.max(8, Math.min(128, Math.ceil(deg(d) / 2)));
    const sd = Math.sin(d);
    pts = [];
    for (let i = 0; i <= n; i++) {
      const f = i / n;
      const A = Math.sin((1 - f) * d) / sd, B = Math.sin(f * d) / sd;
      const x = A * Math.cos(p1[0]) * Math.cos(p1[1]) + B * Math.cos(p2[0]) * Math.cos(p2[1]);
      const y = A * Math.cos(p1[0]) * Math.sin(p1[1]) + B * Math.cos(p2[0]) * Math.sin(p2[1]);
      const z = A * Math.sin(p1[0]) + B * Math.sin(p2[0]);
      pts.push([deg(Math.atan2(z, Math.sqrt(x * x + y * y))), deg(Math.atan2(y, x))]);
    }
  }
  let prev = null;
  return pts.map((pt) => {
    let lon = pt[1];
    if (prev !== null) {
      while (lon - prev > 180) lon -= 360;
      while (lon - prev < -180) lon += 360;
    }
    prev = lon;
    return [pt[0], lon];
  });
}

/* ---------- Leaflet loader (shared, retryable) ---------- */

let leafletPromise = null;

function loadLeaflet() {
  if (typeof window !== "undefined" && window.L && window.L.map) return Promise.resolve(window.L);
  if (leafletPromise) return leafletPromise;
  leafletPromise = new Promise((resolve, reject) => {
    let done = false;
    const fail = (why) => { if (!done) { done = true; leafletPromise = null; reject(new Error(why)); } };
    try {
      if (!document.querySelector("link[data-hmap-leaflet]")) {
        const css = document.createElement("link");
        css.rel = "stylesheet"; css.href = LEAFLET_CSS; css.setAttribute("data-hmap-leaflet", "");
        document.head.appendChild(css);
      }
      document.querySelectorAll("script[data-hmap-leaflet]").forEach((s) => s.remove());
      const s = document.createElement("script");
      s.src = LEAFLET_JS; s.async = true; s.setAttribute("data-hmap-leaflet", "");
      s.onload = () => {
        if (done) return;
        if (window.L && window.L.map) { done = true; resolve(window.L); } else fail("Leaflet missing after load");
      };
      s.onerror = () => fail("Leaflet script failed to load");
      document.head.appendChild(s);
      setTimeout(() => fail("Leaflet load timed out"), LOAD_TIMEOUT_MS);
    } catch (e) { fail(String(e)); }
  });
  return leafletPromise;
}

/* ---------- module ---------- */

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const finite = (v) => typeof v === "number" && isFinite(v);

export function createMap(containerEl, opts) {
  opts = opts || {};
  const onSelectCode = typeof opts.onSelectCode === "function" ? opts.onSelectCode : () => {};
  const onSelectDate = typeof opts.onSelectDate === "function" ? opts.onSelectDate : null;
  const reduceMotion = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  let L = null, map = null, tiles = null, arcLayer = null, pinLayer = null;
  let destroyed = false, visible = false, loading = false;
  let theme = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  let colors = null;
  let trips = [], filter = { tok: null, date: null };
  let lastFilterKey = "";
  let userMoved = false, fitting = false, lastFitCodes = null, needFit = true;
  let tileErrs = [], dateKey = "", srKey = "";
  const arcs = new Map();     // key -> {leg, line, geo, path, styleKey, label}
  const pins = new Map();     // ICAO -> {markers:[...], lat, lon, label, lit, pressed}

  /* DOM scaffold */
  containerEl.classList.add("hmap");
  containerEl.setAttribute("data-theme", theme);
  containerEl.hidden = true;
  containerEl.textContent = "";
  const bar = el("div", "hmap__bar");
  const chipsEl = el("div", "hmap__chips");
  chipsEl.setAttribute("role", "group");
  chipsEl.setAttribute("aria-label", "Highlight flights by date");
  const fitBtn = el("button", "btn btn--sm hmap__fit", "Fit all");
  fitBtn.type = "button";
  bar.append(chipsEl, fitBtn);
  const stage = el("div", "hmap__stage");
  const mapEl = el("div", "hmap__map");
  mapEl.setAttribute("aria-label", "Map of flight routes. The date buttons above and the airport filters on the board give the same information as a list.");
  const emptyEl = el("p", "hmap__empty", "No routes to show yet.");
  emptyEl.hidden = true;
  const msgEl = el("div", "hmap__msg");
  msgEl.hidden = true;
  const noticeEl = el("p", "hmap__notice", "Map tiles aren't loading right now. Routes and markers still work.");
  noticeEl.setAttribute("role", "status");
  noticeEl.hidden = true;
  stage.append(mapEl, emptyEl, msgEl, noticeEl);
  const legend = el("ul", "hmap__legend");
  legend.setAttribute("aria-label", "Legend");
  [["airborne", "Airborne"], ["ground", "On ground"], ["scheduled", "Scheduled"], ["selected", "Selected"]].forEach(([k, t]) => {
    const li = el("li", "hmap__key");
    li.innerHTML = '<svg class="hmap__swatch hmap__swatch--' + k + '" width="34" height="10" viewBox="0 0 34 10" aria-hidden="true" focusable="false"><line x1="2" y1="5" x2="32" y2="5"/></svg>';
    li.appendChild(el("span", null, t));
    legend.appendChild(li);
  });
  const srList = el("ul", "hmap-sr");
  srList.setAttribute("aria-label", "Flights shown on the map");
  containerEl.append(bar, stage, legend, srList);

  /* ---- colours: read once per theme change, never per style pass ---- */
  function readColors() {
    const cs = getComputedStyle(containerEl);
    const v = (n, f) => (cs.getPropertyValue(n).trim() || f);
    colors = {
      airborne: v("--hm-airborne", "#3fc9b0"),
      ground: v("--hm-ground", "#b58cf0"),
      scheduled: v("--hm-scheduled", "#6f95f5"),
      selected: v("--hm-selected", "#ffffff"),
    };
  }

  /* ---- messages ---- */
  function showMsg(text, withRetry) {
    msgEl.textContent = "";
    msgEl.setAttribute("role", "status");
    msgEl.appendChild(el("p", null, text));
    if (withRetry) {
      const b = el("button", "btn btn--sm", "Retry");
      b.type = "button";
      b.addEventListener("click", () => { if (visible) boot(); });
      msgEl.appendChild(b);
    }
    msgEl.hidden = false;
    mapEl.hidden = true;
  }
  function hideMsg() { msgEl.hidden = true; mapEl.hidden = false; }

  /* ---- boot ---- */
  function boot() {
    if (destroyed || map || loading) return;
    loading = true;
    showMsg("Loading map...", false);
    loadLeaflet().then((Lib) => {
      loading = false;
      if (destroyed) return;
      L = Lib;
      try { initMap(); hideMsg(); syncAll(); doShowWork(); }
      catch (e) { teardownMap(); showMsg("The map couldn't load. The board still works.", true); }
    }).catch(() => {
      loading = false;
      if (destroyed) return;
      showMsg("The map couldn't load. The board still works.", true);
    });
  }

  function initMap() {
    map = L.map(mapEl, {
      worldCopyJump: true, zoomControl: true, attributionControl: true,
      minZoom: 0, zoomAnimation: !reduceMotion, fadeAnimation: !reduceMotion, markerZoomAnimation: !reduceMotion,
    }).setView([30, 0], 2);
    mapEl.setAttribute("role", "region");
    tiles = L.tileLayer(TILE_URL, { attribution: ATTRIBUTION, maxZoom: 19 }).addTo(map);
    tiles.on("tileerror", onTileError);
    tiles.on("tileload", onTileLoad);
    arcLayer = L.layerGroup().addTo(map);
    pinLayer = L.layerGroup().addTo(map);
    map.on("movestart", () => { if (!fitting) userMoved = true; });
    readColors();
  }

  function teardownMap() {
    try { if (map) map.remove(); } catch (e) { /* ignore */ }
    map = null; tiles = null; arcLayer = null; pinLayer = null;
    arcs.clear(); pins.clear(); lastFitCodes = null; needFit = true;
  }

  function onTileError() {
    const now = Date.now();
    tileErrs = tileErrs.filter((t) => now - t < TILE_ERR_WINDOW_MS);
    tileErrs.push(now);
    if (tileErrs.length >= TILE_ERR_COUNT) noticeEl.hidden = false;
  }
  function onTileLoad() { tileErrs = []; noticeEl.hidden = true; }

  /* ---- filtering ---- */
  function matches(leg) {
    if (filter.tok) return leg.flight_no === filter.tok || leg.from === filter.tok || leg.to === filter.tok;
    if (filter.date) return leg.date_local === filter.date;
    return true;
  }
  const isActive = () => !!(filter.tok || filter.date);

  /* ---- styling (diffed; only touches layers whose style actually changed) ---- */
  function arcStyle(leg) {
    const st = leg.live_state;
    let color, weight, dash, opacity;
    if (st === "airborne") { color = colors.airborne; weight = 3.5; dash = null; opacity = 0.95; }
    else if (st === "on_ground") { color = colors.ground; weight = 2.5; dash = "7 6"; opacity = 0.9; }
    else { color = colors.scheduled; weight = 1.5; dash = null; opacity = 0.75; }
    const active = isActive();
    const hit = active && matches(leg);
    if (hit) { color = colors.selected; weight += 1.5; opacity = 1; }
    else if (active) { weight = Math.max(1, weight - 0.5); opacity = 0.3; }
    return { color, weight, dashArray: dash, opacity, hit };
  }

  function restyle(force) {
    if (!map) return;
    const litCodes = new Set();
    arcs.forEach((a) => {
      const s = arcStyle(a.leg);
      if (s.hit) { litCodes.add(a.leg.from); litCodes.add(a.leg.to); }
      const key = [s.color, s.weight, s.dashArray, s.opacity].join("|");
      if (force || key !== a.styleKey) {
        a.styleKey = key;
        a.line.setStyle({ color: s.color, weight: s.weight, dashArray: s.dashArray, opacity: s.opacity });
        if (s.hit) a.line.bringToFront();
      }
    });
    const active = isActive();
    pins.forEach((p, code) => {
      const lit = active && litCodes.has(code);
      const pressed = filter.tok === code;
      if (lit !== p.lit || pressed !== p.pressed) {
        p.lit = lit; p.pressed = pressed;
        p.markers.forEach((m, i) => {
          const node = m.getElement();
          if (!node) return;
          node.classList.toggle("is-lit", lit);
          if (i === 1) node.setAttribute("aria-pressed", pressed ? "true" : "false");
        });
      }
    });
  }

  /* ---- data sync ---- */
  function collectLegs() {
    const out = [];
    (trips || []).forEach((t) => {
      ["out", "ret"].forEach((dir) => {
        ((t && t[dir]) || []).forEach((leg, i) => {
          if (!leg) return;
          out.push({ key: (leg.direction || dir) + ":" + t.id + ":" + (leg.seq != null ? leg.seq : i), leg });
        });
      });
    });
    return out;
  }

  const legLabel = (l) => (l.flight_no || "Flight") + " " + (l.from || "?") + " to " + (l.to || "?");

  /* Three copies per airport (world offsets -360/0/+360) so the pin is visible
     wherever an arc is drawn; only the middle one is in the tab order. */
  function makePin(code, lat, lon, label) {
    const markers = WORLD_OFFSETS.map((off) => {
      const primary = off === 0;
      const m = L.marker([lat, lon + off], {
        icon: L.divIcon({ className: "hmap-pin", html: '<span class="hmap-pin__dot"></span>', iconSize: [22, 22], iconAnchor: [11, 11] }),
        keyboard: primary, riseOnHover: true,
      });
      m.on("click", () => onSelectCode(code));
      /* Leaflet 1.9 only turns Enter into a popup open, not a click, so wire it here. */
      m.on("keypress", (e) => {
        const k = e.originalEvent && e.originalEvent.keyCode;
        if (k === 13 || k === 32) { e.originalEvent.preventDefault(); onSelectCode(code); }
      });
      m.addTo(pinLayer);
      const node = m.getElement();
      if (node) {
        if (primary) { node.setAttribute("role", "button"); node.setAttribute("aria-label", label); node.setAttribute("aria-pressed", "false"); }
        else { node.setAttribute("aria-hidden", "true"); }
      }
      if (primary) m.bindTooltip(label, { direction: "top", offset: [0, -8] });
      return m;
    });
    return { markers, lat, lon, label, lit: false, pressed: false };
  }

  function syncAll() {
    if (!map) return;
    const legs = collectLegs();
    const wantArcs = new Map();
    const airports = new Map();
    legs.forEach(({ key, leg }) => {
      if (![leg.from_lat, leg.from_lon, leg.to_lat, leg.to_lon].every(finite)) return;
      wantArcs.set(key, leg);
      [["from", "from_lat", "from_lon"], ["to", "to_lat", "to_lon"]].forEach(([c, la, lo]) => {
        const code = leg[c];
        if (!code) return;
        const cur = airports.get(code) || { lat: leg[la], lon: leg[lo], name: leg[c + "_name"], city: leg[c + "_city"], count: 0 };
        cur.count++;
        airports.set(code, cur);
      });
    });

    /* arcs */
    arcs.forEach((a, key) => { if (!wantArcs.has(key)) { arcLayer.removeLayer(a.line); arcs.delete(key); } });
    wantArcs.forEach((leg, key) => {
      const geo = [leg.from_lat, leg.from_lon, leg.to_lat, leg.to_lon].join(",");
      let a = arcs.get(key);
      const path = (!a || a.geo !== geo) ? gcPath(leg.from_lat, leg.from_lon, leg.to_lat, leg.to_lon) : null;
      const rings = path ? WORLD_OFFSETS.map((off) => path.map((p) => [p[0], p[1] + off])) : null;
      if (a) {
        a.leg = leg; a.label = legLabel(leg);
        if (rings) { a.line.setLatLngs(rings); a.geo = geo; a.path = path; }
      } else {
        const line = L.polyline(rings || [], { weight: 1.5, interactive: true, bubblingMouseEvents: false, lineCap: "round", lineJoin: "round" });
        a = { leg, line, geo, path: path || [], styleKey: "", label: legLabel(leg) };
        line.bindTooltip(() => a.label, { sticky: true });
        line.on("click", () => { if (a.leg.flight_no) onSelectCode(a.leg.flight_no); });
        line.addTo(arcLayer);
        arcs.set(key, a);
      }
    });

    /* airports */
    pins.forEach((p, code) => { if (!airports.has(code)) { p.markers.forEach((m) => pinLayer.removeLayer(m)); pins.delete(code); } });
    airports.forEach((ap, code) => {
      const label = code + (ap.name ? " " + ap.name : "") + (ap.city ? ", " + ap.city : "") + ", " + ap.count + (ap.count === 1 ? " flight" : " flights");
      const p = pins.get(code);
      if (!p) pins.set(code, makePin(code, ap.lat, ap.lon, label));
      else {
        if (p.lat !== ap.lat || p.lon !== ap.lon) {
          p.markers.forEach((m, i) => m.setLatLng([ap.lat, ap.lon + WORLD_OFFSETS[i]]));
          p.lat = ap.lat; p.lon = ap.lon;
        }
        if (p.label !== label) {
          p.label = label;
          const n = p.markers[1].getElement();
          if (n) n.setAttribute("aria-label", label);
          p.markers[1].setTooltipContent(label);
        }
      }
    });

    emptyEl.hidden = arcs.size > 0 || pins.size > 0;
    syncChips(legs);
    syncSr(legs);
    restyle(false);
    maybeAutoFit();
  }

  function syncChips(legs) {
    const dates = [];
    legs.forEach(({ leg }) => { if (leg.date_local && dates.indexOf(leg.date_local) < 0) dates.push(leg.date_local); });
    dates.sort();
    const key = dates.join("|");
    if (key !== dateKey) {
      dateKey = key;
      chipsEl.textContent = "";
      dates.forEach((d) => {
        const b = el("button", "chip", d);
        b.type = "button"; b.dataset.date = d;
        b.addEventListener("click", () => {
          const next = filter.date === d ? null : d;
          filter = { tok: null, date: next };
          if (onSelectDate) { try { onSelectDate(next); } catch (e) { /* a shell bug must not break the map */ } }
          paintChips(); restyle(false);
        });
        chipsEl.appendChild(b);
      });
    }
    paintChips();
  }
  function paintChips() {
    chipsEl.querySelectorAll(".chip").forEach((b) => {
      const on = filter.date === b.dataset.date;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function syncSr(legs) {
    const items = legs.filter(({ leg }) => leg.from && leg.to).map(({ leg }) =>
      legLabel(leg) + (leg.date_local ? " on " + leg.date_local : "") +
      (leg.live_state === "airborne" ? ", airborne" : leg.live_state === "on_ground" ? ", on the ground" : ""));
    const key = items.join("\n");
    if (key === srKey) return;
    srKey = key;
    srList.textContent = "";
    items.forEach((t) => srList.appendChild(el("li", null, t)));
  }

  /* ---- fitting ---- */
  const visibleSized = () => !!(map && visible && mapEl.clientWidth > 0 && mapEl.clientHeight > 0);
  const codesKey = () => Array.from(pins.keys()).sort().join(",");

  function boundsPoints() {
    const pts = [];
    arcs.forEach((a) => a.path.forEach((p) => pts.push(p)));
    pins.forEach((p) => pts.push([p.lat, p.lon]));
    return pts;
  }

  function doFit() {
    const pts = boundsPoints();
    if (!pts.length || !visibleSized()) return false;
    fitting = true;
    try {
      map.invalidateSize({ pan: false, animate: false });
      const pad = mapEl.clientWidth < 520 ? 24 : 40;
      map.fitBounds(L.latLngBounds(pts), { padding: [pad, pad], maxZoom: 6, animate: false });
    } finally { fitting = false; }
    lastFitCodes = codesKey();
    needFit = false;
    return true;
  }

  /* Fit only on first draw or when the airport set changes, and never after the
     user has panned or zoomed (until they press "Fit all"). */
  function maybeAutoFit() {
    if (!map) return;
    const k = codesKey();
    if (lastFitCodes === null || k !== lastFitCodes) needFit = true;
    if (!needFit || userMoved) return;
    if (!pins.size) { lastFitCodes = k; needFit = false; return; }
    doFit();
  }

  fitBtn.addEventListener("click", () => { userMoved = false; if (map) doFit(); });

  function doShowWork() {
    if (!map || !visible) return;
    fitting = true;
    try { map.invalidateSize({ pan: false, animate: false }); } finally { fitting = false; }
    maybeAutoFit();
  }

  /* ---- public API ---- */
  return {
    update(newTrips, newFilter) {
      if (destroyed) return;
      try {
        trips = Array.isArray(newTrips) ? newTrips : [];
        const f = newFilter || {};
        const next = { tok: f.tok || null, date: f.date || null };
        const key = next.tok + "|" + next.date;
        if (key !== lastFilterKey) { lastFilterKey = key; filter = next; }
        if (map) syncAll();
        else { const legs = collectLegs(); syncChips(legs); syncSr(legs); }
      } catch (e) { /* never throw into the board */ }
    },
    setTheme(t) {
      theme = t === "light" ? "light" : "dark";
      containerEl.setAttribute("data-theme", theme);
      if (!map) return;
      readColors();
      restyle(true);
      /* tokens may flip a frame after the caller toggles <html data-theme>; re-read once. */
      requestAnimationFrame(() => { if (!destroyed && map) { readColors(); restyle(true); } });
    },
    show() {
      if (destroyed) return;
      visible = true;
      containerEl.hidden = false;
      if (map) doShowWork(); else boot();
    },
    hide() {
      if (destroyed) return;
      visible = false;
      containerEl.hidden = true;
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      teardownMap();
      containerEl.textContent = "";
      containerEl.classList.remove("hmap");
      containerEl.removeAttribute("data-theme");
    },
    /* test/diagnostic hook: no behaviour depends on it */
    _debug() {
      return {
        ready: !!map, userMoved,
        arcs: Array.from(arcs.entries()).map(([key, a]) => ({ key, path: a.path, styleKey: a.styleKey })),
        pins: Array.from(pins.keys()),
        map,
      };
    },
  };
}

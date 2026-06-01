/* Hyperfixed Flight Tracker — frontend */
(function () {
  "use strict";
  var APP = window.__APP__ || { user: null, gated: false };

  /* ---------- clock ---------- */
  var clk = document.getElementById("zclock");
  function pad(n) { return String(n).padStart(2, "0"); }
  function tick() {
    if (!clk) return;
    var d = new Date();
    clk.textContent = pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds()) + " Z";
  }
  tick(); setInterval(tick, 1000);

  /* ---------- gated-page messaging ---------- */
  var note = document.getElementById("gateNote");
  if (note) {
    var p = new URLSearchParams(location.search).get("auth");
    var msgs = { not_member: "That account isn't in the server.", denied: "Login was cancelled.", error: "Something went wrong signing in." };
    if (p && msgs[p]) note.textContent = msgs[p];
  }

  var boardEl = document.getElementById("board");
  if (!boardEl) return; /* gated or login page — nothing more to wire */

  /* ---------- helpers ---------- */
  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function initials(h) { return (h || "?").replace(/[^a-z0-9]/gi, "").slice(0, 2).toUpperCase() || "?"; }
  function hhmm(local) { var m = /\d{2}:\d{2}/.exec(local || ""); return m ? m[0] : ""; }
  function tok(v) { return v ? '<span class="tok" data-tok="' + esc(v) + '">' + esc(v) + "</span>" : "—"; }
  var planeIc = '<svg class="ic" viewBox="0 0 24 24" fill="currentColor"><path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z"/></svg>';

  var STATE = { tok: null, date: null, trips: [], me: null };

  /* ---------- data ---------- */
  function load() {
    fetch("api/trips", { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        STATE.trips = data.trips || [];
        STATE.me = data.me || null;
        render();
      })
      .catch(function () {
        boardEl.innerHTML = '<p class="empty">Could not load the board.</p>';
      });
  }

  function groupByOwner(trips) {
    var owners = [], idx = {};
    trips.forEach(function (t) {
      if (!idx[t.owner_id]) { idx[t.owner_id] = { id: t.owner_id, name: t.owner_name, journeys: [] }; owners.push(idx[t.owner_id]); }
      [["out", t.out], ["ret", t.ret]].forEach(function (pair) {
        if (pair[1] && pair[1].length) idx[t.owner_id].journeys.push({ trip_id: t.id, legs: pair[1] });
      });
    });
    return owners;
  }

  function legHtml(leg, isCxn) {
    var dateCell = isCxn
      ? '<span class="cxn">↳ cxn</span>'
      : esc(leg.date_local || "") + (hhmm(leg.dep_local) ? '<span class="time">' + hhmm(leg.dep_local) + "</span>" : "");

    var routeHtml;
    if (!leg.from && !leg.to) {
      routeHtml = '<span class="unresolved">couldn\'t resolve — edit to add airports</span>';
    } else {
      routeHtml = '<div class="route">' + tok(leg.from) + '<span class="arr">→</span>' + tok(leg.to) + "</div>";
      var bits = [];
      if (leg.ac_model) bits.push(esc(leg.ac_model));
      if (leg.ac_age != null) bits.push(esc(leg.ac_age) + " yrs");
      if (leg.reg) bits.push(esc(leg.reg));
      if (bits.length) routeHtml += '<div class="acline">' + bits.join('<span class="sep">·</span>') + "</div>";
    }

    var track;
    if (leg.live) {
      track = '<a class="btn btn--live btn--sm" href="' + esc(leg.live_url) + '" target="_blank" rel="noopener">' + planeIc + "Live</a>" +
              '<a class="btn btn--sm" href="' + esc(leg.fa_url) + '" target="_blank" rel="noopener">FA</a>';
    } else {
      track = '<a class="btn btn--sm" href="' + esc(leg.fa_url) + '" target="_blank" rel="noopener">FlightAware</a>' +
              '<a class="btn btn--muted btn--sm" href="' + esc(leg.live_url) + '" target="_blank" rel="noopener" title="Shows the aircraft only while it is airborne">' + planeIc + "</a>";
    }

    return '<div class="leg" data-flight="' + esc(leg.flight_no || "") + '" data-from="' + esc(leg.from || "") +
      '" data-to="' + esc(leg.to || "") + '" data-date="' + esc(leg.date_local || "") + '">' +
      '<span class="col-date">' + dateCell + "</span>" +
      '<span class="col-flight">' + tok(leg.flight_no) + (leg.callsign ? '<span class="csign">' + esc(leg.callsign) + "</span>" : "") + "</span>" +
      '<span class="col-route">' + routeHtml + "</span>" +
      '<span class="track">' + track + "</span></div>";
  }

  function render() {
    var owners = groupByOwner(STATE.trips);
    if (!owners.length) {
      boardEl.innerHTML = '<p class="empty">No active trips. ' + (APP.user ? "Add one with the button above." : "") + "</p>";
      return;
    }
    var html = "";
    owners.forEach(function (o) {
      var airborne = o.journeys.some(function (j) { return j.legs.some(function (l) { return l.live; }); });
      html += '<div class="flyer"><div class="flyer-head"><div class="avatar">' + initials(o.name) +
        '</div><span class="handle">@' + esc(o.name) + "</span>" +
        (airborne ? '<span class="status"><span class="pulse"></span>airborne</span>' : "") + "</div>";
      o.journeys.forEach(function (j) {
        var multi = j.legs.length > 1;
        var owned = STATE.me && STATE.me === o.id;
        html += '<div class="journey ' + (multi ? "multi" : "single") + '">';
        if (owned) html += '<div class="j-tools"><button class="j-del" data-trip="' + j.trip_id + '" title="Remove this trip">remove</button></div>';
        j.legs.forEach(function (l, i) { html += legHtml(l, i > 0); });
        html += "</div>";
      });
      html += "</div>";
    });
    boardEl.innerHTML = html;
    wireBoard();
    if (mapInited) drawMap();
    rebuildDateChips();
    apply();
  }

  /* ---------- highlight ---------- */
  function wireBoard() {
    boardEl.querySelectorAll(".tok").forEach(function (t) {
      t.onclick = function () { var v = t.getAttribute("data-tok"); selTok(STATE.tok === v ? null : v); };
    });
    boardEl.querySelectorAll(".j-del").forEach(function (b) {
      b.onclick = function () {
        if (!confirm("Remove this trip from the board?")) return;
        fetch("api/trips/" + b.getAttribute("data-trip"), { method: "DELETE", credentials: "same-origin" })
          .then(function (r) { if (r.ok) load(); });
      };
    });
  }

  var fbar = document.getElementById("filterbar"), ftext = document.getElementById("filterText");
  function selTok(t) { STATE.tok = t; STATE.date = null; apply(); }
  function selDate(d) { STATE.date = d; STATE.tok = null; apply(); }

  function apply() {
    var active = STATE.tok || STATE.date;
    boardEl.querySelectorAll(".leg").forEach(function (l) {
      var keep = true;
      if (STATE.tok) keep = [l.getAttribute("data-flight"), l.getAttribute("data-from"), l.getAttribute("data-to")].indexOf(STATE.tok) >= 0;
      else if (STATE.date) keep = l.getAttribute("data-date") === STATE.date;
      l.classList.toggle("hl", !!active && keep);
      l.classList.toggle("dim", !!active && !keep);
    });
    boardEl.querySelectorAll(".tok").forEach(function (t) { t.classList.toggle("sel", STATE.tok && t.getAttribute("data-tok") === STATE.tok); });
    if (fbar) {
      fbar.classList.toggle("show", !!active);
      ftext.innerHTML = STATE.tok ? "Showing <b>" + esc(STATE.tok) + "</b>" : (STATE.date ? "Flights on <b>" + esc(STATE.date) + "</b>" : "");
    }
    document.querySelectorAll(".chip").forEach(function (c) { c.classList.toggle("on", STATE.date && c.textContent === STATE.date); });
    styleArcs();
  }
  var clearBtn = document.getElementById("clearBtn");
  if (clearBtn) clearBtn.onclick = function () { STATE.tok = null; STATE.date = null; apply(); };

  /* ---------- map ---------- */
  var map = null, mapInited = false, arcs = [], markers = {};
  function allLegs() {
    var out = [];
    STATE.trips.forEach(function (t) { (t.out || []).concat(t.ret || []).forEach(function (l) { out.push(l); }); });
    return out;
  }
  function toRad(d) { return d * Math.PI / 180; }
  function toDeg(r) { return r * 180 / Math.PI; }
  function gcSegments(lat1, lon1, lat2, lon2, n) {
    var p1 = [toRad(lat1), toRad(lon1)], p2 = [toRad(lat2), toRad(lon2)];
    var d = 2 * Math.asin(Math.sqrt(Math.pow(Math.sin((p1[0] - p2[0]) / 2), 2) +
      Math.cos(p1[0]) * Math.cos(p2[0]) * Math.pow(Math.sin((p1[1] - p2[1]) / 2), 2)));
    var pts = [];
    if (d === 0) return [[[lat1, lon1]]];
    for (var i = 0; i <= n; i++) {
      var f = i / n;
      var A = Math.sin((1 - f) * d) / Math.sin(d), B = Math.sin(f * d) / Math.sin(d);
      var x = A * Math.cos(p1[0]) * Math.cos(p1[1]) + B * Math.cos(p2[0]) * Math.cos(p2[1]);
      var y = A * Math.cos(p1[0]) * Math.sin(p1[1]) + B * Math.cos(p2[0]) * Math.sin(p2[1]);
      var z = A * Math.sin(p1[0]) + B * Math.sin(p2[0]);
      pts.push([toDeg(Math.atan2(z, Math.sqrt(x * x + y * y))), toDeg(Math.atan2(y, x))]);
    }
    var segs = [[]], prev = null;
    pts.forEach(function (pt) {
      if (prev !== null && Math.abs(pt[1] - prev) > 180) segs.push([]);
      segs[segs.length - 1].push(pt); prev = pt[1];
    });
    return segs;
  }
  function initMap() {
    map = L.map("map", { worldCopyJump: true, attributionControl: true }).setView([30, 0], 2);
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
      attribution: '&copy; OpenStreetMap &copy; CARTO', subdomains: "abcd", maxZoom: 19
    }).addTo(map);
    mapInited = true;
    drawMap();
  }
  function drawMap() {
    arcs.forEach(function (a) { map.removeLayer(a.line); });
    Object.keys(markers).forEach(function (k) { map.removeLayer(markers[k].m); });
    arcs = []; markers = {};
    var bounds = [];
    function marker(code, lat, lon, name) {
      if (!code || lat == null || lon == null || markers[code]) return;
      var m = L.circleMarker([lat, lon], { radius: 4, color: cssVar("--accent-teal"), weight: 1.5, fillColor: cssVar("--text"), fillOpacity: 0.9 })
        .bindTooltip(code + (name ? " · " + name : ""), { direction: "top" }).addTo(map);
      m.on("click", function () { selTok(STATE.tok === code ? null : code); });
      markers[code] = { m: m, code: code };
    }
    allLegs().forEach(function (l) {
      if (l.from_lat == null || l.to_lat == null) return;
      marker(l.from, l.from_lat, l.from_lon, l.from_name);
      marker(l.to, l.to_lat, l.to_lon, l.to_name);
      var group = L.layerGroup();
      gcSegments(l.from_lat, l.from_lon, l.to_lat, l.to_lon, 64).forEach(function (seg) {
        if (seg.length > 1) group.addLayer(L.polyline(seg, { weight: 1.5, opacity: 0.9 }));
      });
      group.addTo(map);
      arcs.push({ line: group, leg: l });
      bounds.push([l.from_lat, l.from_lon], [l.to_lat, l.to_lon]);
    });
    if (bounds.length) { try { map.fitBounds(bounds, { padding: [40, 40], maxZoom: 6 }); } catch (e) {} }
    styleArcs();
  }
  function styleArcs() {
    if (!mapInited) return;
    var active = STATE.tok || STATE.date;
    var lit = {};
    arcs.forEach(function (a) {
      var l = a.leg, keep = true;
      if (STATE.tok) keep = [l.flight_no, l.from, l.to].indexOf(STATE.tok) >= 0;
      else if (STATE.date) keep = l.date_local === STATE.date;
      var color, weight, opacity;
      if (active && keep) { color = cssVar("--accent"); weight = 3; opacity = 1; lit[l.from] = lit[l.to] = 1; }
      else if (active) { color = cssVar("--border"); weight = 1; opacity = 0.5; }
      else { color = l.live ? cssVar("--accent-warm") : cssVar("--accent-teal"); weight = l.live ? 2.5 : 1.5; opacity = 0.9; }
      a.line.eachLayer(function (ly) { ly.setStyle({ color: color, weight: weight, opacity: opacity }); });
    });
    Object.keys(markers).forEach(function (code) {
      var on = active && lit[code];
      markers[code].m.setStyle({ color: on ? cssVar("--accent") : cssVar("--accent-teal"), radius: on ? 6 : 4 });
    });
  }
  function rebuildDateChips() {
    var wrap = document.getElementById("datechips");
    if (!wrap) return;
    var dates = [];
    allLegs().forEach(function (l) { if (l.date_local && dates.indexOf(l.date_local) < 0) dates.push(l.date_local); });
    dates.sort();
    wrap.innerHTML = "";
    dates.forEach(function (d) {
      var b = document.createElement("button");
      b.className = "chip"; b.textContent = d;
      b.onclick = function () { selDate(STATE.date === d ? null : d); };
      wrap.appendChild(b);
    });
  }

  var bBoard = document.getElementById("bBoard"), bMap = document.getElementById("bMap");
  var mapcard = document.getElementById("mapcard");
  if (bBoard) bBoard.onclick = function () { bBoard.classList.add("on"); bMap.classList.remove("on"); boardEl.style.display = ""; mapcard.style.display = "none"; };
  if (bMap) bMap.onclick = function () {
    bMap.classList.add("on"); bBoard.classList.remove("on"); boardEl.style.display = "none"; mapcard.style.display = "block";
    if (!mapInited) initMap(); else { map.invalidateSize(); drawMap(); }
  };

  /* ---------- add-trip modal ---------- */
  var modal = document.getElementById("addModal");
  if (modal && APP.user) {
    var outLegs = document.getElementById("outLegs"), retLegs = document.getElementById("retLegs");
    function flightRow() {
      var row = document.createElement("div"); row.className = "legrow"; row.dataset.kind = "flight";
      row.innerHTML = '<input class="f-flight" placeholder="Flight no. (e.g. DL1200)"><input class="f-date" type="date"><button class="rm" title="Remove">&times;</button>';
      row.querySelector(".rm").onclick = function () { row.remove(); };
      return row;
    }
    function manualRow() {
      var row = document.createElement("div"); row.className = "legrow manual"; row.dataset.kind = "manual";
      row.innerHTML = '<input class="f-from" placeholder="From (ICAO/IATA)"><input class="f-to" placeholder="To"><input class="f-date" type="date"><button class="rm" title="Remove">&times;</button>';
      row.querySelector(".rm").onclick = function () { row.remove(); };
      return row;
    }
    var manualMode = document.getElementById("manualMode");
    function rowFor() { return manualMode && manualMode.checked ? manualRow() : flightRow(); }
    function rebuildRows(container) {
      var n = Math.max(1, container.children.length);
      container.innerHTML = "";
      for (var i = 0; i < n; i++) container.appendChild(rowFor());
    }
    function resetModal() {
      outLegs.innerHTML = ""; retLegs.innerHTML = "";
      if (manualMode) manualMode.checked = false;
      outLegs.appendChild(rowFor());
      document.getElementById("retBlock").hidden = true;
      document.getElementById("addErr").textContent = "";
    }
    function openModal() { resetModal(); modal.hidden = false; }
    function closeModal() { modal.hidden = true; }

    document.getElementById("openAdd").onclick = openModal;
    document.getElementById("closeAdd").onclick = closeModal;
    document.getElementById("cancelAdd").onclick = closeModal;
    modal.addEventListener("click", function (e) { if (e.target === modal) closeModal(); });
    if (manualMode) manualMode.onchange = function () {
      rebuildRows(outLegs);
      if (!document.getElementById("retBlock").hidden) rebuildRows(retLegs);
    };
    document.getElementById("toggleRet").onclick = function () {
      var rb = document.getElementById("retBlock");
      rb.hidden = false;
      if (!retLegs.children.length) retLegs.appendChild(rowFor());
    };
    document.querySelectorAll(".addleg").forEach(function (btn) {
      btn.onclick = function () {
        var tgt = btn.getAttribute("data-dir") === "out" ? outLegs : retLegs;
        tgt.appendChild(rowFor());
      };
    });

    function gather(container) {
      var items = [];
      container.querySelectorAll(".legrow").forEach(function (row) {
        var date = (row.querySelector(".f-date") || {}).value || "";
        if (row.dataset.kind === "manual") {
          var f = row.querySelector(".f-from").value.trim(), t = row.querySelector(".f-to").value.trim();
          if (f && t) items.push({ from: f, to: t, date: date });
        } else {
          var fn = row.querySelector(".f-flight").value.trim();
          if (fn) items.push({ flight_no: fn, date: date });
        }
      });
      return items;
    }

    document.getElementById("submitAdd").onclick = function () {
      var err = document.getElementById("addErr");
      var payload = { out: gather(outLegs), ret: document.getElementById("retBlock").hidden ? [] : gather(retLegs) };
      try { payload.tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) {}
      if (!payload.out.length && !payload.ret.length) { err.textContent = "Add at least one flight."; return; }
      err.textContent = "Saving…";
      fetch("api/trips", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
      }).then(function (r) {
        if (!r.ok) throw new Error(r.status);
        closeModal(); load();
      }).catch(function () { err.textContent = "Could not save — check the flight number and date."; });
    };
  }

  load();
  setInterval(load, 90000); /* refresh live flags + lifecycle every 90s */
})();

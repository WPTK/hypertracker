import { createMap, gcPath } from "/static/js/map.js";

const AP = {
  KJAX: [30.4941, -81.6879, "Jacksonville Intl", "Jacksonville"],
  KDEN: [39.8561, -104.6737, "Denver Intl", "Denver"],
  KATL: [33.6407, -84.4277, "Hartsfield-Jackson", "Atlanta"],
  KLAX: [33.9416, -118.4085, "Los Angeles Intl", "Los Angeles"],
  YSSY: [-33.9461, 151.1772, "Sydney Kingsford Smith", "Sydney"],
  PHNL: [21.3187, -157.9224, "Honolulu Intl", "Honolulu"],
  RJAA: [35.7647, 140.3864, "Narita Intl", "Tokyo"],
  EGLL: [51.47, -0.4543, "Heathrow", "London"],
};

function leg(dir, seq, flight, from, to, date, state) {
  const f = AP[from], t = AP[to];
  return {
    direction: dir, seq, date_local: date, flight_no: flight, callsign: null,
    from, from_lat: f[0], from_lon: f[1], from_name: f[2], from_city: f[3],
    to, to_lat: t[0], to_lon: t[1], to_name: t[2], to_city: t[3],
    live_state: state || null,
  };
}

export const SAMPLE = [
  { id: 1, out: [leg("out", 0, "DL1200", "KJAX", "KATL", "2026-10-10", "airborne"), leg("out", 1, "DL1200", "KATL", "KDEN", "2026-10-10", null)], ret: [leg("ret", 0, "DL88", "KDEN", "KJAX", "2026-10-14", "on_ground")] },
  { id: 2, out: [leg("out", 0, "UA839", "KLAX", "YSSY", "2026-10-11", null)], ret: [] },
];
export const DATELINE = [
  { id: 3, out: [leg("out", 0, "UA839", "KLAX", "YSSY", "2026-10-11", null), leg("out", 1, "HA1", "PHNL", "RJAA", "2026-10-12", null)], ret: [] },
];
export const OTHER_SET = [
  { id: 4, out: [leg("out", 0, "BA117", "EGLL", "KATL", "2026-10-12", null)], ret: [] },
];

const calls = { codes: [], dates: [] };
const host = document.getElementById("host");
const params = new URLSearchParams(location.search);
if (params.get("theme") === "light") document.documentElement.setAttribute("data-theme", "light");
const m = createMap(host, {
  onSelectCode: (c) => calls.codes.push(c),
  onSelectDate: (d) => calls.dates.push(d),
});
m.setTheme(document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark");
window.H = {
  m, calls, SAMPLE, DATELINE, OTHER_SET, gcPath,
  setPage(theme) {
    if (theme === "light") document.documentElement.setAttribute("data-theme", "light");
    else document.documentElement.removeAttribute("data-theme");
    m.setTheme(theme);
  },
  state() {
    const d = m._debug();
    const mp = d.map;
    return {
      ready: d.ready, userMoved: d.userMoved, pins: d.pins,
      center: mp ? [mp.getCenter().lat, mp.getCenter().lng] : null,
      zoom: mp ? mp.getZoom() : null,
      arcs: d.arcs,
    };
  },
};
window.H.ready = true;

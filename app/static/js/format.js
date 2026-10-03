/* Pure formatting helpers: no DOM, no state. */

const TS_RE = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$/;

function offsetMinutes(tz) {
  if (!tz) return null;
  if (tz === "Z") return 0;
  const m = /^([+-])(\d{2}):?(\d{2})?$/.exec(tz);
  if (!m) return null;
  const mins = Number(m[2]) * 60 + Number(m[3] || 0);
  return m[1] === "-" ? -mins : mins;
}

/** "UTC", "UTC-4", "UTC+5:30" */
export function zoneLabel(offMin) {
  if (offMin == null) return "";
  if (offMin === 0) return "UTC";
  const sign = offMin < 0 ? "-" : "+";
  const abs = Math.abs(offMin);
  const h = Math.floor(abs / 60);
  const m = abs % 60;
  return "UTC" + sign + h + (m ? ":" + String(m).padStart(2, "0") : "");
}

/**
 * Parse an AeroDataBox local string such as "2026-10-10 08:15-04:00".
 * Returns {date, time, offMin, zone, ms} or null. ms is the UTC epoch when an
 * offset is present.
 */
export function parseStamp(str) {
  if (typeof str !== "string") return null;
  const m = TS_RE.exec(str.trim());
  if (!m) return null;
  const [, y, mo, d, hh, mm, ss, tz] = m;
  const offMin = offsetMinutes(tz);
  const wall = Date.UTC(+y, +mo - 1, +d, +hh, +mm, +(ss || 0));
  return {
    date: `${y}-${mo}-${d}`,
    time: `${hh}:${mm}`,
    offMin,
    zone: zoneLabel(offMin),
    ms: wall - (offMin == null ? 0 : offMin) * 60000,
  };
}

/** UTC epoch ms from an AeroDataBox UTC string ("2026-10-10 12:15Z"), or null. */
export function parseUtc(str) {
  const p = parseStamp(str);
  return p ? p.ms : null;
}

/** "YYYY-MM-DD" of the wall clock at a given UTC offset (minutes) right now. */
export function todayAt(nowMs, offMin) {
  if (offMin == null) {
    const d = new Date(nowMs);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }
  return new Date(nowMs + offMin * 60000).toISOString().slice(0, 10);
}

function dayNumber(dateStr) {
  const [y, m, d] = dateStr.split("-").map(Number);
  return Math.round(Date.UTC(y, m - 1, d) / 86400000);
}

const DAY_FMT = new Intl.DateTimeFormat("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });

/** "Today", "Tomorrow", "Yesterday" or "Sat Oct 10" (with the year when it is not this one). */
export function dayLabel(dateStr, todayStr) {
  if (!dateStr) return "";
  if (todayStr) {
    const diff = dayNumber(dateStr) - dayNumber(todayStr);
    if (diff === 0) return "Today";
    if (diff === 1) return "Tomorrow";
    if (diff === -1) return "Yesterday";
  }
  const [y, m, d] = dateStr.split("-").map(Number);
  const parts = DAY_FMT.formatToParts(new Date(Date.UTC(y, m - 1, d)));
  const get = (t) => (parts.find((p) => p.type === t) || {}).value || "";
  let out = `${get("weekday")} ${get("month")} ${get("day")}`;
  if (todayStr && dateStr.slice(0, 4) !== todayStr.slice(0, 4)) out += ` ${y}`;
  return out;
}

/** "under a minute", "45m", "3h", "3h 5m", "2d 4h" */
export function duration(ms) {
  const mins = Math.max(0, Math.round(ms / 60000));
  if (mins < 1) return "under a minute";
  if (mins < 60) return mins + "m";
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (h < 48) return m ? `${h}h ${m}m` : `${h}h`;
  const d = Math.floor(h / 24);
  const rh = h % 24;
  return rh ? `${d}d ${rh}h` : `${d}d`;
}

/**
 * Status of one leg right now.
 * Returns {key, label} where key is one of airborne, ground, scheduled,
 * landed, unverified. It maps onto the .status--* modifiers.
 */
export function legStatus(leg, nowMs) {
  const arr = parseUtc(leg.arr_utc);
  if (leg.live_state === "airborne") return { key: "airborne", label: "Airborne" };
  if (arr != null && nowMs >= arr) return { key: "landed", label: "Landed" };
  if (leg.live_state === "on_ground") return { key: "ground", label: "On ground" };
  if (leg.unverified) return { key: "unverified", label: "Unverified" };
  // Between the scheduled departure and arrival with no live confirmation (the live check
  // is unavailable or has not seen the aircraft): say so without claiming it is airborne.
  const dep = parseUtc(leg.dep_utc);
  if (dep != null && arr != null && nowMs >= dep && nowMs < arr) {
    return { key: "scheduled", label: "Past departure" };
  }
  if (arr == null && leg.manual && leg.date_local) {
    const off = (parseStamp(leg.dep_local) || {}).offMin;
    if (leg.date_local < todayAt(nowMs, off == null ? null : off)) return { key: "landed", label: "Date passed" };
  }
  return { key: "scheduled", label: "Scheduled" };
}

/** Relative text under the arrival time: "lands in 3h", "landed 20m ago", "departs in 2d 4h". */
export function relativeText(leg, nowMs) {
  const dep = parseUtc(leg.dep_utc);
  const arr = parseUtc(leg.arr_utc);
  const airborne = leg.live_state === "airborne";
  if (arr != null && nowMs >= arr) {
    if (airborne) return "past its scheduled arrival";
    const ago = nowMs - arr;
    return ago < 60000 ? "just landed" : `landed ${duration(ago)} ago`;
  }
  if (arr != null && (airborne || (dep != null && nowMs >= dep))) return `lands in ${duration(arr - nowMs)}`;
  if (dep != null && nowMs < dep) return `departs in ${duration(dep - nowMs)}`;
  return "";
}

/** The code shown on a token: IATA first, then ICAO. */
export function displayCode(iata, icao) {
  return iata || icao || "";
}

export function plural(n, one, many) {
  return n === 1 ? one : many;
}

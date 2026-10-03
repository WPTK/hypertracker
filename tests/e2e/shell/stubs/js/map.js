/* Throwaway stub of js/map.js: records calls on window.__mapCalls. */
export function createMap(el, opts) {
  window.__mapCalls = [];
  window.__mapOpts = opts;
  el.className = "hmap";
  el.textContent = "map stub";
  const log = (n, a) => window.__mapCalls.push([n, a]);
  return {
    update: (trips, filter) => log("update", { n: trips.length, filter }),
    setTheme: (t) => log("setTheme", t),
    show: () => { el.hidden = false; log("show"); },
    hide: () => { el.hidden = true; log("hide"); },
    destroy: () => log("destroy"),
  };
}

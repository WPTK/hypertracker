/* Sets the theme before first paint. Loaded as a plain script in <head> so the
   page never flashes the wrong palette. Midnight (dark) is the default and has
   no attribute; Daylight is data-theme="light". A saved choice wins, otherwise
   the system preference decides on the first visit. */
(function () {
  var theme = null;
  try {
    var saved = localStorage.getItem("wptk-theme");
    if (saved === "light" || saved === "dark") theme = saved;
  } catch (e) { /* storage blocked: fall through */ }
  if (!theme) {
    try {
      theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    } catch (e) { theme = "dark"; }
  }
  if (theme === "light") document.documentElement.setAttribute("data-theme", "light");
  else document.documentElement.removeAttribute("data-theme");
})();

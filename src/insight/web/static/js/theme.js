// Runs before first paint, so the page never flashes the wrong theme.
(function () {
  var stored = null;
  try { stored = localStorage.getItem("insight-theme"); } catch (e) {}
  var dark = stored ? stored === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
})();

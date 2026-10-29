// Runs before first paint, so the page never flashes the wrong theme. Dark is the default.
(function () {
  var stored = null;
  try { stored = localStorage.getItem("insight-theme"); } catch (e) {}
  document.documentElement.setAttribute("data-theme", stored === "light" ? "light" : "dark");
})();

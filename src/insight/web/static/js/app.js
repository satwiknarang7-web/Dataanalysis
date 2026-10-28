// Insight front end: charts, upload, dialogs and small interactions.
// No framework: server-rendered HTML, htmx for partial updates, Plotly for charts.
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // ── Theme ───────────────────────────────────────────────────────────────
  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("insight-theme", theme); } catch (e) {}
    renderCharts(document, true);
  }
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-theme-toggle]")) {
      setTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
    }
  });

  // ── Number formatting (matches the server's) ──────────────────────────
  function compact(x) {
    const ax = Math.abs(x);
    const units = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
    for (const [size, suffix] of units) {
      if (ax >= size) {
        const v = x / size;
        const d = Math.abs(v) < 10 ? 2 : Math.abs(v) < 100 ? 1 : 0;
        return parseFloat(v.toFixed(d)) + suffix;
      }
    }
    return Number.isInteger(x) ? x.toLocaleString() : parseFloat(x.toFixed(ax < 10 ? 2 : 1)).toLocaleString();
  }
  function fmt(x, spec) {
    if (x === null || x === undefined || Number.isNaN(x)) return "–";
    if (spec.format === "percent") return ((spec.percent_scale === 100 ? x : x * 100).toFixed(1)).replace(/\.0$/, "") + "%";
    if (spec.format === "currency") return (x < 0 ? "-$" : "$") + compact(Math.abs(x));
    return compact(x);
  }
  function axis(spec) {
    if (spec.format === "percent") return spec.percent_scale === 100 ? { ticksuffix: "%" } : { tickformat: ".0%" };
    if (spec.format === "currency") return { tickprefix: "$", tickformat: "~s" };
    return { tickformat: "~s" };
  }

  // ── Charts ──────────────────────────────────────────────────────────────
  function palette() {
    return [1, 2, 3, 4, 5, 6, 7, 8].map((i) => css(`--chart-${i}`));
  }
  function alpha(hex, a) {
    const h = hex.replace("#", "");
    const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  }

  function layoutBase(extra = {}) {
    const text = css("--text-2");
    const grid = css("--chart-grid");
    return Object.assign({
      margin: { l: 56, r: 16, t: 12, b: 44 },
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "rgba(0,0,0,0)",
      font: { family: "Inter, system-ui, sans-serif", size: 12, color: text },
      hoverlabel: { bgcolor: css("--bg-elev"), bordercolor: css("--border"), font: { color: css("--text"), family: "Inter, sans-serif", size: 13 } },
      xaxis: { gridcolor: grid, zeroline: false, linecolor: grid, automargin: true, fixedrange: true },
      yaxis: { gridcolor: grid, zeroline: false, linecolor: grid, automargin: true, fixedrange: true },
      showlegend: false,
      bargap: 0.28,
      hovermode: "closest",
    }, extra);
  }

  function figure(spec) {
    const colors = palette();
    const accent = colors[0];
    const other = css("--chart-other");
    const bars = () => (spec.labels || []).map((l) => (l === "Other" ? other : accent));
    const errors = (spec.ranges || []).some(Boolean)
      ? {
          type: "data", symmetric: false, thickness: 1.4, width: 4, color: css("--text-3"),
          array: spec.ranges.map((r, i) => (r ? r[1] - spec.values[i] : 0)),
          arrayminus: spec.ranges.map((r, i) => (r ? spec.values[i] - r[0] : 0)),
        }
      : undefined;

    switch (spec.viz) {
      case "line":
      case "area": {
        const many = spec.series.length > 1;
        const data = spec.series.map((s, i) => ({
          type: "scatter", mode: spec.x.length > 40 ? "lines" : "lines+markers", name: s.name, x: spec.x, y: s.y,
          line: { color: colors[i % colors.length], width: 2.4, shape: "spline", smoothing: 0.6 },
          marker: { size: 5 },
          fill: spec.viz === "area" && !many ? "tozeroy" : "none",
          fillcolor: alpha(colors[i % colors.length], 0.1),
          hovertemplate: `%{x}<br><b>%{customdata}</b><extra>${many ? s.name : ""}</extra>`,
          customdata: s.y.map((v) => fmt(v, spec)),
          connectgaps: true,
        }));
        return { data, layout: layoutBase({ yaxis: Object.assign(layoutBase().yaxis, axis(spec)), showlegend: many, legend: { orientation: "h", y: -0.2 }, hovermode: "x unified" }) };
      }
      case "bar":
        return {
          data: [{ type: "bar", x: spec.labels, y: spec.values, marker: { color: bars(), cornerradius: 5 }, error_y: errors,
                   customdata: spec.text, hovertemplate: "%{x}<br><b>%{customdata}</b><extra></extra>" }],
          layout: layoutBase({ yaxis: Object.assign(layoutBase().yaxis, axis(spec)) }),
        };
      case "hbar":
        return {
          data: [{ type: "bar", orientation: "h", y: spec.labels, x: spec.values, marker: { color: bars(), cornerradius: 5 }, error_x: errors,
                   text: spec.text, textposition: "outside", cliponaxis: false, customdata: spec.text,
                   hovertemplate: "%{y}<br><b>%{customdata}</b><extra></extra>" }],
          layout: layoutBase({ margin: { l: 8, r: 56, t: 8, b: 24 }, yaxis: Object.assign(layoutBase().yaxis, { autorange: "reversed", gridcolor: "rgba(0,0,0,0)" }),
                               xaxis: Object.assign(layoutBase().xaxis, axis(spec), { showticklabels: false }) }),
        };
      case "donut": {
        const total = spec.values.reduce((a, b) => a + (b || 0), 0);
        return {
          data: [{ type: "pie", hole: 0.64, labels: spec.labels, values: spec.values, sort: false, direction: "clockwise",
                   marker: { colors: spec.labels.map((l, i) => (l === "Other" ? other : colors[i % colors.length])), line: { color: css("--bg-elev"), width: 2 } },
                   textinfo: "percent", textposition: "outside", customdata: spec.text,
                   hovertemplate: "%{label}<br><b>%{customdata}</b> · %{percent}<extra></extra>" }],
          layout: layoutBase({ margin: { l: 16, r: 16, t: 16, b: 16 }, showlegend: true, legend: { orientation: "v", x: 1.02, y: 0.5 },
                               annotations: [{ text: `<b>${fmt(total, spec)}</b><br><span style="font-size:11px">${spec.measure}</span>`, showarrow: false, font: { size: 17, color: css("--text") } }] }),
        };
      }
      case "scatter": {
        const fit = trendline(spec.x, spec.y);
        const data = [{ type: "scatter", mode: "markers", x: spec.x, y: spec.y,
                        marker: { color: alpha(accent, 0.45), size: 6, line: { width: 0 } },
                        hovertemplate: `${spec.x_label}: %{x}<br>${spec.y_label}: %{y}<extra></extra>` }];
        if (fit) data.push({ type: "scatter", mode: "lines", x: fit.x, y: fit.y, line: { color: colors[3], width: 2.2, dash: "dot" }, hoverinfo: "skip" });
        return { data, layout: layoutBase({ xaxis: Object.assign(layoutBase().xaxis, { title: { text: spec.x_label, font: { size: 12 } } }),
                                            yaxis: Object.assign(layoutBase().yaxis, { title: { text: spec.y_label, font: { size: 12 } } }) }) };
      }
      case "histogram":
        return {
          data: [{ type: "bar", x: spec.x, y: spec.counts, customdata: spec.labels, marker: { color: alpha(accent, 0.85), cornerradius: 3 },
                   hovertemplate: "%{customdata}<br><b>%{y} rows</b><extra></extra>" }],
          layout: layoutBase({ bargap: 0.06, xaxis: Object.assign(layoutBase().xaxis, axis(spec)) }),
        };
      default:
        return null;
    }
  }

  function trendline(xs, ys) {
    const n = xs.length;
    if (n < 3) return null;
    let sx = 0, sy = 0, sxx = 0, sxy = 0;
    for (let i = 0; i < n; i++) { sx += xs[i]; sy += ys[i]; sxx += xs[i] * xs[i]; sxy += xs[i] * ys[i]; }
    const d = n * sxx - sx * sx;
    if (!d) return null;
    const b = (n * sxy - sx * sy) / d, a = (sy - b * sx) / n;
    const lo = Math.min(...xs), hi = Math.max(...xs);
    return { x: [lo, hi], y: [a + b * lo, a + b * hi] };
  }

  function renderCharts(root, force) {
    if (!window.Plotly) return;
    $$("[data-chart]", root).forEach((el) => {
      if (el.dataset.rendered && !force) return;
      let spec;
      try { spec = JSON.parse(el.dataset.chart); } catch (e) { return; }
      const fig = figure(spec);
      if (!fig) return;
      window.Plotly.react(el, fig.data, fig.layout, { displayModeBar: false, responsive: true });
      el.dataset.rendered = "1";
    });
  }

  // ── Upload with progress ───────────────────────────────────────────────
  function initUpload() {
    const zone = $("[data-upload]");
    if (!zone) return;
    const input = $("input[type=file]", zone);
    const title = $("[data-progress-title]", zone);
    const fill = $("[data-progress-fill]", zone);
    const note = $("[data-progress-note]", zone);
    const maxBytes = Number(zone.dataset.maxMb) * 1024 * 1024;

    ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, () => zone.classList.remove("over")));
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      if (e.dataTransfer.files.length) send(e.dataTransfer.files[0]);
    });
    input.addEventListener("change", () => input.files.length && send(input.files[0]));

    function fail(message) {
      zone.classList.remove("busy");
      input.value = "";
      toast(message);
    }

    function send(file) {
      if (file.size > maxBytes) return fail(`That file is ${(file.size / 1048576).toFixed(1)} MB; your plan takes up to ${zone.dataset.maxMb} MB.`);
      zone.classList.add("busy");
      title.textContent = `Uploading ${file.name}…`;
      fill.classList.remove("indeterminate");
      fill.style.width = "0%";
      const form = new FormData();
      form.append("file", file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", zone.action);
      xhr.setRequestHeader("X-Requested-With", "fetch");
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const pct = Math.round((e.loaded / e.total) * 100);
        fill.style.width = pct + "%";
        if (pct >= 100) {
          title.textContent = "Reading and cleaning your data…";
          note.textContent = "Working out dates, numbers and categories, then building your board.";
          fill.classList.add("indeterminate");
        }
      };
      xhr.onload = () => {
        let body = {};
        try { body = JSON.parse(xhr.responseText); } catch (e) {}
        if (xhr.status < 300 && body.redirect) {
          title.textContent = "Done. Opening your dashboard…";
          window.location.assign(body.redirect);
        } else if (xhr.status === 401) {
          window.location.assign("/signin?next=/home");
        } else {
          fail(body.error || "The upload didn't work. Try again.");
        }
      };
      xhr.onerror = () => fail("The connection dropped during the upload. Try again.");
      xhr.send(form);
    }
  }

  // ── Toasts ──────────────────────────────────────────────────────────────
  function toast(message) {
    const zone = $("#toasts");
    const el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("data-toast", "");
    el.textContent = message;
    zone.appendChild(el);
    autoHide(el);
  }
  function autoHide(el) {
    if (el.dataset.hiding) return;
    el.dataset.hiding = "1";
    setTimeout(() => { el.style.transition = "opacity .3s"; el.style.opacity = "0"; setTimeout(() => el.remove(), 320); }, 5200);
  }

  // ── Small interactions ─────────────────────────────────────────────────
  document.addEventListener("click", (e) => {
    const open = e.target.closest("[data-open-dialog]");
    if (open) { const d = document.getElementById(open.dataset.openDialog); if (d) d.showModal(); return; }
    const close = e.target.closest("[data-close-dialog]");
    if (close) { close.closest("dialog").close(); return; }
    const nav = e.target.closest("[data-nav-toggle]");
    if (nav) { $("#app").classList.toggle("nav-open"); return; }
    const ask = e.target.closest("[data-ask]");
    if (ask) {
      const form = $("#ask-form");
      $("input[name=question]", form).value = ask.dataset.ask;
      form.requestSubmit();
      return;
    }
    const jump = e.target.closest("[data-jump]");
    if (jump) {
      const target = document.querySelector(jump.getAttribute("href"));
      if (target) {
        e.preventDefault();
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        target.classList.add("flash");
        setTimeout(() => target.classList.remove("flash"), 1600);
      }
      return;
    }
    // Close an open filter menu when clicking elsewhere.
    $$("details[data-filter][open]").forEach((d) => { if (!d.contains(e.target)) d.removeAttribute("open"); });
    // Close a dialog when its backdrop is clicked.
    if (e.target.tagName === "DIALOG") e.target.close();
  });

  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) { e.preventDefault(); return; }
    if (form.hasAttribute("data-busy-form")) {
      const btn = form.querySelector("button:not([type=button])");
      if (btn) btn.setAttribute("aria-busy", "true");
    }
  });

  // htmx: re-arm what a swap brings in, and end busy states.
  document.addEventListener("htmx:afterSwap", (e) => {
    renderCharts(e.detail.target, false);
    $$("[data-toast]", e.detail.target).forEach(autoHide);
    const empty = $("#ask-empty");
    if (empty && e.detail.target.id === "answers") empty.remove();
    if (e.detail.target.id === "toasts") $$("dialog[open]").forEach((d) => d.close());
  });
  document.addEventListener("htmx:afterRequest", (e) => {
    const form = e.detail.elt.closest ? e.detail.elt.closest("form") : null;
    if (!form) return;
    $$("[aria-busy]", form).forEach((b) => b.removeAttribute("aria-busy"));
    if (e.detail.successful && form.hasAttribute("data-reset")) form.reset();
  });
  document.addEventListener("htmx:beforeSwap", (e) => {
    // Plan limits (402) and validation messages come back as a toast to show.
    if (e.detail.xhr.status === 402 || e.detail.xhr.status === 400) { e.detail.shouldSwap = true; e.detail.isError = false; }
  });
  window.addEventListener("pageshow", () => $$("[aria-busy]").forEach((b) => b.removeAttribute("aria-busy")));

  document.addEventListener("DOMContentLoaded", () => {
    renderCharts(document, false);
    initUpload();
  });
})();

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
    for (const [size, suffix] of [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]]) {
      if (ax >= size) {
        const v = x / size;
        return parseFloat(v.toFixed(Math.abs(v) < 10 ? 2 : Math.abs(v) < 100 ? 1 : 0)) + suffix;
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
  function axisFormat(spec) {
    if (spec.format === "percent") return spec.percent_scale === 100 ? { ticksuffix: "%" } : { tickformat: ".0%" };
    if (spec.format === "currency") return { tickprefix: "$", tickformat: "~s" };
    return { tickformat: "~s" };
  }

  // ── Charts ──────────────────────────────────────────────────────────────
  // One emphasis hue says "this one" (the bar the headline names); the rest stay quiet, as in the original.
  const ink = () => ({
    palette: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => css(`--chart-${i}`)),
    emphasis: css("--chart-emphasis"),
    quiet: css("--chart-quiet"),
    other: css("--chart-other"),
    grid: css("--chart-grid"),
    axis: css("--chart-axis"),
    text: css("--text"),
    surface: css("--surface"),
    border: css("--card-border"),
  });
  function rgba(color, a) {
    const m = color.match(/^#([0-9a-f]{6})$/i);
    if (!m) return color;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  }

  function base(c, extra = {}) {
    const axis = { gridcolor: c.grid, zeroline: false, showline: false, automargin: true, fixedrange: true, ticks: "", tickfont: { size: 11, color: c.axis }, title: { font: { size: 11, color: c.axis } } };
    return Object.assign({
      margin: { l: 44, r: 12, t: 8, b: 34 },
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "rgba(0,0,0,0)",
      font: { family: "Inter, system-ui, sans-serif", size: 12, color: c.axis },
      hoverlabel: { bgcolor: c.surface, bordercolor: c.border, font: { color: c.text, family: "Inter, sans-serif", size: 12.5 }, align: "left" },
      xaxis: Object.assign({}, axis, { showgrid: false }),
      yaxis: Object.assign({}, axis),
      showlegend: false,
      bargap: 0.34,
      hovermode: "closest",
      dragmode: false,
    }, extra);
  }

  function barColors(spec, c) {
    const focus = spec.focus;
    return (spec.labels || []).map((l) => (l === "Other" ? c.other : !focus || l === focus ? (focus ? c.emphasis : rgba(c.emphasis, 0.8)) : c.quiet));
  }

  function figure(spec) {
    const c = ink();
    const errors = (spec.ranges || []).some(Boolean)
      ? { type: "data", symmetric: false, thickness: 1.2, width: 3, color: c.axis,
          array: spec.ranges.map((r, i) => (r ? r[1] - spec.values[i] : 0)), arrayminus: spec.ranges.map((r, i) => (r ? spec.values[i] - r[0] : 0)) }
      : undefined;
    switch (spec.viz) {
      case "line":
      case "area": {
        const many = spec.series.length > 1;
        const data = spec.series.map((s, i) => {
          const color = many ? c.palette[i % c.palette.length] : c.emphasis;
          const trace = {
            type: "scatter", mode: spec.x.length > 36 ? "lines" : "lines+markers", name: s.name, x: spec.x, y: s.y,
            line: { color, width: many ? 2 : 2.4, shape: "spline", smoothing: 0.5 },
            marker: { size: 5, color, line: { width: 2, color: c.surface } },
            customdata: s.y.map((v) => fmt(v, spec)), connectgaps: true,
            hovertemplate: `<b>%{customdata}</b>${many ? " · " + s.name : ""}<extra></extra>`,
          };
          if (!many) {
            trace.fill = "tozeroy";
            trace.fillgradient = { type: "vertical", colorscale: [[0, rgba(color, 0)], [1, rgba(color, 0.28)]] };
          }
          return trace;
        });
        return { data, layout: base(c, { yaxis: Object.assign(base(c).yaxis, axisFormat(spec)), showlegend: many, legend: { orientation: "h", y: -0.18, font: { size: 11.5 } }, hovermode: "x unified",
          xaxis: Object.assign(base(c).xaxis, { showspikes: true, spikemode: "across", spikethickness: 1, spikecolor: c.axis, spikedash: "dot" }) }) };
      }
      case "bar":
        return {
          data: [{ type: "bar", x: spec.labels, y: spec.values, marker: { color: barColors(spec, c), cornerradius: 5 }, error_y: errors,
                   customdata: spec.text, hovertemplate: "%{x}<br><b>%{customdata}</b><extra></extra>" }],
          layout: base(c, { yaxis: Object.assign(base(c).yaxis, axisFormat(spec)) }),
        };
      case "hbar":
        return {
          data: [{ type: "bar", orientation: "h", y: spec.labels, x: spec.values, marker: { color: barColors(spec, c), cornerradius: 4 }, error_x: errors,
                   text: spec.text, textposition: "outside", textfont: { size: 11.5, color: c.axis }, cliponaxis: false, customdata: spec.text,
                   hovertemplate: "%{y}<br><b>%{customdata}</b><extra></extra>" }],
          layout: base(c, { margin: { l: 8, r: 52, t: 4, b: 8 }, bargap: 0.38, yaxis: Object.assign(base(c).yaxis, { autorange: "reversed", showgrid: false, tickfont: { size: 12, color: c.text } }),
                            xaxis: Object.assign(base(c).xaxis, axisFormat(spec), { showticklabels: false, showgrid: false }) }),
        };
      case "donut": {
        const total = spec.values.reduce((a, b) => a + (b || 0), 0);
        return {
          data: [{ type: "pie", hole: 0.7, labels: spec.labels, values: spec.values, sort: false, direction: "clockwise", rotation: 0,
                   marker: { colors: spec.labels.map((l, i) => (l === "Other" ? c.other : c.palette[i % c.palette.length])), line: { color: c.surface, width: 3 } },
                   textinfo: "none", customdata: spec.text, hovertemplate: "%{label}<br><b>%{customdata}</b> · %{percent}<extra></extra>" }],
          layout: base(c, { margin: { l: 8, r: 8, t: 8, b: 8 }, showlegend: true, legend: { orientation: "v", x: 1, xanchor: "left", y: 0.5, font: { size: 12, color: c.text } },
                            annotations: [{ text: `<span style="font-family:Inter Tight;font-size:22px;font-weight:600">${fmt(total, spec)}</span><br><span style="font-size:11px">${spec.measure}</span>`, showarrow: false, font: { color: c.text } }] }),
        };
      }
      case "scatter": {
        const fit = trendline(spec.x, spec.y);
        const data = [{ type: "scatter", mode: "markers", x: spec.x, y: spec.y, marker: { color: rgba(c.emphasis, 0.38), size: 6, line: { width: 0 } },
                        hovertemplate: `${spec.x_label}: %{x}<br>${spec.y_label}: <b>%{y}</b><extra></extra>` }];
        if (fit) data.push({ type: "scatter", mode: "lines", x: fit.x, y: fit.y, line: { color: c.palette[1], width: 2, dash: "dot" }, hoverinfo: "skip" });
        return { data, layout: base(c, { margin: { l: 52, r: 12, t: 8, b: 44 }, xaxis: Object.assign(base(c).xaxis, { showgrid: true, title: { text: spec.x_label, font: { size: 11.5 } } }),
                                         yaxis: Object.assign(base(c).yaxis, { title: { text: spec.y_label, font: { size: 11.5 } } }) }) };
      }
      case "histogram":
        return {
          data: [{ type: "bar", x: spec.x, y: spec.counts, customdata: spec.labels, marker: { color: rgba(c.emphasis, 0.75), cornerradius: 2 }, hovertemplate: "%{customdata}<br><b>%{y} rows</b><extra></extra>" }],
          layout: base(c, { bargap: 0.08, xaxis: Object.assign(base(c).xaxis, axisFormat(spec)) }),
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

  // A bar of a split filters the whole board by it, when that split is one of the board's filters.
  function filterInput(field, label) {
    return $$(`#filters input[name="f.${CSS.escape(field)}"]`).find((i) => i.value === String(label));
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
      const field = el.dataset.filterField;
      if (field && !el.dataset.rendered && (spec.labels || []).some((l) => filterInput(field, l))) {
        el.classList.add("clickable");
        el.on("plotly_click", (ev) => {
          const p = ev.points && ev.points[0];
          const input = p && filterInput(field, spec.viz === "hbar" ? p.y : p.x !== undefined ? p.x : p.label);
          if (!input) return;
          input.checked = !input.checked;
          input.dispatchEvent(new Event("change", { bubbles: true }));
        });
      }
      el.dataset.rendered = "1";
    });
  }

  // Headline figures count up once, from zero, the way the original's did.
  function countUp(root) {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    $$("[data-count]", root).forEach((el, i) => {
      if (el.dataset.counted) return;
      el.dataset.counted = "1";
      const m = el.dataset.count.match(/^([^\d-]*)(-?[\d,]*\.?\d+)(.*)$/);
      if (!m) return;
      const target = parseFloat(m[2].replace(/,/g, ""));
      const decimals = (m[2].split(".")[1] || "").length;
      const commas = m[2].includes(",");
      const start = performance.now() + i * 60;
      const step = (now) => {
        const t = Math.min(1, Math.max(0, (now - start) / 900));
        const v = target * (1 - Math.pow(1 - t, 3));
        let s = v.toFixed(decimals);
        if (commas) s = Number(s).toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
        el.textContent = m[1] + s + m[3];
        if (t < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
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
      if (file.size > maxBytes) return fail(`That file is ${(file.size / 1048576).toFixed(1)} MB; the most that can be uploaded here is ${zone.dataset.maxMb} MB. Import larger files from a link.`);
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
    const phrase = e.target.closest("[data-phrase]");
    if (phrase) {
      const input = $("#phrase");
      input.value = phrase.dataset.phrase;
      input.form.requestSubmit();
      return;
    }
    if (e.target.closest("[data-close-builder]")) {
      $("#step-builder").innerHTML = "";
      $$("[data-kind]").forEach((k) => k.removeAttribute("aria-pressed"));
      return;
    }
    const kind = e.target.closest("[data-kind]");
    if (kind) {
      $$("[data-kind]").forEach((k) => k.setAttribute("aria-pressed", String(k === kind)));
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
  // htmx:load fires on each piece of new content once it is in the page. afterSwap's target can be
  // the element an outerHTML swap just removed, so charts are drawn from here, never from there.
  document.addEventListener("htmx:load", (e) => {
    renderCharts(e.detail.elt.parentElement || document, false);
    if (e.detail.elt.parentElement && e.detail.elt.parentElement.id === "step-builder") e.detail.elt.scrollIntoView({ behavior: "smooth", block: "nearest" });
    countUp(e.detail.elt.parentElement || document);
    $$("[data-toast]", document).forEach(autoHide);
  });
  document.addEventListener("htmx:afterSwap", (e) => {
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
    if ([400, 402, 403].includes(e.detail.xhr.status)) { e.detail.shouldSwap = true; e.detail.isError = false; }
  });
  window.addEventListener("pageshow", () => $$("[aria-busy]").forEach((b) => b.removeAttribute("aria-busy")));

  document.addEventListener("DOMContentLoaded", () => {
    renderCharts(document, false);
    countUp(document);
    initUpload();
  });
})();

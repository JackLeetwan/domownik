/* Domownik – small client-side helpers: modal, recurrence form, timeline. */
(function () {
  "use strict";

  const store = {
    get(k) { try { return window.localStorage.getItem(k); } catch (_) { return null; } },
    set(k, v) { try { window.localStorage.setItem(k, v); } catch (_) { /* private mode */ } },
  };

  // ------------------------------------------------------------------ theme
  // auto (system) → light → dark → auto; stored per device
  const THEMES = {
    auto: ["🌓", "Motyw: automatyczny (jak w systemie)"],
    light: ["☀️", "Motyw: jasny"],
    dark: ["🌙", "Motyw: ciemny"],
  };
  const NEXT = { auto: "light", light: "dark", dark: "auto" };
  function currentTheme() {
    const t = store.get("dm-theme");
    return t === "light" || t === "dark" ? t : "auto";
  }
  function applyTheme(t) {
    if (t === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = t;
    const btn = document.getElementById("theme-toggle");
    if (btn) { btn.textContent = THEMES[t][0]; btn.title = THEMES[t][1]; }
  }
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest || !ev.target.closest("#theme-toggle")) return;
    const t = NEXT[currentTheme()];
    store.set("dm-theme", t);
    applyTheme(t);
  });
  applyTheme(currentTheme());
  document.addEventListener("htmx:afterSettle", () => applyTheme(currentTheme()));

  // ------------------------------------------------------------------ drawer ("Więcej" on phones)
  function setNav(open) { document.documentElement.classList.toggle("nav-open", open); }
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest) return;
    if (ev.target.closest("[data-nav-toggle]")) {
      setNav(!document.documentElement.classList.contains("nav-open"));
    } else if (ev.target.closest("[data-nav-close]") || ev.target.closest("#sidebar a")) {
      setNav(false);
    }
  });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") setNav(false); });
  document.addEventListener("htmx:beforeRequest", (ev) => { if (ev.detail.boosted) setNav(false); });

  // ------------------------------------------------------------------ copy to clipboard
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest && ev.target.closest("[data-copy]");
    if (!btn) return;
    const label = btn.textContent;
    try {
      await navigator.clipboard.writeText(btn.dataset.copy);
      btn.textContent = "✓ skopiowano";
    } catch (_) {
      window.prompt("Skopiuj:", btn.dataset.copy); // no clipboard API (plain http)
      return;
    }
    setTimeout(() => { btn.textContent = label; }, 2000);
  });

  // ------------------------------------------------------------------ errors
  // htmx swallows failed requests by default – then a click "does nothing".
  function toast(msg) {
    let el = document.getElementById("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast";
      el.setAttribute("role", "alert");
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.hidden = true; }, 6000);
  }
  document.addEventListener("htmx:sendError", (e) => {
    const cfg = e.detail.requestConfig || {};
    if (cfg.boosted && cfg.verb === "get") {
      window.location.href = cfg.path; // let the browser show its own error page
      return;
    }
    toast("Brak połączenia z serwerem Domownika – sprawdź, czy działa, i spróbuj ponownie.");
  });
  document.addEventListener("htmx:responseError", (e) => {
    const xhr = e.detail.xhr;
    const tmp = document.createElement("div");
    tmp.innerHTML = xhr.responseText || "";
    toast(`Błąd ${xhr.status}: ${tmp.textContent.trim().slice(0, 200) || xhr.statusText}`);
  });

  // ------------------------------------------------------------------ modal
  // look the dialog up at event time – hx-boost replaces the <body> content on every navigation
  const modalEl = () => document.getElementById("modal");
  document.addEventListener("htmx:afterSwap", (e) => {
    const modal = modalEl();
    if (e.detail.target && e.detail.target.id === "modal-body" && modal && !modal.open) {
      modal.showModal();
    }
  });
  document.addEventListener("click", (e) => {
    const modal = modalEl();
    if (!modal || !modal.open) return;
    if (e.target === modal || e.target.closest("[data-close]")) {
      e.preventDefault();
      modal.close();
    }
  });

  // ------------------------------------------------------------------ forms
  function syncRuleForm(root) {
    root.querySelectorAll("select[data-mode]").forEach((sel) => {
      if (sel.dataset.dmInit) return;
      sel.dataset.dmInit = "1";
      const form = sel.closest("form");
      const apply = () => {
        form.querySelectorAll("[data-for]").forEach((el) => {
          const off = el.dataset.for !== sel.value;
          el.hidden = off;
          // hidden fields must not block submission with native validation (and are not sent)
          const fields = el.matches("input, select, textarea") ? [el] : el.querySelectorAll("input, select, textarea");
          fields.forEach((f) => { f.disabled = off; });
        });
      };
      sel.addEventListener("change", apply);
      apply();
    });
    root.querySelectorAll(".pick-row").forEach((row) => {
      if (row.dataset.dmInit) return;
      row.dataset.dmInit = "1";
      const cb = row.querySelector("input[type=checkbox]");
      const date = row.querySelector("input[data-required-if-checked]");
      if (!cb || !date) return;
      const apply = () => { date.required = cb.checked; };
      cb.addEventListener("change", apply);
      apply();
    });
  }
  // htmx:load fires for the initial page and for every swapped-in fragment / boosted page
  document.addEventListener("htmx:load", (e) => {
    const root = e.detail.elt;
    syncRuleForm(root);
    const card = root.matches && root.matches(".timeline-card") ? root : root.querySelector && root.querySelector(".timeline-card");
    if (card) initTimeline(card);
  });

  // ------------------------------------------------------------------ timeline
  let current = null; // the live vis.Timeline – destroyed when a boosted navigation replaces the page

  function initTimeline(card) {
  const container = card.querySelector(".timeline");
  if (!container || container.dataset.dmInit || typeof vis === "undefined") return;
  container.dataset.dmInit = "1";
  if (current) { try { current.destroy(); } catch (_) { /* already gone */ } current = null; }
  const status = card.querySelector("[data-timeline-status]");
  function setStatus(text) { if (status) { status.textContent = text; status.hidden = !text; } }

  const DAY = 86400000;
  const itemId = card.dataset.item || "";
  const items = new vis.DataSet();
  let groups = null;
  let loaded = null; // {start, end} in ms
  let lanes = store.get("domownik.lanes") === "1";
  const lanesSwitch = card.querySelector("[data-lanes]");
  if (lanesSwitch) lanesSwitch.checked = lanes;

  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const savedRange = parseInt(store.get("domownik.range") || "106", 10);

  const fmtDay = new Intl.DateTimeFormat("pl-PL", { day: "numeric", month: "short" });
  const fmtMonth = new Intl.DateTimeFormat("pl-PL", { month: "short" });
  const fmtMonthYear = new Intl.DateTimeFormat("pl-PL", { month: "long", year: "numeric" });
  const fmtYear = new Intl.DateTimeFormat("pl-PL", { year: "numeric" });
  const fmtFull = new Intl.DateTimeFormat("pl-PL", { weekday: "short", day: "numeric", month: "long", year: "numeric" });
  const pln = new Intl.NumberFormat("pl-PL", { style: "currency", currency: "PLN", maximumFractionDigits: 0 });

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  const STATUS_TEXT = { overdue: "zaległe", today: "dziś", soon: "wkrótce", later: "zaplanowane", future: "kolejne wystąpienie", done: "wykonane" };

  function toItem(p, i) {
    const d = new Date(p.date + "T12:00:00");
    const amount = p.amount && p.direction !== "none" ? " · " + (p.direction === "income" ? "+" : "−") + pln.format(p.amount / 100) : "";
    const status = p.kind === "forecast" ? "forecast" : p.status;
    return {
      id: `${p.kind}-${p.task_id}-${p.date}-${i}`,
      task_id: p.task_id,
      start: d,
      type: "point",
      // only the next occurrence gets a label – repeats stay as quiet icons
      content: p.status === "future" || p.kind === "done"
        ? `<span class="tl-icon">${esc(p.icon)}</span>`
        : `<span class="tl-icon">${esc(p.icon)}</span><span class="tl-label">${esc(p.title)}</span>`,
      title: `<strong>${esc(p.icon)} ${esc(p.title)}</strong><br>${esc(p.zone)}<br>${esc(fmtFull.format(d))}${esc(amount)}<br><em>${esc(p.kind === "forecast" ? "prognoza z przebiegu" : STATUS_TEXT[p.status] || "")}</em>`,
      className: `st-${status}`,
      group: p.group,
    };
  }

  const timeline = new vis.Timeline(container, items, {
    start: new Date(today.getTime() - savedRange * 0.15 * DAY),
    end: new Date(today.getTime() + savedRange * 0.85 * DAY),
    zoomMin: 7 * DAY,
    zoomMax: 6 * 365 * DAY,
    showCurrentTime: true,
    stack: true,
    stackSubgroups: true,
    maxHeight: 340,
    minHeight: 120,
    margin: { item: { horizontal: 2, vertical: 4 }, axis: 6 },
    orientation: { axis: "top", item: "top" },
    selectable: false,
    tooltip: { followMouse: true, overflowMethod: "cap", delay: 150 },
    cluster: { maxItems: 4, titleTemplate: "{count} zadań – przybliż", showStipes: false, fitOnDoubleClick: true },
    groupOrder: "order",
    format: {
      minorLabels: (date, scale) => {
        const d = new Date(date.valueOf());
        if (scale === "year") return fmtYear.format(d);
        if (scale === "month") return fmtMonth.format(d);
        return fmtDay.format(d);
      },
      majorLabels: (date, scale) => {
        const d = new Date(date.valueOf());
        if (scale === "year" || scale === "month") return fmtYear.format(d);
        return fmtMonthYear.format(d);
      },
    },
  });

  async function load(force) {
    const w = timeline.getWindow();
    const span = w.end - w.start;
    if (!force && loaded && w.start >= loaded.start && w.end <= loaded.end) return;
    const start = new Date(w.start.getTime() - span);
    const end = new Date(w.end.getTime() + span);
    const iso = (d) => d.toISOString().slice(0, 10);
    const qs = new URLSearchParams({ start: iso(start), end: iso(end) });
    if (itemId) qs.set("item", itemId);
    let res;
    try {
      res = await fetch(`/api/timeline?${qs}`, { credentials: "same-origin" });
    } catch (_) {
      setStatus("Nie udało się wczytać osi czasu – brak połączenia z serwerem.");
      return;
    }
    if (res.status === 401) { window.location.href = "/login"; return; }
    if (!res.ok) { setStatus(`Nie udało się wczytać osi czasu (błąd ${res.status}).`); return; }
    const data = await res.json();
    groups = data.groups;
    items.clear();
    items.add(data.points.map(toItem));
    applyLanes();
    loaded = { start: start.getTime(), end: end.getTime() };
    setStatus(data.points.length ? "" : "Brak terminów w tym okresie.");
    // vis reveals itself only after its first range change; when that never comes
    // (container resized while loading) it would stay invisible – reveal it ourselves
    const root = container.querySelector(".vis-timeline");
    if (root && root.style.visibility === "hidden") { timeline.redraw(); root.style.visibility = "visible"; }
  }

  function applyLanes() {
    if (lanes && groups && groups.length > 1) {
      timeline.setGroups(new vis.DataSet(groups));
    } else {
      timeline.setGroups(null);
    }
  }

  timeline.on("rangechanged", () => load(false));
  timeline.on("click", (props) => {
    if (!props.item) return;
    const it = items.get(props.item);
    if (it && it.task_id) {
      htmx.ajax("GET", `/tasks/${it.task_id}`, { target: "#modal-body", swap: "innerHTML" });
    } else {
      // a cluster – zoom in around it
      const t = props.time.getTime();
      const w = timeline.getWindow();
      const span = (w.end - w.start) / 3;
      timeline.setWindow(new Date(t - span / 2), new Date(t + span / 2));
    }
  });

  card.querySelectorAll("[data-range]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const days = parseInt(btn.dataset.range, 10);
      store.set("domownik.range", String(days));
      timeline.setWindow(new Date(today.getTime() - days * 0.15 * DAY), new Date(today.getTime() + days * 0.85 * DAY));
    });
  });
  const todayBtn = card.querySelector("[data-today]");
  if (todayBtn) todayBtn.addEventListener("click", () => timeline.moveTo(new Date(today.getTime() + 0.35 * (timeline.getWindow().end - timeline.getWindow().start))));
  if (lanesSwitch) {
    lanesSwitch.addEventListener("change", () => {
      lanes = lanesSwitch.checked;
      store.set("domownik.lanes", lanes ? "1" : "0");
      applyLanes();
    });
  }

  current = timeline;
  load(true);
  }
})();

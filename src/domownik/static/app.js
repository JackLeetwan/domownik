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

  // ------------------------------------------------------------------ push notifications
  // Web Push straight from Domownik: service worker /sw.js + one subscription per browser.
  const PUSH_SNOOZE_DAYS = 7;
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
  const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  const pushSupported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const loggedIn = () => !!document.getElementById("sidebar");

  function csrf() {
    try { return JSON.parse(document.body.getAttribute("hx-headers"))["X-CSRF-Token"] || ""; } catch (_) { return ""; }
  }
  function keyBytes(b64) {
    const pad = "=".repeat((4 - (b64.length % 4)) % 4);
    const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }
  async function postJSON(url, body) {
    const r = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
  }
  async function pushRegistration() {
    await navigator.serviceWorker.register("/sw.js");
    return navigator.serviceWorker.ready;
  }
  async function pushSubscribe() {
    const reg = await pushRegistration();
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      const { key } = await (await fetch("/push/key", { credentials: "same-origin" })).json();
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
    }
    await postJSON("/push/subscribe", sub.toJSON());
    try { sessionStorage.setItem("dm-push-synced", "1"); } catch (_) { /* private mode */ }
  }
  async function pushEnable() {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") return permission;
    await pushSubscribe();
    return "granted";
  }
  async function pushDisable() {
    const reg = await pushRegistration();
    const sub = await reg.pushManager.getSubscription();
    if (sub) {
      await postJSON("/push/unsubscribe", { endpoint: sub.endpoint });
      await sub.unsubscribe();
    }
  }

  // settings page: what is the state of *this* device
  async function renderPushStatus(root) {
    const status = root.querySelector && root.querySelector("[data-push-status]");
    if (!status) return;
    const enableBtn = root.querySelector("[data-push-enable]");
    const disableBtn = root.querySelector("[data-push-disable]");
    const show = (text, on, off) => { status.textContent = text; enableBtn.hidden = !on; disableBtn.hidden = !off; };
    if (!pushSupported) {
      show(isIOS && !standalone
        ? "Na iPhonie: w Safari stuknij Udostępnij → „Do ekranu początkowego”, otwórz Domownika z nowej ikony i wróć tutaj."
        : "Ta przeglądarka nie obsługuje powiadomień. Na telefonie użyj Chrome (Android) albo Safari z ekranu początkowego (iPhone).", false, false);
      return;
    }
    if (Notification.permission === "denied") {
      show("Powiadomienia są zablokowane dla Domownika. Odblokuj je w ustawieniach przeglądarki (ikona kłódki przy adresie → Powiadomienia), potem odśwież stronę.", false, false);
      return;
    }
    let sub = null;
    try { sub = await (await pushRegistration()).pushManager.getSubscription(); } catch (_) { /* no SW */ }
    if (Notification.permission === "granted" && sub) show("✅ Powiadomienia są włączone na tym urządzeniu.", false, true);
    else show("Na tym urządzeniu powiadomienia są wyłączone.", true, false);
    if (!enableBtn.dataset.dmInit) {
      enableBtn.dataset.dmInit = disableBtn.dataset.dmInit = "1";
      enableBtn.addEventListener("click", async () => {
        enableBtn.disabled = true;
        try {
          const result = await pushEnable();
          if (result === "granted") window.location.reload();
          else renderPushStatus(document);
        } catch (e) { toast(`Nie udało się włączyć powiadomień: ${e.message}`); }
        enableBtn.disabled = false;
      });
      disableBtn.addEventListener("click", async () => {
        try { await pushDisable(); window.location.reload(); } catch (e) { toast(`Nie udało się: ${e.message}`); }
      });
    }
  }
  document.addEventListener("htmx:load", (e) => renderPushStatus(e.detail.elt));

  // once per page load: keep an existing subscription in sync, or ask (politely) on a new device
  async function pushOnLoad() {
    if (!loggedIn() || location.pathname.startsWith("/ustawienia")) return;
    const snoozed = Number(store.get("dm-push-snooze") || 0) > Date.now();
    if (!pushSupported) {
      if (isIOS && !standalone && !snoozed) askPush(true);
      return;
    }
    if (Notification.permission === "granted") {
      let synced = false;
      try { synced = sessionStorage.getItem("dm-push-synced") === "1"; } catch (_) { /* private mode */ }
      if (!synced) pushSubscribe().catch(() => { /* offline – next time */ });
    } else if (Notification.permission === "default" && !snoozed) {
      setTimeout(() => askPush(false), 1200);
    }
  }
  function askPush(iosHint) {
    const dlg = document.getElementById("push-ask");
    if (!dlg || dlg.open) return;
    const yes = dlg.querySelector("[data-push-ask-yes]");
    const no = dlg.querySelector("[data-push-ask-no]");
    if (iosHint) {
      dlg.querySelector("[data-push-ask-text]").textContent =
        "Na iPhonie powiadomienia działają, gdy Domownik jest na ekranie początkowym: stuknij Udostępnij → „Do ekranu początkowego”, a potem otwieraj go z ikony.";
      yes.hidden = true;
      no.textContent = "OK";
    }
    yes.onclick = async () => {
      dlg.close();
      try {
        const result = await pushEnable();
        if (result === "granted") toast("🔔 Powiadomienia włączone.");
        else if (result === "denied") toast("Powiadomienia zablokowane – możesz je włączyć później w Ustawieniach.");
      } catch (e) { toast(`Nie udało się włączyć powiadomień: ${e.message}`); }
    };
    no.onclick = () => {
      store.set("dm-push-snooze", String(Date.now() + PUSH_SNOOZE_DAYS * 86400000));
      dlg.close();
    };
    dlg.showModal();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", pushOnLoad);
  else pushOnLoad();

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
  // phones get their own (shorter) default range and a lower timeline – at most ~40% of the screen
  const phone = window.matchMedia("(max-width: 639.98px)").matches;
  const rangeKey = phone ? "domownik.range.phone" : "domownik.range";
  const savedRange = parseInt(store.get(rangeKey) || (phone ? "30" : "106"), 10);
  let loadedBin = null;
  let loadSeq = 0; // only the newest request may fill the timeline (zoom + lanes toggle can overlap)
  if (phone) lanes = false; // too narrow for zone lanes – the switch is hidden there

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
  const URGENT = { overdue: 1, today: 1, soon: 1 };
  const signed = (amount, direction) =>
    amount && direction !== "none" ? (direction === "income" ? "+" : "−") + pln.format(amount / 100) : "";

  // how coarse the server should bundle ordinary points: by pixels available per day
  function binFor() {
    const w = timeline ? timeline.getWindow() : { start: 0, end: savedRange * DAY };
    const pxPerDay = (container.clientWidth || 360) / ((w.end - w.start) / DAY);
    if (pxPerDay >= 8) return "day";
    if (pxPerDay >= 1.2) return "week";
    return "month";
  }

  function bundleItem(p, i) {
    const d = new Date(p.date + "T12:00:00");
    const from = new Date(p.start + "T12:00:00");
    const to = new Date(p.end + "T12:00:00");
    const when = p.start === p.end ? fmtFull.format(d) : `${fmtDay.format(from)} – ${fmtDay.format(to)}`;
    const icons = p.icons.map(esc).join("");
    const names = p.tasks.slice(0, 10).map((t) => `${esc(t.icon)} ${esc(t.title)}`).join("<br>");
    const more = p.tasks.length > 10 ? `<br>… i ${p.tasks.length - 10} więcej` : "";
    return {
      id: `bundle-${p.of}-${p.start}-${p.group}-${i}`,
      bundle: p,
      start: d,
      type: "point",
      // the next occurrences carry a name ("💳 Opłaty 17"), further repeats only icons + count
      content: `<span class="tl-icon">${icons}</span>${p.status === "later" && p.category ? `<span class="tl-label">${esc(p.label)}</span>` : ""}<span class="tl-count">${p.count}</span>`,
      title: `<strong>${icons} ${esc(p.label)}</strong><br>${esc(when)}<br>${esc(p.summary)}<br><small>${names}${more}</small><br><em>kliknij, żeby zobaczyć listę</em>`,
      className: `st-${p.status} tl-bundle`,
      group: p.group,
    };
  }

  function showBundle(p) {
    const modal = document.getElementById("modal");
    const body = document.getElementById("modal-body");
    if (!modal || !body) return;
    const d = new Date(p.date + "T12:00:00");
    const when = p.start === p.end ? fmtFull.format(d) : `${fmtDay.format(new Date(p.start + "T12:00:00"))} – ${fmtDay.format(new Date(p.end + "T12:00:00"))}`;
    const rows = p.tasks.map((t) => `<li><a href="#" hx-get="/tasks/${t.task_id}" hx-target="#modal-body" hx-push-url="false">`
      + `<span>${esc(t.icon)} ${esc(t.title)}</span><small class="muted">${esc(t.zone)} · ${esc(fmtDay.format(new Date(t.date + "T12:00:00")))}</small></a>`
      + `<span class="amount">${esc(signed(t.amount, t.direction))}</span></li>`).join("");
    body.innerHTML = `<header><button class="close" aria-label="Zamknij" data-close>✕</button>`
      + `<h3 id="modal-title">${p.icons.map(esc).join("")} ${esc(p.label)}</h3><p class="muted">${esc(when)} · ${esc(p.summary)}</p></header>`
      + `<ul class="bundle-list">${rows}</ul>`;
    htmx.process(body);
    if (!modal.open) modal.showModal();
  }

  function toItem(p, i) {
    if (p.kind === "bundle") return bundleItem(p, i);
    const d = new Date(p.date + "T12:00:00");
    const amount = signed(p.amount, p.direction) ? " · " + signed(p.amount, p.direction) : "";
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
      className: `st-${status}${URGENT[p.status] && p.kind === "due" ? " tl-urgent" : ""}`,
      group: p.group,
    };
  }

  let timeline = null;
  timeline = new vis.Timeline(container, items, {
    start: new Date(today.getTime() - savedRange * 0.15 * DAY),
    end: new Date(today.getTime() + savedRange * 0.85 * DAY),
    zoomMin: 7 * DAY,
    zoomMax: 6 * 365 * DAY,
    showCurrentTime: true,
    stack: true,
    stackSubgroups: true,
    maxHeight: phone ? Math.max(170, Math.round(window.innerHeight * 0.4) - 110) : 340,
    minHeight: phone ? 110 : 120,
    margin: { item: { horizontal: 2, vertical: 4 }, axis: 6 },
    orientation: { axis: "top", item: "top" },
    selectable: false,
    tooltip: { followMouse: true, overflowMethod: "cap", delay: 150 },
    // vis sanitises item HTML and drops every attribute – keep our classes (all text is escaped in esc())
    xss: { disabled: false, filterOptions: { whiteList: { span: ["class"], strong: [], em: [], br: [], small: [] } } },
    // no vis clustering: anonymous "17" bubbles hid urgent tasks – the server bundles ordinary points instead
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
    const bin = binFor();
    if (!force && bin === loadedBin && loaded && w.start >= loaded.start && w.end <= loaded.end) return;
    // preload one window on each side, but stay within the API limit (6 years in total)
    const pad = Math.max(0, Math.min(span, (6 * 365 * DAY - span) / 2));
    const start = new Date(w.start.getTime() - pad);
    const end = new Date(w.end.getTime() + pad);
    const iso = (d) => d.toISOString().slice(0, 10);
    const qs = new URLSearchParams({ start: iso(start), end: iso(end), bin });
    if (lanes) qs.set("lanes", "1");
    if (itemId) qs.set("item", itemId);
    const seq = ++loadSeq;
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
    if (seq !== loadSeq) return;
    groups = data.groups;
    items.clear();
    items.add(data.points.map(toItem));
    applyLanes();
    loaded = { start: start.getTime(), end: end.getTime() };
    loadedBin = bin;
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
    } else if (it && it.bundle) {
      showBundle(it.bundle);
    }
  });

  card.querySelectorAll("[data-range]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const days = parseInt(btn.dataset.range, 10);
      store.set(rangeKey, String(days));
      timeline.setWindow(new Date(today.getTime() - days * 0.15 * DAY), new Date(today.getTime() + days * 0.85 * DAY));
    });
  });
  const todayBtn = card.querySelector("[data-today]");
  if (todayBtn) todayBtn.addEventListener("click", () => timeline.moveTo(new Date(today.getTime() + 0.35 * (timeline.getWindow().end - timeline.getWindow().start))));
  if (lanesSwitch) {
    lanesSwitch.addEventListener("change", () => {
      lanes = lanesSwitch.checked;
      store.set("domownik.lanes", lanes ? "1" : "0");
      load(true); // bundles are per lane when lanes are on
    });
  }

  current = timeline;
  load(true);
  }
})();

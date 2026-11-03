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

  // ------------------------------------------------------------------ search box (sidebar / drawer)
  const searchPanel = () => document.getElementById("search-live");
  function closeSearch() { const p = searchPanel(); if (p) p.innerHTML = ""; }
  document.addEventListener("keydown", (ev) => {
    const input = document.getElementById("side-search");
    if (!input) return;
    if (ev.key === "Escape" && document.activeElement === input) { closeSearch(); input.blur(); return; }
    if (ev.key !== "/" || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const t = ev.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    const modal = document.getElementById("modal");
    if (modal && modal.open) return;
    if (!input.offsetParent) return; // phones: the box sits in the closed drawer
    ev.preventDefault();
    input.focus();
    input.select();
  });
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest) return;
    if (ev.target.closest("#search-live [data-search-hit]") || !ev.target.closest("[data-side-search]")) {
      // a hit opening the task window keeps the page – fold the results away after htmx has read the link
      setTimeout(closeSearch, 0);
    }
  });

  // ------------------------------------------------------------------ month calendar: selected day
  document.addEventListener("htmx:beforeRequest", (ev) => {
    const cell = ev.detail.elt;
    if (!cell || !cell.dataset || !cell.dataset.calDay) return;
    document.querySelectorAll(".cal-cell.sel").forEach((c) => { c.classList.remove("sel"); c.setAttribute("aria-selected", "false"); });
    cell.classList.add("sel");
    cell.setAttribute("aria-selected", "true");
  });

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

  // ------------------------------------------------------------------ offline: shopping list
  // The service worker keeps the last /zakupy page. Without signal (shop basement) taps on the list are
  // done on screen at once and queued here; they are sent when the connection is back.
  const QKEY = "dm-shop-queue";
  const loadQ = () => { try { return JSON.parse(store.get(QKEY) || "[]") || []; } catch (_) { return []; } };
  const saveQ = (q) => { store.set(QKEY, JSON.stringify(q)); showOffline(); };
  function changes(n) {
    if (n === 1) return "zmiana czeka";
    const few = [2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100);
    return few ? "zmiany czekają" : "zmian czeka";
  }
  function showOffline() {
    const bar = document.getElementById("offline-bar");
    if (!bar) return;
    const n = loadQ().length;
    const off = !navigator.onLine;
    bar.hidden = !off && !n;
    bar.textContent = off
      ? `📴 Bez sieci – lista zakupów działa dalej${n ? `; ${n} ${changes(n)} na wysłanie` : ""}.`
      : `⏳ Wysyłanie zmian z listy zakupów (${n})…`;
  }
  function rowOf(id) {
    const btn = document.querySelector(`#shopping-list .shop-check[data-sid="${id}"]`);
    return btn ? btn.closest("li") : null;
  }
  function paintCheck(id, to) {
    const li = rowOf(id);
    if (!li) return;
    const btn = li.querySelector(".shop-check");
    li.classList.toggle("in-cart", to === 1);
    btn.textContent = to === 1 ? "☑" : "☐";
    btn.setAttribute("hx-post", `/zakupy/${id}/check?to=${to === 1 ? 0 : 1}`);
    if (typeof htmx !== "undefined") htmx.process(btn);
  }
  function paintPending() {
    const list = document.getElementById("shopping-list");
    if (!list) return;
    list.querySelectorAll(".shop-pending").forEach((el) => el.remove());
    const adds = [];
    loadQ().forEach((op) => {
      if (op.op === "check") paintCheck(op.id, op.to);
      else if (op.op === "del") { const li = rowOf(op.id); if (li) li.remove(); }
      else if (op.op === "add") adds.push(op.text);
    });
    if (adds.length) {
      const ul = document.createElement("ul");
      ul.className = "shop-list shop-pending";
      adds.forEach((t) => {
        const li = document.createElement("li");
        li.className = "shop-row";
        li.innerHTML = '<span class="shop-check" aria-hidden="true">⏳</span><span class="shop-body"></span>';
        li.querySelector(".shop-body").textContent = t;
        ul.appendChild(li);
      });
      list.prepend(ul);
    }
  }
  // what a request to the shopping list means, as an op that can wait
  function shopOp(cfg) {
    if (!cfg || cfg.verb !== "post") return null;
    const path = (cfg.path || "").split("#")[0];
    let m = path.match(/^\/zakupy\/(\d+)\/check\?to=([01])$/);
    if (m) return { op: "check", id: +m[1], to: +m[2] };
    m = path.match(/^\/zakupy\/(\d+)\/delete$/);
    if (m) return { op: "del", id: +m[1] };
    if (path === "/zakupy" && cfg.elt && cfg.elt.matches && cfg.elt.matches("form.shop-add")) {
      const text = ((cfg.elt.querySelector("input[name=text]") || {}).value || "").trim();
      return text ? { op: "add", text } : null;
    }
    return null;
  }
  function queueOp(op, elt) {
    let q = loadQ();
    if (op.op === "check" || op.op === "del") q = q.filter((o) => !(o.id === op.id && o.op === "check"));
    q.push(op);
    saveQ(q);
    if (op.op === "add" && elt && elt.reset) elt.reset();
    paintPending();
  }
  document.addEventListener("htmx:beforeRequest", (e) => {
    if (navigator.onLine) return;
    const cfg = e.detail.requestConfig || {};
    if (cfg.verb === "get" && (cfg.path || "").startsWith("/zakupy/lista")) { e.preventDefault(); return; }
    const op = shopOp(Object.assign({ elt: e.detail.elt }, cfg));
    if (op) { e.preventDefault(); queueOp(op, e.detail.elt); }
  });
  document.addEventListener("htmx:sendError", (e) => {
    const cfg = e.detail.requestConfig || {};
    const op = shopOp(Object.assign({ elt: e.detail.elt }, cfg));
    if (op) { e.detail.dmOffline = true; queueOp(op, e.detail.elt); }
    else if (cfg.verb === "get" && (cfg.path || "").startsWith("/zakupy/lista")) e.detail.dmOffline = true;
  });
  let flushing = false;
  async function flushQueue() {
    if (flushing || !navigator.onLine || !loadQ().length) { showOffline(); return; }
    flushing = true;
    showOffline();
    try {
      for (let q = loadQ(); q.length; q = loadQ()) {
        const op = q[0];
        const url = op.op === "check" ? `/zakupy/${op.id}/check?to=${op.to}`
          : op.op === "del" ? `/zakupy/${op.id}/delete` : "/zakupy";
        const body = new URLSearchParams({ csrf: csrf() });
        if (op.op === "add") body.set("text", op.text);
        let r;
        try {
          r = await fetch(url, { method: "POST", credentials: "same-origin", body, headers: { "X-CSRF-Token": csrf() } });
        } catch (_) { break; } // still no connection – try later
        if (!r.ok && r.status !== 404 && r.status !== 400) break; // e.g. logged out: keep it
        saveQ(loadQ().slice(1));
      }
    } finally {
      flushing = false;
      showOffline();
      const list = document.getElementById("shopping-list");
      if (list && typeof htmx !== "undefined" && navigator.onLine) {
        htmx.ajax("GET", "/zakupy/lista", { target: "#shopping-list", swap: "outerHTML" });
      }
    }
  }
  window.addEventListener("online", flushQueue);
  window.addEventListener("offline", showOffline);
  setInterval(flushQueue, 30000);
  document.addEventListener("htmx:afterSwap", (e) => {
    if (e.detail.target && (e.detail.target.id === "shopping-list" || e.detail.target.querySelector?.("#shopping-list"))) paintPending();
  });
  // the service worker: offline copy of /zakupy (and push, when switched on)
  function warmOffline() {
    if (!("serviceWorker" in navigator) || !loggedIn()) return;
    navigator.serviceWorker.register("/sw.js").then(() => navigator.serviceWorker.ready).then((reg) => {
      const last = +(store.get("dm-offline-warm") || 0);
      if (!navigator.onLine || Date.now() - last < 10 * 60000 || !reg.active) return;
      store.set("dm-offline-warm", String(Date.now()));
      reg.active.postMessage({ type: "warm" });
    }).catch(() => {});
  }
  function offlineOnLoad() {
    showOffline();
    paintPending();
    warmOffline();
    flushQueue();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", offlineOnLoad);
  else offlineOnLoad();
  document.addEventListener("htmx:afterSettle", showOffline);

  // ------------------------------------------------------------------ errors
  // htmx swallows failed requests by default – then a click "does nothing".
  function toast(msg, ok) {
    let el = document.getElementById("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast";
      el.setAttribute("role", "alert");
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.toggle("ok", !!ok);
    el.hidden = false;
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.hidden = true; }, ok ? 2500 : 6000);
  }
  document.addEventListener("htmx:sendError", (e) => {
    const cfg = e.detail.requestConfig || {};
    if (e.detail.dmOffline) return; // the shopping list queued it
    if (cfg.boosted && cfg.verb === "get") {
      window.location.href = cfg.path; // let the browser show its own error page
      return;
    }
    toast("Brak połączenia z serwerem Domownika – sprawdź, czy działa, i spróbuj ponownie.");
  });
  document.addEventListener("htmx:responseError", (e) => {
    const xhr = e.detail.xhr;
    // an inert document: unlike a detached <div>, nothing in it loads or runs (<img onerror> included)
    const doc = new DOMParser().parseFromString(xhr.responseText || "", "text/html");
    const text = (doc.body ? doc.body.textContent : "").trim().slice(0, 200);
    // 400 = the server explains what to fix (e.g. a wrong amount) – show just that
    if (xhr.status >= 500) {
      // the server's own fault – its text ("Internal Server Error") says nothing to anyone at home
      toast(`Coś poszło nie tak po stronie Domownika (błąd ${xhr.status}). Spróbuj ponownie za chwilę.`);
      return;
    }
    toast(xhr.status === 400 && text ? text : `Błąd ${xhr.status}: ${text || xhr.statusText}`);
  });

  // ------------------------------------------------------------------ shopping: "Cofnij" after removing
  // the banner pauses the list's auto-refresh; it goes away by itself after a while
  document.addEventListener("htmx:afterSwap", () => {
    const undo = document.querySelector("#shopping-list .shop-undo");
    if (undo) setTimeout(() => undo.remove(), 8000);
  });

  // ------------------------------------------------------------------ notes: filter cards as you type
  document.addEventListener("input", (e) => {
    const box = e.target.closest && e.target.closest("[data-note-filter]");
    if (!box) return;
    const q = box.value.trim().toLowerCase();
    let shown = 0;
    document.querySelectorAll("[data-notes-grid] .note-card").forEach((card) => {
      const hit = !q || (card.dataset.noteText || "").includes(q);
      card.hidden = !hit;
      if (hit) shown += 1;
    });
    const none = document.querySelector("[data-notes-none]");
    if (none) none.hidden = shown > 0;
  });

  // ------------------------------------------------------------------ dictation (🎤, Web Speech API)
  // Only where the browser can do it (Chrome on Android, Safari); the buttons stay hidden elsewhere.
  // For a list ("mleko chleb i masło") "i" / "oraz" become commas – the server splits on those.
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const showMics = () => {
    if (!Recognition) return;
    document.querySelectorAll("[data-dictate][hidden]").forEach((b) => { b.hidden = false; });
  };
  showMics();
  document.addEventListener("htmx:afterSettle", showMics);
  document.addEventListener("htmx:load", showMics);
  let listening = null;
  document.addEventListener("click", (e) => {
    const btn = e.target.closest && e.target.closest("[data-dictate]");
    if (!btn || !Recognition) return;
    e.preventDefault();
    if (listening) { listening.stop(); return; }
    const form = btn.closest("form");
    const field = form && form.querySelector(btn.dataset.dictate);
    if (!field) return;
    const rec = new Recognition();
    rec.lang = "pl-PL";
    rec.interimResults = false;
    rec.maxAlternatives = 1;
    const asList = btn.dataset.dictateList === "1";
    rec.onresult = (ev) => {
      let text = Array.from(ev.results).map((r) => r[0].transcript).join(" ").trim();
      if (asList) text = text.replace(/\s+(i|oraz|a także)\s+/gi, ", ");
      if (!text) return;
      const sep = field.value.trim() ? (field.tagName === "TEXTAREA" ? "\n" : ", ") : "";
      field.value = field.value.trim() + sep + text;
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.focus();
    };
    rec.onerror = (ev) => {
      if (ev.error === "not-allowed" || ev.error === "service-not-allowed") toast("Brak zgody na mikrofon – włącz ją w ustawieniach przeglądarki.");
      else if (ev.error !== "no-speech" && ev.error !== "aborted") toast("Dyktowanie nie zadziałało – spróbuj jeszcze raz.");
    };
    rec.onend = () => { btn.classList.remove("listening"); btn.setAttribute("aria-pressed", "false"); listening = null; };
    btn.classList.add("listening");
    btn.setAttribute("aria-pressed", "true");
    listening = rec;
    rec.start();
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

  // ------------------------------------------------------------------ quick add ("＋")
  // a quick-add form was saved (HX-Trigger: dm-added): close the window, say so, refresh the page under it
  document.addEventListener("dm-added", (e) => {
    const modal = modalEl();
    if (modal && modal.open) modal.close();
    toast((e.detail && e.detail.value) || "Dodano", true);
    if (typeof htmx === "undefined" || !document.querySelector(".shell")) return;
    htmx.ajax("GET", location.pathname + location.search, {
      target: ".shell", select: ".shell", swap: "outerHTML", headers: { "HX-Boosted": "true" },
    });
  });
  // calendar: double click on a day (or its "＋") opens "＋ Dodaj" for that day – event, task, expense
  const addOnDay = (iso) => {
    if (typeof htmx === "undefined" || !/^\d{4}-\d{2}-\d{2}$/.test(iso || "")) return;
    htmx.ajax("GET", `/dodaj?d=${iso}`, { target: "#modal-body", swap: "innerHTML" });
  };
  document.addEventListener("dblclick", (e) => {
    if (!e.target.closest || e.target.closest(".cal-chip, a, button, input, select, textarea, summary")) return;
    const cell = e.target.closest("[data-cal-day], .cal-week-day[data-sel]");
    if (!cell) return;
    e.preventDefault();
    addOnDay(cell.dataset.calDay || cell.dataset.sel);
  });
  document.addEventListener("click", (e) => {
    const btn = e.target.closest && e.target.closest("[data-cal-add]");
    if (btn) addOnDay(btn.dataset.calAdd);
  });
  // ------------------------------------------------------------------ confirm
  // the app's own dialog instead of the browser's confirm() (hx-confirm everywhere + deleting a task)
  function askConfirm(text, okLabel) {
    const dlg = document.getElementById("confirm-dlg");
    if (!dlg || typeof dlg.showModal !== "function") return Promise.resolve(window.confirm(text));
    const yes = dlg.querySelector("[data-confirm-yes]");
    const no = dlg.querySelector("[data-confirm-no]");
    dlg.querySelector("#confirm-text").textContent = text;
    const destructive = /^(usunąć|usuń|skasować)/i.test(text.trim());
    yes.textContent = okLabel || (destructive ? "🗑 Usuń" : "Tak");
    yes.classList.toggle("danger", destructive);
    return new Promise((resolve) => {
      const done = (value) => {
        yes.onclick = no.onclick = dlg.oncancel = null;
        if (dlg.open) dlg.close();
        resolve(value);
      };
      yes.onclick = () => done(true);
      no.onclick = () => done(false);
      dlg.oncancel = (e) => { e.preventDefault(); done(false); }; // Esc closes only this, not the task window
      dlg.showModal();
      yes.focus();
    });
  }
  document.addEventListener("htmx:confirm", (e) => {
    if (!e.detail.question) return;
    e.preventDefault();
    askConfirm(e.detail.question).then((ok) => { if (ok) e.detail.issueRequest(true); });
  });

  // deleting a task: "🗑 Usuń" in its window, or the Delete key on a focused calendar entry / open window
  const refreshShell = () => {
    if (typeof htmx !== "undefined" && document.querySelector(".shell")) {
      htmx.ajax("GET", location.pathname + location.search, {
        target: ".shell", select: ".shell", swap: "outerHTML", headers: { "HX-Boosted": "true" },
      });
    } else location.reload();
  };
  async function deleteTask(el) {
    const title = el.dataset.taskTitle || "to zadanie";
    const question = el.dataset.taskRec
      ? `Usunąć „${title}” razem ze wszystkimi przyszłymi terminami i historią? Żeby odpuścić tylko ten jeden termin, użyj „Pomiń termin”.`
      : `Usunąć „${title}”?`;
    if (!(await askConfirm(question))) return;
    const headers = JSON.parse(document.body.getAttribute("hx-headers") || "{}");
    try {
      const r = await fetch(`/tasks/${el.dataset.taskDel}/delete`, {
        method: "POST", headers: { ...headers, "HX-Request": "true", "HX-Current-URL": location.href },
      });
      if (!r.ok) { toast("Nie udało się usunąć", false); return; }
    } catch (err) {
      toast("Brak połączenia – nie usunięto", false);
      return;
    }
    const modal = document.getElementById("modal");
    if (modal && modal.open) modal.close();
    toast(`Usunięto „${title}”`, true);
    if (location.pathname.startsWith("/tasks/")) location.href = "/kalendarz";
    else refreshShell();
  }
  document.addEventListener("click", (e) => {
    const btn = e.target.closest && e.target.closest(".task-del[data-task-del]");
    if (!btn) return;
    e.preventDefault();
    deleteTask(btn);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Delete" && e.key !== "Backspace") return;
    const t = e.target;
    if (t.closest && t.closest("input, textarea, select, [contenteditable]")) return;
    const confirmDlg = document.getElementById("confirm-dlg");
    if (confirmDlg && confirmDlg.open) return;
    const modal = document.getElementById("modal");
    let el = null;
    if (modal && modal.open) el = modal.querySelector(".task-del[data-task-del]");
    else if (t.closest) el = t.closest("[data-task-del]");
    if (!el) return;
    e.preventDefault();
    deleteTask(el);
  });

  // calendar: drag a task's current date onto another day = "Przełóż" (mouse; on a phone use "Przełóż")
  let dragged = null;
  const dropDay = (el) => el && el.closest && el.closest("[data-cal-day], .cal-week-day[data-sel]");
  const dayOf = (cell) => cell.dataset.calDay || cell.dataset.sel;
  document.addEventListener("dragstart", (e) => {
    const src = e.target.closest && e.target.closest("[data-drag-task]");
    if (!src) return;
    dragged = { id: src.dataset.dragTask, from: src.dataset.dragFrom };
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", dragged.id);
    document.body.classList.add("cal-dragging");
  });
  document.addEventListener("dragend", () => {
    dragged = null;
    document.body.classList.remove("cal-dragging");
    document.querySelectorAll(".drop-over").forEach((c) => c.classList.remove("drop-over"));
  });
  document.addEventListener("dragover", (e) => {
    const cell = dragged && dropDay(e.target);
    if (!cell) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    document.querySelectorAll(".drop-over").forEach((c) => c !== cell && c.classList.remove("drop-over"));
    cell.classList.add("drop-over");
  });
  document.addEventListener("drop", async (e) => {
    const cell = dragged && dropDay(e.target);
    if (!cell) return;
    e.preventDefault();
    const { id, from } = dragged;
    const to = dayOf(cell);
    cell.classList.remove("drop-over");
    if (!to || to === from) return;
    const headers = JSON.parse(document.body.getAttribute("hx-headers") || "{}");
    const body = new URLSearchParams({ to });
    try {
      const r = await fetch(`/tasks/${id}/postpone`, { method: "POST", headers: { ...headers, "HX-Request": "true" }, body });
      if (!r.ok) {
        const msg = (await r.text()).replace(/<[^>]+>/g, "").trim();
        toast(msg || "Nie udało się przełożyć", false);
        return;
      }
    } catch (err) {
      toast("Brak połączenia – nie przełożono", false);
      return;
    }
    const [y, m, d] = to.split("-");
    toast(`Przełożono na ${d}.${m}`, true);
    if (typeof htmx !== "undefined" && document.querySelector(".shell")) {
      htmx.ajax("GET", location.pathname + location.search, {
        target: ".shell", select: ".shell", swap: "outerHTML", headers: { "HX-Boosted": "true" },
      });
    } else location.reload();
  });
  // the app icon's "＋ Dodaj" shortcut (manifest) opens /?dodaj=1 – show the menu, then drop the flag
  window.addEventListener("load", () => {
    const params = new URLSearchParams(location.search);
    if (params.get("dodaj") !== "1" || typeof htmx === "undefined") return;
    history.replaceState(null, "", location.pathname);
    htmx.ajax("GET", "/dodaj", { target: "#modal-body", swap: "innerHTML" });
  });
  // "Więcej opcji…" of the quick task form opens the full form for the item picked
  document.addEventListener("change", (e) => {
    const sel = e.target;
    if (!sel.matches || !sel.matches(".qa-form select[name=item_id]")) return;
    const more = sel.form.querySelector("[data-qa-more]");
    if (more && sel.value) more.href = `/tasks/new?item=${encodeURIComponent(sel.value)}`;
  });

  // ------------------------------------------------------------------ meters: the kind suggests the unit
  document.addEventListener("change", (e) => {
    const sel = e.target;
    if (!sel.matches || !sel.matches("select[data-meter-kind]")) return;
    const unit = sel.form && sel.form.querySelector("[data-meter-unit]");
    const opt = sel.selectedOptions[0];
    if (unit && opt && opt.dataset.unit) unit.value = opt.dataset.unit;
  });

  // ------------------------------------------------------------------ documents
  // file inputs hidden behind "Zrób zdjęcie" / "Wybierz pliki": show what was picked
  document.addEventListener("change", (e) => {
    const input = e.target;
    if (!input.matches || !input.matches("input[type=file][data-file-pick]")) return;
    const form = input.closest("form");
    const out = form && form.querySelector("[data-file-names]");
    if (!out) return;
    const names = [];
    form.querySelectorAll("input[type=file][data-file-pick]").forEach((i) => {
      Array.from(i.files || []).forEach((f) => names.push(f.name));
    });
    if (!out.dataset.hint) out.dataset.hint = out.textContent;
    out.textContent = names.length ? `Wybrano (${names.length}): ${names.join(", ")}` : out.dataset.hint;
    out.classList.toggle("muted", !names.length);
  });
  document.addEventListener("click", (e) => {
    const btn = e.target.closest && e.target.closest("[data-doc-more]");
    if (!btn) return;
    const box = btn.previousElementSibling;
    if (box) box.classList.add("show-all");
    btn.remove();
  });

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
  // Short task form: "Powtarzanie" choice reveals the full rule editor ("Inaczej…") and the series end
  function syncTaskForm(root) {
    const forms = root.matches && root.matches("form[data-task-form]") ? [root] : root.querySelectorAll ? root.querySelectorAll("form[data-task-form]") : [];
    forms.forEach((form) => {
      if (form.dataset.dmInit) return;
      form.dataset.dmInit = "1";
      const freq = form.querySelector("select[name=freq]");
      const mode = form.querySelector("select[name=mode]");
      const end = form.querySelector("select[name=end]");
      const due = form.querySelector("input[name=next_due_date]");
      const km = form.querySelector("input[data-km]");
      // hidden fields must not block native validation and are not sent (disabled)
      const show = (el, on) => {
        el.hidden = !on;
        const fields = el.matches("input, select, textarea") ? [el] : el.querySelectorAll("input, select, textarea");
        fields.forEach((f) => { f.disabled = !on; });
      };
      const apply = () => {
        const f = freq.value;
        const custom = f === "custom" || f === "km";
        const m = f === "km" ? "interval" : mode.value;
        form.querySelectorAll("[data-freq-for]").forEach((el) => show(el, el.dataset.freqFor.split(" ").includes(f)));
        form.querySelectorAll("[data-for]").forEach((el) => { if (custom) show(el, el.dataset.for === m); });
        form.querySelectorAll("[data-repeating]").forEach((el) => show(el, f !== "once"));
        form.querySelectorAll("[data-end-for]").forEach((el) => show(el, f !== "once" && el.dataset.endFor === end.value));
        // a date is needed unless the schedule itself says when (calendar rules, mileage)
        due.required = !(f === "km" || (f === "custom" && m !== "interval"));
        if (km) km.required = f === "km";
      };
      [freq, mode, end].forEach((el) => el.addEventListener("change", apply));
      apply();
    });
  }
  // htmx:load fires for the initial page and for every swapped-in fragment / boosted page
  document.addEventListener("htmx:load", (e) => {
    const root = e.detail.elt;
    syncRuleForm(root);
    syncTaskForm(root);
    // details folded to one line on phones (item facts) start open where there is room
    if (root.querySelectorAll && window.matchMedia("(min-width: 1024px)").matches) {
      root.querySelectorAll("details[data-open-wide]").forEach((d) => { d.open = true; });
    }
    const card = root.matches && root.matches(".timeline-card") ? root : root.querySelector && root.querySelector(".timeline-card");
    if (card) initTimeline(card);
    updateBulk();
  });

  // ------------------------------------------------------------------ bulk assignment (/przypisania)
  function updateBulk() {
    const bar = document.querySelector("[data-bulk]");
    if (!bar) return;
    const n = document.querySelectorAll("input[name=task_id][form=bulk]:checked").length;
    bar.querySelector("[data-bulk-count]").textContent = n;
    bar.hidden = n === 0;
    bar.querySelectorAll("button").forEach((b) => { b.disabled = n === 0; });
  }
  document.addEventListener("change", (e) => {
    const all = e.target.closest && e.target.closest("[data-check-all]");
    if (all) {
      all.closest("section").querySelectorAll("input[name=task_id]").forEach((cb) => { cb.checked = all.checked; });
    }
    if (all || e.target.matches("input[name=task_id][form=bulk]")) updateBulk();
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
    const rows = p.tasks.map((t) => `<li><a href="/tasks/${t.task_id}" hx-get="/tasks/${t.task_id}" hx-target="#modal-body" hx-push-url="false">`
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

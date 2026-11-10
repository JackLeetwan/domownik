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

  // ------------------------------------------------------------------ a form that comes back to this page
  // A boosted form (✓ Zamknij, ☐ zrobione, ＋ wpis…) posts, and the server answers with the same page;
  // htmx then scrolls to the top as for any new page. When the answer is the page we are on, stay where
  // we were. Going to another page still starts at the top.
  let formScroll = null;
  document.addEventListener("htmx:beforeRequest", (e) => {
    const elt = e.detail.elt;
    formScroll = e.detail.boosted && elt && elt.tagName === "FORM" ? { path: location.pathname, y: window.scrollY } : null;
  });
  document.addEventListener("htmx:afterSettle", (e) => {
    if (!formScroll || !e.detail.boosted) return;
    const back = formScroll;
    formScroll = null;
    // where the answer came from (after the server's redirect) – the address bar may still be updating
    const url = e.detail.xhr && e.detail.xhr.responseURL;
    // htmx scrolls to the top right after this event – put the page back in the next frame, before paint
    if (url && new URL(url).pathname === back.path) requestAnimationFrame(() => window.scrollTo(0, back.y));
  });

  // ------------------------------------------------------------------ notes: fold to the head
  // On the notes board ([data-notes-board]) cards are folded to label + title + a short preview, pinned
  // ones open; a tap on the head (or ▾) opens / folds one, and this device remembers that per note.
  // A card swapped in after ticking a point keeps its state (htmx:load runs for the new card too).
  const NOTES_KEY = "dm-notes-open";
  const notesState = () => { try { return JSON.parse(localStorage.getItem(NOTES_KEY)) || {}; } catch (_) { return {}; } };
  const saveNotesState = (st) => { try { localStorage.setItem(NOTES_KEY, JSON.stringify(st)); } catch (_) { /* private mode */ } };
  function setNoteOpen(card, open) {
    card.classList.add("foldable");
    card.classList.toggle("collapsed", !open);
    const btn = card.querySelector("[data-note-toggle]");
    if (btn) {
      btn.setAttribute("aria-expanded", String(open));
      btn.setAttribute("aria-label", open ? "Zwiń notatkę" : "Rozwiń notatkę");
    }
  }
  function foldNotes(root) {
    const cards = root.matches && root.matches(".note-card") ? [root] : [...root.querySelectorAll(".note-card")];
    const st = notesState();
    for (const card of cards) {
      if (!card.closest("[data-notes-board]")) continue;
      setNoteOpen(card, card.id in st ? st[card.id] : card.classList.contains("pinned"));
    }
  }
  document.addEventListener("htmx:load", (e) => foldNotes(e.detail.elt));
  document.addEventListener("click", (e) => {
    const all = e.target.closest("[data-notes-all]");
    if (all) {
      const open = all.dataset.notesAll === "open";
      const st = notesState();
      document.querySelectorAll("[data-notes-grid] .note-card.foldable").forEach((card) => { setNoteOpen(card, open); st[card.id] = open; });
      saveNotesState(st);
      return;
    }
    const head = e.target.closest(".note-card.foldable [data-note-head]");
    if (!head) return;
    // links, the 🔒 label etc. keep working; only the head itself and ▾ fold
    if (e.target.closest("a, input, select, textarea, label, form") || (e.target.closest("button") && !e.target.closest("[data-note-toggle]"))) return;
    const card = head.closest(".note-card");
    const open = card.classList.contains("collapsed");
    setNoteOpen(card, open);
    const st = notesState();
    st[card.id] = open;
    saveNotesState(st);
  });

  // ------------------------------------------------------------------ sections folded to their heading
  // Inside [data-fold-scope="key"] every <section> that starts with a heading (h2/h3, or a header with
  // one) folds to that heading; a tap on it (or ▾) opens / folds, remembered on this device per scope.
  // Default: data-fold-default on the section, else open when it holds something urgent (a task overdue,
  // due today or soon), else folded. Only CSS hides the rest – nothing is moved, htmx targets keep working.
  const FOLD_KEY = "dm-fold";
  const foldState = () => { try { return JSON.parse(localStorage.getItem(FOLD_KEY)) || {}; } catch (_) { return {}; } };
  const saveFoldState = (st) => { try { localStorage.setItem(FOLD_KEY, JSON.stringify(st)); } catch (_) { /* private mode */ } };
  const URGENT = ".status-overdue, .status-today, .status-soon, [data-fold-urgent]";
  function foldHead(sec) {
    const first = sec.firstElementChild;
    if (!first) return null;
    if (/^H[23]$/.test(first.tagName)) return first;
    if (first.tagName === "HEADER" && first.querySelector("h2, h3")) return first;
    return null;
  }
  function foldId(scope, sec, head) {
    const name = sec.id || (head.querySelector("h2, h3") || head).textContent.replace(/\(\d+\)|\d+/g, "").trim().slice(0, 40);
    return `${scope.dataset.foldScope}:${name}`;
  }
  function setFold(sec, open) {
    sec.classList.toggle("collapsed", !open);
    // looked up each time: a page restored from htmx's history cache has the button but no JS state
    const btn = sec.querySelector(":scope > .fold-head .fold-toggle");
    if (btn) {
      btn.setAttribute("aria-expanded", String(open));
      btn.setAttribute("aria-label", open ? "Zwiń sekcję" : "Rozwiń sekcję");
    }
  }
  function foldSections(root) {
    const scopes = new Set();
    const own = root.closest && root.closest("[data-fold-scope]");
    if (own) scopes.add(own);
    if (root.querySelectorAll) root.querySelectorAll("[data-fold-scope]").forEach((s) => scopes.add(s));
    const st = foldState();
    for (const scope of scopes) {
      const secs = [...scope.querySelectorAll("section")];
      if (root.matches && root.matches("section")) secs.push(root);
      for (const sec of secs) {
        if (sec.classList.contains("fold") || sec.closest("[data-fold-scope]") !== scope) continue;
        const head = foldHead(sec);
        if (!head) continue;
        sec.classList.add("fold");
        head.classList.add("fold-head");
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "fold-toggle";
        btn.textContent = "▾";
        btn.title = "Zwiń / rozwiń";
        head.appendChild(btn);
        sec.dataset.foldId = foldId(scope, sec, head);
        const def = sec.dataset.foldDefault ? sec.dataset.foldDefault === "open" : !!sec.querySelector(URGENT);
        setFold(sec, sec.dataset.foldId in st ? st[sec.dataset.foldId] : def);
      }
    }
  }
  document.addEventListener("htmx:load", (e) => foldSections(e.detail.elt));
  document.addEventListener("click", (e) => {
    const head = e.target.closest(".fold > .fold-head");
    if (!head) return;
    if (e.target.closest("a, input, select, textarea, label, form") || (e.target.closest("button") && !e.target.closest(".fold-toggle"))) return;
    const sec = head.parentElement;
    const open = sec.classList.contains("collapsed");
    setFold(sec, open);
    const st = foldState();
    st[sec.dataset.foldId] = open;
    saveFoldState(st);
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

  // ------------------------------------------------------------------ "saving…" on a slow connection
  // a submit that takes more than a moment shows Pico's spinner on its (labelled) button
  document.addEventListener("htmx:beforeRequest", (e) => {
    if (e.defaultPrevented || !e.detail.xhr) return;
    const cfg = e.detail.requestConfig || {};
    const elt = e.detail.elt;
    let btn = cfg.triggeringEvent && cfg.triggeringEvent.submitter;
    if (!btn && elt && elt.matches && elt.matches("button")) btn = elt;
    if (!btn && elt && elt.tagName === "FORM") btn = elt.querySelector("button[type=submit], button:not([type])");
    if (!btn || btn.textContent.trim().length < 3) return; // ✓ / ✕ icons stay as they are
    const timer = setTimeout(() => btn.setAttribute("aria-busy", "true"), 350);
    e.detail.xhr.addEventListener("loadend", () => { clearTimeout(timer); btn.removeAttribute("aria-busy"); }, { once: true });
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

  // ------------------------------------------------------------------ ⏱ timers on the dashboard
  // the end comes from the server (data-timer-ends, ms); the countdown is counted here, the push by the server
  const timerText = (ms) => {
    const mins = Math.ceil(Math.abs(ms) / 60000);
    const h = Math.floor(mins / 60), m = mins % 60;
    const span = h ? `${h} godz.${m ? ` ${m} min` : ""}` : `${m} min`;
    return ms > 0 ? `zostało ${span}` : (mins <= 1 ? "koniec!" : `koniec – czeka ${span}`);
  };
  const tickTimers = () => {
    for (const row of document.querySelectorAll("[data-timer-ends]")) {
      const left = Number(row.dataset.timerEnds) - Date.now();
      const out = row.querySelector(".timer-left");
      if (out) out.textContent = timerText(left);
      row.classList.toggle("timer-over", left <= 0);
    }
  };
  tickTimers();
  setInterval(tickTimers, 15000);
  document.addEventListener("htmx:afterSettle", tickTimers);

  // ------------------------------------------------------------------ a new version after a deploy
  // hx-boost swaps only <body>: the <head> (app.css, app.js) stays from the first load. When a page comes
  // with other asset versions (#asset-v), load it fully once so the new styles and scripts apply.
  const assetV = () => { const t = document.getElementById("asset-v"); return t ? t.dataset.v : null; };
  const loadedAssetV = assetV();
  document.addEventListener("htmx:afterSettle", (e) => {
    if (!e.detail.boosted) return;
    const v = assetV();
    if (v && loadedAssetV && v !== loadedAssetV) location.reload();
  });

  // ------------------------------------------------------------------ 🏋️ the rest between sets
  // data-rest-ends (ms) from the server; counted down here every second, a buzz at the end (the push comes
  // from the server too, for a phone in a pocket)
  let restBuzzed = null;
  const tickRest = () => {
    const bar = document.querySelector("[data-rest-ends]");
    if (!bar) return;
    const ends = Number(bar.dataset.restEnds), left = Math.round((ends - Date.now()) / 1000);
    const out = bar.querySelector(".wo-rest-left");
    const mmss = (s) => `${Math.floor(Math.abs(s) / 60)}:${String(Math.abs(s) % 60).padStart(2, "0")}`;
    if (out) out.textContent = left > 0 ? `Przerwa ${mmss(left)}` : `Do roboty! (+${mmss(left)})`;
    bar.classList.toggle("over", left <= 0);
    if (left <= 0 && restBuzzed !== ends) {
      restBuzzed = ends;
      if (navigator.vibrate) { try { navigator.vibrate([250, 120, 250]); } catch (_) { /* not allowed */ } }
    }
  };
  setInterval(tickRest, 1000);
  document.addEventListener("htmx:afterSettle", tickRest);
  // 👫 training together: #wo-partner is polled; its counts go into the badges by each exercise, and when the
  // partner changed the shared list (data-sig differs) the workout reloads – not while typing a set
  let quietReload = false;
  document.addEventListener("htmx:afterSettle", (e) => {
    const strip = e.detail.elt;
    if (!strip || strip.id !== "wo-partner") return;
    strip.querySelectorAll("[data-badge-src]").forEach((src) => {
      const badge = document.querySelector(`[data-badge="${src.dataset.badgeSrc}"]`);
      if (!badge) return;
      badge.textContent = src.textContent;
      badge.classList.toggle("done", src.dataset.done === "1");
    });
    const wo = document.getElementById("workout");
    const typing = wo && wo.contains(document.activeElement) && document.activeElement.matches("input");
    if (wo && strip.dataset.sig && wo.dataset.sig !== strip.dataset.sig && !typing && typeof htmx !== "undefined") {
      quietReload = true;
      htmx.ajax("GET", location.pathname, { target: "#workout", swap: "outerHTML" });
    }
  });
  // after a set the next one to do is in view
  document.addEventListener("htmx:afterSettle", (e) => {
    if (!e.detail.elt || e.detail.elt.id !== "workout") return;
    if (quietReload) { quietReload = false; return; }
    const cur = document.querySelector(".wo-ex.current");
    if (cur) cur.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });

  // ------------------------------------------------------------------ "there is more" cue on scroll strips
  // ------------------------------------------------------------------ menu groups that fold
  // <details data-side-group> in the sidebar: this device remembers the folded ones; the group of the page
  // you are on ([data-here]) always stays open.
  const SIDE_KEY = "dm-side-folded";
  const sideFolded = () => { try { return JSON.parse(localStorage.getItem(SIDE_KEY)) || []; } catch (_) { return []; } };
  const foldSideGroups = (root) => (root.querySelectorAll ? root : document).querySelectorAll("[data-side-group]").forEach((g) => {
    g.open = g.hasAttribute("data-here") || !sideFolded().includes(g.dataset.sideGroup);
  });
  document.addEventListener("htmx:load", (e) => foldSideGroups(e.detail.elt));
  document.addEventListener("toggle", (e) => {
    const g = e.target;
    if (!g.matches || !g.matches("[data-side-group]")) return;
    const folded = new Set(sideFolded());
    if (g.open) folded.delete(g.dataset.sideGroup); else folded.add(g.dataset.sideGroup);
    try { localStorage.setItem(SIDE_KEY, JSON.stringify([...folded])); } catch (_) { /* private mode */ }
    requestAnimationFrame(scrollHints);
  }, true);

  // [data-scroll-hint]: .more-start / .more-end fade the edge that hides content (category chips, the drawer);
  // [data-scroll-more] inside one is shown while its end is out of sight and scrolls there on a tap
  const scrollHint = (el) => {
    const x = el.scrollWidth - el.clientWidth > 2, y = el.scrollHeight - el.clientHeight > 2;
    const pos = x ? el.scrollLeft : el.scrollTop, max = x ? el.scrollWidth - el.clientWidth : el.scrollHeight - el.clientHeight;
    el.classList.toggle("more-start", (x || y) && pos > 4);
    el.classList.toggle("more-end", (x || y) && pos < max - 4);
    const more = el.querySelector("[data-scroll-more]");
    if (more) more.hidden = !(y && pos < max - 40);
  };
  const scrollHints = () => document.querySelectorAll("[data-scroll-hint]").forEach((el) => {
    const cur = el.querySelector('[aria-current="true"]');  // the chosen chip in view after the page loads
    if (cur && el.scrollWidth > el.clientWidth && cur.offsetLeft + cur.offsetWidth > el.clientWidth) el.scrollLeft = cur.offsetLeft - 16;
    scrollHint(el);
  });
  document.addEventListener("scroll", (e) => { if (e.target.matches && e.target.matches("[data-scroll-hint]")) scrollHint(e.target); }, { capture: true, passive: true });
  document.addEventListener("click", (e) => {
    const more = e.target.closest("[data-scroll-more]");
    if (more) { const box = more.closest("[data-scroll-hint]"); box.scrollTo({ top: box.scrollHeight, behavior: "smooth" }); }
    if (e.target.closest("[data-nav-toggle]")) requestAnimationFrame(scrollHints);  // the drawer just opened
  });
  window.addEventListener("resize", scrollHints, { passive: true });
  document.addEventListener("htmx:afterSettle", scrollHints);

  // [data-autosubmit]: a field that saves itself (the palette of a note category) – "change" fires once
  // the colour picker is closed, not on every drag
  document.addEventListener("change", (e) => {
    const el = e.target.closest && e.target.closest("[data-autosubmit]");
    if (el && el.form) el.form.requestSubmit();
  });
  scrollHints();

  // 📏 a photo picked for a pose (front / side / back): shown in its tile before saving
  document.addEventListener("change", (e) => {
    const input = e.target;
    if (!input.matches || !input.matches("input[type=file][data-pose-pick]")) return;
    const box = input.closest(".body-pose") && input.closest(".body-pose").querySelector("[data-pose-preview]");
    const file = input.files && input.files[0];
    if (!box || !file) return;
    const img = document.createElement("img");
    img.alt = "";
    img.src = URL.createObjectURL(file);
    box.replaceChildren(img);
    box.classList.add("picked");
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
      payDefaults(form, freq);
    });
  }
  // New task in a zone full of bills: an amount makes it a monthly payment, no amount – a one-off
  // of the usual category. Only fields the user has not changed by hand follow the amount.
  function payDefaults(form, freq) {
    const payCat = form.dataset.payCategory;
    const amount = form.querySelector("input[name=amount]");
    if (!payCat || !amount) return;
    const cat = form.querySelector("select[name=category]");
    const bud = form.querySelector("select[name=budget_category_id]");
    const plain = { freq: freq.value, cat: cat && cat.value, bud: bud && bud.value };
    const paid = { freq: "month", cat: payCat, bud: form.dataset.payBudget };
    const touched = new Set();
    let auto = false;
    [["freq", freq], ["cat", cat], ["bud", bud]].forEach(([k, el]) => {
      if (el) el.addEventListener("change", () => { if (!auto) touched.add(k); });
    });
    const set = (k, el, v) => {
      if (!el || touched.has(k) || v === undefined || el.value === v) return;
      if (v !== "" && !el.querySelector(`option[value="${CSS.escape(v)}"]`)) return;
      auto = true;
      el.value = v;
      el.dispatchEvent(new Event("change", { bubbles: true })); // rule preview + shown fields
      auto = false;
    };
    const note = form.querySelector("[data-pay-note]");
    const follow = () => {
      const want = amount.value.trim() ? paid : plain;
      set("freq", freq, want.freq);
      set("cat", cat, want.cat);
      set("bud", bud, want.bud);
      // the category sits in the folded "Więcej szczegółów" – say what changed
      if (note) note.hidden = !(want === paid && cat && cat.value === payCat && !touched.has("cat"));
    };
    amount.addEventListener("input", follow);
    follow();
  }
  // "Powiadom" means something only with an hour: greyed out (and not sent) while "Godzina" is empty
  function syncRemind(form) {
    const time = form && form.querySelector("input[name=due_time]");
    const remind = form && form.querySelector("select[name=remind_minutes]");
    if (time && remind) remind.disabled = !time.value;
  }
  document.addEventListener("input", (e) => { if (e.target.matches("input[name=due_time]")) syncRemind(e.target.form); });
  document.addEventListener("change", (e) => { if (e.target.matches("input[name=due_time]")) syncRemind(e.target.form); });
  document.addEventListener("htmx:load", (e) => {
    const root = e.detail.elt;
    if (!root.querySelectorAll) return;
    const forms = new Set([...root.querySelectorAll("input[name=due_time]")].map((i) => i.form));
    if (root.matches && root.matches("form")) forms.add(root);
    forms.forEach(syncRemind);
  });
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
    // phones: two icons at most – the count says the rest, the tap opens the list
    const shownIcons = phone ? p.icons.slice(0, 2).map(esc).join("") : icons;
    const names = p.tasks.slice(0, 10).map((t) => `${esc(t.icon)} ${esc(t.title)}`).join("<br>");
    const more = p.tasks.length > 10 ? `<br>… i ${p.tasks.length - 10} więcej` : "";
    return {
      id: `bundle-${p.of}-${p.start}-${p.group}-${i}`,
      bundle: p,
      start: d,
      type: "point",
      // the next occurrences carry a name ("💳 Opłaty 17"), further repeats only icons + count
      content: `<span class="tl-icon">${shownIcons}</span>${!phone && p.status === "later" && p.category ? `<span class="tl-label">${esc(p.label)}</span>` : ""}<span class="tl-count">${p.count}</span>`,
      title: `<strong>${icons} ${esc(p.label)}</strong><br>${esc(when)}<br>${esc(p.summary)}<br><small>${names}${more}</small><br><em>kliknij, żeby zobaczyć listę</em>`,
      className: `st-${p.status} tl-bundle`,
      group: p.group,
    };
  }

  // a done point opens that completion, not the form for ticking off the next occurrence
  function taskUrl(t) {
    return `/tasks/${t.task_id}` + (t.completion_id ? `?wykonanie=${t.completion_id}` : "");
  }

  function showBundle(p) {
    const modal = document.getElementById("modal");
    const body = document.getElementById("modal-body");
    if (!modal || !body) return;
    const d = new Date(p.date + "T12:00:00");
    const when = p.start === p.end ? fmtFull.format(d) : `${fmtDay.format(new Date(p.start + "T12:00:00"))} – ${fmtDay.format(new Date(p.end + "T12:00:00"))}`;
    const rows = p.tasks.map((t) => `<li><a href="${taskUrl(t)}" hx-get="${taskUrl(t)}" hx-target="#modal-body" hx-push-url="false">`
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
      completion_id: p.completion_id,
      start: d,
      type: "point",
      // only the next occurrence gets a label – repeats stay as quiet icons
      // phones: a name only where it matters now (overdue, today, soon) – long names ran over the next weeks
      content: p.status === "future" || p.kind === "done" || (phone && !URGENT[p.status])
        ? `<span class="tl-icon">${esc(p.icon)}</span>`
        : `<span class="tl-icon">${esc(p.icon)}</span><span class="tl-label">${esc(p.title)}</span>`,
      title: `<strong>${esc(p.icon)} ${esc(p.title)}</strong><br>${esc(p.zone)}<br>${esc(fmtFull.format(d))}${esc(amount)}<br><em>${esc(p.kind === "forecast" ? "prognoza z przebiegu" : STATUS_TEXT[p.status] || "")}</em>`,
      className: `st-${status}${URGENT[p.status] && p.kind === "due" ? " tl-urgent" : ""}`,
      group: p.group,
    };
  }

  // vis spaces minor ticks for labels of this many characters (default 7). On a phone a 3-month window
  // then got month ticks only; planned for 2-3 characters it picks weeks, labelled with the day number.
  const PHONE_DENSE = 3;
  function minorChars(span) {
    return phone && span > 45 * DAY && span < 200 * DAY ? PHONE_DENSE : 7;
  }
  function spanNow() {
    if (!timeline) return savedRange * DAY;
    const w = timeline.getWindow();
    return w.end - w.start;
  }

  // weeks start on Monday (vis rounds week ticks with its bundled moment, English locale = Sunday)
  if (vis.moment && vis.moment.updateLocale) vis.moment.updateLocale("en", { week: { dow: 1, doy: 4 } });

  let timeline = null;
  let plannedChars = minorChars(savedRange * DAY);
  timeline = new vis.Timeline(container, items, {
    start: new Date(today.getTime() - savedRange * 0.15 * DAY),
    end: new Date(today.getTime() + savedRange * 0.85 * DAY),
    zoomMin: 7 * DAY,
    zoomMax: 6 * 365 * DAY,
    showCurrentTime: true,
    // a tick per week (vis jumps from months straight to every 2nd day without it) – "1 mies." had no dates at all
    showWeekScale: true,
    stack: true,
    stackSubgroups: true,
    // phones: room for ~6 rows ("Rok" hid its last row behind a scroll inside the card)
    maxHeight: phone ? Math.max(230, Math.round(window.innerHeight * 0.5) - 110) : 340,
    minHeight: phone ? 110 : 120,
    // phones: a wider gap – a long urgent name ran right up to the next dot and read as one entry
    margin: { item: { horizontal: phone ? 10 : 2, vertical: phone ? 3 : 4 }, axis: 6 },
    maxMinorChars: plannedChars,
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
        // week ticks: vis adds a tick on the 1st of the month too – unlabelled, it crowded the Monday next to it
        if (scale === "week" && d.getDate() === 1 && d.getDay() !== 1) return "";
        // phones, ~3 months: a tick per week, labelled with the day only ("9") – "9 lis" does not fit;
        // the month is in the major label above
        if (phone && (scale === "week" || scale === "day") && minorChars(spanNow()) === PHONE_DENSE) return String(d.getDate());
        return fmtDay.format(d);
      },
      majorLabels: (date, scale) => {
        const d = new Date(date.valueOf());
        if (scale === "year" || scale === "month") return fmtYear.format(d);
        return fmtMonthYear.format(d);
      },
    },
  });

  // the range being fetched right now: vis reports its first range change while the initial load is
  // still on its way – without this the same points were fetched twice on every page load
  let pending = null;
  const covers = (r, w, bin) => r && r.bin === bin && w.start >= r.start && w.end <= r.end;

  async function load(force) {
    const w = timeline.getWindow();
    const span = w.end - w.start;
    const bin = binFor();
    if (!force && (covers(loaded && { ...loaded, bin: loadedBin }, w, bin) || covers(pending, w, bin))) return;
    // preload one window on each side, but stay within the API limit (6 years in total)
    const pad = Math.max(0, Math.min(span, (6 * 365 * DAY - span) / 2));
    const start = new Date(w.start.getTime() - pad);
    const end = new Date(w.end.getTime() + pad);
    const iso = (d) => d.toISOString().slice(0, 10);
    const qs = new URLSearchParams({ start: iso(start), end: iso(end), bin });
    if (lanes) qs.set("lanes", "1");
    if (itemId) qs.set("item", itemId);
    const seq = ++loadSeq;
    const req = { start: start.getTime(), end: end.getTime(), bin };
    pending = req;
    let data;
    try {
      let res;
      try {
        res = await fetch(`/api/timeline?${qs}`, { credentials: "same-origin" });
      } catch (_) {
        setStatus("Nie udało się wczytać osi czasu – brak połączenia z serwerem.");
        return;
      }
      if (res.status === 401) { window.location.href = "/login"; return; }
      if (!res.ok) { setStatus(`Nie udało się wczytać osi czasu (błąd ${res.status}).`); return; }
      data = await res.json();
    } finally {
      // answered or failed: from now on `loaded` decides (a failed range may be asked for again)
      if (pending === req) pending = null;
    }
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

  timeline.on("rangechanged", () => {
    // the label width vis plans its ticks for follows the zoom (see minorChars)
    const chars = minorChars(spanNow());
    if (chars !== plannedChars) { plannedChars = chars; timeline.setOptions({ maxMinorChars: chars }); }
    load(false);
  });
  timeline.on("click", (props) => {
    if (!props.item) return;
    const it = items.get(props.item);
    if (it && it.task_id) {
      htmx.ajax("GET", taskUrl(it), { target: "#modal-body", swap: "innerHTML" });
    } else if (it && it.bundle) {
      showBundle(it.bundle);
    }
  });

  // the chosen range looks pressed, like every other view switch in the app
  const markRange = (days) => card.querySelectorAll("[data-range]").forEach((b) => b.setAttribute("aria-pressed", String(parseInt(b.dataset.range, 10) === days)));
  markRange(savedRange);
  card.querySelectorAll("[data-range]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const days = parseInt(btn.dataset.range, 10);
      markRange(days);
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

// ---------------------------------------------------------------- menu: start loading on touch
// On the small phone server a page takes ~30–120 ms to compute; the tap ("click") comes ~100 ms after
// the finger touches the screen. Touching a menu link ([data-preload]) asks the service worker to fetch
// the page right away, with the headers htmx will send for that boosted click; the click then gets
// that answer (used once, kept at most a few seconds – nothing is cached for later).
(() => {
  if (!("serviceWorker" in navigator)) return;
  document.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !navigator.serviceWorker.controller) return;
    const a = e.target.closest("[data-preload] a[href]");
    if (!a || a.target || a.hasAttribute("download") || a.getAttribute("hx-boost") === "false") return;
    const url = new URL(a.href, location.href);
    if (url.origin !== location.origin || url.hash || url.href === location.href) return;
    fetch(url.href, {
      credentials: "same-origin",
      headers: { "HX-Request": "true", "HX-Boosted": "true", "HX-Current-URL": location.href, "X-DM-Preload": "1" },
    }).catch(() => {});
  }, { passive: true, capture: true });
})();

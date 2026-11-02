/* Domownik service worker – shows push notifications and handles their buttons.
   Served at /sw.js (scope "/"). No offline caching yet. */
"use strict";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let msg = {};
  try { msg = event.data ? event.data.json() : {}; } catch (_) { msg = { body: event.data && event.data.text() }; }
  const actions = (msg.actions || []).map((a) => ({ action: a.action, title: a.title }));
  event.waitUntil(
    self.registration.showNotification(msg.title || "Domownik", {
      body: msg.body || "",
      icon: "/static/icon-192.png",
      badge: "/static/badge-96.png",
      tag: msg.tag || "domownik",
      renotify: true,
      requireInteraction: !!msg.urgent,
      data: { url: msg.url || "/", actions: msg.actions || [] },
      actions,
    }),
  );
});

async function openApp(url) {
  const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const c of all) {
    if (new URL(c.url).origin === self.location.origin && "focus" in c) {
      await c.focus();
      if ("navigate" in c) { try { await c.navigate(url); } catch (_) { /* cross-state */ } }
      return;
    }
  }
  await self.clients.openWindow(url);
}

self.addEventListener("notificationclick", (event) => {
  const n = event.notification;
  const data = n.data || {};
  n.close();
  const done = (data.actions || []).find((a) => a.action === event.action);
  if (done) {
    // "✓ task" – tick it off without opening the app (the phone must reach Domownik, e.g. Tailscale on)
    event.waitUntil(
      fetch(done.url, { method: "POST", credentials: "omit" })
        .then((r) => r.text().then((text) => ({ ok: r.ok, text })))
        .then(({ ok, text }) => self.registration.showNotification(ok ? "✓ " + text : "Nie udało się odhaczyć", {
          body: ok ? "" : "Otwórz Domownika i odhacz tam.",
          icon: "/static/icon-192.png",
          badge: "/static/badge-96.png",
          tag: "domownik-done",
        }))
        .catch(() => openApp(data.url || "/")),
    );
    return;
  }
  event.waitUntil(openApp(data.url || "/"));
});

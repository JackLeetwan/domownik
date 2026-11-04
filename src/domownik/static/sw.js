/* Domownik service worker – push notifications and their buttons, plus an offline copy of the shopping list.
   Served at /sw.js (scope "/").

   Offline: /zakupy and /zakupy/lista are network-first (a slow shop connection falls back to the copy after
   a few seconds). Static files with ?v=N come from the cache without asking the server again (a new N is a new
   URL); other static files stale-while-revalidate. Without a connection any page opens the shopping list.
   Taps made offline are queued by app.js, not here. */
"use strict";

const VERSION = "dm-2";
const PAGES = VERSION + "-pages";
const STATIC = VERSION + "-static";
const OFFLINE_PAGES = ["/zakupy", "/zakupy/lista"];
const NET_TIMEOUT = 3500;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil((async () => {
  for (const key of await caches.keys()) if (!key.startsWith(VERSION)) await caches.delete(key);
  await self.clients.claim();
})()));

function cacheable(resp) {
  return resp && resp.ok && !resp.redirected && resp.type === "basic";
}

async function savePage(path, resp) {
  if (!cacheable(resp)) return;
  const cache = await caches.open(PAGES);
  await cache.put(path, resp);
}

async function warm() {
  for (const path of OFFLINE_PAGES) {
    try { await savePage(path, await fetch(path, { credentials: "same-origin" })); } catch (_) { /* offline */ }
  }
}

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "warm") event.waitUntil(warm());
});

function offlineResponse() {
  return new Response(
    "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
    + "<title>Domownik – bez sieci</title><p style='font:1.1rem system-ui;padding:2rem'>📴 Brak połączenia z Domownikiem."
    + " Otwórz go raz z siecią, żeby lista zakupów była dostępna także offline.</p>",
    { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } },
  );
}

async function networkFirst(request, path) {
  const cache = await caches.open(PAGES);
  const net = fetch(request).then((resp) => {
    if (cacheable(resp)) cache.put(path, resp.clone());
    return resp;
  });
  const timeout = new Promise((resolve) => setTimeout(resolve, NET_TIMEOUT, null));
  try {
    const first = await Promise.race([net.catch(() => null), timeout]);
    if (first) return first;
  } catch (_) { /* fall through */ }
  const cached = await cache.match(path);
  if (cached) return cached;
  try { return await net; } catch (_) { return offlineResponse(); }
}

// versioned file (?v=N): once cached it never changes – no download on every page load
// (on a small home server those background downloads competed with rendering the page itself)
async function cacheFirst(request) {
  const cache = await caches.open(STATIC);
  const cached = await cache.match(request);
  if (cached) return cached;
  const resp = await fetch(request);
  if (cacheable(resp)) {
    await cache.put(request, resp.clone());
    // drop the older versions of the same file
    const path = new URL(request.url).pathname;
    for (const old of await cache.keys()) {
      if (old.url !== request.url && new URL(old.url).pathname === path) await cache.delete(old);
    }
  }
  return resp;
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(STATIC);
  const cached = await cache.match(request);
  const net = fetch(request).then((resp) => {
    if (cacheable(resp)) cache.put(request, resp.clone());
    return resp;
  }).catch(() => cached);
  return cached || net;
}

// ---- menu pages loaded on touch (app.js): the answer waits here for the click that follows – once, briefly
const PRELOAD_TTL = 3000;
const preloaded = new Map(); // key → { at, response: Promise<Response> }

// the headers htmx sends for a boosted click that the server looks at – a preload must match them all
function preloadKey(req) {
  const h = req.headers;
  return [req.url, h.get("HX-Request"), h.get("HX-Boosted"), h.get("HX-Current-URL")].join("\n");
}

function preload(req) {
  const now = Date.now();
  for (const [k, v] of preloaded) if (now - v.at > PRELOAD_TTL) preloaded.delete(k);
  const key = preloadKey(req);
  if (!preloaded.has(key)) {
    const headers = new Headers(req.headers);
    headers.delete("X-DM-Preload");
    const response = fetch(new Request(req, { headers }));
    response.catch(() => preloaded.delete(key));
    preloaded.set(key, { at: now, response });
  }
  return new Response(null, { status: 204 });
}

function takePreloaded(req) {
  const key = preloadKey(req);
  const hit = preloaded.get(key);
  if (!hit) return null;
  preloaded.delete(key);
  if (Date.now() - hit.at > PRELOAD_TTL) return null;
  return hit.response.catch(() => fetch(req)); // a failed preload: ask again as if nothing happened
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.headers.get("X-DM-Preload") === "1") {
    event.respondWith(preload(req));
    return;
  }
  if (req.headers.get("HX-Boosted") === "true") {
    const hit = takePreloaded(req);
    if (hit) {
      // the shopping list keeps its offline copy fresh exactly as without the preload
      event.respondWith(!OFFLINE_PAGES.includes(url.pathname) ? hit : hit.then(async (resp) => {
        if (cacheable(resp)) await (await caches.open(PAGES)).put(url.pathname, resp.clone());
        return resp;
      }));
      return;
    }
  }
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(url.searchParams.has("v") ? cacheFirst(req) : staleWhileRevalidate(req));
    return;
  }
  if (OFFLINE_PAGES.includes(url.pathname)) {
    event.respondWith(networkFirst(req, url.pathname));
    return;
  }
  if (req.mode === "navigate") {
    // any other page: the network; without it, the shopping list
    event.respondWith(fetch(req).catch(async () => (await caches.match("/zakupy", { cacheName: PAGES })) || offlineResponse()));
  }
});

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

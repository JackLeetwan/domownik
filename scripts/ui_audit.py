"""UI/UX audit in a real browser: crawls the app as a phone (light + dark) and a desktop and reports horizontal
scrolling, small touch targets, unlabelled fields and buttons, low contrast, tiny fonts and JS errors per page.

Run against a dev server on a COPY of the data (it only reads, but it does log in):

    DOMOWNIK_AUDIT_EMAIL=... DOMOWNIK_AUDIT_PASSWORD=... \
        uv run --with playwright python scripts/ui_audit.py [http://localhost:8765] [max pages]

Set PLAYWRIGHT_CHROMIUM to a chromium binary if the one matching the playwright version is not installed.
"""

import collections
import os
import re
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765"
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 400
EMAIL, PW = os.environ["DOMOWNIK_AUDIT_EMAIL"], os.environ["DOMOWNIK_AUDIT_PASSWORD"]

CHECK = r"""
() => {
  const out = [];
  const vw = window.innerWidth;
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && !el.closest('dialog:not([open]), [hidden], details:not([open]) > :not(summary)'); };
  const name = el => (el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || el.value || el.getAttribute('alt') || '').trim();
  const desc = el => { let d = el.tagName.toLowerCase(); if (el.id) d += '#' + el.id; if (el.className && typeof el.className === 'string') d += '.' + el.className.trim().split(/\s+/).slice(0,2).join('.'); const t = name(el).slice(0, 30).replace(/\s+/g,' '); return t ? d + ' «' + t + '»' : d; };
  if (document.documentElement.scrollWidth > vw + 1) {
    const wide = [...document.querySelectorAll('body *')].filter(e => { const r = e.getBoundingClientRect(); return vis(e) && r.right > vw + 1 && !e.closest('[style*="overflow"], .scroll-x, table, pre') && getComputedStyle(e).position !== 'fixed'; }).slice(-3).map(desc);
    out.push(['overflow-x', `${document.documentElement.scrollWidth}px > ${vw}px: ${wide.join(' | ')}`]);
  }
  for (const el of document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button]')) {
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    const inline = el.tagName === 'A' && el.closest('p, li, td, .muted, small') && !el.className;
    if (!inline && (r.height < 32 || r.width < 32) && !(el.type === 'checkbox' || el.type === 'radio') && vw < 500)
      out.push(['tap-small', `${desc(el)} ${Math.round(r.width)}×${Math.round(r.height)}`]);
    if (['BUTTON','A','SUMMARY'].includes(el.tagName) && !name(el) && !el.querySelector('img[alt]:not([alt=""]), svg title'))
      out.push(['no-name', desc(el) + ' ' + el.outerHTML.slice(0, 80)]);
    if (['INPUT','SELECT','TEXTAREA'].includes(el.tagName) && !['submit','button'].includes(el.type)) {
      const lab = el.labels && el.labels.length || el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.getAttribute('title');
      if (!lab) out.push(['no-label', `${desc(el)} name=${el.name} ph=${el.placeholder||''}`]);
    }
  }
  for (const img of document.querySelectorAll('img')) if (!img.hasAttribute('alt')) out.push(['img-alt', img.src.slice(-40)]);
  const ids = {}; for (const e of document.querySelectorAll('[id]')) ids[e.id] = (ids[e.id]||0)+1;
  for (const [k,v] of Object.entries(ids)) if (v > 1) out.push(['dup-id', `${k} ×${v}`]);
  if (!document.querySelector('h1')) out.push(['no-h1', '']);
  if (!document.title.trim()) out.push(['no-title', '']);
  // contrast for text nodes
  const lum = c => { const m = c.match(/[\d.]+/g); if (!m) return null; let [r,g,b,a=1] = m.map(Number); if (c.startsWith('color(')) { r*=255; g*=255; b*=255; } return {l: [r,g,b].map(v => { v/=255; return v <= .03928 ? v/12.92 : ((v+.055)/1.055)**2.4; }).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0), a}; };
  const bgOf = el => { for (let e = el; e; e = e.parentElement) { const b = getComputedStyle(e).backgroundColor; const L = lum(b); if (L && L.a > .5) return L.l; if (getComputedStyle(e).backgroundImage !== 'none') return null; } return lum(getComputedStyle(document.body).backgroundColor)?.l ?? 1; };
  const seen = new Set();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const t = walker.currentNode; if (!t.textContent.trim()) continue; const el = t.parentElement; if (!el || seen.has(el) || !vis(el)) continue; seen.add(el);
    const s = getComputedStyle(el); const fg = lum(s.color); const bg = bgOf(el); if (!fg || bg == null) continue;
    let op = 1; for (let e = el; e; e = e.parentElement) op *= Number(getComputedStyle(e).opacity);
    if (op < .99 || fg.a < .99) continue;
    const ratio = (Math.max(fg.l,bg)+.05)/(Math.min(fg.l,bg)+.05);
    const fs = parseFloat(s.fontSize); const big = fs >= 24 || (fs >= 18.6 && Number(s.fontWeight) >= 700);
    if (ratio < (big ? 3 : 4.5)) out.push(['contrast', `${ratio.toFixed(2)} ${fs}px ${desc(el)}`]);
    if (fs < 12) out.push(['tiny-font', `${fs}px ${desc(el)}`]);
  }
  return out;
}
"""


def main():
    issues = collections.defaultdict(lambda: collections.defaultdict(set))  # kind -> detail -> pages
    errors = []
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM") or None)
        for label, vp, scheme in [
            ("phone", {"width": 390, "height": 844}, "light"),
            ("phone-dark", {"width": 390, "height": 844}, "dark"),
            ("desktop", {"width": 1280, "height": 900}, "light"),
        ]:
            ctx = br.new_context(
                viewport=vp,
                color_scheme=scheme,
                is_mobile=vp["width"] < 500,
                has_touch=vp["width"] < 500,
                locale="pl-PL",
            )
            pg = ctx.new_page()
            pg.on("console", lambda m, label=label, pg=pg: m.type == "error" and errors.append((label, pg.url, m.text)))
            pg.on("pageerror", lambda e, label=label, pg=pg: errors.append((label, pg.url, str(e))))
            pg.goto(BASE + "/login")
            pg.fill("input[name=email]", EMAIL)
            pg.fill("input[name=password]", PW)
            pg.click("form button[type=submit]")
            pg.wait_for_load_state("networkidle")
            todo, seen = ["/"], set()
            if label != "phone":
                todo = list(PAGES)
            while todo and len(seen) < LIMIT:
                u = todo.pop(0)
                key = re.sub(r"\d+", "N", u.split("?")[0]) + (
                    "?" + re.sub(r"=[^&]*", "=", u.split("?")[1]) if "?" in u else ""
                )
                if key in seen:
                    continue
                seen.add(key)
                try:
                    r = pg.goto(BASE + u, wait_until="networkidle")
                except Exception as e:
                    errors.append((label, u, f"goto {e}"))
                    continue
                if r and r.status >= 400:
                    errors.append((label, u, f"HTTP {r.status}"))
                    continue
                if label == "phone":
                    PAGES.append(u)
                    for h in pg.eval_on_selector_all("a[href^='/']", "as => as.map(a => a.getAttribute('href'))"):
                        if not re.search(r"logout|wyloguj|\.ics|/files/|/export|download|/n/done|/static/", h):
                            todo.append(h)
                for kind, detail in pg.evaluate(CHECK):
                    issues[kind][detail].add(f"{label}:{key}")
            ctx.close()
        br.close()
    for kind, d in sorted(issues.items(), key=lambda kv: -len(kv[1])):
        print(f"\n## {kind}: {len(d)} distinct")
        for detail, pages in sorted(d.items(), key=lambda kv: -len(kv[1]))[:25]:
            ps = sorted(pages)
            print(f"  [{len(ps)}] {detail}   e.g. {', '.join(ps[:3])}")
    print(f"\n## console/page errors: {len(errors)}")
    for e in errors[:30]:
        print("  ", e)
    print("\npages:", len(PAGES))


PAGES = []
main()

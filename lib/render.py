#!/usr/bin/env python3
"""Render one or more layout JSON documents as a self-contained Launchpad mock-up.

Every icon is embedded as a base64 data URI, so the resulting HTML file works
offline and can be moved anywhere. Pass --layout more than once, oldest first,
to get a history view: a timeline of snapshots, each shown with what moved,
what is new and what was uninstalled since the one before. A layout with
"draft": true is marked as not applied.
"""

import argparse
import base64
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dump  # noqa: E402  (sibling module, same directory)

LIB = os.path.dirname(os.path.abspath(__file__))
SWIFT = os.path.join(LIB, "icon-export.swift")


def title_to_bundleid(db=None) -> dict:
    """Map every Launchpad app title to its bundle identifier."""
    conn, tmp = dump.open_snapshot(db or dump.db_path())
    try:
        rows = conn.execute(
            "SELECT title, bundleid FROM apps WHERE bundleid IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    return {title: bid for title, bid in rows if title}


def fetch_icons(pairs, px: int) -> dict:
    """Ask AppKit for each app's icon; return {bundleid: data-uri}.

    `pairs` is an iterable of (bundleid, app title); the title lets the helper
    fall back to a filesystem search when LaunchServices does not know the id.
    """
    if not pairs:
        return {}
    env = dict(os.environ, ICON_PX=str(px))
    proc = subprocess.run(
        ["swift", SWIFT],
        input="\n".join(f"{bid}\t{title}" for bid, title in sorted(pairs)),
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise SystemExit(f"launchpad-map: icon export failed\n{proc.stderr}")

    icons = {}
    for line in proc.stdout.splitlines():
        if "\t" not in line:
            continue
        bid, payload = line.split("\t", 1)
        if payload != "MISS":
            icons[bid] = "data:image/png;base64," + payload
    return icons


def normalize(doc: dict, page_size: int) -> dict:
    """Expand the compact layout form into explicit apps and folder pages."""
    pages = []
    for raw_page in doc.get("pages", []):
        items = []
        for entry in raw_page:
            if isinstance(entry, str):
                items.append({"kind": "app", "title": entry})
            elif "folder" in entry:
                apps = list(entry.get("apps", []))
                chunks = [
                    apps[i : i + page_size] for i in range(0, len(apps), page_size)
                ] or [[]]
                items.append(
                    {
                        "kind": "folder",
                        "title": entry["folder"],
                        "apps": apps,
                        "pages": chunks,
                    }
                )
            elif "app" in entry:
                items.append({"kind": "app", "title": entry["app"]})
        if items:  # the database keeps empty pages around; Launchpad never shows them
            pages.append(items)
    return {
        "title": doc.get("title", "Layout"),
        "date": doc.get("date"),
        "time": doc.get("time"),
        "draft": bool(doc.get("draft")),
        "note": doc.get("note"),
        "pages": pages,
    }


def load_review(path):
    """The showcase approvals: {app title: {"show": bool, ...}}."""
    if not path or not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh).get("apps", {})


def showcase_only(doc: dict, review: dict) -> dict:
    """Keep only approved apps. An app missing from the review stays hidden."""
    shown = lambda a: review.get(a, {}).get("show") is True
    pages = []
    for page in doc.get("pages", []):
        kept = []
        for entry in page:
            if isinstance(entry, str):
                if shown(entry):
                    kept.append(entry)
            elif "folder" in entry:
                apps = [a for a in entry.get("apps", []) if shown(a)]
                if apps:
                    kept.append({"folder": entry["folder"], "apps": apps})
        if kept:
            pages.append(kept)
    return {"title": doc.get("title"), "date": doc.get("date"), "time": doc.get("time"), "pages": pages}


def collect_titles(layouts) -> set:
    titles = set()
    for layout in layouts:
        for page in layout["pages"]:
            for item in page:
                if item["kind"] == "app":
                    titles.add(item["title"])
                else:
                    titles.update(item["apps"])
    return titles


# --- HTML -------------------------------------------------------------------
#
# The page is rendered in the browser from one embedded JSON document, so each
# icon is stored once however many snapshots and folders it appears in.

CSS = """
:root {
  --ink: #eef1f7; --dim: #98a2b6; --faint: rgba(238,241,247,.42);
  --line: rgba(255,255,255,.12); --glass: rgba(255,255,255,.07);
  --moved: #f3b544; --new: #52d6b0; --gone: #f08497; --warn: #ff7a6b;
  --tile: 96px; --icon: 64px;
}
* { box-sizing: border-box; }
html { color-scheme: dark; }
body {
  margin: 0; height: 100vh; overflow: hidden; color: var(--ink);
  font: 13px/1.45 -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  background: #141b2b radial-gradient(140% 100% at 20% -10%, #2a3a57 0%, #151c2c 55%, #10141f 100%) fixed;
  -webkit-font-smoothing: antialiased;
  display: grid; grid-template-columns: minmax(0, 1fr) 248px;
}
button, input, select { font: inherit; color: inherit; }
:focus-visible { outline: 2px solid var(--ink); outline-offset: 3px; border-radius: 6px; }

/* Timeline rail: newest at the top, like Time Machine. */
.rail {
  position: sticky; top: 0; height: 100vh; overflow-y: auto;
  border-left: 1px solid var(--line); padding: 28px 18px 28px 0;
  background: rgba(10,14,24,.35);
}
.rail ol { list-style: none; margin: 0; padding: 0; position: relative; }
.rail ol::before {
  content: ""; position: absolute; left: 27px; top: 14px; bottom: 14px;
  width: 1px; background: var(--line);
}
.snap {
  appearance: none; border: 0; background: none; cursor: pointer; text-align: left;
  display: grid; grid-template-columns: 56px 1fr; width: 100%;
  padding: 10px 10px 10px 0; border-radius: 0 12px 12px 0; color: var(--dim);
}
.snap:hover { background: var(--glass); }
.snap[aria-current="true"] { background: rgba(255,255,255,.11); color: var(--ink); }
.dot {
  width: 11px; height: 11px; border-radius: 50%; margin: 5px 0 0 22px;
  background: #151c2c; border: 2px solid var(--dim); position: relative;
}
.snap[aria-current="true"] .dot { background: var(--ink); border-color: var(--ink);
  box-shadow: 0 0 0 5px rgba(238,241,247,.14); }
.snap.draft .dot { border-style: dashed; }
.snap.base .dot { border-color: var(--moved); }
.when { font-size: 15px; font-weight: 600; color: inherit; font-variant-numeric: tabular-nums;
  letter-spacing: -.01em; }
.what { display: block; font-size: 12.5px; }
.stat { display: block; font-size: 11.5px; color: var(--faint); margin-top: 2px;
  font-variant-numeric: tabular-nums; }
.stat b { font-weight: 500; }
.stat .up { color: var(--new); } .stat .down { color: var(--gone); }

/* Only the main column scrolls, with its scrollbar space always reserved, so a
   snapshot tall enough to scroll never shifts the layout sideways. */
main { padding: 28px 40px 80px; min-width: 0; height: 100vh; overflow-y: auto;
  scrollbar-gutter: stable; }
.top { display: flex; gap: 24px; align-items: flex-start; justify-content: space-between;
  flex-wrap: wrap; }
h1 { margin: 0; font-size: 28px; font-weight: 700; letter-spacing: -.02em; line-height: 1.1; }
h1 small { display: block; font-size: 13px; font-weight: 400; color: var(--dim);
  letter-spacing: 0; margin-top: 6px; }
.find { position: relative; width: min(320px, 100%); }
.find input {
  width: 100%; padding: 9px 14px; border-radius: 10px; border: 1px solid var(--line);
  background: rgba(0,0,0,.22);
}
.find input::placeholder { color: var(--faint); }
.trail {
  position: absolute; right: 0; top: calc(100% + 8px); width: 100%; z-index: 5;
  background: rgba(28,36,54,.96); border: 1px solid var(--line); border-radius: 12px;
  padding: 12px 14px; backdrop-filter: blur(20px);
}
.trail[hidden] { display: none; }
.trail h2 { margin: 0 0 8px; font-size: 13px; font-weight: 600; }
.trail ol { list-style: none; margin: 0; padding: 0; }
.trail li { display: flex; justify-content: space-between; gap: 12px; padding: 3px 0;
  font-size: 12.5px; }
.trail li span:first-child { color: var(--dim); font-variant-numeric: tabular-nums; }
.trail li.absent span:last-child { color: var(--faint); }
.trail li.here { color: var(--ink); font-weight: 600; }
.trail .more { color: var(--faint); font-size: 11.5px; margin: 8px 0 0; }

.compare { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px;
  margin: 22px 0 6px; color: var(--dim); }
.compare select {
  background: rgba(0,0,0,.22); border: 1px solid var(--line); border-radius: 8px;
  padding: 5px 8px;
}
.chip {
  appearance: none; cursor: pointer; border: 1px solid var(--line); background: none;
  border-radius: 999px; padding: 4px 12px 4px 10px; display: inline-flex; align-items: center;
  gap: 7px; font-variant-numeric: tabular-nums;
}
.chip i { width: 8px; height: 8px; border-radius: 50%; display: block; }
.chip[aria-pressed="true"] { background: rgba(255,255,255,.14); border-color: transparent; }
.chip[hidden] { display: none; }
.chip.moved i { background: var(--moved); } .chip.new i { background: var(--new); }
.chip.gone i { background: var(--gone); }
.chip.unreviewed i { background: var(--dim); }
.chip.shown i { background: var(--ink); }
.toggle { appearance: none; cursor: pointer; margin-left: auto; border: 1px solid var(--line);
  background: none; border-radius: 8px; padding: 5px 12px; }
.toggle[aria-pressed="true"] { background: var(--ink); color: #141b2b; border-color: transparent; }
.no .mark { background: var(--gone); } .no .why { color: var(--gone); }
.no .ic { filter: grayscale(1); opacity: .45; }
.unrev .mark { background: var(--dim); } .unrev .why { color: var(--dim); }
.compare[hidden] { display: none; }

/* Every page on screen at once, left to right, as Launchpad orders them. */
#pages { display: flex; gap: 20px; overflow-x: auto; margin-top: 30px; padding-bottom: 8px; }
.page { flex: 0 0 auto;
  border: 1px solid var(--line); border-radius: 18px; padding: 16px 14px 20px;
  background: rgba(255,255,255,.025); }
.page .grid { --col: 70px; --icon: 54px; gap: 12px 2px;
  /* Always the full 7x5 of a Launchpad page at fixed cell sizes, so a panel and
     every icon in it look the same whichever snapshot is on screen. */
  grid-template-columns: repeat(7, var(--col));
  grid-template-rows: repeat(5, calc(var(--icon) + 56px)); }
.page h2 { margin: 0 0 16px; font-size: 13px; font-weight: 600; }
.page h2 span { font-weight: 400; color: var(--dim); margin-left: 8px; }
.page h2 .over { color: var(--warn); }
.grid { display: grid; grid-template-columns: repeat(7, var(--tile)); gap: 22px 14px; }

.cell { text-align: center; position: relative; transition: opacity .2s; }
.cell.linkable .iw { cursor: pointer; }
.storemenu { position: fixed; transform: translateX(-50%); z-index: 50; display: flex; flex-direction: column;
  min-width: 110px; padding: 4px; border-radius: 10px; background: #2a2a2e; color: #eee; box-shadow: 0 6px 24px #0008; font-size: 12px; }
.storemenu a { color: inherit; text-decoration: none; padding: 6px 10px; border-radius: 6px; white-space: nowrap; }
.storemenu a:hover { background: #ffffff1f; }
button.cell { appearance: none; border: 0; background: none; padding: 0; cursor: pointer;
  display: flex; flex-direction: column; align-items: center; }
.cell { align-self: start; }
.iw { position: relative; width: var(--icon); height: var(--icon); margin: 0 auto 6px; }
.ic { width: 100%; height: 100%; display: block; }
.ph { border-radius: 15px; background: rgba(255,255,255,.1); display: flex;
  align-items: center; justify-content: center; font-size: 26px; font-weight: 300; color: var(--dim); }
.label { font-size: 11.5px; line-height: 1.3; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; max-width: 100%;
  text-shadow: 0 1px 3px rgba(0,0,0,.6); }
.why, .count { max-width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.why { font-size: 10.5px; margin-top: 1px; line-height: 1.3; }
.mark { position: absolute; top: -3px; right: -3px; width: 13px; height: 13px;
  border-radius: 50%; border: 2px solid #1a2233; }
.moved .mark { background: var(--moved); } .moved .why { color: var(--moved); }
.new .mark { background: var(--new); } .new .why { color: var(--new); }
.dim { opacity: .16; }
.hit .iw { outline: 2px solid var(--ink); outline-offset: 4px; border-radius: 16px; }

.folder { width: 100%; height: 100%; border-radius: 16px; background: rgba(170,176,190,.28);
  padding: 6px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 3px; align-content: start; }
.folder img { width: 100%; display: block; }
.count { display: block; font-size: 10.5px; color: var(--faint); margin-top: 1px; }
.count.over { color: var(--warn); font-weight: 600; }

.gone-row { margin-top: 44px; padding-top: 22px; border-top: 1px dashed var(--line); }
.gone-row h2 { margin: 0 0 16px; font-size: 13px; font-weight: 600; color: var(--gone); }
.gone-row .cell { opacity: .55; }
.gone-row .cell .ic { filter: grayscale(1); }

dialog { border: 0; padding: 0; background: transparent; color: var(--ink);
  max-width: min(92vw, 820px); max-height: 88vh; }
dialog::backdrop { background: rgba(8,10,16,.6); backdrop-filter: blur(14px); }
.sheet { background: rgba(110,118,136,.28); border: 1px solid var(--line); border-radius: 26px;
  padding: 26px 30px 30px; backdrop-filter: blur(26px); position: relative; }
.sheet h3 { margin: 0; font-size: 20px; font-weight: 600; text-align: center; }
.sheet .meta { text-align: center; color: var(--dim); margin: 4px 0 22px; }
.sheet .grid { justify-content: center; }
.sheet hr { border: 0; border-top: 1px dashed var(--line); margin: 22px 0; }
.out { margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--line); }
.out h4 { margin: 0 0 8px; font-size: 12.5px; font-weight: 600; color: var(--dim); }
.out ul { margin: 0; padding: 0; list-style: none; columns: 2 220px; column-gap: 24px; }
.out li { padding: 2px 0; font-size: 12.5px; break-inside: avoid; }
.out li span { color: var(--moved); }
.close { position: absolute; top: 12px; right: 16px; appearance: none; border: 0;
  background: none; color: var(--dim); font-size: 24px; line-height: 1; cursor: pointer; }

@keyframes land { from { transform: scale(.4); opacity: 0; } }

.mark { animation: land .35s cubic-bezier(.2,.9,.3,1.3) both; }
@media (prefers-reduced-motion: reduce) {   .mark { animation: none; } .cell { transition: none; } }

@media (max-width: 900px) {
  body { grid-template-columns: minmax(0, 1fr); height: auto; overflow: visible; }
  .rail { position: static; height: auto; border-left: 0; border-bottom: 1px solid var(--line);
    padding: 12px 16px; order: -1; overflow-x: auto; }
  .rail ol { display: flex; gap: 6px; }
  .rail ol::before { display: none; }
  .snap { grid-template-columns: 1fr; padding: 8px 12px; border-radius: 12px; min-width: 150px; }
  .dot { display: none; }
  main { padding: 20px 16px 60px; height: auto; overflow: visible; }
  :root { --tile: 78px; --icon: 52px; }
  .grid { grid-template-columns: repeat(auto-fill, var(--tile)); justify-content: space-between; }
}
"""

JS = r"""
const D = JSON.parse(document.getElementById('data').textContent);
const S = D.snapshots, ICON = D.icons, PAGE = D.pageSize;
// Open on the newest snapshot, compared with the one before it: the live
// layout against its proposal, when there is one.
let cur = S.length - 1, base = S.length - 2, filter = null, query = '';

const $ = s => document.querySelector(s);
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function when(s) { return s.date ? fmtDate(s.date) + (s.time ? ', ' + s.time : '') : ''; }
// Snapshots are named by when they were taken; a draft is the one proposal.
// An app the showcase approvals do not mention yet; hidden from the showcase.
function unreviewed(app) { return !!D.review && !(app in D.review); }
// Showcase view: marks each app shown, hidden (with the reason) or not reviewed,
// in place of the changes since the snapshot before.
let sc = false;
try { sc = !!D.review && localStorage.getItem('launchpad-map-showcase') === '1'; } catch (e) {}
function showcaseStatus(app) {
  const r = D.review[app];
  if (!r) return { k: 'unrev' };
  return r[0] ? null : { k: 'no', reason: r[1] };
}
function caption(st) {
  return { new: 'new', moved: 'from ' + st.from, no: st.reason || 'hidden', unrev: 'not reviewed' }[st.k];
}
function label(s) { if (D.public) return 'Launchpad'; return s.draft ? 'Proposal' : when(s) || s.title || 'Layout'; }
function fmtDate(d) {
  if (!d) return '';
  const t = new Date(d + 'T12:00:00');
  return isNaN(t) ? d : t.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function locs(s) {
  if (s._l) return s._l;
  const m = new Map();
  s.pages.forEach((p, pi) => p.forEach(it => {
    if (it.kind === 'app') m.set(it.title, { folder: null, page: pi });
    else it.apps.forEach(a => m.set(a, { folder: it.title, page: pi }));
  }));
  return (s._l = m);
}
function where(l) { return l.folder || 'Page ' + (l.page + 1); }

// A folder counts as renamed, not dissolved, when at least half of its apps
// went to one folder of another name; its apps then have not moved.
let _fm = null;
function folderMap() {
  const key = cur + ':' + base;
  if (_fm && _fm.key === key) return _fm.map;
  const b = locs(S[base]), c = locs(S[cur]), flows = {}, size = {}, map = {};
  b.forEach((l, a) => {
    if (!l.folder) return;
    size[l.folder] = (size[l.folder] || 0) + 1;
    const cl = c.get(a);
    if (!cl || !cl.folder) return;
    const f = (flows[l.folder] = flows[l.folder] || {});
    f[cl.folder] = (f[cl.folder] || 0) + 1;
  });
  for (const bf in flows) {
    const [cf, n] = Object.entries(flows[bf]).sort((x, y) => y[1] - x[1])[0];
    if (n * 2 >= size[bf]) map[bf] = cf;
  }
  _fm = { key, map };
  return map;
}
function status(app) {
  if (sc) return showcaseStatus(app);
  if (base < 0) return null;
  const b = locs(S[base]).get(app);
  if (!b) return { k: 'new' };
  const c = locs(S[cur]).get(app);
  const expected = b.folder ? (folderMap()[b.folder] || b.folder) : where(b);
  if (expected !== where(c)) return { k: 'moved', from: where(b) };
  return null;
}

// The page a folder was on in the base snapshot, under its old name if it was
// renamed; null when it is new or on the same page now.
function folderFrom(name, page) {
  if (base < 0) return null;
  const map = folderMap();
  const olds = [name, ...Object.keys(map).filter(b => map[b] === name)];
  let was = -1;
  S[base].pages.forEach((p, pi) => p.forEach(x => {
    if (x.kind === 'folder' && was < 0 && olds.includes(x.title)) was = pi;
  }));
  return was >= 0 && was !== page ? 'Page ' + (was + 1) : null;
}
function folderPage(f) { return S[cur].pages.findIndex(p => p.includes(f)); }

function folderIsNew(name) {
  if (base < 0) return false;
  const before = S[base].pages.flat().some(x => x.kind === 'folder' && x.title === name);
  return !before && !Object.values(folderMap()).includes(name);
}

// How folders changed between the base snapshot and this one.
function folderChanges() {
  const none = { renamed: [], merged: [], created: [], dissolved: [] };
  if (base < 0) return none;
  const map = folderMap();
  const folders = s => s.pages.flat().filter(x => x.kind === 'folder').map(x => x.title);
  const before = folders(S[base]), after = folders(S[cur]);
  const kept = new Set(Object.values(map));
  const into = {};
  Object.entries(map).forEach(([b, c]) => (into[c] = into[c] || []).push(b));
  return {
    renamed: Object.entries(map).filter(([b, c]) => b !== c && into[c].length === 1),
    merged: Object.entries(into).filter(([c, bs]) => bs.length > 1),
    created: after.filter(f => !kept.has(f) && !before.includes(f)),
    dissolved: before.filter(f => !(f in map) && !after.includes(f)),
  };
}
// Which folders a folder-level chip highlights, by their current name.
function folderHits(k) {
  const c = folderChanges();
  if (k === 'renamed') return c.renamed.map(([, n]) => n);
  if (k === 'merged') return c.merged.map(([n]) => n);
  if (k === 'created') return c.created;
  return [];
}
function gone() {
  if (base < 0) return [];
  const now = locs(S[cur]);
  return [...locs(S[base]).keys()].filter(a => !now.has(a));
}
function matches(app) {
  const st = status(app);
  let ok = !filter || (st && st.k === filter);
  if (filter === 'unreviewed') ok = unreviewed(app);
  if (filter === 'ok') ok = !st;
  if (filter === 'dissolved') {
    const b = locs(S[base]).get(app);
    ok = !!(b && b.folder && folderChanges().dissolved.includes(b.folder));
  }
  return ok && (!query || app.toLowerCase().includes(query));
}
const narrowing = () => !!(filter || query);

function icon(t) {
  if (ICON[t]) { const i = el('img', 'ic'); i.src = ICON[t]; i.alt = ''; return i; }
  return el('div', 'ic ph', (t[0] || '?').toUpperCase());
}
let menuEl = null;
function closeMenu() { if (menuEl) { menuEl.remove(); menuEl = null; } }
function storeMenu(anchor, name) {
  const same = menuEl && menuEl.anchor === anchor;
  closeMenu();
  if (same) return;
  const m = el('div', 'storemenu');
  const q = encodeURIComponent(name);
  [['Google Play', 'https://play.google.com/store/search?c=apps&q=' + q],
   ['F-Droid', 'https://search.f-droid.org/?lang=en&q=' + q]].forEach(([l, u]) => {
    const a = el('a', null, l); a.href = u; a.target = '_blank'; a.rel = 'noopener'; m.append(a);
  });
  m.anchor = anchor;
  const r = anchor.getBoundingClientRect();
  m.style.left = (r.left + r.width / 2) + 'px';
  m.style.top = (r.bottom + 4) + 'px';
  (document.querySelector('dialog[open]') || document.body).append(m);
  menuEl = m;
}
document.addEventListener('click', closeMenu);
document.addEventListener('scroll', closeMenu, true);
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeMenu(); });
function appCell(t, withStatus = true) {
  const st = withStatus ? status(t) : null;
  const c = el('div', 'cell' + (st ? ' ' + st.k : ''));
  const w = el('div', 'iw');
  w.append(icon(t));
  if (st) w.append(el('span', 'mark'));
  const lb = el('div', 'label', t); lb.title = t;
  c.append(w, lb);
  if (S[cur].grid) {
    c.classList.add('linkable');
    w.addEventListener('click', e => { e.stopPropagation(); storeMenu(w, plain(t)); });
  }
  if (st) { const y = el('div', 'why', caption(st)); y.title = y.textContent; c.append(y); }
  if (narrowing() && withStatus) c.classList.add(matches(t) ? 'hit' : 'dim');
  return c;
}
function folderCell(f) {
  const b = el('button', 'cell');
  b.type = 'button';
  const w = el('div', 'iw'), tile = el('div', 'folder');
  f.apps.slice(0, 9).forEach(a => { if (ICON[a]) { const i = el('img'); i.src = ICON[a]; i.alt = ''; tile.append(i); } });
  w.append(tile);
  const sts = f.apps.map(status).filter(Boolean);
  const fresh = folderIsNew(f.title);
  const pages = Math.ceil(f.apps.length / PAGE) || 1;
  const moved = sts.filter(x => x.k === 'moved').length, added = sts.length - moved;
  const from = fresh ? null : folderFrom(f.title, folderPage(f));
  const parts = [], fc = folderChanges();
  const was = fc.renamed.filter(([, n]) => n === f.title).map(([o]) => o)
    .concat(...fc.merged.filter(([n]) => n === f.title).map(([, bs]) => bs));
  if (was.length) parts.push('was ' + was.join(' and '));
  if (fresh) parts.push('new folder');
  if (from) parts.push('from ' + from);
  if (!fresh && moved) parts.push(moved + ' moved in');
  if (!fresh && added) parts.push(added + ' new');
  let why = parts.join(', '), kind = fresh || (!moved && !from) ? 'new' : 'moved';
  if (sc) {
    const no = sts.filter(x => x.k === 'no').length, un = sts.filter(x => x.k === 'unrev').length;
    why = [no && no + ' hidden', un && un + ' not reviewed'].filter(Boolean).join(', ');
    kind = no ? 'no' : 'unrev';
  }
  if (why) { b.classList.add(kind); w.append(el('span', 'mark')); }
  const lb = el('div', 'label', f.title); lb.title = f.title;
  b.append(w, lb,
    el('span', 'count' + (pages > 1 ? ' over' : ''), f.apps.length + (pages > 1 ? ' apps, ' + pages + ' pages' : ' apps')));
  if (why) { const y = el('div', 'why', why); y.title = why; b.append(y); }
  b.setAttribute('aria-label', f.title + ', folder of ' + f.apps.length + ' apps' + (why ? ', ' + why : ''));
  const self = ((filter === 'folders' && from) || folderHits(filter).includes(f.title)) &&
    (!query || f.title.toLowerCase().includes(query));
  if (narrowing()) b.classList.add(self || f.apps.some(matches) ? 'hit' : 'dim');
  b.addEventListener('click', () => openFolder(f));
  return b;
}

function openFolder(f) {
  const dlg = $('#folder'), sh = dlg.querySelector('.sheet');
  sh.replaceChildren();
  const x = el('button', 'close', '×'); x.type = 'button'; x.setAttribute('aria-label', 'Close');
  x.addEventListener('click', () => dlg.close());
  const sts = f.apps.map(status).filter(Boolean);
  const moved = sts.filter(s => s.k === 'moved').length, added = sts.length - moved;
  let meta = f.apps.length + ' apps';
  if (moved) meta += ', ' + moved + ' moved in';
  if (added) meta += ', ' + added + ' new';
  sh.append(x, el('h3', null, f.title), el('p', 'meta', meta));
  for (let i = 0; i < f.apps.length || i === 0; i += PAGE) {
    if (i) sh.append(el('hr'));
    const g = el('div', 'grid');
    f.apps.slice(i, i + PAGE).forEach(a => g.append(appCell(a)));
    sh.append(g);
  }
  if (base >= 0) {
    const now = locs(S[cur]);
    const out = [...locs(S[base])].filter(([a, l]) => l.folder === f.title && now.get(a) && now.get(a).folder !== f.title);
    const lost = [...locs(S[base])].filter(([a, l]) => l.folder === f.title && !now.get(a));
    if (out.length || lost.length) {
      const box = el('div', 'out');
      box.append(el('h4', null, 'Moved out'));
      const ul = el('ul');
      out.forEach(([a]) => { const li = el('li', null, a + ' '); li.append(el('span', null, 'now in ' + where(now.get(a)))); ul.append(li); });
      lost.forEach(([a]) => { const li = el('li', null, a + ' '); const s = el('span', null, 'uninstalled'); s.style.color = 'var(--gone)'; li.append(s); ul.append(li); });
      box.append(ul); sh.append(box);
    }
  }
  dlg.showModal();
}

function drawRail() {
  const ol = $('.rail ol');
  if (!ol) return;  // a single layout has no timeline
  ol.replaceChildren();
  for (let i = S.length - 1; i >= 0; i--) {
    const s = S[i], li = el('li'), b = el('button', 'snap' + (s.draft ? ' draft' : '') + (i === base ? ' base' : ''));
    b.type = 'button';
    b.setAttribute('aria-current', String(i === cur));
    const n = locs(s).size, folders = s.pages.flat().filter(x => x.kind === 'folder').length;
    const txt = el('span');
    txt.append(el('span', 'when', label(s)));
    const st = el('span', 'stat', n + ' apps, ' + folders + ' folders');
    if (i > 0) {
      const prev = locs(S[i - 1]), now = locs(s);
      const add = [...now.keys()].filter(a => !prev.has(a)).length;
      const del = [...prev.keys()].filter(a => !now.has(a)).length;
      if (add || del) {
        st.append(document.createElement('br'));
        if (add) st.append(el('b', 'up', '+' + add + ' installed '));
        if (del) st.append(el('b', 'down', '−' + del + ' removed'));
      }
    }
    txt.append(st);
    b.append(el('span', 'dot'), txt);
    b.addEventListener('click', () => select(i));
    li.append(b); ol.append(li);
  }
}

function drawHead() {
  const s = S[cur];
  const h = $('h1');
  h.replaceChildren(document.createTextNode(label(s)));
  const n = locs(s).size, folders = s.pages.flat().filter(x => x.kind === 'folder').length;
  const loose = s.pages.flat().filter(x => x.kind === 'app').length;
  h.append(el('small', null, n + ' apps, ' + s.pages.length + (s.pages.length === 1 ? ' page, ' : ' pages, ') +
    folders + ' folders, ' + loose + ' loose'));

  const sel = $('#base');
  sel.replaceChildren(new Option('nothing', -1));
  S.forEach((o, i) => { if (i !== cur) sel.append(new Option(label(o), i)); });
  sel.value = String(base);

  const all = [...locs(s).keys()].map(status).filter(Boolean);
  const movedFolders = s.pages.flat().filter(x => x.kind === 'folder' && !folderIsNew(x.title) && folderFrom(x.title, folderPage(x))).length;
  const fc = folderChanges();
  const counts = { moved: all.filter(x => x.k === 'moved').length, folders: movedFolders,
    renamed: fc.renamed.length, merged: fc.merged.length, created: fc.created.length,
    dissolved: fc.dissolved.length, new: all.filter(x => x.k === 'new').length, gone: gone().length,
    unreviewed: [...locs(s).keys()].filter(unreviewed).length };
  const plural = (n, one, many) => n + ' ' + (n === 1 ? one : many);
  const text = {
    moved: n => plural(n, 'app moved', 'apps moved'),
    folders: n => plural(n, 'folder moved', 'folders moved'),
    renamed: n => plural(n, 'folder renamed', 'folders renamed'),
    merged: n => plural(n, 'folder merged', 'folders merged'),
    created: n => plural(n, 'new folder', 'new folders'),
    dissolved: n => plural(n, 'folder broken up', 'folders broken up'),
    new: n => n + ' new', gone: n => n + ' uninstalled', unreviewed: n => n + ' not reviewed',
  };
  const tips = {
    renamed: fc.renamed.map(([b, c]) => b + ' to ' + c).join(', '),
    merged: fc.merged.map(([c, bs]) => bs.join(' and ') + ' into ' + c).join(', '),
    created: fc.created.join(', '),
    dissolved: fc.dissolved.join(', '),
  };
  if (sc) {
    const all = [...locs(s).keys()], st = all.map(showcaseStatus);
    Object.assign(counts, { ok: st.filter(x => !x).length, no: st.filter(x => x && x.k === 'no').length,
      unrev: st.filter(x => x && x.k === 'unrev').length });
  }
  Object.assign(text, { ok: n => n + ' shown', no: n => n + ' hidden', unrev: n => n + ' not reviewed' });
  const scKeys = ['ok', 'no', 'unrev'];
  const sw = $('#sc');
  if (sw) sw.setAttribute('aria-pressed', String(sc));
  $('label[for="base"]').hidden = sel.hidden = sc;
  document.querySelectorAll('.chip').forEach(c => {
    const k = c.dataset.k;
    if (sc !== scKeys.includes(k)) { c.hidden = true; return; }
    c.lastChild.textContent = text[k](counts[k]);
    c.title = tips[k] || '';
    c.setAttribute('aria-pressed', String(filter === k));
    c.hidden = !counts[k] || (base < 0 && k !== 'unreviewed' && !sc);
  });
}

function drawPages() {
  const m = $('#pages');
  m.replaceChildren();
  S[cur].pages.forEach((p, pi) => {
    const sec = el('section', 'page');
    const h = el('h2', null, 'Page ' + (pi + 1));
    h.append(el('span', p.length > PAGE ? 'over' : null, p.length + ' of ' + PAGE));
    const g = el('div', 'grid');
    p.forEach(it => g.append(it.kind === 'app' ? appCell(it.title) : folderCell(it)));
    sec.append(h, g); m.append(sec);
  });
  const gbox = $('#gone');
  gbox.replaceChildren();
  const g = gone();
  if (g.length) {
    const sec = el('section', 'gone-row');
    sec.append(el('h2', null, 'Uninstalled since ' + label(S[base])));
    const grid = el('div', 'grid');
    g.forEach(a => grid.append(appCell(a, false)));
    sec.append(grid); gbox.append(sec);
  }
}


function drawTrail() {
  const t = $('.trail');
  if (!query) { t.hidden = true; return; }
  const every = new Set();
  S.forEach(s => locs(s).forEach((_, a) => every.add(a)));
  const hits = [...every].filter(a => a.toLowerCase().includes(query)).sort((a, b) =>
    (a.toLowerCase().startsWith(query) ? 0 : 1) - (b.toLowerCase().startsWith(query) ? 0 : 1) || a.localeCompare(b));
  t.replaceChildren();
  if (!hits.length) { t.append(el('p', 'more', 'No matches')); t.hidden = false; return; }
  const app = hits[0];
  t.append(el('h2', null, app));
  const ol = el('ol');
  for (let i = S.length - 1; i >= 0; i--) {
    const l = locs(S[i]).get(app);
    const li = el('li', (l ? '' : 'absent') + (i === cur ? ' here' : ''));
    li.append(el('span', null, label(S[i])), el('span', null, l ? where(l) : 'not installed'));
    ol.append(li);
  }
  t.append(ol);
  if (hits.length > 1) t.append(el('p', 'more', 'Also matches: ' + hits.slice(1, 6).join(', ') + (hits.length > 6 ? '…' : '')));
  t.hidden = false;
}

function draw() { drawRail(); drawHead(); drawPages(); drawTrail(); }
// Stepping through the timeline always compares with the snapshot before;
// a pick from "Changes since" holds only until the next step.
function select(i) {
  cur = i;
  base = i - 1;
  filter = null;
  draw();
}

$('#base').addEventListener('change', e => { base = +e.target.value; filter = null; draw(); holdHead(); });
document.querySelectorAll('.chip').forEach(c => c.addEventListener('click', () => {
  if (c.dataset.k === 'gone') { $('#gone').scrollIntoView({ behavior: 'smooth' }); return; }
  filter = filter === c.dataset.k ? null : c.dataset.k;
  drawHead(); drawPages();
}));
if ($('#sc')) {
  if (!D.review) $('#sc').remove();
  else $('#sc').addEventListener('click', () => {
    sc = !sc; filter = null;
    try { localStorage.setItem('launchpad-map-showcase', sc ? '1' : '0'); } catch (e) {}
    draw();
  });
}
$('#q').addEventListener('input', e => { query = e.target.value.trim().toLowerCase(); drawPages(); drawTrail(); });
$('#q').addEventListener('keydown', e => { if (e.key === 'Escape') { e.target.value = ''; query = ''; drawPages(); drawTrail(); } });
$('#folder').addEventListener('click', e => { if (e.target.id === 'folder') e.target.close(); });
document.addEventListener('keydown', e => {
  if (e.target.closest('input, select, dialog')) return;
  if (e.key === 'ArrowUp' && cur < S.length - 1) { e.preventDefault(); select(cur + 1); }
  if (e.key === 'ArrowDown' && cur > 0) { e.preventDefault(); select(cur - 1); }
  if (e.key === '/') { e.preventDefault(); $('#q').focus(); }
});
if (S.length < 2) { document.querySelector('.rail').remove(); document.body.style.gridTemplateColumns = '1fr'; }
if (D.public || !D.review && S.length < 2) $('.compare').hidden = true;
// The header grows with the note and the folder summary. Reserve the tallest
// one any snapshot needs, so the pages stay put while stepping through them.
function holdHead() {
  const head = $('#head'), was = [cur, base];
  head.style.minHeight = '';
  let tallest = 0;
  S.forEach((_, i) => {
    cur = i; base = i - 1;
    drawHead();
    tallest = Math.max(tallest, head.offsetHeight);
  });
  [cur, base] = was;
  drawHead();
  head.style.minHeight = tallest + 'px';
}
draw();
holdHead();
let _rz;
addEventListener('resize', () => { clearTimeout(_rz); _rz = setTimeout(holdHead, 150); });
"""


def render(layouts, icons, ids, page_size, review=None, public=False) -> str:
    """Render the snapshots, oldest first, as one page with a history rail."""
    by_title = {t: icons[ids[t]] for t in collect_titles(layouts) if ids.get(t) in icons}
    data = {
        "pageSize": page_size,
        "public": public,
        # Only the approved/hidden flags reach the page; reasons stay local.
        # The public page gets no approvals at all: they name the hidden apps.
        "review": None if review is None or public
        else {a: [bool(v.get("show")), v.get("reason", "")] for a, v in review.items()},
        "icons": by_title,
        "snapshots": [
            {k: layout[k] for k in ("title", "date", "time", "draft", "note", "pages")} for layout in layouts
        ],
    }
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    heading = "Launchpad" if public else "Launchpad history" if len(layouts) > 1 else "Launchpad layout"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{heading}</title>
<style>{CSS}</style></head><body>
<main>
  <header id="head">
  <div class="top">
    <h1></h1>
    <div class="find">
      <input id="q" type="search" placeholder="Find an app" autocomplete="off"
             aria-label="Find an app and see where it has been">
      <div class="trail" hidden aria-live="polite"></div>
    </div>
  </div>
  <div class="compare">
    <label for="base">Changes since</label> <select id="base"></select>
    <button class="chip moved" type="button" data-k="moved"><i></i><span></span></button>
    <button class="chip moved" type="button" data-k="folders"><i></i><span></span></button>
    <button class="chip moved" type="button" data-k="renamed"><i></i><span></span></button>
    <button class="chip moved" type="button" data-k="merged"><i></i><span></span></button>
    <button class="chip new" type="button" data-k="created"><i></i><span></span></button>
    <button class="chip gone" type="button" data-k="dissolved"><i></i><span></span></button>
    <button class="chip new" type="button" data-k="new"><i></i><span></span></button>
    <button class="chip gone" type="button" data-k="gone"><i></i><span></span></button>
    <button class="chip shown" type="button" data-k="ok"><i></i><span></span></button>
    <button class="chip gone" type="button" data-k="no"><i></i><span></span></button>
    <button class="chip unreviewed" type="button" data-k="unrev"><i></i><span></span></button>
    <button class="toggle" type="button" id="sc" aria-pressed="false">Showcase</button>
    <button class="chip unreviewed" type="button" data-k="unreviewed" title="Not in the showcase approvals yet, so hidden from the showcase"><i></i><span></span></button>
  </div>
  </header>
  <div id="pages"></div>
  <div id="gone"></div>
</main>
<nav class="rail" aria-label="Snapshots, newest first"><ol></ol></nav>
<dialog id="folder" aria-label="Folder contents"><div class="sheet"></div></dialog>
<script type="application/json" id="data">{blob}</script>
<script>{JS}</script>
</body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="launchpad-map render",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--layout",
        "-l",
        action="append",
        default=[],
        help="layout JSON file; repeat, oldest first, for a history view "
        "(default: the live Launchpad database)",
    )
    ap.add_argument("--out", "-o", required=True, help="HTML file to write")
    ap.add_argument("--page-size", type=int, default=35, help="icons per page (default 35)")
    ap.add_argument("--icon-px", type=int, default=96, help="icon pixel size (default 96)")
    ap.add_argument("--db", help="database to read titles from (default: the live one)")
    ap.add_argument("--review", help="showcase approvals JSON; the history view "
                    "counts apps not reviewed yet")
    ap.add_argument("--showcase", help="showcase approvals JSON; render the last "
                    "layout with only the approved apps, for publishing")
    args = ap.parse_args()

    docs = []
    if args.layout:
        for path in args.layout:
            with open(path) as fh:
                docs.append(json.load(fh))
    else:
        conn, tmp = dump.open_snapshot(args.db or dump.db_path())
        try:
            docs.append(
                {"title": "Current", "pages": dump.read_layout(conn)}
            )
        finally:
            conn.close()
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    public = bool(args.showcase)
    review = load_review(args.showcase or args.review)
    if public:
        if review is None:
            raise SystemExit(f"launchpad-map: no approvals at {args.showcase}")
        docs = [showcase_only(docs[-1], review)]
    layouts = [normalize(d, args.page_size) for d in docs]
    ids = title_to_bundleid(args.db)
    titles = collect_titles(layouts)

    icons = fetch_icons({(ids[t], t) for t in titles if t in ids}, args.icon_px)
    # Either the title has no bundle id recorded, or AppKit could not find the app.
    missing = sorted(t for t in titles if ids.get(t) not in icons)

    with open(args.out, "w") as fh:
        fh.write(render(layouts, icons, ids, args.page_size, review, public))

    size_mb = os.path.getsize(args.out) / 1024 / 1024
    print(f"wrote {args.out} ({size_mb:.1f} MB, {len(icons)} icons)", file=sys.stderr)
    if missing:
        print(
            "no icon for: " + ", ".join(missing[:12])
            + (" …" if len(missing) > 12 else ""),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

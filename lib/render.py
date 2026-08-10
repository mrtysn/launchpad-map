#!/usr/bin/env python3
"""Render one or more layout JSON documents as a self-contained Launchpad mock-up.

Every icon is embedded as a base64 data URI, so the resulting HTML file works
offline and can be moved anywhere. Pass --layout more than once to get a tabbed
page for comparing layouts (current versus proposed, say).
"""

import argparse
import base64
import html
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
    return {"title": doc.get("title", "Layout"), "pages": pages}


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

CSS = """
:root {
  --tile: 104px; --gap: 26px; --radius: 22px;
  --ink: #f5f5f7; --dim: #a8a8b3; --warn: #ff6b6b;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 0 60px;
  font: 13px/1.4 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
  color: var(--ink);
  background: #101014 radial-gradient(120% 90% at 50% 0%, #23303a 0%, #0d0d11 70%) fixed;
  -webkit-font-smoothing: antialiased;
}
header { padding: 28px 32px 8px; }
h1 { margin: 0 0 4px; font-size: 19px; font-weight: 600; letter-spacing: -0.01em; }
.sub { color: var(--dim); font-size: 12.5px; }
.tabs { display: flex; gap: 8px; flex-wrap: wrap; padding: 16px 32px 0; }
.tab {
  appearance: none; border: 1px solid rgba(255,255,255,.14); cursor: pointer;
  background: rgba(255,255,255,.06); color: var(--ink);
  padding: 7px 15px; border-radius: 999px; font-size: 12.5px; font-family: inherit;
}
.tab[aria-selected="true"] { background: rgba(255,255,255,.9); color: #16161a; border-color: transparent; }
.layout[hidden] { display: none; }
.page { padding: 26px 32px 6px; }
.page > h2 {
  margin: 0 0 18px; font-size: 11px; font-weight: 600; letter-spacing: .09em;
  text-transform: uppercase; color: var(--dim);
}
.grid {
  display: grid; grid-template-columns: repeat(7, var(--tile));
  gap: var(--gap) 18px; justify-content: start;
}
.cell { text-align: center; }
.cell img, .folder-tile, .ph {
  width: 72px; height: 72px; display: block; margin: 0 auto 7px;
}
.ph {
  border-radius: 16px; background: rgba(255,255,255,.1);
  display: flex; align-items: center; justify-content: center;
  font-size: 28px; font-weight: 300; color: var(--dim);
}
.label {
  font-size: 11.5px; line-height: 1.25; color: var(--ink);
  text-shadow: 0 1px 3px rgba(0,0,0,.65);
  overflow-wrap: anywhere;
}
button.cell { appearance: none; border: 0; background: none; padding: 0; cursor: pointer;
  font-family: inherit; color: inherit; }
.folder-tile {
  border-radius: 18px; background: rgba(160,160,170,.34);
  backdrop-filter: blur(8px); padding: 7px;
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 3px;
}
.folder-tile img { width: 100%; height: auto; margin: 0; border-radius: 3px; }
.count { color: var(--dim); font-size: 10.5px; display: block; margin-top: 2px; }
.count.over { color: var(--warn); font-weight: 600; }
dialog {
  border: 0; padding: 0; background: transparent; color: var(--ink); max-width: 92vw;
}
dialog::backdrop { background: rgba(8,8,10,.62); backdrop-filter: blur(14px); }
.sheet {
  background: rgba(120,120,130,.30); border: 1px solid rgba(255,255,255,.14);
  border-radius: 26px; padding: 26px 30px 30px; backdrop-filter: blur(26px);
}
.sheet h3 { margin: 0 0 4px; font-size: 17px; font-weight: 600; text-align: center; }
.sheet .meta { text-align: center; color: var(--dim); margin: 0 0 22px; font-size: 12px; }
.sheet .grid { gap: 20px 18px; }
.sheet hr {
  border: 0; border-top: 1px dashed rgba(255,255,255,.22); margin: 22px 0 20px;
}
.pagemark { text-align: center; color: var(--warn); font-size: 11px; margin: 0 0 16px;
  letter-spacing: .04em; text-transform: uppercase; }
.close {
  position: absolute; top: 14px; right: 18px; appearance: none; border: 0;
  background: none; color: var(--dim); font-size: 22px; cursor: pointer; line-height: 1;
}
footer { color: var(--dim); padding: 30px 32px 0; font-size: 12px; }
"""

JS = """
document.querySelectorAll('.tab').forEach(function (tab) {
  tab.addEventListener('click', function () {
    var group = tab.closest('.tabs');
    group.querySelectorAll('.tab').forEach(function (t) {
      t.setAttribute('aria-selected', String(t === tab));
    });
    document.querySelectorAll('.layout').forEach(function (l) {
      l.hidden = l.dataset.layout !== tab.dataset.layout;
    });
  });
});
document.querySelectorAll('button.cell[data-folder]').forEach(function (btn) {
  btn.addEventListener('click', function () {
    document.getElementById(btn.dataset.folder).showModal();
  });
});
document.querySelectorAll('dialog').forEach(function (d) {
  d.addEventListener('click', function (e) { if (e.target === d) d.close(); });
  var x = d.querySelector('.close');
  if (x) x.addEventListener('click', function () { d.close(); });
});
"""


def icon_html(title, icons, ids, cls="") -> str:
    bid = ids.get(title)
    src = icons.get(bid) if bid else None
    if src:
        return f'<img src="{src}" alt="" loading="lazy">'
    letter = html.escape(title[:1].upper() or "?")
    return f'<div class="ph">{letter}</div>'


def app_cell(title, icons, ids) -> str:
    return (
        '<div class="cell">'
        + icon_html(title, icons, ids)
        + f'<div class="label">{html.escape(title)}</div></div>'
    )


def folder_cell(item, icons, ids, dom_id, page_size) -> str:
    minis = "".join(
        icon_html(t, icons, ids) for t in item["apps"][:9] if ids.get(t) in icons
    )
    n_pages = len(item["pages"])
    over = " over" if n_pages > 1 else ""
    note = f"{len(item['apps'])} apps"
    if n_pages > 1:
        note += f" · {n_pages} pages"
    return (
        f'<button class="cell" data-folder="{dom_id}">'
        f'<div class="folder-tile">{minis}</div>'
        f'<div class="label">{html.escape(item["title"])}</div>'
        f'<span class="count{over}">{note}</span></button>'
    )


def folder_dialog(item, icons, ids, dom_id) -> str:
    blocks = []
    for index, chunk in enumerate(item["pages"]):
        if index:
            blocks.append('<hr><p class="pagemark">page ' + str(index + 1) + "</p>")
        cells = "".join(app_cell(t, icons, ids) for t in chunk)
        blocks.append(f'<div class="grid">{cells}</div>')
    n_pages = len(item["pages"])
    meta = f"{len(item['apps'])} apps"
    if n_pages > 1:
        meta += f" — spills onto {n_pages} pages"
    return (
        f'<dialog id="{dom_id}"><div class="sheet">'
        f'<button class="close" aria-label="Close">&times;</button>'
        f'<h3>{html.escape(item["title"])}</h3>'
        f'<p class="meta">{html.escape(meta)}</p>'
        + "".join(blocks)
        + "</div></dialog>"
    )


def render(layouts, icons, ids, page_size) -> str:
    tabs, bodies, dialogs = [], [], []
    for li, layout in enumerate(layouts):
        slug = f"l{li}"
        selected = "true" if li == 0 else "false"
        tabs.append(
            f'<button class="tab" data-layout="{slug}" aria-selected="{selected}">'
            f'{html.escape(layout["title"])}</button>'
        )

        pages_html = []
        for pi, page in enumerate(layout["pages"]):
            cells = []
            for ii, item in enumerate(page):
                if item["kind"] == "app":
                    cells.append(app_cell(item["title"], icons, ids))
                else:
                    dom_id = f"{slug}-p{pi}-f{ii}"
                    cells.append(folder_cell(item, icons, ids, dom_id, page_size))
                    dialogs.append(folder_dialog(item, icons, ids, dom_id))
            over = len(page) > page_size
            warn = (
                f' <span class="count over">{len(page)} icons — over the '
                f"{page_size} that fit</span>"
                if over
                else f' <span class="count">{len(page)} icons</span>'
            )
            pages_html.append(
                f'<section class="page"><h2>Page {pi + 1}{warn}</h2>'
                f'<div class="grid">{"".join(cells)}</div></section>'
            )

        folder_count = sum(
            1 for p in layout["pages"] for i in p if i["kind"] == "folder"
        )
        spill = sum(
            1
            for p in layout["pages"]
            for i in p
            if i["kind"] == "folder" and len(i["pages"]) > 1
        )
        summary = (
            f"{len(layout['pages'])} home pages · {folder_count} folders · "
            f"{spill} folder(s) spilling onto a second page"
        )
        bodies.append(
            f'<div class="layout" data-layout="{slug}"{"" if li == 0 else " hidden"}>'
            f'<footer>{html.escape(summary)}</footer>'
            + "".join(pages_html)
            + "</div>"
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Launchpad layout</title>
<style>{CSS}</style></head><body>
<header>
  <h1>Launchpad layout</h1>
  <div class="sub">Click any folder to see what is inside it.
  A folder page holds {page_size} icons ({page_size // 5}&times;5).</div>
</header>
<div class="tabs">{"".join(tabs)}</div>
{"".join(bodies)}
{"".join(dialogs)}
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
        help="layout JSON file; repeat to compare layouts as tabs "
        "(default: the live Launchpad database)",
    )
    ap.add_argument("--out", "-o", required=True, help="HTML file to write")
    ap.add_argument("--page-size", type=int, default=35, help="icons per page (default 35)")
    ap.add_argument("--icon-px", type=int, default=96, help="icon pixel size (default 96)")
    ap.add_argument("--db", help="database to read titles from (default: the live one)")
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

    layouts = [normalize(d, args.page_size) for d in docs]
    ids = title_to_bundleid(args.db)
    titles = collect_titles(layouts)

    icons = fetch_icons({(ids[t], t) for t in titles if t in ids}, args.icon_px)
    # Either the title has no bundle id recorded, or AppKit could not find the app.
    missing = sorted(t for t in titles if ids.get(t) not in icons)

    with open(args.out, "w") as fh:
        fh.write(render(layouts, icons, ids, args.page_size))

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

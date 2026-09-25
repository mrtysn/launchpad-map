#!/usr/bin/env python3
"""Turn a folder reorganisation into a proposal layout for the phone.

A reorganisation (reorg.json) says which new folders to make and from what,
what remains in the folders it took from, and single-app moves; it names no
cells. This places it on the newest snapshot: everything not mentioned keeps
its cell, folders that lost apps keep theirs, a folder emptied out is
removed, and new folders take the free cells of their page in reading order,
spilling onto the next page when one is full. The result is a draft layout
(`"draft": true`) that `phone history` shows and `phone write` can apply.

    launchpad-map phone reorg REORG.json --title "Proposal 2" --note "..." > layouts/phone/proposal-2.json
"""

import argparse
import copy
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phone as ph  # noqa: E402


def newest_snapshot():
    names = sorted(n for n in os.listdir(ph.LAYOUTS) if re.match(r"\d{4}-\d{2}-\d{2}-\d{4}\.json$", n))
    if not names:
        raise SystemExit("launchpad-map phone reorg: no snapshot in layouts/phone/")
    return os.path.join(ph.LAYOUTS, names[-1])


def folder_entry(pages, name):
    for pg in pages:
        for it in pg:
            if it.get("folder") == name:
                return it
    return None


def free_cells(page, cols, rows):
    used = set()
    for it in page:
        c, r = it["at"]
        w, h = it.get("span", [1, 1])
        used |= {(c + i, r + j) for i in range(w) for j in range(h)}
    return [[c, r] for r in range(rows) for c in range(cols) if (c, r) not in used]


def apply(doc, reorg):
    doc = copy.deepcopy(doc)
    pages = doc["pages"]
    cols, rows = doc["grid"]["cols"], doc["grid"]["rows"]
    touched = {}
    for f in reorg.get("folders", []):
        touched.setdefault(f["from"], set()).update(f["apps"])
        for pg in pages:  # a loose app taken into a new folder leaves its page
            pg[:] = [it for it in pg if it.get("app") not in f["apps"]]
    for m in reorg.get("moves", []):
        if m.get("from"):
            touched.setdefault(m["from"], set()).add(m["app"])
        else:  # a loose app: taken off its page
            for pg in pages:
                pg[:] = [it for it in pg if it.get("app") != m["app"]]
        dest = folder_entry(pages, m["to"])
        if dest is None:
            raise SystemExit(f"launchpad-map phone reorg: move to unknown folder {m['to']!r}")
        dest["apps"].append(m["app"])
    for k in reorg.get("keep", []):
        e = folder_entry(pages, k["name"])
        if e is None:
            raise SystemExit(f"launchpad-map phone reorg: keep names unknown folder {k['name']!r}")
        e["apps"] = list(k["apps"])
    for name, gone in touched.items():
        e = folder_entry(pages, name)
        if e is None:
            raise SystemExit(f"launchpad-map phone reorg: unknown folder {name!r}")
        e["apps"] = [a for a in e["apps"] if a not in gone]
    for pg in pages:
        pg[:] = [it for it in pg if not ("folder" in it and not it["apps"])]
    for pl in reorg.get("place", []):  # an existing folder moved to another page
        e = folder_entry(pages, pl["folder"])
        if e is None:
            raise SystemExit(f"launchpad-map phone reorg: place names unknown folder {pl['folder']!r}")
        for pg in pages:
            if e in pg:
                pg.remove(e)
        while len(pages) < pl["page"]:
            pages.append([])
        p = pl["page"] - 1
        cells = free_cells(pages[p], cols, rows)
        if not cells:
            raise SystemExit(f"launchpad-map phone reorg: page {pl['page']} is full; cannot place {pl['folder']!r}")
        e["at"] = cells[0]
        pages[p].append(e)
    for f in reorg.get("folders", []):
        page = f.get("page", len(pages))
        while len(pages) < page:
            pages.append([])
        p = page - 1
        while not free_cells(pages[p], cols, rows):
            p += 1
            if p == len(pages):
                pages.append([])
        cell = free_cells(pages[p], cols, rows)[0]
        pages[p].append({"folder": f["name"], "apps": list(f["apps"]), "at": cell})
    for pg in pages:
        pg.sort(key=lambda e: (e["at"][1], e["at"][0]))
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(prog="launchpad-map phone reorg", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reorg", help="the reorganisation JSON")
    ap.add_argument("--from", dest="snapshot", help="snapshot to place it on (default: the newest)")
    ap.add_argument("--title", default="Proposal")
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    with open(args.reorg) as fh:
        reorg = json.load(fh)
    with open(args.snapshot or newest_snapshot()) as fh:
        doc = json.load(fh)
    out = apply(doc, reorg)
    out["draft"], out["title"] = True, args.title
    if args.note:
        out["note"] = args.note
    for pg in out["pages"]:
        for it in pg:
            it.pop("_icon", None)
            it.pop("_icons", None)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

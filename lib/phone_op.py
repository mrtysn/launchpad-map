#!/usr/bin/env python3
"""One gesture on the phone's home screen at a time, from the shell.

`phone write` runs a whole plan; this runs a single step of it by hand, or
just looks and navigates, with the same driver and the same checks. Every
edit updates the driver's record of the phone (layouts/phone/.write-log/
state.json), so a later `phone write` plans from what is really there, and
`phone dump --save` can replace the record with a fresh read at any time.

  peek                         page, edit mode, open folder, selection, thumbnails
  go PAGE                      turn to a home page
  home | back                  a key
  read FOLDER                  the apps in a folder, in order
  split FOLDER --to PAGE APP...   select APPs inside FOLDER, drop on PAGE's thumbnail
  group PAGE NAME APP...       Group the loose APPs on PAGE into a new folder NAME
  into FOLDER APP...           carry loose APPs (wherever the record has them) into FOLDER
  to-page PAGE APP...          carry loose APPs to PAGE
  move-folder FOLDER --to PAGE
  reorder FOLDER APP...        drag those APPs to the end of FOLDER, in that order
  lock on|off                  the layout lock, through the launcher settings
  pause | resume | stop        a running `phone write`, between its operations
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phone as ph  # noqa: E402
import phone_write as pw  # noqa: E402


def page_of(live, app):
    for pi, pg in enumerate(live["pages"]):
        if any(it.get("app") == app for it in pg):
            return pi + 1
    pw.die(f"{app!r} is not loose on any page in the driver's record; `phone dump --save` if the record is stale")


def by_page(live, apps):
    out = {}
    for a in apps:
        out.setdefault(page_of(live, a), []).append(a)
    return out


def peek(d):
    scr = d.dump()
    pg = scr.page()
    title, items = d.walk.popup(scr, set())
    checked = [n["desc"][:-len(", checked")] for n in scr.launcher() if n["desc"].endswith(", checked")]
    thumbs = [n["desc"].split("page ")[1] for n in scr.launcher() if n["desc"].startswith("Screen preview, page")]
    print(f"front: {d.p.front()}")
    print(f"page: {pg[0]} of {pg[1]}" if pg else "page: none (not on a home page)")
    print(f"edit mode: {'yes' if d.edit_mode(scr) else 'no'}")
    print(f"open folder: {title or 'none'}" + (f" ({len(items)} items on screen)" if title else ""))
    print(f"selected: {', '.join(checked) or 'nothing'}")
    print(f"thumbnails: {', '.join(thumbs) or 'none'}")


def read_folder(d, name):
    fpage, fat = d.folder_at.get(name, (None, None))
    if fpage is None:
        pw.die(f"folder {name!r} is not in the record")
    d.enter_edit(fpage)
    d.open_folder(*pw.centre(pw.EDIT, *fat), f"folder {name!r}")
    entry = d.folder_entry(name)
    ok, seen = d.folder_contains(entry["apps"], tries=24)
    d.p.key("KEYCODE_BACK", 0.5)
    d.leave_edit()
    for a in seen:
        print(a)
    extra, missing = [a for a in seen if a not in entry["apps"]], [a for a in entry["apps"] if a not in seen]
    if extra or missing:
        print(f"record differs: not in record {extra}; not seen {missing}", file=sys.stderr)
    entry["apps"] = seen
    return True  # the record changed


def main():
    ap = argparse.ArgumentParser(prog="launchpad-map phone op", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["peek", "go", "home", "back", "read", "split", "group", "into", "to-page",
                                     "move-folder", "reorder", "lock", "pause", "resume", "stop"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--to", type=int, help="target page for split / move-folder")
    ap.add_argument("--from", dest="snapshot", help="layout to take as the record (default: the driver's state, else the newest snapshot)")
    ap.add_argument("--serial", "-s", default=os.environ.get("ANDROID_SERIAL"))
    args = ap.parse_intermixed_args()  # `split 🎮 --to 4 APP...`: options between positionals

    if args.what in ("pause", "resume", "stop"):  # no phone needed: a note for the running writer
        os.makedirs(pw.LOG, exist_ok=True)
        if args.what == "resume":
            if os.path.exists(pw.CONTROL):
                os.remove(pw.CONTROL)
        else:
            with open(pw.CONTROL, "w") as fh:
                fh.write(args.what)
        return 0

    with open(args.snapshot or pw.starting_point()) as fh:
        live = json.load(fh)
    work = os.path.join(ph.LAYOUTS, ".write")
    os.makedirs(work, exist_ok=True)
    os.makedirs(pw.LOG, exist_ok=True)
    phone = ph.Phone(ph.pick_serial(args.serial), work)
    phone.want_icons = False
    d = pw.Driver(phone, live)
    a, what = args.args, args.what
    changed = False
    try:
        if what == "peek":
            peek(d)
        elif what == "go":
            d.leave_edit()
            d.go(int(a[0]))
        elif what in ("home", "back"):
            phone.key(f"KEYCODE_{what.upper()}", 0.5)
        elif what == "read":
            changed = read_folder(d, a[0])
        elif what == "lock":
            d.set_lock(a[0] == "on")
        else:
            if what == "split":
                op = {"op": "split-folder", "folder": a[0], "page": d.folder_at[a[0]][0], "apps": a[1:], "target": args.to}
            elif what == "group":
                op = {"op": "group", "page": int(a[0]), "name": a[1], "apps": a[2:]}
            elif what == "into":
                pages = by_page(live, a[1:])
                if len(pages) != 1:
                    pw.die(f"the apps sit on pages {sorted(pages)}; one page per call")
                (page, apps), = pages.items()
                op = {"op": "into-folder", "page": page, "apps": apps, "folder": a[0]}
            elif what == "to-page":
                pages = by_page(live, a[1:])
                if len(pages) != 1:
                    pw.die(f"the apps sit on pages {sorted(pages)}; one page per call")
                (page, apps), = pages.items()
                op = {"op": "to-page", "page": page, "apps": apps, "target": int(a[0])}
            elif what == "move-folder":
                op = {"op": "move-folder", "folder": a[0], "page": d.folder_at[a[0]][0], "target": args.to}
            elif what == "reorder":
                entry = d.folder_entry(a[0])
                order = [x for x in entry["apps"] if x not in a[1:]] + a[1:]
                op = {"op": "reorder", "folder": a[0], "apps": a[1:], "order": order}
            if "apps" in op and not op["apps"] and what != "reorder":
                pw.die("no apps named")
            print(f"-- {pw.describe(op)}", file=sys.stderr)
            pw.attempt(d, 1, op)
            changed = True
    finally:
        if what not in ("peek", "home", "back", "go", "lock"):
            d.leave_edit()
        phone.finish()
        if changed:
            pw.record(d.live, {"note": "by hand"}, finished=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

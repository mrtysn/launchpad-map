#!/usr/bin/env python3
"""Keep and show the history of Launchpad layouts.

Snapshots live in the repo's layouts/ directory as
`YYYY-MM-DD-HHMM.json`. `dump --save` adds one by hand. `write` adds
one after every change, carrying the proposal's note, and first one of the
grid as it was if it drifted since the last snapshot (an update can pull an
app out of its folder). A layout with "draft": true is
a proposal; it is shown after the snapshot it was drawn from, until `write`
applies it, which removes the draft, since the new snapshot now holds it.
The phone's `write` keeps the proposal and adds one snapshot only once the
whole of it is applied, so the page reads before, proposal, after.

`history` renders every snapshot and draft in time order to one page.
`showcase` renders the newest snapshot with only the apps showcase.json
approves; anything it does not list stays hidden until reviewed.
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dump  # noqa: E402
import render  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAYOUTS = os.path.join(REPO, "layouts")
PAGE = os.path.join(REPO, "history.html")
REVIEW = os.path.join(REPO, "showcase.json")
SHOWCASE = os.path.join(REPO, "showcase.html")
# Every device on the one history page, in switcher order: (id, label, snapshot
# directory). The Android devices keep their snapshots in layouts/<id>/.
DEVICES = [("mac", "Mac", LAYOUTS), ("phone", "Phone", os.path.join(LAYOUTS, "phone")),
           ("tablet", "Tablet", os.path.join(LAYOUTS, "tablet"))]


def live_pages(db=None) -> list:
    conn, tmp = dump.open_snapshot(db or dump.db_path())
    try:
        return dump.read_layout(conn)
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def save_snapshot(db=None, directory=LAYOUTS, note=None) -> str:
    """Dump the live layout into the history and return the file's path."""
    now = datetime.datetime.now()
    doc = {"date": now.date().isoformat(), "time": now.strftime("%H:%M")}
    if note:
        doc["note"] = note
    doc["pages"] = live_pages(db)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{now:%Y-%m-%d-%H%M}.json")
    with open(path, "w") as fh:
        fh.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return path


def drifted(db=None, directory=LAYOUTS) -> bool:
    """Whether the live grid differs from the newest snapshot."""
    snaps = [p for p in collect(directory) if not json.load(open(p)).get("draft")]
    if not snaps:
        return True
    shown = lambda pages: [p for p in pages if p]  # Launchpad never shows empty pages
    with open(snaps[-1]) as fh:
        return shown(json.load(fh)["pages"]) != shown(live_pages(db))


def retire_draft(layout_path: str) -> None:
    """Remove a draft once `write` has applied it; the new snapshot holds it."""
    with open(layout_path) as fh:
        if json.load(fh).get("draft") and os.path.dirname(os.path.abspath(layout_path)) == LAYOUTS:
            os.remove(layout_path)


def collect(directory=LAYOUTS) -> list:
    """Every snapshot and draft in time order; a draft follows the snapshot it was drawn from."""
    layouts = []
    for path in glob.glob(os.path.join(directory, "*.json")):
        if os.path.basename(path) in ("example.json", "icons.json"):
            continue
        with open(path) as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict) or not isinstance(doc.get("pages"), list):
            continue  # an Android device's icons, usage, categories or reorg plan
        key = (doc.get("date") or "", doc.get("time") or "", bool(doc.get("draft")), os.path.basename(path))
        layouts.append((key, path))
    return [p for _, p in sorted(layouts)]


def device_part(dev, label, directory):
    """One device's data for the page; None when it has no snapshots."""
    layouts = collect(directory)
    docs = [json.load(open(p)) for p in layouts]
    if not any(not d.get("draft") for d in docs):
        return None
    if dev == "mac":
        part = render.device_data(docs, review_path=REVIEW if os.path.exists(REVIEW) else None)
    else:
        usage = os.path.join(directory, "usage.json")  # from `phone usage --save`
        part = render.device_data(docs, icons_path=os.path.join(directory, "icons.json"), name="Home screen",
                                  usage_path=usage if os.path.exists(usage) else None)
    return {"id": dev, "label": label, **part}


def main() -> int:
    showcase = len(sys.argv) > 1 and sys.argv[1] == "showcase"
    ap = argparse.ArgumentParser(
        prog="launchpad-map " + ("showcase" if showcase else "history"),
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--out", "-o", default=SHOWCASE if showcase else PAGE,
                    help="HTML file to write (default: in the repo)")
    ap.add_argument("--open", action="store_true", help="open the page when done")
    ap.add_argument("--device", choices=[d for d, _, _ in DEVICES],
                    help="the device --open shows first (default: the one last viewed)")
    args = ap.parse_args(sys.argv[2:] if showcase else sys.argv[1:])

    if showcase:
        snaps = [p for p in collect(LAYOUTS) if not json.load(open(p)).get("draft")]
        if not snaps:
            raise SystemExit("launchpad-map: no snapshots yet; run `launchpad-map dump --save`")
        if not os.path.exists(REVIEW):
            raise SystemExit(f"launchpad-map: no {REVIEW}; copy showcase.example.json")
        parts = [{"id": "mac", "label": "Mac",
                  **render.device_data([json.load(open(snaps[-1]))], showcase_path=REVIEW)}]
    else:
        parts = [p for p in (device_part(*d) for d in DEVICES) if p]
        if not parts:
            raise SystemExit("launchpad-map: no snapshots yet; run `launchpad-map dump --save`, "
                             "`launchpad-map phone dump --save` or `launchpad-map tablet dump --save`")
    with open(args.out, "w") as fh:
        fh.write(render.page(parts))
    render.report(args.out, parts)
    print(args.out)
    if args.open:
        url = "file://" + os.path.abspath(args.out) + (f"#{args.device}" if args.device else "")
        # `open` drops a file URL's fragment; hand the URL to the default browser instead.
        subprocess.run(["osascript", "-e", f'open location "{url}"'] if args.device else ["open", args.out],
                       check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

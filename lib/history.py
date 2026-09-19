#!/usr/bin/env python3
"""Keep and show the history of Launchpad layouts.

Snapshots live in the repo's layouts/ directory as
`YYYY-MM-DD-HHMM-<label>.json`. `dump --save` adds one by hand. `write` adds
an "Applied" snapshot after every change, carrying the proposal's note, and
first a "Live layout" one if the grid has drifted since the last snapshot
(an update can pull an app out of its folder). A layout with "draft": true is
a proposal; it is shown after the snapshots until `write` applies it, which
removes the draft, since the Applied snapshot now holds it.

`history` renders every snapshot in time order, drafts last, to one page.
"""

import argparse
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dump  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAYOUTS = os.path.join(REPO, "layouts")
PAGE = os.path.join(REPO, "launchpad-history.html")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "snapshot"


def live_pages(db=None) -> list:
    conn, tmp = dump.open_snapshot(db or dump.db_path())
    try:
        return dump.read_layout(conn)
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def save_snapshot(title: str, db=None, directory=LAYOUTS, note=None) -> str:
    """Dump the live layout into the history and return the file's path."""
    now = datetime.datetime.now()
    doc = {"title": title, "date": now.date().isoformat(), "time": now.strftime("%H:%M")}
    if note:
        doc["note"] = note
    doc["pages"] = live_pages(db)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{now:%Y-%m-%d-%H%M}-{slug(title)}.json")
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
    """Remove a draft once `write` has applied it; the Applied snapshot holds it."""
    with open(layout_path) as fh:
        if json.load(fh).get("draft") and os.path.dirname(os.path.abspath(layout_path)) == LAYOUTS:
            os.remove(layout_path)


def collect(directory=LAYOUTS) -> list:
    """Every snapshot in time order, then every open draft."""
    snaps, drafts = [], []
    for path in glob.glob(os.path.join(directory, "*.json")):
        if os.path.basename(path) == "example.json":
            continue
        with open(path) as fh:
            doc = json.load(fh)
        key = (doc.get("date") or "", doc.get("time") or "", os.path.basename(path))
        (drafts if doc.get("draft") else snaps).append((key, path))
    return [p for _, p in sorted(snaps)] + [p for _, p in sorted(drafts)]


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="launchpad-map history",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--out", "-o", default=PAGE, help="HTML file to write "
                    "(default: launchpad-history.html in the repo)")
    ap.add_argument("--open", action="store_true", help="open the page when done")
    args = ap.parse_args()

    layouts = collect()
    if not layouts:
        raise SystemExit("launchpad-map: no snapshots yet; run `launchpad-map dump --save`")
    cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "render.py"),
           "--out", args.out]
    for path in layouts:
        cmd += ["--layout", path]
    rc = subprocess.run(cmd).returncode
    if rc == 0:
        print(args.out)
        if args.open:
            subprocess.run(["open", args.out], check=False)
    return rc


if __name__ == "__main__":
    sys.exit(main())

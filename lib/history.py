#!/usr/bin/env python3
"""Keep and show the history of Launchpad layouts.

Snapshots live in the repo's layouts/ directory as
`YYYY-MM-DD-HHMM-<label>.json`. `dump --save` adds one by hand, and `write`
adds one before and after every change. A layout with "draft": true is a
proposal; it is shown after the snapshots until `write` applies it, which
marks it "applied" and drops it from the history.

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


def save_snapshot(title: str, db=None, directory=LAYOUTS) -> str:
    """Dump the live layout into the history and return the file's path."""
    now = datetime.datetime.now()
    conn, tmp = dump.open_snapshot(db or dump.db_path())
    try:
        doc = {
            "title": title,
            "date": now.date().isoformat(),
            "time": now.strftime("%H:%M"),
            "pages": dump.read_layout(conn),
        }
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{now:%Y-%m-%d-%H%M}-{slug(title)}.json")
    with open(path, "w") as fh:
        fh.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return path


def mark_applied(layout_path: str) -> None:
    """Retire a draft once `write` has put it on the grid."""
    with open(layout_path) as fh:
        doc = json.load(fh)
    if not doc.get("draft"):
        return
    doc["applied"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(layout_path, "w") as fh:
        fh.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def collect(directory=LAYOUTS) -> list:
    """Every snapshot in time order, then every open draft."""
    snaps, drafts = [], []
    for path in glob.glob(os.path.join(directory, "*.json")):
        if os.path.basename(path) == "example.json":
            continue
        with open(path) as fh:
            doc = json.load(fh)
        if doc.get("applied"):
            continue
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

#!/usr/bin/env python3
"""Read the live Launchpad database and emit a layout JSON document.

The emitted shape is the same one `render.py` consumes, and is meant to be
hand-editable:

    {
      "title": "...",
      "date": "2026-08-10",
      "pages": [
        ["Google Chrome", {"folder": "Media", "apps": ["VLC", "GIMP"]}]
      ]
    }

Apps are named by their Launchpad title. `render.py` resolves titles back to
bundle identifiers against the live database, so a hand-written layout never
has to carry bundle ids.
"""

import argparse
import datetime
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT_UUID = "ROOTPAGE"
TYPE_ROOT, TYPE_FOLDER, TYPE_PAGE, TYPE_APP = 1, 2, 3, 4


def db_path() -> str:
    """Locate the per-user Launchpad database inside the darwin user dir."""
    darwin_user_dir = subprocess.run(
        ["getconf", "DARWIN_USER_DIR"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return os.path.normpath(
        os.path.join(darwin_user_dir, "..", "0", "com.apple.dock.launchpad", "db", "db")
    )


def open_snapshot(path: str):
    """Copy the db (plus WAL) to a temp dir and open it read-only.

    The Dock holds the real database open; copying first keeps us from taking
    a lock on it or seeing a torn read mid-write.
    """
    tmp = tempfile.mkdtemp(prefix="launchpad-map.")
    snap = os.path.join(tmp, "db")
    shutil.copy2(path, snap)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            shutil.copy2(path + suffix, snap + suffix)
    return sqlite3.connect(snap), tmp


def read_layout(conn) -> list:
    """Return the root pages as a list of lists of items."""
    cur = conn.cursor()

    root = cur.execute(
        "SELECT rowid FROM items WHERE uuid = ? AND type = ?", (ROOT_UUID, TYPE_ROOT)
    ).fetchone()
    if root is None:
        raise SystemExit("launchpad-map: no ROOTPAGE in database")

    def children(parent_id):
        return cur.execute(
            """SELECT i.rowid, i.type, a.title, g.title
                 FROM items i
                 LEFT JOIN apps a   ON a.item_id = i.rowid
                 LEFT JOIN groups g ON g.item_id = i.rowid
                WHERE i.parent_id = ?
                ORDER BY i.ordering""",
            (parent_id,),
        ).fetchall()

    pages = []
    for page_id, _type, _app, _group in children(root[0]):
        items = []
        for item_id, item_type, app_title, group_title in children(page_id):
            if item_type == TYPE_APP and app_title:
                items.append(app_title)
            elif item_type == TYPE_FOLDER:
                apps = []
                for sub_page_id, *_ in children(item_id):
                    for _, sub_type, sub_app, _ in children(sub_page_id):
                        if sub_type == TYPE_APP and sub_app:
                            apps.append(sub_app)
                items.append({"folder": group_title or "Untitled", "apps": apps})
        pages.append(items)
    return pages


def snapshot_date(args) -> str:
    if args.date:
        return args.date
    if args.db:
        return datetime.date.fromtimestamp(os.path.getmtime(args.db)).isoformat()
    return datetime.date.today().isoformat()


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="launchpad-map dump", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", "-o", help="write here instead of stdout")
    ap.add_argument("--title", default="Current Launchpad layout")
    ap.add_argument("--db", help="database to read (default: the live one)")
    ap.add_argument("--date", help="snapshot date for the history view (default: "
                    "today, or the file date of --db)")
    ap.add_argument("--save", action="store_true",
                    help="add the live layout to the history in layouts/ "
                    "(titled by --title)")
    args = ap.parse_args()

    if args.save:
        import history
        title = args.title if args.title != "Current Launchpad layout" else "Live layout"
        print(history.save_snapshot(title, args.db))
        return 0

    path = args.db or db_path()
    if not os.path.exists(path):
        raise SystemExit(f"launchpad-map: no Launchpad database at {path}")

    conn, tmp = open_snapshot(path)
    try:
        doc = {"title": args.title, "date": snapshot_date(args),
               "pages": read_layout(conn)}
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)

    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

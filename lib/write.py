#!/usr/bin/env python3
"""Write a layout JSON document into the live Launchpad database.

This is the only command in the tool that modifies anything. It backs the
database up first, applies the layout inside a single transaction, and then
restarts the Dock so the change is picked up.

How the database represents a layout, since the writing depends on it:

    items(rowid, uuid, flags, type, parent_id, ordering)
      type 1  root          (uuid 'ROOTPAGE')
      type 2  folder        + a groups row carrying the title
      type 3  page          flags 2, parent is the root or a folder
      type 4  app           + an apps row carrying title and bundle id

Every ordering-related trigger on `items` is guarded by
`dbinfo.ignore_items_update_triggers`; it is set for the duration of the write
so the triggers do not renumber rows underneath us.
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid as uuidlib
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dump  # noqa: E402
import history  # noqa: E402
import render  # noqa: E402

TYPE_ROOT, TYPE_FOLDER, TYPE_PAGE, TYPE_APP = 1, 2, 3, 4
PAGE_FLAGS = 2


def restore(backup_path: str, live_path: str) -> None:
    """Put a backup back, WAL and all.

    Copying only the main database file is not a restore: the Dock keeps
    recent changes in the write-ahead log, so the three files have to travel
    together or the layout silently reverts to whenever the db was last
    checkpointed.
    """
    for suffix in ("", "-wal", "-shm"):
        target = live_path + suffix
        if os.path.exists(target):
            os.remove(target)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(backup_path + suffix):
            shutil.copy2(backup_path + suffix, live_path + suffix)


def backup(path: str, directory: str) -> str:
    os.makedirs(directory, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = os.path.join(directory, f"launchpad-{stamp}.db")
    shutil.copy2(path, target)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            shutil.copy2(path + suffix, target + suffix)
    return target


class Writer:
    def __init__(self, conn, page_size: int):
        self.conn = conn
        self.cur = conn.cursor()
        self.page_size = page_size
        self.created = []

    # -- helpers ----------------------------------------------------------
    def new_item(self, item_type: int, parent_id: int, ordering: int, flags: int) -> int:
        self.cur.execute(
            "INSERT INTO items (uuid, flags, type, parent_id, ordering) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(uuidlib.uuid4()).upper(), flags, item_type, parent_id, ordering),
        )
        return self.cur.lastrowid

    def new_page(self, parent_id: int, ordering: int) -> int:
        item_id = self.new_item(TYPE_PAGE, parent_id, ordering, PAGE_FLAGS)
        self.cur.execute(
            "INSERT INTO groups (item_id, category_id, title) VALUES (?, NULL, NULL)",
            (item_id,),
        )
        return item_id

    def new_folder(self, title: str, parent_id: int, ordering: int) -> int:
        item_id = self.new_item(TYPE_FOLDER, parent_id, ordering, 0)
        self.cur.execute(
            "INSERT INTO groups (item_id, category_id, title) VALUES (?, NULL, ?)",
            (item_id, title),
        )
        self.created.append(title)
        return item_id

    def place(self, item_id: int, parent_id: int, ordering: int) -> None:
        self.cur.execute(
            "UPDATE items SET parent_id = ?, ordering = ? WHERE rowid = ?",
            (parent_id, ordering, item_id),
        )

    # -- the write --------------------------------------------------------
    def apply(self, layout: dict, app_ids: dict, candidates: dict) -> dict:
        root = self.cur.execute(
            "SELECT rowid FROM items WHERE uuid = 'ROOTPAGE' AND type = ?", (TYPE_ROOT,)
        ).fetchone()[0]

        existing_folders = {
            title: item_id
            for item_id, title in self.cur.execute(
                "SELECT g.item_id, g.title FROM groups g JOIN items i "
                "ON i.rowid = g.item_id WHERE i.type = ? AND g.title IS NOT NULL "
                "AND g.title != ''",
                (TYPE_FOLDER,),
            )
        }
        # The first root page is HOLDINGPAGE: Launchpad's inbox for newly
        # installed apps. It sits at ordering 0, is never displayed, and is
        # emptied onto the real pages at startup — so a layout written into it
        # is torn apart the moment the Dock restarts. Leave it alone, and start
        # the real pages at ordering 1.
        root_pages, holding = [], []
        for rowid, page_uuid in self.cur.execute(
            "SELECT rowid, uuid FROM items WHERE parent_id = ? AND type = ? "
            "ORDER BY ordering",
            (root, TYPE_PAGE),
        ):
            (holding if page_uuid == "HOLDINGPAGE" else root_pages).append(rowid)

        placed, missing, keep_folders, keep_pages = set(), [], set(), set()
        keep_pages.update(holding)

        offset = len(holding)  # real pages sit after the holding page
        for page_index, page in enumerate(layout["pages"]):
            if page_index < len(root_pages):
                page_id = root_pages[page_index]
                self.place(page_id, root, page_index + offset)
            else:
                page_id = self.new_page(root, page_index + offset)
            keep_pages.add(page_id)

            for slot, item in enumerate(page):
                if item["kind"] == "app":
                    app_id = app_ids.get(item["title"])
                    if app_id is None:
                        missing.append(item["title"])
                        continue
                    self.place(app_id, page_id, slot)
                    placed.add(app_id)
                    continue

                title = item["title"]
                folder_id = existing_folders.get(title)
                if folder_id is None:
                    folder_id = self.new_folder(title, page_id, slot)
                else:
                    self.place(folder_id, page_id, slot)
                keep_folders.add(folder_id)

                folder_pages = [
                    row[0]
                    for row in self.cur.execute(
                        "SELECT rowid FROM items WHERE parent_id = ? AND type = ? "
                        "ORDER BY ordering",
                        (folder_id, TYPE_PAGE),
                    )
                ]
                for chunk_index, chunk in enumerate(item["pages"]):
                    if chunk_index < len(folder_pages):
                        chunk_id = folder_pages[chunk_index]
                        self.place(chunk_id, folder_id, chunk_index)
                    else:
                        chunk_id = self.new_page(folder_id, chunk_index)
                    keep_pages.add(chunk_id)
                    for slot_index, app_title in enumerate(chunk):
                        app_id = app_ids.get(app_title)
                        if app_id is None:
                            missing.append(app_title)
                            continue
                        self.place(app_id, chunk_id, slot_index)
                        placed.add(app_id)

        # Any app the layout did not mention (installed after the dump, say)
        # goes on a page of its own rather than being orphaned.
        orphans = [
            (item_id, title)
            for item_id, title in sorted(candidates.items(), key=lambda kv: kv[1])
            if item_id not in placed
        ]
        if orphans:
            spare = self.new_page(root, len(layout["pages"]) + offset)
            keep_pages.add(spare)
            for slot, (item_id, _title) in enumerate(orphans):
                self.place(item_id, spare, slot)

        # Drop folders and pages the new layout no longer uses. Deleting a
        # folder or page item never touches an apps row, and every app has
        # already been re-parented above.
        stale_folders = [i for i in existing_folders.values() if i not in keep_folders]
        stale_pages = [
            row[0]
            for row in self.cur.execute(
                "SELECT rowid FROM items WHERE type = ?", (TYPE_PAGE,)
            )
            if row[0] not in keep_pages
        ]
        for item_id in stale_pages + stale_folders:
            self.cur.execute("DELETE FROM items WHERE rowid = ?", (item_id,))

        return {
            "placed": len(placed),
            "missing": missing,
            "orphans": [t for _, t in orphans],
            "created_folders": self.created,
            "removed_folders": len(stale_folders),
            "removed_pages": len(stale_pages),
        }


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="launchpad-map write",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("layout", help="layout JSON file to apply")
    ap.add_argument("--page-size", type=int, default=35, help="icons per page (default 35)")
    ap.add_argument(
        "--backup-dir",
        default=os.path.expanduser("~/Library/Application Support/launchpad-backups"),
        help="where to put the pre-write backup",
    )
    ap.add_argument("--db", help="database to write (default: the live one)")
    ap.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    ap.add_argument(
        "--no-restart", action="store_true", help="do not restart the Dock afterwards"
    )
    args = ap.parse_args()

    path = args.db or dump.db_path()
    if not os.path.exists(path):
        raise SystemExit(f"launchpad-map: no Launchpad database at {path}")

    with open(args.layout) as fh:
        layout = render.normalize(json.load(fh), args.page_size)
    with open(args.layout) as fh:
        note = json.load(fh).get("note")

    if args.dry_run:
        conn, tmp = dump.open_snapshot(path)
        target = None
    else:
        target = backup(path, args.backup_dir)
        print(f"backed up to {target}", file=sys.stderr)
        if not args.db and history.drifted():
            print(f"history: {history.save_snapshot('Live layout')}", file=sys.stderr)
        conn, tmp = sqlite3.connect(path), None

    try:
        # Only app items hanging off ROOTPAGE are on the grid the user sees.
        # Launchpad keeps a parallel ROOTPAGE_VERS tree with duplicate rows for
        # some system apps; touching those would put phantom icons on a page.
        candidates = {
            item_id: title
            for item_id, title in conn.execute(
                """WITH RECURSIVE tree(id) AS (
                       SELECT rowid FROM items WHERE uuid = 'ROOTPAGE'
                       UNION ALL
                       SELECT i.rowid FROM items i JOIN tree ON i.parent_id = tree.id)
                   SELECT a.item_id, a.title FROM apps a
                     JOIN tree ON tree.id = a.item_id
                    WHERE a.title IS NOT NULL"""
            )
        }
        app_ids = {title: item_id for item_id, title in candidates.items()}
        conn.execute(
            "UPDATE dbinfo SET value = 1 WHERE key = 'ignore_items_update_triggers'"
        )
        result = Writer(conn, args.page_size).apply(layout, app_ids, candidates)
        conn.execute(
            "UPDATE dbinfo SET value = 0 WHERE key = 'ignore_items_update_triggers'"
        )
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
            # Fold the WAL into the main database file. Left in the WAL, the
            # changes are invisible to a Dock that still holds a stale shared
            # memory index; it falls back to the old contents and then "repairs"
            # the layout from App Store categories, which is not a repair.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    verb = "would place" if args.dry_run else "placed"
    print(f"{verb} {result['placed']} apps")
    if result["created_folders"]:
        print("new folders: " + ", ".join(result["created_folders"]))
    print(
        f"removed {result['removed_folders']} empty folder(s), "
        f"{result['removed_pages']} unused page(s)"
    )
    if result["orphans"]:
        print(
            f"not in the layout, parked on a spare page: "
            + ", ".join(result["orphans"])
        )
    if result["missing"]:
        print("named in the layout but not installed: " + ", ".join(result["missing"]))

    if args.dry_run:
        return 0

    if args.no_restart:
        print(f"restore with:  cp '{target}' '{path}' && killall Dock")
        return 0

    # SIGKILL, deliberately. The Dock keeps the layout in memory and writes it
    # back when it is asked to quit, so a polite `killall Dock` flushes the
    # pre-write layout straight over everything above. Killed outright it saves
    # nothing, and the replacement Dock that launchd starts reads what we wrote.
    subprocess.run(["killall", "-KILL", "Dock"], check=False)
    time.sleep(5)  # let the Dock come back and read the database

    wanted = {
        item["title"]: len(item["apps"])
        for page in layout["pages"]
        for item in page
        if item["kind"] == "folder"
    }
    conn, tmp = dump.open_snapshot(path)
    try:
        actual = {}
        for page in dump.read_layout(conn):
            for entry in page:
                if isinstance(entry, dict):
                    actual[entry["folder"]] = len(entry["apps"])
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)

    wrong = {k: (v, actual.get(k)) for k, v in wanted.items() if actual.get(k) != v}
    if wrong:
        print("\nthe Dock did not accept the layout:", file=sys.stderr)
        for name, (want, got) in sorted(wrong.items()):
            print(f"  {name}: expected {want} apps, found {got}", file=sys.stderr)
        restore(target, path)
        # SIGKILL here too: a Dock asked politely to quit writes its in-memory
        # copy of the bad layout straight back over the file just restored.
        subprocess.run(["killall", "-KILL", "Dock"], check=False)
        print("rolled back to the backup; nothing was left half-applied", file=sys.stderr)
        return 1

    print(f"Dock restarted; verified {len(wanted)} folders")
    if not args.db:
        print(f"history: {history.save_snapshot('Applied', note=note)}")
        history.retire_draft(args.layout)
    print(f"backup kept at {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

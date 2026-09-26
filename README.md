# launchpad-map

Read the macOS Launchpad layout out of its SQLite database and render it as a
self-contained HTML mock-up — real icons, real folders, real page breaks — so you
can see what a reorganisation would look like before dragging 200 icons around.

It can also apply a layout, which is the part worth being careful about — see
[Writing a layout](#writing-a-layout).

```
launchpad-map open                          # render the live layout and open it
launchpad-map dump --out layouts/current.json
launchpad-map render --layout layouts/current.json \
                     --layout layouts/proposed.json \
                     --out ~/Desktop/launchpad.html
launchpad-map write layouts/proposed.json --dry-run
```

```
launchpad-map dump --save                   # add the live layout to the history
launchpad-map history                       # every device's snapshots on one page, history.html
```

The history is the dated files in `layouts/` (`YYYY-MM-DD-HHMM.json`), shown by date and time.
`write` adds one for every layout it applies, carrying the proposal's note, and
first one of the grid as it was if it drifted since the last snapshot. `dump --save` adds one by hand. The page shows a timeline of snapshots; for the selected one it marks
every app that moved (and from where), every new install, and everything
uninstalled since the snapshot before. Search for an app to see where it lived
in each snapshot.

A proposal is a layout with `"draft": true`, plus an optional `"note"`. It is
shown after the snapshots until `write` applies it, which removes the draft. `layouts/*.json` is gitignored: a snapshot is an
inventory of one machine's apps.

## Home Screens app

`app/bundle.sh` builds **Home Screens** into `~/Applications`: the history page
in a window, with the device picked in the toolbar (Mac, Phone, Tablet) and the
commands that feed it one click away.

- **Scan** runs that device's `dump --save` and re-renders the page. For the
  phone and tablet it is enabled only while the device is attached, awake and
  unlocked (`launchpad-map phone status`), since a scan drives the screen.
- **Apply Proposal…** lists the device's drafts and, after a confirmation, runs
  `write` on the one picked.
- Progress shows in the bar at the bottom; **Log** shows every line.

The app runs the commands of the checkout it was built from, so rebuild it with
`app/bundle.sh` after changing the app; changes to the commands need no rebuild.

## Showcase

`launchpad-map showcase` renders the newest snapshot with only the apps you have
approved, to `showcase.html`, for publishing. Approvals live in `showcase.json`
(gitignored; copy `showcase.example.json`). An app it does not list is hidden, so
a new install never appears publicly until you review it; the history view
counts those as "not reviewed". The public page carries no approvals and no
reasons, only the apps it shows.

To review, switch the history view to **Showcase**: every app is marked shown,
hidden (with its reason) or not reviewed, and each folder says how many of its
apps are hidden.

## The layout format

`dump` emits a document that is meant to be edited by hand. Apps are named by
their Launchpad title; folders are objects. Bundle identifiers are resolved from
the live database at render time, so a layout you write yourself never has to
carry them.

```json
{
  "title": "Proposed",
  "pages": [
    [
      { "folder": "Media", "apps": ["VLC", "GIMP", "Blender"] },
      "Google Chrome",
      "iTerm"
    ]
  ]
}
```

Each list under `pages` is one home screen. A folder's `apps` list is chunked
into folder pages automatically, and any folder that needs more than one page is
flagged in red in the output — the whole reason this exists.

## What it shows

- Every home page and every folder, with the icons macOS actually uses
- Folder tiles you can click to see the contents, Launchpad-style
- App counts per folder, and a warning on anything that spills past one page
- A page holds 7×5 = 35 icons; override with `--page-size` if your grid differs

## Writing a layout

`launchpad-map write layout.json` backs the database up, applies the layout in
one transaction, restarts the Dock, and then **re-reads the database to check
the Dock actually kept what was written** — rolling back automatically if it did
not. `--dry-run` reports every move and writes nothing.

Three things about this database will eat a layout if you do not know them, and
all three are handled here:

**The first root page is `HOLDINGPAGE`.** It sits at ordering 0, is never
displayed, and is Launchpad's inbox for newly installed apps. Its contents are
flushed onto the real pages at startup — so a layout written into it is torn
apart the moment the Dock restarts, leaving folders behind as empty shells. Real
pages start at ordering 1.

**The Dock saves its in-memory layout when asked to quit.** `killall Dock` sends
SIGTERM, and the departing Dock writes its pre-write copy of the layout straight
over the file you just changed. Use SIGKILL. This applies just as much to
restoring a backup as to writing a new layout.

**Recent changes live in the write-ahead log, not the database file.** Copying
just `db` is not a backup and restoring just `db` is not a restore — `db`,
`db-wal` and `db-shm` travel together, or the layout silently reverts to
whenever SQLite last checkpointed. Backups here keep all three, and the writer
checkpoints the WAL before letting the Dock restart.

Backups land in `~/Library/Application Support/launchpad-backups/` by default,
and the path is printed on every run.

## App inventory

`launchpad-map inventory INVENTORY.md` cross-references a saved app inventory
against this machine and writes a self-contained HTML report — unrelated to
Launchpad's own layout, but it shares this repo's icon pipeline, hence
living here. The inventory is the markdown produced during a machine
offboarding, with three sections:

```
## Mac App Store (mas list) ...
1234567890  App Name    (1.2.3)
## Homebrew casks (brew list --cask) ...
cask-one cask-two ...
## /Applications (full)
Some App.app
```

Each entry is marked **on both**, **not here** (in the inventory but missing
locally) or **new here** (installed locally since the inventory was taken).
Local state — installed casks, `mas list`, `/Applications` — is collected at
run time; icons come from `lib/icon-export.swift`, the same helper `render`
uses, resolving each local bundle's `CFBundleIdentifier` from its
`Info.plist` (via `plutil`) and falling back to icon-export's own by-name
filesystem search for bundles that carry no identifier at all (Platypus-style
launcher wrappers, or a Catalyst app whose real bundle sits under `Wrapper/`).

```
launchpad-map inventory offboarding-inventory.md --out /tmp/report.html
```

`--label` names the inventory's machine in the report (default: the file's
stem); `--no-icons` skips icon extraction for a much smaller file. Read-only.

## Requirements

macOS with the Swift toolchain (`/usr/bin/swift`, present with the Command Line
Tools) and Python 3. Icons come from AppKit via `NSWorkspace`, so they match the
system exactly, including apps that only exist inside a wrapper directory.

## Where the database lives

`$(getconf DARWIN_USER_DIR)/../0/com.apple.dock.launchpad/db/db` — a per-user
temporary directory, which is why the path is derived rather than written down.
The tool copies the database (and its WAL) before reading, so it never contends
with the Dock for a lock.

## Note on Launchpad's future

Launchpad is the macOS 15 and earlier app grid. macOS 26 replaces it with the
Spotlight app view, which does not use this database.

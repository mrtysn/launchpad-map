#!/usr/bin/env python3
"""Apply a proposal to the phone's home screen by driving the launcher's edit mode.

The launcher's database is private without root, so the layout is changed the
way a finger changes it. Every move uses a gesture verified in docs/phone.md
(edit mode, multi-select, carrying a stack across pages, dropping it into an
open folder or onto an empty cell, Group, Edit folder) and is checked from a
UI dump before the next one starts. Anything unexpected stops the run; nothing
is retried blind, and the trash / Uninstall targets are never approached.

The plan is computed from the live layout (a fresh `phone dump`), never from an
old snapshot, and `--dry-run` prints it without touching the phone.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phone as ph  # noqa: E402

# Edit-mode and drag-mode grids, measured on the Xiaomi 15T Pro (docs/phone.md).
EDIT = {"ox": 119, "oy": 431, "cw": 260.5, "ch": 261.5}
DRAG = {"ox": 119, "oy": 471, "cw": 260.5, "ch": 261.5}
GROUP_BTN = (200, 272)          # [70,215][331,330]
DONE_BTN = (1079, 272)          # [949,215][1210,330]
EDITOR_OK = (1165, 373)         # [1101,309][1229,437]
POPUP_CENTRE = (640, 1000)
EDGE_NEXT, EDGE_PREV = 1272, 8
CENTRE = (640, 1300)
SETTINGS = "com.miui.home/.settings.MiuiHomeSettingActivity"
LOCK_DESC = "Lock Home screen layout"
CHECKED = re.compile(r"^(.*), (checked|unchecked)$")
# One frame per finished operation, kept for reading back what a run did.
LOG = os.path.join(ph.LAYOUTS, ".write-log")


def die(msg):
    raise SystemExit(f"launchpad-map phone write: {msg}")


def centre(g, c, r):
    return round(g["ox"] + g["cw"] * (c + 0.5)), round(g["oy"] + g["ch"] * (r + 0.5))


def mid(b):
    return (b[0] + b[2]) // 2, (b[1] + b[3]) // 2


# ---------------------------------------------------------------- planning

def placements(doc):
    """[(title, ("page", n) | ("folder", name))] for every app, either layout
    shape. A list, not a dict: a title can occur twice (two apps called
    "Calendar"), and each occurrence is one placement."""
    out = []
    for pi, pg in enumerate(doc["pages"]):
        for it in pg:
            if isinstance(it, str):
                out.append((it, ("page", pi + 1)))
            elif "folder" in it:
                out += [(a, ("folder", it["folder"])) for a in it["apps"]]
            elif "app" in it:
                out.append((it["app"], ("page", pi + 1)))
    return out


def folder_page(doc, name):
    for pi, pg in enumerate(doc["pages"]):
        for it in pg:
            if isinstance(it, dict) and it.get("folder") == name:
                return pi + 1
    return None


def free_cells(live, page):
    cols, rows = live["grid"]["cols"], live["grid"]["rows"]
    used = set()
    for it in live["pages"][page - 1]:
        c, r = it["at"]
        w, h = it.get("span", [1, 1])
        used |= {(c + i, r + j) for i in range(w) for j in range(h)}
    return cols * rows - len(used)


def plan(live, proposal):
    """The ordered operations that take `live` to `proposal`.

    Only loose apps move; apps already inside a folder stay where they are
    (the proposal has no folder-to-folder moves, and the drag out of a folder
    is a slower gesture). A folder that does not exist yet is built on the page
    the proposal puts it on when that page has room for its apps, so only
    verified gestures are needed; otherwise it is grouped on the page holding
    most of its apps and dragged over at the end."""
    wanted = {}
    for title, where in placements(proposal):
        wanted.setdefault(title, []).append(where)
    live_folders = {it["folder"] for pg in live["pages"] for it in pg if "folder" in it}
    # Apps already inside a folder keep the matching placement, so a duplicate
    # title loose on a page is matched against what is left.
    for pg in live["pages"]:
        for it in pg:
            if "folder" in it:
                for a in it["apps"]:
                    if ("folder", it["folder"]) in wanted.get(a, []):
                        wanted[a].remove(("folder", it["folder"]))
    moves = {}  # target -> {page: [apps]}
    for pi, pg in enumerate(live["pages"]):
        for it in pg:
            if "app" not in it:
                continue
            app, page = it["app"], pi + 1
            options = wanted.get(app)
            if not options:
                print(f"note: {app!r} on page {page} is not in the proposal; it stays where it is", file=sys.stderr)
                continue
            target = ("page", page) if ("page", page) in options else options[0]
            options.remove(target)
            if target != ("page", page):
                moves.setdefault(target, {}).setdefault(page, []).append(app)
    ops, late = [], []
    room = {p: free_cells(live, p) for p in range(1, len(live["pages"]) + 1)}
    for name in sorted({t[1] for t in moves if t[0] == "folder"} - live_folders):
        pages = moves.pop(("folder", name))
        total = sum(len(v) for v in pages.values())
        dst = folder_page(proposal, name)
        if dst and dst not in pages and room.get(dst, 0) >= total:
            for page, apps in sorted(pages.items()):
                ops.append({"op": "to-page", "page": page, "apps": apps, "target": dst})
            ops.append({"op": "group", "page": dst, "apps": [a for _, v in sorted(pages.items()) for a in v], "name": name})
            room[dst] -= 1
        else:
            home = max(pages, key=lambda p: len(pages[p]))
            ops.append({"op": "group", "page": home, "apps": pages.pop(home), "name": name})
            for page, apps in sorted(pages.items()):
                ops.append({"op": "into-folder", "page": page, "apps": apps, "folder": name})
            if dst and dst != home:
                late.append({"op": "move-folder", "folder": name, "page": home, "target": dst})
    for target, pages in sorted(moves.items(), key=lambda kv: str(kv[0])):
        for page, apps in sorted(pages.items()):
            if target[0] == "folder":
                ops.append({"op": "into-folder", "page": page, "apps": apps, "folder": target[1]})
            else:
                ops.append({"op": "to-page", "page": page, "apps": apps, "target": target[1]})
    for pg in live["pages"]:
        for it in pg:
            if "folder" not in it:
                continue
            want_order = [a for a, w in placements(proposal) if w == ("folder", it["folder"])]
            have = [a for a in it["apps"] if a in want_order]
            expect = [a for a in want_order if a in it["apps"]]
            if have != expect:
                tail = expect[len(lcp(have, expect)):]
                late.append({"op": "reorder", "folder": it["folder"], "apps": tail})
    return ops + late


def lcp(a, b):
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return a[:n]


def describe(op):
    if op["op"] == "group":
        note = "" if op["name"].isascii() else "  (name is not ASCII: the folder will be called 'Folder'; rename it by hand)"
        return f"page {op['page']}: Group {len(op['apps'])} apps into a new folder {op['name']!r}{note}"
    if op["op"] == "into-folder":
        return f"page {op['page']}: carry {len(op['apps'])} apps into folder {op['folder']!r}"
    if op["op"] == "to-page":
        return f"page {op['page']}: carry {len(op['apps'])} apps to page {op['target']}"
    if op["op"] == "reorder":
        return f"folder {op['folder']!r}: move {len(op['apps'])} apps to the end"
    return f"page {op['page']}: drag folder {op['folder']!r} to page {op['target']}  [UNVERIFIED gesture]"


# ---------------------------------------------------------------- judging

JUDGE_THRESHOLD = 0.7


def judge(state: str, questions: dict):
    """Ask system-one (local decision model) about a screen state. Returns
    the answers dict, or None when the server is down (fail open: the
    string-exact checks still stand on their own)."""
    try:
        out = subprocess.run(["system-one", "ask"], input=json.dumps({"state": state, "questions": questions}),
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout)["answers"]
    except (ValueError, KeyError):
        return None


def screen_state(scr, front: str, edit: bool, popup_title: str, popup_items: list, expected: str) -> str:
    pg = scr.page()
    icons = [n["label"] for n in scr.launcher() if n["cls"] == "TextView" and n["label"]
             and n["b"][3] - n["b"][1] >= 100][:40]
    return (f"Android launcher (HyperOS home screen), read from a UI dump after a gesture.\n"
            f"expected result: {expected}\n"
            f"page indicator: {pg[0] if pg else 'none'} of {pg[1] if pg else '?'}\n"
            f"edit mode (icons carry checkboxes): {'yes' if edit else 'no'}\n"
            f"foreground window: {front}\n"
            f"open folder popup: {popup_title or 'none'}; its first items: {', '.join(popup_items[:12]) or '-'}\n"
            f"icons visible: {', '.join(icons)}")


# ---------------------------------------------------------------- driving

class Driver:
    def __init__(self, phone: ph.Phone, live):
        self.p, self.walk, self.live = phone, ph.Walk(phone), live
        self.folder_at = {}   # folder name -> (page, [c, r]) as last seen
        self.free = {}        # page -> set of (c, r) known empty
        cols, rows = live["grid"]["cols"], live["grid"]["rows"]
        for pi, pg in enumerate(live["pages"]):
            used = set()
            for it in pg:
                c, r = it["at"]
                w, h = it.get("span", [1, 1])
                used |= {(c + i, r + j) for i in range(w) for j in range(h)}
                if "folder" in it:
                    self.folder_at[it["folder"]] = (pi + 1, it["at"])
            self.free[pi + 1] = {(c, r) for c in range(cols) for r in range(rows)} - used

    # -- reading
    def dump(self):
        return self.p.screen()

    def page_no(self, scr=None):
        pg = (scr or self.dump()).page()
        return pg[0] if pg else None

    def edit_mode(self, scr):
        return any(CHECKED.match(n["desc"]) for n in scr.launcher())

    def find(self, scr, label):
        """Bounds of the icon labelled `label` on this screen (edit-mode suffix stripped)."""
        for n in scr.launcher():
            if n["cls"] != "TextView" or len(n["b"]) != 4:
                continue
            m = CHECKED.match(n["desc"])
            name = m.group(1) if m else n["label"]
            if ph.BADGE.sub("", name).strip() == label and n["b"][3] - n["b"][1] >= 100:
                return n["b"]
        return None

    def folder_cell_bounds(self, scr, name):
        """A folder's icon cell, found from its caption underneath."""
        caps = [n for n in scr.launcher() if n["cls"] == "TextView" and n["label"] == name
                and n["b"][3] - n["b"][1] < 100]
        if not caps:
            return None
        cb = caps[0]["b"]
        cx = (cb[0] + cb[2]) // 2
        cells = [n["b"] for n in scr.launcher() if n["cls"] == "RelativeLayout" and len(n["b"]) == 4
                 and n["b"][0] <= cx <= n["b"][2] and n["b"][3] >= cb[1] - 5 and n["b"][1] < cb[1]]
        return min(cells, key=lambda b: (b[3] - b[1]) * (b[2] - b[0])) if cells else None

    def popup_items(self, scr):
        """Labels in the open folder, top to bottom. Page icons behind the
        popup are in the dump too; the popup's columns tell them apart."""
        items = [(a, b) for a, b in self.walk.popup(scr, set())[1] if b[0] in self.POP_COLS]
        return [(CHECKED.match(a) or re.match(r"(.*)", a)).group(1) for a, _ in items]

    def popup_bounds(self, scr, label):
        for a, b in self.walk.popup(scr, set())[1]:
            if b[0] in self.POP_COLS and (CHECKED.match(a) or re.match(r"(.*)", a)).group(1) == label:
                return b
        return None

    def folder_contains(self, names, tries=6):
        """Scroll through the open folder until every name is seen."""
        seen, pending = [], set(names)
        for _ in range(tries):
            scr = self.dump()
            items = self.popup_items(scr)
            if not items:
                return False, seen
            seen += [i for i in items if i not in seen]
            pending -= set(items)
            if not pending:
                return True, seen
            full = [b for a, b in self.walk.popup(scr, set())[1] if b[0] in self.POP_COLS]
            top, bottom = min(b[1] for b in full), max(b[3] for b in full)
            row = min(b[3] - b[1] for b in full)
            cx, cy = scr.width // 2, (top + bottom) // 2
            self.p.swipe(cx, cy + row, cx, cy - row, wait=0.6, ms=600)
        return False, seen

    # -- launcher settings: the layout lock
    def lock_state(self):
        raw = self.p.adb("exec-out", "uiautomator", "dump", "/dev/tty")
        m = re.search(r'content-desc="' + re.escape(LOCK_DESC) + r'"[^>]*checked="(true|false)"', raw)
        b = re.search(r'content-desc="' + re.escape(LOCK_DESC) + r'"[^>]*bounds="([^"]+)"', raw)
        if not m or not b:
            die("could not find the layout lock switch in the launcher settings")
        return m.group(1) == "true", [int(v) for v in re.findall(r"\d+", b.group(1))]

    def set_lock(self, on: bool):
        self.p.adb("shell", "am", "start", "-n", SETTINGS)
        time.sleep(1.2)
        state, bounds = self.lock_state()
        if state != on:
            self.p.tap(*mid(bounds), wait=0.8)
            if self.lock_state()[0] != on:
                die("the layout lock did not change state")
        self.p.key("KEYCODE_BACK", 0.6)
        self.p.key("KEYCODE_HOME", 0.8)
        print(f"layout lock {'on' if on else 'off'}", file=sys.stderr)

    # -- navigation
    def go(self, page):
        scr, _ = ph.go_to(self.walk, page)
        return scr

    def leave_edit(self):
        scr = self.dump()
        if self.edit_mode(scr):
            self.p.tap(*DONE_BTN, wait=0.8)
        self.p.key("KEYCODE_BACK", 0.4)

    def enter_edit(self, page):
        """Edit mode, showing `page`. Entered by a long press on an empty cell
        of this page, or of a neighbour when this page is full."""
        self.leave_edit()
        self.go(page)
        g = self.walk.grid(self.dump(), self.dump().page()[2])
        candidates = [page] + [p for d in (1, 2, 3) for p in (page - d, page + d) if p in self.free]
        for p in candidates:
            if self.free.get(p):
                c, r = sorted(self.free[p])[-1]
                self.go(p)
                x, y = centre(g, c, r)
                for hold in (1.0, 1.4):
                    time.sleep(0.6)
                    self.p.adb("shell", f"input motionevent DOWN {x} {y}; sleep {hold}; input motionevent UP {x} {y}")
                    time.sleep(0.8)
                    scr = self.dump()
                    if self.edit_mode(scr):
                        break
                else:
                    die(f"long press on empty cell {c},{r} of page {p} did not enter edit mode")
                if p != page:
                    step = (scr.width - 180, 180) if p < page else (180, scr.width - 180)
                    for _ in range(abs(page - p)):
                        self.p.swipe(step[0], 1200, step[1], 1200, wait=0.4)
                    scr = self.dump()
                    if not self.edit_mode(scr) or self.page_no(scr) != page:
                        die(f"could not reach page {page} in edit mode (at {self.page_no(scr)})")
                return scr
        die(f"no empty cell near page {page} to enter edit mode from")

    def select(self, scr, apps):
        for a in apps:
            b = self.find(scr, a)
            if not b:
                die(f"{a!r} is not on this page")
            self.p.tap(*mid(b), wait=0.35)
        scr = self.dump()
        bad = [a for a in apps if not any(CHECKED.match(n["desc"]) and CHECKED.match(n["desc"]).group(1) == a
                                          and n["desc"].endswith(", checked") for n in scr.launcher())]
        if bad:
            die(f"not selected after tapping: {bad}")
        return scr

    # -- the one-shot gestures, run entirely on the phone
    @staticmethod
    def page_loop(target, start=None):
        """Shell for the phone: turn pages by touching the edge until the
        indicator reads `target`. When the starting page is known the turns are
        sent blind (one 0.5 s edge touch is one page, measured) and a single
        dump confirms; the reading loop only corrects a miss. The dump goes to
        a file, since without a terminal `uiautomator dump /dev/tty` prints
        only its banner. An unreadable page is never treated as a number (this
        shell's `[ "" -lt N ]` is true)."""
        blind = ""
        if start is not None and start != target:
            e = EDGE_NEXT if start < target else EDGE_PREV
            one = f"input motionevent MOVE {e} {CENTRE[1]}; sleep 0.5; input motionevent MOVE {CENTRE[0]} {CENTRE[1]}; sleep 0.7; "
            blind = one * abs(target - start)
        return blind + (
            "reached=0; for i in 1 2 3 4; do "
            "uiautomator dump /data/local/tmp/lm-page.xml >/dev/null 2>&1; "
            "cur=$(grep -o 'Page [0-9]* of' /data/local/tmp/lm-page.xml | head -1 | grep -o '[0-9]*'); "
            "echo \"page:$cur\"; "
            "case \"$cur\" in ''|*[!0-9]*) sleep 0.5; continue;; esac; "
            f"if [ \"$cur\" -eq {target} ]; then reached=1; break; fi; "
            f"if [ \"$cur\" -lt {target} ]; then e={EDGE_NEXT}; else e={EDGE_PREV}; fi; "
            f"input motionevent MOVE $e {CENTRE[1]}; sleep 0.5; input motionevent MOVE {CENTRE[0]} {CENTRE[1]}; sleep 0.7; "
            "done; "
        )

    def carry(self, from_b, target_page, then, start=None):
        """Pick up the icon at `from_b` (a selected icon lifts the whole stack),
        carry it to `target_page`, then run `then` (more shell, ending in UP,
        or printing ABORT after putting the stack back). If the target page is
        never confirmed the stack is put back where it was picked up."""
        x, y = mid(from_b)
        # Putting the stack back means carrying it home first: releasing at the
        # pick-up coordinates on the wrong page drops it there.
        home = ""
        if start is not None and start != target_page:
            e = EDGE_NEXT if target_page < start else EDGE_PREV
            home = (f"input motionevent MOVE {e} {CENTRE[1]}; sleep 0.5; "
                    f"input motionevent MOVE {CENTRE[0]} {CENTRE[1]}; sleep 0.7; ") * abs(start - target_page)
        back = f"{home}input motionevent MOVE {x} {y}; sleep 0.8; input motionevent UP {x} {y}; echo ABORT"
        script = (
            f"input motionevent DOWN {x} {y}; sleep 0.9; "
            f"input motionevent MOVE {x + 20} {y}; sleep 0.2; "
            f"input motionevent MOVE {CENTRE[0]} {CENTRE[1]}; sleep 0.6; "
            + self.page_loop(target_page, start)
            + f"if [ $reached = 1 ]; then {then.replace('__BACK__', back)}; "
            + f"else {back}; fi"
        )
        out = self.gesture(script)
        if "ABORT" in out:
            die(f"the gesture was abandoned and the stack put back: {' '.join(out.split())[-200:]}")

    def gesture(self, script: str) -> str:
        """Run one shell script on the phone as a single adb call, so no
        network delay falls inside a gesture. Pushed as a file: the scripts
        carry app names with quotes and ampersands."""
        local = os.path.join(self.p.work, "gesture.sh")
        with open(local, "w") as fh:
            fh.write(script + "\n")
        self.p.adb("push", local, "/data/local/tmp/lm-gesture.sh")
        out = self.p.adb("shell", "sh", "/data/local/tmp/lm-gesture.sh")
        time.sleep(0.8)
        print("   " + " ".join(out.split())[-300:], file=sys.stderr)
        return out

    # Edit-mode folder popup on this phone: 3 columns at x 239 / 513 / 786
    # (261 px wide, 274 apart), rows 294 apart. Page icons behind it sit on a
    # different x grid (119 / 379 / 640 / 901), which is how the two are told apart.
    POP_COLS = (239, 513, 786)
    POP_W, POP_DX, POP_DY = 261, 274, 294

    @classmethod
    def after_last(cls, last: str, max_scrolls=12) -> str:
        """Shell: with the stack held over an open folder popup, find the
        folder's last app in a UI dump, scrolling the popup by hovering at its
        bottom edge until it is on screen, then release just after it."""
        pat = re.escape(last + ", unchecked").replace("\\ ", " ").replace("'", "'\\''")
        cols = "(" + "|".join(str(c) for c in cls.POP_COLS) + ")"
        item = "bounds=\"\\[" + cols + ",[0-9]*\\]\\[[0-9]*,[0-9]*\\]\""
        return (
            f"found=0; for j in $(seq 1 {max_scrolls}); do "
            "uiautomator dump /data/local/tmp/lm-pop.xml >/dev/null 2>&1; "
            f"b=$(grep -oE 'content-desc=\"{pat}\"[^>]*{item}' /data/local/tmp/lm-pop.xml | head -1 | grep -o 'bounds=\"[^\"]*\"' | grep -o '[0-9]*' | tr '\\n' ' '); "
            "if [ -n \"$b\" ]; then found=1; break; fi; "
            f"edge=$(grep -oE 'unchecked\"[^>]*{item}' /data/local/tmp/lm-pop.xml | grep -o ',[0-9]*\\]\"$' | tr -d ',]\"' | sort -n | tail -1); "
            "[ -z \"$edge\" ] && edge=1900; "
            "input motionevent MOVE 640 $((edge - 30)); sleep 1.3; input motionevent MOVE 640 $((edge - 400)); sleep 0.6; "
            "done; "
            f"if [ $found = 1 ]; then set -- $b; "
            f"if [ $1 -lt 700 ]; then rx=$(($1 + {cls.POP_DX} + {cls.POP_W // 2})); ry=$(($2 + {cls.POP_W // 2})); "
            f"else rx=$(({cls.POP_COLS[0]} + {cls.POP_W // 2})); ry=$(($2 + {cls.POP_DY} + {cls.POP_W // 2})); fi; "
            "echo \"release:$rx,$ry after:$b\"; input motionevent MOVE $rx $ry; sleep 0.9; input motionevent UP $rx $ry; echo dropped; "
            "else echo 'last not found'; __BACK__; fi"
        )

    # -- operations
    def into_folder(self, op):
        page, apps, name = op["page"], op["apps"], op["folder"]
        fpage, fat = self.folder_at.get(name, (None, None))
        if fpage is None:
            die(f"folder {name!r} is not on the phone")
        scr = self.enter_edit(page)
        scr = self.select(scr, apps)
        fx, fy = centre(EDIT, *fat)
        last = self.folder_entry(name)["apps"][-1]
        then = (f"input motionevent MOVE {fx} {fy}; sleep 1.4; "
                f"input motionevent MOVE {POPUP_CENTRE[0]} {POPUP_CENTRE[1]}; sleep 0.6; "
                + self.after_last(last))
        self.carry(self.find(scr, apps[0]), fpage, then, start=page)
        ok, seen = self.folder_contains(apps)
        if not ok:
            die(f"after the drop, folder {name!r} shows {seen[:8]}…; missing {sorted(set(apps) - set(seen))}")
        self.take(page, apps)
        self.folder_entry(name)["apps"] += apps
        expect = self.folder_entry(name)["apps"]
        if seen[-len(expect):] != expect and seen != expect:
            print(f"   order in {name!r} is not the expected one; a reorder op will follow", file=sys.stderr)
            self.folder_entry(name)["apps"] = seen if len(seen) >= len(expect) else expect
        self.leave_edit()

    def take(self, page, apps):
        """Drop `apps` from `page` in the in-memory layout; their cells are free."""
        entries = self.live["pages"][page - 1]
        self.free[page] |= {tuple(it["at"]) for it in entries if it.get("app") in apps}
        entries[:] = [it for it in entries if it.get("app") not in apps]

    def folder_entry(self, name):
        for pg in self.live["pages"]:
            for it in pg:
                if it.get("folder") == name:
                    return it
        die(f"folder {name!r} vanished from the layout")

    def to_page(self, op):
        page, apps, target = op["page"], op["apps"], op["target"]
        cells = sorted(self.free[target], key=lambda cr: (cr[1], cr[0]))
        if len(cells) < len(apps):
            die(f"page {target} has {len(cells)} free cells for {len(apps)} apps")
        scr = self.enter_edit(page)
        scr = self.select(scr, apps)
        tx, ty = centre(EDIT, *cells[0])
        then = f"input motionevent MOVE {tx} {ty}; sleep 0.9; input motionevent UP {tx} {ty}"
        self.carry(self.find(scr, apps[0]), target, then, start=page)
        scr = self.dump()
        if self.page_no(scr) != target:
            die(f"expected to be on page {target} after the drop, at {self.page_no(scr)}")
        missing = [a for a in apps if not self.find(scr, a)]
        if missing:
            die(f"not on page {target} after the drop: {missing}")
        self.take(page, apps)
        while len(self.live["pages"]) < target:
            self.live["pages"].append([])
        for a in apps:
            b = self.find(scr, a)
            c = round((b[0] - EDIT["ox"]) / EDIT["cw"]); r = round((b[1] - EDIT["oy"]) / EDIT["ch"])
            self.free[target].discard((c, r))
            self.live["pages"][target - 1].append({"app": a, "at": [c, r]})
        self.leave_edit()

    def group(self, op):
        page, apps, name = op["page"], op["apps"], op["name"]
        scr = self.enter_edit(page)
        scr = self.select(scr, apps)
        first = self.find(scr, apps[0])
        self.p.tap(*GROUP_BTN, wait=1.0)
        self.leave_edit()
        self.go(page)
        scr = self.dump()
        fb = self.folder_cell_bounds(scr, "Folder")
        if not fb:
            die("no folder named 'Folder' appeared after Group")
        g = self.walk.grid(scr, scr.page()[2])
        at = self.walk.at(g, fb)
        self.p.tap(*mid(fb), wait=0.8)
        ok, seen = self.folder_contains(apps)
        self.p.key("KEYCODE_BACK", 0.5)
        if not ok:
            die(f"the new folder shows {seen}; missing {sorted(set(apps) - set(seen))}")
        final = name if name.isascii() else "Folder"
        if final != "Folder":
            self.rename(fb, final)
        self.folder_at[name] = (page, at)
        self.take(page, apps)
        self.free[page].discard(tuple(at))
        self.live["pages"][page - 1].append({"folder": name, "apps": list(apps), "at": list(at)})

    def rename(self, fb, name):
        x, y = mid(fb)
        self.p.adb("shell", f"input motionevent DOWN {x} {y}; sleep 0.9; input motionevent UP {x} {y}")
        time.sleep(0.8)
        scr = self.dump()
        entry = next((n for n in scr.launcher() if n["label"] == "Edit folder"), None)
        if not entry:
            die("long press on the new folder did not offer 'Edit folder'")
        self.p.tap(*mid(entry["b"]), wait=1.0)
        scr = self.dump()
        field = next((n for n in scr.launcher() if n["label"] == "Folder" and n["cls"] in ("TextView", "EditText")), None)
        if not field:
            die("the folder editor did not show the name field")
        self.p.tap(*mid(field["b"]), wait=0.6)
        self.p.adb("shell", "input keyevent KEYCODE_MOVE_END")
        for _ in range(8):
            self.p.adb("shell", "input keyevent KEYCODE_DEL")
        self.p.adb("shell", "input", "text", name)
        time.sleep(0.4)
        self.p.tap(*EDITOR_OK, wait=1.0)
        scr = self.dump()
        if not self.folder_cell_bounds(scr, name):
            die(f"the folder is not called {name!r} after the rename")

    def reorder(self, op):
        """Drag `apps`, in order, to the end of folder `name` (edit mode, so
        labels read back as 'Name, unchecked'). Each drag: pick the app up
        inside the open popup, then the same scroll-to-last and release-after
        as a drop from outside."""
        name, apps = op["folder"], op["apps"]
        fpage, fat = self.folder_at[name]
        entry = self.folder_entry(name)
        self.enter_edit(fpage)
        self.p.tap(*centre(EDIT, *fat), wait=0.9)
        for a in apps:
            scr = self.dump()
            b = self.popup_bounds(scr, a)
            if not b:
                # scroll the popup to find it
                ok, _ = self.folder_contains([a])
                scr = self.dump()
                b = self.popup_bounds(scr, a)
            if not b:
                die(f"{a!r} is not in folder {name!r}")
            last = [x for x in entry["apps"] if x != a][-1]
            x, y = mid(b)
            script = (f"input motionevent DOWN {x} {y}; sleep 0.9; input motionevent MOVE {x + 20} {y}; sleep 0.3; "
                      f"input motionevent MOVE {x + 40} {y + 20}; sleep 0.5; "
                      + self.after_last(last).replace("__BACK__", f"input motionevent MOVE {x} {y}; sleep 0.8; input motionevent UP {x} {y}; echo ABORT"))
            out = self.gesture(script)
            if "ABORT" in out:
                die(f"could not move {a!r} to the end of {name!r}")
            entry["apps"].remove(a); entry["apps"].append(a)
        ok, seen = self.folder_contains(entry["apps"][-3:])
        if seen[-len(apps):] != apps:
            die(f"folder {name!r} ends with {seen[-4:]}, expected …{apps}")
        self.p.key("KEYCODE_BACK", 0.5)
        self.leave_edit()

    def move_folder(self, op):
        name, page, target = op["folder"], op["page"], op["target"]
        cells = sorted(self.free[target], key=lambda cr: (cr[1], cr[0]))
        if not cells:
            die(f"page {target} has no free cell for folder {name!r}")
        self.leave_edit()
        scr = self.go(page)
        fb = self.folder_cell_bounds(scr, name if name.isascii() else "Folder")
        if not fb:
            die(f"folder {name!r} not found on page {page}")
        tx, ty = centre(DRAG, *cells[0])
        then = f"input motionevent MOVE {tx} {ty}; sleep 0.9; input motionevent UP {tx} {ty}"
        self.carry(fb, target, then, start=page)
        scr = self.dump()
        if self.page_no(scr) != target or not self.folder_cell_bounds(scr, name if name.isascii() else "Folder"):
            die(f"folder {name!r} did not land on page {target}")
        self.free[target].discard(cells[0])
        self.free[page].add(tuple(self.folder_at[name][1]))
        self.folder_at[name] = (target, list(cells[0]))
        entry = self.folder_entry(name)
        self.live["pages"][page - 1].remove(entry)
        entry["at"] = list(cells[0])
        self.live["pages"][target - 1].append(entry)

    def gate(self, op):
        """After an operation: the string-exact checks in each op have passed;
        system-one then judges the whole screen once more, and a confident
        'not as expected' or 'not safe to go on' stops the run."""
        scr = self.dump()
        title, items = self.walk.popup(scr, set())
        state = screen_state(scr, self.p.front(), self.edit_mode(scr), title, [a for a, _ in items], describe(op))
        ans = judge(state, {
            "as_expected": {"type": "noul", "instructions": "Does this screen show the expected result of the operation, with the launcher settled on a home page?"},
            "stray": {"type": "choice", "instructions": "What is in front right now?",
                      "criteria": {"home": "a home page of the launcher, possibly in edit mode",
                                   "folder": "an open folder popup of the launcher",
                                   "other": "some other app, dialog, or the launcher settings"}},
        })
        if ans is None:
            print("   system-one: no answer (server down); continuing on the exact checks", file=sys.stderr)
            return
        ok, stray = ans["as_expected"]["noul"], ans["stray"]
        print(f"   system-one: as_expected={ok:.2f} front={stray['choice']} "
              f"({', '.join(f'{k} {v:.2f}' for k, v in stray['probabilities'].items())})", file=sys.stderr)
        if stray["choice"] == "other" and stray["probabilities"]["other"] >= JUDGE_THRESHOLD:
            self.p.key("KEYCODE_HOME")
            die(f"system-one sees something other than the launcher in front (p={stray['probabilities']['other']:.2f}); went HOME and stopped")
        if ok < 1 - JUDGE_THRESHOLD:
            die(f"system-one does not see the expected result (p={ok:.2f}); stopped for a look")

    def run(self, i, op):
        getattr(self, op["op"].replace("-", "_"))(op)
        self.walk.check_front(describe(op))
        self.gate(op)
        if self.p.mirror:
            shutil.copy(self.p.frame(f"op{i:02}"), os.path.join(LOG, f"op{i:02}.png"))


# ---------------------------------------------------------------- main

def fresh_dump(serial):
    """A live layout, read now, without saving a snapshot."""
    class A:
        save, out, note = False, None, None
    if os.path.exists(ph.PROGRESS):  # a resumed walk would be stale, not live
        os.remove(ph.PROGRESS)
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ph.dump_to(A, serial)
    return json.loads(buf.getvalue())


def newest_snapshot():
    names = sorted(n for n in os.listdir(ph.LAYOUTS) if re.match(r"\d{4}-\d{2}-\d{2}-\d{4}\.json$", n))
    if not names:
        die("no snapshot in layouts/phone/; run `phone dump --save` once")
    return os.path.join(ph.LAYOUTS, names[-1])


def save_snapshot(live, done, total):
    now = datetime.datetime.now()
    live = dict(live)
    live["date"], live["time"] = now.date().isoformat(), now.strftime("%H:%M")
    live["note"] = f"after phone write: {done} of {total} operations"
    for pg in live["pages"]:
        pg.sort(key=lambda e: (e["at"][1], e["at"][0]))
    path = os.path.join(ph.LAYOUTS, f"{now:%Y-%m-%d-%H%M}.json")
    with open(path, "w") as fh:
        json.dump(live, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(path)


def main():
    ap = argparse.ArgumentParser(prog="launchpad-map phone write", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("proposal", help="layout JSON to apply")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; touch nothing")
    ap.add_argument("--from", dest="snapshot", help="plan from this snapshot (default: the newest in layouts/phone/)")
    ap.add_argument("--dump", action="store_true", help="read the phone afresh before planning instead of trusting the newest snapshot")
    ap.add_argument("--serial", "-s", default=os.environ.get("ANDROID_SERIAL"))
    ap.add_argument("--max-ops", type=int, help="stop after this many operations")
    ap.add_argument("--lock", action="store_true", help="turn the layout lock back on at the end (default: leave it off)")
    args = ap.parse_args()

    with open(args.proposal) as fh:
        proposal = json.load(fh)
    serial = None if args.dry_run and not args.dump else ph.pick_serial(args.serial)
    if args.dump:
        live = fresh_dump(serial)
    else:
        with open(args.snapshot or newest_snapshot()) as fh:
            live = json.load(fh)
        if live.get("draft"):
            die("the newest layout is a draft, not a snapshot")

    ops = plan(live, proposal)
    moved = sum(len(o.get("apps", [])) for o in ops)
    print(f"{len(ops)} operations, {moved} apps", file=sys.stderr)
    for i, op in enumerate(ops, 1):
        print(f"{i:3}. {describe(op)}")
        if "apps" in op:
            print("      " + ", ".join(op["apps"]))
    if args.dry_run:
        return 0

    work = os.path.join(ph.LAYOUTS, ".write")
    os.makedirs(work, exist_ok=True)
    os.makedirs(LOG, exist_ok=True)
    phone = ph.Phone(serial, work, known=ph.load_icons())
    d = Driver(phone, live)
    d.set_lock(False)
    done = 0
    try:
        for i, op in enumerate(ops[:args.max_ops], 1):
            print(f"-- {i}/{len(ops)} {describe(op)}", file=sys.stderr)
            d.run(i, op)
            done += 1
    finally:
        try:
            d.leave_edit()
            if args.lock:
                d.set_lock(True)
        except (ph.Dropped, ph.Failed, SystemExit) as e:
            print(f"could not tidy up: {e}", file=sys.stderr)
        phone.finish()
        shutil.rmtree(work, ignore_errors=True)
        if done:
            save_snapshot(d.live, done, len(ops))
    print(f"done: {done} of {len(ops)} operations", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

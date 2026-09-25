#!/usr/bin/env python3
"""Apply a proposal to the phone's home screen by driving the launcher's edit mode.

The launcher's database is private without root, so the layout is changed the
way a finger changes it. Every move uses a gesture verified in docs/phone.md
(edit mode, multi-select, carrying a stack across pages, dropping it into an
open folder or onto an empty cell, Group, Edit folder) and is checked from a
UI dump before the next one starts. Anything unexpected stops the run; nothing
is retried blind, and the trash / Uninstall targets are never approached.

The plan is computed from the newest snapshot in layouts/phone/, or from the
driver's own record of the phone in layouts/phone/.write-log/state.json when a
previous run left the proposal half applied (`--dump` reads the phone afresh
instead). `--dry-run` prints the plan without touching the phone.

Only a finished proposal enters the history: the snapshot is saved when nothing
of it remains to do, and the partial states between runs stay in the write log.
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
STACK = 4
CENTRE = (640, 1300)
SETTINGS = "com.miui.home/.settings.MiuiHomeSettingActivity"
LOCK_DESC = "Lock Home screen layout"
CHECKED = re.compile(r"^(.*), (checked|unchecked)$")
# One frame per finished operation, kept for reading back what a run did, and
# the driver's record of the phone between runs of one proposal.
LOG = os.path.join(ph.LAYOUTS, ".write-log")
STATE = os.path.join(LOG, "state.json")


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
    outs = []   # apps to drag out of a folder onto its page first
    loose = []  # (app, page) as the plan will see them
    for pi, pg in enumerate(live["pages"]):
        for it in pg:
            if "app" in it:
                loose.append((it["app"], pi + 1))
            elif "folder" in it:
                for a in list(it["apps"]):
                    opts = wanted.get(a, [])
                    if opts and ("folder", it["folder"]) not in opts and a not in [x for x, _ in loose]:
                        outs.append({"op": "out-of-folder", "folder": it["folder"], "apps": [a], "page": pi + 1})
                        loose.append((a, pi + 1))
    for app, page in loose:
        if True:
            options = wanted.get(app)
            if not options:
                print(f"note: {app!r} on page {page} is not in the proposal; it stays where it is", file=sys.stderr)
                continue
            target = ("page", page) if ("page", page) in options else options[0]
            options.remove(target)
            if target != ("page", page):
                moves.setdefault(target, {}).setdefault(page, []).append(app)
    ops, late = list(outs), []
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
                # a stack of up to 4 drops into an open folder reliably; a 15-stack left it
                for i in range(0, len(apps), STACK):
                    ops.append({"op": "into-folder", "page": page, "apps": apps[i:i + STACK], "folder": target[1]})
            else:
                ops.append({"op": "to-page", "page": page, "apps": apps, "target": target[1]})
    for pg in live["pages"]:
        for it in pg:
            if "folder" not in it:
                continue
            want_order = [a for a, w in placements(proposal) if w == ("folder", it["folder"])]
            leaving = {o["apps"][0] for o in outs if o["folder"] == it["folder"]}
            have = [a for a in it["apps"] if a in want_order and a not in leaving]
            expect = [a for a in want_order if a in it["apps"]]
            if have != expect:
                # the shortest tail of the wanted order whose removal leaves
                # the rest already in place: those are the apps to drag to the end
                tail = next((expect[-k:] for k in range(1, len(expect))
                             if [a for a in have if a not in expect[-k:]] == expect[:-k]), None)
                if tail is None:
                    print(f"note: folder {it['folder']!r} is ordered differently from the proposal in a way "
                          f"a move-to-end cannot fix; left as it is", file=sys.stderr)
                else:
                    late.append({"op": "reorder", "folder": it["folder"], "apps": tail, "order": expect})
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
    if op["op"] == "out-of-folder":
        return f"folder {op['folder']!r}: drag {op['apps'][0]!r} out onto page {op['page']}"
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


# What the launcher can be showing when a post-move check fails, and what the
# driver does about each. Only BACK, HOME, a page turn and one more look are
# ever taken on its own; everything else stops the run for a human.
CLASSES = {
    "folder_open": "a folder popup is still open over the page",
    "wrong_page": "the page indicator shows a page other than the target",
    "dialog": "a system or app dialog, sheet or another app is in front",
    "locked": "the lock screen or keyguard is showing",
    "unsettled": "the launcher is mid-animation or the tree is incomplete (few nodes, no page indicator)",
    "landed_off": "the moved apps are on the target page or in the folder but not where planned",
    "ok": "the expected layout is present as described",
    "unknown": "none of the above fits",
}
CLASSIFIED_RETRIES = 2
CLASSIFY_LOG = os.path.join(LOG, "classify.jsonl")


def tree_excerpt(scr, front: str, target_b=None) -> str:
    """The part of a UI dump a mismatch can be classified from: page
    indicator, wide nodes (dialogs, sheets, popups), the target cell, window
    and dialog titles, the keyguard if present."""
    lines = [f"foreground window: {front}", f"nodes in tree: {len(scr.nodes)}"]
    pg = scr.page()
    lines.append(f"page indicator: {pg[0]} of {pg[1]}" if pg else "page indicator: none")
    for n in scr.nodes:
        if len(n["b"]) != 4:
            continue
        wide = n["b"][2] - n["b"][0] >= 0.6 * scr.width
        keyguard = "keyguard" in (n["pkg"] or "") or "lock" in n["desc"].lower()
        titled = n["cls"] in ("TextView", "Button") and n["label"] and n["pkg"] != ph.LAUNCHER
        if wide or keyguard or titled:
            lines.append(f"{n['pkg']} {n['cls']} {n['b']} {n['desc'][:60]!r}")
    if target_b:
        inside = [n for n in scr.launcher() if n["label"] and n["b"][0] >= target_b[0] - 5
                  and n["b"][2] <= target_b[2] + 5 and n["b"][1] >= target_b[1] - 5 and n["b"][3] <= target_b[3] + 5]
        lines.append(f"target cell {target_b}: " + (", ".join(n["label"] for n in inside[:6]) or "empty"))
    return "\n".join(lines[:60])


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
        self.dumps = getattr(self, "dumps", 0) + 1
        return self.p.screen()

    # -- classifying a failed check
    def classify(self, scr, expected: str, target_b=None):
        """One system-one question over the dump: what is the launcher showing?
        Returns (class, probabilities); ('unknown', {}) when there is no answer.
        Every verdict is appended to the write log for later labelling."""
        state = f"expected: {expected}\n" + tree_excerpt(scr, self.p.front(), target_b)
        ans = judge(state, {"state": {"type": "choice", "instructions": "What is the launcher showing after the move?",
                                      "criteria": CLASSES}})
        choice, probs = ("unknown", {}) if ans is None else (ans["state"]["choice"], ans["state"]["probabilities"])
        os.makedirs(LOG, exist_ok=True)
        with open(CLASSIFY_LOG, "a") as fh:
            fh.write(json.dumps({"time": datetime.datetime.now().isoformat(timespec="seconds"), "expected": expected,
                                 "choice": choice, "probabilities": probs, "state": state}, ensure_ascii=False) + "\n")
        print(f"   system-one: {choice} ({', '.join(f'{k} {v:.2f}' for k, v in probs.items())})", file=sys.stderr)
        return choice, probs

    def verified(self, check, expected: str, target_page=None, target_b=None):
        """Run `check() -> (ok, detail)`. On a failure the screen is classified
        once and the class decides: folder_open -> BACK, wrong_page -> turn to
        the target page, unsettled -> one more look after 0.6 s, each followed
        by the check again, at most CLASSIFIED_RETRIES times; landed_off comes
        back to the caller as (False, detail); anything else stops the run."""
        for attempt in range(CLASSIFIED_RETRIES + 1):
            ok, detail = check()
            if ok:
                return True, detail
            if attempt == CLASSIFIED_RETRIES:
                break
            choice, probs = self.classify(self.dump(), expected, target_b)
            if choice == "folder_open" and probs.get(choice, 0) >= JUDGE_THRESHOLD:
                self.p.key("KEYCODE_BACK", 0.6)
            elif choice == "wrong_page" and probs.get(choice, 0) >= JUDGE_THRESHOLD and target_page:
                self.go(target_page)
            elif choice == "unsettled" and probs.get(choice, 0) >= JUDGE_THRESHOLD:
                time.sleep(0.6)
            elif choice == "landed_off" and probs.get(choice, 0) >= JUDGE_THRESHOLD:
                return False, detail
            else:
                break
        die(f"{expected}: not confirmed ({detail}); stopped for a look")

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
    def after_last(cls, last: str, max_scrolls=16) -> str:
        """Shell: with the stack held over an open folder popup, find the
        folder's last app in a UI dump, scrolling the popup by hovering at its
        bottom edge until it is on screen, then release just after it."""
        pat = re.escape(last + ", unchecked").replace("\\ ", " ").replace("'", "'\\''")
        cols = "(" + "|".join(str(c) for c in cls.POP_COLS) + ")"
        item = "bounds=\"\\[" + cols + ",[0-9]*\\]\\[[0-9]*,[0-9]*\\]\""
        return (
            f"found=0; for j in $(seq 1 {max_scrolls}); do "
            # a fresh dump every time: uiautomator writes nothing while the popup is still
            # animating, and a stale file would be read as "still not there" forever
            "rm -f /data/local/tmp/lm-pop.xml; sleep 0.6; uiautomator dump /data/local/tmp/lm-pop.xml >/dev/null 2>&1 || sleep 0.8; "
            "[ -s /data/local/tmp/lm-pop.xml ] || { sleep 0.8; uiautomator dump /data/local/tmp/lm-pop.xml >/dev/null 2>&1; }; "
            "echo \"scan:$j:$(wc -c < /data/local/tmp/lm-pop.xml 2>/dev/null)\"; "
            f"b=$(grep -oE 'content-desc=\"{pat}\"[^>]*{item}' /data/local/tmp/lm-pop.xml | head -1 | grep -o 'bounds=\"[^\"]*\"' | grep -o '[0-9]*' | tr '\\n' ' '); "
            "if [ -n \"$b\" ]; then found=1; break; fi; "
            f"edge=$(grep -oE 'unchecked\"[^>]*{item}' /data/local/tmp/lm-pop.xml | grep -o ',[0-9]*\\]\"$' | tr -d ',]\"' | sort -n | tail -1); "
            "[ -z \"$edge\" ] && edge=1900; "
            "input motionevent MOVE 640 $((edge - 150)); sleep 1.3; input motionevent MOVE 640 $((edge - 400)); sleep 0.6; "
            "done; "
            f"if [ $found = 1 ]; then set -- $b; "
            # clipped at the grid's bottom edge: scroll once more so the whole cell (and the
            # slot after it) is inside the grid, then re-read its bounds
            f"if [ $(($4 - $2)) -lt 200 ]; then input motionevent MOVE 640 $(($4 - 120)); sleep 1.3; input motionevent MOVE 640 $(($4 - 500)); sleep 0.8; "
            "rm -f /data/local/tmp/lm-pop.xml; uiautomator dump /data/local/tmp/lm-pop.xml >/dev/null 2>&1; "
            f"b2=$(grep -oE 'content-desc=\"{pat}\"[^>]*{item}' /data/local/tmp/lm-pop.xml | head -1 | grep -o 'bounds=\"[^\"]*\"' | grep -o '[0-9]*' | tr '\\n' ' '); "
            "[ -n \"$b2\" ] && set -- $b2; echo \"unclipped:$b2\"; fi; "
            f"if [ $1 -lt 700 ]; then rx=$(($1 + {cls.POP_DX} + {cls.POP_W // 2})); ry=$(($2 + {cls.POP_W // 2})); "
            # row full: no empty cell exists in the grid, so release on the last app
            # itself, which inserts the stack before it; the caller then moves that
            # app back in front of the stack (release on an item = insert before it)
            f"else rx=$(($1 + {cls.POP_W // 2})); ry=$(($2 + {cls.POP_W // 2})); echo before-last; fi; "
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
        if not any(self.find(scr, a) for a in apps):
            self.leave_edit()
            if self.reconcile(op):
                print("   already in the folder from an interrupted run; recorded", file=sys.stderr)
                return
            die(f"{apps} are not on page {page}")
        scr = self.select(scr, apps)
        fx, fy = centre(EDIT, *fat)
        last = self.folder_entry(name)["apps"][-1]
        then = (f"input motionevent MOVE {fx} {fy}; sleep 1.4; "
                f"input motionevent MOVE {POPUP_CENTRE[0]} {POPUP_CENTRE[1]}; sleep 0.6; "
                + self.after_last(last))
        self.carry(self.find(scr, apps[0]), fpage, then, start=page)
        ok, seen = self.verified(lambda: self.folder_contains(apps),
                                 f"folder {name!r} open in edit mode, showing {apps}", target_page=fpage)
        if not ok:
            die(f"after the drop, folder {name!r} shows {seen[:8]}…; missing {sorted(set(apps) - set(seen))}")
        if seen.index(last) > seen.index(apps[0]):
            # the stack went in before the old last app (full last row): put that app back in front
            self.move_before(last, apps[0])
            ok, seen = self.folder_contains([last] + apps)
            if not ok or seen.index(last) > seen.index(apps[0]):
                die(f"could not move {last!r} back in front of the stack in {name!r}: {seen[-8:]}")
        self.take(page, apps)
        self.folder_entry(name)["apps"] += apps
        expect = self.folder_entry(name)["apps"]
        if seen[-len(expect):] != expect and seen != expect:
            print(f"   order in {name!r} is not the expected one; a reorder op will follow", file=sys.stderr)
            self.folder_entry(name)["apps"] = seen if len(seen) >= len(expect) else expect
        self.leave_edit()

    def take(self, page, apps):
        """Drop `apps` from `page` in the in-memory layout; their cells are
        free. A page left empty is removed, as the launcher removes it, and
        everything after it moves up one."""
        entries = self.live["pages"][page - 1]
        self.free[page] |= {tuple(it["at"]) for it in entries if it.get("app") in apps}
        entries[:] = [it for it in entries if it.get("app") not in apps]
        if not entries and page > 1:
            print(f"   page {page} is empty now; the launcher drops it, later pages move up", file=sys.stderr)
            del self.live["pages"][page - 1]
            self.free = {(k - 1 if k > page else k): v for k, v in self.free.items() if k != page}
            self.folder_at = {n: ((pg - 1 if pg > page else pg), at) for n, (pg, at) in self.folder_at.items()}

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

        def landed():
            s = self.dump()
            if self.page_no(s) != target:
                return False, (s, f"at page {self.page_no(s)}")
            missing = [a for a in apps if not self.find(s, a)]
            return not missing, (s, f"missing {missing}" if missing else "")
        ok, (scr, detail) = self.verified(landed, f"page {target} in edit mode showing {apps}",
                                          target_page=target, target_b=[tx - 130, ty - 130, tx + 130, ty + 130])
        if not ok:  # the cells are read from the screen below, so only an absent app is a failure
            die(f"after the drop on page {target}: {detail}")
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
        """Bring folder `name` to the wanted order. The live order is read
        from the popup first (the snapshot's can be stale). Then, walking the
        wanted order, every app found out of place is picked up and released
        on the app that should come after it: a release on an item inserts
        before it, which is the one drop the grid accepts everywhere. Both
        apps have to be on screen, which holds when the disorder sits within
        the popup's last rows, as an append-gone-wrong always does."""
        name, want = op["folder"], op["order"]
        fpage, fat = self.folder_at[name]
        entry = self.folder_entry(name)
        self.enter_edit(fpage)
        self.p.tap(*centre(EDIT, *fat), wait=0.9)
        ok, seen = self.verified(lambda: self.folder_contains(entry["apps"], tries=12),
                                 f"folder {name!r} open in edit mode with all {len(entry['apps'])} apps", target_page=fpage)
        if not ok:
            die(f"could not read all of folder {name!r}: {len(seen)} of {len(entry['apps'])}")
        entry["apps"] = seen
        # a title can occur twice (two "File Manager"s); the order is compared on first occurrences
        uniq = lambda xs: list(dict.fromkeys(xs))
        have = uniq(x for x in seen if x in want)
        expect = uniq(x for x in want if x in seen)
        moves = 0
        for j in range(len(expect)):
            if have[j] == expect[j]:
                continue
            app, before = expect[j], have[j]
            scr = self.dump()
            if not (self.popup_bounds(scr, app) and self.popup_bounds(scr, before)):
                self.popup_top()
                self.folder_contains([app, before], tries=12)
                scr = self.dump()
            if not (self.popup_bounds(scr, app) and self.popup_bounds(scr, before)):
                die(f"{app!r} and {before!r} are not on screen together in {name!r}; cannot reorder")
            self.move_before(app, before)
            have.remove(app); have.insert(j, app)
            moves += 1
        # verify on the popup's last screen alone: a single dump, no scroll
        # stitching (which can misplace a row in a long folder)
        self.folder_contains([expect[-1]], tries=12)
        bottom = uniq(x for x in self.popup_items(self.dump()) if x in want)
        if bottom != expect[-len(bottom):]:
            die(f"folder {name!r} ends with {bottom[-6:]}, wanted …{expect[-6:]}")
        for x in expect[-len(bottom):]:
            entry["apps"].remove(x)
        entry["apps"] += expect[-len(bottom):]
        print(f"   {name!r} in order after {moves} moves", file=sys.stderr)
        self.p.key("KEYCODE_BACK", 0.5)
        self.leave_edit()

    def popup_top(self):
        """Scroll the open popup back to its first row."""
        first = None
        for _ in range(12):
            scr = self.dump()
            items = self.popup_items(scr)
            if not items or items[0] == first:
                return
            first = items[0]
            full = [b for a, b in self.walk.popup(scr, set())[1] if b[0] in self.POP_COLS]
            top, bottom = min(b[1] for b in full), max(b[3] for b in full)
            row = min(b[3] - b[1] for b in full)
            cx, cy = scr.width // 2, (top + bottom) // 2
            self.p.swipe(cx, cy - row, cx, cy + row * 2, wait=0.6, ms=600)

    def reconcile(self, op) -> bool:
        """After adb broke mid-operation: did the gesture finish on the phone?
        For a folder drop, the apps gone from their page and present in the
        folder means yes; the layout is updated and the op counts as done."""
        if op["op"] != "into-folder":
            return False
        page, apps, name = op["page"], op["apps"], op["folder"]
        scr = self.go(page)
        if any(self.find(scr, a) for a in apps):
            return False
        fpage, fat = self.folder_at[name]
        self.enter_edit(fpage)
        self.p.tap(*centre(EDIT, *fat), wait=0.9)
        ok, seen = self.folder_contains(apps)
        self.p.key("KEYCODE_BACK", 0.5)
        self.leave_edit()
        if not ok:
            die(f"{apps} are neither on page {page} nor in {name!r}")
        self.take(page, apps)
        self.folder_entry(name)["apps"] += apps
        return True

    def move_before(self, app, target):
        """Inside the open popup (edit mode): pick `app` up and release it on
        `target`, which inserts it before that app. Both must be on screen."""
        scr = self.dump()
        a, t = self.popup_bounds(scr, app), self.popup_bounds(scr, target)
        if not a or not t:
            die(f"{app!r} or {target!r} is not on screen in the open folder")
        x, y = mid(a); tx, ty = mid(t)
        self.gesture(f"input motionevent DOWN {x} {y}; sleep 0.9; input motionevent MOVE {x + 20} {y}; sleep 0.3; "
                     f"input motionevent MOVE {tx} {ty}; sleep 1.0; input motionevent UP {tx} {ty}; echo moved")

    def out_of_folder(self, op):
        """In normal mode (experiment 7): open the folder, pick the app up
        inside the popup, drag it out past the popup's edge (the popup closes
        and the page shows in drag mode), drop it on a free cell of the page."""
        name, app, page = op["folder"], op["apps"][0], op["page"]
        cells = sorted(self.free[page], key=lambda cr: (cr[1], cr[0]))
        if not cells:
            die(f"page {page} has no free cell for {app!r}")
        self.leave_edit()
        scr = self.go(page)
        background = {tuple(n["b"]) for n in scr.launcher()}
        fb = self.folder_cell_bounds(scr, name if name.isascii() else "Folder")
        if not fb:
            die(f"folder {name!r} not found on page {page}")
        self.p.tap(*mid(fb), wait=0.9)
        b = None
        for _ in range(10):
            scr = self.dump()
            items = [(a, bb) for a, bb in self.walk.popup(scr, background)[1]]
            hit = [bb for a, bb in items if a == app]
            if hit:
                b = hit[0]
                break
            if not items:
                break
            full = [bb for _, bb in items]
            top, bottom = min(x[1] for x in full), max(x[3] for x in full)
            row = min(x[3] - x[1] for x in full)
            cx, cy = scr.width // 2, (top + bottom) // 2
            self.p.swipe(cx, cy + row, cx, cy - row, wait=0.6, ms=600)
        if not b:
            self.p.key("KEYCODE_BACK", 0.5)
            die(f"{app!r} is not in folder {name!r}")
        x, y = mid(b)
        tx, ty = centre(DRAG, *cells[0])
        script = (f"input motionevent DOWN {x} {y}; sleep 0.9; input motionevent MOVE {x + 20} {y}; sleep 0.3; "
                  f"input motionevent MOVE 40 {y}; sleep 0.9; input motionevent MOVE 40 {ty}; sleep 0.6; "
                  f"input motionevent MOVE {tx} {ty}; sleep 0.9; input motionevent UP {tx} {ty}; echo dropped")
        self.gesture(script)
        scr = self.dump()
        if self.walk.popup(scr, background)[1]:
            self.p.key("KEYCODE_BACK", 0.5)
            die(f"the folder popup is still open after the drag; {app!r} stayed in {name!r}")
        if not self.find(scr, app):
            die(f"{app!r} did not land on page {page}")
        entry = self.folder_entry(name); entry["apps"].remove(app)
        self.free[page].discard(cells[0])
        self.live["pages"][page - 1].append({"app": app, "at": list(cells[0])})

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
        t0, d0 = time.monotonic(), getattr(self, "dumps", 0)
        getattr(self, op["op"].replace("-", "_"))(op)
        self.walk.check_front(describe(op))
        self.gate(op)
        if self.p.mirror:
            shutil.copy(self.p.frame(f"op{i:02}"), os.path.join(LOG, f"op{i:02}.png"))
        print(f"   {self.dumps - d0} dumps, {time.monotonic() - t0:.0f} s", file=sys.stderr)


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


def starting_point():
    """The driver's record of the phone if a proposal is half applied, else the newest snapshot."""
    if os.path.exists(STATE) and os.path.getmtime(STATE) > os.path.getmtime(newest_snapshot()):
        return STATE
    return newest_snapshot()


def record(live, proposal, finished):
    """Keep the driver's model: in the history once the proposal is applied, else in the write log."""
    now = datetime.datetime.now()
    live = dict(live)
    live["date"], live["time"] = now.date().isoformat(), now.strftime("%H:%M")
    for pg in live["pages"]:
        pg.sort(key=lambda e: (e["at"][1], e["at"][0]))
    if finished:
        live["note"] = "applied: " + (proposal.get("note") or proposal.get("title") or "the proposal")
        path = os.path.join(ph.LAYOUTS, f"{now:%Y-%m-%d-%H%M}.json")
    else:
        live["note"] = "the driver's record between runs; not yet the applied proposal"
        path = STATE
    with open(path, "w") as fh:
        json.dump(live, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    if finished and os.path.exists(STATE):
        os.remove(STATE)
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
    ap.add_argument("--unlock", action="store_true", help="visit the launcher settings first to switch the layout lock off (default: assume it is off)")
    ap.add_argument("--lock", action="store_true", help="turn the layout lock back on at the end (default: leave it off)")
    args = ap.parse_args()

    with open(args.proposal) as fh:
        proposal = json.load(fh)
    serial = None if args.dry_run and not args.dump else ph.pick_serial(args.serial)
    if args.dump:
        live = fresh_dump(serial)
    else:
        with open(args.snapshot or starting_point()) as fh:
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
    phone.want_icons = False
    d = Driver(phone, live)
    if args.unlock:
        d.set_lock(False)
    done = 0
    try:
        for i, op in enumerate(ops[:args.max_ops], 1):
            print(f"-- {i}/{len(ops)} {describe(op)}", file=sys.stderr)
            for attempt in (1, 2):
                try:
                    d.run(i, op)
                    break
                except (ph.Dropped, ph.Failed) as e:
                    if attempt == 2:
                        raise
                    print(f"   adb broke mid-operation ({e}); waiting for the phone", file=sys.stderr)
                    for _ in range(3):
                        try:
                            phone.wait_back(240)
                            d.leave_edit()
                            settled = d.reconcile(op)
                            break
                        except (ph.Dropped, ph.Failed):
                            continue
                    else:
                        raise
                    if settled:
                        print("   the operation had completed on the phone; recorded", file=sys.stderr)
                        break
                    print("   retrying the operation", file=sys.stderr)
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
            record(d.live, proposal, finished=not plan(d.live, proposal))
    print(f"done: {done} of {len(ops)} operations", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

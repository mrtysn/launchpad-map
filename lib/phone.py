#!/usr/bin/env python3
"""Read an Android phone's home screen over adb and keep it as a layout snapshot.

The launcher's own database is private without root, so the layout is read the
way a person reads it: from the screen. For every home page this takes a
UI hierarchy dump (labels and bounds) and a screenshot (icons), and opens each
folder to read what is inside, paging through folders that hold more than one
screen. The only input sent to the phone is HOME, BACK, page swipes and taps on
folder icons; every tap is checked to have left the launcher in front.

Written for HyperOS's launcher (com.miui.home). The snapshot has the same shape
as a Launchpad one, plus the dock, widgets, and each item's grid cell:

    {"device": "...", "date": "...", "time": "...",
     "grid": {"cols": 4, "rows": 7, "folder": [3, 5]},
     "dock": ["Phone", ...],
     "pages": [[{"app": "Maps", "at": [0, 5]},
                {"folder": "office", "apps": [...], "at": [3, 6]},
                {"widget": "Digital clock", "at": [0, 2], "span": [2, 1]}]]}

Icons are cropped from the screenshots and kept, one per app title, in
icons.json next to the snapshots.
"""

import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import sys
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
import time
import xml.etree.ElementTree as ET

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Which Android device this run is about: `launchpad-map tablet ...` sets it.
# Each kind keeps its own snapshots, icons and write log.
KIND = os.environ.get("LAUNCHPAD_MAP_DEVICE", "phone")
if KIND not in ("phone", "tablet"):
    raise SystemExit(f"launchpad-map: LAUNCHPAD_MAP_DEVICE must be phone or tablet, not {KIND!r}")
LAYOUTS = os.path.join(REPO, "layouts", KIND)
# What differs between the phone (portrait, 4x7) and the tablet (landscape,
# 6x4), measured from each one's screenshots and UI dumps:
#   title   an open folder's title is wider than this share of the screen
#   folder  a folder screen's [cols, rows]; longer folders scroll
#   crop    the icon within its cell, as (size / cell width, top / cell
#           height) on a home page, in an open folder, and in the dock,
#           where it is centred
LOOK = {
    "phone": {"title": 0.5, "folder": [3, 5],
              "crop": {"cell": (0.59, 0.11), "folder": (0.59, 0.11), "dock": (0.66, None)}},
    "tablet": {"title": 0.35, "folder": [4, 3],
               "crop": {"cell": (0.434, 0.28), "folder": (0.5, 0.252), "dock": (0.84, None)}},
}[KIND]
ICONS = os.path.join(LAYOUTS, "icons.json")
# Pages read so far, so a failed walk resumes instead of starting over.
PROGRESS = os.path.join(LAYOUTS, ".progress")
# Screenshots of a walk in progress; icons are cropped from them once it ends.
SHOTS = os.path.join(LAYOUTS, ".shots")
LAUNCHER = "com.miui.home"
ICON_PX = 96
WINDOW_ID = os.path.join(os.path.dirname(os.path.abspath(__file__)), "window-id.swift")
MIRROR_TITLE = "launchpad-map"

# A badge is announced in the label: "Clock: 1 new notification". Anything else
# after a colon is part of the name ("Termux:Widget").
BADGE = re.compile(r"\s*:\s*\d+ new notifications?$")
# The tablet announces it in place of the name: "2 unread message from Google".
UNREAD = re.compile(r"^\d+ unread messages? from (.+)$")
PAGE_OF = re.compile(r"^Page (\d+)(?: of|\. Total pages:) (\d+)")  # phone; tablet


def die(msg: str):
    raise SystemExit(f"launchpad-map {KIND}: {msg}")


class Dropped(Exception):
    """The phone left adb mid-walk. Android switches wireless debugging off
    on every Wi-Fi network change (a band switch counts) and the keeper app
    on the phone turns it back on within seconds, on a new port; adb's own
    mDNS cache then keeps dialling the old port. `reconnect` follows the
    phone by its hardware serial instead."""


class Failed(Exception):
    """An adb command failed for another reason; a retry often works
    (uiautomator cannot always get an idle UI to dump)."""


DROP = re.compile(r"device offline|device '.*' not found|no devices|failed to connect|error: closed|protocol fault")
ADB_TIMEOUT = 150  # s; the longest gesture script (a folder append with 16 popup scrolls) stays under this
HW_SERIAL = re.compile(r"^adb-([A-Za-z0-9]+)-[^-]+\._adb-tls-connect")


class Phone:
    # Screenshots are taken to here on the phone, which is quick, and copied to
    # the Mac in the background while the walk moves on.
    REMOTE = "/data/local/tmp/launchpad-map"

    def __init__(self, serial: str, work: str, known=()):
        self.serial, self.work, self.shots = serial, work, 0
        self.run = datetime.datetime.now().strftime("%H%M%S")
        self.known = set(known)  # app titles whose icon we already have
        self.want_icons = True   # a dump crops icons; driving the screen needs none
        self.pulls = ThreadPoolExecutor(max_workers=2)
        self.adb("shell", "mkdir", "-p", self.REMOTE)
        self.serialno = self.adb("shell", "getprop", "ro.serialno").strip()
        size = re.findall(r"\d+", self.adb("shell", "wm", "size"))
        model = self.adb("shell", "getprop", "ro.product.model").strip()
        self.mirror = Mirror(int(size[0]), int(size[1]), model) if len(size) >= 2 else None
        if self.mirror and self.mirror.window:
            print("frames from the scrcpy mirror", file=sys.stderr)

    def frame(self, name) -> str:
        """A screenshot as a local PNG path: from the mirror when one is up,
        else screencap over adb."""
        path = os.path.join(self.work, f"{name}.png")
        if self.mirror and self.mirror.frame(path):
            return path
        self.adb("shell", f"mkdir -p {self.REMOTE} && screencap -p {self.REMOTE}/{name}.png")
        self.adb("pull", f"{self.REMOTE}/{name}.png", path)
        return path

    def adb(self, *args, binary=False, timeout=ADB_TIMEOUT):
        try:
            proc = subprocess.run(["adb", "-s", self.serial, *args], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise Dropped(f"adb {' '.join(args[:3])} gave no answer in {timeout}s")
        if proc.returncode != 0 and DROP.search(proc.stderr.decode(errors="replace")):
            raise Dropped(proc.stderr.decode(errors="replace").strip()[:120])
        if proc.returncode != 0:
            why = (proc.stderr + proc.stdout).decode(errors="replace").strip()
            raise Failed(f"adb {' '.join(args[:3])} failed: {why[:300]}")
        return proc.stdout if binary else proc.stdout.decode(errors="replace")

    # The UI dump waits for the screen to settle, so the pauses after input
    # only need to let an animation start.
    def key(self, name: str, wait=0.4):
        self.adb("shell", "input", "keyevent", name)
        time.sleep(wait)

    def swipe(self, x1, y1, x2, y2, wait=0.5, ms=250):
        self.adb("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))
        time.sleep(wait)

    def tap(self, x, y, wait=0.6):
        self.adb("shell", "input", "tap", str(x), str(y))
        time.sleep(wait)

    def wait_back(self, seconds=90):
        """Get this phone back after a drop: first whatever adb lists it as
        (mDNS name or ip:port), else by dialling its current port through
        `reconnect`, until `seconds` have passed."""
        print(f"phone dropped off adb; waiting up to {seconds}s", file=sys.stderr)
        hw = self.serialno or hardware_serial(self.serial)
        end = time.time() + seconds
        while time.time() < end:
            time.sleep(3)
            for s in connected():
                if s == self.serial or (hw and hw in s) or self.is_me(s):
                    self.serial = s
                    print(f"phone is back as {s}", file=sys.stderr)
                    return
            got = reconnect(hw)
            if got:
                self.serial = got
                print(f"phone reattached as {got}", file=sys.stderr)
                return
        die(f"the phone did not come back within {seconds}s")

    def is_me(self, serial) -> bool:
        if not self.serialno or serial.startswith("adb-"):
            return False  # an mDNS name carries the serial number itself
        out = subprocess.run(["adb", "-s", serial, "shell", "getprop", "ro.serialno"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() == self.serialno

    def front(self) -> str:
        """Every display's focused window: a mirror (scrcpy) adds a display
        whose focus is always null, so one line would be the wrong one."""
        return " ".join(self.adb("shell", "dumpsys window | grep mCurrentFocus").split())

    def screen(self):
        """The UI hierarchy, and a screenshot of the same moment when it shows
        an icon we have not got yet."""
        for attempt in range(3):
            try:
                out = self.adb("exec-out", "uiautomator", "dump", "/dev/tty")
                if "</hierarchy>" in out:
                    break
                raise Failed(f"uiautomator dump: {out.strip()[:200]}")
            except Failed as e:
                if attempt == 2:
                    die(str(e))
                time.sleep(1.5)
        xml = out[out.index("<"):out.index("</hierarchy>") + len("</hierarchy>")]
        name = f"{self.run}-{self.shots:03}"
        self.shots += 1
        with open(os.path.join(self.work, f"{name}.xml"), "w") as fh:
            fh.write(xml)
        scr = Screen(ET.fromstring(xml), None)
        if self.want_icons and any(n["label"] not in self.known and n["cls"] == "TextView" and n["b"][3] - n["b"][1] >= 100
               and n["b"][2] - n["b"][0] < scr.width * LOOK["title"]  # not a folder's title bar
               for n in scr.launcher()):
            scr.png = os.path.join(self.work, f"{name}.png")
            if not (self.mirror and self.mirror.frame(scr.png)):
                remote = f"{self.REMOTE}/{name}.png"
                self.adb("shell", f"mkdir -p {self.REMOTE} && screencap -p {remote}")
                scr.remote = remote
                scr.serial = self.serialno or self.serial
                self.pulls.submit(self.pull, remote, scr.png)
        return scr

    def pull(self, remote, local):
        """Copy a screenshot to the Mac; a failure is retried when cropping."""
        try:
            self.adb("pull", remote, local)
        except (Dropped, Failed, SystemExit):
            pass

    def finish(self):
        self.pulls.shutdown(wait=True)


class Mirror:
    """A scrcpy window of the phone on this Mac. A frame of it is a local
    screen capture (~0.2 s) instead of a 1.4 MB `screencap` over Wi-Fi (4-6 s),
    and the same picture. Any scrcpy window of this phone will do (the ADB
    Reconnect mirror button, or scrcpy by hand); a borderless one captures
    cleanest, since a title bar would be scaled into the frame.

    Window pixels are scaled back to phone pixels from the phone's own size."""

    def __init__(self, width, height, model=""):
        self.phone_w, self.phone_h = width, height
        self.model = model
        self.window = self.find()

    @staticmethod
    def helper():
        """window-id.swift compiled once (running it through `swift` costs
        seconds per call), cached next to the source and rebuilt when older."""
        exe = os.path.join(os.path.dirname(WINDOW_ID), ".window-id")
        if not os.path.exists(exe) or os.path.getmtime(exe) < os.path.getmtime(WINDOW_ID):
            subprocess.run(["swiftc", "-O", "-o", exe, WINDOW_ID], capture_output=True)
        return exe if os.path.exists(exe) else None

    def find(self):
        """scrcpy titles its window after the device model unless told
        otherwise; any scrcpy window will do when neither name matches."""
        exe = self.helper()
        if not exe:
            return None
        for title in (MIRROR_TITLE, self.model, ""):
            out = subprocess.run([exe, "scrcpy", title], capture_output=True, text=True)
            if out.returncode == 0:
                wid, x, y, w, h = (int(v) for v in out.stdout.split())
                return {"id": wid, "w": w, "h": h}
        return None

    def frame(self, path) -> bool:
        if not self.window:
            self.window = self.find()  # cheap once compiled: a mirror started mid-run is picked up
            if not self.window:
                return False
        r = subprocess.run(["screencapture", "-x", "-o", "-l", str(self.window["id"]), path], capture_output=True)
        if r.returncode != 0 or not os.path.exists(path):
            self.window = None
            return False
        # Retina doubles the pixels; scale the capture to the phone's own size
        # so every later crop can use phone coordinates unchanged.
        subprocess.run(["sips", "-z", str(self.phone_h), str(self.phone_w), path], capture_output=True)
        return True


class Screen:
    def __init__(self, root, png):
        self.png, self.remote = png, None
        self.nodes = []
        for n in root.iter("node"):
            b = [int(v) for v in re.findall(r"\d+", n.get("bounds", ""))]
            desc = n.get("content-desc") or n.get("text") or ""
            self.nodes.append({
                "cls": n.get("class", "").rsplit(".", 1)[-1],
                "pkg": n.get("package"),
                "label": UNREAD.sub(r"\1", BADGE.sub("", desc).strip()),
                "desc": desc,
                "b": b,
            })
        self.width = max((n["b"][2] for n in self.nodes if len(n["b"]) == 4), default=0)

    def launcher(self):
        return [n for n in self.nodes if n["pkg"] == LAUNCHER and len(n["b"]) == 4]

    def page(self):
        for n in self.nodes:
            m = PAGE_OF.match(n["desc"])
            if m:
                return int(m.group(1)), int(m.group(2)), n["b"]
        return None


def crop(spec) -> str:
    """Cut one icon out of its screenshot: {"png", "b" (cell bounds), "dock"}.
    Already-cropped icons (from an older progress file) pass through."""
    if spec is None or isinstance(spec, str):
        return spec
    if not os.path.exists(spec["png"]) and spec.get("remote"):
        # the phone that took the screenshot, whatever adb calls it now
        serial = spec.get("serial") or os.environ.get("ANDROID_SERIAL") or ""
        got = reconnect(hardware_serial(serial)) if serial and serial not in connected() else serial
        cmd = ["adb"] + (["-s", got] if got else []) + ["pull", spec["remote"], spec["png"]]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            pass
    b = spec["b"]
    where = spec.get("where") or ("dock" if spec.get("dock") else "cell")
    cw, ch = b[2] - b[0], b[3] - b[1]
    ratio, at = LOOK["crop"][where]
    size = round(cw * ratio)
    top = (ch - size) // 2 if at is None else round(ch * at)
    x = b[0] + (cw - size) // 2
    fd, out = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        subprocess.run(["sips", "-c", str(size), str(size), "--cropOffset", str(b[1] + top), str(x),
                        spec["png"], "--out", out], capture_output=True, check=True)
        subprocess.run(["sips", "-Z", str(ICON_PX), out], capture_output=True, check=True)
        with open(out, "rb") as fh:
            return "data:image/png;base64," + base64.b64encode(fh.read()).decode()
    finally:
        os.remove(out)


class Walk:
    def __init__(self, phone: Phone):
        self.phone = phone

    def icon(self, scr, b, label, dock=False, folder=False):
        """Where the icon is; the walk moves on and crops them all at the end.
        None when this screen needed no screenshot: the icon is already known."""
        if label in self.phone.known and not scr.png:
            return None
        self.phone.known.add(label)
        return {"png": scr.png, "remote": scr.remote, "serial": getattr(scr, "serial", ""), "b": list(b), "dock": dock,
                "where": "dock" if dock else "folder" if folder else "cell"}

    def grid(self, scr: Screen, indicator):
        """Cell size and origin, from the one-cell icons on screen."""
        cells = [n["b"] for n in scr.launcher() if n["cls"] == "TextView" and n["label"]
                 and n["b"][3] <= indicator[1] and n["b"][3] - n["b"][1] >= 100]  # not folder captions
        if not cells:
            die("no app icons on the first page to measure the grid from")
        cw = sorted(b[2] - b[0] for b in cells)[len(cells) // 2]
        ch = sorted(b[3] - b[1] for b in cells)[len(cells) // 2]
        # The grid is centred; its columns span the screen less the narrower
        # side margin (the tablet leaves wide ones, the phone almost none).
        margin = min(min(b[0] for b in cells), scr.width - max(b[2] for b in cells))
        cols, rows = round((scr.width - 2 * margin) / cw), indicator[1] // ch
        return {"cw": cw, "ch": ch, "cols": cols, "rows": rows,
                "ox": (scr.width - cols * cw) // 2, "oy": indicator[1] - rows * ch}

    def at(self, g, b):
        return [round((b[0] - g["ox"]) / g["cw"]), round((b[1] - g["oy"]) / g["ch"])]

    def span(self, g, b):
        return [max(1, round((b[2] - b[0]) / g["cw"])), max(1, round((b[3] - b[1]) / g["ch"]))]

    def one_cell(self, g, b):
        return abs((b[2] - b[0]) - g["cw"]) < g["cw"] * 0.2 and abs((b[3] - b[1]) - g["ch"]) < g["ch"] * 0.2

    def read_page(self, scr: Screen, g, indicator):
        nodes = scr.launcher()
        widgets, entries, folders, dock = [], [], [], []
        for n in nodes:
            b = n["b"]
            if b[1] >= indicator[3]:
                if n["cls"] == "TextView" and n["label"]:
                    dock.append((n["label"], self.icon(scr, b, n["label"], dock=True)))
                continue
            if b[3] > indicator[1] or any(inside(b, w) for w in widgets):
                continue
            big = not self.one_cell(g, b) and (b[2] - b[0]) >= g["cw"] * 0.9
            if n["cls"] == "AppWidgetHostView" or (n["cls"] == "RelativeLayout" and big and n["label"]):
                widgets.append(b)
                entries.append({"widget": n["label"] or "Widget", "at": self.at(g, b), "span": self.span(g, b)})
            elif n["cls"] == "RelativeLayout" and self.one_cell(g, b):
                folders.append(b)
            elif n["cls"] == "TextView" and n["label"] and self.one_cell(g, b):
                entries.append({"app": n["label"], "at": self.at(g, b), "_icon": self.icon(scr, b, n["label"])})
        return entries, folders, dock

    def check_front(self, what):
        """Stop at once if anything but the launcher came to the front.
        Mid-animation the focus reads as null; that is not another app."""
        for _ in range(5):
            front = self.phone.front()
            if LAUNCHER in front:
                return
            if "null" not in front:
                break
            time.sleep(0.4)
        if LAUNCHER not in front:
            self.phone.key("KEYCODE_HOME")
            die(f"{what} brought another app to the front ({front.strip()}); went HOME and stopped")

    def popup(self, scr: Screen, background):
        """An open folder's title and its (label, bounds) icons; nothing when closed."""
        title, items = "", []
        for n in scr.launcher():
            nb = tuple(n["b"])
            if not n["label"] or nb in background or n["cls"] not in ("TextView", "EditText"):
                continue
            if (nb[2] - nb[0]) > scr.width * LOOK["title"]:
                title = title or n["label"]
            elif (nb[3] - nb[1]) >= 100:  # shorter ones are captions under icons behind
                items.append((n["label"], nb))
        return title, items

    def read_folder(self, b, page_no, background):
        """Open the folder at `b`, read every screen of it, and close it."""
        p = self.phone
        p.tap((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
        self.check_front(f"tapping the folder at {b} on page {page_no}")
        title, apps = "", []
        for _ in range(20):  # folder screens; a folder longer than 3x5 scrolls vertically
            scr = p.screen()
            t, shown = self.popup(scr, background)
            if not shown and not apps:  # the tap did not open it (page still settling): once more
                time.sleep(1.0)
                p.tap((b[0] + b[2]) // 2, (b[1] + b[3]) // 2, wait=1.0)
                self.check_front(f"tapping the folder at {b} on page {page_no} again")
                scr = p.screen()
                t, shown = self.popup(scr, background)
                if not shown:
                    die(f"the folder at {b} on page {page_no} did not open; nothing recorded for it")
            title = title or t
            full = [x for x in shown if (x[1][3] - x[1][1]) >= 0.9 * max(y[1][3] - y[1][1] for y in shown)] if shown else []
            labels = [a for a, _ in apps]
            k = overlap(labels, [a for a, _ in full])
            fresh = full[k:]
            for label, nb in fresh:
                apps.append((label, self.icon(scr, nb, label, folder=True)))
            cols, rows = LOOK["folder"]
            if not fresh or (not labels and (len(full) < cols * rows or len(full) % cols)):
                break  # nothing new, or it all fits: fewer than a screenful, or a part-filled last row
            # Scroll by two rows with a steady drag through the middle of the
            # folder: a fling from near its edge closes the folder instead.
            left, right = min(x_[1][0] for x_ in full), max(x_[1][2] for x_ in full)
            top, bottom = min(x_[1][1] for x_ in full), max(x_[1][3] for x_ in full)
            row = min(x_[1][3] - x_[1][1] for x_ in full)
            cx, cy = (left + right) // 2, (top + bottom) // 2
            p.swipe(cx, cy + row, cx, cy - row, wait=0.6, ms=600)
            self.check_front("scrolling a folder")
        p.key("KEYCODE_BACK", 0.5)
        scr = p.screen()
        left = self.popup(scr, background)[1]
        if left:  # a tap outside the folder closes it too
            p.tap(scr.width // 2, max(x[1][3] for x in left) + 40)
            self.check_front("tapping outside a folder")
            scr = p.screen()
        pg = scr.page()
        if self.popup(scr, background)[1] or not pg or pg[0] != page_no:
            die(f"the folder at {b} on page {page_no} did not close cleanly (page {pg})")
        return title, apps


def folder_open(scr: Screen) -> bool:
    """An open folder shows its name as a title wider than any home-screen label."""
    return any(n["cls"] in ("TextView", "EditText") and n["label"]
               and (n["b"][2] - n["b"][0]) > scr.width * LOOK["title"] for n in scr.launcher())


def overlap(have, seen):
    """How many of `seen` repeat the tail of `have` (the rows a scroll kept)."""
    for k in range(min(len(have), len(seen)), 0, -1):
        if have[-k:] == seen[:k]:
            return k
    return 0


def inside(b, outer):
    return b[0] >= outer[0] and b[1] >= outer[1] and b[2] <= outer[2] and b[3] <= outer[3]


def connected():
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=20).stdout
    except subprocess.TimeoutExpired:
        return []
    return [l.split()[0] for l in out.splitlines()[1:] if l.strip().endswith("device")]


def hardware_serial(serial: str) -> str:
    """The phone's own serial number from an adb serial: an mDNS name carries
    it (adb-<serial>-<suffix>...), a bare serial is one, an ip:port is not."""
    m = HW_SERIAL.match(serial or "")
    if m:
        return m.group(1)
    return serial if serial and re.fullmatch(r"[A-Za-z0-9]{6,}", serial) else ""


def reconnect(hw: str) -> str:
    """Get adb attached to the phone with hardware serial `hw`, wherever it
    is now, and return the serial to pass to `adb -s`; "" when it cannot.
    Delegates to `adb-reconnect --serial` (the tools repo), which tries adb's
    mDNS list, then a fresh Bonjour lookup of the current port, then the
    device's saved address, and never restarts the adb server."""
    if not hw or not shutil.which("adb-reconnect"):
        return ""
    try:
        out = subprocess.run(["adb-reconnect", "--serial", hw], capture_output=True, text=True, timeout=40)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    got = out.stdout.strip().splitlines()
    return got[-1] if out.returncode == 0 and got else ""


def is_tablet(serial) -> bool:
    try:
        out = subprocess.run(["adb", "-s", serial, "shell", "getprop", "ro.build.characteristics"],
                             capture_output=True, text=True, timeout=10).stdout
    except subprocess.TimeoutExpired:
        return False
    return "tablet" in out.strip().split(",")


def of_kind(serials):
    """The connected devices of this run's KIND, one serial per device: adb
    can list the same phone twice, by ip:port and by its mDNS name."""
    seen, out = set(), []
    for s in serials:
        if is_tablet(s) != (KIND == "tablet"):
            continue
        try:
            hw = subprocess.run(["adb", "-s", s, "shell", "getprop", "ro.serialno"],
                                capture_output=True, text=True, timeout=10).stdout.strip()
        except subprocess.TimeoutExpired:
            continue
        if hw and hw not in seen:
            seen.add(hw)
            out.append(s)
    return out


def pick_serial(wanted):
    """The adb serial to drive: `wanted` when adb has it, else the same
    device wherever it is now; with nothing wanted, the one connected
    device of this run's KIND."""
    if wanted:
        if wanted in connected():
            return wanted
        got = reconnect(hardware_serial(wanted))
        if got:
            print(f"phone attached as {got}", file=sys.stderr)
            return got
        die(f"{wanted} is not connected and could not be reached")
    ready = connected()
    if len(ready) > 1:
        ready = of_kind(ready)
    if len(ready) != 1:
        die(f"expected exactly one connected {KIND}, found {len(ready)}; pass --serial")
    return ready[0]


def main() -> int:
    ap = argparse.ArgumentParser(prog=f"launchpad-map {KIND} dump", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", "-s", default=os.environ.get("ANDROID_SERIAL"),
                    help="adb serial (default: $ANDROID_SERIAL, else the one connected device)")
    ap.add_argument("--save", action="store_true", help=f"add the snapshot to layouts/{KIND}/")
    ap.add_argument("--out", help="write the snapshot JSON here instead of stdout")
    ap.add_argument("--note", help="note to store with the snapshot")
    ap.add_argument("--pages-only", action="store_true",
                    help="read the pages but not the folders: a folder's apps are taken from the newest "
                         "snapshot by its name (the same name, or the name with its emoji prefix stripped)")
    args = ap.parse_args()

    serial = pick_serial(args.serial)
    try:
        return dump_to(args, serial)
    except Failed as e:
        die(str(e))


def dump_to(args, serial) -> int:
    os.makedirs(SHOTS, exist_ok=True)
    phone = Phone(serial, SHOTS, known=load_icons())
    model = (phone.adb("shell", "getprop", "ro.product.marketname").strip()
             or phone.adb("shell", "getprop", "ro.product.model").strip())
    walk = Walk(phone)
    pages, dock, g = collect(walk, known_folders() if getattr(args, "pages_only", False) else None)
    phone.finish()

    specs = [u for _, u in dock]
    for page in pages:
        for e in page:
            if "_icon" in e:
                specs.append(e["_icon"])
            specs += [u for _, u in e.get("_icons", [])]
    print(f"cropping {len(specs)} icons", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=8) as pool:
        cropped = list(pool.map(crop, specs))
    uri = {id(spec): c for spec, c in zip(specs, cropped)}
    dock = [(label, uri[id(u)]) for label, u in dock]
    for page in pages:
        for e in page:
            if "_icon" in e:
                e["_icon"] = uri[id(e["_icon"])]
            if "_icons" in e:
                e["_icons"] = [(label, uri[id(u)]) for label, u in e["_icons"]]

    now = datetime.datetime.now()
    doc = {"device": model, "date": now.date().isoformat(), "time": now.strftime("%H:%M")}
    if args.note:
        doc["note"] = args.note
    doc["grid"] = {"cols": g["cols"], "rows": g["rows"], "folder": LOOK["folder"]}
    doc["dock"] = [label for label, _ in dock]
    icons = dict(load_icons())
    for label, uri in dock:
        if uri:
            icons[label] = uri
    for page in pages:
        for e in page:
            if "_icon" in e:
                uri = e.pop("_icon")
                if uri:
                    icons[e["app"]] = uri
            if "_icons" in e:
                for label, uri in e.pop("_icons"):
                    if uri:
                        icons[label] = uri
    doc["pages"] = pages
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"

    if args.save:
        os.makedirs(LAYOUTS, exist_ok=True)
        path = os.path.join(LAYOUTS, f"{now:%Y-%m-%d-%H%M}.json")
        with open(path, "w") as fh:
            fh.write(text)
        print(path)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    if not args.save and not args.out:
        sys.stdout.write(text)
    if args.save or args.out:
        os.makedirs(LAYOUTS, exist_ok=True)
        with open(ICONS, "w") as fh:
            json.dump(icons, fh, ensure_ascii=False)
        if os.path.exists(PROGRESS):
            os.remove(PROGRESS)
        shutil.rmtree(SHOTS, ignore_errors=True)
        try:
            phone.adb("shell", "rm", "-r", Phone.REMOTE)
        except (Dropped, Failed):
            pass
    return 0


def go_to(walk: Walk, target: int):
    """Close any folder, then reach home page `target`: read where we are once,
    send every swipe back to back, and read once more at the destination."""
    p = walk.phone
    # A walk cut off inside a folder leaves it open, and HOME does not close
    # it; BACK does, and does nothing on a bare home page.
    p.key("KEYCODE_BACK", 0.4)
    for _ in range(3):
        scr = p.screen()
        pg = scr.page()
        if not pg:
            p.key("KEYCODE_HOME", 0.6)
            walk.check_front("pressing HOME")
            continue
        if folder_open(scr):
            die("a folder is still open after BACK; stopped without tapping anything")
        if pg[0] == target:
            return scr, pg
        step = (scr.width - 180, 180) if pg[0] < target else (180, scr.width - 180)
        for _ in range(abs(target - pg[0])):
            p.swipe(step[0], 1200, step[1], 1200, wait=0.35)
        time.sleep(0.4)
        walk.check_front("turning the page")
    die(f"could not reach page {target}")


def known_folders() -> dict:
    """Folder name -> apps, from the newest snapshot, for a pages-only dump.
    Keyed by the name as it is and by its ASCII part ('🎮 logic' answers to
    'logic' and the other way round), so a rename by hand still matches."""
    names = sorted(n for n in os.listdir(LAYOUTS) if re.match(r"\d{4}-\d{2}-\d{2}-\d{4}\.json$", n))
    if not names:
        die(f"--pages-only needs an earlier snapshot in layouts/{KIND}/ to take the folders from")
    with open(os.path.join(LAYOUTS, names[-1])) as fh:
        doc = json.load(fh)
    out = {}
    for pg in doc["pages"]:
        for it in pg:
            if "folder" in it:
                out[it["folder"]] = it["apps"]
                out.setdefault("".join(ch for ch in it["folder"] if ch.isascii()).strip(), it["apps"])
    return out


def caption_of(scr: Screen, fb) -> str:
    """The name under a folder icon: the short text inside its cell."""
    for n in scr.launcher():
        if n["cls"] == "TextView" and n["label"] and n["b"][3] - n["b"][1] < 100 and inside(n["b"], fb):
            return n["label"]
    return ""


def collect(walk: Walk, folders_from: dict = None):
    """Walk every page; keep the icons on the entries for main() to file.
    With `folders_from` (name -> apps) no folder is opened: its caption is
    read from the page and its apps come from that map.

    A drop off adb re-reads the page it happened on, once the phone is back."""
    p, drops = walk.phone, 0
    pages, dock, g, total, indicator = [], None, None, None, None
    done = {}  # folders already read on the page in progress, by their bounds
    saved = load_progress()
    if saved:
        pages, dock, g, total, indicator = (saved[k] for k in ("pages", "dock", "grid", "total", "indicator"))
        done = {k: v for k, v in saved.get("folders", {}).items() if v[0] or v[1]}  # an empty read is no read
        print(f"resuming after page {len(pages)} of {total}"
              + (f" and {len(done)} folders of the next" if done else ""), file=sys.stderr)
    state = lambda: {"pages": pages, "dock": dock, "grid": g, "total": total,
                     "indicator": indicator, "folders": done}
    while total is None or len(pages) < total:
        page_no = len(pages) + 1
        try:
            scr, pg = go_to(walk, page_no)
            if g is None:
                total, indicator = pg[1], pg[2]
                g = walk.grid(scr, indicator)
            entries, folders, d = walk.read_page(scr, g, indicator)
            background = {tuple(n["b"]) for n in scr.launcher()}
            if dock is None:
                dock = d
            for fb in folders:
                if str(fb) not in done and folders_from is not None:
                    name = caption_of(scr, fb)
                    apps = folders_from.get(name) or folders_from.get("".join(ch for ch in name if ch.isascii()).strip())
                    if apps is None:
                        die(f"folder {name!r} on page {page_no} is not in the newest snapshot; run a full dump")
                    done[str(fb)] = (name, [(a, None) for a in apps])
                elif str(fb) not in done:
                    done[str(fb)] = walk.read_folder(fb, page_no, background)
                    save_progress(state())
                title, apps = done[str(fb)]
                entries.append({"folder": title, "apps": [a for a, _ in apps], "at": walk.at(g, fb),
                                "_icons": apps})
        except Dropped:
            drops += 1
            if drops > 3:
                die("the phone dropped off adb more than 3 times")
            p.wait_back()
            continue
        entries.sort(key=lambda e: (e["at"][1], e["at"][0]))
        pages.append(entries)
        done = {}
        save_progress(state())
        print(f"page {page_no} of {total}: {len(entries)} items", file=sys.stderr)
    p.key("KEYCODE_HOME")
    return pages, dock or [], g


def load_progress():
    if not os.path.exists(PROGRESS) or time.time() - os.path.getmtime(PROGRESS) > 3600:
        return None
    with open(PROGRESS) as fh:
        return json.load(fh)


def save_progress(state):
    os.makedirs(LAYOUTS, exist_ok=True)
    with open(PROGRESS, "w") as fh:
        json.dump(state, fh, ensure_ascii=False)


def load_icons() -> dict:
    if not os.path.exists(ICONS):
        return {}
    with open(ICONS) as fh:
        return json.load(fh)


if __name__ == "__main__":
    sys.exit(main())

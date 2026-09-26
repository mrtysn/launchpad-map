#!/usr/bin/env python3
"""Whether the phone (or tablet) can be scanned right now, as one JSON line.

    {"kind": "tablet", "attached": true, "serial": "...", "model": "Redmi Pad 2",
     "awake": true, "locked": false, "front": "com.miui.home", "ready": true}

A scan needs the device attached, its screen on and unlocked: it presses HOME
and reads the screen, which a sleeping or locked device does not show. Sends no
input and never reconnects; `adb-reconnect` keeps the device attached.
"""

import json
import re
import subprocess
import sys

import phone as ph


def shell(serial, cmd) -> str:
    try:
        return subprocess.run(["adb", "-s", serial, "shell", cmd], capture_output=True, text=True, timeout=8).stdout
    except subprocess.TimeoutExpired:
        return ""


def status() -> dict:
    out = {"kind": ph.KIND, "attached": False, "ready": False}
    ready = ph.connected()
    if len(ready) > 1:
        ready = ph.of_kind(ready)
    elif ready and ph.is_tablet(ready[0]) != (ph.KIND == "tablet"):
        ready = []
    if len(ready) != 1:
        return out
    serial = ready[0]
    power = shell(serial, "dumpsys power | grep mWakefulness=")
    window = shell(serial, "dumpsys window | grep -E 'isKeyguardShowing=|mCurrentFocus=' | head -2")
    if not power:
        return out  # listed by adb but not answering: about to drop
    front = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([^/}\s]+)", window)
    out.update({
        "attached": True,
        "serial": serial,
        "model": shell(serial, "getprop ro.product.marketname").strip() or shell(serial, "getprop ro.product.model").strip(),
        "awake": "mWakefulness=Awake" in power,
        "locked": "isKeyguardShowing=true" in window,
        "front": front.group(1) if front else "",
    })
    out["ready"] = out["awake"] and not out["locked"]
    return out


if __name__ == "__main__":
    print(json.dumps(status()))
    sys.exit(0)

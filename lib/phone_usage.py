#!/usr/bin/env python3
"""How much each app on the phone's home screen is used.

Android's usage-stats service keeps, per package, the foreground time, the
last use and the launch count over the last day, week, month and year, and
`dumpsys usagestats` prints all of it without root. The service knows
packages; the home screen shows titles; nothing on the phone maps one to
the other without reading the APKs, so the two are matched by name: a
title's letters against the package's segments (`com.agileworks.chordwise`
is ChordWise), and layouts/phone/packages.json, {title: package}, settles
the rest by hand. Whatever still does not match is listed at the end.

Output: one row per app in the newest snapshot, or `--json` for the same as
a dict keyed by title, for a proposal to read.
"""

import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phone as ph  # noqa: E402

PERIODS = ("daily", "weekly", "monthly", "yearly")
PACKAGES = os.path.join(ph.LAYOUTS, "packages.json")
ROW = re.compile(r'package=(\S+) totalTimeUsed="([^"]*)" lastTimeUsed="([^"]*)".*?appLaunchCount=(\d+)')


def seconds(hms: str) -> int:
    parts = [int(p) for p in hms.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def parse(text: str) -> dict:
    """{period: {package: {"secs", "last", "launches"}}} from `dumpsys usagestats`."""
    stats, period = {}, None
    for line in text.splitlines():
        m = re.match(r"\s*In-memory (\w+) stats", line)
        if m:
            period = m.group(1) if m.group(1) in PERIODS else None
            continue
        m = ROW.search(line)
        if m and period:
            pkg, used, last, launches = m.groups()
            cur = stats.setdefault(period, {}).setdefault(pkg, {"secs": 0, "last": "", "launches": 0})
            cur["secs"] += seconds(used)
            cur["launches"] += int(launches)
            if not last.startswith("1970") and last > cur["last"]:
                cur["last"] = last
    return stats


def letters(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def match(titles, packages) -> dict:
    """title -> package, by the title's letters against the package's tail."""
    out, taken = {}, set()
    by_tail = {}
    for p in packages:
        for seg in p.split(".")[1:]:
            by_tail.setdefault(letters(seg), []).append(p)
        by_tail.setdefault(letters("".join(p.split(".")[1:])), []).append(p)
    for t in sorted(titles, key=len, reverse=True):
        key = letters(re.sub(r"[-:(].*$", "", t))  # "LMR - Copyleft Music" -> "lmr"
        for cand in (letters(t), key):
            hits = [p for p in by_tail.get(cand, []) if p not in taken]
            if len(cand) >= 3 and hits:
                out[t] = hits[0]
                taken.add(hits[0])
                break
    return out


def titles_in(doc) -> list:
    seen = []
    for pg in doc["pages"]:
        for it in pg:
            if "app" in it:
                seen.append(it["app"])
            elif "folder" in it:
                seen += it["apps"]
    return list(dict.fromkeys(seen))


def main() -> int:
    ap = argparse.ArgumentParser(prog="launchpad-map phone usage", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", "-s", default=os.environ.get("ANDROID_SERIAL"))
    ap.add_argument("--from", dest="snapshot", help="snapshot whose titles to report (default: the newest)")
    ap.add_argument("--dumpsys", help="a saved `dumpsys usagestats` to read instead of the phone")
    ap.add_argument("--json", action="store_true", help="print JSON keyed by title")
    ap.add_argument("--save", action="store_true", help="also write layouts/phone/usage.json, which "
                    "`phone history` shows on the page as the Usage view")
    args = ap.parse_args()

    if args.dumpsys:
        with open(args.dumpsys) as fh:
            text = fh.read()
    else:
        text = subprocess.run(["adb", "-s", ph.pick_serial(args.serial), "shell", "dumpsys", "usagestats"],
                              capture_output=True, text=True, check=True, timeout=60).stdout
    stats = parse(text)
    if not stats:
        raise SystemExit("launchpad-map phone usage: no per-package stats in the dump")

    snap = args.snapshot
    if not snap:
        names = sorted(n for n in os.listdir(ph.LAYOUTS) if re.match(r"\d{4}-\d{2}-\d{2}-\d{4}\.json$", n))
        if not names:
            raise SystemExit("launchpad-map phone usage: no snapshot in layouts/phone/")
        snap = os.path.join(ph.LAYOUTS, names[-1])
    with open(snap) as fh:
        titles = titles_in(json.load(fh))
    packages = set().union(*(set(v) for v in stats.values()))
    pkg_of = {}
    if os.path.exists(PACKAGES):
        with open(PACKAGES) as fh:
            pkg_of = {t: p for t, p in json.load(fh).items() if p in packages}
    pkg_of.update(match([t for t in titles if t not in pkg_of], packages - set(pkg_of.values())))

    rows = {}
    for t in titles:
        p = pkg_of.get(t)
        row = {"package": p}
        for per in PERIODS:
            s = stats.get(per, {}).get(p, {}) if p else {}
            row[per] = {"minutes": round(s.get("secs", 0) / 60), "launches": s.get("launches", 0)}
        row["last"] = max((stats[per][p]["last"] for per in PERIODS if p and p in stats.get(per, {})), default="")
        rows[t] = row

    if args.save:
        with open(os.path.join(ph.LAYOUTS, "usage.json"), "w") as fh:
            json.dump(rows, fh, indent=1, ensure_ascii=False)
    if args.json:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return 0
    print(f"{'app':32} {'month min':>9} {'launches':>8} {'year min':>8} {'launches':>8}  last used")
    for t, r in sorted(rows.items(), key=lambda kv: -kv[1]["yearly"]["minutes"]):
        if not r["package"]:
            continue
        print(f"{t[:32]:32} {r['monthly']['minutes']:9} {r['monthly']['launches']:8} "
              f"{r['yearly']['minutes']:8} {r['yearly']['launches']:8}  {r['last'][:10]}")
    missing = [t for t, r in rows.items() if not r["package"]]
    print(f"\n{len(rows) - len(missing)} of {len(rows)} apps matched a package; not matched: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

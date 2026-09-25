#!/usr/bin/env python3
"""Charts of the phone's app usage, as inline SVG for the history page.

`charts()` takes the rows `phone usage --json` produces and a snapshot for the
folder each app is in, and draws: the most-used apps this year and this
month, minutes per folder, how each folder's apps split into never / rarely /
often launched, and when apps were last opened. `render.py` embeds the
fragment as the history page's Usage view; `page()` wraps it as a page of its
own for a quick look.
"""

import argparse
import datetime
import html
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phone as ph  # noqa: E402

# Styles the charts need wherever they are embedded; scoped to .usage.
CHART_CSS = """
.usage { --bar:#5b8def; --bar2:#f2a93b; --never:#e05a5a; --rare:#f2a93b; --often:#4fbf7a; }
.usage h2 { font-size: 16px; margin: 36px 0 10px; }
.usage p.sub { color: var(--faint); margin: 0 0 8px; }
.usage svg { display:block; width:100%; height:auto; font-size: 12px; }
.usage svg text { fill: var(--ink); } .usage svg text.faint { fill: var(--faint); font-size: 11px; }
.usage .legend span { display:inline-block; margin-right:14px; color:var(--faint); }
.usage .legend i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:5px; vertical-align:-1px; }
.usage .two { display:grid; grid-template-columns: 1fr 1fr; gap: 24px; } @media (max-width: 800px) { .usage .two { grid-template-columns: 1fr; } }
"""
CSS = """
:root { --bg:#141b2b; --ink:#e8ecf4; --faint:#8b94a8; }
@media (prefers-color-scheme: light) { :root:not([data-theme="dark"]) { --bg:#f6f7fa; --ink:#1a2030; --faint:#5c6577; } }
body { margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--ink); font:14px/1.45 -apple-system, "Helvetica Neue", sans-serif; }
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
""" + CHART_CSS


def esc(s):
    return html.escape(str(s), quote=True)


def hbar_chart(rows, value, label, unit="", colour="var(--bar)", width=1100, row_h=20, left=230):
    """rows: [(name, value, note)]; horizontal bars, longest first."""
    n = len(rows)
    if not n:
        return "<p class=sub>nothing to show</p>"
    top = max(v for _, v, _ in rows) or 1
    h = n * row_h + 10
    out = [f'<svg viewBox="0 0 {width} {h}" role="img" aria-label="{esc(label)}">']
    for i, (name, v, note) in enumerate(rows):
        y = 5 + i * row_h
        w = (width - left - 220) * v / top
        out.append(f'<text x="{left - 8}" y="{y + 14}" text-anchor="end">{esc(name[:34])}</text>')
        out.append(f'<rect x="{left}" y="{y + 3}" width="{w:.1f}" height="{row_h - 6}" rx="3" fill="{colour}"/>')
        out.append(f'<text class="faint" x="{left + w + 6:.1f}" y="{y + 14}">{value(v)}{unit}{("  " + esc(note)) if note else ""}</text>')
    out.append("</svg>")
    return "\n".join(out)


def stacked_chart(rows, width=1100, row_h=22, left=200):
    """rows: [(folder, never, rare, often, unknown)] as counts."""
    h = len(rows) * row_h + 10
    out = [f'<svg viewBox="0 0 {width} {h}" role="img" aria-label="usage buckets per category">']
    top = max(sum(r[1:]) for r in rows) or 1
    scale = (width - left - 80) / top
    cols = ["var(--never)", "var(--rare)", "var(--often)", "#8b94a8"]
    for i, (name, *parts) in enumerate(rows):
        y = 5 + i * row_h
        out.append(f'<text x="{left - 8}" y="{y + 15}" text-anchor="end">{esc(name[:26])}</text>')
        x = left
        for c, v in zip(cols, parts):
            if v:
                w = v * scale
                out.append(f'<rect x="{x:.1f}" y="{y + 3}" width="{w:.1f}" height="{row_h - 6}" fill="{c}"/>')
                if w > 16:
                    out.append(f'<text x="{x + w / 2:.1f}" y="{y + 15}" text-anchor="middle" style="fill:#fff;font-size:11px">{v}</text>')
                x += w
        out.append(f'<text class="faint" x="{x + 6:.1f}" y="{y + 15}">{sum(parts)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def vbar_chart(cols, label, width=1100, height=220, colour="var(--bar)"):
    """cols: [(label, value)] left to right."""
    n = len(cols)
    top = max(v for _, v in cols) or 1
    bw = (width - 40) / n
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{esc(label)}">']
    for i, (lab, v) in enumerate(cols):
        h = (height - 50) * v / top
        x = 20 + i * bw
        out.append(f'<rect x="{x + 3:.1f}" y="{height - 30 - h:.1f}" width="{bw - 6:.1f}" height="{h:.1f}" rx="3" fill="{colour}"/>')
        out.append(f'<text x="{x + bw / 2:.1f}" y="{height - 34 - h:.1f}" text-anchor="middle" class="faint">{v}</text>')
        out.append(f'<text x="{x + bw / 2:.1f}" y="{height - 12}" text-anchor="middle" class="faint">{esc(lab)}</text>')
    out.append("</svg>")
    return "\n".join(out)


CATEGORIES = os.path.join(ph.LAYOUTS, "categories.json")


def folder_of(doc):
    where = {}
    for pg in doc["pages"]:
        for it in pg:
            if "folder" in it:
                for a in it["apps"]:
                    where.setdefault(a, it["folder"] or "the unnamed folder")
            elif "app" in it:
                where.setdefault(it["app"], "loose")
    for a in doc.get("dock", []):
        where.setdefault(a, "dock")
    return where


def category_of(doc):
    """An app's category: its named folder (every '🎮 …' folder is 🎮), else
    the hand map in layouts/phone/categories.json, else 'other'."""
    by_hand = {}
    if os.path.exists(CATEGORIES):
        with open(CATEGORIES) as fh:
            by_hand = {a: c for a, c in json.load(fh).items() if not a.startswith("_")}
    cat = {}
    for a, f in folder_of(doc).items():
        if f in ("loose", "dock", "the unnamed folder"):
            cat[a] = by_hand.get(a, "other")
        else:
            cat[a] = "🎮" if f.startswith("🎮") else f
    return cat


def bucket(r):
    if not r["package"]:
        return 3
    n = r["yearly"]["launches"]
    return 0 if n == 0 else 1 if n < 12 else 2


def charts(rows, doc, when) -> str:
    """The charts as an HTML fragment (headings, legend and SVGs), for a
    container with class "usage"."""
    where = folder_of(doc)
    known = {t: r for t, r in rows.items() if r["package"]}
    fmt_min = lambda v: f"{v / 60:.1f} h" if v >= 120 else f"{v} min"

    year = sorted(known.items(), key=lambda kv: -kv[1]["yearly"]["minutes"])[:40]
    year_rows = [(t, r["yearly"]["minutes"], f'on screen · {r["yearly"]["launches"]} launches · in {where.get(t, "?")}') for t, r in year]
    month = sorted(known.items(), key=lambda kv: -kv[1]["monthly"]["minutes"])[:25]
    month_rows = [(t, r["monthly"]["minutes"], f'on screen · {r["monthly"]["launches"]} launches') for t, r in month
                  if r["monthly"]["minutes"]]
    launches = sorted(known.items(), key=lambda kv: -kv[1]["yearly"]["launches"])[:25]
    launch_rows = [(t, r["yearly"]["launches"], f'launches · {fmt_min(r["yearly"]["minutes"])} on screen') for t, r in launches]

    per_folder, cats = {}, category_of(doc)
    for t, r in rows.items():
        f = cats.get(t, "other")
        d = per_folder.setdefault(f, {"min": 0, "apps": 0, "b": [0, 0, 0, 0]})
        d["apps"] += 1
        d["min"] += r["yearly"]["minutes"]
        d["b"][bucket(r)] += 1
    fold = sorted(per_folder.items(), key=lambda kv: -kv[1]["min"])
    fold_rows = [(f, d["min"], f'· {d["apps"]} apps') for f, d in fold]
    stack_rows = [(f, *d["b"]) for f, d in sorted(per_folder.items(), key=lambda kv: -kv[1]["apps"])]

    months = []
    m0 = when.replace(day=1)
    for i in range(11, -1, -1):
        y, m = divmod(m0.month - 1 - i, 12)
        months.append(((m0.year + y, m + 1), datetime.date(m0.year + y, m + 1, 1).strftime("%b %y")))
    last_count = {k: 0 for k, _ in months}
    never, older = 0, 0
    for t, r in known.items():
        if not r["last"]:
            never += 1
            continue
        y, m = int(r["last"][:4]), int(r["last"][5:7])
        if (y, m) in last_count:
            last_count[(y, m)] += 1
        else:
            older += 1
    last_cols = [("older", older)] + [(lab, last_count[k]) for k, lab in months] + [("never", never)]

    total = len(rows)
    unknown = total - len(known)
    b = [0, 0, 0, 0]
    for r in rows.values():
        b[bucket(r)] += 1
    legend = ('<p class="legend"><span><i style="background:var(--never)"></i>never launched this year</span>'
              '<span><i style="background:var(--rare)"></i>under 12 launches</span>'
              '<span><i style="background:var(--often)"></i>12 or more</span>'
              '<span><i style="background:#8b94a8"></i>package unknown</span></p>')
    return f"""<p class="sub">Android usage stats read on {when:%d %b %Y}; the year window is the last 12 months. {total} apps on the home screen:
{b[2]} launched 12 times or more this year, {b[1]} fewer, {b[0]} never, {unknown} with no package match (unknown, not zero).</p>

<h2>Most used this year</h2>
<p class="sub">Bar: hours on screen in the last 12 months. Then the number of launches, and the folder it is in now.</p>
{hbar_chart(year_rows, fmt_min, "minutes this year")}

<div class="two"><div>
<h2>Most used this month</h2>
<p class="sub">Bar: hours on screen since 1 September.</p>
{hbar_chart(month_rows, fmt_min, "minutes this month", colour="var(--bar2)", width=540, left=170)}
</div><div>
<h2>Opened most often this year</h2>
<p class="sub">Bar: launches in the last 12 months.</p>
{hbar_chart(launch_rows, lambda v: f"{v}×", "launches this year", width=540, left=170)}
</div></div>

<h2>Time per category this year</h2>
<p class="sub">Hours on screen summed over a category's apps. A category is the app's folder (the 🎮 sub-folders count as 🎮); apps outside folders are categorised in layouts/phone/categories.json.</p>
{hbar_chart(fold_rows, fmt_min, "minutes per category")}

<h2>How each category's apps are used</h2>
{legend}
{stacked_chart(stack_rows)}

<h2>When apps were last opened</h2>
<p class="sub">Apps with a known package, by the month of their last use.</p>
{vbar_chart(last_cols, "last use by month")}
"""


def page(rows, doc, when) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Phone usage</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>{CSS}</style></head>
<body><main class="usage"><h1>Phone usage</h1>
{charts(rows, doc, when)}
</main></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(prog="launchpad-map phone usage --html", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("usage", help="usage JSON (`phone usage --json`)")
    ap.add_argument("--from", dest="snapshot", help="snapshot for folder membership (default: the newest)")
    ap.add_argument("--out", "-o", required=True, help="HTML file to write")
    ap.add_argument("--date", help="date of the usage read, YYYY-MM-DD (default: today)")
    args = ap.parse_args()
    with open(args.usage) as fh:
        rows = json.load(fh)
    snap = args.snapshot
    if not snap:
        names = sorted(n for n in os.listdir(ph.LAYOUTS) if re.match(r"\d{4}-\d{2}-\d{2}-\d{4}\.json$", n))
        snap = os.path.join(ph.LAYOUTS, names[-1])
    with open(snap) as fh:
        doc = json.load(fh)
    when = datetime.date.fromisoformat(args.date) if args.date else datetime.date.today()
    with open(args.out, "w") as fh:
        fh.write(page(rows, doc, when))
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

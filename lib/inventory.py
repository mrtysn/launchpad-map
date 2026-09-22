#!/usr/bin/env python3
"""Compare an installed-apps inventory (Mac App Store / Homebrew casks / /Applications)
against what is installed on the machine running this script, and write a self-contained
HTML page marking each entry as present on both, missing here, or new here.

Icons are pulled from the installed app bundles by resolving each bundle's
CFBundleIdentifier out of its Info.plist and feeding it to the sibling
`icon-export.swift` helper (NSWorkspace, so asset-catalog icons work too),
then inlined as data URIs. Entries with no local bundle — anything the
inventory has that this machine lacks — fall back to a coloured monogram.

The inventory is the markdown produced during a machine offboarding, with three sections:

    ## Mac App Store (mas list) ...
    1234567890  App Name    (1.2.3)
    ## Homebrew casks (brew list --cask) ...
    cask-one cask-two ...
    ## /Applications (full)
    Some App.app

Local state is collected at run time — nothing about this machine is baked in.
"""
import argparse
import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

STATE_LABEL = {"both": "on both", "remote-only": "not here", "local-only": "new here"}
LIB = Path(__file__).resolve().parent
SWIFT = LIB / "icon-export.swift"


def key(name: str) -> str:
    """Collapse a display name, cask token or bundle name to a comparable key."""
    return re.sub(r"[^a-z0-9]", "", name.removesuffix(".app").lower())


# --- inputs -----------------------------------------------------------------
def parse_inventory(md: str) -> tuple[list[dict], list[str], list[str]]:
    def section(header: str) -> str:
        m = re.search(rf"^## {re.escape(header)}.*?$(.*?)(?=^## |\Z)", md, re.S | re.M)
        return m.group(1).strip() if m else ""

    mas = []
    for line in section("Mac App Store (mas list)").splitlines():
        m = re.match(r"\s*(\d+)\s+(.+?)\s{2,}\((.+)\)\s*$", line)
        if m:
            mas.append({"id": m.group(1), "name": m.group(2).strip(), "version": m.group(3)})

    casks = sorted(set(section("Homebrew casks (brew list --cask)").split()))
    apps = [l.strip() for l in section("/Applications (full)").splitlines() if l.strip()]
    return mas, casks, apps


def run(cmd: list[str]) -> list[str]:
    """Run a listing command; return [] if the tool is absent or fails."""
    if not shutil.which(cmd[0]):
        return []
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except subprocess.SubprocessError:
        return []
    return [l.strip() for l in out.stdout.splitlines() if l.strip()]


def collect_local() -> tuple[dict, set[str], dict[str, Path]]:
    """Return (mas by id, cask tokens, {bundle name: path}) for this machine."""
    bundles: dict[str, Path] = {}
    for d in (Path("/Applications"), Path.home() / "Applications"):
        if d.is_dir():
            for p in sorted(d.iterdir()):
                if p.suffix == ".app":
                    bundles.setdefault(p.name, p)

    mas = {}
    for line in run(["mas", "list"]):
        m = re.match(r"\s*(\d+)\s+(.+?)\s{2,}\((.+)\)\s*$", line)
        if m:
            mas[m.group(1)] = {"name": m.group(2).strip(), "version": m.group(3)}
    return mas, set(run(["brew", "list", "--cask"])), bundles


def cask_metadata(tokens: set[str]) -> dict[str, dict]:
    """{token: {name, apps, homepage}} from brew's own metadata.

    Name-based guessing cannot connect cask `github` to `GitHub Desktop.app` or
    `foxitreader` to `Foxit PDF Reader.app`; brew knows the real artifact names.
    Unknown tokens (third-party taps that are gone) are simply skipped — a chunk
    containing one errors out wholesale, so failed chunks are retried per token.
    """
    if not tokens or not shutil.which("brew"):
        return {}

    def query(batch: list[str]) -> list[dict]:
        try:
            r = subprocess.run(["brew", "info", "--json=v2", "--cask", *batch],
                               capture_output=True, text=True, timeout=180, check=False)
            return json.loads(r.stdout)["casks"] if r.stdout.strip() else []
        except (subprocess.SubprocessError, ValueError, KeyError):
            return []

    meta: dict[str, dict] = {}
    ordered = sorted(tokens)
    for i in range(0, len(ordered), 25):
        chunk = ordered[i:i + 25]
        found = query(chunk)
        if not found and len(chunk) > 1:  # one bad token poisons the whole chunk
            found = [c for t in chunk for c in query([t])]
        for c in found:
            meta[c["token"]] = {
                "name": (c.get("name") or [None])[0],
                "apps": [a for art in c.get("artifacts", []) if isinstance(art, dict)
                         for a in art.get("app", []) if isinstance(a, str)],
                "homepage": c.get("homepage"),
            }
    return meta


# --- icons ------------------------------------------------------------------
def bundle_id(path: Path) -> str | None:
    """Read CFBundleIdentifier straight out of the bundle's Info.plist.

    Goes through `plutil` rather than `plistlib` directly: some bundles ship
    an old-style ASCII plist that is neither XML nor binary, which plistlib
    cannot parse but `plutil` (and CFBundle itself) reads fine.
    """
    plist_path = path / "Contents" / "Info.plist"
    if not plist_path.is_file():
        return None
    try:
        out = subprocess.run(
            ["plutil", "-extract", "CFBundleIdentifier", "raw", "-o", "-", str(plist_path)],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except subprocess.SubprocessError:
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def extract_icons(bundles: dict[str, Path], px: int) -> dict[str, str]:
    """Resolve each bundle to a bundle id, feed lib/icon-export.swift, and
    return {key(name): data URI} for whatever it could render.
    """
    if not bundles or not SWIFT.is_file() or not shutil.which("swift"):
        return {}

    # bundle name -> bundle id (or, failing that, a placeholder that will
    # never match a real LaunchServices id, so icon-export.swift falls
    # through to its own by-name filesystem search). Some real-world bundles
    # have no CFBundleIdentifier at all — Platypus-style launcher wrappers,
    # or a Catalyst app whose real bundle sits one level down in Wrapper/ —
    # and the by-name search still finds their icon, exactly as the original
    # app-icons.swift did by going straight to NSWorkspace on the path.
    ids: dict[str, str] = {
        name: bundle_id(path) or f"unresolved.{key(name)}"
        for name, path in bundles.items()
    }

    env = dict(os.environ, ICON_PX=str(px))
    # icon-export.swift's by-name fallback appends ".app" itself, so the name
    # sent over the wire has to be the bare app name, not the bundle filename.
    pairs = sorted({(bid, name.removesuffix(".app")) for name, bid in ids.items()})
    try:
        proc = subprocess.run(
            ["swift", str(SWIFT)],
            input="\n".join(f"{bid}\t{name}" for bid, name in pairs),
            capture_output=True, text=True, env=env, timeout=600, check=False,
        )
    except subprocess.SubprocessError:
        return {}
    if proc.returncode != 0:
        return {}

    by_bid: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "\t" not in line:
            continue
        bid, payload = line.split("\t", 1)
        if payload != "MISS":
            by_bid[bid] = "data:image/png;base64," + payload

    return {key(name): by_bid[bid] for name, bid in ids.items() if bid in by_bid}


# --- rows -------------------------------------------------------------------
def build_rows(inventory, local, icons, casks_meta) -> list[dict]:
    inv_mas, inv_casks, inv_apps = inventory
    local_mas, local_casks, local_bundles = local
    local_keys = {key(n) for n in local_bundles}

    # Cross-source lookups so a bare /Applications bundle can inherit the store page
    # or homepage of the App Store entry / cask it actually came from.
    mas_id_by_key = {key(e["name"]): e["id"] for e in inv_mas}
    mas_id_by_key |= {key(e["name"]): mid for mid, e in local_mas.items()}
    cask_by_key = {key(c): c for c in set(inv_casks) | local_casks}
    for token, m in casks_meta.items():  # brew's real artifact + display names
        for alias in m["apps"] + ([m["name"]] if m["name"] else []):
            cask_by_key.setdefault(key(alias), token)

    def link_for(kind: str, name: str, mas_id: str | None) -> tuple[str, str] | tuple[None, None]:
        if kind == "mas" or mas_id:
            return f"https://apps.apple.com/app/id{mas_id}", "App Store"

        token = name if kind == "cask" else cask_by_key.get(key(name))
        if token:
            home = (casks_meta.get(token) or {}).get("homepage")
            return (home, "homepage") if home else \
                   (f"https://formulae.brew.sh/cask/{token}", "brew")

        k = key(name)
        if k in mas_id_by_key:
            return f"https://apps.apple.com/app/id{mas_id_by_key[k]}", "App Store"
        return None, None

    def row(kind, name, meta, state, remote_ver=None, local_ver=None, mas_id=None):
        href, dest = link_for(kind, name, mas_id)
        return {"kind": kind, "name": name, "meta": meta, "state": state,
                "remote_ver": remote_ver, "local_ver": local_ver,
                "icon": icons.get(key(name)), "href": href, "dest": dest}

    rows = []
    for e in inv_mas:
        here = e["id"] in local_mas
        rows.append(row("mas", e["name"], f"MAS {e['id']}", "both" if here else "remote-only",
                        e["version"], local_mas[e["id"]]["version"] if here else None,
                        mas_id=e["id"]))
    for c in inv_casks:
        rows.append(row("cask", c, "brew cask", "both" if c in local_casks else "remote-only"))
    for a in inv_apps:
        if a.endswith(".app"):
            here = key(a) in local_keys
            rows.append(row("app", a.removesuffix(".app"), "/Applications",
                            "both" if here else "remote-only"))

    inv_mas_ids = {e["id"] for e in inv_mas}
    inv_app_keys = {key(a) for a in inv_apps if a.endswith(".app")}
    for mid, e in sorted(local_mas.items(), key=lambda kv: kv[1]["name"].lower()):
        if mid not in inv_mas_ids:
            rows.append(row("mas", e["name"], f"MAS {mid}", "local-only",
                            local_ver=e["version"], mas_id=mid))
    for c in sorted(local_casks - set(inv_casks)):
        rows.append(row("cask", c, "brew cask", "local-only"))
    for name in sorted(local_bundles, key=str.lower):
        if key(name) not in inv_app_keys:
            rows.append(row("app", name.removesuffix(".app"), "/Applications", "local-only"))
    return rows


# --- rendering --------------------------------------------------------------
CSS = """
:root{--bg:#fafafa;--card:#fff;--ink:#1a1a1a;--muted:#666;--line:#e3e3e3;--accent:#2d6cdf;
--ok:#1a7f4b;--warn:#b4690e;--new:#8257e6;--chip:#eef1f6;--shadow:0 1px 2px rgba(0,0,0,.06);}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#14161a;--card:#1c1f26;
--ink:#e8e8e8;--muted:#9aa3ad;--line:#2c313b;--accent:#5b9bff;--ok:#4ec98a;--warn:#e0a54a;
--new:#b79cff;--chip:#242832;--shadow:0 1px 2px rgba(0,0,0,.4);}}
:root[data-theme="dark"]{--bg:#14161a;--card:#1c1f26;--ink:#e8e8e8;--muted:#9aa3ad;--line:#2c313b;
--accent:#5b9bff;--ok:#4ec98a;--warn:#e0a54a;--new:#b79cff;--chip:#242832;
--shadow:0 1px 2px rgba(0,0,0,.4);}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);padding:2rem 1rem;
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1100px;margin:0 auto}
h1{font-size:1.5rem;margin:0 0 .2rem}
.sub{color:var(--muted);margin:0 0 1.25rem;font-size:.9rem;max-width:62ch}
.totals{display:flex;flex-wrap:wrap;gap:.5rem;margin-bottom:1rem}
.chip{background:var(--chip);border:1px solid var(--line);border-radius:999px;
padding:.25rem .75rem;font-size:.82rem;color:var(--muted)}
.chip b{color:var(--ink)}
.controls{display:flex;flex-wrap:wrap;gap:.5rem;margin-bottom:1.25rem;align-items:center}
input[type=search]{flex:1 1 14rem;padding:.45rem .7rem;border:1px solid var(--line);
border-radius:8px;background:var(--card);color:var(--ink);font-size:.9rem}
button{padding:.4rem .8rem;border:1px solid var(--line);border-radius:999px;background:var(--card);
color:var(--muted);font-size:.82rem;cursor:pointer}
button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
.grid{display:grid;gap:.6rem;grid-template-columns:repeat(auto-fill,minmax(210px,1fr))}
.item{display:flex;gap:.65rem;align-items:center;background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:.6rem .7rem;box-shadow:var(--shadow);min-width:0}
.item.dim{opacity:.62}
a.item{text-decoration:none;color:inherit;transition:border-color .12s,transform .12s,opacity .12s}
a.item:hover,a.item:focus-visible{border-color:var(--accent);transform:translateY(-1px);opacity:1}
a.item:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.go{color:var(--muted);font-size:.8rem;opacity:0;transition:opacity .12s;flex:0 0 auto}
a.item:hover .go,a.item:focus-visible .go{opacity:1}
.ico{width:40px;height:40px;flex:0 0 40px;border-radius:9px;object-fit:contain;
image-rendering:-webkit-optimize-contrast}
.mono{width:40px;height:40px;flex:0 0 40px;border-radius:9px;display:grid;place-items:center;
font-weight:700;font-size:1rem;color:#fff;letter-spacing:-.02em}
.txt{min-width:0;flex:1}
.nm{font-weight:600;font-size:.88rem;line-height:1.25;overflow-wrap:anywhere}
.meta{color:var(--muted);font-size:.74rem;margin-top:.15rem;display:flex;gap:.35rem;
align-items:center;flex-wrap:wrap}
.pill{font-size:.66rem;text-transform:uppercase;letter-spacing:.03em;padding:.05rem .4rem;
border-radius:5px;border:1px solid currentColor;white-space:nowrap}
.pill.both{color:var(--ok)}
.pill.remote-only{color:var(--warn)}
.pill.local-only{color:var(--new)}
.item.hide{display:none}
#empty{padding:2rem;text-align:center;color:var(--muted);display:none}
footer{color:var(--muted);font-size:.82rem;margin-top:1.5rem;text-align:center}
"""

JS = """
const sel = {state:'all', kind:'all'};
const items = [...document.querySelectorAll(".item")];
const q = document.getElementById('q');
function apply() {
  const term = q.value.trim().toLowerCase();
  let shown = 0;
  for (const it of items) {
    const ok = (sel.state === 'all' || it.dataset.state === sel.state)
      && (sel.kind === 'all' || it.dataset.kind === sel.kind)
      && (!term || it.dataset.name.includes(term));
    it.classList.toggle('hide', !ok);
    if (ok) shown++;
  }
  document.getElementById('empty').style.display = shown ? 'none' : 'block';
  document.getElementById('shown').textContent = shown;
}
for (const b of document.querySelectorAll('button[data-f]')) {
  b.addEventListener('click', () => {
    const f = b.dataset.f;
    sel[f] = b.dataset.v;
    for (const o of document.querySelectorAll(`button[data-f="${f}"]`))
      o.setAttribute('aria-pressed', String(o === b));
    apply();
  });
}
q.addEventListener('input', apply);
apply();
"""


def monogram(name: str) -> tuple[str, str]:
    """Deterministic initials + hue for entries with no local icon."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name) if w]
    initials = (words[0][0] + (words[1][0] if len(words) > 1 else "")).upper() if words else "?"
    hue = sum(ord(c) * (i + 1) for i, c in enumerate(name)) % 360
    return initials, f"hsl({hue} 45% 45%)"


def render(rows: list[dict], label: str, when: str) -> str:
    counts = {
        "total": len(rows),
        "both": sum(r["state"] == "both" for r in rows),
        "remote": sum(r["state"] == "remote-only" for r in rows),
        "local": sum(r["state"] == "local-only" for r in rows),
        "icons": sum(bool(r["icon"]) for r in rows),
    }

    cards = []
    for r in rows:
        if r["icon"]:
            art = f'<img class="ico" src="{r["icon"]}" alt="" loading="lazy">'
        else:
            initials, colour = monogram(r["name"])
            art = f'<div class="mono" style="background:{colour}" aria-hidden="true">{html.escape(initials)}</div>'
        ver = " → ".join(v for v in (r["remote_ver"], r["local_ver"]) if v)
        classes = "item" + ("" if r["state"] != "remote-only" else " dim")
        inner = (
            '{art}<div class="txt"><div class="nm">{name}</div>'
            '<div class="meta"><span class="pill {state}">{lab}</span>'
            '<span>{meta}</span>{ver}</div></div>{go}'
        ).format(
            art=art, name=html.escape(r["name"]), state=r["state"],
            meta=html.escape(r["meta"]), lab=STATE_LABEL[r["state"]],
            ver=f"<span>{html.escape(ver)}</span>" if ver else "",
            go=f'<span class="go" aria-hidden="true">↗</span>' if r["href"] else "",
        )
        attrs = 'data-kind="{kind}" data-state="{state}" data-name="{lname}"'.format(
            kind=r["kind"], state=r["state"], lname=html.escape(r["name"].lower()))
        if r["href"]:
            cards.append(
                f'<a class="{classes} link" {attrs} href="{html.escape(r["href"])}" '
                f'target="_blank" rel="noopener" '
                f'title="Open on {r["dest"]}">{inner}</a>')
        else:
            cards.append(f'<div class="{classes}" {attrs}>{inner}</div>')

    src = html.escape(label)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>App inventory — {src} vs this machine</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>App inventory</h1>
<p class="sub">Inventory <b>{src}</b> cross-referenced against this machine, {html.escape(when)}.
Icons come from the installed bundles, so anything showing a coloured monogram is
<b>not on this machine</b> — there is no bundle here to take an icon from.</p>
<div class="totals">
<span class="chip">showing: <b id="shown">{counts['total']}</b> of {counts['total']}</span>
<span class="chip">on both: <b>{counts['both']}</b></span>
<span class="chip">not here: <b>{counts['remote']}</b></span>
<span class="chip">new here: <b>{counts['local']}</b></span>
<span class="chip">real icons: <b>{counts['icons']}</b></span>
</div>
<div class="controls">
<input type="search" id="q" placeholder="filter by name…" autocomplete="off">
<button data-f="state" data-v="all" aria-pressed="true">all</button>
<button data-f="state" data-v="both" aria-pressed="false">on both</button>
<button data-f="state" data-v="remote-only" aria-pressed="false">not here</button>
<button data-f="state" data-v="local-only" aria-pressed="false">new here</button>
<button data-f="kind" data-v="all" aria-pressed="true">any source</button>
<button data-f="kind" data-v="mas" aria-pressed="false">App Store</button>
<button data-f="kind" data-v="cask" aria-pressed="false">cask</button>
<button data-f="kind" data-v="app" aria-pressed="false">/Applications</button>
</div>
<div class="grid">
{chr(10).join(cards)}
</div>
<div id="empty">nothing matches</div>
<footer>An app often appears up to three times — as an App Store entry, a Homebrew cask,
and an <code>/Applications</code> bundle. Filter by source to deduplicate.</footer>
</div><script>{JS}</script></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="launchpad-map inventory",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inventory", type=Path, help="markdown inventory to compare against")
    ap.add_argument("-o", "--output", type=Path,
                    help="HTML to write (default: <today>-app-inventory.html beside the inventory)")
    ap.add_argument("--label", help="name for the inventory's machine (default: file stem)")
    ap.add_argument("--no-icons", action="store_true", help="skip icon extraction (much smaller)")
    ap.add_argument("--icon-px", type=int, default=128, help="icon pixel size (default 128)")
    args = ap.parse_args()

    if not args.inventory.is_file():
        print(f"no such inventory: {args.inventory}", file=sys.stderr)
        return 1

    out = args.output or args.inventory.parent / f"{date.today():%Y-%m-%d}-app-inventory.html"
    inventory = parse_inventory(args.inventory.read_text(encoding="utf-8"))
    local = collect_local()
    icons = {} if args.no_icons else extract_icons(local[2], args.icon_px)
    casks_meta = cask_metadata(set(inventory[1]) | local[1])
    rows = build_rows(inventory, local, icons, casks_meta)
    if not rows:
        print("inventory parsed to zero entries — check its section headers", file=sys.stderr)
        return 1

    out.write_text(render(rows, args.label or args.inventory.stem,
                          f"{date.today():%Y-%m-%d}"), encoding="utf-8")
    print(f"{len(rows)} entries, {sum(bool(r['icon']) for r in rows)} icons "
          f"→ {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

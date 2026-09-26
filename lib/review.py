#!/usr/bin/env python3
"""Record showcase decisions in showcase.json.

    launchpad-map review --show APP...
    launchpad-map review --hide REASON APP...

An app is hidden from the public showcase only with a reason; the reason stays
in showcase.json and never reaches the page. The file is kept sorted by app
name, and created from showcase.example.json's note when it does not exist.
"""

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIEW = os.path.join(REPO, "showcase.json")
EXAMPLE = os.path.join(REPO, "showcase.example.json")


def main() -> int:
    ap = argparse.ArgumentParser(prog="launchpad-map review", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    how = ap.add_mutually_exclusive_group(required=True)
    how.add_argument("--show", action="store_true", help="publish these apps in the showcase")
    how.add_argument("--hide", metavar="REASON", help="keep these apps out of the showcase, for this reason")
    ap.add_argument("apps", nargs="+", help="Launchpad titles, as the history page shows them")
    args = ap.parse_args()
    if args.hide is not None and not args.hide.strip():
        ap.error("--hide needs a reason")

    if os.path.exists(REVIEW):
        with open(REVIEW) as fh:
            doc = json.load(fh)
    else:
        with open(EXAMPLE) as fh:
            doc = {"_about": json.load(fh).get("_about", ""), "apps": {}}
    apps = doc.setdefault("apps", {})
    for app in args.apps:
        apps[app] = {"show": True} if args.show else {"show": False, "reason": args.hide.strip()}
        print(f"{app}: {'shown' if args.show else 'hidden (' + args.hide.strip() + ')'}")
    doc["apps"] = dict(sorted(apps.items(), key=lambda kv: kv[0].lower()))
    tmp = REVIEW + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, REVIEW)
    return 0


if __name__ == "__main__":
    sys.exit(main())

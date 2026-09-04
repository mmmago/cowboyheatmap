#!/usr/bin/env python3
"""
    python3 make_heatmap.py

Downloaded trips are cached on disk, so a second run
only picks up what is new, and interrupting a long first download loses nothing.
"""
import argparse
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import build_heatmap as bh
import cowboy_routes as cb

BANNER = """\
 ↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗        ↗↗↗↗↗↗↗↗↗↗↗       ↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↘↘↘↘↓  ↘↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗  ↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗                   ↘↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗  ↗  ↗↗↗↗↗↗↗↗↗↗↗→    ←↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↗↗↗↗↗↗            ↗↗↗  ↗↗↗↗↗↗↗↗↗↙  ↗↗             ↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↗↗↗   ↙↗↗↗↗↗↗↗↗     ↗↗  ↗↗↗↗↗↗↗   ↗↗↖    ↗↗↗↗↗↗↗↗↓   ↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↗→  ↗↗↗↗↗↗↗↗↗↗  ↗↗↗  ↗↗  ↗↗↗↗↗  ↖↗↗  ↗↗→  ↗↗↗↗↗↗↗↗↗↗  ↘↗↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗→  ↗↗↗↗↗↗↗↗↗↗  ↗↗↗↗↗  →↗  ↗↗↗  ↘↗↗  ↗↗↗↗↗  ↗↗↗↗↗↗↗↗↗↗  ↓↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗  ↗↗↗↗↗↗↗↗↗↗  ↗↗↗↗↗↗↗  ↗↗  ↗  ↗↗↗  ↗↗↗↗↗↗↗  ↗↗↗↗↗↗↗↗↗↗  ↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗  ↗↗↗↗↗↗↗↗↗  ↘↘↘↘↘↘↘↘  ↘↘↘   ↗↗↗↗  ↗↗↗↗↗↗↗↗  ↗↗↗↗↗↗↗↗↗↙ ↘↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗→  ↗↗↗↗↗↗↗↗↗                 ←↗↗↗↗  ↗↗↗↗↗↗↗↗↗ ↗↗↗↗↗↗↗↗↗↙ ↘↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗  ↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗  ↗      ↗↗↗  ↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗  ↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↘  ↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗  ↘↗↗↗↗↗↗↗↗↗↗→  ↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗  ←↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↗↘  ↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗  →↗↗↗↗↗↗↗↗↗↗↗↗→  ↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗  ↙↗↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↗↗↗   →↗↗↗↗↗↗↗↗↗↘   ↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗   ↓↗↗↗↗↗↗↗↗↗→   ↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↗↗↗↗↗→           →↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗           →↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗
↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗↗"""


def rule(title):
    print(f"\n\033[1m{title}\033[0m")
    print("─" * max(len(title), 36))


def human(seconds):
    if seconds < 90:
        return f"{seconds:.0f} seconds"
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds / 3600:.1f} hours"


def cached_trips():
    return len(list(cb.TRIPS_DIR.glob("*.json"))) if cb.TRIPS_DIR.exists() else 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--grid", type=float, default=10,
                    help="grid size in metres; smaller = finer but heavier")
    ap.add_argument("--theme", choices=sorted(bh.THEMES), default="strava")
    ap.add_argument("--period", choices=("month", "quarter", "year"), default="quarter",
                    help="granularity of the date buttons")
    ap.add_argument("--out", default="heatmap.html")
    ap.add_argument("--routes", default="routes.geojson")
    ap.add_argument("--days", type=int, default=2000,
                    help="how far back to look for trips")
    ap.add_argument("--workers", type=int, default=cb.WORKERS,
                    help="parallel downloads from Cowboy; lower it if the "
                         "server starts refusing requests")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="extra pause between Cowboy requests (0 = none)")
    ap.add_argument("--skip-fetch", action="store_true",
                    help="use the trips already cached, do not contact Cowboy")
    ap.add_argument("--full", action="store_true",
                    help="re-list your whole history instead of only the part "
                         "newer than what is already cached")
    a = ap.parse_args()

    print(BANNER)

    steps = 2 if a.skip_fetch else 3
    n = 0

    # ── trips ───────────────────────────────────────────────────────────────
    if not a.skip_fetch:
        n += 1
        rule(f"Step {n}/{steps}   Fetch your rides from Cowboy")
        have = cached_trips()
        if have:
            print(f"{have:,} rides are already downloaded, so only new ones will be\n"
                  "fetched. This should take seconds.")
        else:
            print(f"Nothing is cached yet, so this is a full download of your riding\n"
                  f"history. It runs {a.workers} downloads at once over reused\n"
                  f"connections, so expect minutes rather than hours — but it is still\n"
                  f"the slowest step, and how long depends on your connection.\n\n"
                  "You can interrupt it safely: each ride is written to disk as it\n"
                  "arrives, and running this script again carries on where it left off.")
        print("\nSign in with the same email and password you use in the Cowboy app.")
        print("The password is not shown as you type and is never written to disk.\n")
        t0 = time.time()
        try:
            cb.cmd_fetch(SimpleNamespace(days=a.days, delay=a.delay,
                                         workers=a.workers, full=a.full,
                                         overlap=cb.OVERLAP_DAYS))
        except KeyboardInterrupt:
            print(f"\n\nStopped. {cached_trips():,} rides are safely saved — run this "
                  "script again to carry on from here.")
            return 1
        print(f"Took {human(time.time() - t0)}.")

    # ── routes.geojson ──────────────────────────────────────────────────────
    n += 1
    rule(f"Step {n}/{steps}   Extract the GPS tracks")
    if not cached_trips():
        print("No rides are cached, so there is nothing to build from.")
        print("Run this script again without --skip-fetch to download them first.")
        return 1
    cb.cmd_geojson(SimpleNamespace(out=a.routes))

    # ── heatmap.html ────────────────────────────────────────────────────────
    n += 1
    rule(f"Step {n}/{steps}   Build the map")
    print("Counting how often each stretch of road was ridden…")
    tracks = bh.load_tracks(a.routes)
    div = {"month": 1, "quarter": 3, "year": 12}[a.period]
    base = (min(m for _, m in tracks) // div) * div
    runs, per_bucket = bh.build_runs(tracks, a.grid, bh.SIMPLIFY_M, div, base)
    stats, mx = bh.render(
        runs, per_bucket, tracks, a.theme, a.out, a.grid,
        (bh.GLOW_WIDTH, bh.GLOW_OPACITY, bh.CORE_WIDTH, bh.CORE_LIGHTEN),
        div, base)
    print(f"{stats['trips']:,} rides · {stats['stretches']:,} stretches of road · "
          f"busiest one ridden {mx:,} times")

    out = Path(a.out).resolve()
    rule("Done")
    print(f"Open  {out}   ({out.stat().st_size / 1e6:.1f} MB)")
    print("Double-click it, or drag it into a browser window.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)

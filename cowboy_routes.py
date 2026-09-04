#!/usr/bin/env python3
"""Pull GPS route traces for Cowboy bike trips, for building a road heatmap.

Cowboy's own app-api does keep the route: each trip has a /charts endpoint returning a
`positions` array of [lat, lng] samples. This walks your trip history, caches
each trip's charts to disk, and emits GeoJSON.

Endpoints follow sam-dumont/cowboybike-strava-sync

Stdlib only — no pip install needed.

Credentials: just run the script and it will ask for them. The password is read
with getpass, so it is not echoed and never enters your shell history.

To skip the prompt (e.g. for an unattended run) set them in the environment
instead — but not on the command line, where they would be recorded:

    export COWBOY_USER_EMAIL='you@example.com'
    export COWBOY_USER_PASSWORD='...'

Usage:
    python3 cowboy_routes.py probe                    # does my bike record routes?
    python3 cowboy_routes.py fetch --days 400         # cache trips + geometry
    python3 cowboy_routes.py geojson --out routes.geojson
"""
import argparse
import getpass
import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

API = "https://app-api.cowboy.bike"
HOST = "app-api.cowboy.bike"
# Trips are fetched concurrently over persistent connections. Sequentially with
# a fresh TLS handshake each time it ran ~1.4 s per trip, which is hours for a
# full history; almost all of that was connection setup and waiting, not work.
WORKERS = 16
# Rides can reach the server days after they were ridden (the bike syncs when
# it next has a connection), so an incremental listing re-checks a few days
# either side of what is already cached rather than only strictly newer ones.
OVERLAP_DAYS = 7
CACHE = Path(__file__).parent / "cowboy_cache"
TRIPS_DIR = CACHE / "trips"
TOKEN_FILE = CACHE / "token.json"

# The app identifies itself with a fixed token; not a secret, not user-specific.
BASE_HEADERS = {
    "Content-Type": "application/json;charset=utf-8",
    "X-Cowboy-App-Token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
    "Client-Type": "Android-App",
    # Required. The API sits behind Cloudflare, which rejects urllib's default
    # "Python-urllib/x.y" agent with a 403 (error 1010) before the request ever
    # reaches Cowboy. okhttp is what the real Android app sends.
    "User-Agent": "okhttp/4.9.3",
}


class HttpError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status


def request(method, url, headers, body=None):
    """Returns (parsed_json, response_headers). Raises HttpError on 4xx/5xx."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return (json.loads(raw) if raw else None), dict(r.headers)
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode(errors="replace")) from None
    except urllib.error.URLError as e:
        sys.exit(f"Network error reaching {url}: {e.reason}")


class Pool:
    """One keep-alive HTTPS connection per worker thread.

    urllib opens a new connection per call, so every request paid a full TCP and
    TLS handshake. Reusing a connection removes that entirely; giving each
    thread its own avoids any locking on the hot path.
    """

    def __init__(self):
        self._local = threading.local()

    def conn(self):
        c = getattr(self._local, "c", None)
        if c is None:
            c = http.client.HTTPSConnection(HOST, timeout=30)
            self._local.c = c
        return c

    def drop(self):
        c = getattr(self._local, "c", None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
        self._local.c = None

    def get(self, path, hdrs, tries=3):
        for attempt in range(tries):
            try:
                c = self.conn()
                c.request("GET", path, headers=hdrs)
                r = c.getresponse()
                body = r.read()            # must drain to reuse the connection
                if r.status == 200:
                    return json.loads(body)
                if r.status in (401, 403, 429):
                    raise HttpError(r.status, body.decode(errors="replace"))
                raise HttpError(r.status, body.decode(errors="replace"))
            except (http.client.HTTPException, OSError):
                # a dropped keep-alive is normal; reconnect and try again
                self.drop()
                if attempt == tries - 1:
                    raise
                time.sleep(0.2 * (attempt + 1))


def login():
    email = os.getenv("COWBOY_USER_EMAIL")
    password = os.getenv("COWBOY_USER_PASSWORD")
    # Fall back to prompting. getpass keeps the password off the screen and out
    # of shell history; it needs a real terminal, hence the isatty check.
    if not email or not password:
        if not sys.stdin.isatty():
            sys.exit("No terminal to prompt on. Set COWBOY_USER_EMAIL and "
                     "COWBOY_USER_PASSWORD in the environment, or run this "
                     "directly in Terminal.")
        print("Cowboy credentials (the same ones you use in the Cowboy app).")
        email = email or input("  Email: ").strip()
        password = password or getpass.getpass("  Password (hidden): ")
    if not email or not password:
        sys.exit("Both an email and a password are required.")
    try:
        _, h = request("POST", f"{API}/auth/sign_in",
                       {**BASE_HEADERS, "Client": "Android-App"},
                       {"email": email, "password": password})
    except HttpError as e:
        if e.status == 401:
            sys.exit("Cowboy rejected the email/password (401 bad_credentials). "
                     "Use the same ones you sign in with in the Cowboy app.")
        if e.status == 403:
            sys.exit(f"Blocked before reaching Cowboy (403 — likely a Cloudflare "
                     f"rule against this client). Details: {e}")
        sys.exit(f"Cowboy login failed: {e}")
    # Header names are case-insensitive on the wire; normalise before reading.
    low = {k.lower(): v for k, v in h.items()}
    try:
        auth = {"Uid": low["uid"], "Access-Token": low["access-token"],
                "Client": low["client"]}
    except KeyError:
        sys.exit(f"Login returned no auth headers; the API may have changed. Got: {sorted(low)}")
    CACHE.mkdir(exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(auth))
    TOKEN_FILE.chmod(0o600)
    return auth


def headers(auth):
    return {**BASE_HEADERS, **auth}


def window(days, start=None):
    """The [start, end) the trip listing covers.

    `start` may narrow the window past what --days asks for, so an incremental
    run lists a couple of pages instead of every page of the full history. It
    never widens it: --days stays the outer bound.
    """
    end = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    oldest = end - timedelta(days=days)
    return (max(start, oldest) if start else oldest), end


def list_trips(auth, days, start=None):
    """Page through trip summaries. Returns a flat list of trip dicts."""
    start, end = window(days, start)
    trips, page, last, retried = [], 1, False, False
    while not last:
        # The app sends the window as a JSON body on GET — unusual, but it is
        # what the API expects; a query string alone returns an unfiltered page.
        try:
            data, _ = request("GET", f"{API}/trips", headers(auth),
                              {"page": page,
                               "from": start.strftime("%Y-%m-%dT%H:%M:%S"),
                               "to": end.strftime("%Y-%m-%dT%H:%M:%S")})
        except HttpError as e:
            if e.status == 401 and not retried:
                auth.update(login()); retried = True; continue
            raise
        retried = False
        for day in (data.get("daily_summaries") or {}).values():
            trips.extend(day.get("trips", []))
        last = data.get("last_page", True)
        page += 1
        print(f"\r  page {page - 1}: {len(trips)} trips", end="", flush=True)
        time.sleep(.2)
    print()
    return trips


def fetch_charts(auth, trip_id):
    try:
        data, _ = request("GET", f"{API}/trips/{trip_id}/charts", headers(auth))
    except HttpError as e:
        if e.status != 401:
            raise
        auth.update(login())
        data, _ = request("GET", f"{API}/trips/{trip_id}/charts", headers(auth))
    return data


def cmd_probe(args):
    """Answer the one question that decides everything: does this bike log positions?"""
    auth = login()
    print("Logged in. Listing recent trips…")
    trips = list_trips(auth, args.days)
    if not trips:
        sys.exit("No trips in that window — try a larger --days.")
    withdash = [t for t in trips if t.get("has_dashboard_data")]
    print(f"\n{len(trips)} trips, {len(withdash)} flagged has_dashboard_data "
          f"({100 * len(withdash) / len(trips):.0f}%)")
    if not withdash:
        sys.exit("None carry dashboard data — this firmware/bike does not upload routes.")

    ch = fetch_charts(auth, withdash[0]["id"])
    pos = ch.get("positions") or []
    real = [p for p in pos if p and p[0] is not None]
    print(f"\nSample trip {withdash[0]['id']}: keys = {sorted(ch.keys())}")
    print(f"  positions: {len(pos)} samples, {len(real)} with coordinates")
    if real:
        print(f"  first: {real[0]}   last: {real[-1]}")
        print("\n✅ Routes ARE available. Run:  python3 cowboy_routes.py fetch --days 400")
    else:
        print("\n❌ positions present but empty — no usable geometry.")


def newest_cached_start():
    """When the newest already-cached trip started, or None if nothing is cached.

    Each cache file is one big JSON object whose `trip` key comes first, so the
    timestamp is in the first few hundred bytes; reading only the head keeps
    this well under a second even with thousands of cached trips, where parsing
    each file in full (charts and all) would take minutes.
    """
    newest = None
    for f in TRIPS_DIR.glob("*.json"):
        try:
            with f.open("rb") as fh:
                head = fh.read(4096).decode("utf-8", "replace")
        except OSError:
            continue
        m = re.search(r'"started_at"\s*:\s*"([^"]+)"', head)
        if not m:
            continue
        try:
            # Timestamps come back as ISO 8601, usually Zulu. Normalise to naive
            # local time, which is what the window in list_trips is built from.
            t = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            if t.tzinfo is not None:
                t = t.astimezone().replace(tzinfo=None)
        except ValueError:
            continue
        if newest is None or t > newest:
            newest = t
    return newest


def cmd_fetch(args):
    auth = login()
    TRIPS_DIR.mkdir(parents=True, exist_ok=True)

    # Listing the whole history costs one request per 25 trips whether or not
    # anything is new, so on a re-run ask the server for the tail end only.
    # Charts for cached trips were already skipped below; this skips rediscovering
    # them in the first place.
    start = None
    if not getattr(args, "full", False):
        newest = newest_cached_start()
        if newest:
            overlap = getattr(args, "overlap", OVERLAP_DAYS)
            start, _ = window(args.days, newest - timedelta(days=overlap))
            oldest, _ = window(args.days)
            if start > oldest:
                print(f"Cache reaches {newest:%Y-%m-%d}; listing trips from "
                      f"{start:%Y-%m-%d} rather than all {args.days} days "
                      f"(--full to re-list everything).")

    trips = list_trips(auth, args.days, start)
    todo = [t for t in trips
            if t.get("has_dashboard_data") and not (TRIPS_DIR / f"{t['id']}.json").exists()]
    cached = len(trips) - len(todo)
    workers = max(1, args.workers)
    print(f"{len(trips)} trips, {cached} already cached, {len(todo)} to fetch "
          f"({workers} at a time)")
    if not todo:
        print("Nothing new to download.")
        return

    pool = Pool()
    hdrs = headers(auth)
    lock = threading.Lock()
    state = {"done": 0, "failed": 0, "blocked": 0}
    t0 = time.time()

    def relogin():
        # One thread refreshes the token; the rest pick up the new headers.
        nonlocal hdrs
        with lock:
            hdrs = headers(login())
        return hdrs

    def work(t):
        h = hdrs
        for attempt in range(2):
            try:
                ch = pool.get(f"/trips/{t['id']}/charts", h)
                (TRIPS_DIR / f"{t['id']}.json").write_text(
                    json.dumps({"trip": t, "charts": ch}))
                return True, None
            except HttpError as e:
                if e.status == 401 and attempt == 0:
                    h = relogin()
                    continue
                return False, e
            except Exception as e:
                return False, e
        return False, None

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(work, t): t for t in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                ok, err = fut.result()
                if ok:
                    state["done"] += 1
                else:
                    state["failed"] += 1
                    if isinstance(err, HttpError) and err.status in (403, 429):
                        state["blocked"] += 1
                rate = i / max(time.time() - t0, 1e-6)
                left = (len(todo) - i) / max(rate, 1e-6)
                print(f"\r  {i}/{len(todo)}  {rate:5.1f} trips/s  "
                      f"{state['failed']} failed  ~{left / 60:.1f} min left   ",
                      end="", flush=True)
                if args.delay:
                    time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\nStopping…")
        raise

    dt = time.time() - t0
    print(f"\nDone. {state['done']} fetched in {dt:.0f}s "
          f"({state['done'] / max(dt, 1e-6):.1f}/s), {cached} already cached, "
          f"{state['failed']} failed → {TRIPS_DIR}")
    if state["blocked"]:
        print(f"  {state['blocked']} were refused (403/429) — that is the server "
              f"pushing back. Rerun with --workers {max(2, workers // 2)} to go "
              f"gentler; anything already saved is kept.")


def cmd_geojson(args):
    files = sorted(TRIPS_DIR.glob("*.json"))
    if not files:
        sys.exit(f"Nothing cached in {TRIPS_DIR}. Run `fetch` first.")
    feats, dropped = [], 0
    for f in files:
        d = json.loads(f.read_text())
        trip, ch = d["trip"], d["charts"]
        # positions is [lat, lng]; GeoJSON wants [lng, lat]. Nulls appear where
        # the bike logged a sample without a GPS fix — drop those points.
        coords = [[p[1], p[0]] for p in (ch.get("positions") or [])
                  if p and len(p) == 2 and p[0] is not None and p[1] is not None]
        if len(coords) < 2:
            dropped += 1; continue
        feats.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"id": trip.get("id"), "started_at": trip.get("started_at"),
                           "distance_km": trip.get("distance"),
                           "duration_s": trip.get("unlocked_time")},
        })
    out = Path(args.out)
    out.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    pts = sum(len(f["geometry"]["coordinates"]) for f in feats)
    print(f"{len(feats)} routes, {pts} points → {out}"
          + (f"  ({dropped} trips had no usable geometry)" if dropped else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", help="check whether routes are available at all")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=cmd_probe)
    p = sub.add_parser("fetch", help="download and cache route geometry")
    p.add_argument("--days", type=int, default=400)
    p.add_argument("--workers", type=int, default=WORKERS,
                   help="parallel downloads; lower it if the server pushes back")
    p.add_argument("--delay", type=float, default=0.0,
                   help="extra pause between requests (0 = none)")
    p.add_argument("--overlap", type=int, default=OVERLAP_DAYS,
                   help="days of already-cached history to re-list, to catch "
                        "rides that reached the server late")
    p.add_argument("--full", action="store_true",
                   help="re-list the entire --days window instead of only the "
                        "part newer than the cache")
    p.set_defaults(func=cmd_fetch)
    p = sub.add_parser("geojson", help="build GeoJSON from the cache")
    p.add_argument("--out", default="routes.geojson")
    p.set_defaults(func=cmd_geojson)
    a = ap.parse_args()
    a.func(a)

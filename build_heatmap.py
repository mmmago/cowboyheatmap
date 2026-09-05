#!/usr/bin/env python3
"""Turn routes.geojson into an interactive road-frequency map.

Rendering is WebGL (MapLibre GL). Geometry is uploaded once and re-projected by
a vertex shader each frame, so pan and zoom never touch the paths in
JavaScript — a canvas renderer redraws all of them per frame, which is what
made a fine grid lag no matter how few layers it was grouped into.

Read it at neighbourhood-to-city zoom. Pushed to maximum zoom you will see
faint parallel lines where GPS jitter split one road across adjacent cells;
that is cosmetic and does not affect the counts (a coarser --grid reduces it).

Stdlib only.

Usage:
    python3 build_heatmap.py --routes routes.geojson --grid 10
    python3 build_heatmap.py --grid 5 --theme dark --out fine.html
"""
import argparse
import json
import re
import shutil
import subprocess
import tempfile
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Vector basemap: OpenFreeMap is keyless and, being real vector tiles, has
# coloured water and parks that a greyscale raster basemap cannot give.
OFM = "https://tiles.openfreemap.org/styles/{}"

# A stationary bike keeps logging, so one cell can hold hundreds of fixes for a
# single traffic light. Budget points per cell-pair crossed rather than a flat
# cap, so long runs keep their shape.
PTS_PER_PAIR = 3
# Douglas-Peucker tolerance in metres. A fine grid keeps every GPS fix and
# uploading all of them is wasteful; 2 m is well under a line's own width on
# screen, so the shape is unchanged.
SIMPLIFY_M = 2.0
# Coordinates ship as integers at 1e-5 degrees (~1.1 m), delta-encoded along
# each path. Neighbouring fixes are metres apart, so deltas are 1-2 digit
# integers instead of 9-character floats — the file parses far faster.
COORD_SCALE = 100000

# Glow. Each feature is drawn twice: a wide blurred halo in its colour, then a
# thin bright core on top, which is what gives the long-exposure look.
GLOW_WIDTH = 3.4        # halo width as a multiple of the core
GLOW_OPACITY = 0.22     # halo opacity at full brightness
CORE_WIDTH = 2.6        # how much the core thickens from quiet to busy
CORE_LIGHTEN = 0.45     # how far the core is pushed toward white

# Flow. A third pass of short bright dashes that crawl along each path, so the
# map reads as motion rather than as a still exposure. Runs are stored in the
# direction the ride was actually made (build_runs emits them along the trip
# that first claimed the stretch), so the dashes travel the way the bike went.
FLOW_WIDTH = 2.5        # constant px: dash lengths count in line widths, and a
                        # data-driven width would size every road's dash apart
FLOW_DASH = 0.3        # dash length, in line widths
FLOW_GAP = 11.0         # gap between dashes, same units
FLOW_MS = 1500           # milliseconds for a dash to travel one whole cycle:
                        # (dash + gap) * width / ms ~ 27 px/s, a walking pace
                        # that reads as travel without turning into a strobe
FLOW_STEPS = 22         # phases per cycle; each is one paint-property write

THEMES = {
    # Strava-style: crimson capillaries -> orange -> white-hot, over navy.
    "strava": {
        "style": OFM.format("dark"),
        "bg": "#0D1522", "panel": "rgba(9,15,26,.86)", "text": "#EAF1FC",
        "muted": "rgba(234,241,252,.62)",
        "ramp": ["#B02318", "#DC3A1B", "#F5601C", "#FF8F27", "#FFC65C", "#FFF3DC"],
    },
    "dark": {
        "style": OFM.format("dark"),
        "bg": "#12131A", "panel": "rgba(10,11,16,.86)", "text": "#EEF1F6",
        "muted": "rgba(238,241,246,.62)",
        "ramp": ["#2B3A67", "#3F6BB0", "#48A0D9", "#5FD0E0", "#B9F5E8", "#FFFFFF"],
    },
    "light": {
        "style": OFM.format("positron"),
        "bg": "#F7F2EB", "panel": "rgba(247,242,235,.90)", "text": "#081F5C",
        "muted": "rgba(8,31,92,.62)",
        "ramp": ["#D0E3FF", "#BAD6EB", "#7096D1", "#334EAC", "#12296B", "#081F5C"],
    },
}


# ── geometry helpers ────────────────────────────────────────────────────────
def simplify(pts, tol_m, mx, my):
    """Douglas-Peucker. Iterative, so a long path cannot blow the stack."""
    n = len(pts)
    if n < 3 or tol_m <= 0:
        return pts
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    tol2 = tol_m * tol_m
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ax, ay = pts[a][1] * mx, pts[a][0] * my
        bx, by = pts[b][1] * mx, pts[b][0] * my
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i][1] * mx, pts[i][0] * my
            if L <= 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
                qx, qy = ax + t * dx, ay + t * dy
                d = (px - qx) ** 2 + (py - qy) ** 2
            if d > best:
                best, bi = d, i
        if best > tol2:
            keep[bi] = True
            stack.append((a, bi))
            stack.append((bi, b))
    return [q for q, k in zip(pts, keep) if k]


def decimate(pts, k):
    """Keep at most k points, always including the first and last."""
    if len(pts) <= k:
        return pts
    step = (len(pts) - 1) / (k - 1)
    return [pts[round(i * step)] for i in range(k)]


def encode(pts):
    """Flat ints: absolute first point, then deltas. Deltas come from the
    ROUNDED values, so rounding error cannot accumulate along a path."""
    la = round(pts[0][0] * COORD_SCALE)
    ln = round(pts[0][1] * COORD_SCALE)
    out = [la, ln]
    for q in pts[1:]:
        nla = round(q[0] * COORD_SCALE)
        nln = round(q[1] * COORD_SCALE)
        out.append(nla - la)
        out.append(nln - ln)
        la, ln = nla, nln
    return out


# ── input ───────────────────────────────────────────────────────────────────
def load_tracks(path):
    """(coords, absolute month index) per trip.

    Trips without a start date are dropped rather than shown: a line that
    cannot be placed in time would sit at the wrong slider position, and a
    silently mis-dated road is worse than a missing one.
    """
    try:
        gj = json.loads(Path(path).read_text())
    except FileNotFoundError:
        sys.exit(f"{path} not found. Run:  python3 cowboy_routes.py geojson")
    out, undated = [], 0
    for f in gj.get("features", []):
        g = f.get("geometry") or {}
        if g.get("type") != "LineString" or len(g.get("coordinates", [])) < 2:
            continue
        started = (f.get("properties") or {}).get("started_at")
        if not started or len(started) < 7:
            undated += 1
            continue
        out.append((g["coordinates"], int(started[:4]) * 12 + int(started[5:7]) - 1))
    if not out:
        sys.exit(f"No usable dated LineStrings in {path}.")
    if undated:
        print(f"  {undated} trips had no start date and were skipped", file=sys.stderr)
    return out


# ── counting ────────────────────────────────────────────────────────────────
def build_runs(tracks, grid_m, simplify_m, div, base):
    """Runs of real track, each tagged with the months it was ridden in.

    A run breaks when the month-vector changes, not when a colour would: two
    neighbouring stretches sharing a vector render identically at EVERY date
    range, so merging them is safe wherever the slider sits, whereas merging on
    colour would be wrong the moment the range moved.
    """
    lats = [c[1] for t, _ in tracks for c in t]
    mean_lat = sum(lats) / len(lats)
    my = 111_320.0
    mx = 111_320.0 * max(math.cos(math.radians(mean_lat)), 1e-6)
    dlat, dlng = grid_m / my, grid_m / mx

    # Pass 1 — trips per stretch, per month.
    per_month = defaultdict(Counter)
    trips = []
    for coords, month in tracks:
        bucket = (month - base) // div
        cells = [(round(lat / dlat), round(lng / dlng)) for lng, lat in coords]
        trans, prev, entered = [], cells[0], 0
        for i in range(1, len(cells)):
            if cells[i] == prev:
                continue                      # still in the same cell, or stopped
            key = (prev, cells[i]) if prev <= cells[i] else (cells[i], prev)
            trans.append((key, entered, i))
            prev, entered = cells[i], i
        trips.append((coords, trans))
        for k in {k for k, _, _ in trans}:    # a set: one trip counts once
            per_month[k][bucket] += 1

    sig = {k: tuple(sorted(v.items())) for k, v in per_month.items()}

    # Pass 2 — each trip draws only the stretches nobody has drawn yet, as
    # unbroken runs. Taking geometry per stretch from whichever trip crossed it
    # first would leave adjacent pieces sourced from different trips, which do
    # not meet: the map breaks into disconnected dashes.
    claimed, runs = set(), []

    def emit(lo, hi, coords, pairs, vec):
        pts = [[c[1], c[0]] for c in coords[lo:hi + 1]]
        pts = simplify(decimate(pts, max(2, pairs * PTS_PER_PAIR)), simplify_m, mx, my)
        runs.append((encode(pts), vec))

    for coords, trans in trips:
        start = end = run_sig = None
        run_pairs = 0
        for key, a, b in trans:
            fresh = key not in claimed
            s = sig[key]
            if fresh and start is not None and a == end and s == run_sig:
                end, run_pairs = b, run_pairs + 1
            else:
                if start is not None:
                    emit(start, end, coords, run_pairs, run_sig)
                if fresh:
                    start, end, run_sig, run_pairs = a, b, s, 1
                else:
                    start = end = None
            claimed.add(key)
        if start is not None:
            emit(start, end, coords, run_pairs, run_sig)

    return runs, per_month


# ── output ──────────────────────────────────────────────────────────────────
def render(runs, per_month, tracks, theme, out, grid_m, glow, div, base):
    T = THEMES[theme]
    nb = max((m - base) // div for _, m in tracks) + 1

    # Runs sharing a month-vector render identically at every range, so they
    # collapse into one MultiLineString feature. That is what keeps the feature
    # count workable: ~300k stretches become a few tens of thousands of shapes.
    groups = defaultdict(list)
    for path, vec in runs:
        groups[vec].append(path)
    # Month vectors ship as a flat, delta-encoded int array rather than a JSON
    # object: [firstMonth, count, gap, count, ...]. Written as {"c37":2,...} the
    # keys and quotes alone cost more than the numbers, and there are ~800k
    # entries — this is the difference between an 11 MB and an 8 MB file.
    def encvec(vec):
        out, prev = [], 0
        for i, n in vec:
            out.append(i - prev)
            out.append(n)
            prev = i
        return out

    feats = [{"c": encvec(vec), "l": paths} for vec, paths in groups.items()]
    feats.sort(key=lambda f: sum(f["c"][1::2]))        # quiet first, busy on top

    tpb = Counter((m - base) // div for _, m in tracks)

    # Opening view: where the riding concentrates, not the full extent. A couple
    # of day trips out of the city would otherwise zoom out to the whole region
    # and shrink the daily riding to a blob. Weighting by trips lets the roads
    # ridden daily decide the frame.
    def weighted_span(pairs, lo=.02, hi=.98):
        pairs = sorted(pairs)
        total = sum(w for _, w in pairs) or 1
        res, run, want = [], 0, [lo * total, hi * total]
        for v, w in pairs:
            run += w
            while want and run >= want[0]:
                res.append(v)
                want.pop(0)
        while len(res) < 2:
            res.append(pairs[-1][0])
        return res[0], res[1]

    mids = [(p[0] / COORD_SCALE, p[1] / COORD_SCALE, sum(c for _, c in v))
            for p, v in runs]
    lat_core = weighted_span([(a, n) for a, _, n in mids])
    lng_core = weighted_span([(b, n) for _, b, n in mids])
    lats = [c[1] for t, _ in tracks for c in t]
    lngs = [c[0] for t, _ in tracks for c in t]

    payload = {
        "feats": feats, "scale": COORD_SCALE,
        "ramp": T["ramp"], "style": T["style"],
        "core": [[lat_core[0], lng_core[0]], [lat_core[1], lng_core[1]]],
        "bounds": [[min(lats), min(lngs)], [max(lats), max(lngs)]],
        "base": base, "div": div, "nb": nb,
        "m0": min(m for _, m in tracks), "m1": max(m for _, m in tracks),
        "tpb": [tpb.get(i, 0) for i in range(nb)],
        # The colour scale is scaled from the all-time value by how many trips
        # the window holds, rather than re-derived in the browser: the exact
        # percentile means scanning every shape's vector on each drag frame,
        # which is the single most expensive thing the slider used to do.
        "capAll": max(sorted((sum(v.values()) for v in per_month.values()),
                             reverse=True)[max(0, int(len(per_month) * .01))], 2),
        "trips": len(tracks),
        "glowW": glow[0], "glowO": glow[1], "coreW": glow[2], "coreLite": glow[3],
        "flowW": FLOW_WIDTH, "flowDash": FLOW_DASH, "flowGap": FLOW_GAP,
        "flowMs": FLOW_MS, "flowSteps": FLOW_STEPS,
    }
    mx_count = max(sum(v.values()) for v in per_month.values())
    Path(out).write_text(TEMPLATE.format(
        payload=json.dumps(payload, separators=(",", ":")),
        bg=T["bg"], panel=T["panel"], text=T["text"], muted=T["muted"],
        accent=T["ramp"][3], bar=",".join(T["ramp"]),
        trips=f"{len(tracks):,}", grid=f"{grid_m:g}",
        stretches=f"{len(per_month):,}", mx=f"{mx_count:,}"))
    check_js(out)
    return {"trips": len(tracks), "stretches": len(per_month),
            "paths": len(runs), "shapes": len(feats)}, mx_count


def check_js(path):
    """Parse the generated script if node is around.

    Worth the few hundred ms: the page is one big inline script, so a single
    syntax slip anywhere in it stops the whole thing and the map comes up blank
    with no clue as to why. Silent if node is missing — it is only a guard.
    """
    node = shutil.which("node")
    if not node:
        return
    html = Path(path).read_text()
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    if not blocks:
        return
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(blocks[-1])
        tmp = f.name
    try:
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
        if r.returncode:
            print(f"!! generated JavaScript does not parse — the page will be blank:\n"
                  f"{r.stderr.strip()[:600]}", file=sys.stderr)
    finally:
        Path(tmp).unlink(missing_ok=True)


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Roads I ride most</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.css" />
<script src="https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.js"></script>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  html,body,#map{{height:100%;width:100%}}
  body{{background:{bg};font-family:'Inter',sans-serif;color:{text};
    --accent:{accent};--panelbg:{bg}}}
  .panel{{position:absolute;z-index:500;top:18px;left:18px;background:{panel};
    backdrop-filter:blur(10px);border-radius:16px;padding:16px 18px 14px;width:300px}}
  h1{{font-size:15.5px;font-weight:700;letter-spacing:-.02em;margin-bottom:3px}}
  .sub{{font-size:11px;color:{muted};line-height:1.55}}
  .legend{{margin-top:12px;display:flex;align-items:center;gap:8px}}
  .bar{{flex:1;height:7px;border-radius:99px;background:linear-gradient(90deg,{bar})}}
  .lab{{font-size:9px;font-weight:600;letter-spacing:.06em;color:{muted};white-space:nowrap}}
  .foot{{margin-top:10px;font-size:9px;letter-spacing:.07em;color:{muted};font-weight:500}}
  .foot a{{color:{text};text-decoration:none;border-bottom:1px solid {muted};cursor:pointer;
    white-space:nowrap}}
  .foot.acts{{margin-top:6px}}
  .dates{{margin-top:13px;border-top:1px solid color-mix(in srgb,currentColor 16%,transparent);
    padding-top:11px}}
  .drow{{display:flex;justify-content:space-between;align-items:baseline}}
  .drow b{{font-size:11.5px;font-weight:700;letter-spacing:-.01em}}
  .drow span{{font-size:9.5px;color:{muted};font-weight:600;letter-spacing:.05em}}
  .dbtns{{margin-top:10px;display:flex;gap:5px;flex-wrap:wrap}}
  .dbtns button{{font-size:9px;font-weight:700;letter-spacing:.05em;cursor:pointer;
    background:color-mix(in srgb,currentColor 10%,transparent);color:inherit;
    border:none;border-radius:99px;padding:4px 9px;font-family:inherit}}
  .dbtns button:hover{{background:color-mix(in srgb,currentColor 22%,transparent)}}
  .dbtns button.on{{background:var(--accent);color:{bg}}}
  #tip{{position:absolute;z-index:600;display:none;pointer-events:none;background:{panel};
    color:{text};font-size:10px;font-weight:700;padding:4px 7px;border-radius:6px;
    white-space:nowrap}}
</style>
</head>
<body>
<div id="map"></div>
<div id="tip"></div>
<div class="panel">
  <h1>Roads I ride most</h1>
  <div class="sub">{trips} trips on a {grid} m grid. Brighter and thicker = more
    separate trips down that stretch.</div>
  <div class="legend"><span class="lab">1</span><span class="bar"></span><span class="lab" id="capLab"></span></div>
  <div class="foot">{stretches} STRETCHES · MAX {mx} TRIPS</div>
  <div class="foot acts"><a id="fitall">FIT ALL</a> · <a id="export">EXPORT PNG</a>
    · <a id="flow">FLOW ON</a></div>
  <div class="dates">
    <div class="drow"><b id="dRange"></b><span id="dTrips"></span></div>
    <div class="dbtns">
      <button data-span="all">ALL</button>
      <button data-span="12">LAST YEAR</button>
      <button data-span="3">LAST 3M</button>
      <button data-span="year">THIS YEAR</button>
    </div>
  </div>
</div>
<script>
const D = {payload};
const S = D.scale, G = {{ w: D.glowW, o: D.glowO, core: D.coreW, lite: D.coreLite }};
const F = {{ w: D.flowW, dash: D.flowDash, gap: D.flowGap,
            ms: D.flowMs, steps: D.flowSteps }};

// Dash phases, precomputed once. line-dasharray is measured in line widths and
// takes only constants — no expression, so no per-vertex attribute is rebuilt
// when it changes, which is what makes animating it cheap at this many paths.
// A phase u ships as [0, u, dash, gap - u]: a zero-length dash, then a gap that
// pushes the real dash u along. Past u = gap the dash straddles the end of the
// pattern, so it goes out as its tail, the gap, then its head — the head joins
// the next repetition's tail across the trailing zero gap and reads as one dash.
const PHASES = (() => {{
  const P = F.dash + F.gap, out = [];
  for (let i = 0; i < F.steps; i++) {{
    const u = P * i / F.steps;
    out.push(u === 0 ? [F.dash, F.gap]
           : u <= F.gap ? [0, u, F.dash, F.gap - u]
           : [u + F.dash - P, F.gap, P - u, 0]);
  }}
  return out;
}})();
const MONTHS = ['January','February','March','April','May','June','July',
                'August','September','October','November','December'];
// NB: this template is rendered with str.format(), which escapes braces but
// leaves % alone — a doubled %% would survive into the JS verbatim.
const month = m => MONTHS[m % 12] + ' ' + String(Math.floor(m / 12)).slice(-2);
// A bucket spans `div` months, so a range runs from the first month of its first
// bucket to the LAST month of its last one — Q4 '21 through Q3 '26 is October to
// September. Both ends are then pulled back to the months actually ridden, so a
// bucket reaching past the first or last trip cannot name a month nobody rode in.
const monthRange = (a, b, sep) => {{
  const clamp = m => Math.min(Math.max(m, D.m0), D.m1);
  const first = clamp(D.base + a * D.div);
  const last = clamp(D.base + b * D.div + D.div - 1);
  return first === last ? month(first) : month(first) + sep + month(last);
}};

// Geometry is uploaded to the GPU once. Changing the date range only rebuilds a
// paint expression, so not a single coordinate is re-sent — that is why the
// slider stays smooth with hundreds of thousands of paths.
const FC = {{ type: 'FeatureCollection', features: D.feats.map(f => ({{
  type: 'Feature',
  properties: (() => {{
    const p = {{}};
    let m = 0;
    for (let i = 0; i < f.c.length; i += 2) {{ m += f.c[i]; p['c' + m] = f.c[i + 1]; }}
    return p;
  }})(),
  geometry: {{ type: 'MultiLineString', coordinates: f.l.map(flat => {{
    let la = flat[0], ln = flat[1];
    const pts = [[ln / S, la / S]];
    for (let i = 2; i < flat.length; i += 2) {{
      la += flat[i]; ln += flat[i + 1];
      pts.push([ln / S, la / S]);
    }}
    return pts;
  }}) }},
}})) }};

function lighten(hex, amt) {{
  const n = parseInt(hex.slice(1), 16);
  const m = c => Math.round(c + (255 - c) * amt);
  return 'rgb(' + m((n >> 16) & 255) + ',' + m((n >> 8) & 255) + ',' + m(n & 255) + ')';
}}

// Sum of the selected months. coalesce lets a feature omit months it was never
// ridden in, which keeps the file sparse — most stretches see a handful of the
// months on offer.
function sumExpr(a, b) {{
  const t = ['+', 0];
  for (let m = a; m <= b; m++) t.push(['coalesce', ['get', 'c' + m], 0]);
  return t;
}}
function vExpr(sum, cap) {{
  return ['min', 1, ['sqrt', ['/', ['max', 0, ['-', sum, 1]], Math.max(1, cap - 1)]]];
}}
// Opacity is folded into the colour's alpha instead of being its own property.
// Every data-driven paint property makes MapLibre rebuild a per-vertex
// attribute array across every tile on each change, and with ~400k vertices
// that is what made the slider crawl. Colour carries alpha, blur and the halo
// width are constants, so a range change touches three arrays rather than
// eight. `shown` collapses to a fully transparent colour, which also avoids a
// filter — changing a filter would force a re-tessellation, far worse again.
function colorExpr(v, shown, lite, a0, a1) {{
  const stops = [];
  for (let i = 0; i < D.ramp.length; i++) {{
    const t = i / (D.ramp.length - 1);
    const c = lite ? lighten(D.ramp[i], lite * t) : D.ramp[i];
    const rgb = c.startsWith('#')
      ? [parseInt(c.slice(1, 3), 16), parseInt(c.slice(3, 5), 16), parseInt(c.slice(5, 7), 16)]
      : c.match(/\\d+/g).map(Number);
    stops.push(t, 'rgba(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ',' +
                    (a0 + (a1 - a0) * t).toFixed(3) + ')');
  }}
  return ['case', shown, ['interpolate', ['linear'], v].concat(stops), 'rgba(0,0,0,0)'];
}}

// The scale has to follow the window — one quarter holds far smaller counts
// than five years, and reusing the all-time value leaves a short range almost
// black. Scaling by the share of trips in the window is O(1); taking the exact
// percentile meant walking every shape's vector on every drag frame.
function capFor(a, b) {{
  let n = 0;
  for (let i = a; i <= b; i++) n += D.tpb[i];
  return Math.max(2, Math.round(D.capAll * n / D.trips));
}}

const map = new maplibregl.Map({{
  container: 'map', style: D.style,
  bounds: [[D.core[0][1], D.core[0][0]], [D.core[1][1], D.core[1][0]]],
  fitBoundsOptions: {{ padding: 30 }},
  attributionControl: {{ compact: true }}, antialias: true,
}});
map.addControl(new maplibregl.NavigationControl({{ showCompass: false }}), 'top-right');
document.getElementById('fitall').onclick = () =>
  map.fitBounds([[D.bounds[0][1], D.bounds[0][0]], [D.bounds[1][1], D.bounds[1][0]]],
                {{ padding: 30, duration: 600 }});

// Fixed presets rather than a draggable range. A range input fires on every
// pixel of a drag, and each change rebuilds the per-vertex paint attributes for
// every tile — no amount of throttling makes that feel continuous at this many
// vertices, whereas a preset costs exactly one rebuild.
let RANGE = [0, D.nb - 1];

function apply() {{
  const a = RANGE[0], b = RANGE[1];
  const sum = sumExpr(a, b), v = vExpr(sum, capFor(a, b));
  const shown = ['>', sum, 0];
  if (map.getLayer('core')) {{
    map.setPaintProperty('halo', 'line-color',
      colorExpr(v, shown, 0, G.o * 0.4, G.o));
    map.setPaintProperty('core', 'line-color',
      colorExpr(v, shown, G.lite, 0.55, 1.0));
    map.setPaintProperty('core', 'line-width', ['+', 0.65, ['*', v, G.core]]);
    // Near-white dashes, faint on a road ridden once and hot on the commute,
    // so the flow picks out the busy lines instead of speckling everything.
    map.setPaintProperty('flow', 'line-color',
      colorExpr(v, shown, 0.75, 0.20, 0.95));
  }}
  let n = 0;
  for (let i = a; i <= b; i++) n += D.tpb[i];
  document.getElementById('dRange').textContent = monthRange(a, b, ' – ');
  document.getElementById('dTrips').textContent = n.toLocaleString() + ' TRIPS';
  document.getElementById('capLab').textContent = capFor(a, b) + '+';
}}

// Motion is opt-out: the whole point of the layer is that it moves, but a
// reader who has asked the OS for less of it should not have to.
let flowOn = !matchMedia('(prefers-reduced-motion: reduce)').matches;
let flowRAF = 0, flowStep = -1;
const flowLink = document.getElementById('flow');

// One paint write per phase rather than per frame: at a couple of dozen phases
// a cycle the motion is already continuous, and each write re-renders every
// visible path.
function flowTick(t) {{
  flowRAF = requestAnimationFrame(flowTick);
  const i = Math.floor(t % F.ms / F.ms * F.steps) % F.steps;
  if (i === flowStep || !map.getLayer('flow')) return;
  flowStep = i;
  map.setPaintProperty('flow', 'line-dasharray', PHASES[i]);
}}

// A hidden tab stops firing rAF on its own, so the loop parks itself whenever
// the page is not on screen — nothing else to unwind.
function setFlow(on) {{
  flowOn = on;
  flowLink.textContent = on ? 'FLOW ON' : 'FLOW OFF';
  if (map.getLayer('flow'))
    map.setLayoutProperty('flow', 'visibility', on ? 'visible' : 'none');
  if (on && !flowRAF) flowRAF = requestAnimationFrame(flowTick);
  if (!on && flowRAF) {{ cancelAnimationFrame(flowRAF); flowRAF = 0; flowStep = -1; }}
}}
flowLink.onclick = () => setFlow(!flowOn);

map.on('load', () => {{
  // The dark basemap stamps a oneway arrow along every one-way street; at high
  // zoom that reads as noise under the heat lines, so drop those layers.
  for (const l of map.getStyle().layers)
    if (/oneway|arrow/.test(l.id)) map.setLayoutProperty(l.id, 'visibility', 'none');

  map.addSource('rides', {{ type: 'geojson', data: FC }});
  // Halo width and blur are constants: they never change with the range, so
  // keeping them out of the data-driven set is free.
  const hw = (0.65 + 0.5 * G.core) * G.w;
  map.addLayer({{ id: 'halo', type: 'line', source: 'rides',
    layout: {{ 'line-cap': 'round', 'line-join': 'round' }},
    paint: {{ 'line-width': hw, 'line-blur': hw * 0.6 }} }});
  map.addLayer({{ id: 'core', type: 'line', source: 'rides',
    layout: {{ 'line-cap': 'round', 'line-join': 'round' }} }});
  // On top of the core, and deliberately a constant width: a dash is a multiple
  // of the line's own width, so the data-driven core width would make a busy
  // road's dashes five times longer than a quiet one's and the whole field
  // would crawl at different speeds. Round caps round the dashes into comets.
  map.addLayer({{ id: 'flow', type: 'line', source: 'rides',
    layout: {{ 'line-cap': 'round', 'line-join': 'round',
              visibility: flowOn ? 'visible' : 'none' }},
    paint: {{ 'line-width': F.w, 'line-blur': F.w * 0.5,
             'line-dasharray': PHASES[0] }} }});
  apply();
  setFlow(flowOn);

  const tip = document.getElementById('tip');
  map.on('mousemove', e => {{
    const hit = map.queryRenderedFeatures(e.point, {{ layers: ['core'] }});
    if (!hit.length) {{ tip.style.display = 'none'; return; }}
    const p = hit[0].properties;
    let n = 0;
    for (let i = RANGE[0]; i <= RANGE[1]; i++) n += (p['c' + i] || 0);
    if (!n) {{ tip.style.display = 'none'; return; }}
    tip.textContent = n + (n === 1 ? ' trip' : ' trips');
    tip.style.display = 'block';
    tip.style.left = (e.point.x + 14) + 'px';
    tip.style.top = (e.point.y + 14) + 'px';
  }});
  map.on('mouseout', () => {{ tip.style.display = 'none'; }});
}});

// Export the current view at print resolution. The on-screen canvas is only as
// big as the window, so grabbing it gives a picture the size of a screenshot.
// Instead a second map is built off-screen from the live style — same centre,
// zoom and the current range's paint expressions — with its pixel ratio cranked
// up. MapLibre then re-renders tiles, labels and every line at that ratio, so
// the result is genuinely drawn large rather than upscaled.
const EXP_LONG = 8000;   // target long edge in pixels
const EXP_MAX = 8192;    // per-axis ceiling; MapLibre re-clamps if the GPU says no
const EXP_PIX = 42e6;    // total pixels, so a square window cannot ask for 64 MP
const expLink = document.getElementById('export');

function expLabel(t) {{ expLink.textContent = t; }}
function expBusy(on) {{
  expLink.dataset.busy = on ? '1' : '';
  expLink.style.opacity = on ? 0.55 : 1;
  expLabel(on ? 'RENDERING…' : 'EXPORT PNG');
}}
function expNote(t) {{
  expLabel(t);
  setTimeout(() => {{ if (!expLink.dataset.busy) expLabel('EXPORT PNG'); }}, 3000);
}}

// Burn in the attribution: the exported image leaves the page without the map's
// own attribution control, and these are OpenStreetMap tiles.
function stamp(cv, ratio) {{
  let out;
  try {{
    out = document.createElement('canvas');
    out.width = cv.width;
    out.height = cv.height;
    const g = out.getContext('2d');
    if (!g) return cv;
    g.drawImage(cv, 0, 0);
    const px = Math.max(11, Math.round(11 * ratio));
    g.font = '600 ' + px + 'px Inter, sans-serif';
    g.textBaseline = 'alphabetic';
    const txt = '© OpenStreetMap contributors · OpenFreeMap';
    const pad = px * 0.7;
    const w = g.measureText(txt).width;
    g.fillStyle = 'rgba(0,0,0,.42)';
    g.fillRect(cv.width - w - pad * 2, cv.height - px - pad * 2, w + pad * 2, px + pad * 2);
    g.fillStyle = 'rgba(255,255,255,.86)';
    g.fillText(txt, cv.width - w - pad, cv.height - pad * 1.15);
    // A canvas past the browser's 2D size limit fails quietly rather than
    // throwing, so check that the copy actually landed: the basemap paints an
    // opaque background, so a transparent pixel means nothing was drawn.
    if (g.getImageData(1, 1, 1, 1).data[3] === 0) return cv;
  }} catch (e) {{
    return cv;
  }}
  return out;
}}

function expName(w, h) {{
  return ('heatmap ' + monthRange(RANGE[0], RANGE[1], '-') + ' ' + w + 'x' + h)
    .replace(/[^\\w.-]+/g, '_') + '.png';
}}

function exportPNG() {{
  if (expLink.dataset.busy) return;
  if (!map.isStyleLoaded()) {{ expNote('STILL LOADING'); return; }}
  const box = map.getContainer();
  const w = box.offsetWidth, h = box.offsetHeight;
  const ratio = Math.max(1, Math.min(EXP_LONG / Math.max(w, h),
                                     EXP_MAX / Math.max(w, h),
                                     Math.sqrt(EXP_PIX / (w * h))));
  expBusy(true);

  // Off-screen rather than hidden: display:none or visibility:hidden would stop
  // the browser compositing the canvas, and nothing would ever be drawn.
  const holder = document.createElement('div');
  holder.style.cssText = 'position:fixed;top:0;left:-20000px;pointer-events:none;' +
                         'width:' + w + 'px;height:' + h + 'px';
  document.body.appendChild(holder);

  // The flow layer only means anything in motion; frozen into a still it is
  // just a dashed line laid over the picture, so the shot renders without it.
  const style = map.getStyle();
  style.layers = style.layers.filter(l => l.id !== 'flow');

  const shot = new maplibregl.Map({{
    container: holder,
    style: style,
    center: map.getCenter(), zoom: map.getZoom(),
    bearing: map.getBearing(), pitch: map.getPitch(),
    pixelRatio: ratio, maxCanvasSize: [EXP_MAX, EXP_MAX],
    preserveDrawingBuffer: true, antialias: true,
    interactive: false, attributionControl: false, fadeDuration: 0,
  }});

  let done = false;
  const grab = () => {{
    if (done) return;
    done = true;
    clearTimeout(timer);
    const out = stamp(shot.getCanvas(), ratio);
    const size = out.width + '×' + out.height;
    const name = expName(out.width, out.height);
    // Only tear the map down once the blob is out: if the 2D copy was refused,
    // `out` is the map's own canvas and removing it first would empty the file.
    out.toBlob(blob => {{
      shot.remove();
      holder.remove();
      expBusy(false);
      if (!blob) {{ expNote('EXPORT FAILED'); return; }}
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      // Firefox only acts on a programmatic click if the anchor is in the
      // document, so it has to be attached before the click, not after.
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      expNote('SAVED ' + size);
    }}, 'image/png');
  }};

  // A missing tile must not abort the shot, so `idle` is backed by a deadline
  // rather than by an error handler: whatever has arrived by then gets written.
  // Generous, because it is a backstop and not a budget — a full history at
  // 35 MP takes ~18 s on a fast laptop, and half a picture beats none.
  const timer = setTimeout(grab, 90000);
  shot.on('idle', grab);
  shot.on('error', e => console.warn('export:', e && e.error));
}}

expLink.onclick = exportPNG;

const buttons = [...document.querySelectorAll('.dbtns button')];
function span(s) {{
  if (s === 'all') return [0, D.nb - 1];
  if (s === 'year') {{
    // first bucket of the last calendar year present in the data
    const lastY = Math.floor((D.base + (D.nb - 1) * D.div) / 12);
    return [Math.max(0, Math.ceil((lastY * 12 - D.base) / D.div)), D.nb - 1];
  }}
  // spans are given in months; round to whole buckets, at least one
  return [Math.max(0, D.nb - Math.max(1, Math.round(+s / D.div))), D.nb - 1];
}}
buttons.forEach(btn => btn.addEventListener('click', () => {{
  RANGE = span(btn.dataset.span);
  buttons.forEach(b => b.classList.toggle('on', b === btn));
  apply();
}}));
buttons[0].classList.add('on');
apply();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--routes", default="routes.geojson")
    ap.add_argument("--out", default="heatmap.html")
    ap.add_argument("--grid", type=float, default=10,
                    help="snap size in metres; smaller = more detail but more "
                         "jitter-split, larger = merges nearby roads")
    ap.add_argument("--theme", choices=sorted(THEMES), default="strava")
    ap.add_argument("--period", choices=("month", "quarter", "year"),
                    default="quarter",
                    help="date-slider granularity. Finer means more shapes and a "
                         "longer sum expression per feature, so month is the "
                         "slowest and year the fastest")
    ap.add_argument("--simplify", type=float, default=SIMPLIFY_M, metavar="METRES",
                    help="Douglas-Peucker tolerance; 0 keeps every GPS fix")
    ap.add_argument("--glow-width", type=float, default=GLOW_WIDTH)
    ap.add_argument("--glow-opacity", type=float, default=GLOW_OPACITY)
    ap.add_argument("--core-width", type=float, default=CORE_WIDTH)
    ap.add_argument("--core-lighten", type=float, default=CORE_LIGHTEN)
    a = ap.parse_args()

    tracks = load_tracks(a.routes)
    div = {"month": 1, "quarter": 3, "year": 12}[a.period]
    # Align buckets to the calendar, not to the first ride, so a label like
    # "Q4 '21" means the actual quarter rather than an offset from October.
    base = (min(m for _, m in tracks) // div) * div
    runs, per_month = build_runs(tracks, a.grid, a.simplify, div, base)
    stats, mx = render(runs, per_month, tracks, a.theme, a.out, a.grid,
                       (a.glow_width, a.glow_opacity, a.core_width, a.core_lighten),
                       div, base)
    print(f"{stats['trips']:,} trips → {stats['stretches']:,} stretches, "
          f"{stats['paths']:,} paths in {stats['shapes']:,} shapes")
    print(f"busiest stretch: {mx:,} trips")
    print(f"→ {a.out}")

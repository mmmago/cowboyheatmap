# Cowboy Heatmap

A Strava-style heatmap of every ride your [Cowboy](https://cowboy.com) e-bike has
ever recorded, built from your own data and rendered as a single self-contained
HTML file.


<p align="center"> <img width="580" height="456" alt="map" src="https://github.com/user-attachments/assets/62a4bea9-4c89-4d48-9b2f-3513819511ac" />
 </p>


Brighter and thicker means more separate trips down that stretch of road. The
white core is the commute; the crimson capillaries are the streets ridden once.

---

## Quick start

```bash
git clone https://github.com/mmmago/cowboyheatmap.git
cd cowboyheatmap

python3 make_heatmap.py
```

It asks for your Cowboy email and password (the same ones you use in the app),
downloads your rides, and writes `heatmap.html`. Open that in any browser.

The first run downloads your entire history, might take some time.

## Options

```bash
python3 make_heatmap.py --skip-fetch          # rebuild from cache, no network
python3 make_heatmap.py --grid 5              # finer detail (heavier file)
python3 make_heatmap.py --grid 20             # coarser, lighter
```

The lines carry a **flow** overlay: bright dashes crawling along each stretch in
the direction the ride was actually made. Toggle it with FLOW ON/OFF in the
panel — it repaints continuously, so turn it off on a laptop running on battery.
It starts off if your system asks for reduced motion, and it is left out of the
exported PNG, where a frozen dash pattern would only read as a dashed line.

`--grid` is the main dial. It sets how finely GPS points are bucketed when
counting. Below about 20 m the grid gets finer than urban GPS error, so repeated
passes down one street stop sharing a cell and the road smears into parallel
lines; much above 30 m and neighbouring streets start merging into one.

## Privacy
`routes.geojson` is a precise record of everywhere you have cycled`cowboy_cache/token.json` holds a live API token.

The scripts never send your credentials anywhere except Cowboy's own login
endpoint, and the password is read with `getpass`, so it is not echoed and never
written to disk.

## Notes

The routes have been extracted following [cowboybike-strava-sync](https://github.com/sam-dumont/cowboybike-strava-sync).

Basemap tiles are [OpenFreeMap](https://openfreemap.org) (keyless), rendered with
[MapLibre GL](https://maplibre.org). Map data © OpenStreetMap contributors.

Not affiliated with or endorsed by Cowboy.

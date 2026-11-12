# Nearest Sky weather hints

Hourly maps of where a weather model shows rain or snow at the places the
[Nearest Sky](https://nearestsky.com) app knows. The app downloads the map for the current hour
and uses it to decide which places to ask Apple Weather about first; every result it shows is
still an Apple Weather reading. Everyone gets the same file, and nothing about the user is sent.

The same job publishes the model's total cloud cover for each hour, which the app draws over the
map it shows with a result.

The maps are built from the NOAA/NCEP Global Forecast System, which is public domain. They hold
no Apple Weather data.

## What is published

```
https://data.nearestsky.com/v1/status.json                  run, hours, cloud hours and atlases of the current set
https://data.nearestsky.com/v1/<atlas>/<YYYYMMDDHH>.bin     one map per hour (UTC), about 22 KB
https://data.nearestsky.com/v1/clouds/<YYYYMMDDHH>.bin      the cloud cover of that hour, about 260 KB
```

`<atlas>` is the fingerprint of the app's place list (`atlases/<atlas>.tsv`). A file holds one
byte per place, in the order of that list, after a 40-byte header; the layout is described at the
top of `hintmap/hintfile.py`, and the app reads it with `WeatherHintFile` (in the app's repository).

Each byte says what the model shows at the place (dry, rain, snow, mixed, freezing rain), whether
the precipitation is showery, and how near the nearest model rain and snow are (within 30 km,
within 100 km, or neither). How the model fields become these bytes is described at the top of
`hintmap/classify.py`.

A cloud file is the model's total cloud cover over the whole globe: the 0.25 degree field
smoothed and thinned to a 0.5 degree grid (720 × 361 points, rows from 90° N, columns from 0° E),
one byte per point (0 to 100 percent, 255 where the model has nothing), after a 48-byte header.
The layout is described at the top of `hintmap/cloudfile.py`; the app reads it with
`CloudLayerFile`. A forecast's cloud cover is smooth at about 50 km: it is not a satellite photo.

## How it is built

`.github/workflows/publish.yml` runs three times after each GFS run (00, 06, 12 and 18 UTC; the
files reach NOAA's mirror on AWS about four hours later). It takes the newest complete run, builds
forecast hours +3 to +18 for every atlas, and deploys them with GitHub Pages, which replaces the
whole site. A run that is already published, cloud files included, is skipped. A map that looks wrong (places without
data, rain at almost half the places, no precipitation anywhere, cloud cover missing anywhere or
averaging below 20 % or above 90 % over the globe) is not published, and neither is a run older
than eleven hours: the job fails, GitHub sends a failure email, and the site keeps the previous
maps. When nothing current is published the app simply searches without a map, and its maps show
no clouds.

Each published run's hint maps are also kept as `gfs-<run>.tar.gz` in a monthly release
(`archive-YYYYMM`), so that they can be compared with what was really observed later. The cloud
files are not archived.

The builder is plain Python (3.9 or newer, standard library only), GRIB2 decoding included
(`hintmap/grib2.py`). To run it locally:

```sh
python3 -I -m unittest discover -s tests
python3 -I hints.py live --site _site                    # what the job does
python3 -I hints.py backfill --places atlases/<atlas>.tsv --out maps --cache cache 2026-01-15T12
```

`backfill` builds maps for past hours from the run a live map would have had then (one that
started at least five hours earlier); the app's benchmark uses them to measure what the maps are
worth (`Tools/SearchBench` in the app's repository).

## A new atlas

When the app's place list changes, export it before the app version that carries it is released:

```sh
python3 -I hints.py atlas <app>/Packages/NearestSkyCore/Sources/NearestSkyCore/Resources/places.tsv atlases
```

Keep the old atlas as long as app versions that use it are in use: each gets its own folder.

## Looking after it

GitHub turns off scheduled workflows in a public repository after 60 days without a commit, and
its email about that is easy to miss. The app's Settings show the time of the map in use; if it
is old, open the Actions tab, choose Publish and press "Enable workflow". A look at the Actions
tab every six weeks, or any commit, keeps it running.

## Credits

- Weather model: NOAA/NCEP Global Forecast System (GFS), 0.25 degree, public domain, read from
  the NOAA Open Data Dissemination programme on AWS (`noaa-gfs-bdp-pds`).
- Places: derived from GeoNames (geonames.org), CC BY 4.0.

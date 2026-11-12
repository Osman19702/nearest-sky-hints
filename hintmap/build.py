"""Builds weather hint files from NOAA GFS forecasts.

    python3 -m hintmap.build live --site <folder> [--atlases atlases] [--status-url URL]
        The scheduled job: the newest complete GFS run, forecast hours +3 to +18, every atlas in
        `atlases/`, written as <site>/v1/<atlas>/<YYYYMMDDHH>.bin plus <site>/v1/status.json.
        Exits 0 without building when `--status-url` already names that run, with its cloud files
        (nothing new), and
        with an error when the newest run is older than it should be or a map looks implausible,
        so that the published site stays as it is.

    python3 -m hintmap.build backfill --places <atlas tsv> --out <folder> [--cache <folder>] [--min-lead 5] <hours …|@file>
        Hint files for past hours, each from the newest run that a live map would have had at that
        hour (one that started at least `--min-lead` hours earlier). Hours look like 2026-01-15T12.

    python3 -m hintmap.build atlas <the app's places.tsv> <atlases folder>
        Exports an atlas of the app for the job (id, latitude, longitude, elevation).

Plain Python 3.9 or newer, standard library only.
"""
import argparse
import calendar
import datetime
import json
import os
import sys
import time
import urllib.request

from . import atlas, classify, cloudfile, gfs, hintfile

SOURCE = "gfs0p25"
FIRST_HOUR, LAST_HOUR = 3, 18
# The newest complete run is normally 4 to 10 hours old; older means NOAA or this job is behind.
OLDEST_ACCEPTABLE_RUN_HOURS = 11
# A map outside these shares of the places is not published (a decoding or data problem).
MOST_RAIN_SHARE, MOST_SNOW_SHARE = 0.45, 0.35
# Nor is a cloud layer whose global mean cover lies outside this range (the Earth's is about 65 %).
CLOUD_MEAN_RANGE = (20.0, 90.0)
UTC = datetime.timezone.utc


def log(message):
    print(message, flush=True)


def cells_for(run, forecast_hour, atlases, hoods_by_atlas, cache_dir=None, fields=None):
    """{atlas name: cells} for one forecast hour of one run. `fields` are the decoded messages of
    that hour when the caller has them already."""
    if fields is None:
        fields = gfs.load_fields(run, forecast_hour, log=log, cache_dir=cache_dir)
    grid = classify.Grid.of(fields[gfs.TEMPERATURE])
    values = {}
    for field in gfs.HINT_FIELDS:
        message = fields[field]
        if classify.Grid.of(message) != grid:
            raise ValueError("%s is on another grid" % field[0])
        values[field] = message.values()
    kinds = classify.grid_kinds(values[gfs.PRECIPITATION_RATE], values[gfs.RAIN_FLAG], values[gfs.SNOW_FLAG],
                                values[gfs.ICE_PELLET_FLAG], values[gfs.FREEZING_RAIN_FLAG], values[gfs.TEMPERATURE])
    result = {}
    for name, places in atlases:
        hoods = hoods_by_atlas.get(name)
        if hoods is None or hoods.grid != grid:
            started = time.time()
            hoods = classify.Neighbourhoods(grid, places)
            hoods_by_atlas[name] = hoods
            log("neighbourhoods of atlas %s: %d places, %.1f s" % (name, len(places), time.time() - started))
        result[name] = classify.place_cells(places, hoods, kinds, values[gfs.PRECIPITATION_RATE], values[gfs.CONVECTIVE_RATE],
                                            values[gfs.TEMPERATURE], values[gfs.TERRAIN_HEIGHT])
    return result


def plausibility_problem(cells):
    counts = classify.summary(cells)
    total = max(1, len(cells))
    if counts.get(hintfile.NO_DATA, 0):
        return "%d places without data" % counts[hintfile.NO_DATA]
    if counts.get(hintfile.RAIN, 0) / total > MOST_RAIN_SHARE:
        return "rain at %.0f %% of the places" % (100.0 * counts[hintfile.RAIN] / total)
    if counts.get(hintfile.SNOW, 0) / total > MOST_SNOW_SHARE:
        return "snow at %.0f %% of the places" % (100.0 * counts[hintfile.SNOW] / total)
    if counts.get(hintfile.RAIN, 0) + counts.get(hintfile.SNOW, 0) + counts.get(hintfile.MIXED, 0) == 0:
        return "no precipitation anywhere"
    return None


def cloud_file(message, valid, run, generated):
    """The bytes of the cloud file for one hour, and the problem with it if it looks wrong."""
    grid = classify.Grid.of(message)
    values, width, height = cloudfile.halve(message.values(), grid.ni, grid.nj)
    cells = cloudfile.quantise(values)
    known = [value for value in cells if value != cloudfile.NO_DATA]
    mean = sum(known) / len(known) if known else 0.0
    problem = None
    if len(known) < len(cells) or not (CLOUD_MEAN_RANGE[0] <= mean <= CLOUD_MEAN_RANGE[1]):
        problem = "cloud cover: mean %.0f %%, %d cells without data" % (mean, len(cells) - len(known))
    data = cloudfile.encode(cells, width, height, grid.la1, grid.lo1, grid.dj * 2, grid.di * 2, calendar.timegm(valid.timetuple()),
                            calendar.timegm(run.timetuple()), calendar.timegm(generated.timetuple()), SOURCE)
    return data, problem, mean


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%MZ")


def file_name(valid):
    return valid.strftime("%Y%m%d%H") + ".bin"


def write_hint(path, places, cells, valid, run, generated):
    hintfile.write(path, [place.id for place in places], cells, calendar.timegm(valid.timetuple()), calendar.timegm(run.timetuple()),
                   calendar.timegm(generated.timetuple()), SOURCE)


def published_status(status_url):
    """The published site's status.json, or None when it cannot be read."""
    try:
        request = urllib.request.Request(status_url, headers={"User-Agent": gfs.USER_AGENT, "Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=20) as response:
            status = json.loads(response.read().decode("utf-8"))
            return status if isinstance(status, dict) else None
    except Exception:  # unreachable before the first deployment, or while DNS is being set up
        return None


def already_published(status, run):
    """True when the published site holds everything this builder makes for `run`. A site built
    before the cloud files existed names the run but lists no cloud hours: it is rebuilt at once
    rather than at the next run."""
    return bool(status) and status.get("run") == stamp(run) and bool(status.get("clouds"))


def set_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as out:
            out.write("%s=%s\n" % (name, value))


INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nearest Sky weather hints</title></head>
<body style="font: 16px/1.5 -apple-system, sans-serif; max-width: 40em; margin: 2em auto; padding: 0 1em">
<h1>Nearest Sky weather hints</h1>
<p>Hourly maps of where a weather model shows rain or snow at the places the Nearest Sky app knows,
and of the model's cloud cover over the globe. The app uses the first only to decide where to look first
(every result it shows is confirmed by Apple Weather) and draws the second on its map.</p>
<p>Built from the NOAA/NCEP Global Forecast System (public domain). Place list derived from GeoNames (CC BY 4.0).
Status: <a href="v1/status.json">v1/status.json</a>.</p>
</body></html>
"""


def live(arguments):
    now = datetime.datetime.now(UTC)
    run = gfs.newest_complete_run(now, LAST_HOUR)
    if run is None:
        log("no complete GFS run among the last four")
        return 1
    age = (now - run).total_seconds() / 3600.0
    log("newest complete run: %s (%.1f h old)" % (stamp(run), age))
    if arguments.status_url and already_published(published_status(arguments.status_url), run):
        log("that run is already published")
        set_output("changed", "false")
        return 0
    if age > OLDEST_ACCEPTABLE_RUN_HOURS:
        log("the newest run is older than %d h: not publishing" % OLDEST_ACCEPTABLE_RUN_HOURS)
        return 1
    atlases = atlas.all_in(arguments.atlases)
    if not atlases:
        log("no atlas in %s" % arguments.atlases)
        return 1
    generated = datetime.datetime.now(UTC).replace(microsecond=0)
    hoods = {}
    hours = []
    for forecast_hour in range(FIRST_HOUR, LAST_HOUR + 1):
        valid = run + datetime.timedelta(hours=forecast_hour)
        fields = gfs.load_fields(run, forecast_hour, fields=gfs.LIVE_FIELDS, log=log)
        clouds, problem, mean_cover = cloud_file(fields[gfs.TOTAL_CLOUD], valid, run, generated)
        if problem:
            log("%s for %s: %s; not publishing" % (stamp(valid), stamp(run), problem))
            return 1
        cloud_folder = os.path.join(arguments.site, "v1", "clouds")
        os.makedirs(cloud_folder, exist_ok=True)
        with open(os.path.join(cloud_folder, file_name(valid)), "wb") as out:
            out.write(clouds)
        cells_by_atlas = cells_for(run, forecast_hour, atlases, hoods, fields=fields)
        for name, places in atlases:
            cells = cells_by_atlas[name]
            problem = plausibility_problem(cells)
            if problem:
                log("%s for %s, atlas %s: %s; not publishing" % (stamp(valid), stamp(run), name, problem))
                return 1
            folder = os.path.join(arguments.site, "v1", name)
            os.makedirs(folder, exist_ok=True)
            write_hint(os.path.join(folder, file_name(valid)), places, cells, valid, run, generated)
        counts = classify.summary(cells_by_atlas[atlases[0][0]])
        log("  %s: rain %d, snow %d, mixed %d, freezing %d, mean cloud cover %.0f %%" % (
            stamp(valid), counts.get(hintfile.RAIN, 0), counts.get(hintfile.SNOW, 0), counts.get(hintfile.MIXED, 0),
            counts.get(hintfile.FREEZING_RAIN, 0), mean_cover))
        hours.append(file_name(valid)[:-4])
    status = {"format": hintfile.VERSION, "source": SOURCE, "run": stamp(run), "generated": stamp(generated), "hours": hours,
              "atlases": [name for name, _ in atlases], "clouds": hours}
    with open(os.path.join(arguments.site, "v1", "status.json"), "w", encoding="utf-8") as out:
        json.dump(status, out, indent=1)
    with open(os.path.join(arguments.site, "index.html"), "w", encoding="utf-8") as out:
        out.write(INDEX_HTML)
    # Pages serves "_"-prefixed folders only without Jekyll; there are none, but say so explicitly.
    open(os.path.join(arguments.site, ".nojekyll"), "w").close()
    set_output("changed", "true")
    set_output("run", run.strftime("%Y%m%d%H"))
    log("published run %s: %d hours for %d atlas(es)" % (stamp(run), len(hours), len(atlases)))
    return 0


def parse_hour(text):
    text = text.strip()
    return datetime.datetime(int(text[0:4]), int(text[5:7]), int(text[8:10]), int(text[11:13]), tzinfo=UTC)


def backfill(arguments):
    hours = []
    for item in arguments.hours:
        if item.startswith("@"):
            with open(item[1:], encoding="utf-8") as source:
                hours.extend(line.strip() for line in source if line.strip())
        else:
            hours.extend(part for part in item.split(",") if part)
    places = atlas.read(arguments.places)
    atlases = [(atlas.name(places), places)]
    os.makedirs(arguments.out, exist_ok=True)
    hoods = {}
    failures = 0
    for text in hours:
        valid = parse_hour(text)
        path = os.path.join(arguments.out, file_name(valid))
        if os.path.exists(path):
            continue
        run = gfs.run_before(valid, arguments.min_lead)
        forecast_hour = int(round((valid - run).total_seconds() / 3600.0))
        started = time.time()
        try:
            cells = cells_for(run, forecast_hour, atlases, hoods, cache_dir=arguments.cache)[atlases[0][0]]
        except (gfs.FetchError, ValueError) as error:
            log("%s: %s" % (stamp(valid), error))
            failures += 1
            continue
        write_hint(path, places, cells, valid, run, datetime.datetime.now(UTC).replace(microsecond=0))
        counts = classify.summary(cells)
        log("%s from run %s (+%d h): rain %d, snow %d, mixed %d | %.1f s" % (
            stamp(valid), stamp(run), forecast_hour, counts.get(hintfile.RAIN, 0), counts.get(hintfile.SNOW, 0), counts.get(hintfile.MIXED, 0),
            time.time() - started))
    return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hintmap.build", description="Builds weather hint files from NOAA GFS.")
    commands = parser.add_subparsers(dest="command", required=True)
    live_parser = commands.add_parser("live")
    live_parser.add_argument("--site", required=True)
    live_parser.add_argument("--atlases", default="atlases")
    live_parser.add_argument("--status-url")
    back_parser = commands.add_parser("backfill")
    back_parser.add_argument("--places", required=True)
    back_parser.add_argument("--out", required=True)
    back_parser.add_argument("--cache")
    back_parser.add_argument("--min-lead", type=float, default=5.0)
    back_parser.add_argument("hours", nargs="+")
    atlas_parser = commands.add_parser("atlas")
    atlas_parser.add_argument("places")
    atlas_parser.add_argument("folder")
    arguments = parser.parse_args(argv)
    if arguments.command == "live":
        return live(arguments)
    if arguments.command == "backfill":
        return backfill(arguments)
    log("wrote %s" % atlas.export(arguments.places, arguments.folder))
    return 0


if __name__ == "__main__":
    sys.exit(main())

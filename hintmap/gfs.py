"""Finds and fetches fields of NOAA's Global Forecast System (GFS), 0.25 degree grid.

The files are public domain and mirrored on public cloud storage, run by run, with an index beside
each file that says at which byte every field starts. So a field is one small ranged download:
this module reads the index, asks for the bytes of the fields it wants and decodes them.

Runs start at 00, 06, 12 and 18 UTC; the forecast hours this builder uses appear on the mirror
about 3 h 35 min to 3 h 55 min after the run's start (measured over ten runs in November 2026).
"""
import datetime
import os
import time
import urllib.error
import urllib.request

from . import grib2

# NOAA's bucket in the AWS Open Data programme, and the same tree on Google Cloud as a fallback.
MIRRORS = ["https://noaa-gfs-bdp-pds.s3.amazonaws.com", "https://storage.googleapis.com/global-forecast-system"]
USER_AGENT = "hintmap/1.0"
UTC = datetime.timezone.utc

# The fields a hint needs, as (name in the index, level in the index).
PRECIPITATION_RATE = ("PRATE", "surface")
CONVECTIVE_RATE = ("CPRAT", "surface")
SNOW_FLAG = ("CSNOW", "surface")
ICE_PELLET_FLAG = ("CICEP", "surface")
FREEZING_RAIN_FLAG = ("CFRZR", "surface")
RAIN_FLAG = ("CRAIN", "surface")
TEMPERATURE = ("TMP", "2 m above ground")
TERRAIN_HEIGHT = ("HGT", "surface")
HINT_FIELDS = [PRECIPITATION_RATE, CONVECTIVE_RATE, SNOW_FLAG, ICE_PELLET_FLAG, FREEZING_RAIN_FLAG, RAIN_FLAG, TEMPERATURE, TERRAIN_HEIGHT]
# What each field is in GRIB2 terms (discipline, category, number): every decoded message is
# checked against it, so an index that points at the wrong bytes cannot slip a wrong field in.
CODES = {PRECIPITATION_RATE: (0, 1, 7), CONVECTIVE_RATE: (0, 1, 37), SNOW_FLAG: (0, 1, 195), ICE_PELLET_FLAG: (0, 1, 194),
         FREEZING_RAIN_FLAG: (0, 1, 193), RAIN_FLAG: (0, 1, 192), TEMPERATURE: (0, 0, 0), TERRAIN_HEIGHT: (0, 3, 5)}


class FetchError(RuntimeError):
    """A file or field could not be had from any mirror."""


def run_path(run, forecast_hour):
    """Path of one forecast hour's file under a mirror: gfs.YYYYMMDD/HH/atmos/gfs.tHHz.pgrb2.0p25.fFFF"""
    return "gfs.%s/%02d/atmos/gfs.t%02dz.pgrb2.0p25.f%03d" % (run.strftime("%Y%m%d"), run.hour, run.hour, forecast_hour)


def fetch(url, first_byte=None, last_byte=None, timeout=60, attempts=3):
    """The body of `url`, or of the inclusive byte range; raises `FetchError` (with the HTTP status) on failure."""
    headers = {"User-Agent": USER_AGENT}
    if first_byte is not None:
        headers["Range"] = "bytes=%d-%s" % (first_byte, "" if last_byte is None else last_byte)
    problem = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(2 * attempt)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
                body = response.read()
            if first_byte is not None and last_byte is not None and len(body) != last_byte - first_byte + 1:
                problem = "short answer (%d bytes)" % len(body)
                continue
            return body
        except urllib.error.HTTPError as error:
            if error.code in (403, 404):
                raise FetchError("%s: HTTP %d" % (url, error.code)) from error
            problem = "HTTP %d" % error.code
        except (urllib.error.URLError, OSError) as error:
            problem = str(error)
    raise FetchError("%s: %s" % (url, problem))


def parse_index(text):
    """The entries of a `.idx` file: dicts with number, first and last byte, name, level and forecast label.

    A line looks like `593:421757219:d=2026111206:PRATE:surface:6 hour fcst:`. The last entry has
    no `last` byte (the field runs to the end of the file).
    """
    entries = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        entries.append({"number": int(parts[0]), "first": int(parts[1]), "last": None, "date": parts[2], "name": parts[3],
                        "level": parts[4], "forecast": parts[5]})
    for entry, following in zip(entries, entries[1:]):
        if following["first"] > entry["first"]:
            entry["last"] = following["first"] - 1
    return entries


def forecast_label(forecast_hour):
    """How the index labels an instantaneous field of that forecast hour."""
    return "anl" if forecast_hour == 0 else "%d hour fcst" % forecast_hour


def select(entries, fields, forecast_hour):
    """The index entries of `fields` (name, level) valid at `forecast_hour` itself (not averages), by field."""
    label = forecast_label(forecast_hour)
    chosen = {}
    for entry in entries:
        key = (entry["name"], entry["level"])
        if key in fields and entry["forecast"] == label and key not in chosen:
            chosen[key] = entry
    missing = [field for field in fields if field not in chosen]
    if missing:
        raise FetchError("the index has no %s for %s" % (", ".join("%s at %s" % field for field in missing), label))
    return chosen


def ranges(entries):
    """Byte ranges that cover `entries`, with neighbours merged into one request."""
    merged = []
    for entry in sorted(entries, key=lambda item: item["first"]):
        if entry["last"] is None:
            raise FetchError("field %s is the last of its file; its length is unknown" % entry["name"])
        if merged and entry["first"] == merged[-1][1] + 1:
            merged[-1][1] = entry["last"]
        else:
            merged.append([entry["first"], entry["last"]])
    return [tuple(item) for item in merged]


def _by_code(messages, fields, forecast_hour):
    """{field: message} for `fields`, matched by GRIB2 code; raises `FetchError` when one is missing or for another hour."""
    wanted = {CODES[field]: field for field in fields}
    if len(wanted) != len(fields):
        raise FetchError("two requested fields share a GRIB2 code")
    result = {}
    for message in messages:
        field = wanted.get((message.discipline, message.category, message.number))
        if field is None or field in result:
            continue
        if message.forecast_time != forecast_hour and not (forecast_hour == 0 and message.forecast_time in (0, None)):
            raise FetchError("%s is for hour %s, not %d" % (field[0], message.forecast_time, forecast_hour))
        result[field] = message
    missing = [field[0] for field in fields if field not in result]
    if missing:
        raise FetchError("no %s among the downloaded messages" % ", ".join(missing))
    return result


def load_fields(run, forecast_hour, fields=None, mirrors=None, log=None, cache_dir=None):
    """Downloads and decodes `fields` of one forecast hour: {(name, level): grib2.Message}.

    Tries each mirror in turn. Raises `FetchError` when the hour is not there (yet) or a field
    is not the one asked for. With `cache_dir` the downloaded bytes are kept there and read back
    on the next call for the same run and hour (the files never change once published).
    """
    fields = list(fields or HINT_FIELDS)
    for field in fields:
        if field not in CODES:
            raise FetchError("no GRIB2 code known for %s at %s" % field)
    cache_path = None
    if cache_dir:
        cache_path = os.path.join(cache_dir, "gfs%s_f%03d.grib2" % (run.strftime("%Y%m%d%H"), forecast_hour))
        if os.path.exists(cache_path):
            with open(cache_path, "rb") as source:
                body = source.read()
            try:
                return _by_code(list(grib2.messages(body)), fields, forecast_hour)
            except (FetchError, grib2.GribError):
                pass  # an older cache with other fields: fetch again
    problem = None
    for mirror in mirrors or MIRRORS:
        base = mirror + "/" + run_path(run, forecast_hour)
        try:
            entries = parse_index(fetch(base + ".idx").decode("ascii", "replace"))
            chosen = select(entries, fields, forecast_hour)
            parts = [fetch(base, first, last) for first, last in ranges(chosen.values())]
            body = b"".join(parts)
            result = _by_code(list(grib2.messages(body)), fields, forecast_hour)
            if log:
                log("%s f%03d: %d fields, %.1f MB from %s" % (run.strftime("%Y-%m-%d %HZ"), forecast_hour, len(result), len(body) / 1e6,
                                                           mirror.split("/")[2]))
            if cache_path:
                os.makedirs(cache_dir, exist_ok=True)
                with open(cache_path + ".part", "wb") as out:
                    out.write(body)
                os.replace(cache_path + ".part", cache_path)
            return result
        except (FetchError, grib2.GribError) as error:
            problem = error
    raise FetchError("f%03d of run %s: %s" % (forecast_hour, run.strftime("%Y-%m-%d %HZ"), problem))


def is_available(run, forecast_hour, mirrors=None):
    """True when the index of that forecast hour can be fetched from a mirror."""
    for mirror in mirrors or MIRRORS:
        try:
            fetch(mirror + "/" + run_path(run, forecast_hour) + ".idx", attempts=1, timeout=20)
            return True
        except FetchError:
            continue
    return False


def run_before(moment, at_least_hours=0.0):
    """The latest run that started at least `at_least_hours` before `moment` (a UTC datetime)."""
    latest = moment - datetime.timedelta(hours=at_least_hours)
    start = latest.replace(minute=0, second=0, microsecond=0)
    return start - datetime.timedelta(hours=start.hour % 6)


def newest_complete_run(now, last_forecast_hour, mirrors=None, look_back=4):
    """The newest run whose file for `last_forecast_hour` is on a mirror, or None among the last `look_back` runs."""
    run = run_before(now)
    for _ in range(look_back):
        if is_available(run, last_forecast_hour, mirrors):
            return run
        run -= datetime.timedelta(hours=6)
    return None

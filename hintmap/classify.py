"""Turns model fields into a hint: what the model shows at every atlas place, and close by.

Per grid point the model says whether it precipitates (the rate) and, by four yes/no flags, as
what (rain, snow, ice pellets, freezing rain). Convective showers often carry no flag at all, so
where the rate says "wet" and no flag is set the 2 m temperature decides.

Per place:
- the class at the place comes from the nearest grid point; where the place lies more than
  `REDECIDE_ABOVE_M` above or below the model's terrain there, rain against snow is decided
  again from the temperature carried to the place's own height (a valley town under a snowy grid
  cell often gets rain);
- the bands say how near the nearest wet grid point of each kind is: within `NEAR_KM`, within
  `FAR_KM`, or neither. Mixed precipitation counts for both kinds.

The thresholds are starting values, measured against airport observations by
`Tools/SearchBench/scripts/calibrate_hints.py` in the app's repository.
"""
import math
from array import array

from . import hintfile

EARTH_RADIUS_KM = 6371.0088
LAPSE_C_PER_M = 0.0065
RAIN_THRESHOLD_MM_H = 0.10
SNOW_THRESHOLD_MM_H = 0.05
SNOW_BELOW_C = 0.5
MIXED_BELOW_C = 2.0
REDECIDE_ABOVE_M = 200.0
NEAR_KM = 30.0
FAR_KM = 100.0

DRY, RAIN, SNOW, MIXED, FREEZING = hintfile.DRY, hintfile.RAIN, hintfile.SNOW, hintfile.MIXED, hintfile.FREEZING_RAIN
FOR_RAIN = bytes([1 if kind in (RAIN, MIXED) else 0 for kind in range(8)])
FOR_SNOW = bytes([1 if kind in (SNOW, MIXED) else 0 for kind in range(8)])


class Grid:
    """A regular latitude/longitude grid that runs west to east and north to south (GFS 0.25)."""

    def __init__(self, ni, nj, la1, lo1, di, dj):
        self.ni, self.nj, self.la1, self.lo1, self.di, self.dj = ni, nj, la1, lo1, di, dj

    @classmethod
    def of(cls, message):
        if message.grid_template != 0 or message.scanning_mode != 0:
            raise ValueError("only west-to-east, north-to-south latitude/longitude grids are supported")
        return cls(message.ni, message.nj, message.la1, message.lo1, message.di, message.dj)

    def __eq__(self, other):
        return isinstance(other, Grid) and (self.ni, self.nj, self.la1, self.lo1, self.di, self.dj) == (
            other.ni, other.nj, other.la1, other.lo1, other.di, other.dj)

    def row_column(self, latitude, longitude):
        row = min(self.nj - 1, max(0, int(round((self.la1 - latitude) / self.dj))))
        column = int(round(((longitude - self.lo1) % 360.0) / self.di)) % self.ni
        return row, column

    def index(self, latitude, longitude):
        row, column = self.row_column(latitude, longitude)
        return row * self.ni + column

    def centre(self, index):
        row, column = divmod(index, self.ni)
        longitude = self.lo1 + column * self.di
        return self.la1 - row * self.dj, (longitude + 180.0) % 360.0 - 180.0


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


class Neighbourhoods:
    """For every place, the grid points within `FAR_KM`, nearest first. Built once per atlas."""

    def __init__(self, grid, places, radius_km=FAR_KM):
        self.grid = grid
        self.nearest = array("I")
        self.starts = array("I", [0])
        self.indices = array("I")
        self.distances = array("f")
        row_span = int(math.ceil(radius_km / (math.radians(grid.dj) * EARTH_RADIUS_KM))) + 1
        for place in places:
            self.nearest.append(grid.index(place.latitude, place.longitude))
            row0, column0 = grid.row_column(place.latitude, place.longitude)
            found = []
            for row in range(max(0, row0 - row_span), min(grid.nj, row0 + row_span + 1)):
                latitude = grid.la1 - row * grid.dj
                width = math.radians(grid.di) * EARTH_RADIUS_KM * max(0.01, math.cos(math.radians(latitude)))
                column_span = min(grid.ni // 2, int(math.ceil(radius_km / width)) + 1)
                seen = set()
                for offset in range(-column_span, column_span + 1):
                    column = (column0 + offset) % grid.ni
                    if column in seen:
                        continue
                    seen.add(column)
                    d = distance_km(place.latitude, place.longitude, latitude, grid.lo1 + column * grid.di)
                    if d <= radius_km:
                        found.append((d, row * grid.ni + column))
            found.sort()
            for d, index in found:
                self.indices.append(index)
                self.distances.append(d)
            self.starts.append(len(self.indices))

    def first_distance(self, place, kinds, wanted):
        """Distance to the nearest grid point whose kind `wanted` marks, or None within the radius."""
        indices, distances = self.indices, self.distances
        for position in range(self.starts[place], self.starts[place + 1]):
            if wanted[kinds[indices[position]]]:
                return distances[position]
        return None


def band(kilometres):
    if kilometres is None:
        return hintfile.BEYOND_100_KM
    if kilometres <= NEAR_KM:
        return hintfile.WITHIN_30_KM
    if kilometres <= FAR_KM:
        return hintfile.WITHIN_100_KM
    return hintfile.BEYOND_100_KM


def phase(temperature_c, rate_mm_h, rain_threshold=RAIN_THRESHOLD_MM_H):
    """Rain, snow or mixed by temperature alone, or dry when it is rain below the rain threshold."""
    if temperature_c <= SNOW_BELOW_C:
        return SNOW
    if temperature_c <= MIXED_BELOW_C:
        return MIXED
    return RAIN if rate_mm_h >= rain_threshold else DRY


def grid_kinds(rate, rain_flag, snow_flag, pellet_flag, freezing_flag, temperature_k,
               rain_threshold=RAIN_THRESHOLD_MM_H, snow_threshold=SNOW_THRESHOLD_MM_H):
    """The kind of every grid point (a bytearray of DRY, RAIN, SNOW, MIXED or FREEZING).

    `rate` is the precipitation rate in kg m-2 s-1 (mm/s); the flags are 0 or 1; the
    temperature is in kelvin. Rain and freezing rain need `rain_threshold` mm/h, snow and mixed
    precipitation `snow_threshold` (snow carries less water per hour for the same weather).
    """
    count = len(rate)
    kinds = bytearray(count)
    lowest = min(rain_threshold, snow_threshold) / 3600.0
    for i in range(count):
        per_second = rate[i]
        if per_second < lowest:
            continue
        r = per_second * 3600.0
        if freezing_flag[i] >= 0.5:
            kind = FREEZING if r >= rain_threshold else DRY
        elif pellet_flag[i] >= 0.5 or (snow_flag[i] >= 0.5 and rain_flag[i] >= 0.5):
            kind = MIXED if r >= snow_threshold else DRY
        elif snow_flag[i] >= 0.5:
            kind = SNOW if r >= snow_threshold else DRY
        elif rain_flag[i] >= 0.5:
            kind = RAIN if r >= rain_threshold else DRY
        else:
            kind = phase(temperature_k[i] - 273.15, r, rain_threshold)
            if kind != RAIN and r < snow_threshold:
                kind = DRY
        kinds[i] = kind
    return kinds


def place_cells(places, hoods, kinds, rate, convective_rate, temperature_k, terrain_m, rain_threshold=RAIN_THRESHOLD_MM_H):
    """One hint byte per place, in atlas order."""
    cells = bytearray(len(places))
    for position, place in enumerate(places):
        i = hoods.nearest[position]
        kind = kinds[i]
        if kind in (RAIN, SNOW, MIXED) and abs(place.elevation - terrain_m[i]) > REDECIDE_ABOVE_M:
            temperature = temperature_k[i] - 273.15 - LAPSE_C_PER_M * (place.elevation - terrain_m[i])
            kind = phase(temperature, rate[i] * 3600.0, rain_threshold)
        showery = kind != DRY and rate[i] > 0 and convective_rate[i] >= 0.5 * rate[i]
        cells[position] = hintfile.cell(kind, showery=showery,
                                        rain_band=band(hoods.first_distance(position, kinds, FOR_RAIN)),
                                        snow_band=band(hoods.first_distance(position, kinds, FOR_SNOW)))
    return cells


def summary(cells):
    """Counts of the classes in `cells`, for logs and the plausibility check."""
    counts = {}
    for byte in cells:
        counts[byte & 7] = counts.get(byte & 7, 0) + 1
    return counts

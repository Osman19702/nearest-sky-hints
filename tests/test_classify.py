"""Tests of the classification from model fields to hint bytes."""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hintmap import atlas, classify, gfs, hintfile  # noqa: E402

PER_HOUR = 1 / 3600.0  # mm/h to kg m-2 s-1


class KindTests(unittest.TestCase):
    def kinds(self, rate_mm_h, rain=0, snow=0, pellets=0, freezing=0, temperature_c=10.0):
        return classify.grid_kinds([rate_mm_h * PER_HOUR], [rain], [snow], [pellets], [freezing], [temperature_c + 273.15])[0]

    def test_flags_decide_the_kind(self):
        self.assertEqual(self.kinds(1.0, rain=1), classify.RAIN)
        self.assertEqual(self.kinds(1.0, snow=1, temperature_c=-3), classify.SNOW)
        self.assertEqual(self.kinds(1.0, rain=1, snow=1), classify.MIXED)
        self.assertEqual(self.kinds(1.0, pellets=1), classify.MIXED)
        self.assertEqual(self.kinds(1.0, freezing=1, rain=1), classify.FREEZING)

    def test_the_rate_decides_wet_or_dry(self):
        self.assertEqual(self.kinds(0.0, rain=1), classify.DRY)
        self.assertEqual(self.kinds(0.08, rain=1), classify.DRY)
        self.assertEqual(self.kinds(0.12, rain=1), classify.RAIN)
        self.assertEqual(self.kinds(0.06, snow=1, temperature_c=-3), classify.SNOW)
        self.assertEqual(self.kinds(0.04, snow=1, temperature_c=-3), classify.DRY)

    def test_without_a_flag_the_temperature_decides(self):
        self.assertEqual(self.kinds(1.0, temperature_c=12), classify.RAIN)
        self.assertEqual(self.kinds(1.0, temperature_c=1.5), classify.MIXED)
        self.assertEqual(self.kinds(1.0, temperature_c=-2), classify.SNOW)
        self.assertEqual(self.kinds(0.06, temperature_c=12), classify.DRY)


class PlaceTests(unittest.TestCase):
    """A 0.25 degree grid of 41 x 41 points around 50 N, 10 E."""

    def setUp(self):
        self.grid = classify.Grid(ni=41, nj=41, la1=55.0, lo1=5.0, di=0.25, dj=0.25)
        count = 41 * 41
        self.rate = [0.0] * count
        self.convective = [0.0] * count
        self.temperature = [283.15] * count
        self.terrain = [100.0] * count
        self.flags = {name: [0] * count for name in ("rain", "snow", "pellets", "freezing")}

    def wet(self, latitude, longitude, mm_h=1.0, flag="rain", convective_share=0.0):
        index = self.grid.index(latitude, longitude)
        self.rate[index] = mm_h * PER_HOUR
        self.convective[index] = convective_share * mm_h * PER_HOUR
        if flag:
            self.flags[flag][index] = 1
        return index

    def cells(self, places):
        hoods = classify.Neighbourhoods(self.grid, places)
        kinds = classify.grid_kinds(self.rate, self.flags["rain"], self.flags["snow"], self.flags["pellets"], self.flags["freezing"],
                                    self.temperature)
        return classify.place_cells(places, hoods, kinds, self.rate, self.convective, self.temperature, self.terrain)

    def test_class_and_bands(self):
        self.wet(50.0, 10.0, convective_share=0.8)
        places = [atlas.Place(1, 50.0, 10.0, 100), atlas.Place(2, 50.0, 10.25, 100), atlas.Place(3, 50.5, 10.0, 100),
                  atlas.Place(4, 52.0, 10.0, 100)]
        cells = self.cells(places)
        # The place itself: rain, showery, rain within 30 km (its own grid point).
        self.assertEqual(cells[0], hintfile.cell(hintfile.RAIN, showery=True, rain_band=hintfile.WITHIN_30_KM))
        # 18 km away: dry, rain within 30 km.
        self.assertEqual(cells[1], hintfile.cell(hintfile.DRY, rain_band=hintfile.WITHIN_30_KM))
        # 56 km away: within 100 km. 222 km away: nothing.
        self.assertEqual(cells[2], hintfile.cell(hintfile.DRY, rain_band=hintfile.WITHIN_100_KM))
        self.assertEqual(cells[3], hintfile.cell(hintfile.DRY))

    def test_a_place_far_below_the_model_terrain_gets_rain_instead_of_snow(self):
        index = self.wet(50.0, 10.0, flag="snow")
        self.temperature[index] = 273.15 - 1.0
        self.terrain[index] = 1500.0
        valley = atlas.Place(1, 50.0, 10.0, 300)        # 1,200 m lower: about 7.8 degrees warmer
        summit = atlas.Place(2, 50.0, 10.0, 1450)       # within 200 m of the terrain: the model's own verdict
        cells = self.cells([valley, summit])
        self.assertEqual(cells[0] & 7, hintfile.RAIN)
        self.assertEqual((cells[0] >> 6) & 3, hintfile.WITHIN_30_KM, "the snow up the hill is still close by")
        self.assertEqual(cells[1] & 7, hintfile.SNOW)

    def test_mixed_counts_for_both_kinds_nearby(self):
        self.wet(50.0, 10.25, flag="pellets")
        cells = self.cells([atlas.Place(1, 50.0, 10.0, 100)])
        self.assertEqual((cells[0] >> 4) & 3, hintfile.WITHIN_30_KM)
        self.assertEqual((cells[0] >> 6) & 3, hintfile.WITHIN_30_KM)


class GfsIndexTests(unittest.TestCase):
    INDEX = "\n".join([
        "1:0:d=2026111206:PRMSL:mean sea level:6 hour fcst:",
        "2:1000:d=2026111206:TMP:2 m above ground:6 hour fcst:",
        "3:2500:d=2026111206:PRATE:surface:6 hour fcst:",
        "4:3000:d=2026111206:PRATE:surface:0-6 hour ave fcst:",
        "5:3600:d=2026111206:CRAIN:surface:6 hour fcst:",
        "6:3700:d=2026111206:CSNOW:surface:6 hour fcst:",
        "7:3800:d=2026111206:TCDC:entire atmosphere:6 hour fcst:",
    ])

    def test_reads_the_index_and_picks_the_instantaneous_fields(self):
        entries = gfs.parse_index(self.INDEX)
        self.assertEqual(len(entries), 7)
        self.assertEqual((entries[1]["first"], entries[1]["last"]), (1000, 2499))
        self.assertIsNone(entries[-1]["last"])
        chosen = gfs.select(entries, [gfs.TEMPERATURE, gfs.PRECIPITATION_RATE, gfs.RAIN_FLAG, gfs.SNOW_FLAG], 6)
        self.assertEqual(chosen[gfs.PRECIPITATION_RATE]["first"], 2500)
        # Neighbours merge into one request; the averaged rate between them is not asked for.
        self.assertEqual(gfs.ranges(chosen.values()), [(1000, 2999), (3600, 3799)])
        with self.assertRaises(gfs.FetchError):
            gfs.select(entries, [gfs.ICE_PELLET_FLAG], 6)

    def test_the_run_a_live_map_would_have(self):
        utc = datetime.timezone.utc
        def run(hour, lead=5.0):
            return gfs.run_before(datetime.datetime(2026, 11, 12, hour, tzinfo=utc), lead)
        self.assertEqual(run(11), datetime.datetime(2026, 11, 12, 6, tzinfo=utc))
        self.assertEqual(run(10), datetime.datetime(2026, 11, 12, 0, tzinfo=utc))
        self.assertEqual(run(4), datetime.datetime(2026, 11, 11, 18, tzinfo=utc))
        self.assertEqual(gfs.run_path(run(11), 5), "gfs.20261112/06/atmos/gfs.t06z.pgrb2.0p25.f005")


if __name__ == "__main__":
    unittest.main()

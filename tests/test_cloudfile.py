"""Tests of hintmap/cloudfile.py.

The golden file is the one the app's `CloudLayerTests.swift` decodes: if this test and that one
both pass, this writer and the app's reader agree.
"""
import calendar
import datetime
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hintmap import build  # noqa: E402
from hintmap import cloudfile as c  # noqa: E402
from hintmap import gfs  # noqa: E402

# A 3 x 2 grid: rows at 90 N and 0, columns at 180 W, 60 W and 60 E; valid 2026-11-12 14:00 UTC.
GOLDEN_HEX = ("4e53434c0130000003000200804a5d05006b45f5804a5d05000e270760c6f56ae055f56a638ef56a"
              "67667330703235000019324b64ff")
GOLDEN_CELLS = [0, 25, 50, 75, 100, 255]
VALID = calendar.timegm((2026, 11, 12, 14, 0, 0))
RUN = calendar.timegm((2026, 11, 12, 6, 0, 0))
GENERATED = calendar.timegm((2026, 11, 12, 10, 1, 7))


class CloudFileTests(unittest.TestCase):
    def test_writes_and_reads_the_golden_file(self):
        data = c.encode(GOLDEN_CELLS, 3, 2, 90.0, -180.0, 90.0, 120.0, VALID, RUN, GENERATED, "gfs0p25")
        self.assertEqual(data.hex(), GOLDEN_HEX)
        cloud = c.decode(bytes.fromhex(GOLDEN_HEX))
        self.assertEqual((cloud["width"], cloud["height"], cloud["first_latitude"], cloud["first_longitude"]), (3, 2, 90.0, -180.0))
        self.assertEqual((cloud["latitude_step"], cloud["longitude_step"], cloud["valid"], cloud["run"], cloud["generated"]),
                         (90.0, 120.0, VALID, RUN, GENERATED))
        self.assertEqual(cloud["source"], "gfs0p25")
        self.assertEqual(list(cloud["cells"]), GOLDEN_CELLS)

    def test_rejects_what_it_does_not_know(self):
        golden = bytes.fromhex(GOLDEN_HEX)
        for broken in (golden[:47], b"XSCL" + golden[4:], golden[:4] + b"\x02" + golden[5:], golden[:6] + b"\x01" + golden[7:],
                       golden[:-1], golden + b"\0"):
            with self.assertRaises(ValueError):
                c.decode(broken)
        with self.assertRaises(ValueError):
            c.encode(GOLDEN_CELLS[:-1], 3, 2, 90.0, -180.0, 90.0, 120.0, VALID, RUN, GENERATED, "gfs0p25")
        with self.assertRaises(ValueError):
            c.encode([101] * 6, 3, 2, 90.0, -180.0, 90.0, 120.0, VALID, RUN, GENERATED, "gfs0p25")
        with self.assertRaises(ValueError):
            c.encode(GOLDEN_CELLS, 3, 2, 90.0, -180.0, 90.0, 120.0, VALID + 60, RUN, GENERATED, "gfs0p25")

    def test_halving_keeps_the_grid_and_smooths(self):
        # 8 x 5 grid (an even width and an odd height, like 1440 x 721): a single cloudy cell.
        width, height = 8, 5
        values = [0.0] * (width * height)
        values[2 * width + 4] = 100.0
        halved, out_width, out_height = c.halve(values, width, height)
        self.assertEqual((out_width, out_height), (4, 3))
        # The output cell on top of the cloudy one gets the centre weight (4 of 16).
        self.assertAlmostEqual(halved[1 * out_width + 2], 25.0)
        # A uniform field stays uniform, across the date line and at the poles.
        uniform, _, _ = c.halve([40.0] * (width * height), width, height)
        self.assertTrue(all(abs(value - 40.0) < 1e-9 for value in uniform))
        # Missing values are left out of the average; a cell with nothing around it has no data.
        holes = [float("nan")] * (width * height)
        holes[0] = 80.0
        halved, _, _ = c.halve(holes, width, height)
        self.assertAlmostEqual(halved[0], 80.0)
        self.assertTrue(math.isnan(halved[2]))
        self.assertEqual(list(c.quantise([12.4, 12.6, -3.0, 140.0, float("nan")])), [12, 13, 0, 100, c.NO_DATA])

    def test_total_cloud_is_a_live_field_with_its_grib_code(self):
        self.assertIn(gfs.TOTAL_CLOUD, gfs.LIVE_FIELDS)
        self.assertNotIn(gfs.TOTAL_CLOUD, gfs.HINT_FIELDS)
        self.assertEqual(gfs.CODES[gfs.TOTAL_CLOUD], (0, 6, 1))

    def test_a_run_published_without_clouds_is_built_again(self):
        run = datetime.datetime(2026, 11, 12, 18, tzinfo=datetime.timezone.utc)
        with_clouds = {"run": "2026-11-12T18:00Z", "hours": ["2026111221"], "clouds": ["2026111221"]}
        self.assertTrue(build.already_published(with_clouds, run))
        # The site as the first builder left it: the same run, no cloud files.
        self.assertFalse(build.already_published({"run": "2026-11-12T18:00Z", "hours": ["2026111221"]}, run))
        self.assertFalse(build.already_published(dict(with_clouds, run="2026-11-12T12:00Z"), run))
        self.assertFalse(build.already_published(None, run), "no site yet, or unreachable")


if __name__ == "__main__":
    unittest.main()

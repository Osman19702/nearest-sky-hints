#!/usr/bin/env python3
"""Tests of hintmap/hintfile.py, a copy of the app repository's Tools/SearchBench/scripts/hintfile.py.

The golden file is the one the app's `WeatherHintFileTests.swift` decodes: if this test and that
one both pass, this writer and the app's reader agree.
"""
import calendar
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hintmap import hintfile as h  # noqa: E402

GOLDEN_HEX = "4e53484d012800007f224e01795242d20600000060c6f56ae055f56a638ef56a6766733070323500002982631007"
GOLDEN_IDS = [101, 202, 303, 404, 505, 606]
GOLDEN_CELLS = [
    h.cell(h.DRY),
    h.cell(h.RAIN, showery=True, rain_band=h.WITHIN_30_KM),
    h.cell(h.SNOW, snow_band=h.WITHIN_30_KM),
    h.cell(h.MIXED, rain_band=h.WITHIN_30_KM, snow_band=h.WITHIN_100_KM),
    h.cell(h.DRY, rain_band=h.WITHIN_100_KM),
    h.cell(h.NO_DATA),
]
VALID = calendar.timegm((2026, 11, 12, 14, 0, 0))
RUN = calendar.timegm((2026, 11, 12, 6, 0, 0))
GENERATED = calendar.timegm((2026, 11, 12, 10, 1, 7))


class HintFileTests(unittest.TestCase):
    def test_writes_the_golden_file(self):
        data = h.encode(GOLDEN_IDS, GOLDEN_CELLS, VALID, RUN, GENERATED, "gfs0p25")
        self.assertEqual(data.hex(), GOLDEN_HEX)

    def test_reads_the_golden_file(self):
        hint = h.decode(bytes.fromhex(GOLDEN_HEX))
        self.assertEqual(hint["fingerprint"], h.fingerprint(GOLDEN_IDS))
        self.assertEqual((hint["count"], hint["valid"], hint["run"], hint["generated"], hint["source"]),
                         (6, VALID, RUN, GENERATED, "gfs0p25"))
        self.assertEqual(list(hint["cells"]), GOLDEN_CELLS)

    def test_fingerprint(self):
        self.assertEqual(h.fingerprint(GOLDEN_IDS), 0xD2425279014E227F)
        self.assertEqual(h.fingerprint([]), 0xCBF29CE484222325)
        self.assertNotEqual(h.fingerprint(GOLDEN_IDS), h.fingerprint(list(reversed(GOLDEN_IDS))))

    def test_every_atlas_matches_its_name(self):
        folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "atlases")
        names = [name for name in os.listdir(folder) if name.endswith(".tsv")]
        self.assertTrue(names)
        for name in names:
            self.assertEqual("%016x" % h.fingerprint(h.atlas_ids(os.path.join(folder, name))), name[:-4])

    def test_rejects_what_it_does_not_know(self):
        golden = bytes.fromhex(GOLDEN_HEX)
        for broken in (golden[:39], b"XSHM" + golden[4:], golden[:4] + b"\x02" + golden[5:], golden[:6] + b"\x01" + golden[7:],
                       golden[:-1], golden + b"\0"):
            with self.assertRaises(ValueError):
                h.decode(broken)
        with self.assertRaises(ValueError):
            h.encode(GOLDEN_IDS, GOLDEN_CELLS[:-1], VALID, RUN, GENERATED, "gfs0p25")
        with self.assertRaises(ValueError):
            h.encode(GOLDEN_IDS, GOLDEN_CELLS, VALID + 60, RUN, GENERATED, "gfs0p25")
        with self.assertRaises(ValueError):
            h.encode(GOLDEN_IDS, GOLDEN_CELLS, VALID, RUN, GENERATED, "a-label-too-long")
        with self.assertRaises(ValueError):
            h.cell(5)
        with self.assertRaises(ValueError):
            h.cell(h.RAIN, rain_band=3)


if __name__ == "__main__":
    unittest.main()

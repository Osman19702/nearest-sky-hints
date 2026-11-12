"""Tests of the GRIB2 reader: a hand-built message with simple packing, and a real GFS field.

Run all tests with: python3 -I -m unittest discover -s tests
"""
import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from hintmap import classify, grib2  # noqa: E402


def section(number, body):
    return struct.pack(">IB", 5 + len(body), number) + body


def sign_magnitude(value, size):
    magnitude = abs(value)
    return (magnitude | (1 << (size * 8 - 1)) if value < 0 else magnitude).to_bytes(size, "big")


def simple_message(values, bits, reference, binary_scale=0, decimal_scale=0, ni=4, nj=3, category=0, number=0, hour=6):
    """A GRIB2 message on a small 0.25 degree grid starting at 2 N, 0 E, values packed with template 5.0."""
    grid = (struct.pack(">BIBBH", 0, ni * nj, 0, 0, 0)
            + bytes([6, 0]) + (0).to_bytes(4, "big") + bytes([0]) + (0).to_bytes(4, "big") + bytes([0]) + (0).to_bytes(4, "big")
            + struct.pack(">II", ni, nj) + struct.pack(">II", 0, 0xFFFFFFFF)
            + sign_magnitude(2000000, 4) + sign_magnitude(0, 4) + bytes([48])
            + sign_magnitude(2000000 - (nj - 1) * 250000, 4) + sign_magnitude((ni - 1) * 250000, 4)
            + struct.pack(">II", 250000, 250000) + bytes([0]))
    product = struct.pack(">HH", 0, 0) + bytes([category, number, 2, 0, 96]) + struct.pack(">HB", 0, 0) + bytes([1]) + struct.pack(">I", hour) \
        + bytes([1, 0]) + (0).to_bytes(4, "big") + bytes([255, 0]) + (0).to_bytes(4, "big")
    packing = struct.pack(">IH", ni * nj, 0) + struct.pack(">f", reference) + sign_magnitude(binary_scale, 2) \
        + sign_magnitude(decimal_scale, 2) + bytes([bits, 0])
    stream, used = 0, 0
    for value in values:
        stream = (stream << bits) | value
        used += bits
    padding = (-used) % 8
    data = (stream << padding).to_bytes((used + padding) // 8, "big") if used else b""
    identification = struct.pack(">HHBBBHBBBBBBB", 7, 0, 2, 1, 1, 2026, 11, 12, 6, 0, 0, 0, 1)
    body = (section(1, identification) + section(3, grid) + section(4, product) + section(5, packing) + section(6, bytes([255]))
            + section(7, data) + b"7777")
    return b"GRIB" + b"\0\0" + bytes([0, 2]) + (16 + len(body)).to_bytes(8, "big") + body


class UnpackTests(unittest.TestCase):
    def test_unpacks_values_across_byte_boundaries(self):
        values = [0, 1, 1023, 512, 7, 300, 1000, 0, 5, 1023, 2, 999] * 40
        stream = 0
        for value in values:
            stream = (stream << 10) | value
        data = stream.to_bytes(len(values) * 10 // 8, "big")
        self.assertEqual(grib2.unpack_bits(data, 0, len(values), 10), values)
        self.assertEqual(grib2.unpack_bits(data, 10, 3, 10), values[1:4])
        self.assertEqual(grib2.unpack_bits(data, 0, 5, 0), [0] * 5)

    def test_refuses_to_read_past_the_end(self):
        with self.assertRaises(grib2.GribError):
            grib2.unpack_bits(b"\xff", 0, 2, 8)


class SimplePackingTests(unittest.TestCase):
    def test_decodes_a_hand_built_message(self):
        packed = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
        data = simple_message(packed, bits=4, reference=270.0, binary_scale=-1, decimal_scale=0)
        message = next(grib2.messages(data))
        self.assertEqual((message.discipline, message.category, message.number, message.forecast_time, message.time_unit), (0, 0, 0, 6, 1))
        self.assertEqual(message.reference_time, (2026, 11, 12, 6, 0, 0))
        self.assertEqual((message.ni, message.nj), (4, 3))
        self.assertAlmostEqual(message.la1, 2.0)
        self.assertAlmostEqual(message.di, 0.25)
        self.assertEqual(message.values(), [270.0 + value * 0.5 for value in packed])
        grid = classify.Grid.of(message)
        self.assertEqual(grid.index(2.0, 0.0), 0)
        self.assertEqual(grid.index(1.5, 0.75), 2 * 4 + 3)
        self.assertEqual(grid.centre(2 * 4 + 3), (1.5, 0.75))

    def test_decimal_scale_and_a_constant_field(self):
        message = next(grib2.messages(simple_message([3, 0, 1, 2] * 3, bits=2, reference=10.0, decimal_scale=1)))
        self.assertEqual([round(value, 6) for value in message.values()], [1.3, 1.0, 1.1, 1.2] * 3)
        constant = next(grib2.messages(simple_message([], bits=0, reference=5.0)))
        self.assertEqual(constant.values(), [5.0] * 12)

    def test_reads_several_messages_and_skips_stray_bytes(self):
        first = simple_message([1] * 12, bits=1, reference=0.0, category=1, number=192)
        second = simple_message([0] * 12, bits=1, reference=0.0, category=1, number=195)
        found = list(grib2.messages(first + b"\n\n" + second))
        self.assertEqual([(message.category, message.number) for message in found], [(1, 192), (1, 195)])

    def test_rejects_damaged_messages(self):
        data = simple_message([1] * 12, bits=1, reference=0.0)
        with self.assertRaises(grib2.GribError):
            list(grib2.messages(data[:-1]))
        with self.assertRaises(grib2.GribError):
            list(grib2.messages(data[:7] + b"\x01" + data[8:]))


class RealFieldTests(unittest.TestCase):
    """GFS 0.25, run 2026-11-11 12Z, +9 h: categorical freezing rain (complex packing with spatial differencing)."""

    def test_decodes_a_real_gfs_field(self):
        with open(os.path.join(ROOT, "tests", "fixtures", "gfs2026111112_f009_cfrzr.grib2"), "rb") as source:
            message = next(grib2.messages(source.read()))
        self.assertEqual((message.category, message.number, message.packing_template), (1, 193, 3))
        self.assertEqual((message.ni, message.nj), (1440, 721))
        values = message.values()
        self.assertEqual(len(values), 1440 * 721)
        # A yes/no field: a decoding error would leave values other than 0 and 1.
        self.assertTrue(all(value in (0.0, 1.0) for value in values))
        ones = [index for index, value in enumerate(values) if value == 1.0]
        self.assertEqual(len(ones), 121)
        self.assertEqual(ones[:4], [124052, 128337, 139208, 140647])
        self.assertEqual(ones[-2:], [875344, 875345])
        grid = classify.Grid.of(message)
        # Freezing rain over the Northwest Territories of Canada that November evening.
        self.assertEqual(grid.centre(142088), (65.5, -118.0))


if __name__ == "__main__":
    unittest.main()
